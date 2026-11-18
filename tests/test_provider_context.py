import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from bridge.api_models import IoceClaimRequest
from bridge.provider_context import OPEN_TERMINATION_DATE, parse_provider_types, supplied_provider

ROOT = Path(__file__).resolve().parent.parent
OPPS_JAR = ROOT / "jars" / "pricers" / "opps-pricer-application-2.16.0.jar"


def rural_rule_context(**overrides):
    # A critical access hospital in a metro county, treated as reclassified to
    # its state's rural wage area (Iowa, SSA state code 16).
    context = {
        "providerCcn": "161322", "providerType": "37", "stateCode": "16", "effectiveDate": "2026-07-01",
        "cbsaActualGeographicLocation": "19780", "specialPaymentIndicator": "Y",
        "cbsaWageIndexLocation": "16", "hospitalQualityIndicator": "1",
    }
    context.update(overrides)
    return context


def emergency_claim(context=None, **claim_overrides):
    def line(number, revenue, hcpcs, charge, modifiers=()):
        return {
            "claimLineId": number, "lineNumber": number, "revenueCode": revenue, "hcpcs": hcpcs,
            "dateOfService": "2026-07-29", "units": 1, "chargeAmount": charge, "modifiers": list(modifiers),
        }

    claim = {
        "claimId": "TEST-CAH-LOCATION", "billType": "131", "providerNumber": "161322", "patientAge": 56,
        "patientSex": "U", "principalDiagnosis": {"code": "M25571"},
        "claimLines": [
            line(1, "0320", "73610", "490.00", ["RT"]),
            line(2, "0320", "73630", "454.00", ["RT"]),
            line(3, "0450", "99283", "672.00", ["25"]),
        ],
    }
    if context is not None:
        claim["providerContext"] = context
    claim.update(claim_overrides)
    return claim


class ProviderContextModelTests(unittest.TestCase):
    def test_rural_rule_context_is_accepted(self):
        context = IoceClaimRequest.model_validate(emergency_claim(rural_rule_context())).providerContext
        self.assertEqual(context.providerCcn, "161322")
        self.assertEqual(context.cbsaActualGeographicLocation, "19780")
        self.assertEqual(context.specialPaymentIndicator, "Y")
        self.assertEqual(context.cbsaWageIndexLocation, "16")

    def test_context_is_optional(self):
        self.assertIsNone(IoceClaimRequest.model_validate(emergency_claim()).providerContext)

    def test_actual_location_without_reclassification_is_accepted(self):
        context = rural_rule_context(cbsaActualGeographicLocation="16", specialPaymentIndicator="", cbsaWageIndexLocation="")
        self.assertEqual(IoceClaimRequest.model_validate(emergency_claim(context)).providerContext.cbsaWageIndexLocation, "")

    def test_area_codes_must_be_pricer_table_keys(self):
        for value in ["99916", "1978", "197800", "1", "1978A", ""]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(cbsaActualGeographicLocation=value)))
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(cbsaWageIndexLocation="99916")))

    def test_reclassification_requires_a_wage_index_location_and_only_with_it(self):
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(cbsaWageIndexLocation="")))
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(specialPaymentIndicator="")))
        for indicator in ["D", "1", "N"]:
            with self.subTest(indicator=indicator), self.assertRaises(ValidationError):
                IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(specialPaymentIndicator=indicator)))

    def test_context_must_describe_the_claim_provider(self):
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(providerCcn="161303")))
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(stateCode="19")))

    def test_effective_date_cannot_follow_the_claim(self):
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(effectiveDate="2026-07-30")))
        accepted = IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(effectiveDate="2026-07-29")))
        self.assertEqual(str(accepted.providerContext.effectiveDate), "2026-07-29")

    def test_quality_indicator_and_unknown_fields(self):
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(hospitalQualityIndicator="0")))
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(operatingCostToChargeRatio=0.5)))


class ProviderContextAllowlistTests(unittest.TestCase):
    def test_parse_provider_types(self):
        self.assertEqual(parse_provider_types(None), frozenset())
        self.assertEqual(parse_provider_types("  "), frozenset())
        self.assertEqual(parse_provider_types("37"), frozenset({"37"}))
        self.assertEqual(parse_provider_types("37, 01;7a"), frozenset({"37", "01", "7A"}))
        for raw in ["3", "037", "3-"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_provider_types(raw)


class ProviderContextMappingTests(unittest.TestCase):
    def test_supplied_provider_carries_only_the_context(self):
        payload = IoceClaimRequest.model_validate(emergency_claim(rural_rule_context(), npi="1669477089"))
        provider = supplied_provider(payload)
        self.assertEqual(provider.provider_ccn, "161322")
        self.assertEqual(provider.national_provider_identifier, "1669477089")
        self.assertEqual(provider.provider_type, "37")
        self.assertEqual(provider.state_code, "16")
        self.assertEqual(provider.effective_date, 20260701)
        self.assertEqual(provider.fiscal_year_begin_date, 20260701)
        self.assertEqual(provider.termination_date, OPEN_TERMINATION_DATE)
        self.assertEqual(provider.cbsa_actual_geographic_location, "19780")
        self.assertEqual(provider.cbsa_wage_index_location, "16")
        self.assertEqual(provider.special_payment_indicator, "Y")
        self.assertEqual(provider.hospital_quality_indicator, "1")
        for field in ["operating_cost_to_charge_ratio", "county_code", "special_wage_index", "supplemental_wage_index", "payment_cbsa"]:
            self.assertIsNone(getattr(provider, field), field)

    def test_supplied_provider_requires_a_context(self):
        with self.assertRaises(ValueError):
            supplied_provider(IoceClaimRequest.model_validate(emergency_claim()))


class ProviderContextEndpointTests(unittest.TestCase):
    def test_disallowed_type_is_rejected_before_processing(self):
        import bridge.main as main

        # No engine is loaded: the allowlist check must come before any engine use.
        unready = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        original = main.PROVIDER_CONTEXT_TYPES
        try:
            payload = IoceClaimRequest.model_validate(emergency_claim(rural_rule_context()))
            for allowed in (frozenset(), frozenset({"01"})):
                main.PROVIDER_CONTEXT_TYPES = allowed
                with self.subTest(allowed=sorted(allowed)), self.assertRaises(HTTPException) as raised:
                    main.process_opps(payload, unready)
                self.assertEqual(raised.exception.status_code, 422)
                self.assertEqual(raised.exception.detail["providerType"], "37")
                self.assertEqual(raised.exception.detail["allowedProviderTypes"], sorted(allowed))
        finally:
            main.PROVIDER_CONTEXT_TYPES = original

    def test_allowed_or_absent_context_passes_the_check(self):
        import bridge.main as main

        original = main.PROVIDER_CONTEXT_TYPES
        try:
            main.PROVIDER_CONTEXT_TYPES = frozenset({"37"})
            main.enforce_provider_context(IoceClaimRequest.model_validate(emergency_claim(rural_rule_context())))
            main.PROVIDER_CONTEXT_TYPES = frozenset()
            main.enforce_provider_context(IoceClaimRequest.model_validate(emergency_claim()))
        finally:
            main.PROVIDER_CONTEXT_TYPES = original

    def test_ioce_endpoint_rejects_a_provider_context(self):
        import bridge.main as main

        unready = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        original = main.PROVIDER_CONTEXT_TYPES
        try:
            main.PROVIDER_CONTEXT_TYPES = frozenset({"37"})
            with self.assertRaises(HTTPException) as raised:
                main.process_ioce(IoceClaimRequest.model_validate(emergency_claim(rural_rule_context())), unready)
            self.assertEqual(raised.exception.status_code, 422)
        finally:
            main.PROVIDER_CONTEXT_TYPES = original


@unittest.skipUnless(list((ROOT / "jars").glob("ioce-standalone-*.jar")) and OPPS_JAR.is_file(),
                     "The CMS I/OCE and OPPS Pricer jars are not available.")
class ProviderContextPricerTests(unittest.TestCase):
    """Runs the unmodified CMS editor and OPPS Pricer with a supplied context."""

    @classmethod
    def setUpClass(cls):
        import jpype
        from sqlalchemy import create_engine

        if not jpype.isJVMStarted():
            jpype.startJVM(classpath=[str(ROOT / "jars" / "*")])

        try:
            from bridge.opps_client import WindowsSafeOppsClient
            from myelin.ioce.ioce_client import IoceClient

            cls.ioce = IoceClient()
            # The supplied context needs no provider table; any engine satisfies the client.
            cls.opps = WindowsSafeOppsClient(jar_path=str(OPPS_JAR), db=create_engine("sqlite://"))
        except Exception as error:  # A JVM started elsewhere without the CMS jars.
            raise unittest.SkipTest(f"The CMS components could not be loaded: {error}") from error

    def price(self, context):
        from bridge.claim_mapper import to_myelin_claim

        payload = IoceClaimRequest.model_validate(emergency_claim(context))
        claim = to_myelin_claim(payload)
        output, provider = self.opps.process(claim, supplied_provider(payload), self.ioce.process(claim, include_descriptions=False))
        return output, provider

    def test_rural_rule_uses_the_state_rural_wage_area(self):
        output, provider = self.price(rural_rule_context())
        self.assertEqual(output.return_code.code, "01")
        self.assertEqual(output.final_cbsa, "16")
        self.assertAlmostEqual(output.final_wage_index, 0.9016, places=4)
        self.assertAlmostEqual(output.total_claim_payment, 262.42, places=2)
        self.assertEqual([line.payment for line in output.service_lines][:2], [0.0, 0.0])
        self.assertEqual(provider.provider_ccn, "161322")

    def test_actual_location_uses_the_metro_area(self):
        output, _ = self.price(rural_rule_context(specialPaymentIndicator="", cbsaWageIndexLocation=""))
        self.assertEqual(output.return_code.code, "01")
        self.assertEqual(output.final_cbsa, "19780")
        self.assertAlmostEqual(output.final_wage_index, 0.9090, places=4)
        self.assertAlmostEqual(output.total_claim_payment, 263.66, places=2)

    def test_unknown_area_is_a_cms_wage_index_failure(self):
        output, _ = self.price(rural_rule_context(specialPaymentIndicator="", cbsaWageIndexLocation="",
                                                  cbsaActualGeographicLocation="12060"))
        self.assertEqual(output.return_code.code, "50")


if __name__ == "__main__":
    unittest.main()
