import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from bridge.provider_routes import router
from myelin.pricers.ipsf import IPSF


class ProviderRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        IPSF.metadata.create_all(self.db)
        with Session(self.db) as session:
            session.add_all([
                IPSF(provider_ccn="010001", effective_date=20240101, termination_date=0, provider_type="07", waiver_indicator="N", national_provider_identifier="1164403861"),
                IPSF(provider_ccn="010001", effective_date=20250101, termination_date=20251231, provider_type="37", waiver_indicator="N"),
            ])
            session.commit()
        self.app = FastAPI()
        self.app.state.myelin_engine = SimpleNamespace(db_manager=SimpleNamespace(engine=self.db))
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.db.dispose()

    def test_historical_lookup_uses_pricer_effective_date_rules(self):
        old = self.client.get("/api/v1/provider/ipsf?ccn=010001&date=2024-07-10")
        self.assertEqual(old.status_code, 200)
        self.assertEqual(old.json()["providerType"], "07")
        self.assertEqual(old.json()["terminationDate"], 20991231)
        current = self.client.get("/api/v1/provider/ipsf?ccn=010001&date=2025-07-10")
        self.assertEqual(current.json()["providerType"], "37")
        self.assertEqual(current.json()["effectiveDate"], 20250101)
        # Return the record as Myelin selects it; callers can inspect termination.
        expired = self.client.get("/api/v1/provider/ipsf?ccn=010001&date=2026-01-01")
        self.assertEqual(expired.json()["terminationDate"], 20251231)

    def test_not_found_and_future_only(self):
        for url in ("ccn=999999&date=2025-07-10", "ccn=010001&date=2023-01-01"):
            self.assertEqual(self.client.get("/api/v1/provider/ipsf?" + url).status_code, 404)

    def test_validation(self):
        for query in ("ccn=x&date=2025-07-10", "ccn=010001&date=bad", "ccn=010001&date=2025-02-30"):
            self.assertEqual(self.client.get("/api/v1/provider/ipsf?" + query).status_code, 422)

    def test_unavailable_is_not_not_found(self):
        self.app.state.myelin_engine = None
        self.assertEqual(self.client.get("/api/v1/provider/ipsf?ccn=010001&date=2025-07-10").status_code, 503)

    def test_database_error_is_sanitized(self):
        with patch("bridge.provider_routes.IPSFProvider.from_db", side_effect=RuntimeError("internal details")):
            response = self.client.get("/api/v1/provider/ipsf?ccn=010001&date=2025-07-10")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("internal details", response.text)


if __name__ == "__main__":
    unittest.main()
