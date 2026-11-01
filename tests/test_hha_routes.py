"""HTTP contract checks without external HTTP clients or service startup."""

import asyncio
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from bridge.hha_models import HhaResponse
from bridge.hha_routes import readiness, router
from tests.test_hha_models import request_data, success_response


async def _asgi_request(app, method, path, payload=None):
    body = payload if isinstance(payload, bytes) else b"" if payload is None else json.dumps(payload).encode("utf-8")
    messages = []
    received = False

    async def receive():
        nonlocal received
        if received:
            await asyncio.Event().wait()
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    result = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return status, json.loads(result)


def request(app, method, path, payload=None):
    return asyncio.run(asyncio.wait_for(_asgi_request(app, method, path, payload), 5))


class FakeProcessor:
    def __init__(self, result="success"):
        self.result = result
        self.requests = []

    def process(self, payload):
        self.requests.append(payload)
        if self.result == "error":
            raise RuntimeError("private SQL password; patient details; java stack trace")
        if self.result == "unpriced":
            return HhaResponse(
                claimId=payload.claimId, status="UnableToPrice", reasonCode="HHA_PROVIDER_NOT_FOUND",
                reason="No dated HH provider reference was found.",
            )
        result = success_response()
        result["claimId"] = payload.claimId
        result["cms"]["paymentData"] = {"totalPayment": Decimal("1602.87")}
        return HhaResponse.model_validate(result)


class HhaRouteChecks(unittest.TestCase):
    def setUp(self):
        jvm = patch("bridge.hha_routes.jpype.isJVMStarted", return_value=True)
        jvm.start()
        self.addCleanup(jvm.stop)
        self.app = FastAPI()
        self.app.include_router(router)
        self.processor = FakeProcessor()
        self.app.state.hha_processor = self.processor

    def test_ready_returns_exact_release_and_supported_scope(self):
        status, body = request(self.app, "GET", "/api/v1/hha/ready")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertTrue(body["ready"])
        self.assertEqual(body["pricerRelease"], "2.6.0")
        self.assertEqual(body["calculationYears"], [2026])
        self.assertEqual(body["calculationVersions"], {"2026": "2026.0"})
        self.assertEqual(body["firstServiceDate"], "2026-01-01")
        self.assertEqual(body["lastServiceDate"], "2026-12-31")
        self.assertEqual(body["grouping"], "SuppliedHippsOnly")
        self.assertEqual(body["supportedPayment"], "OrdinaryPeriodLupaPartialPeriodShortenedPeriodLateNoaAndOutlier")
        self.assertEqual(body["supportedPeriodTypes"], ["Full30Day", "Partial30Day", "Shortened30Day"])

    def test_outlier_context_and_raw_policy_evidence_cross_http_boundary(self):
        cases=json.loads((Path(__file__).parent/'fixtures/hha-2026-outlier.json').read_text())['comparisons']
        case=next(case for case in cases if case['name']=='ordinary_zero_history')
        expected=HhaResponse.model_validate(case['bridge'])
        with patch.object(self.processor,'process',return_value=expected):
            status,body=request(self.app,'POST','/api/v1/hha/process',case['request'])
        self.assertEqual(status,200)
        self.assertEqual(body['totalPayment'],'1610.68')
        self.assertEqual(body['cms']['returnCodeData']['code'],'01')
        self.assertEqual(body['outlier']['effectiveReturnCode'],'02')
        self.assertEqual(body['outlier']['adjustmentMethod'],'ZeroHistoryCapEnforcement')

    def test_invalid_outlier_history_never_reaches_processor(self):
        for context in ({},{'calendarYear':2025},{'calendarYear':2026,'totalsExcludeCurrentClaim':False}):
            data=request_data();data['outlier']=context
            status,_=request(self.app,'POST','/api/v1/hha/process',data)
            self.assertEqual(status,422)
        self.assertFalse(self.processor.requests)

    def test_partial_context_and_exact_adjustment_evidence_cross_http_boundary(self):
        from tests.test_hha_partial import fixture_cases
        case = fixture_cases()[0]
        expected = HhaResponse.model_validate(case["bridge"])
        with patch.object(self.processor, "process", return_value=expected):
            status, body = request(self.app, "POST", "/api/v1/hha/process", case["request"])
        self.assertEqual(status, 200)
        self.assertEqual(body["reasonCode"], "HHA_PARTIAL_PERIOD_ESTIMATED")
        self.assertEqual(body["totalPayment"], "805.34")
        self.assertEqual(body["partialPeriod"]["paymentRatio"], "0.50")
        self.assertTrue(body["partialPeriod"]["adjustmentApplied"])

    def test_invalid_partial_context_never_reaches_processor(self):
        from tests.test_hha_partial import partial_request
        for context in [None, {}, {"eventType": "Death"}]:
            data = partial_request()
            data["partialPeriod"] = context
            status, _ = request(self.app, "POST", "/api/v1/hha/process", data)
            self.assertEqual(status, 422)
        self.assertFalse(self.processor.requests)

    def test_disabled_and_missing_processors_return_503(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    del self.app.state.hha_processor
                else:
                    self.app.state.hha_processor = None
                for method, path, payload in [
                    ("GET", "/api/v1/hha/ready", None),
                    ("POST", "/api/v1/hha/process", request_data()),
                ]:
                    status, body = request(self.app, method, path, payload)
                    self.assertEqual(status, 503)
                    self.assertEqual(body, {"detail": "HHA pricing is disabled or unavailable."})
        self.assertFalse(self.processor.requests)
        self.assertEqual(readiness(None)["ready"], False)

    def test_processor_without_a_running_jvm_is_unavailable(self):
        with patch("bridge.hha_routes.jpype.isJVMStarted", return_value=False):
            self.assertTrue(readiness(self.processor)["enabled"])
            self.assertFalse(readiness(self.processor)["ready"])
            status, _ = request(self.app, "GET", "/api/v1/hha/ready")
            self.assertEqual(status, 503)
            status, _ = request(self.app, "POST", "/api/v1/hha/process", request_data())
            self.assertEqual(status, 503)
        self.assertFalse(self.processor.requests)

    def test_original_payment_preserves_identity_and_exact_decimal_strings(self):
        payload = request_data()
        payload["claimId"] = "HH-HTTP-IDENTITY"
        status, body = request(self.app, "POST", "/api/v1/hha/process", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["claimId"], payload["claimId"])
        self.assertEqual(body["status"], "Success")
        self.assertEqual(body["pricingMethod"], "HH_PPS")
        self.assertEqual(body["totalPayment"], "1602.87")
        self.assertEqual(body["cms"]["paymentData"]["totalPayment"], "1602.87")
        self.assertTrue(body["limitations"])
        self.assertNotIn("lupa", body)
        self.assertNotIn("partialPeriod", body)
        self.assertEqual(len(self.processor.requests), 1)
        self.assertEqual(self.processor.requests[0].billType, "329")
        self.assertEqual(self.processor.requests[0].serviceCountyFips, "01023")
        self.assertTrue(body["providerEvidence"])
        self.assertTrue(body["geographyEvidence"])

    def test_unsupported_and_spoofed_requests_never_reach_processor(self):
        for changes in [
            {"billType": "327"}, {"billType": "328"}, {"billType": "32A"},
            {"fromDate": "2027-06-01", "throughDate": "2027-06-30"},
            {"qualityReportingProgramIndicator": "0"},
            {"providerData": {"vbpAdjustment": "1"}},
            {"unknownField": "spoofed"}, {"periodType": "Partial"},
        ]:
            with self.subTest(changes=changes):
                payload = request_data()
                payload.update(changes)
                status, body = request(self.app, "POST", "/api/v1/hha/process", payload)
                self.assertEqual(status, 422)
                self.assertTrue(body["detail"])
        self.assertFalse(self.processor.requests)

    def test_missing_and_malformed_body_is_422(self):
        for payload in (None, {}, [request_data()], "invalid request", b'{"claimId":'):
            with self.subTest(payload=type(payload).__name__):
                status, body = request(self.app, "POST", "/api/v1/hha/process", payload)
                self.assertEqual(status, 422)
                self.assertTrue(body["detail"])
        self.assertFalse(self.processor.requests)

    def test_failure_is_sanitized_and_contains_no_claim_or_backend_details(self):
        self.app.state.hha_processor = FakeProcessor("error")
        status, body = request(self.app, "POST", "/api/v1/hha/process", request_data())
        self.assertEqual(status, 503)
        self.assertEqual(body, {"detail": "HHA pricing failed; retry after checking service availability."})
        encoded = json.dumps(body)
        for secret in ("password", "patient details", "java stack", "HHA-CONTRACT-TEST"):
            self.assertNotIn(secret, encoded)

    def test_unpriced_outcome_is_explicit_with_null_total(self):
        self.app.state.hha_processor = FakeProcessor("unpriced")
        status, body = request(self.app, "POST", "/api/v1/hha/process", request_data())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "UnableToPrice")
        self.assertEqual(body["reasonCode"], "HHA_PROVIDER_NOT_FOUND")
        self.assertIsNone(body["totalPayment"])
        self.assertIn("calculationVersion", body)
        self.assertNotIn("lupa", body)
        self.assertEqual(body["claimId"], "HHA-CONTRACT-TEST")

    def test_main_registers_router_and_reports_method_specific_readiness(self):
        # Importing the app does not enter its lifespan, initialize a JVM, or
        # open provider databases. Patch only readiness dependencies here.
        from bridge import main

        paths = main.app.openapi()["paths"]
        self.assertIn("/api/v1/hha/process", paths)
        self.assertIn("/api/v1/hha/ready", paths)
        with (
            patch.object(main.app.state, "opps_jar", Path("opps-pricer-fixture.jar"), create=True),
            patch.object(main.app.state, "opsf_record_count", 1, create=True),
            patch.object(main.app.state, "hha_processor", None, create=True),
            patch.object(main.app.state, "ioce_client", object(), create=True),
            patch.object(main.app.state, "opps_client", object(), create=True),
            patch.object(main.jpype, "isJVMStarted", return_value=True),
        ):
            status, body = request(main.app, "GET", "/api/v1/version")
            self.assertEqual(status, 200)
            self.assertFalse(body["hha"]["enabled"])
            self.assertEqual(body["hha"]["calculationYears"], [2026])
            status, body = request(main.app, "GET", "/health/ready")
            self.assertEqual(status, 200)
            self.assertFalse(body["hha"]["ready"])
            status, _ = request(main.app, "GET", "/api/v1/hha/ready")
            self.assertEqual(status, 503)
            main.app.state.hha_processor = self.processor
            status, body = request(main.app, "GET", "/api/v1/version")
            self.assertEqual(status, 200)
            self.assertTrue(body["hha"]["ready"])


    def test_late_noa_http_retains_raw_cms_and_explicit_net_evidence(self):
        case = json.loads((Path(__file__).parent / "fixtures/hha-2026-late-noa.json").read_text())["comparisons"][0]
        class Captured:
            def process(self, payload):
                assert payload.lateNoa.exceptionStatus == "NoException"
                return HhaResponse.model_validate(case["bridge"])
        self.app.state.hha_processor = Captured()
        status, body = request(self.app, "POST", "/api/v1/hha/process", case["request"])
        self.assertEqual(status, 200)
        self.assertEqual(body["totalPayment"], "1288.54")
        self.assertEqual(body["lateNoa"]["penaltyAmount"], "322.14")
        self.assertTrue(body["lateNoa"]["providerLiability"])
        self.assertEqual(body["lateNoa"]["beneficiaryLiabilityAmount"], "0.00")

    def test_late_noa_http_rejects_missing_context_and_exception_spoof(self):
        case = json.loads((Path(__file__).parent / "fixtures/hha-2026-late-noa.json").read_text())["comparisons"][0]
        for context in (None, {}, {"exceptionStatus": "Granted"}, {"exceptionStatus": "NoException", "penaltyAmount": 0}):
            payload = dict(case["request"], lateNoa=context)
            status, _ = request(self.app, "POST", "/api/v1/hha/process", payload)
            self.assertEqual(status, 422)
        self.assertFalse(self.processor.requests)


if __name__ == "__main__":
    unittest.main()
