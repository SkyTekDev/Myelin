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


def provider_for_period(engine, ccn: str, start: date, end: date) -> dict:
    """Resolve a single actual IPSF HH record for the full supported period.

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
        if effective > start:
            raise HhaInputError(
                "HHA_PROVIDER_CHANGED_WITHIN_PERIOD", "The provider reference changes within the full HH period; this case is outside first-pass support."
            )
        if (row.provider_type or "").strip() != "36":
            raise HhaInputError(
                "HHA_PROVIDER_TYPE_MISMATCH", "The effective IPSF provider record is not a home health agency."
            )
        # NULL and these sentinels are source representations of no termination.
        raw_termination = row.termination_date
        termination = None
        if raw_termination not in (None, 0, 19000101, 99991231):
            termination = _reference_date(raw_termination, "termination date")
            if termination <= end:
                raise HhaInputError(
                    "HHA_PROVIDER_TERMINATED", "The HH provider is terminated on or before the period's through date."
                )

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
