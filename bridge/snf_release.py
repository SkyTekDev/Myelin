"""Reconciled CMS release policy. FY 2027 is not enabled without release evidence."""
from datetime import date

PRICER_RELEASE = "2.5.1"
JAR_FILENAME = "snf-pricer-application-2.5.1.jar"
JAR_SHA256 = "c19bbc339fff3e44c65ff09811f6c85921846342ddf8b4100520c95382864122"
CALCULATION_VERSIONS = {2023: "2023.0", 2024: "2024.1", 2025: "2025.0", 2026: "2026.0"}
FIRST_SERVICE_DATE = date(2023, 1, 1)
LAST_SERVICE_DATE = date(2026, 9, 30)
UNSUPPORTED_DATES_REASON = (
    "SNF pricing is verified for January 1, 2023 through September 30, 2026. "
    "FY 2027 requires a verified CMS pricer release and provider data; later dates cannot use FY 2026 rates."
)
