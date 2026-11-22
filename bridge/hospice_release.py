"""CMS Hospice Pricer 2.7.1 (calculation versions 2026.0 and 2027.0), reconciled for FY 2026-2027 dates of service.

The jar carries FY 2020-2027, but only these fiscal years were reconciled: its FY 2027 rate constants
match CR 14557 (Transmittal 13976, September 30, 2026) Tables 1 and 2, its FY 2027 wage index table
holds exactly the 50xxx transition codes of Table 3, and its FY 2020-2026 wage index tables and FY 2026
rate constants are identical to the FY 2026 release (2.5.1). Dates outside the window are refused
rather than priced with another year's rates.
"""
from datetime import date

PRICER_RELEASE = "2.7.1"
JAR_FILENAME = "hospice-pricer-application-2.7.1.jar"
JAR_SHA256 = "5ad0787e703c6155273f977c8376fb4acb4147429ccbe13425fb436d9b04598a"
# CMS fiscal years: FY 2026 runs October 1, 2025 through September 30, 2026.
SUPPORTED_FISCAL_YEARS = (2026, 2027)
CALCULATION_VERSIONS = {2026: "2026.0", 2027: "2027.0"}
FIRST_SERVICE_DATE = date(2025, 10, 1)
LAST_SERVICE_DATE = date(2027, 9, 30)
UNSUPPORTED_DATES_REASON = (
    "Hospice pricing is verified for dates of service October 1, 2025 through September 30, 2027 "
    "(FY 2026-2027). Other dates require the CMS Hospice Pricer release for their fiscal year."
)

ROUTINE_HOME_CARE = "0651"
CONTINUOUS_HOME_CARE = "0652"
INPATIENT_RESPITE_CARE = "0655"
GENERAL_INPATIENT_CARE = "0656"
LEVELS_OF_CARE = {
    ROUTINE_HOME_CARE: "RoutineHomeCare",
    CONTINUOUS_HOME_CARE: "ContinuousHomeCare",
    INPATIENT_RESPITE_CARE: "InpatientRespiteCare",
    GENERAL_INPATIENT_CARE: "GeneralInpatientCare",
}
# The jar returns four amounts in this fixed order (the FISS record's REV1-REV4 slots). It labels
# them with the submitted groups' revenue codes by position, so the labels cannot be trusted.
PAYMENT_SLOTS = (ROUTINE_HOME_CARE, CONTINUOUS_HOME_CARE, INPATIENT_RESPITE_CARE, GENERAL_INPATIENT_CARE)
# Day-based levels: units are days starting on the line's date of service.
DAY_LEVELS = frozenset({ROUTINE_HOME_CARE, INPATIENT_RESPITE_CARE, GENERAL_INPATIENT_CARE})
# Return codes of a priced call. The one call that carries the end-of-life add-on returns 74 (low) or 77 (high
# routine home care rate with the add-on) instead of 73 or 75.
PRICED_RETURN_CODES = frozenset({"00", "73", "75"})
END_OF_LIFE_RETURN_CODES = frozenset({"74", "77"})
INVALID_UNITS_RETURN_CODE = "10"
INVALID_CBSA_RETURN_CODE = "30"

# Manual section 30.2.2 (CR 9201): the service intensity add-on covers registered nurse and social worker visits on
# routine home care days in the last 7 days of life, at most 16 fifteen-minute units (4 hours) a day. FISS sends the
# Pricer one unit total per day, day 1 being the date of death.
END_OF_LIFE_DAYS = 7
END_OF_LIFE_MAXIMUM_DAILY_UNITS = 16

# Claims Processing Manual chapter 11: CHC needs 8 hours (32 fifteen-minute units) in a day that begins
# and ends at midnight, or the day is paid as routine home care; a respite period lasts at most 5 days.
CHC_MINIMUM_UNITS = 32
CHC_MAXIMUM_UNITS = 96
RESPITE_MAXIMUM_DAYS = 5
HIGH_RATE_DAYS = 60

HOSPICE_PROVIDER_TYPE = "35"


def fiscal_year(day: date) -> int:
    return day.year + 1 if day.month >= 10 else day.year


def fiscal_year_start(year: int) -> date:
    return date(year - 1, 10, 1)


def release_info() -> dict:
    return {
        "pricerRelease": PRICER_RELEASE,
        "jarFilename": JAR_FILENAME,
        "jarSha256": JAR_SHA256,
        "calculationVersions": {str(year): version for year, version in CALCULATION_VERSIONS.items()},
        "supportedFiscalYears": list(SUPPORTED_FISCAL_YEARS),
        "firstServiceDate": FIRST_SERVICE_DATE.isoformat(),
        "lastServiceDate": LAST_SERVICE_DATE.isoformat(),
        # Bridge 0.9.1+: requests may carry the seven daily end-of-life add-on unit totals.
        "endOfLifeAddOn": True,
    }
