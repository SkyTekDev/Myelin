import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from bridge.api_models import IoceClaimRequest
from bridge.claim_mapper import to_myelin_claim
from bridge.edit_bypass import disallowed_bypasses, normalize_edit, parse_allowlist

ROOT = Path(__file__).resolve().parent.parent


def drug_claim(**line_overrides):
    line = {
        "claimLineId": 2, "lineNumber": 1, "revenueCode": "0636", "hcpcs": "J0586",
        "dateOfService": "2026-07-30", "units": 120, "chargeAmount": "3000.00",
    }
    line.update(line_overrides)
    return {
        "claimId": "990104", "billType": "131", "providerNumber": "290019", "patientAge": 56,
        "patientSex": "U", "principalDiagnosis": {"code": "G243"}, "claimLines": [line],
    }


def visit_drug_procedures_claim(bypass):
    def line(number, revenue, hcpcs, units, charge, modifiers=(), edit_bypass=()):
        return {
            "claimLineId": number, "lineNumber": number, "revenueCode": revenue, "hcpcs": hcpcs,
            "dateOfService": "2026-07-30", "units": units, "chargeAmount": charge,
            "modifiers": list(modifiers), "editBypass": list(edit_bypass),
        }

    return {
        "claimId": "990105", "billType": "131", "providerNumber": "290019", "patientAge": 56,
        "patientSex": "U", "principalDiagnosis": {"code": "G243"},
        "secondaryDiagnoses": [{"code": "G248"}],
        "claimLines": [
            line(1, "0510", "G0463", 1, "150.00", ["25"]),
            line(2, "0636", "J0586", 120, "3000.00", edit_bypass=bypass),
            line(3, "0761", "64616", 1, "2500.00", ["50"]),
            line(4, "0761", "64646", 1, "2000.00"),
            line(5, "0922", "95874", 2, "400.00"),
        ],
    }


class EditBypassModelTests(unittest.TestCase):
    def test_bypass_is_normalized_to_three_digits_without_duplicates(self):
        request = IoceClaimRequest.model_validate(drug_claim(editBypass=["12", "012", " 62 "]))
        self.assertEqual(request.claimLines[0].editBypass, ["012", "062"])

    def test_bypass_defaults_to_empty(self):
        self.assertEqual(IoceClaimRequest.model_validate(drug_claim()).claimLines[0].editBypass, [])

    def test_invalid_bypass_is_rejected(self):
        for value in ["abc", "0", "000", "1234", "", "1.2", "-1"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                IoceClaimRequest.model_validate(drug_claim(editBypass=[value]))

    def test_more_than_four_bypasses_are_rejected(self):
        with self.assertRaises(ValidationError):
            IoceClaimRequest.model_validate(drug_claim(editBypass=["1", "2", "3", "4", "5"]))

    def test_normalize_edit(self):
        self.assertEqual(normalize_edit("12"), "012")
        self.assertEqual(normalize_edit("012"), "012")
        self.assertEqual(normalize_edit("7"), "007")


class EditBypassAllowlistTests(unittest.TestCase):
    def test_parse_allowlist(self):
        self.assertEqual(parse_allowlist(None), frozenset())
        self.assertEqual(parse_allowlist("  "), frozenset())
        self.assertEqual(parse_allowlist("12"), frozenset({"012"}))
        self.assertEqual(parse_allowlist("12, 062;92"), frozenset({"012", "062", "092"}))
        with self.assertRaises(ValueError):
            parse_allowlist("12,abc")

    def test_disallowed_bypasses(self):
        request = IoceClaimRequest.model_validate(drug_claim(editBypass=["12", "62"]))
        self.assertEqual(disallowed_bypasses(request, frozenset({"012", "062"})), [])
        self.assertEqual(
            disallowed_bypasses(request, frozenset({"012"})),
            [{"claimLineId": "2", "lineNumber": 1, "edits": ["062"]}],
        )
        self.assertEqual(
            disallowed_bypasses(request, frozenset()),
            [{"claimLineId": "2", "lineNumber": 1, "edits": ["012", "062"]}],
        )


class EditBypassMapperTests(unittest.TestCase):
    def test_bypass_reaches_the_line_override(self):
        mapped = to_myelin_claim(IoceClaimRequest.model_validate(drug_claim(editBypass=["12"])))
        self.assertEqual(mapped.lines[0].override.edit_bypass_list, ["012"])

    def test_no_bypass_leaves_no_override(self):
        mapped = to_myelin_claim(IoceClaimRequest.model_validate(drug_claim()))
        self.assertIsNone(mapped.lines[0].override)


class EditBypassEndpointTests(unittest.TestCase):
    def test_disallowed_bypass_is_rejected_before_processing(self):
        import bridge.main as main

        # No engine is loaded: the allowlist check must come before any engine use.
        unready = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        original = main.EDIT_BYPASS_ALLOWED
        try:
            main.EDIT_BYPASS_ALLOWED = frozenset({"012"})
            payload = IoceClaimRequest.model_validate(drug_claim(editBypass=["62"]))
            for endpoint in (main.process_ioce, main.process_opps):
                with self.subTest(endpoint=endpoint.__name__), self.assertRaises(HTTPException) as raised:
                    endpoint(payload, unready)
                self.assertEqual(raised.exception.status_code, 422)
                self.assertEqual(raised.exception.detail["allowedEdits"], ["012"])
                self.assertEqual(raised.exception.detail["lines"], [{"claimLineId": "2", "lineNumber": 1, "edits": ["062"]}])

            main.EDIT_BYPASS_ALLOWED = frozenset()
            with self.assertRaises(HTTPException) as off:
                main.process_opps(IoceClaimRequest.model_validate(drug_claim(editBypass=["12"])), unready)
            self.assertEqual(off.exception.status_code, 422)
            self.assertEqual(off.exception.detail["allowedEdits"], [])
        finally:
            main.EDIT_BYPASS_ALLOWED = original

    def test_requests_without_a_bypass_pass_the_check(self):
        import bridge.main as main

        original = main.EDIT_BYPASS_ALLOWED
        try:
            main.EDIT_BYPASS_ALLOWED = frozenset()
            main.enforce_edit_bypass(IoceClaimRequest.model_validate(drug_claim()))
        finally:
            main.EDIT_BYPASS_ALLOWED = original

    def test_echo_lists_the_bypass_per_line(self):
        import bridge.main as main

        payload = IoceClaimRequest.model_validate(visit_drug_procedures_claim(["12"]))
        output = SimpleNamespace(line_item_list=[None] * 5)
        lines = main.map_ioce_lines(payload, output)
        self.assertEqual([line["editBypass"] for line in lines], [[], ["012"], [], [], []])


@unittest.skipUnless(list((ROOT / "jars").glob("ioce-standalone-*.jar")), "The CMS I/OCE jar is not available.")
class EditBypassEditorTests(unittest.TestCase):
    """Runs the unmodified CMS editor: bypassing edit 012 clears the suspension."""

    @classmethod
    def setUpClass(cls):
        import jpype

        if not jpype.isJVMStarted():
            jpype.startJVM(classpath=[str(ROOT / "jars" / "*")])

        try:
            from myelin.ioce.ioce_client import IoceClient

            cls.client = IoceClient()
        except Exception as error:  # A JVM started elsewhere without the CMS jars.
            raise unittest.SkipTest(f"The CMS I/OCE could not be loaded: {error}") from error

    def process(self, bypass):
        request = IoceClaimRequest.model_validate(visit_drug_procedures_claim(bypass))
        return self.client.process(to_myelin_claim(request), include_descriptions=False).model_dump()

    def test_bypass_clears_the_edit_012_suspension(self):
        held = self.process([])
        self.assertEqual(held["claim_suspension_disposition"], "1")
        self.assertEqual([edit["edit"] for edit in held["claim_suspension_edit_list"]], ["012"])
        self.assertEqual([edit["edit"] for edit in held["line_item_list"][1]["hcpcs_edit_list"]], ["012"])

        clean = self.process(["12"])
        self.assertEqual(clean["claim_disposition"], "0")
        self.assertEqual(clean["claim_suspension_disposition"], "0")
        self.assertEqual(clean["claim_suspension_edit_list"], [])
        self.assertEqual(clean["line_item_list"][1]["hcpcs_edit_list"], [])
        self.assertEqual(clean["line_item_list"][1]["status_indicator"], "K")


if __name__ == "__main__":
    unittest.main()
