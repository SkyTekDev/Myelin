from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bridge.edit_bypass import normalize_edit


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class DiagnosisRequest(ApiModel):
    code: str = Field(min_length=3, max_length=8)
    poa: Literal["Y", "N", "W", "U", "1", "E", ""] = "U"

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.replace(".", "").upper()

        if not normalized.isalnum():
            raise ValueError(
                "Diagnosis code must contain only letters and numbers."
            )

        return normalized


class ValueCodeRequest(ApiModel):
    code: str = Field(min_length=2, max_length=2)
    amount: Decimal = Field(ge=0)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.upper()

        if not normalized.isalnum():
            raise ValueError(
                "Value code must contain only letters and numbers."
            )

        return normalized


class ClaimLineRequest(ApiModel):
    claimLineId: int | str
    lineNumber: int = Field(ge=1)
    revenueCode: str = Field(min_length=4, max_length=4)
    hcpcs: str | None = Field(default=None, max_length=5)
    dateOfService: date

    # Myelin's current I/OCE mapper converts units to int. Reject fractional
    # units here rather than allowing silent truncation.
    units: int = Field(ge=1, le=999_999_999)

    chargeAmount: Decimal = Field(ge=0)
    modifiers: list[str] = Field(default_factory=list, max_length=4)

    # Contractor edit bypass for this line, as I/OCE edit numbers. The endpoint
    # accepts only edits listed in the deployment's IOCE_EDIT_BYPASS_ALLOWED.
    editBypass: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("revenueCode")
    @classmethod
    def validate_revenue_code(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError(
                "Revenue code must contain exactly four digits."
            )

        return value

    @field_validator("hcpcs")
    @classmethod
    def normalize_hcpcs(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None

        normalized = value.replace(".", "").upper()

        if not normalized.isalnum():
            raise ValueError(
                "HCPCS must contain only letters and numbers."
            )

        return normalized

    @field_validator("modifiers")
    @classmethod
    def normalize_modifiers(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []

        for value in values:
            modifier = value.strip().upper()

            if len(modifier) != 2 or not modifier.isalnum():
                raise ValueError(
                    "Each HCPCS modifier must contain exactly two "
                    "letters or numbers."
                )

            normalized.append(modifier)

        return normalized

    @field_validator("editBypass")
    @classmethod
    def normalize_edit_bypass(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []

        for value in values:
            try:
                edit = normalize_edit(value)
            except ValueError as error:
                raise ValueError(
                    "Each edit bypass must be an I/OCE edit number of "
                    "one to three digits."
                ) from error

            if edit not in normalized:
                normalized.append(edit)

        return normalized


CBSA_PATTERN = re.compile(r"[0-9]{2}|[0-9]{5}")


def validate_cbsa(value: str, field: str) -> str:
    # The Pricer's wage tables key a rural area by its two-digit state code and
    # an urban area by its five-digit CBSA; "999SS" is not a table key.
    if not CBSA_PATTERN.fullmatch(value) or value.startswith("999"):
        raise ValueError(
            f"{field} must be a two-digit rural state code or a five-digit CBSA."
        )

    return value


class ProviderContextRequest(ApiModel):
    """Provider fields the OPPS Pricer uses in place of an OPSF record."""

    providerCcn: str = Field(min_length=6, max_length=6)
    providerType: str = Field(min_length=2, max_length=2)
    # SSA state code, the first two characters of the CCN.
    stateCode: str = Field(min_length=2, max_length=2)
    effectiveDate: date
    cbsaActualGeographicLocation: str
    # "Y" with a wage-index location applies a reclassification; blank uses
    # the actual geographic location.
    specialPaymentIndicator: Literal["", "Y"] = ""
    cbsaWageIndexLocation: str = ""
    hospitalQualityIndicator: Literal["", "1"] = ""

    @field_validator("providerCcn", "providerType")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.upper()

        if not normalized.isalnum():
            raise ValueError(
                "Provider identifiers must contain only letters and numbers."
            )

        return normalized

    @field_validator("stateCode")
    @classmethod
    def validate_state_code(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("stateCode must contain two digits.")

        return value

    @field_validator("cbsaActualGeographicLocation")
    @classmethod
    def validate_actual_location(cls, value: str) -> str:
        return validate_cbsa(value, "cbsaActualGeographicLocation")

    @field_validator("cbsaWageIndexLocation")
    @classmethod
    def validate_wage_index_location(cls, value: str) -> str:
        if value == "":
            return value

        return validate_cbsa(value, "cbsaWageIndexLocation")

    @model_validator(mode="after")
    def validate_reclassification(self) -> "ProviderContextRequest":
        if self.stateCode != self.providerCcn[:2]:
            raise ValueError(
                "stateCode must match the first two characters of providerCcn."
            )

        if (self.specialPaymentIndicator == "Y") != (self.cbsaWageIndexLocation != ""):
            raise ValueError(
                "cbsaWageIndexLocation is required with specialPaymentIndicator "
                "\"Y\" and must be blank otherwise."
            )

        return self


class IoceClaimRequest(ApiModel):
    claimId: int | str

    fromDate: date | None = None
    throughDate: date | None = None

    # Retained for compatibility with the original single-date contract.
    dateOfService: date | None = None

    billType: str = Field(min_length=3, max_length=3)
    patientStatus: str = Field(default="01", min_length=2, max_length=2)

    # CMS Certification Number (CCN) / provider number.
    providerNumber: str = Field(min_length=6, max_length=6)
    npi: str | None = Field(default=None, min_length=10, max_length=10)

    patientControlNumber: str | None = Field(default=None, max_length=50)
    patientAge: int = Field(ge=0, le=124)
    patientSex: Literal["M", "F", "U"]
    oppsFlag: Literal[1, 2] = 1

    principalDiagnosis: DiagnosisRequest
    secondaryDiagnoses: list[DiagnosisRequest] = Field(default_factory=list)
    conditionCodes: list[str] = Field(default_factory=list, max_length=30)
    valueCodes: list[ValueCodeRequest] = Field(default_factory=list)
    claimLines: list[ClaimLineRequest] = Field(min_length=1)

    # OPPS only: provider fields used in place of the OPSF lookup. Accepted only
    # for provider types in the deployment's OPPS_PROVIDER_CONTEXT_TYPES.
    providerContext: ProviderContextRequest | None = None

    @field_validator("billType")
    @classmethod
    def validate_bill_type(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError(
                "Bill type must contain exactly three digits."
            )

        return value

    @field_validator("patientStatus")
    @classmethod
    def validate_patient_status(cls, value: str) -> str:
        if not value.isalnum():
            raise ValueError(
                "Patient status must contain two letters or numbers."
            )

        return value.upper()

    @field_validator("providerNumber")
    @classmethod
    def validate_provider_number(cls, value: str) -> str:
        normalized = value.upper()

        if not normalized.isalnum():
            raise ValueError(
                "Provider number must contain exactly six letters or numbers."
            )

        return normalized

    @field_validator("npi")
    @classmethod
    def validate_npi(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("NPI must contain exactly ten digits.")

        return value

    @field_validator("conditionCodes")
    @classmethod
    def normalize_condition_codes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []

        for value in values:
            code = value.strip().upper()

            if len(code) != 2 or not code.isalnum():
                raise ValueError(
                    "Each condition code must contain exactly two "
                    "letters or numbers."
                )

            normalized.append(code)

        return normalized

    @model_validator(mode="after")
    def set_and_validate_dates(self) -> "IoceClaimRequest":
        line_dates = [line.dateOfService for line in self.claimLines]

        if self.fromDate is None:
            self.fromDate = self.dateOfService or min(line_dates)

        if self.throughDate is None:
            self.throughDate = self.dateOfService or max(line_dates)

        if self.fromDate > self.throughDate:
            raise ValueError("fromDate cannot be after throughDate.")

        for line in self.claimLines:
            if not self.fromDate <= line.dateOfService <= self.throughDate:
                raise ValueError(
                    f"Claim line {line.lineNumber} dateOfService must be "
                    "between fromDate and throughDate."
                )

        context = self.providerContext

        if context is not None:
            if context.providerCcn != self.providerNumber:
                raise ValueError(
                    "providerContext.providerCcn must equal providerNumber."
                )

            if context.effectiveDate > self.fromDate:
                raise ValueError(
                    "providerContext.effectiveDate cannot be after fromDate."
                )

        return self
