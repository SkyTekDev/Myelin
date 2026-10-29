"""Validate submitted CMS HH geography; never infer a county/CBSA from ZIP.

The pinned CMS wage-index release lists 41 counties affected by transition
codes, including counties whose transition expired for 2026. It does not
provide a complete FIPS-to-CBSA crosswalk. Other submitted county/area pairs
remain explicit caller inputs and are identified as unverified in evidence.
"""

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import io
from pathlib import Path
import re

from bridge.hha_provider import HhaInputError


TRANSITION_SOURCE_URL = "https://www.cms.gov/files/zip/cy-2026-final-hh-pps-wage-index.zip"
TRANSITION_SOURCE_SHA256 = "c8734767077549704faf9627a8c92b34cb8ce13626379500239e07defc2aeca3"
TRANSITION_CSV_SHA256 = "0a5c37ae6d44bad3550671925c34a8874498b6192812d4b7f1fdefe5f6a89bda"
TRANSITION_CSV = Path(__file__).parent / "references" / "hha-2026-transition-counties.csv"

# Rural wage-index codes use CMS state codes, not Census/FIPS state codes.
# Validate their state NAME from the pinned CMS wage table against FIPS below.
# For example Texas is FIPS 48, CMS rural code 99945 (or submitted code 45).
FIPS_STATE_NAMES = {
    "01": "ALABAMA", "02": "ALASKA", "04": "ARIZONA", "05": "ARKANSAS",
    "06": "CALIFORNIA", "08": "COLORADO", "09": "CONNECTICUT", "10": "DELAWARE",
    "11": "DISTRICT OF COLUMBIA", "12": "FLORIDA", "13": "GEORGIA", "15": "HAWAII",
    "16": "IDAHO", "17": "ILLINOIS", "18": "INDIANA", "19": "IOWA", "20": "KANSAS",
    "21": "KENTUCKY", "22": "LOUISIANA", "23": "MAINE", "24": "MARYLAND",
    "25": "MASSACHUSETTS", "26": "MICHIGAN", "27": "MINNESOTA", "28": "MISSISSIPPI",
    "29": "MISSOURI", "30": "MONTANA", "31": "NEBRASKA", "32": "NEVADA",
    "33": "NEW HAMPSHIRE", "34": "NEW JERSEY", "35": "NEW MEXICO", "36": "NEW YORK",
    "37": "NORTH CAROLINA", "38": "NORTH DAKOTA", "39": "OHIO", "40": "OKLAHOMA",
    "41": "OREGON", "42": "PENNSYLVANIA", "44": "RHODE ISLAND",
    "45": "SOUTH CAROLINA", "46": "SOUTH DAKOTA", "47": "TENNESSEE", "48": "TEXAS",
    "49": "UTAH", "50": "VERMONT", "51": "VIRGINIA", "53": "WASHINGTON",
    "54": "WEST VIRGINIA", "55": "WISCONSIN", "56": "WYOMING",
    "60": "AMERICAN SAMOA", "66": "GUAM", "69": "NORTHERN MARIANA ISLANDS",
    "72": "PUERTO RICO", "78": "VIRGIN ISLANDS",
}


@lru_cache(maxsize=1)
def _transition_counties() -> dict[str, dict]:
    # Git can change CRLF/LF between Windows and Linux checkouts. Pin normalized
    # UTF-8 CSV content so promotion does not depend on checkout line endings.
    content = TRANSITION_CSV.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(content).hexdigest() != TRANSITION_CSV_SHA256:
        raise RuntimeError("HH county transition reference checksum mismatch.")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    mapped = {row["serviceCountyFips"]: row for row in rows}
    if len(rows) != 41 or len(mapped) != 41:
        raise RuntimeError("HH county transition reference is incomplete or ambiguous.")
    return mapped


def _wage_index(row: dict) -> tuple[Decimal, str]:
    try:
        wage = Decimal(str(row["geographicWageIndex"]))
        raw_date = row["effectiveDate"]
        if type(raw_date) is date:
            effective = raw_date
        else:
            effective = datetime.strptime(str(raw_date), "%Y%m%d").date()
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise HhaInputError("HHA_WAGE_REFERENCE_INVALID", "The selected HH wage-index reference is invalid.") from None
    if not wage.is_finite() or wage <= 0 or effective != date(2026, 1, 1):
        raise HhaInputError("HHA_WAGE_REFERENCE_INVALID", "The selected HH wage-index reference is invalid.")
    return wage, effective.isoformat()


def resolve_geography(county_fips: str, submitted_cbsa: str, wage_rows: dict[str, dict]) -> dict:
    """Return evidence for explicit CMS value-code 85 county and 61 area inputs.

    Only listed transition counties have an independently verified county-to-
    area mapping. Rural state matching is additional validation, not proof
    that an arbitrary county belongs to a rural area.
    """
    if (not isinstance(county_fips, str) or not re.fullmatch(r"[0-9]{5}", county_fips)
            or county_fips[:2] not in FIPS_STATE_NAMES or county_fips[2:] == "000"):
        raise HhaInputError("HHA_COUNTY_INVALID", "Supply a five-digit service-county FIPS code with a valid state and nonzero county code.")
    if not isinstance(submitted_cbsa, str) or not re.fullmatch(r"(?:[0-9]{2}|[0-9]{5})", submitted_cbsa):
        raise HhaInputError("HHA_CBSA_INVALID", "Supply the CMS wage-index area code, preserving leading zeros.")
    cbsa = "999" + submitted_cbsa if len(submitted_cbsa) == 2 else submitted_cbsa
    row = wage_rows.get(cbsa)
    if not isinstance(row, dict) or str(row.get("cbsa")) != cbsa:
        raise HhaInputError("HHA_WAGE_REFERENCE_MISSING", "No validated CY 2026 wage-index row exists for the submitted area.")
    wage, effective = _wage_index(row)
    transition = _transition_counties().get(county_fips)
    if transition is not None:
        if cbsa != transition["cmsWageIndexCode"]:
            raise HhaInputError("HHA_COUNTY_CBSA_MISMATCH", "The service county requires a different CY 2026 CMS wage-index code.")
        if wage != Decimal(transition["wageIndex"]):
            raise HhaInputError("HHA_WAGE_REFERENCE_MISMATCH", "The selected wage index disagrees with the official CY 2026 county transition reference.")
    elif cbsa.startswith("50"):
        raise HhaInputError("HHA_TRANSITION_COUNTY_MISMATCH", "A transition wage-index code requires its verified service county.")
    if cbsa.startswith("999"):
        name = row.get("name")
        if not isinstance(name, str) or name.strip().upper() != FIPS_STATE_NAMES[county_fips[:2]]:
            raise HhaInputError("HHA_RURAL_STATE_MISMATCH", "The submitted rural CMS area and service-county FIPS refer to different states.")
    result = {
        "submittedCbsaCode": submitted_cbsa,
        "cbsaCode": cbsa,
        "serviceCountyFips": county_fips,
        "wageIndex": wage,
        "wageIndexEffectiveDate": effective,
        "wageIndexReference": "cbsa-wage-index-2026.csv",
        "geographyBasis": "SubmittedCmsValueCodes",
        "countyMappingVerified": transition is not None,
        "limitations": [] if transition is not None else [
            "County existence and county-to-CBSA/rural-area mapping were not independently verified; submitted CMS value codes are used."
        ],
    }
    if transition is not None:
        result.update({
            "transitionSourceUrl": TRANSITION_SOURCE_URL,
            "sourceSha256": TRANSITION_SOURCE_SHA256,
            "csvSha256": TRANSITION_CSV_SHA256,
        })
    return result


def validate_geography_references(wage_rows: dict[str, dict]) -> None:
    """Fail startup unless pinned transitions reconcile with the HHA JAR table."""
    # Each processor startup revalidates bytes even if a previous processor in
    # this interpreter initialized this small immutable lookup successfully.
    _transition_counties.cache_clear()
    for county, row in _transition_counties().items():
        resolve_geography(county, row["cmsWageIndexCode"], wage_rows)
