from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import jpype
from sqlalchemy import Engine, func, select

from myelin import DrgClient, IPSFProvider, Myelin
from myelin.helpers.claim_examples import claim_example
from myelin.plugins import apply_client_methods, run_client_load_classes
from myelin.pricers.ipps import IppsClient
from myelin.pricers.ipsf import IPSF
from myelin.pricers.url_loader import UrlLoader


BASE_DIR = Path(__file__).resolve().parent
JAR_DIR = BASE_DIR / "jars"
PRICER_DIR = JAR_DIR / "pricers"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "myelin.db"
OUTPUT_PATH = BASE_DIR / "drg_ipps_output.json"

logger = logging.getLogger("run_ipps")


class WindowsSafeIppsClient(IppsClient):
    """
    IPPS client that uses a normalized file URI for the isolated Java
    URLClassLoader.

    This avoids the Windows file-path issue caused by constructing a Java URL
    directly from a path such as C:\\Users\\...\\ipps-pricer.jar.

    The IPPS JAR is intentionally NOT added to Myelin's primary JVM classpath.
    Myelin's IPPS client uses an isolated class loader and Javassist to modify
    a few IPPS classes during startup.
    """

    def __init__(
        self,
        jar_path: str | None = None,
        db: Engine | None = None,
        logger: logging.Logger | None = None,
    ):
        if not jpype.isJVMStarted():
            raise RuntimeError(
                "JVM is not started. Start Myelin before creating "
                "WindowsSafeIppsClient."
            )

        if jar_path is None:
            raise ValueError(
                "jar_path must be provided to WindowsSafeIppsClient."
            )

        resolved_jar = Path(jar_path).expanduser().resolve()

        if not resolved_jar.is_file():
            raise ValueError(
                f"IPPS Pricer JAR does not exist: {resolved_jar}"
            )

        self.logger = logger or logging.getLogger("WindowsSafeIppsClient")
        self.url_loader = UrlLoader()

        jar_uri = resolved_jar.as_uri()

        self.logger.info(
            "Loading IPPS Pricer from URI: %s",
            jar_uri,
        )

        # Keep the IPPS Pricer isolated from Myelin's primary JVM classpath.
        self.url_loader.load_urls([jar_uri])

        self.db = db

        # IppsClient modifies a few classes with Javassist before loading the
        # rest of the Pricer classes. Preserve Myelin's initialization order.
        self.add_hmo(str(resolved_jar))
        self.load_classes()

        try:
            run_client_load_classes(self)
        except Exception:
            self.logger.debug(
                "No IPPS plugin class-loading extensions were applied.",
                exc_info=True,
            )

        self.pricer_setup()

        try:
            apply_client_methods(self)
        except Exception:
            self.logger.debug(
                "No IPPS plugin methods were applied.",
                exc_info=True,
            )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Myelin sample inpatient claim through the CMS MS-DRG "
            "Grouper and CMS IPPS Pricer."
        )
    )

    parser.add_argument(
        "--provider-number",
        default="010001",
        help="Six-character CMS provider CCN. Default: 010001",
    )

    parser.add_argument(
        "--total-charges",
        type=float,
        default=50000.00,
        help="Covered claim charges used by the IPPS Pricer. Default: 50000",
    )

    parser.add_argument(
        "--refresh-ipsf",
        action="store_true",
        help="Download and completely reload the CMS IPSF provider table.",
    )

    parser.add_argument(
        "--skip-ipps",
        action="store_true",
        help="Run the MS-DRG Grouper only and do not call the IPPS Pricer.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"JSON output path. Default: {OUTPUT_PATH}",
    )

    return parser.parse_args()


def find_ipps_jar() -> Path:
    """
    Locate the IPPS executable JAR.

    Set IPPS_JAR_PATH when more than one IPPS Pricer JAR is present.
    """
    configured_path = os.getenv("IPPS_JAR_PATH")

    if configured_path:
        jar_path = Path(configured_path).expanduser().resolve()

        if not jar_path.is_file():
            raise FileNotFoundError(
                "IPPS_JAR_PATH does not point to an existing file: "
                f"{jar_path}"
            )

        return jar_path

    if not PRICER_DIR.is_dir():
        raise FileNotFoundError(
            f"CMS Pricer directory was not found: {PRICER_DIR}"
        )

    matches = sorted(
        (
            path
            for path in PRICER_DIR.glob("*.jar")
            if "ipps-pricer" in path.name.lower()
        ),
        key=lambda path: path.name.lower(),
    )

    if not matches:
        raise FileNotFoundError(
            "No IPPS Pricer JAR containing 'ipps-pricer' was found in "
            f"{PRICER_DIR}"
        )

    if len(matches) > 1:
        available = "\n".join(
            f"  - {path.name}" for path in matches
        )

        raise RuntimeError(
            "More than one IPPS Pricer JAR was found. Set IPPS_JAR_PATH "
            f"to the exact JAR to use:\n{available}"
        )

    return matches[0].resolve()


def get_ipsf_record_count(myelin_engine: Myelin) -> int:
    ipsf_db = myelin_engine.db_manager.ipsf_db

    if ipsf_db is None:
        raise RuntimeError(
            "Myelin's IPSF database was not initialized."
        )

    with ipsf_db.session() as session:
        count = session.execute(
            select(func.count()).select_from(IPSF)
        ).scalar_one()

    return int(count)


def ensure_ipsf_data(
    myelin_engine: Myelin,
    refresh_ipsf: bool,
) -> int:
    """
    Ensure the inpatient provider-specific file is available for pricing.
    """
    ipsf_db = myelin_engine.db_manager.ipsf_db

    if ipsf_db is None:
        raise RuntimeError(
            "Myelin's IPSF database was not initialized."
        )

    record_count = get_ipsf_record_count(myelin_engine)

    if refresh_ipsf or record_count == 0:
        reason = (
            "--refresh-ipsf was specified"
            if refresh_ipsf
            else "the IPSF table is empty"
        )

        logger.info(
            "Downloading and loading CMS IPSF provider data because %s.",
            reason,
        )

        record_count = ipsf_db.populate(
            download=True,
            truncate=True,
        )

        logger.info(
            "Loaded %s IPSF provider records.",
            f"{record_count:,}",
        )
    else:
        logger.info(
            "Using %s existing IPSF provider records.",
            f"{record_count:,}",
        )

    return record_count


def serialize_model(model: Any) -> Any:
    if model is None:
        return None

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")

    return model


def build_test_claim(
    provider_number: str,
    total_charges: float,
):
    """
    Start with Myelin's inpatient example and make the pricing inputs explicit.
    """
    claim = claim_example()

    claim.claimid = "QON_IPPS_TEST_001"
    claim.bill_type = "111"

    if claim.billing_provider is None:
        raise RuntimeError(
            "Myelin's inpatient example did not create a billing provider."
        )

    claim.billing_provider.other_id = provider_number

    # IPPS uses claim.total_charges as CoveredCharges. The standard Myelin
    # example does not set a meaningful amount, so set one for this test.
    claim.total_charges = total_charges

    # Make the optional IPPS values explicit for reproducibility.
    if not isinstance(claim.additional_data, dict):
        claim.additional_data = {}

    claim.additional_data["ipps"] = {
        "review_code": "00",
        "lifetime_reserve_days": 0,
        "midnight_adjustment_geolocation": "",
    }

    return claim


def run(args: argparse.Namespace) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Construct Myelin directly. Do NOT use:
    #
    #     with Myelin(...) as myelin_engine:
    #
    # __enter__() initializes every editor, grouper, and detected Pricer.
    # For this test we intentionally initialize only DrgClient and IPPS.
    myelin_engine = Myelin(
        build_jar_dirs=True,
        build_db=False,
        jar_path=str(JAR_DIR),
        db_path=str(DB_PATH),
    )

    try:
        if myelin_engine.db_manager.engine is None:
            raise RuntimeError(
                "Myelin's database engine was not initialized."
            )

        claim = build_test_claim(
            provider_number=args.provider_number,
            total_charges=args.total_charges,
        )

        logger.info(
            "Claim ID: %s",
            claim.claimid,
        )
        logger.info(
            "Provider CCN: %s",
            claim.billing_provider.other_id
            if claim.billing_provider is not None
            else None,
        )
        logger.info(
            "Claim dates: %s through %s",
            claim.from_date,
            claim.thru_date,
        )
        logger.info(
            "Total covered charges: %.2f",
            claim.total_charges,
        )

        # DRG classes are loaded from the normal Myelin JAR classpath.
        logger.info(
            "Initializing CMS MS-DRG Grouper."
        )
        drg_client = DrgClient()

        logger.info(
            "Processing claim %s through CMS MS-DRG Grouper.",
            claim.claimid,
        )

        drg_output = drg_client.process(claim)

        logger.info(
            "MS-DRG result: version=%s final_drg=%s final_mdc=%s severity=%s",
            drg_output.drg_version,
            drg_output.final_drg_value,
            drg_output.final_mdc_value,
            drg_output.final_severity,
        )

        combined_result: dict[str, Any] = {
            "success": True,
            "claimId": claim.claimid,
            "input": {
                "billType": claim.bill_type,
                "providerCcn": (
                    claim.billing_provider.other_id
                    if claim.billing_provider is not None
                    else None
                ),
                "admitDate": (
                    claim.admit_date.isoformat()
                    if hasattr(claim.admit_date, "isoformat")
                    else str(claim.admit_date)
                ),
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
                "lengthOfStay": claim.los,
                "patientStatus": claim.patient_status,
                "totalCharges": claim.total_charges,
                "principalDiagnosis": (
                    claim.principal_dx.code
                    if claim.principal_dx is not None
                    else None
                ),
                "secondaryDiagnoses": [
                    diagnosis.code
                    for diagnosis in claim.secondary_dxs
                ],
                "inpatientProcedures": [
                    procedure.code
                    for procedure in claim.inpatient_pxs
                ],
            },
            "msdrg": serialize_model(drg_output),
            "ipsfProvider": None,
            "ipps": None,
        }

        if args.skip_ipps:
            logger.info(
                "Skipping IPPS pricing because --skip-ipps was specified."
            )
            return combined_result

        ensure_ipsf_data(
            myelin_engine=myelin_engine,
            refresh_ipsf=args.refresh_ipsf,
        )

        provider = IPSFProvider()

        logger.info(
            "Resolving IPSF provider data for CCN %s.",
            claim.billing_provider.other_id
            if claim.billing_provider is not None
            else None,
        )

        provider.from_claim(
            claim,
            myelin_engine.db_manager.engine,
        )

        logger.info(
            "IPSF provider resolved: CCN=%s effective_date=%s "
            "provider_type=%s cbsa_wi=%s.",
            provider.provider_ccn,
            provider.effective_date,
            provider.provider_type,
            provider.cbsa_wi_location,
        )

        ipps_jar = find_ipps_jar()

        logger.info(
            "Using IPPS Pricer JAR: %s",
            ipps_jar,
        )

        # Do not add jars/pricers/* to Myelin's main JVM classpath.
        # The IPPS client must keep its Pricer classes isolated because it uses
        # Javassist during startup.
        ipps_client = WindowsSafeIppsClient(
            jar_path=str(ipps_jar),
            db=myelin_engine.db_manager.engine,
            logger=logger,
        )

        logger.info(
            "Processing claim %s through CMS IPPS Pricer.",
            claim.claimid,
        )

        ipps_output, resolved_provider = ipps_client.process(
            claim,
            provider,
            drg_output,
        )

        combined_result["ipsfProvider"] = serialize_model(
            resolved_provider
        )
        combined_result["ipps"] = serialize_model(
            ipps_output
        )

        logger.info(
            "IPPS result: calculation_version=%s total_payment=%s "
            "final_cbsa=%s final_wage_index=%s return_code=%s.",
            ipps_output.calculation_version,
            ipps_output.total_payment,
            ipps_output.final_cbsa,
            ipps_output.final_wage_index,
            (
                ipps_output.return_code.code
                if ipps_output.return_code is not None
                else None
            ),
        )

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

        print()
        print(output_json)
        print()
        print(f"Output written to: {output_path}")

    except Exception:
        logger.exception(
            "MS-DRG and IPPS processing failed."
        )
        raise


if __name__ == "__main__":
    main()
