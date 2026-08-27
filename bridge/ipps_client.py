from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import jpype
from sqlalchemy import Engine, func, select

from myelin import DrgClient, IPSFProvider, Myelin
from myelin.plugins import apply_client_methods, run_client_load_classes
from myelin.pricers.ipps import IppsClient
from myelin.pricers.ipsf import IPSF
from myelin.pricers.url_loader import UrlLoader


logger = logging.getLogger(__name__)


class WindowsSafeIppsClient(IppsClient):
    """
    IPPS client using Myelin's isolated Pricer classloader while supplying
    a Windows-safe Java file URI.

    This is the same initialization sequence proven by run_ipps.py.
    """

    def __init__(
        self,
        jar_path: str,
        db: Engine,
        logger_instance: logging.Logger | None = None,
    ):
        if not jpype.isJVMStarted():
            raise RuntimeError(
                "The JVM is not running. Initialize Myelin/JVM before "
                "creating the IPPS processor."
            )

        resolved_jar = Path(jar_path).expanduser().resolve()

        if not resolved_jar.is_file():
            raise FileNotFoundError(
                f"IPPS Pricer JAR does not exist: {resolved_jar}"
            )

        self.logger = logger_instance or logging.getLogger(
            "WindowsSafeIppsClient"
        )
        self.url_loader = UrlLoader()

        jar_uri = resolved_jar.as_uri()

        self.logger.info(
            "Loading IPPS Pricer from URI: %s",
            jar_uri,
        )

        # Do not place IPPS Pricer classes on the primary JVM classpath.
        self.url_loader.load_urls([jar_uri])

        self.db = db

        # Preserve the exact Myelin IPPS/Javassist initialization order.
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


def find_ipps_jar(jar_root: str | Path | None = None) -> Path:
    configured_path = os.getenv("IPPS_JAR_PATH")

    if configured_path:
        path = Path(configured_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"IPPS_JAR_PATH does not exist: {path}"
            )
        return path

    if jar_root is None:
        project_root = Path(__file__).resolve().parent.parent
        jar_root_path = Path(
            os.getenv(
                "MYELIN_JAR_PATH",
                str(project_root / "jars"),
            )
        )
    else:
        jar_root_path = Path(jar_root)

    pricer_dir = jar_root_path.expanduser().resolve() / "pricers"

    if not pricer_dir.is_dir():
        raise FileNotFoundError(
            f"CMS Pricer directory was not found: {pricer_dir}"
        )

    matches = sorted(
        [
            path
            for path in pricer_dir.glob("*.jar")
            if "ipps-pricer" in path.name.lower()
        ],
        key=lambda item: item.name.lower(),
    )

    if not matches:
        raise FileNotFoundError(
            "No IPPS Pricer JAR containing 'ipps-pricer' was found in "
            f"{pricer_dir}"
        )

    if len(matches) > 1:
        available = ", ".join(path.name for path in matches)
        raise RuntimeError(
            "Multiple IPPS Pricer JARs were found. Set IPPS_JAR_PATH "
            f"to select one explicitly. Found: {available}"
        )

    return matches[0].resolve()


def _serialize_model(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if isinstance(model, dict):
        return model
    raise TypeError(
        f"Cannot serialize object of type {type(model).__name__}"
    )


class IppsProcessor:
    """
    Long-lived IPPS processor for a FastAPI process.

    Initialize this once during FastAPI lifespan startup. A process-level lock
    serializes access to the CMS Java Grouper/Pricer clients.
    """

    def __init__(
        self,
        myelin_engine: Myelin,
        jar_root: str | Path | None = None,
    ):
        if not jpype.isJVMStarted():
            raise RuntimeError(
                "Myelin exists but the JVM is not started. Start the JVM "
                "before initializing IppsProcessor."
            )

        if myelin_engine.db_manager.engine is None:
            raise RuntimeError(
                "Myelin database engine is not initialized."
            )

        self.myelin_engine = myelin_engine
        self.db = myelin_engine.db_manager.engine
        self.lock = threading.Lock()

        self.ipsf_record_count = self._get_ipsf_record_count()

        if self.ipsf_record_count <= 0:
            raise RuntimeError(
                "The IPSF table is empty. Populate it before starting the "
                "API. For the current local setup, run "
                "'python run_ipps.py --refresh-ipsf' and then rebuild the "
                "container with the updated data/myelin.db."
            )

        self.ipps_jar = find_ipps_jar(jar_root)

        logger.info(
            "Initializing CMS MS-DRG client for IPPS."
        )
        self.drg_client = DrgClient()

        logger.info(
            "Initializing CMS IPPS Pricer using %s.",
            self.ipps_jar,
        )
        self.ipps_client = WindowsSafeIppsClient(
            jar_path=str(self.ipps_jar),
            db=self.db,
            logger_instance=logger,
        )

        logger.info(
            "IPPS processor ready. IPSF records=%s",
            f"{self.ipsf_record_count:,}",
        )

    def _get_ipsf_record_count(self) -> int:
        ipsf_db = self.myelin_engine.db_manager.ipsf_db

        if ipsf_db is None:
            raise RuntimeError(
                "Myelin IPSF database was not initialized."
            )

        with ipsf_db.session() as session:
            value = session.execute(
                select(func.count()).select_from(IPSF)
            ).scalar_one()

        return int(value)

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": True,
            "ippsJar": self.ipps_jar.name,
            "ipsfRecordCount": self.ipsf_record_count,
        }

    def process(self, claim) -> dict[str, Any]:
        with self.lock:
            logger.info(
                "Running claim %s through CMS MS-DRG.",
                claim.claimid,
            )

            drg_output = self.drg_client.process(claim)

            logger.info(
                "MS-DRG complete. claim=%s version=%s drg=%s",
                claim.claimid,
                drg_output.drg_version,
                drg_output.final_drg_value,
            )

            provider = IPSFProvider()
            provider.from_claim(
                claim,
                self.db,
            )

            logger.info(
                "IPSF resolved. claim=%s ccn=%s effectiveDate=%s",
                claim.claimid,
                provider.provider_ccn,
                provider.effective_date,
            )

            ipps_output, resolved_provider = self.ipps_client.process(
                claim,
                provider,
                drg_output,
            )

            logger.info(
                "IPPS complete. claim=%s calculationVersion=%s "
                "returnCode=%s payment=%s",
                claim.claimid,
                ipps_output.calculation_version,
                (
                    ipps_output.return_code.code
                    if ipps_output.return_code is not None
                    else None
                ),
                ipps_output.total_payment,
            )

            return {
                "success": True,
                "claimId": claim.claimid,
                "msdrg": _serialize_model(drg_output),
                "ipsfProvider": _serialize_model(resolved_provider),
                "ipps": _serialize_model(ipps_output),
            }
