from __future__ import annotations

import jpype
import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from myelin import Myelin
from myelin.helpers.claim_examples import opps_claim_example
from myelin.ioce import IoceClient
from myelin.pricers.opps import OppsClient
from myelin.pricers.opsf import OPSF, OPSFProvider


BASE_DIR = Path(__file__).resolve().parent
JAR_DIR = BASE_DIR / "jars"
PRICER_DIR = JAR_DIR / "pricers"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "myelin.db"
OUTPUT_PATH = BASE_DIR / "ioce_opps_output.json"

logger = logging.getLogger("run_ioce")

from myelin.pricers.url_loader import UrlLoader


class WindowsSafeOppsClient(OppsClient):
    def __init__(
        self,
        jar_path: str | None = None,
        db=None,
        logger=None,
    ):
        if not jpype.isJVMStarted():
            raise RuntimeError(
                "JVM is not started. "
                "Please start the JVM before using OppsClient."
            )

        if jar_path is None:
            raise ValueError("jar_path must be provided to OppsClient")

        resolved_jar = Path(jar_path).resolve()

        if not resolved_jar.is_file():
            raise ValueError(
                f"jar_path does not exist: {resolved_jar}"
            )

        self.logger = logger or logging.getLogger("OppsClient")
        self.url_loader = UrlLoader()

        jar_uri = resolved_jar.as_uri()

        self.logger.info(
            "Loading OPPS Pricer from URI: %s",
            jar_uri,
        )

        self.url_loader.load_urls([jar_uri])

        self.db = db

        self.load_classes()

        try:
            run_client_load_classes(self)
        except Exception:
            pass

        self.pricer_setup()

        try:
            apply_client_methods(self)
        except Exception:
            pass


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Myelin's sample outpatient claim through CMS I/OCE and "
            "then the CMS OPPS Pricer."
        )
    )

    parser.add_argument(
        "--refresh-opsf",
        action="store_true",
        help="Download and fully reload the CMS OPSF provider table.",
    )

    parser.add_argument(
        "--skip-opps",
        action="store_true",
        help="Run I/OCE only and do not call the OPPS Pricer.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"JSON output path. Default: {OUTPUT_PATH}",
    )

    return parser.parse_args()


def find_opps_jar() -> Path:
    """
    Find the OPPS Pricer JAR without placing all pricer JARs on the JVM's
    primary classpath.

    Set OPPS_JAR_PATH to explicitly choose a JAR when multiple versions exist.
    """
    configured_path = os.getenv("OPPS_JAR_PATH")

    if configured_path:
        jar_path = Path(configured_path).expanduser().resolve()

        if not jar_path.is_file():
            raise FileNotFoundError(
                f"OPPS_JAR_PATH does not point to an existing file: {jar_path}"
            )

        return jar_path

    if not PRICER_DIR.is_dir():
        raise FileNotFoundError(
            f"CMS pricer directory was not found: {PRICER_DIR}"
        )

    matches = sorted(
        (
            path
            for path in PRICER_DIR.glob("*.jar")
            if "opps-pricer" in path.name.lower()
        ),
        key=lambda path: path.name.lower(),
    )

    if not matches:
        raise FileNotFoundError(
            f"No OPPS Pricer JAR containing 'opps-pricer' was found in "
            f"{PRICER_DIR}"
        )

    if len(matches) > 1:
        available = "\n".join(f"  - {path.name}" for path in matches)

        raise RuntimeError(
            "More than one OPPS Pricer JAR was found. Set the OPPS_JAR_PATH "
            f"environment variable to the version you want to use:\n{available}"
        )

    return matches[0].resolve()


def get_opsf_record_count(myelin_engine: Myelin) -> int:
    opsf_db = myelin_engine.db_manager.opsf_db

    if opsf_db is None:
        raise RuntimeError("Myelin's OPSF database was not initialized.")

    with opsf_db.session() as session:
        record_count = session.execute(
            select(func.count()).select_from(OPSF)
        ).scalar_one()

    return int(record_count)


def ensure_opsf_data(
    myelin_engine: Myelin,
    refresh_opsf: bool,
) -> int:
    """
    The OPPS Pricer needs provider-specific data from the CMS OPSF table.

    Populate it on the first run, or reload it when --refresh-opsf is supplied.
    """
    opsf_db = myelin_engine.db_manager.opsf_db

    if opsf_db is None:
        raise RuntimeError("Myelin's OPSF database was not initialized.")

    record_count = get_opsf_record_count(myelin_engine)

    if refresh_opsf or record_count == 0:
        reason = (
            "--refresh-opsf was specified"
            if refresh_opsf
            else "the OPSF table is empty"
        )

        logger.info(
            "Downloading and loading CMS OPSF provider data because %s.",
            reason,
        )

        record_count = opsf_db.populate(
            download=True,
            truncate=True,
        )

        logger.info("Loaded %s OPSF provider records.", f"{record_count:,}")
    else:
        logger.info(
            "Using %s existing OPSF provider records.",
            f"{record_count:,}",
        )

    return record_count


def serialize_model(model: Any) -> Any:
    if model is None:
        return None

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")

    return model


def run(args: argparse.Namespace) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Construct Myelin directly. Do not use:
    #
    #     with Myelin(...) as myelin_engine:
    #
    # Myelin.__enter__() initializes every discovered grouper and pricer.
    # This script intentionally starts the JVM, then initializes only
    # IoceClient and OppsClient.
    myelin_engine = Myelin(
        build_jar_dirs=True,
        build_db=False,
        jar_path=str(JAR_DIR),
        db_path=str(DB_PATH),
    )

    try:
        if myelin_engine.db_manager.engine is None:
            raise RuntimeError("Myelin's database engine was not initialized.")

        ioce_client = IoceClient()

        claim = opps_claim_example()
        claim.claimid = "QON_OPPS_TEST_001"

        logger.info("Processing claim %s through CMS I/OCE.", claim.claimid)

        ioce_output = ioce_client.process(
            claim,
            include_descriptions=True,
        )

        combined_result: dict[str, Any] = {
            "success": True,
            "claimId": claim.claimid,
            "input": {
                "billType": claim.bill_type,
                "fromDate": (
                    claim.from_date.isoformat()
                    if hasattr(claim.from_date, "isoformat")
                    else str(claim.from_date)
                ),
                "throughDate": (
                    claim.thru_date.isoformat()
                    if hasattr(claim.thru_date, "isoformat")
                    else str(claim.thru_date)
                ),
                "providerCcn": (
                    claim.billing_provider.other_id
                    if claim.billing_provider is not None
                    else None
                ),
                "lineCount": len(claim.lines),
            },
            "ioce": serialize_model(ioce_output),
            "opps": None,
            "opsfProvider": None,
        }

        if args.skip_opps:
            logger.info("Skipping OPPS pricing because --skip-opps was specified.")
            return combined_result

        ensure_opsf_data(
            myelin_engine=myelin_engine,
            refresh_opsf=args.refresh_opsf,
        )

        opps_jar = find_opps_jar()

        logger.info("Using OPPS Pricer JAR: %s", opps_jar)

        # OppsClient uses its own URL classloader. Do not add jars/pricers/*
        # to Myelin's extra_classpaths; that can trigger duplicate Java class
        # definition errors with other CMS pricers.
        opps_client = WindowsSafeOppsClient(
            jar_path=str(opps_jar),
            db=myelin_engine.db_manager.engine,
            logger=logger,
        )

        provider = OPSFProvider()
        provider.from_claim(
            claim,
            myelin_engine.db_manager.engine,
        )

        logger.info("Processing claim %s through CMS OPPS Pricer.", claim.claimid)

        opps_output, resolved_provider = opps_client.process(
            claim,
            provider,
            ioce_output,
        )

        combined_result["opps"] = serialize_model(opps_output)
        combined_result["opsfProvider"] = serialize_model(resolved_provider)

        return combined_result

    finally:
        myelin_engine.cleanup()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    args = parse_arguments()

    try:
        result = run(args)

        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        output_json = json.dumps(
            result,
            indent=2,
            default=str,
        )

        output_path.write_text(
            output_json,
            encoding="utf-8",
        )

        print(output_json)
        print()
        print(f"Output written to: {output_path}")

    except Exception:
        logger.exception("I/OCE and OPPS processing failed.")
        raise


if __name__ == "__main__":
    main()
