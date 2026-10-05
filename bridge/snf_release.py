"""CMS 2.7.1 reconciled for FY 2023-2027; provider inputs are checked separately."""
from datetime import date

PRICER_RELEASE = "2.7.1"
JAR_FILENAME = "snf-pricer-application-2.7.1.jar"
JAR_SHA256 = "9ea9f4763f28bff5902a9266d4e5f7971745a3309b36d1d5d3b7f9a08549b2c5"
CALCULATION_VERSIONS = {2023: "2023.0", 2024: "2024.1", 2025: "2025.0", 2026: "2026.0", 2027: "2027.0"}
FIRST_SERVICE_DATE = date(2023, 1, 1)
LAST_SERVICE_DATE = date(2027, 9, 30)
UNSUPPORTED_DATES_REASON = (
    "SNF pricing is verified for January 1, 2023 through September 30, 2027. "
    "Later dates require a verified CMS pricer release and provider data."
)
