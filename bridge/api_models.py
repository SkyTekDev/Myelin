from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

        return self
