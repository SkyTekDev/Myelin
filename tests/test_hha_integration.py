"""Real pinned CMS calculations with isolated synthetic provider data."""

import copy
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import jpype
from pydantic_core import to_json
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from bridge.hha_client import HhaProcessor
from bridge.hha_models import HhaRequest
from myelin.pricers.ipsf import Base, IPSF
from tests.test_hha_models import request_data


class HhaCmsIntegrationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jar_root = Path(os.getenv("HHA_TEST_JAR_ROOT", str(Path(__file__).resolve().parents[1] / "jars")))
        if not jpype.isJVMStarted():
            jpype.startJVM(convertStrings=True)
        cls.db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(cls.db)
        with Session(cls.db) as session:
            for ccn, qrp, vbp in [("017999", "0", 1), ("017998", "2", 1),
                                  ("017997", "0", 0.95), ("017996", "0", 1.05),
                                  ("017009", "0", 1.00487)]:
                session.add(IPSF(provider_ccn=ccn, provider_type="36", effective_date=20260101,
                                 federal_pps_blend=qrp, special_provider_update_factor=vbp,
                                 vbp_adjustment=0.12345, termination_date=None))
            session.commit()
        from tests.hha_geography_fixture import seed_geography
        seed_geography(cls.db)
        from tests.hha_a9272_fixture import seed_rates
        seed_rates(cls.db)
        cls.processor = HhaProcessor(cls.db, cls.jar_root)

    @classmethod
    def tearDownClass(cls):
        cls.db.dispose()

    def test_captured_ordinary_payments_qrp_vbp_and_geography(self):
        # Values independently calculated from the published CMS constants and
        # intermediate HALF_UP rounding, then compared to the standalone REST
        # executable by tests/reconcile_hha.py before promotion.
        cases = [
            ({}, "1602.87"),
            ({"serviceCountyFips": "48441", "cbsaCode": "10180"}, "2084.49"),
            ({"providerCcn": "017998"}, "1571.57"),
            ({"providerCcn": "017997"}, "1522.73"),
            ({"providerCcn": "017996"}, "1683.01"),
            ({"serviceCountyFips": "01129", "cbsaCode": "50001", "hippsCode": "1FC21"}, "1762.31"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                data = request_data()
                data.update(changes)
                output = self.processor.process(HhaRequest.model_validate(data))
                self.assertEqual(output.status, "Success", output)
                self.assertEqual(output.totalPayment, Decimal(expected))
                self.assertEqual(output.cms["returnCodeData"]["code"], "00")
                self.assertEqual(output.calculationVersion, "2026.0")
                self.assertEqual(len(output.cms["paymentData"]["revenuePayments"]), 6)
                self.assertEqual(output.providerEvidence["source"], "IPSF")
                self.assertEqual(output.providerEvidence["valueBasedPurchasingSourceField"], "special_provider_update_factor")
                self.assertEqual(json.loads(output.model_dump_json())["totalPayment"], expected)

    def test_0327_passes_the_actual_bill_type_to_cms(self):
        original = self.processor.process(HhaRequest.model_validate(request_data()))
        adjustment = HhaRequest.model_validate(dict(request_data(), billType="0327"))
        with patch.object(self.processor, "calculate", wraps=self.processor.calculate) as calculate:
            corrected = self.processor.process(adjustment)
        self.assertEqual(calculate.call_args.args[0]["claimData"]["typeOfBill"], "327")
        self.assertEqual(corrected.status, "Success")
        self.assertEqual(corrected.totalPayment, original.totalPayment)
        self.assertEqual(corrected.cms, original.cms)

    def test_dates_visits_and_zero_discipline_entries_are_explicit(self):
        model = HhaRequest.model_validate(request_data())
        from bridge.hha_provider import provider_for_period
        from bridge.hha_geography import resolve_geography
        provider = provider_for_period(self.db, model.providerCcn, model.fromDate, model.throughDate)
        geo = resolve_geography(model.serviceCountyFips, model.cbsaCode, self.processor.wage_rows, self.processor.geography_catalog)
        payload = self.processor.build_payload(model, provider, geo)
        claim = payload["claimData"]
        self.assertEqual(claim["hhrgNumberOfDays"], 30)
        self.assertEqual(claim["partialEpisodePaymentIndicator"], "N")
        self.assertEqual([row["revenueCode"] for row in claim["revenueLines"]], ["0420", "0430", "0440", "0550", "0560", "0570"])
        self.assertEqual([row["quantityOfCoveredVisits"] for row in claim["revenueLines"]], [0, 0, 0, 6, 0, 0])
        self.assertEqual(claim["revenueLines"][3]["quantityOfOutlierUnits"], 24)
        self.assertEqual(payload["providerData"]["countyCode"], "01023")
        self.assertEqual(payload["providerData"]["cbsaActualGeographicLocation"], "99901")

    def test_saved_standalone_outputs_reconcile(self):
        fixture = Path(__file__).parent / "fixtures" / "hha-2026-ordinary.json"
        cases = json.loads(fixture.read_text(encoding="utf-8"))["comparisons"]
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(case=case["name"]):
                model = HhaRequest.model_validate(case["request"])
                result = self.processor.process(model)
                self.assertEqual(result.totalPayment, Decimal(case["expectedPayment"]))
                actual = json.loads(to_json(result.cms))
                captured = copy.deepcopy(case["standalone"])
                # The standalone server omits these two optional null fields.
                for value in (actual, captured):
                    payment = value.get("paymentData", {})
                    for key in ("finalCbsa", "finalWageIndex"):
                        if payment.get(key) is None:
                            payment.pop(key, None)
                self.assertEqual(actual, captured)

    def test_missing_hipps_provider_and_missing_lupa_context_return_no_amount(self):
        for changes, reason in [({"hippsCode": "ZZZZZ"}, "HHA_HIPPS_NOT_FOUND"),
                                ({"providerCcn": "999999"}, "HHA_PROVIDER_NOT_FOUND")]:
            data = request_data()
            data.update(changes)
            output = self.processor.process(HhaRequest.model_validate(data))
            self.assertEqual(output.reasonCode, reason)
            self.assertIsNone(output.totalPayment)
        data = request_data()
        data["visits"] = data["visits"][:3]
        output = self.processor.process(HhaRequest.model_validate(data))
        self.assertEqual(output.reasonCode, "HHA_LUPA_CONTEXT_REQUIRED")
        self.assertIsNone(output.totalPayment)

    def test_computed_outlier_is_rejected_before_publishing_payment(self):
        data = request_data()
        data["visits"] = [dict(visitId=str(day), serviceDate=f"2026-06-{day:02d}",
                               discipline="SN", outlierUnits=32) for day in range(1, 31)]
        output = self.processor.process(HhaRequest.model_validate(data))
        self.assertEqual(output.reasonCode, "HHA_OUTLIER_CONTEXT_REQUIRED", output)
        self.assertIsNone(output.totalPayment)
        self.assertEqual(output.cms["returnCodeData"]["code"], "01")
        self.assertGreater(output.cms["paymentData"]["outlierPayment"], 0)

    def test_corrupt_release_fails_closed(self):
        with patch.dict(os.environ, {"HHA_JAR_PATH": str(Path(__file__).resolve())}):
            with self.assertRaisesRegex(RuntimeError, "release"):
                HhaProcessor(self.db, self.jar_root)

    def test_invalid_cms_identity_components_and_amounts_are_not_accepted(self):
        valid = self.processor.process(HhaRequest.model_validate(request_data())).cms
        variations = [
            ("calculationVersion", "2027.0"),
            ("totalPayment", Decimal("1602.88")), ("hhrgPayment", None),
            ("hhrgWeight", Decimal("1.1")), ("totalQuantityOfCoveredVisits", True),
            ("lateSubmissionPenaltyAmount", Decimal("0.01")),
            ("outlierPayment", Decimal("0.01")),
            ("valueBasedPurchasingAdjustmentAmount", Decimal("0.01")),
            ("revenuePayments", []), ("finalWageIndex", Decimal("1")),
            ("totalPayment", Decimal("NaN")),
        ]
        for field, value in variations:
            with self.subTest(field=field, value=value):
                cms = copy.deepcopy(valid)
                if field == "calculationVersion":
                    cms[field] = value
                else:
                    cms["paymentData"][field] = value
                with patch.object(self.processor, "calculate", return_value=cms):
                    with self.assertRaises(RuntimeError):
                        self.processor.process(HhaRequest.model_validate(request_data()))


if __name__ == "__main__":
    unittest.main()
