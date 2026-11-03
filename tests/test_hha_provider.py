"""First-pass HH provider policy using isolated, actual IPSF rows."""
from datetime import date, datetime
from decimal import Decimal
import unittest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from bridge.hha_provider import HhaInputError, provider_for_period
from myelin.pricers.ipsf import Base, IPSF

START, END = date(2026, 6, 1), date(2026, 6, 30)
CCN = "017999"

class HhaProviderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def add(self, **changes):
        values = dict(provider_ccn=CCN, effective_date=20260101, provider_type="36",
                      federal_pps_blend="0", special_provider_update_factor=1.02345,
                      vbp_adjustment=0.87, termination_date=None,
                      cbsa_actual_geographic_location="99999", county_code="99999")
        values.update(changes)
        with Session(self.engine) as session:
            row = IPSF(**values)
            session.add(row)
            session.commit()
            return row.id

    def clear(self):
        with Session(self.engine) as session:
            session.execute(delete(IPSF))
            session.commit()

    def reject(self, expected, start=START, end=END, ccn=CCN):
        with self.assertRaises(HhaInputError) as caught:
            provider_for_period(self.engine, ccn, start, end)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(caught.exception.reason, str(caught.exception))

    def test_actual_hh_qrp_and_vbp_fields_with_evidence(self):
        row_id = self.add()
        result = provider_for_period(self.engine, CCN, START, END)
        self.assertEqual(result["providerData"], dict(
            providerCcn=CCN, effectiveDate="2026-01-01", vbpAdjustment=Decimal("1.02345")))
        self.assertEqual(result["qualityReportingProgramIndicator"], "0")
        proof = result["providerEvidence"]
        self.assertEqual(proof["providerRowId"], row_id)
        self.assertEqual(proof["specialProviderUpdateFactor"], Decimal("1.02345"))
        self.assertEqual(proof["valueBasedPurchasingSourceField"], "special_provider_update_factor")
        self.assertEqual(proof["qualityReportingSourceField"], "federal_pps_blend")
        self.assertIsNone(proof["rawTerminationDate"])
        self.assertEqual(proof["lookupThroughDate"], "2026-06-30")
        self.assertEqual(len(proof["sourceRowFingerprint"]), 64)
        self.assertNotIn("cbsaActualGeographicLocation", result["providerData"])
        self.assertNotIn("countyCode", result["providerData"])

    def test_latest_record_and_future_records_do_not_replace_effective_facts(self):
        self.add(effective_date=20260101, special_provider_update_factor=0.99)
        row_id = self.add(effective_date=20260601, federal_pps_blend="2", special_provider_update_factor=0.97)
        self.add(effective_date=20260701, special_provider_update_factor=1.04)
        result = provider_for_period(self.engine, CCN, START, END)
        self.assertEqual(result["providerEvidence"]["providerRowId"], row_id)
        self.assertEqual(result["qualityReportingProgramIndicator"], "2")
        self.assertEqual(result["providerData"]["vbpAdjustment"], Decimal("0.97"))

    def test_period_change_is_not_replaced_by_an_older_row(self):
        self.add()
        self.add(effective_date=20260615)
        self.reject("HHA_PROVIDER_CHANGED_WITHIN_PERIOD")

    def test_crossover_uses_january_payment_facts_and_prior_coverage(self):
        start, end = date(2025, 12, 3), date(2026, 1, 1)
        prior = self.add(effective_date=20250101, special_provider_update_factor=1.04)
        current = self.add(federal_pps_blend="2", special_provider_update_factor=0.98)
        result = provider_for_period(self.engine, CCN, start, end)
        self.assertEqual(result["qualityReportingProgramIndicator"], "2")
        self.assertEqual(result["providerData"]["vbpAdjustment"], Decimal("0.98"))
        proof = result["providerEvidence"]
        self.assertEqual(proof["providerRowId"], current)
        self.assertEqual(proof["crossoverProviderEvidence"], {
            "schemaVersion": "HHACrossoverProviderEvidence/1", "providerCcn": CCN,
            "providerRowId": prior, "providerType": "36", "effectiveDate": "2025-01-01",
            "terminationDate": None,
        })

    def test_crossover_rejects_missing_changed_or_terminated_coverage(self):
        start, end = date(2025, 12, 3), date(2026, 1, 1)
        for prior, current, reason in (
            (None, {}, "HHA_PROVIDER_CROSSOVER_UNVERIFIED"),
            ({"effective_date": 20250101}, None, "HHA_PROVIDER_CROSSOVER_UNVERIFIED"),
            ({"effective_date": 20251204}, {}, "HHA_PROVIDER_CHANGED_WITHIN_PERIOD"),
            ({"effective_date": 20250101, "termination_date": 20251231}, {}, "HHA_PROVIDER_TERMINATED"),
            ({"effective_date": 20250101}, {"termination_date": 20260101}, "HHA_PROVIDER_TERMINATED"),
            ({"effective_date": 20250101, "provider_type": "38"}, {}, "HHA_PROVIDER_TYPE_MISMATCH"),
        ):
            with self.subTest(prior=prior, current=current):
                self.clear()
                if prior is not None:
                    self.add(**prior)
                if current is not None:
                    self.add(**current)
                self.reject(reason, start, end)

    def test_crossover_rejects_duplicate_prior_record(self):
        self.add(effective_date=20250101)
        self.add(effective_date=20250101)
        self.add()
        self.reject("HHA_PROVIDER_REFERENCE_AMBIGUOUS", date(2025, 12, 3), date(2026, 1, 1))

    def test_duplicate_latest_is_ambiguous_even_if_values_match(self):
        self.add()
        self.add()
        self.reject("HHA_PROVIDER_REFERENCE_AMBIGUOUS")

    def test_old_duplicates_do_not_make_unique_current_row_ambiguous(self):
        self.add(effective_date=20250101)
        self.add(effective_date=20250101)
        row_id = self.add()
        self.assertEqual(provider_for_period(self.engine, CCN, START, END)["providerEvidence"]["providerRowId"], row_id)

    def test_missing_and_other_ccn_are_not_substituted(self):
        self.reject("HHA_PROVIDER_NOT_FOUND")
        self.add(provider_ccn="017998")
        self.reject("HHA_PROVIDER_NOT_FOUND")

    def test_unusable_references_have_explicit_failure(self):
        for values, expected in [
            ({"provider_type": "38"}, "HHA_PROVIDER_TYPE_MISMATCH"),
            ({"provider_type": None}, "HHA_PROVIDER_TYPE_MISMATCH"),
            ({"effective_date": 20250101}, "HHA_PROVIDER_REFERENCE_STALE"),
            ({"effective_date": 19000101}, "HHA_PROVIDER_REFERENCE_STALE"),
            ({"effective_date": 20260230}, "HHA_PROVIDER_REFERENCE_INVALID"),
            ({"termination_date": 20260630}, "HHA_PROVIDER_TERMINATED"),
            ({"termination_date": 20260531}, "HHA_PROVIDER_TERMINATED"),
            ({"termination_date": 20260230}, "HHA_PROVIDER_REFERENCE_INVALID"),
            ({"federal_pps_blend": None}, "HHA_PROVIDER_QRP_INVALID"),
            ({"federal_pps_blend": ""}, "HHA_PROVIDER_QRP_INVALID"),
            ({"federal_pps_blend": "1"}, "HHA_PROVIDER_QRP_INVALID"),
            ({"federal_pps_blend": "3"}, "HHA_PROVIDER_QRP_INVALID"),
            ({"special_provider_update_factor": None}, "HHA_PROVIDER_VBP_INVALID"),
            ({"special_provider_update_factor": 0}, "HHA_PROVIDER_VBP_INVALID"),
            ({"special_provider_update_factor": 0.94999}, "HHA_PROVIDER_VBP_INVALID"),
            ({"special_provider_update_factor": 1.05001}, "HHA_PROVIDER_VBP_INVALID"),
            ({"special_provider_update_factor": float("inf")}, "HHA_PROVIDER_VBP_INVALID"),
            ({"special_provider_update_factor": float("nan")}, "HHA_PROVIDER_VBP_INVALID"),
        ]:
            with self.subTest(values=values):
                self.clear()
                self.add(**values)
                self.reject(expected)

    def test_source_open_termination_values_and_future_termination(self):
        for termination in [None, 0, 19000101, 99991231, 20260701]:
            with self.subTest(termination=termination):
                self.clear()
                self.add(termination_date=termination)
                proof = provider_for_period(self.engine, CCN, START, END)["providerEvidence"]
                self.assertEqual(proof["rawTerminationDate"], termination)

    def test_inclusive_adjustment_limits(self):
        for factor in [0.95, 1, 1.05]:
            with self.subTest(factor=factor):
                self.clear()
                self.add(special_provider_update_factor=factor)
                actual = provider_for_period(self.engine, CCN, START, END)["providerData"]["vbpAdjustment"]
                self.assertEqual(actual, Decimal(str(factor)))

    def test_input_guards_and_database_failure_are_not_provider_not_found(self):
        self.reject("HHA_PROVIDER_INVALID_ID", ccn=" 017999")
        self.reject("HHA_INVALID_PERIOD", start=END, end=START)
        self.reject("HHA_INVALID_PERIOD", start=datetime(2026, 6, 1))
        with self.engine.connect() as connection:
            connection.execute(text("DROP TABLE ipsf"))
        with self.assertRaises(OperationalError):
            provider_for_period(self.engine, CCN, START, END)

if __name__ == "__main__":
    unittest.main()