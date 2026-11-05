"""Approved rate seeding for isolated test databases."""
from pathlib import Path

from bridge.hha_separate_payment_rates import CATALOG_FILENAME, CATALOG_SHA256, read_package
from bridge.hha_separate_payment_reference import install_release


def seed_rates(engine):
    document = read_package(Path(__file__).resolve().parents[1]/'bridge/data'/CATALOG_FILENAME,CATALOG_SHA256)
    with engine.begin() as connection:
        install_release(connection.connection.driver_connection,document,CATALOG_SHA256,1,'isolated-test-fixture')
