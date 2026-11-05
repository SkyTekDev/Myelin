"""Pinned CMS LUPA calculations, input attestation and exact allocations."""
import copy
import json
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from bridge.hha_client import ordinal
from bridge.hha_models import HhaRequest
from tests import test_hha_integration as integration
from tests.test_hha_models import request_data


def lupa_request(disciplines=("SN",), *, first=True, source="1", provider="017009"):
    payload = request_data()
    payload.update(providerCcn=provider, lupa=dict(sourceAdmissionIndicator=source, isFirstOrOnlyPeriod=first))
    payload["visits"] = [dict(visitId=str(index + 1), serviceDate=f"2026-06-{index * 4 + 1:02d}",
                              discipline=discipline, outlierUnits=4) for index, discipline in enumerate(disciplines)]
    return payload


class HhaLupaChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integration.HhaCmsIntegrationChecks.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        integration.HhaCmsIntegrationChecks.tearDownClass.__func__(cls)

    def price(self, payload):
        return self.processor.process(HhaRequest.model_validate(payload))

    def test_initial_and_suppressed_addons(self):
        cases = [("SN", "222.63"), ("PT", "229.55"), ("OT", "245.54"), ("SLP", "256.77")]
        for discipline, expected in cases:
            with self.subTest(discipline=discipline):
                result = self.price(lupa_request((discipline,)))
                self.assertEqual(result.status, "Success")
                self.assertEqual(result.totalPayment, Decimal(expected))
                self.assertEqual(result.reasonCode, "HHA_LUPA_ESTIMATED")
                self.assertEqual(result.cms["returnCodeData"]["code"], "14")
                self.assertEqual(result.cms["paymentData"]["hhrgPayment"], 0)
                self.assertEqual(sum(row.totalPayment for row in result.lupa.visitPayments), result.totalPayment)
        for source, first in [("B", True), ("1", False), ("B", False)]:
            result = self.price(lupa_request(source=source, first=first))
            self.assertEqual(result.totalPayment, Decimal("129.44"))
            self.assertEqual(result.cms["returnCodeData"]["code"], "06")
            self.assertEqual(result.lupa.visitPayments[0].addOnPayment, 0)

    def test_earliest_skilled_discipline_and_same_day_priority(self):
        result = self.price(lupa_request(("PT", "SN", "OT")))
        self.assertGreater(result.lupa.visitPayments[0].addOnPayment, 0)
        self.assertEqual([row.discipline for row in result.lupa.visitPayments if row.addOnPayment], ["PT"])
        payload = lupa_request(("PT", "OT", "SN"))
        for visit in payload["visits"]:
            visit["serviceDate"] = "2026-06-01"
        result = self.price(payload)
        self.assertEqual([row.discipline for row in result.lupa.visitPayments if row.addOnPayment], ["SN"])

    def test_equal_cent_allocations_are_stable_and_complete(self):
        payload = lupa_request(("SN", "SN", "SN"))
        payload["visits"] = list(reversed(payload["visits"]))
        result = self.price(payload)
        rows = result.lupa.visitPayments
        self.assertEqual([row.visitId for row in rows], ["1", "2", "3"])
        self.assertLessEqual(max(row.basePayment for row in rows) - min(row.basePayment for row in rows), Decimal("0.01"))
        self.assertGreaterEqual(rows[0].basePayment, rows[-1].basePayment)
        self.assertEqual(sum(row.basePayment for row in rows), result.cms["paymentData"]["revenuePayments"][3]["cost"])
        self.assertEqual(sum(row.addOnPayment for row in rows), result.cms["paymentData"]["revenuePayments"][3]["addOnVisitAmount"])
        self.assertEqual(sum(row.totalPayment for row in rows), result.totalPayment)
        self.assertEqual(sorted(["\ue000", "\U0001f600"], key=ordinal), ["\U0001f600", "\ue000"])

    def test_hipps_threshold_controls_payment_classification(self):
        payload = lupa_request(("SN",) * 3)
        self.assertEqual(self.price(payload).cms["returnCodeData"]["code"], "14")
        payload["visits"].append(dict(visitId="4", serviceDate="2026-06-15", discipline="SN", outlierUnits=4))
        ordinary = self.price(payload)
        self.assertEqual(ordinary.status, "Success")
        self.assertEqual(ordinary.cms["returnCodeData"]["code"], "00")
        self.assertIsNone(ordinary.lupa)
        payload["hippsCode"] = "1IB11"
        lupa = self.price(payload)
        self.assertEqual(lupa.status, "Success")
        self.assertEqual(lupa.cms["returnCodeData"]["code"], "14")
        self.assertEqual(lupa.totalPayment, Decimal("610.93"))

    def test_initial_unskilled_only_fails_closed_but_later_can_price(self):
        for source in ("1", "B"):
            result = self.price(lupa_request(("MSW", "HHA"), source=source))
            self.assertEqual(result.reasonCode, "HHA_LUPA_CONTEXT_UNSUPPORTED")
            self.assertIsNone(result.totalPayment)
        result = self.price(lupa_request(("MSW", "HHA"), first=False))
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.cms["returnCodeData"]["code"], "06")
        self.assertEqual({row.discipline for row in result.lupa.visitPayments}, {"MSW", "HHA"})

    def test_qrp_vbp_geography_and_larger_threshold(self):
        for provider in ("017999", "017998", "017997", "017996"):
            result = self.price(lupa_request(("SN", "SN", "SN"), provider=provider))
            self.assertEqual(result.status, "Success")
            self.assertEqual(sum(row.totalPayment for row in result.lupa.visitPayments), result.totalPayment)
        payload = lupa_request(("OT",))
        payload.update(serviceCountyFips="48441", cbsaCode="10180")
        self.assertEqual(self.price(payload).status, "Success")
        payload.update(serviceCountyFips="01129", cbsaCode="50001")
        self.assertEqual(self.price(payload).status, "Success")
        code = next(code for code, row in self.processor.hrg_rows.items() if int(row["threshold"]) == 5)
        payload = lupa_request(("SN",) * 4)
        payload["hippsCode"] = code
        result = self.price(payload)
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.cms["paymentData"]["standardizedPayment"], Decimal("835.25"))
        payload["visits"].append(dict(visitId="5", serviceDate="2026-06-20", discipline="SN", outlierUnits=4))
        result = self.price(payload)
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.cms["returnCodeData"]["code"], "00")
        self.assertIsNone(result.lupa)

    def test_tampered_lupa_money_rates_and_categorical_evidence_are_rejected(self):
        payload = lupa_request()
        original = self.price(payload).cms
        variants = [("totalPayment", Decimal("222.64")), ("hhrgPayment", Decimal("1")),
                    ("hhrgWeight", Decimal("1")), ("outlierPayment", Decimal(".01")),
                    ("lateSubmissionPenaltyAmount", Decimal(".01")),
                    ("standardizedPayment", Decimal("1")), ("totalQuantityOfCoveredVisits", True),
                    ("valueBasedPurchasingAdjustmentAmount", Decimal("1"))]
        for field, value in variants:
            cms = copy.deepcopy(original)
            cms["paymentData"][field] = value
            with self.subTest(field=field), patch.object(self.processor, "calculate", return_value=cms):
                with self.assertRaises(RuntimeError):
                    self.price(payload)
        for field in ("cost", "dollarRate", "addOnVisitAmount"):
            cms = copy.deepcopy(original)
            cms["paymentData"]["revenuePayments"][3][field] += Decimal(".01")
            with self.subTest(field=field), patch.object(self.processor, "calculate", return_value=cms):
                with self.assertRaises(RuntimeError):
                    self.price(payload)
        cms = copy.deepcopy(original)
        cms["returnCodeData"]["code"] = "06"
        with patch.object(self.processor, "calculate", return_value=cms):
            with self.assertRaises(RuntimeError):
                self.price(payload)

    def test_saved_standalone_lupa_outputs_and_visit_allocations(self):
        fixture = Path(__file__).parent / "fixtures" / "hha-2026-lupa.json"
        self.assertTrue(fixture.exists(), "Pinned standalone LUPA evidence must be retained.")
        for case in json.loads(fixture.read_text(encoding="utf-8"))["comparisons"]:
            with self.subTest(case=case["name"]):
                result = self.price(case["request"])
                self.assertEqual(result.totalPayment, Decimal(case["expectedPayment"]))
                actual = json.loads(result.model_dump_json())["cms"]
                captured = copy.deepcopy(case["standalone"])
                for item in (actual, captured):
                    for field in ("finalCbsa", "finalWageIndex"):
                        if item["paymentData"].get(field) is None:
                            item["paymentData"].pop(field, None)
                self.assertEqual(actual, captured)
                self.assertEqual(json.loads(result.model_dump_json())["lupa"], case["bridge"]["lupa"])
