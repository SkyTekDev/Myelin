"""Explicit package seeding for isolated test databases; never a runtime fallback."""
from functools import lru_cache
from pathlib import Path

from bridge.hha_geography_catalog import read_catalog_package, CATALOG_FILENAME, CATALOG_SHA256
from bridge.hha_geography_reference import install_release


@lru_cache(maxsize=1)
def package_document():
    return read_catalog_package(Path(__file__).resolve().parents[1] / 'bridge/data' / CATALOG_FILENAME, CATALOG_SHA256)


def seed_geography(engine):
    with engine.begin() as connection:
        install_release(connection.connection.driver_connection, package_document(), CATALOG_SHA256, 1, 'isolated-test-fixture')
