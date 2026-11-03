"""Contract boundaries; runnable with Python's standard unittest runner."""

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from pydantic import ValidationError

from bridge.hha_models import HhaRequest, HhaResponse, HhaVisit
from bridge.hha_release import CY_2026, release_for_through


def request_data():
    return {
        "claimId": "HHA-CONTRACT-TEST", "providerCcn": "017999", "billType": "0329",
        "hippsCode": "1FC11", "admissionDate": "2026-06-01", "fromDate": "2026-06-01",
        "throughDate": "2026-06-30", "noticeReceiptDate": "2026-06-01",
        "periodType": "Full30Day", "serviceCountyFips": "01023", "cbsaCode": "01",
        "geographyBasis": "SubmittedCmsValueCodes",
        "visits": [
            {"visitId": str(day), "serviceDate": f"2026-06-{day:02d}",
             "discipline": "SN", "outlierUnits": 4}
            for day in (1, 5, 10, 15, 20, 28)
        ],
    }


def success_response():
    return {
        "claimId": "HHA-CONTRACT-TEST", "status": "Success",
        "reasonCode": "HHA_PRICED", "reason": "Synthetic contract fixture.",
        "totalPayment": Decimal("1602.87"), "calculationVersion": "2026.0",
        "providerData": {"providerCcn": "017999"},
        "providerEvidence": {"effectiveDate": "2026-01-01"},
        "geographyEvidence": {"countyFips": "01023", "basis": "SubmittedCmsValueCodes"},
        "cms": {"returnCodeData": {"code": "00"}},
    }


class HhaContractChecks(unittest.TestCase):
    def test_payment_release_follows_through_and_fails_closed(self):
        december = date(2026, 12, 31)
        january = date(2027, 1, 1)
        synthetic = replace(CY_2026, payment_year=2027, earliest_statement_from=date(2026, 12, 3),
                            calculation_version="Test2027")
        releases = {2026: CY_2026, 2027: synthetic}
        self.assertIs(release_for_through(december, releases), CY_2026)
        self.assertIs(release_for_through(january, releases), synthetic)
        self.assertTrue(synthetic.covers_statement(date(2026, 12, 3), january))
        self.assertIsNone(release_for_through(january))

        payload = request_data()
        offset = date(2026, 12, 2) - date(2026, 6, 1)
        for key in ("admissionDate", "noticeReceiptDate", "fromDate", "throughDate"):
            payload[key] = (date.fromisoformat(payload[key]) + offset).isoformat()
        for visit in payload["visits"]:
            visit["serviceDate"] = (date.fromisoformat(visit["serviceDate"]) + offset).isoformat()
        priced_request = HhaRequest.model_validate(payload)
        self.assertIs(priced_request.release, CY_2026)
        self.assertNotIn("release", priced_request.model_dump())
        payload.update(fromDate="2026-12-03", throughDate="2027-01-01")
        with self.assertRaisesRegex(ValidationError, "No approved HH pricing release"):
            HhaRequest.model_validate(payload)

    def test_crossover_accepts_supported_original_periods(self):
        payload = request_data()
        offset = date(2025, 12, 3) - date(2026, 6, 1)
        for key in ("admissionDate", "noticeReceiptDate", "fromDate", "throughDate"):
            payload[key] = (date.fromisoformat(payload[key]) + offset).isoformat()
        for visit in payload["visits"]:
            visit["serviceDate"] = (date.fromisoformat(visit["serviceDate"]) + offset).isoformat()
        crossover = HhaRequest.model_validate(payload)
        self.assertTrue(crossover.is_crossover)
        self.assertIs(crossover.release, CY_2026)
        lupa = {"sourceAdmissionIndicator": "1", "isFirstOrOnlyPeriod": True}
        self.assertTrue(HhaRequest.model_validate(dict(payload, lupa=lupa)).lupa.isFirstOrOnlyPeriod)
        shortened = deepcopy(payload)
        shortened.update(fromDate="2025-12-04", periodType="Partial30Day", partialPeriod={
            "eventType": "TransferToAnotherHha", "patientStatus": "06",
            "nextCareStartDate": "2026-01-02", "isCommonOwnershipTransfer": False,
        })
        shortened["visits"][0]["serviceDate"] = "2025-12-04"
        self.assertTrue(HhaRequest.model_validate(shortened).is_crossover)
        shortened.update(periodType="Shortened30Day", partialPeriod=None, shortenedPeriod={
            "eventType": "Death", "patientStatus": "20", "eventDate": "2026-01-01",
            "noSubsequentCareWithin30Days": True,
        })
        self.assertTrue(HhaRequest.model_validate(shortened).is_crossover)
        outlier = {"calendarYear": 2026, "asOfDate": "2026-01-01", "priorPaymentTotal": "100000",
                   "priorOutlierTotal": "1000", "totalsExcludeCurrentClaim": True, "basis": "SubmittedProviderYtdTotals"}
        late = dict(payload, noticeReceiptDate="2026-01-01", lateNoa={"exceptionStatus": "NoException"}, outlier=outlier)
        self.assertEqual(HhaRequest.model_validate(late).outlier.calendarYear, 2026)
        with self.assertRaisesRegex(ValidationError, "Through payment year"):
            HhaRequest.model_validate(dict(late, outlier=dict(outlier, calendarYear=2025, asOfDate="2025-12-31")))
        with self.assertRaisesRegex(ValidationError, "Late NOA requires explicit"):
            HhaRequest.model_validate(dict(late, lateNoa=None))
        with self.assertRaisesRegex(ValidationError, "supports original 0329"):
            HhaRequest.model_validate(dict(payload, billType="0327"))
        with self.assertRaisesRegex(ValidationError, "Full periods require 30"):
            HhaRequest.model_validate(dict(payload, fromDate="2025-12-04"))

    def test_lupa_context_requires_explicit_strict_facts(self):
        for context in [{}, {"sourceAdmissionIndicator": "1"}, {"isFirstOrOnlyPeriod": True},
                        {"sourceAdmissionIndicator": "X", "isFirstOrOnlyPeriod": True},
                        {"sourceAdmissionIndicator": "1", "isFirstOrOnlyPeriod": "false"},
                        {"sourceAdmissionIndicator": "1", "isFirstOrOnlyPeriod": 0},
                        {"sourceAdmissionIndicator": "1", "isFirstOrOnlyPeriod": True, "factor": 1}]:
            with self.subTest(context=context):
                payload = request_data()
                payload["lupa"] = context
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)
        payload = request_data()
        payload["lupa"] = {"sourceAdmissionIndicator": "B", "isFirstOrOnlyPeriod": False}
        self.assertFalse(HhaRequest.model_validate(payload).lupa.isFirstOrOnlyPeriod)

    def test_first_only_lupa_cannot_start_after_admission(self):
        payload = request_data()
        payload.update(admissionDate="2026-05-01", noticeReceiptDate="2026-05-01",
                       lupa={"sourceAdmissionIndicator": "1", "isFirstOrOnlyPeriod": True})
        with self.assertRaises(ValidationError):
            HhaRequest.model_validate(payload)
        payload["lupa"]["isFirstOrOnlyPeriod"] = False
        HhaRequest.model_validate(payload)

    def test_original_bill_and_calendar_values_are_preserved(self):
        model = HhaRequest.model_validate(request_data())
        self.assertEqual(model.billType, "329")
        self.assertEqual(model.serviceCountyFips, "01023")
        self.assertEqual(model.cbsaCode, "01")
        self.assertEqual(model.fromDate, date(2026, 6, 1))
        self.assertEqual(len(model.visits), 6)
        self.assertEqual(sum(visit.outlierUnits for visit in model.visits), 24)
        payload = request_data()
        payload.update(billType="329", providerCcn="A123456789012", cbsaCode="50001")
        self.assertEqual(HhaRequest.model_validate(payload).cbsaCode, "50001")

    def test_unsupported_claims_and_untrusted_fields_are_rejected(self):
        for field, value in [
            ("billType", "326"), ("billType", "328"), ("billType", "32A"),
            ("billType", "339"), ("billType", "349"), ("billType", 329),
            ("claimId", " "), ("claimId", " padded"), ("claimId", 42),
            ("providerCcn", "01799"), ("providerCcn", "a17999"),
            ("providerCcn", "A1234567890123"),
            ("hippsCode", "1fc11"), ("hippsCode", "1FC1"),
            ("serviceCountyFips", 1001), ("serviceCountyFips", "1001"),
            ("cbsaCode", 1), ("cbsaCode", "1"),
            ("periodType", "Partial"), ("periodType", False),
            ("geographyBasis", "PatientZip"),
            ("qualityReportingProgramIndicator", "0"),
            ("providerData", {"vbpAdjustment": "1"}),
        ]:
            with self.subTest(field=field, value=value):
                payload = request_data()
                payload[field] = value
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)

    def test_required_pricing_facts_have_no_invented_defaults(self):
        for field in ["hippsCode", "admissionDate", "fromDate", "throughDate", "noticeReceiptDate",
                      "periodType", "serviceCountyFips", "cbsaCode", "geographyBasis", "visits"]:
            with self.subTest(field=field):
                payload = request_data()
                payload.pop(field)
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)

    def test_dates_are_calendar_dates_not_coerced_timestamps(self):
        for value in [1780272000, True, "06/01/2026", "2026-06-01T00:00:00",
                      datetime(2026, 6, 1), "2026-02-30"]:
            with self.subTest(value=value):
                payload = request_data()
                payload["fromDate"] = value
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)

    def test_period_and_noa_scope_is_enforced(self):
        for changes in [
            {"throughDate": "2026-06-29"}, {"throughDate": "2026-07-01"},
            {"fromDate": "2027-06-01", "throughDate": "2027-06-30"},
            {"fromDate": "2025-12-03", "throughDate": "2026-01-01"},
            {"admissionDate": "2026-06-02"}, {"noticeReceiptDate": "2026-05-31"},
            {"noticeReceiptDate": "2026-06-07"},
        ]:
            with self.subTest(changes=changes):
                payload = request_data()
                payload.update(changes)
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)

    def test_timely_notice_and_continuing_admission_boundaries(self):
        payload = request_data()
        payload["noticeReceiptDate"] = "2026-06-06"
        HhaRequest.model_validate(payload)
        payload.update(admissionDate="2026-05-01", noticeReceiptDate="2026-05-06")
        HhaRequest.model_validate(payload)

    def test_visit_units_are_positive_strict_integers(self):
        for units in [True, False, 1.0, "1", 0, -1, 33]:
            with self.subTest(units=units):
                payload = request_data()["visits"][0]
                payload["outlierUnits"] = units
                with self.assertRaises(ValidationError):
                    HhaVisit.model_validate(payload)

    def test_daily_unit_limit_combines_different_disciplines(self):
        payload = request_data()
        payload["visits"] = [
            {"visitId": "NURSING", "serviceDate": "2026-06-01", "discipline": "SN", "outlierUnits": 16},
            {"visitId": "THERAPY", "serviceDate": "2026-06-01", "discipline": "PT", "outlierUnits": 16},
        ]
        HhaRequest.model_validate(payload)
        payload["visits"][1]["outlierUnits"] = 17
        with self.assertRaises(ValidationError):
            HhaRequest.model_validate(payload)

    def test_visit_identity_date_and_completeness(self):
        for change in ["duplicate", "outside", "missing_units", "unknown_discipline", "extra"]:
            with self.subTest(change=change):
                payload = request_data()
                if change == "duplicate":
                    payload["visits"].append(deepcopy(payload["visits"][0]))
                elif change == "outside":
                    payload["visits"][0]["serviceDate"] = "2026-05-31"
                elif change == "missing_units":
                    payload["visits"][0].pop("outlierUnits")
                elif change == "unknown_discipline":
                    payload["visits"][0]["discipline"] = "SUPPLIES"
                else:
                    payload["visits"][0]["quantityOfCoveredVisits"] = 100
                with self.assertRaises(ValidationError):
                    HhaRequest.model_validate(payload)

    def test_exact_decimal_output_and_unpriced_null_total(self):
        response = HhaResponse.model_validate(success_response())
        self.assertEqual(response.totalPayment, Decimal("1602.87"))
        self.assertIn('"totalPayment":"1602.87"', response.model_dump_json())
        self.assertEqual(response.pricingMethod, "HH_PPS")
        self.assertTrue(response.limitations)
        self.assertNotIn("lupa", response.model_dump(mode="json"))
        failed = HhaResponse(claimId="FAIL", status="UnableToPrice", reasonCode="HHA_MISSING_REFERENCE",
                             reason="Provider reference missing.")
        self.assertIsNone(failed.totalPayment)
        self.assertIn('"totalPayment":null', failed.model_dump_json())
        self.assertIn('"calculationVersion":null', failed.model_dump_json())
        self.assertNotIn("lupa", failed.model_dump(mode="json"))

    def test_invalid_financial_output_cannot_be_success(self):
        for amount in ["NaN", "Infinity", "-0.01"]:
            with self.subTest(amount=amount):
                payload = success_response()
                payload["totalPayment"] = amount
                with self.assertRaises(ValidationError):
                    HhaResponse.model_validate(payload)

    def test_success_requires_calculation_and_reference_evidence(self):
        for field in ["totalPayment", "calculationVersion", "providerData", "providerEvidence", "geographyEvidence", "cms"]:
            with self.subTest(field=field):
                payload = success_response()
                payload.pop(field)
                with self.assertRaises(ValidationError):
                    HhaResponse.model_validate(payload)

    def test_rejected_result_cannot_keep_a_payment_amount(self):
        payload = success_response()
        payload["status"] = "UnableToPrice"
        with self.assertRaises(ValidationError):
            HhaResponse.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
