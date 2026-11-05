"""Approved CMS HHA payment-year releases selected by statement Through date."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class HhaRelease:
    payment_year: int
    earliest_statement_from: date
    pricer_release: str
    calculation_version: str
    jar_filename: str
    jar_sha256: str
    source_url: str
    full_period_rate: Decimal
    reduced_period_rate: Decimal
    fixed_dollar_loss_ratio: Decimal
    part_b_deductible: Decimal

    @property
    def effective_from(self) -> date:
        return date(self.payment_year, 1, 1)

    @property
    def effective_through(self) -> date:
        return date(self.payment_year, 12, 31)

    @property
    def hipps_reference(self) -> str:
        return f"hrg-{self.payment_year}.csv"

    @property
    def wage_reference(self) -> str:
        return f"cbsa-wage-index-{self.payment_year}.csv"

    @property
    def visit_reference(self) -> str:
        return f"rev-{self.payment_year}.csv"

    def covers_statement(self, from_date: date, through_date: date) -> bool:
        days = (through_date - from_date).days
        return (through_date.year == self.payment_year and from_date >= self.earliest_statement_from
                and 0 <= days < 30)

    def period_rate(self, qrp: str) -> Decimal:
        return self.full_period_rate if qrp == "0" else self.reduced_period_rate


CY_2026 = HhaRelease(
    payment_year=2026,
    earliest_statement_from=date(2025, 12, 3),
    pricer_release="2.6.0",
    calculation_version="2026.0",
    jar_filename="hha-pricer-application-2.6.0.jar",
    jar_sha256="7a70ae359434f4919d3aa171d0a2e79c98079d8d01b27d328171f23517f2d6f4",
    source_url="https://www.cms.gov/files/zip/hha-pricer-2026-0-v2-6-0-executable-jar.zip",
    full_period_rate=Decimal("2038.22"),
    reduced_period_rate=Decimal("1998.41"),
    fixed_dollar_loss_ratio=Decimal(".37"),
    part_b_deductible=Decimal("283"),
)
CY_2025 = replace(
    CY_2026,
    payment_year=2025,
    earliest_statement_from=date(2025, 1, 1),
    calculation_version="2025.1",
    full_period_rate=Decimal("2057.35"),
    reduced_period_rate=Decimal("2017.28"),
    fixed_dollar_loss_ratio=Decimal(".35"),
    part_b_deductible=Decimal("257"),
)
APPROVED_RELEASES: Mapping[int, HhaRelease] = MappingProxyType({
    release.payment_year: release for release in (CY_2025, CY_2026)
})
DISCIPLINE_REVENUES = {
    "PT": "0420", "OT": "0430", "SLP": "0440",
    "SN": "0550", "MSW": "0560", "HHA": "0570",
}
LUPA_INCREMENTAL_FACTORS = {
    "SN": "0.7200", "PT": "0.6225", "OT": "0.7238", "SLP": "0.6696",
}


def release_for_through(through_date: date, releases: Mapping[int, HhaRelease] = APPROVED_RELEASES) -> HhaRelease | None:
    return releases.get(through_date.year)


def require_statement_release(from_date: date, through_date: date) -> HhaRelease:
    release = release_for_through(through_date)
    if release is None:
        raise ValueError("No approved HH pricing release for the statement Through date.")
    if not release.covers_statement(from_date, through_date):
        raise ValueError("The complete statement period must be covered by the approved HH release.")
    return release


def release_info() -> dict:
    releases = sorted(APPROVED_RELEASES.values(), key=lambda release: release.payment_year)
    current = releases[-1]
    return {
        "pricerRelease": current.pricer_release,
        "jarSha256": current.jar_sha256,
        "calculationYears": [release.payment_year for release in releases],
        "calculationVersions": {release.payment_year: release.calculation_version for release in releases},
        "firstServiceDate": min(release.earliest_statement_from for release in releases).isoformat(),
        "lastServiceDate": current.effective_through.isoformat(),
        "supportedBillTypes": ["329", "0329", "327", "0327"],
        "supportedPeriodType": "Full30Day",
        "supportedPeriodTypes": ["Full30Day", "Partial30Day", "Shortened30Day"],
        "grouping": "SuppliedHippsOnly",
        "supportedPayment": "OrdinaryPeriodLupaPartialPeriodShortenedPeriodLateNoaAndOutlier",
    }
