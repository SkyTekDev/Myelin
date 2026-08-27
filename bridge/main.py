from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any

import jpype
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select

from myelin import Myelin
from myelin.helpers.utils import ProviderDataError
from myelin.ioce import IoceClient
from myelin.pricers.opsf import OPSF, OPSFProvider

from bridge.api_models import IoceClaimRequest
from bridge.claim_mapper import to_myelin_claim
from bridge.opps_client import WindowsSafeOppsClient


BRIDGE_VERSION = "0.4.0"

BASE_DIR = Path(__file__).resolve().parent.parent
JAR_PATH = Path(
    os.getenv("MYELIN_JAR_PATH", str(BASE_DIR / "jars"))
).expanduser().resolve()
DB_PATH = Path(
    os.getenv("MYELIN_DB_PATH", str(BASE_DIR / "data" / "myelin.db"))
).expanduser().resolve()
PRICER_PATH = JAR_PATH / "pricers"

logger = logging.getLogger("qon_ioce_opps_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


DOWNLOAD_CMS_ASSETS = env_bool(
    "MYELIN_DOWNLOAD_CMS_ASSETS",
    default=False,
)


def find_opps_jar() -> Path:
    configured_path = os.getenv("OPPS_JAR_PATH")

    if configured_path:
        jar_path = Path(configured_path).expanduser().resolve()

        if not jar_path.is_file():
            raise FileNotFoundError(
                "OPPS_JAR_PATH does not point to an existing file: "
                f"{jar_path}"
            )

        return jar_path

    if not PRICER_PATH.is_dir():
        raise FileNotFoundError(
            f"CMS Pricer directory was not found: {PRICER_PATH}"
        )

    matches = sorted(
        (
            path
            for path in PRICER_PATH.glob("*.jar")
            if "opps-pricer" in path.name.lower()
        ),
        key=lambda path: path.name.lower(),
    )

    if not matches:
        raise FileNotFoundError(
            "No OPPS Pricer JAR containing 'opps-pricer' was found in "
            f"{PRICER_PATH}"
        )

    if len(matches) > 1:
        available = "\n".join(
            f"  - {path.name}" for path in matches
        )

        raise RuntimeError(
            "More than one OPPS Pricer JAR was found. Set OPPS_JAR_PATH "
            f"to the exact JAR to use:\n{available}"
        )

    return matches[0].resolve()


def get_opsf_record_count(myelin_engine: Myelin) -> int:
    opsf_db = myelin_engine.db_manager.opsf_db

    if opsf_db is None:
        raise RuntimeError(
            "Myelin's OPSF database was not initialized."
        )

    with opsf_db.session() as session:
        count = session.execute(
            select(func.count()).select_from(OPSF)
        ).scalar_one()

    return int(count)


def serialize_model(model: Any) -> Any:
    if model is None:
        return None

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")

    return model


def map_ioce_lines(
    payload: IoceClaimRequest,
    ioce_output: Any,
) -> list[dict[str, Any]]:
    output_lines = ioce_output.line_item_list or []
    results: list[dict[str, Any]] = []

    for index, input_line in enumerate(payload.claimLines):
        output = (
            serialize_model(output_lines[index])
            if index < len(output_lines)
            else None
        )

        results.append(
            {
                "claimLineId": input_line.claimLineId,
                "lineNumber": input_line.lineNumber,
                "output": output,
            }
        )

    return results


def map_opps_lines(
    payload: IoceClaimRequest,
    opps_output: Any,
) -> list[dict[str, Any]]:
    service_lines = opps_output.service_lines or []
    input_by_line_number = {
        line.lineNumber: line
        for line in payload.claimLines
    }

    results: list[dict[str, Any]] = []

    for output_line in service_lines:
        output_line_number = output_line.line_number

        input_line = (
            input_by_line_number.get(output_line_number)
            if output_line_number is not None
            else None
        )

        # The CMS Pricer generally numbers lines in input order. Fall back to
        # that position if the request uses nonsequential lineNumber values.
        if (
            input_line is None
            and output_line_number is not None
            and 1 <= output_line_number <= len(payload.claimLines)
        ):
            input_line = payload.claimLines[output_line_number - 1]

        results.append(
            {
                "claimLineId": (
                    input_line.claimLineId
                    if input_line is not None
                    else None
                ),
                "lineNumber": (
                    input_line.lineNumber
                    if input_line is not None
                    else output_line_number
                ),
                "pricerLineNumber": output_line_number,
                "output": serialize_model(output_line),
            }
        )

    return results


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start one JVM, one I/OCE client, and one isolated OPPS Pricer client.

    Do not use `with Myelin(...)`. Myelin.__enter__() initializes every
    discovered grouper and pricer, including modules not needed by this API.
    """
    logger.info("Starting QON CMS I/OCE + OPPS API")
    logger.info("Myelin JAR path: %s", JAR_PATH)
    logger.info("Myelin database path: %s", DB_PATH)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    myelin_engine: Myelin | None = None

    try:
        if not DB_PATH.is_file():
            raise RuntimeError(
                "The Myelin SQLite database was not found: "
                f"{DB_PATH}. Build and populate it before creating "
                "the container image."
            )

        root_jars = list(JAR_PATH.glob("*.jar"))

        if not root_jars:
            raise RuntimeError(
                "No CMS editor/runtime JARs were found directly under "
                f"{JAR_PATH}. Build the JAR environment before creating "
                "the container image."
            )

        # In a container, the CMS JARs and provider database are baked into
        # the image. Avoid downloading CMS assets every time a task starts.
        myelin_engine = Myelin(
            build_jar_dirs=DOWNLOAD_CMS_ASSETS,
            build_db=False,
            jar_path=str(JAR_PATH),
            db_path=str(DB_PATH),
        )

        if not DOWNLOAD_CMS_ASSETS:
            # Myelin only starts the JVM automatically when build_jar_dirs
            # is true. Start it from the existing, immutable JAR directory.
            # This calls Myelin's existing JVM setup and registers cleanup
            # with its ExitStack.
            myelin_engine._setup_jvm()  # noqa: SLF001

        if myelin_engine.db_manager.engine is None:
            raise RuntimeError(
                "Myelin's database engine was not initialized."
            )

        opsf_record_count = get_opsf_record_count(myelin_engine)

        if opsf_record_count == 0:
            raise RuntimeError(
                "The OPSF provider table is empty. Run "
                "'python run_ioce.py --refresh-opsf' before starting the API."
            )

        opps_jar = find_opps_jar()

        logger.info(
            "Using %s existing OPSF provider records.",
            f"{opsf_record_count:,}",
        )
        logger.info("Using OPPS Pricer JAR: %s", opps_jar)

        ioce_client = IoceClient()

        opps_client = WindowsSafeOppsClient(
            jar_path=str(opps_jar),
            db=myelin_engine.db_manager.engine,
            logger=logger,
        )

        app.state.myelin_engine = myelin_engine
        app.state.ioce_client = ioce_client
        app.state.opps_client = opps_client
        app.state.opps_jar = opps_jar
        app.state.opsf_record_count = opsf_record_count

        # Serialize the complete I/OCE -> OPSF -> OPPS flow until the CMS
        # Java components have been concurrency-tested.
        app.state.processing_lock = Lock()

        logger.info("QON CMS I/OCE + OPPS API is ready")

        yield

    finally:
        logger.info("Stopping QON CMS I/OCE + OPPS API")

        if myelin_engine is not None:
            myelin_engine.cleanup()


app = FastAPI(
    title="QON CMS I/OCE and OPPS Pricer API",
    description=(
        "Runs an outpatient claim through CMS I/OCE and, when requested, "
        "the CMS OPPS Pricer using CMS OPSF provider data."
    ),
    version=BRIDGE_VERSION,
    lifespan=lifespan,
)


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {
        "status": "live",
        "bridgeVersion": BRIDGE_VERSION,
    }


@app.get("/health/ready")
def health_ready(request: Request) -> dict[str, Any]:
    ioce_client = getattr(request.app.state, "ioce_client", None)
    opps_client = getattr(request.app.state, "opps_client", None)

    if (
        ioce_client is None
        or opps_client is None
        or not jpype.isJVMStarted()
    ):
        raise HTTPException(
            status_code=503,
            detail="CMS I/OCE or OPPS Pricer is not ready.",
        )

    return {
        "status": "ready",
        "bridgeVersion": BRIDGE_VERSION,
        "jvmStarted": True,
        "ioceClientInitialized": True,
        "oppsClientInitialized": True,
        "oppsJar": request.app.state.opps_jar.name,
        "opsfRecordCount": request.app.state.opsf_record_count,
        "downloadsCmsAssetsAtStartup": DOWNLOAD_CMS_ASSETS,
    }


@app.get("/api/v1/version")
def version(request: Request) -> dict[str, Any]:
    return {
        "bridgeVersion": BRIDGE_VERSION,
        "oppsJar": request.app.state.opps_jar.name,
        "opsfRecordCount": request.app.state.opsf_record_count,
        "jarPath": str(JAR_PATH),
        "databasePath": str(DB_PATH),
        "downloadsCmsAssetsAtStartup": DOWNLOAD_CMS_ASSETS,
    }


@app.post("/api/v1/ioce/process")
def process_ioce(
    payload: IoceClaimRequest,
    request: Request,
) -> dict[str, Any]:
    ioce_client: IoceClient = request.app.state.ioce_client
    processing_lock: Lock = request.app.state.processing_lock

    logger.info(
        "Processing I/OCE claimId=%s with %s line(s)",
        payload.claimId,
        len(payload.claimLines),
    )

    try:
        claim = to_myelin_claim(payload)
        started = perf_counter()

        with processing_lock:
            ioce_output = ioce_client.process(
                claim,
                include_descriptions=True,
            )

        elapsed_ms = round(
            (perf_counter() - started) * 1000,
            2,
        )

        return {
            "success": True,
            "claimId": payload.claimId,
            "patientControlNumber": payload.patientControlNumber,
            "engine": {
                "name": "CMS I/OCE",
                "bridgeVersion": BRIDGE_VERSION,
                "ioceVersion": (
                    ioce_output.version
                    or ioce_output.processing_information.version
                ),
                "internalVersion": (
                    ioce_output.processing_information.internal_version
                ),
            },
            "processing": {
                "elapsedMilliseconds": elapsed_ms,
                "returnCode": serialize_model(
                    ioce_output.processing_information.return_code
                ),
                "claimProcessedFlag": (
                    ioce_output.claim_processed_flag
                ),
                "claimProcessedFlagDescription": (
                    ioce_output.claim_processed_flag_description
                ),
                "inputLineCount": len(payload.claimLines),
                "outputLineCount": len(
                    ioce_output.line_item_list or []
                ),
            },
            "lineResults": map_ioce_lines(
                payload,
                ioce_output,
            ),
            "ioceResult": serialize_model(ioce_output),
        }

    except Exception as ex:
        logger.exception(
            "CMS I/OCE processing failed for claimId=%s",
            payload.claimId,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "message": "CMS I/OCE processing failed.",
                "claimId": str(payload.claimId),
                "exceptionType": type(ex).__name__,
                "exceptionMessage": str(ex),
            },
        ) from ex


@app.post("/api/v1/opps/process")
def process_opps(
    payload: IoceClaimRequest,
    request: Request,
) -> dict[str, Any]:
    ioce_client: IoceClient = request.app.state.ioce_client
    opps_client: WindowsSafeOppsClient = (
        request.app.state.opps_client
    )
    processing_lock: Lock = request.app.state.processing_lock
    myelin_engine: Myelin = request.app.state.myelin_engine

    logger.info(
        "Processing I/OCE + OPPS claimId=%s with %s line(s)",
        payload.claimId,
        len(payload.claimLines),
    )

    try:
        claim = to_myelin_claim(payload)
        started = perf_counter()

        with processing_lock:
            ioce_output = ioce_client.process(
                claim,
                include_descriptions=True,
            )

            provider = OPSFProvider()
            provider.from_claim(
                claim,
                myelin_engine.db_manager.engine,
            )

            opps_output, resolved_provider = opps_client.process(
                claim,
                provider,
                ioce_output,
            )

        elapsed_ms = round(
            (perf_counter() - started) * 1000,
            2,
        )

        return {
            "success": True,
            "claimId": payload.claimId,
            "patientControlNumber": payload.patientControlNumber,
            "engine": {
                "bridgeVersion": BRIDGE_VERSION,
                "ioceVersion": (
                    ioce_output.version
                    or ioce_output.processing_information.version
                ),
                "ioceInternalVersion": (
                    ioce_output.processing_information.internal_version
                ),
                "oppsJar": request.app.state.opps_jar.name,
                "oppsCalculationVersion": (
                    opps_output.calculation_version
                ),
            },
            "processing": {
                "elapsedMilliseconds": elapsed_ms,
                "ioceReturnCode": serialize_model(
                    ioce_output.processing_information.return_code
                ),
                "oppsReturnCode": serialize_model(
                    opps_output.return_code
                ),
                "claimProcessedFlag": (
                    ioce_output.claim_processed_flag
                ),
                "inputLineCount": len(payload.claimLines),
                "ioceOutputLineCount": len(
                    ioce_output.line_item_list or []
                ),
                "oppsOutputLineCount": len(
                    opps_output.service_lines or []
                ),
            },
            "pricingSummary": {
                "totalClaimCharges": (
                    opps_output.total_claim_charges
                ),
                "totalClaimPayment": (
                    opps_output.total_claim_payment
                ),
                "totalClaimOutlierPayment": (
                    opps_output.total_claim_outlier_payment
                ),
                "totalClaimDeductible": (
                    opps_output.total_claim_deductible
                ),
                "bloodDeductible": (
                    opps_output.blood_deductible
                ),
                "bloodPintsUsed": (
                    opps_output.blood_pints_used
                ),
                "finalCbsa": opps_output.final_cbsa,
                "finalWageIndex": (
                    opps_output.final_wage_index
                ),
            },
            "ioceLineResults": map_ioce_lines(
                payload,
                ioce_output,
            ),
            "pricingLineResults": map_opps_lines(
                payload,
                opps_output,
            ),
            "opsfProvider": serialize_model(
                resolved_provider
            ),
            "ioceResult": serialize_model(ioce_output),
            "oppsResult": serialize_model(opps_output),
        }

    except ProviderDataError as ex:
        logger.warning(
            "OPSF provider lookup failed for claimId=%s: %s",
            payload.claimId,
            ex.description,
        )

        raise HTTPException(
            status_code=422,
            detail={
                "message": "OPSF provider lookup failed.",
                "claimId": str(payload.claimId),
                "code": ex.code,
                "description": ex.description,
                "explanation": ex.explanation,
            },
        ) from ex

    except Exception as ex:
        logger.exception(
            "CMS I/OCE + OPPS processing failed for claimId=%s",
            payload.claimId,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "message": "CMS I/OCE or OPPS processing failed.",
                "claimId": str(payload.claimId),
                "exceptionType": type(ex).__name__,
                "exceptionMessage": str(ex),
            },
        ) from ex
