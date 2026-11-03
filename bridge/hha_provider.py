"""Read dated HH provider facts without the generic IPSF model's defaults."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from myelin.pricers.ipsf import IPSF
from bridge.hha_provider_annual import AnnualProviderReferenceError, annual_payment_evidence


class HhaInputError(ValueError):
    """A supported input/reference failure safe to return to the caller."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _reference_date(value, field: str) -> date:
    if not isinstance(value, int) or isinstance(value, bool) or len(str(value)) != 8:
        raise HhaInputError(
            "HHA_PROVIDER_REFERENCE_INVALID", f"Provider {field} is not a valid reference date."
        )
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        raise HhaInputError(
            "HHA_PROVIDER_REFERENCE_INVALID", f"Provider {field} is not a valid reference date."
        ) from None


def _vbp_factor(value) -> Decimal:
    try:
        factor = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HhaInputError(
            "HHA_PROVIDER_VBP_INVALID", "The HH provider payment adjustment factor is missing or invalid."
        ) from None
    # The expanded HHVBP Model caps the supported payment adjustment at +/-5 percent.
    if not factor.is_finite() or not Decimal("0.95") <= factor <= Decimal("1.05"):
        raise HhaInputError(
            "HHA_PROVIDER_VBP_INVALID", "The HH provider payment adjustment factor is outside the supported range."
        )
    return factor


def _active_termination(row, end: date) -> date | None:
    if (row.provider_type or "").strip() != "36":
        raise HhaInputError(
            "HHA_PROVIDER_TYPE_MISMATCH", "The effective IPSF provider record is not a home health agency."
        )
    # NULL and these sentinels are source representations of no termination.
    if row.termination_date in (None, 0, 19000101, 99991231):
        return None
    termination = _reference_date(row.termination_date, "termination date")
    if termination <= end:
        raise HhaInputError(
            "HHA_PROVIDER_TERMINATED", "The HH provider is terminated on or before the period's through date."
        )
    return termination


def _crossover_coverage(rows, effective: date, start: date, end: date) -> dict:
    if effective != date(end.year, 1, 1) or len(rows) < 2:
        raise HhaInputError("HHA_PROVIDER_CROSSOVER_UNVERIFIED",
            "Cross-year HH pricing requires a January 1 IPSF payment update and a prior record covering the statement start.")
    prior = rows[1]
    prior_date = _reference_date(prior.effective_date, "prior effective date")
    if prior_date > start:
        raise HhaInputError("HHA_PROVIDER_CHANGED_WITHIN_PERIOD",
            "An additional IPSF effective date falls within the cross-year statement.")
    if len(rows) > 2 and rows[2].effective_date == prior.effective_date:
        raise HhaInputError("HHA_PROVIDER_REFERENCE_AMBIGUOUS",
            "More than one IPSF provider record exists on the prior effective date.")
    termination = _active_termination(prior, end)
    return dict(schemaVersion="HHACrossoverProviderEvidence/1", providerCcn=prior.provider_ccn,
                providerRowId=prior.id, providerType="36", effectiveDate=prior_date.isoformat(),
                terminationDate=termination.isoformat() if termination else None)


def provider_for_period(engine, ccn: str, start: date, end: date) -> dict:
    """Resolve IPSF payment facts and verify coverage for the full supported period.

    QRP comes from IPSF Federal PPS Blend Indicator (field 18); the HHVBP
    multiplier comes from Special Provider Update Factor (field 30), as specified
    by CMS's provider-specific-file addendum, not the hospital/SNF VBP field:
    https://www.cms.gov/files/document/r12452cp.pdf

    Older effective records require a pinned CMS full-history/annual reference
    entry covering the complete period and corroborating the actual used facts.
    Missing reference evidence is not an enrollment determination.
    Patient service geography is deliberately resolved outside this function.
    Database exceptions propagate for the route to report service unavailability.
    """
    if not isinstance(ccn, str) or not re.fullmatch(r"[A-Z0-9]{6,13}", ccn):
        raise HhaInputError("HHA_PROVIDER_INVALID_ID", "A normalized HH provider CCN is required.")
    if type(start) is not date or type(end) is not date or start > end:
        raise HhaInputError("HHA_INVALID_PERIOD", "A valid ordered HH period is required.")
    start_int, end_int = int(start.strftime("%Y%m%d")), int(end.strftime("%Y%m%d"))

    with Session(engine) as session:
        rows = session.scalars(
            select(IPSF)
            .where(IPSF.provider_ccn == ccn, IPSF.effective_date <= end_int)
            .order_by(IPSF.effective_date.desc())
        ).all()
        if not rows:
            raise HhaInputError(
                "HHA_PROVIDER_NOT_FOUND", "No effective IPSF provider record exists for the supplied HH CCN and period."
            )
        row = rows[0]
        effective = _reference_date(row.effective_date, "effective date")
        if sum(item.effective_date == row.effective_date for item in rows) != 1:
            raise HhaInputError(
                "HHA_PROVIDER_REFERENCE_AMBIGUOUS", "More than one IPSF provider record exists on the selected effective date."
            )
        crossover_coverage = None
        if start.year != end.year:
            crossover_coverage = _crossover_coverage(rows, effective, start, end)
        elif effective > start:
            raise HhaInputError(
                "HHA_PROVIDER_CHANGED_WITHIN_PERIOD", "The provider reference changes within the full HH period; this case is outside first-pass support."
            )
        raw_termination = row.termination_date
        termination = _active_termination(row, end)

        qrp = (row.federal_pps_blend or "").strip()
        if qrp not in {"0", "2"}:
            raise HhaInputError(
                "HHA_PROVIDER_QRP_INVALID", "The HH provider record does not contain a supported final-payment QRP indicator."
            )
        factor = _vbp_factor(row.special_provider_update_factor)
        raw_used = {
            "providerCcn": row.provider_ccn,
            "providerRowId": row.id,
            "effectiveDate": row.effective_date,
            "terminationDate": raw_termination,
            "providerType": row.provider_type,
            "federalPpsBlend": row.federal_pps_blend,
            "specialProviderUpdateFactor": str(factor),
        }
        fingerprint = hashlib.sha256(
            json.dumps(raw_used, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = {
            "providerData": {
                "providerCcn": ccn,
                "effectiveDate": effective.isoformat(),
                "vbpAdjustment": factor,
            },
            "qualityReportingProgramIndicator": qrp,
            "providerEvidence": {
                "source": "IPSF",
                "providerRowId": row.id,
                "providerCcn": ccn,
                "providerType": "36",
                "effectiveDate": effective.isoformat(),
                "terminationDate": termination.isoformat() if termination else None,
                "rawTerminationDate": raw_termination,
                "lookupThroughDate": end.isoformat(),
                "federalPpsBlend": qrp,
                "specialProviderUpdateFactor": factor,
                "qualityReportingSourceField": "federal_pps_blend",
                "valueBasedPurchasingSourceField": "special_provider_update_factor",
                "sourceRowFingerprint": fingerprint,
            },
        }
        if crossover_coverage is not None:
            result["providerEvidence"]["crossoverProviderEvidence"] = crossover_coverage
        if effective.year != end.year:
            if end.year != 2026:
                raise HhaInputError("HHA_PROVIDER_REFERENCE_STALE",
                    f"The older effective HH provider record lacks reviewed CY {end.year} annual payment evidence.")
            try:
                result["providerEvidence"]["annualPaymentEvidence"] = annual_payment_evidence(
                    result["providerEvidence"], start, end)
            except AnnualProviderReferenceError as unsupported:
                raise HhaInputError(unsupported.code, unsupported.reason) from None
        return result
