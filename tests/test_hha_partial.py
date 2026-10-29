"""Pinned CY2026 partial-period math, eligibility facts and CMS evidence."""
import copy
import json
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from bridge.hha_models import HhaRequest, HhaResponse
from tests import test_hha_integration as integration


def fixture_cases():
    return json.loads((Path(__file__).parent / "fixtures/hha-2026-partial.json").read_text(encoding="utf-8"))["comparisons"]


def partial_request(name="transfer_15_days"):
    return next(copy.deepcopy(c["request"]) for c in fixture_cases() if c["name"] == name)


class HhaPartialPeriodChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integration.HhaCmsIntegrationChecks.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        integration.HhaCmsIntegrationChecks.tearDownClass.__func__(cls)

    def price(self, payload):
        return self.processor.process(HhaRequest.model_validate(payload))

    def test_fourteen_complete_standalone_cms_outputs_and_contexts_match(self):
        cases = fixture_cases()
        self.assertEqual(len(cases), 14)
        for case in cases:
            with self.subTest(case=case["name"]):
                result = self.price(case["request"])
                self.assertEqual(result.status, "Success")
                self.assertEqual(result.totalPayment, Decimal(case["expectedPayment"]))
                actual = json.loads(result.model_dump_json())
                expected = copy.deepcopy(case["standalone"])
                for cms in (actual["cms"], expected):
                    for field in ("finalCbsa", "finalWageIndex"):
                        if cms["paymentData"].get(field) is None:
                            cms["paymentData"].pop(field, None)
                self.assertEqual(actual["cms"], expected)
                self.assertEqual(actual["partialPeriod"], case["bridge"]["partialPeriod"])
                self.assertEqual(result.cms["returnCodeData"]["code"], case["expectedReturnCode"])

    def test_cms_rounds_payment_ratio_before_proration(self):
        for name, days, ratio, payment in [("readmission_7_days", 7, ".23", "370.46"),
                ("transfer_29_days", 29, ".97", "1562.35"), ("same_day_four_visits", 1, ".03", "48.32")]:
            result = self.price(partial_request(name))
            self.assertEqual(result.totalPayment, Decimal(payment))
            self.assertEqual(result.partialPeriod.coveredDays, days)
            self.assertEqual(result.partialPeriod.paymentRatio, Decimal(ratio))
            self.assertTrue(result.partialPeriod.adjustmentApplied)

    def test_billable_visit_span_not_statement_days_determines_payment(self):
        data = partial_request("visit_span_13_statement_18")
        result = self.price(data)
        self.assertEqual(result.partialPeriod.firstBillableDate.isoformat(), "2026-06-03")
        self.assertEqual(result.partialPeriod.lastBillableDate.isoformat(), "2026-06-15")
        self.assertEqual(result.partialPeriod.coveredDays, 13)
        self.assertEqual(result.partialPeriod.paymentRatio, Decimal(".43"))
        from bridge.hha_provider import provider_for_period
        from bridge.hha_geography import resolve_geography
        request = HhaRequest.model_validate(data)
        provider = provider_for_period(self.db, request.providerCcn, request.fromDate, request.throughDate)
        geography = resolve_geography(request.serviceCountyFips, request.cbsaCode, self.processor.wage_rows)
        payload = self.processor.build_payload(request, provider, geography)
        self.assertEqual(payload["claimData"]["hhrgNumberOfDays"], 13)
        self.assertEqual(payload["claimData"]["partialEpisodePaymentIndicator"], "Y")

    def test_lupa_supersedes_partial_proration_with_complete_allocations(self):
        for name, code in [("partial_lupa_initial", "14"), ("partial_lupa_subsequent", "06"), ("partial_lupa_transfer", "06")]:
            data = partial_request(name)
            result = self.price(data)
            self.assertFalse(result.partialPeriod.adjustmentApplied)
            self.assertEqual(result.reasonCode, "HHA_LUPA_ESTIMATED")
            self.assertEqual(result.cms["returnCodeData"]["code"], code)
            self.assertEqual(sum(v.totalPayment for v in result.lupa.visitPayments), result.totalPayment)
            full = copy.deepcopy(data)
            full.update(periodType="Full30Day", throughDate="2026-06-30")
            full.pop("partialPeriod")
            self.assertEqual(self.price(full).totalPayment, result.totalPayment)

    def test_context_fields_are_explicit_strict_and_cannot_override_days(self):
        for missing in ["eventType", "patientStatus", "nextCareStartDate", "isCommonOwnershipTransfer"]:
            data = partial_request()
            data["partialPeriod"].pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                HhaRequest.model_validate(data)
        for field, value in [("eventType", "Death"), ("patientStatus", "01"),
                ("isCommonOwnershipTransfer", "false"), ("isCommonOwnershipTransfer", 0),
                ("isCommonOwnershipTransfer", True), ("nextCareStartDate", "2026-06-16T00:00:00"),
                ("coveredDays", 15), ("paymentRatio", ".5")]:
            data = partial_request()
            data["partialPeriod"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                HhaRequest.model_validate(data)

    def test_period_and_next_care_boundaries_are_validated(self):
        for changes in [dict(partialPeriod=None), dict(periodType="Full30Day"),
                dict(throughDate="2026-06-30"), dict(throughDate="2026-05-31"),
                dict(throughDate="2027-06-15")]:
            data = partial_request()
            data.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                HhaRequest.model_validate(data)
        for next_date in ["2026-06-15", "2026-06-14", "2026-07-01"]:
            data = partial_request()
            data["partialPeriod"]["nextCareStartDate"] = next_date
            with self.subTest(next_date=next_date), self.assertRaises(ValidationError):
                HhaRequest.model_validate(data)

    def test_missing_lupa_context_below_threshold_remains_unpriced(self):
        data = partial_request("partial_lupa_initial")
        data.pop("lupa")
        result = self.price(data)
        self.assertEqual(result.reasonCode, "HHA_LUPA_CONTEXT_REQUIRED")
        self.assertIsNone(result.totalPayment)
        self.assertIsNone(result.partialPeriod)

    def test_partial_outlier_is_unpriced_and_cannot_retain_adjustment_or_amount(self):
        data = partial_request("same_day_four_visits")
        for visit in data["visits"]:
            visit["outlierUnits"] = 4
        result = self.price(data)
        self.assertEqual(result.reasonCode, "HHA_OUTLIER_CONTEXT_REQUIRED")
        self.assertEqual(result.cms["returnCodeData"]["code"], "11")
        self.assertGreater(result.cms["paymentData"]["outlierPayment"], 0)
        self.assertIsNone(result.totalPayment)
        self.assertIsNone(result.partialPeriod)

    def test_all_partial_cms_money_rates_and_disposition_are_verified(self):
        data = partial_request()
        original = self.price(data).cms
        variants = [("totalPayment", Decimal("805.35")), ("hhrgPayment", Decimal("1")),
                ("standardizedPayment", Decimal("1")), ("hhrgWeight", Decimal("1")),
                ("outlierPayment", Decimal(".01")), ("lateSubmissionPenaltyAmount", Decimal(".01")),
                ("valueBasedPurchasingAdjustmentAmount", Decimal(".01")), ("totalQuantityOfCoveredVisits", True)]
        for field, value in variants:
            cms = copy.deepcopy(original)
            cms["paymentData"][field] = value
            with self.subTest(field=field), patch.object(self.processor, "calculate", return_value=cms), self.assertRaises(RuntimeError):
                self.price(data)
        for field in ("cost", "dollarRate", "addOnVisitAmount"):
            cms = copy.deepcopy(original)
            cms["paymentData"]["revenuePayments"][3][field] += Decimal(".01")
            with self.subTest(field=field), patch.object(self.processor, "calculate", return_value=cms), self.assertRaises(RuntimeError):
                self.price(data)
        cms = copy.deepcopy(original)
        cms["returnCodeData"]["code"] = "00"
        with patch.object(self.processor, "calculate", return_value=cms):
            self.assertEqual(self.price(data).reasonCode, "HHA_CMS_REJECTED")

    def test_partial_response_evidence_cannot_be_tampered_or_retained_on_failure(self):
        original = self.price(partial_request()).model_dump(mode="json")
        for field, value in [("coveredDays", 14), ("paymentRatio", ".49"),
                ("adjustmentApplied", False), ("periodDays", 60), ("firstBillableDate", "2026-06-02")]:
            response = copy.deepcopy(original)
            response["partialPeriod"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                HhaResponse.model_validate(response)
        response = copy.deepcopy(original)
        response.update(status="UnableToPrice",totalPayment=None)
        with self.assertRaises(ValidationError):
            HhaResponse.model_validate(response)
