"""Synthetic, test-only annual sources; no fake source enters the shipped catalog."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from bridge.hha_provider import HhaInputError, provider_for_period
from bridge.hha_provider_annual import (
    CATALOG_SCHEMA, AnnualProviderCatalog, annual_payment_evidence,
    canonical_json, load_catalog, provider_row_content_sha256,
)
from myelin.pricers.ipsf import Base, IPSF


CCN = "017999"
START, END = date(2026, 6, 1), date(2026, 6, 30)


def catalog_document(*, cutoff="2026-11-01", next_effective=None):
    """All URLs/hashes below are intentionally synthetic test fixtures."""
    qrp = dict(sourceUrl="https://www.cms.gov/files/document/test-only-qrp-2026.pdf",
               sourceSha256="a" * 64, publishedDate="2026-03-16")
    vbp = dict(sourceUrl="https://data.cms.gov/provider-data/sites/default/files/resources/test-only-2026.csv",
               sourceSha256="b" * 64, publishedDate="2026-01-14",
               factorAuthority="CMS_IPSF_FIELD_30",
               annualCorroboration="CMS_HHVBP_PAYMENT_YEAR_ROUNDED_APP")
    history = dict(sourceUrl=("https://pds.mps.cms.gov/fiss/v2/inpatient/export?fromDate=1980-01-01"
                              f"&toDate={cutoff}&oscarNumber={CCN}&format=csv"),
                   sourceSha256="c" * 64, cutoffDate=cutoff)
    entry = dict(providerCcn=CCN, providerEffectiveDate="2025-01-01",
                 providerNextEffectiveDate=next_effective, rawTerminationDate=None,
                 providerType="36", qualityReportingProgramIndicator="0", vbpAdjustment="1.02345",
                 providerHistoryCutoffDate=cutoff,
                 qualityReportingSource=dict(qrp, rowLocator=f"page=1;CCN={CCN};classification=Compliant"),
                 valueBasedPurchasingSource=dict(vbp, rowLocator=f"CCN={CCN};paymentYear=2026",
                     publishedAdjustedPaymentPercentage="2.345", publishedPercentageDecimalPlaces=3),
                 providerHistorySource=dict(history, rowLocator=f"CCN={CCN};effectiveDate=20250101"))
    entry["providerRowContentSha256"] = provider_row_content_sha256(dict(
        providerCcn=CCN, effectiveDate=entry["providerEffectiveDate"], rawTerminationDate=None,
        providerType="36", federalPpsBlend="0", specialProviderUpdateFactor="1.02345"))
    return dict(schemaVersion=CATALOG_SCHEMA, paymentYear=2026,
                sources=dict(qualityReporting=[qrp], valueBasedPurchasing=[vbp], providerHistory=[history]),
                entries={CCN: entry})


def checked_catalog(document):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "test-only-catalog.json"
        content = canonical_json(document)
        path.write_bytes(content)
        return load_catalog(path, hashlib.sha256(content).hexdigest())


class AnnualProviderChecks(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.catalog = checked_catalog(catalog_document())

    def tearDown(self):
        self.engine.dispose()

    def add(self, **changes):
        values = dict(provider_ccn=CCN, effective_date=20250101, provider_type="36",
                      federal_pps_blend="0", special_provider_update_factor=1.02345,
                      termination_date=None)
        values.update(changes)
        with Session(self.engine) as session:
            row = IPSF(**values)
            session.add(row)
            session.commit()
            return row.id

    def resolve(self, start=START, end=END, catalog=None):
        with patch("bridge.hha_provider_annual.load_annual_catalog", return_value=catalog or self.catalog):
            return provider_for_period(self.engine, CCN, start, end)

    def reject(self, code, **kwargs):
        with self.assertRaises(HhaInputError) as caught:
            self.resolve(**kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_verified_older_row_keeps_exact_ipsf_factor_and_adds_bound_provenance(self):
        row_id = self.add()
        output = self.resolve()
        self.assertEqual(output["providerData"]["vbpAdjustment"], Decimal("1.02345"))
        proof = output["providerEvidence"]["annualPaymentEvidence"]
        self.assertEqual(proof["providerRowId"], row_id)
        self.assertEqual(proof["sourceRowFingerprint"], output["providerEvidence"]["sourceRowFingerprint"])
        self.assertEqual(proof["catalogSha256"], self.catalog.sha256)
        self.assertEqual(proof["vbpAdjustment"], "1.02345")
        self.assertEqual(proof["qualityReportingProgramIndicator"], "0")
        self.assertEqual(proof["valueBasedPurchasingSource"]["publishedAdjustedPaymentPercentage"], "2.345")
        self.assertIsNone(proof["providerNextEffectiveDate"])
        self.assertEqual(output["providerEvidence"]["effectiveDate"], "2025-01-01")

    def test_existing_2026_row_does_not_load_annual_catalog_or_change_response_shape(self):
        self.add(effective_date=20260101)
        with patch("bridge.hha_provider_annual.load_annual_catalog", side_effect=AssertionError("legacy path changed")):
            output = provider_for_period(self.engine, CCN, START, END)
        self.assertNotIn("annualPaymentEvidence", output["providerEvidence"])

    def test_local_payment_or_provider_facts_cannot_be_overridden_by_annual_catalog(self):
        for changes in [dict(federal_pps_blend="2"), dict(special_provider_update_factor=1.02),
                        dict(effective_date=20240101), dict(termination_date=0)]:
            with self.subTest(changes=changes):
                with Session(self.engine) as session:
                    session.execute(delete(IPSF)); session.commit()
                self.add(**changes)
                self.reject("HHA_PROVIDER_ANNUAL_FACTS_CONFLICT")

    def test_history_cutoff_and_next_change_cover_complete_statement(self):
        self.add()
        self.reject("HHA_PROVIDER_ANNUAL_PERIOD_UNCOVERED",
                    catalog=checked_catalog(catalog_document(cutoff="2026-06-29")))
        catalog = checked_catalog(catalog_document(next_effective="2026-06-30"))
        self.reject("HHA_PROVIDER_ANNUAL_PERIOD_UNCOVERED", catalog=catalog)
        self.assertIn("annualPaymentEvidence", self.resolve(end=date(2026, 6, 29), catalog=catalog)["providerEvidence"])

    def test_missing_entry_and_missing_catalog_remain_unpriced(self):
        self.add()
        empty = AnnualProviderCatalog("d" * 64, dict(entries={}))
        self.reject("HHA_PROVIDER_REFERENCE_STALE", catalog=empty)
        with patch("bridge.hha_provider_annual.load_annual_catalog", side_effect=FileNotFoundError()):
            with self.assertRaises(HhaInputError) as caught:
                provider_for_period(self.engine, CCN, START, END)
        self.assertEqual(caught.exception.code, "HHA_PROVIDER_REFERENCE_STALE")

    def test_annual_proof_does_not_bypass_actual_type_termination_or_midperiod_changes(self):
        for changes, code in [(dict(provider_type="38"), "HHA_PROVIDER_TYPE_MISMATCH"),
                              (dict(termination_date=20260630), "HHA_PROVIDER_TERMINATED")]:
            with self.subTest(changes=changes):
                with Session(self.engine) as session:
                    session.execute(delete(IPSF)); session.commit()
                self.add(**changes)
                self.reject(code)
        with Session(self.engine) as session:
            session.execute(delete(IPSF)); session.commit()
        self.add()
        self.add(effective_date=20260615)
        self.reject("HHA_PROVIDER_CHANGED_WITHIN_PERIOD")

    def test_old_interval_and_later_2026_record_each_use_their_actual_reference_path(self):
        self.add()
        self.add(effective_date=20260701, special_provider_update_factor=0.99)
        catalog = checked_catalog(catalog_document(next_effective="2026-07-01"))
        june = self.resolve(catalog=catalog)
        july = self.resolve(start=date(2026, 7, 1), end=date(2026, 7, 30), catalog=catalog)
        self.assertIn("annualPaymentEvidence", june["providerEvidence"])
        self.assertNotIn("annualPaymentEvidence", july["providerEvidence"])
        self.assertEqual(july["providerData"]["vbpAdjustment"], Decimal("0.99"))

    def test_local_row_ids_bind_receipt_evidence_but_do_not_change_cms_content_identity(self):
        self.add()
        before = self.resolve()["providerEvidence"]
        with Session(self.engine) as session:
            session.execute(delete(IPSF)); session.commit()
        self.add()
        after = self.resolve()["providerEvidence"]
        # SQLite may reuse an emptied row ID; explicitly update the retained identity.
        if before["providerRowId"] == after["providerRowId"]:
            with self.engine.begin() as connection:
                connection.exec_driver_sql("UPDATE ipsf SET id=100 WHERE id=?", (after["providerRowId"],))
            after = self.resolve()["providerEvidence"]
        self.assertNotEqual(before["sourceRowFingerprint"], after["sourceRowFingerprint"])
        self.assertEqual(before["annualPaymentEvidence"]["providerRowContentSha256"],
                         after["annualPaymentEvidence"]["providerRowContentSha256"])

    def test_source_evidence_is_a_copy_of_the_pinned_catalog(self):
        self.add()
        proof = self.resolve()["providerEvidence"]["annualPaymentEvidence"]
        proof["providerHistorySource"]["rowLocator"] = "changed"
        self.assertNotEqual(self.resolve()["providerEvidence"]["annualPaymentEvidence"]["providerHistorySource"]["rowLocator"], "changed")


class AnnualCatalogValidationChecks(unittest.TestCase):
    def test_shared_dotnet_contract_fixtures_have_identical_hashes_and_runtime_evidence(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/hha-2026-annual-provider-contract.json").read_text())
        self.assertEqual(len(fixtures["cases"]), 6)
        self.assertEqual(len(fixtures["noAprCases"]), 2)
        self.assertEqual(len(fixtures["invalidNoAprCases"]), 1)
        for case in fixtures["cases"] + fixtures["noAprCases"]:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "test-only-catalog.json"
                path.write_bytes(case["catalogJson"].encode("utf-8"))
                catalog = load_catalog(path, case["catalogSha256"])
                entry = catalog.document["entries"][CCN]
                self.assertEqual(hashlib.sha256(canonical_json(entry)).hexdigest(), case["catalogEntrySha256"])
                self.assertEqual(provider_row_content_sha256(case["providerEvidence"]), case["providerRowContentSha256"])
                with patch("bridge.hha_provider_annual.load_annual_catalog", return_value=catalog):
                    proof = annual_payment_evidence(case["providerEvidence"], START, END)
                self.assertEqual(proof, case["annualPaymentEvidence"])
        for case in fixtures["invalidNoAprCases"]:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "test-only-invalid-catalog.json"
                path.write_bytes(case["catalogJson"].encode("utf-8"))
                with self.assertRaises(ValueError): load_catalog(path, case["catalogSha256"])

    def test_no_apr_requires_both_exact_footnotes_reviewed_policy_and_known_annual_dataset(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/hha-2026-annual-provider-contract.json").read_text())
        original = json.loads(fixtures["noAprCases"][0]["catalogJson"])
        def missing_first(entry): entry["valueBasedPurchasingSource"]["annualPerformanceReportFootnote"] = ""
        def missing_second(entry): entry["valueBasedPurchasingSource"]["adjustedPaymentPercentageFootnote"] = ""
        def bare_na(entry): entry["valueBasedPurchasingSource"]["policySources"] = []
        def wrong_year(entry): entry["valueBasedPurchasingSource"]["observedPaymentYears"] = [2025, 2026]
        def wrong_policy(entry): entry["valueBasedPurchasingSource"]["policySources"][0]["sourceSha256"] = "f" * 64
        def unknown_na(entry): entry["valueBasedPurchasingSource"]["publishedPaymentYear"] = "Not Available"
        for mutate in [missing_first, missing_second, bare_na, wrong_year, wrong_policy, unknown_na]:
            with self.subTest(mutate=mutate.__name__):
                document = deepcopy(original)
                mutate(document["entries"][CCN])
                with self.assertRaises(ValueError): checked_catalog(document)

    def test_no_apr_preserves_neutral_factor_and_independent_qrp_zero_or_two(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/hha-2026-annual-provider-contract.json").read_text())
        self.assertEqual({case["providerEvidence"]["federalPpsBlend"] for case in fixtures["noAprCases"]}, {"0", "2"})
        for case in fixtures["noAprCases"]:
            document = json.loads(case["catalogJson"])
            catalog = checked_catalog(document)
            with patch("bridge.hha_provider_annual.load_annual_catalog", return_value=catalog):
                proof = annual_payment_evidence(case["providerEvidence"], START, END)
            self.assertEqual(proof["vbpAdjustment"], "1")
            self.assertEqual(proof["qualityReportingProgramIndicator"], case["providerEvidence"]["federalPpsBlend"])

    def test_hash_and_canonical_serialization_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test-only-catalog.json"
            raw = canonical_json(catalog_document())
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_catalog(path, "f" * 64)
            path.write_text(json.dumps(catalog_document(), indent=2), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_unverified_year_sources_and_factor_forgery_are_rejected(self):
        def change_year(doc): doc["paymentYear"] = 2025
        def missing_source(doc): doc["sources"]["qualityReporting"] = []
        def bad_authority(doc): doc["entries"][CCN]["providerHistorySource"]["sourceUrl"] = "https://example.com/history.csv"
        def truncated_history(doc):
            for row in [doc["sources"]["providerHistory"][0], doc["entries"][CCN]["providerHistorySource"]]:
                row["sourceUrl"] = row["sourceUrl"].replace("1980-01-01", "2025-01-01")
        def stale_type(doc): doc["entries"][CCN]["providerType"] = "38"
        def modified_factor(doc): doc["entries"][CCN]["vbpAdjustment"] = "1.03"
        def wrong_percentage(doc): doc["entries"][CCN]["valueBasedPurchasingSource"]["publishedAdjustedPaymentPercentage"] = "2.346"
        def guessed_factor(doc): doc["entries"][CCN]["vbpAdjustment"] = "1.023451"
        for change in [change_year, missing_source, bad_authority, truncated_history, stale_type,
                       modified_factor, wrong_percentage, guessed_factor]:
            with self.subTest(change=change.__name__):
                document = catalog_document(); change(document)
                with self.assertRaises(ValueError): checked_catalog(document)

    def test_rounded_percentage_is_only_corroboration_and_half_ties_are_rejected(self):
        document = catalog_document()
        source = document["entries"][CCN]["valueBasedPurchasingSource"]
        source.update(publishedAdjustedPaymentPercentage="2.35", publishedPercentageDecimalPlaces=2)
        # Exact2.345% is a tie between published2.34/2.35; no round rule is guessed.
        with self.assertRaises(ValueError): checked_catalog(document)
        source.update(publishedAdjustedPaymentPercentage="2.3", publishedPercentageDecimalPlaces=1)
        checked = checked_catalog(document)
        self.assertEqual(checked.document["entries"][CCN]["vbpAdjustment"], "1.02345")

    def test_provider_content_has_a_defined_cross_language_canonical_form(self):
        proof = dict(providerCcn=CCN, effectiveDate="2025-01-01", rawTerminationDate=None,
                     providerType="36", federalPpsBlend="0", specialProviderUpdateFactor=Decimal("1.0234500"))
        expected = b'{"effectiveDate":"2025-01-01","federalPpsBlend":"0","providerCcn":"017999","providerType":"36","rawTerminationDate":null,"specialProviderUpdateFactor":"1.02345"}'
        self.assertEqual(provider_row_content_sha256(proof), hashlib.sha256(expected).hexdigest())


if __name__ == "__main__":
    unittest.main()
