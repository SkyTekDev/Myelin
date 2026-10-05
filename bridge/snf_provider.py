from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from myelin.pricers.ipsf import IPSF


class SnfInputError(ValueError):
    pass


def provider_for_segment(engine, ccn: str, start: date, end: date) -> dict:
    """Read actual IPSF fields without IPSFProvider's favorable/default values."""
    start_int = int(start.strftime("%Y%m%d"))
    end_int = int(end.strftime("%Y%m%d"))
    with Session(engine) as session:
        rows = session.scalars(select(IPSF).where(
            IPSF.provider_ccn == ccn, IPSF.effective_date <= end_int
        ).order_by(IPSF.effective_date.desc())).all()
        if not rows:
            raise SnfInputError("No effective IPSF provider record for the supplied CCN.")
        effective = rows[0].effective_date
        if effective > start_int:
            raise SnfInputError("Split HIPPS segment at the provider effective-date change.")
        if sum(x.effective_date == effective for x in rows) != 1:
            raise SnfInputError("Ambiguous IPSF provider records on the effective date.")
        row = rows[0]
        fiscal_year_start = date(start.year if start.month >= 10 else start.year - 1, 10, 1)
        if start >= date(2026, 10, 1) and effective < int(fiscal_year_start.strftime("%Y%m%d")):
            raise SnfInputError("Current fiscal-year SNF provider inputs are missing; refresh IPSF before pricing.")
        if (row.provider_type or "").strip() != "38":
            raise SnfInputError("Provider is not classified as SNF in IPSF.")
        if row.termination_date not in (None, 0, 19000101, 99991231) and row.termination_date <= end_int:
            raise SnfInputError("Provider is terminated within the service period.")
        blend = (row.federal_pps_blend or "").strip()
        cbsa = (row.cbsa_actual_geographic_location or "").strip()
        if blend not in ("0", "1", "4") or not cbsa or not row.vbp_adjustment or row.vbp_adjustment <= 0:
            raise SnfInputError("Provider QRP, geography or VBP data is missing or unsupported.")
        data = dict(providerCcn=ccn,
                    effectiveDate=datetime.strptime(str(effective), "%Y%m%d").date().isoformat(),
                    cbsaActualGeographicLocation=cbsa, federalPpsBlend=blend,
                    vbpAdjustment=Decimal(str(row.vbp_adjustment)))
        for name, value in (("specialPaymentIndicator", row.special_payment_indicator),
                            ("supplementalWageIndexIndicator", row.supplemental_wage_index_indicator)):
            if value is None:
                raise SnfInputError("Provider wage adjustment status is missing.")
            data[name] = value.strip()
        for name, value in (("specialWageIndex", row.special_wage_index),
                            ("supplementalWageIndex", row.supplemental_wage_index)):
            if value is not None:
                data[name] = Decimal(str(value))
        if data["specialPaymentIndicator"] in ("1", "Y") and data.get("specialWageIndex", 0) <= 0:
            raise SnfInputError("Missing special wage index.")
        if data["supplementalWageIndexIndicator"] == "1" and data.get("supplementalWageIndex", 0) <= 0:
            raise SnfInputError("Missing supplemental wage index.")
        # Blank indicators are explicit no-adjustment values in the provider file.
        return {k: v for k, v in data.items() if v != ""}


def prior_fiscal_year_provider(engine, ccn: str, start: date):
    """None means no prior provider history; unavailable history is never defaulted."""
    fiscal_year = start.year + (start.month >= 10)
    prior_end = date(fiscal_year - 1, 9, 30)
    with Session(engine) as session:
        exists = session.scalar(select(IPSF.id).where(
            IPSF.provider_ccn == ccn, IPSF.effective_date <= int(prior_end.strftime("%Y%m%d"))
        ).limit(1))
    if exists is None:
        return None, prior_end
    return provider_for_segment(engine, ccn, prior_end, prior_end), prior_end
