from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


PoaValue = Literal["Y", "N", "W", "U", "1", "E", ""]
SexValue = Literal["M", "F", "U"]


class IppsDiagnosis(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    poa: PoaValue = ""

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper().replace(".", "")


class IppsProcedure(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    date: date | None = None
    modifier: str = ""

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper().replace(".", "")


class IppsValueCode(BaseModel):
    code: str = Field(min_length=1, max_length=2)
    amount: float = 0.0

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class IppsClaimLine(BaseModel):
    claimLineId: int | str | None = None
    lineNumber: int | None = None
    dateOfService: date | None = None
    revenueCode: str = ""
    hcpcs: str = ""
    modifiers: list[str] = Field(default_factory=list, max_length=4)
    units: float = Field(default=0.0, ge=0)
    chargeAmount: float = Field(default=0.0, ge=0)

    @field_validator("revenueCode", "hcpcs")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("modifiers")
    @classmethod
    def normalize_modifiers(cls, values: list[str]) -> list[str]:
        return [value.strip().upper() for value in values if value.strip()]


class IppsClaimRequest(BaseModel):
    claimId: str = Field(min_length=1)
    admitDate: date
    fromDate: date
    throughDate: date

    billType: str = Field(default="111", min_length=3, max_length=3)
    patientStatus: str = Field(min_length=2, max_length=2)

    providerNumber: str = Field(min_length=6, max_length=6)
    npi: str | None = None
    patientControlNumber: str | None = None

    patientAge: int | None = Field(default=None, ge=0, le=124)
    patientDateOfBirth: date | None = None
    patientSex: SexValue

    admissionSource: str = ""
    lengthOfStay: int | None = Field(default=None, ge=0)
    nonCoveredDays: int = Field(default=0, ge=0)
    hmo: bool = False

    totalCharges: float = Field(ge=0)

    principalDiagnosis: IppsDiagnosis
    admitDiagnosis: IppsDiagnosis | None = None
    secondaryDiagnoses: list[IppsDiagnosis] = Field(default_factory=list)
    inpatientProcedures: list[IppsProcedure] = Field(default_factory=list)

    conditionCodes: list[str] = Field(default_factory=list)
    valueCodes: list[IppsValueCode] = Field(default_factory=list)
    claimLines: list[IppsClaimLine] = Field(default_factory=list)

    reviewCode: str = "00"
    lifetimeReserveDays: int = Field(default=0, ge=0)
    midnightAdjustmentGeolocation: str = ""

    @field_validator(
        "billType",
        "patientStatus",
        "providerNumber",
        "npi",
        "patientControlNumber",
        "admissionSource",
        "reviewCode",
        "midnightAdjustmentGeolocation",
        mode="before",
    )
    @classmethod
    def trim_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("conditionCodes")
    @classmethod
    def normalize_condition_codes(cls, values: list[str]) -> list[str]:
        return [value.strip().upper() for value in values if value.strip()]

    @model_validator(mode="after")
    def validate_claim(self) -> "IppsClaimRequest":
        if self.fromDate > self.throughDate:
            raise ValueError("fromDate cannot be after throughDate")

        if self.admitDate > self.throughDate:
            raise ValueError("admitDate cannot be after throughDate")

        if self.patientAge is None and self.patientDateOfBirth is None:
            raise ValueError(
                "Either patientAge or patientDateOfBirth must be supplied"
            )

        for procedure in self.inpatientProcedures:
            if procedure.date is not None:
                if procedure.date < self.admitDate or procedure.date > self.throughDate:
                    raise ValueError(
                        f"Procedure {procedure.code} date must fall between "
                        "admitDate and throughDate"
                    )

        for line in self.claimLines:
            if line.dateOfService is not None:
                if line.dateOfService < self.fromDate or line.dateOfService > self.throughDate:
                    raise ValueError(
                        "Claim line dateOfService must fall between "
                        "fromDate and throughDate"
                    )

        return self


class IppsClaimResponse(BaseModel):
    success: bool
    claimId: str
    msdrg: dict[str, Any]
    ipsfProvider: dict[str, Any]
    ipps: dict[str, Any]
