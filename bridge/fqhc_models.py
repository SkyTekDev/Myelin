"""Request and response contracts for the FQHC endpoint (I/OCE, then the CMS FQHC Pricer)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bridge.api_models import IoceClaimRequest


class FqhcClaimRequest(IoceClaimRequest):
    """An I/OCE claim plus the FQHC site's Medicare carrier and locality.

    The caller resolves the locality from the site's ZIP code; the bridge holds
    no ZIP crosswalk. The GAF comes from the CMS jar's table for that locality.
    """

    # Neither the I/OCE's FQHC logic nor the FQHC Pricer uses the CCN, and many
    # FQHC claims carry only an NPI.
    providerNumber: str | None = Field(default=None, min_length=6, max_length=6)
    carrierCode: str = Field(pattern=r"^[0-9]{5}$")
    localityCode: str = Field(pattern=r"^[0-9]{2}$")
    # Only for Medicare Advantage supplemental (revenue code 0519) claims.
    medicareAdvantagePlanAmount: Decimal | None = Field(default=None, gt=0, max_digits=11, decimal_places=2)

    @field_validator("providerNumber")
    @classmethod
    def validate_provider_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper()
        if not normalized.isalnum():
            raise ValueError("Provider number must contain exactly six letters or numbers.")
        return normalized

    @model_validator(mode="after")
    def validate_fqhc_claim(self) -> "FqhcClaimRequest":
        if not self.billType.startswith("77"):
            raise ValueError("FQHC pricing requires a 77X bill type.")
        if self.oppsFlag != 1:
            raise ValueError("FQHC claims are edited with oppsFlag 1; the I/OCE assigns no FQHC flags otherwise.")
        if self.providerContext is not None:
            raise ValueError("providerContext applies only to /api/v1/opps/process.")
        if any(line.editBypass for line in self.claimLines):
            raise ValueError("Contractor edit bypass is not available for FQHC claims.")
        if len({str(line.claimLineId) for line in self.claimLines}) != len(self.claimLines):
            raise ValueError("claimLineId values must be unique.")
        if any(line.chargeAmount.as_tuple().exponent < -2 for line in self.claimLines):
            raise ValueError("chargeAmount supports at most two decimal places.")
        return self


class FqhcEdit(BaseModel):
    edit: str
    description: str = ""


class FqhcClaimEdits(BaseModel):
    returnToProvider: list[FqhcEdit] = Field(default_factory=list)
    suspension: list[FqhcEdit] = Field(default_factory=list)
    rejection: list[FqhcEdit] = Field(default_factory=list)
    denial: list[FqhcEdit] = Field(default_factory=list)


class FqhcLineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimLineId: int | str
    lineNumber: int
    revenueCode: str
    hcpcs: str | None = None
    dateOfService: date
    units: int
    chargeAmount: Decimal
    # I/OCE output for the line.
    statusIndicator: str = ""
    paymentIndicator: str = ""
    paymentIndicatorDescription: str = ""
    paymentMethodFlag: str = ""
    compositeAdjustmentFlag: str = ""
    packageFlag: str = ""
    actionFlag: str = ""
    denialOrRejectionFlag: str = ""
    serviceUnits: int | None = None
    ioceEdits: list[FqhcEdit] = Field(default_factory=list)
    # CMS FQHC Pricer output for the line.
    pricerReturnCode: str | None = None
    pricerReturnDescription: str | None = None
    payment: Decimal | None = None
    coinsuranceAmount: Decimal | None = None
    addOnPayment: Decimal | None = None


class FqhcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimId: str
    status: Literal["Success", "UnableToPrice"]
    reason: str
    reasonCode: str = ""
    bridgeVersion: str
    pricerRelease: str
    jarSha256: str
    calculationVersion: str | None = None
    ioceVersion: str | None = None
    carrierCode: str
    localityCode: str
    claimProcessedFlag: str | None = None
    claimEdits: FqhcClaimEdits = Field(default_factory=FqhcClaimEdits)
    pricerReturnCode: str | None = None
    pricerReturnDescription: str | None = None
    geographicAdjustmentFactor: Decimal | None = None
    totalPayment: Decimal | None = None
    totalCoinsurance: Decimal | None = None
    lines: list[FqhcLineResult] = Field(default_factory=list)
