"""Pinned CMS HHA executable and deliberately limited first-pass support."""

from datetime import date

PRICER_RELEASE = "2.6.0"
JAR_FILENAME = "hha-pricer-application-2.6.0.jar"
JAR_SHA256 = "7a70ae359434f4919d3aa171d0a2e79c98079d8d01b27d328171f23517f2d6f4"
CALCULATION_VERSIONS = {2026: "2026.0"}
FIRST_SERVICE_DATE = date(2026, 1, 1)
LAST_SERVICE_DATE = date(2026, 12, 31)
DISCIPLINE_REVENUES = {
    "PT": "0420", "OT": "0430", "SLP": "0440",
    "SN": "0550", "MSW": "0560", "HHA": "0570",
}
LUPA_INCREMENTAL_FACTORS = {
    "SN": "0.7200", "PT": "0.6225", "OT": "0.7238", "SLP": "0.6696",
}
SOURCE_URL = "https://www.cms.gov/files/zip/hha-pricer-2026-0-v2-6-0-executable-jar.zip"


def release_info() -> dict:
    return {
        "pricerRelease": PRICER_RELEASE,
        "jarSha256": JAR_SHA256,
        "calculationYears": list(CALCULATION_VERSIONS),
        "calculationVersions": CALCULATION_VERSIONS,
        "firstServiceDate": FIRST_SERVICE_DATE.isoformat(),
        "lastServiceDate": LAST_SERVICE_DATE.isoformat(),
        "supportedBillTypes": ["329", "0329"],
        "supportedPeriodType": "Full30Day",
        "supportedPeriodTypes": ["Full30Day", "Partial30Day"],
        "grouping": "SuppliedHippsOnly",
        "supportedPayment": "OrdinaryPeriodLupaPartialPeriodLateNoaAndOutlier",
    }
