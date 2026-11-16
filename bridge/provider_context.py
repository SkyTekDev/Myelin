from __future__ import annotations

from typing import TYPE_CHECKING

from myelin.pricers.opsf import OPSFProvider

if TYPE_CHECKING:
    from bridge.api_models import IoceClaimRequest


# A caller can supply a limited OPPS provider context for a provider that has no
# usable OPSF record, such as a critical access hospital (type 37) that CMS does
# not list. The deployment allowlist OPPS_PROVIDER_CONTEXT_TYPES names the
# provider types that may be supplied; empty or unset turns the feature off.
# The CMS OPPS Pricer is unchanged: it receives these fields in place of the
# OPSF record and looks up the wage index in its own dated tables.

OPEN_TERMINATION_DATE = 20991231


def parse_provider_types(raw: str | None) -> frozenset[str]:
    """Parse a comma- or space-separated list of two-character provider types."""
    if raw is None or not raw.strip():
        return frozenset()

    types: set[str] = set()

    for item in raw.replace(";", ",").replace(" ", ",").split(","):
        value = item.strip().upper()

        if not value:
            continue

        if len(value) != 2 or not value.isalnum():
            raise ValueError(f"Invalid OPPS provider type: {item!r}")

        types.add(value)

    return frozenset(types)


def supplied_provider(payload: IoceClaimRequest) -> OPSFProvider:
    """Build the provider record the Pricer receives from the supplied context."""
    context = payload.providerContext

    if context is None:
        raise ValueError("The request does not supply a provider context.")

    effective = int(context.effectiveDate.strftime("%Y%m%d"))

    # Fields the context does not supply stay empty, as in an OPSF record
    # without them: no cost-to-charge ratio (no outlier payment), county,
    # special wage index or supplemental wage index.
    return OPSFProvider(
        provider_ccn=context.providerCcn,
        national_provider_identifier=payload.npi,
        provider_type=context.providerType,
        effective_date=effective,
        fiscal_year_begin_date=effective,
        termination_date=OPEN_TERMINATION_DATE,
        state_code=context.stateCode,
        cbsa_actual_geographic_location=context.cbsaActualGeographicLocation,
        cbsa_wage_index_location=context.cbsaWageIndexLocation,
        special_payment_indicator=context.specialPaymentIndicator,
        hospital_quality_indicator=context.hospitalQualityIndicator,
    )
