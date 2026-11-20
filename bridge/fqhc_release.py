"""CMS FQHC Pricer 2.10.0 (calculation version 2026.1), reconciled for CY 2025-2026 dates of service.

The jar's tables start in October 2014, but only the years below were checked
against CMS's published base rates and this bridge's I/OCE. Dates outside the
window are refused rather than priced with another year's rates.
"""
from datetime import date

PRICER_RELEASE = "2.10.0"
JAR_FILENAME = "fqhc-pricer-application-2.10.0.jar"
JAR_SHA256 = "075f229f8bfbbc9576829981e2d4e069ea2181305e2a189b3c01304851ac258e"
CALCULATION_VERSION = "2026.1"
SUPPORTED_YEARS = (2025, 2026)
FIRST_SERVICE_DATE = date(2025, 1, 1)
LAST_SERVICE_DATE = date(2026, 12, 31)
UNSUPPORTED_DATES_REASON = (
    "FQHC pricing is verified for dates of service January 1, 2025 through "
    "December 31, 2026. Later dates require CMS's 2027 FQHC Pricer."
)

# The October 2026 I/OCE (27.3) implements CR 14468: distant-site telehealth
# lines with modifier 93 or 95 on 77X claims receive payment indicator 2 and
# bypass edits 088/089. Earlier I/OCE versions return such claims.
TELEHEALTH_IOCE_MINIMUM = (27, 3)
TELEHEALTH_EFFECTIVE_DATE = date(2026, 10, 1)
TELEHEALTH_MODIFIERS = frozenset({"93", "95"})
MENTAL_HEALTH_REVENUE_CODE = "0900"


def release_info() -> dict:
    return {
        "pricerRelease": PRICER_RELEASE,
        "jarFilename": JAR_FILENAME,
        "jarSha256": JAR_SHA256,
        "calculationVersion": CALCULATION_VERSION,
        "supportedYears": list(SUPPORTED_YEARS),
        "firstServiceDate": FIRST_SERVICE_DATE.isoformat(),
        "lastServiceDate": LAST_SERVICE_DATE.isoformat(),
    }
