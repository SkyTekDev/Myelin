from __future__ import annotations

from logging import Logger, getLogger
from pathlib import Path

import jpype
from sqlalchemy import Engine

from myelin.plugins import apply_client_methods, run_client_load_classes
from myelin.pricers.opps import OppsClient
from myelin.pricers.url_loader import UrlLoader


class WindowsSafeOppsClient(OppsClient):
    """
    OppsClient variant that converts a Windows path into a valid file URI.

    Myelin currently builds a Windows path with a two-slash file URL.
    Java requires a normalized three-slash file URI on Windows.
    """

    def __init__(
        self,
        jar_path: str | None = None,
        db: Engine | None = None,
        logger: Logger | None = None,
    ):
        if not jpype.isJVMStarted():
            raise RuntimeError(
                "JVM is not started. Start Myelin before creating "
                "WindowsSafeOppsClient."
            )

        if jar_path is None:
            raise ValueError(
                "jar_path must be provided to WindowsSafeOppsClient."
            )

        resolved_jar = Path(jar_path).expanduser().resolve()

        if not resolved_jar.is_file():
            raise ValueError(
                f"OPPS Pricer JAR does not exist: {resolved_jar}"
            )

        self.logger = logger or getLogger("WindowsSafeOppsClient")
        self.url_loader = UrlLoader()

        jar_uri = resolved_jar.as_uri()

        self.logger.info(
            "Loading OPPS Pricer from URI: %s",
            jar_uri,
        )

        # Keep the Pricer isolated in its URL classloader. Do not put the
        # entire pricers directory on Myelin's primary JVM classpath.
        self.url_loader.load_urls([jar_uri])

        self.db = db
        self.load_classes()

        try:
            run_client_load_classes(self)
        except Exception:
            self.logger.debug(
                "No OPPS plugin class-loading extensions were applied.",
                exc_info=True,
            )

        self.pricer_setup()

        try:
            apply_client_methods(self)
        except Exception:
            self.logger.debug(
                "No OPPS plugin methods were applied.",
                exc_info=True,
            )
