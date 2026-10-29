"""HH area validation distinguishes CMS state codes, FIPS and transition dates."""

import csv
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.hha_geography import resolve_geography, validate_geography_references, TRANSITION_CSV
from bridge.hha_provider import HhaInputError


def wage(code, amount, name="AREA"):
    return {"cbsa": code, "effectiveDate": "20260101", "geographicWageIndex": amount, "name": name}


class HhaGeographyTests(unittest.TestCase):
    def setUp(self):
        self.rows = {
            "99901": wage("99901", "0.6367", "ALABAMA"),
            "99905": wage("99905", "1.2775", "CALIFORNIA"),
            "99914": wage("99914", "0.8743", "ILLINOIS"),
            "99945": wage("99945", "0.8400", "TEXAS"),
            "50001": wage("50001", "0.6800", "ALABAMA"),
            "50007": wage("50007", "0.8743", "ILLINOIS"),
            "10180": wage("10180", "0.9287", "Abilene, TX"),
        }

    def failure(self, county, cbsa, code):
        with self.assertRaises(HhaInputError) as caught:
            resolve_geography(county, cbsa, self.rows)
        self.assertEqual(caught.exception.code, code)

    def test_explicit_urban_pair_retains_unverified_crosswalk_limitation(self):
        result = resolve_geography("48059", "10180", self.rows)
        self.assertEqual(result["wageIndex"], Decimal("0.9287"))
        self.assertFalse(result["countyMappingVerified"])
        self.assertTrue(result["limitations"])
        self.assertEqual(result["geographyBasis"], "SubmittedCmsValueCodes")

    def test_rural_code_uses_cms_not_fips_state_codes(self):
        for county, code, expected in [("48001", "45", "99945"), ("06001", "99905", "99905")]:
            with self.subTest(county=county):
                result = resolve_geography(county, code, self.rows)
                self.assertEqual(result["cbsaCode"], expected)
                self.assertFalse(result["countyMappingVerified"])
        self.failure("48001", "01", "HHA_RURAL_STATE_MISMATCH")

    def test_known_transition_county_requires_2026_code_and_retains_source(self):
        result = resolve_geography("01129", "50001", self.rows)
        self.assertTrue(result["countyMappingVerified"])
        self.assertFalse(result["limitations"])
        self.assertEqual(len(result["sourceSha256"]), 64)
        self.assertEqual(len(result["csvSha256"]), 64)
        self.failure("01129", "01", "HHA_COUNTY_CBSA_MISMATCH")
        self.failure("01001", "50001", "HHA_TRANSITION_COUNTY_MISMATCH")

    def test_expired_transition_cannot_keep_old_code(self):
        result = resolve_geography("17183", "14", self.rows)
        self.assertEqual(result["cbsaCode"], "99914")
        self.assertTrue(result["countyMappingVerified"])
        self.failure("17183", "50007", "HHA_COUNTY_CBSA_MISMATCH")

    def test_every_listed_county_agrees_with_its_official_code(self):
        with Path(TRANSITION_CSV).open(encoding="utf-8", newline="") as stream:
            references = list(csv.DictReader(stream))
        self.assertEqual(len(references), 41)
        for row in references:
            with self.subTest(county=row["serviceCountyFips"]):
                code = row["cmsWageIndexCode"]
                name = "AREA"
                if code.startswith("999"):
                    from bridge.hha_geography import FIPS_STATE_NAMES
                    name = FIPS_STATE_NAMES[row["serviceCountyFips"][:2]]
                result = resolve_geography(row["serviceCountyFips"], code, {code: wage(code, row["wageIndex"], name)})
                self.assertTrue(result["countyMappingVerified"])

    def test_invalid_or_missing_inputs_and_invalid_references(self):
        for county in ("1001", "01000", "99001", "01A01", 1001, True):
            with self.subTest(county=county):
                self.failure(county, "01", "HHA_COUNTY_INVALID")
        for area in ("1", "0001", "ABC12", 1, None):
            with self.subTest(area=area):
                self.failure("01001", area, "HHA_CBSA_INVALID")
        self.failure("01001", "99999", "HHA_WAGE_REFERENCE_MISSING")
        for amount in ("NaN", "Infinity", "0", "-1", None):
            with self.subTest(amount=amount):
                self.rows["99901"]["geographicWageIndex"] = amount
                self.failure("01001", "01", "HHA_WAGE_REFERENCE_INVALID")

    def test_mismatched_or_stale_source_rates_are_rejected(self):
        self.rows["50001"]["geographicWageIndex"] = "0.6367"
        self.failure("01129", "50001", "HHA_WAGE_REFERENCE_MISMATCH")
        self.rows["99901"]["effectiveDate"] = "20250101"
        self.failure("01001", "01", "HHA_WAGE_REFERENCE_INVALID")

    def test_startup_checks_all_transition_rates_and_rejects_missing_rows(self):
        from bridge.hha_geography import FIPS_STATE_NAMES
        with Path(TRANSITION_CSV).open(encoding="utf-8", newline="") as stream:
            references = list(csv.DictReader(stream))
        rows = {}
        for reference in references:
            code = reference["cmsWageIndexCode"]
            name = FIPS_STATE_NAMES[reference["serviceCountyFips"][:2]] if code.startswith("999") else "AREA"
            rows[code] = wage(code, reference["wageIndex"], name)
        validate_geography_references(rows)
        rows["50001"]["geographicWageIndex"] = "0.6367"
        with self.assertRaises(HhaInputError) as caught:
            validate_geography_references(rows)
        self.assertEqual(caught.exception.code, "HHA_WAGE_REFERENCE_MISMATCH")
        del rows["50001"]
        with self.assertRaises(HhaInputError) as caught:
            validate_geography_references(rows)
        self.assertEqual(caught.exception.code, "HHA_WAGE_REFERENCE_MISSING")

    def test_startup_revalidates_csv_checksum_after_prior_success(self):
        from bridge.hha_geography import _transition_counties
        resolve_geography("01129", "50001", self.rows)
        with tempfile.TemporaryDirectory() as temporary:
            bad_file = Path(temporary) / "transitions.csv"
            bad_file.write_text("serviceCountyFips,cmsWageIndexCode,wageIndex,countyName\n01129,99901,0.6367,WASHINGTON\n", encoding="utf-8")
            try:
                with patch("bridge.hha_geography.TRANSITION_CSV", bad_file):
                    with self.assertRaisesRegex(RuntimeError, "checksum"):
                        validate_geography_references(self.rows)
            finally:
                _transition_counties.cache_clear()

    def test_reference_checksum_survives_windows_checkout_line_endings(self):
        from bridge.hha_geography import _transition_counties
        original = Path(TRANSITION_CSV).read_bytes().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory() as temporary:
            crlf_file = Path(temporary) / "transitions.csv"
            crlf_file.write_bytes(original.replace(b"\n", b"\r\n"))
            try:
                with patch("bridge.hha_geography.TRANSITION_CSV", crlf_file):
                    _transition_counties.cache_clear()
                    result = resolve_geography("01129", "50001", self.rows)
                    self.assertTrue(result["countyMappingVerified"])
            finally:
                _transition_counties.cache_clear()


if __name__ == "__main__":
    unittest.main()
