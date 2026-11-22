"""Request and response contracts for the hospice endpoint (the CMS Hospice Pricer, one call per level-of-care line)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bridge.hospice_release import (
    CHC_MAXIMUM_UNITS,
    CONTINUOUS_HOME_CARE,
    DAY_LEVELS,
    END_OF_LIFE_DAYS,
    GENERAL_INPATIENT_CARE,
    INPATIENT_RESPITE_CARE,
    RESPITE_MAXIMUM_DAYS,
    ROUTINE_HOME_CARE,
)

QUALITY_SOURCES = ("ProviderFile", "FullRatesProviderFileBeforeFiscalYear", "FullRatesProviderNotFound", "FullRatesProviderAmbiguous")


class HospiceApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HospiceLine(HospiceApiModel):
    """A level-of-care line: revenue code 0651, 0652, 0655 or 0656 with its first date of service."""

    claimLineId: int | str
    lineNumber: int = Field(ge=1)
    revenueCode: Literal["0651", "0652", "0655", "0656"]
    # Q5001-Q5010 (site of service). The pricer does not use it.
    hcpcs: str | None = Field(default=None, pattern=r"^[A-Z0-9]{5}$")
    dateOfService: date
    # Days for 0651/0655/0656; 15-minute increments for 0652.
    units: int = Field(ge=1, le=999)

    @property
    def last_day(self) -> date:
        return self.dateOfService + timedelta(days=self.units - 1) if self.revenueCode in DAY_LEVELS else self.dateOfService


def routine_home_care_days(lines: list[HospiceLine]) -> dict[date, HospiceLine]:
    """Each routine home care day and its line.

    A 0651 line's units count its routine home care days only: continuous home care days inside the period are billed on
    their own dated 0652 lines (Claims Processing Manual chapter 11, section 30.3), so the walk skips them.
    """
    continuous = {line.dateOfService for line in lines if line.revenueCode == CONTINUOUS_HOME_CARE}
    days: dict[date, HospiceLine] = {}
    for line in (line for line in lines if line.revenueCode == ROUTINE_HOME_CARE):
        day, counted = line.dateOfService, 0
        while counted < line.units:
            if day not in continuous:
                if day in days:
                    raise ValueError(f"Lines {days[day].lineNumber} and {line.lineNumber} both cover {day.isoformat()}; each day has one level of care.")
                days[day] = line
                counted += 1
            day += timedelta(days=1)
    return days


class HospiceClaimRequest(HospiceApiModel):
    """A monthly hospice claim (81X/82X). The caller sends only its level-of-care lines."""

    claimId: int | str
    billType: str = Field(pattern=r"^8[12][12347]$")
    providerNumber: str | None = Field(default=None, min_length=6, max_length=6)
    npi: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    # The election's effective date, unchanged on continuing claims.
    admissionDate: date
    fromDate: date
    throughDate: date
    # Value code 61 (the beneficiary's location, for 0651/0652) and G8 (the inpatient facility, for 0655/0656):
    # a CBSA, a 50xxx transition code or a rural state code. The CMS jar pads short codes with 9s.
    patientCbsa: str | None = Field(default=None, pattern=r"^[0-9]{1,5}$")
    providerCbsa: str | None = Field(default=None, pattern=r"^[0-9]{1,5}$")
    # Days of a prior hospice election that count toward the 60 high-rate days (CWF supplies them in FISS).
    priorBenefitDays: int = Field(default=0, ge=0, le=999)
    # Sent only with add-on units: the claim's through date, which Medicare takes as the date of death for discharge
    # status 40, 41 or 42.
    dateOfDeath: date | None = None
    # The qualifying registered nurse and social worker visit units on each of the last 7 days of life, day 1 being the
    # date of death (FISS's EOL day 1-7 add-on units). The CMS jar pays at most 16 a day.
    endOfLifeAddOnUnits: list[Annotated[int, Field(ge=0, le=999)]] | None = Field(
        default=None, min_length=END_OF_LIFE_DAYS, max_length=END_OF_LIFE_DAYS)
    claimLines: list[HospiceLine] = Field(min_length=1, max_length=450)

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
    def validate_hospice_claim(self) -> "HospiceClaimRequest":
        # Each check below prevents a result the CMS jar would return without complaint.
        if self.fromDate > self.throughDate:
            raise ValueError("fromDate must not be after throughDate.")
        if (self.fromDate.year, self.fromDate.month) != (self.throughDate.year, self.throughDate.month):
            raise ValueError("Hospice claims cover one calendar month; split the claim at the month boundary.")
        if len({str(line.claimLineId) for line in self.claimLines}) != len(self.claimLines):
            raise ValueError("claimLineId values must be unique.")
        routine = routine_home_care_days(self.claimLines)
        last_routine_day: dict[str, date] = {}
        for day, line in routine.items():
            key = str(line.claimLineId)
            last_routine_day[key] = max(last_routine_day.get(key, day), day)
        for line in self.claimLines:
            last_day = last_routine_day[str(line.claimLineId)] if line.revenueCode == ROUTINE_HOME_CARE else line.last_day
            if line.dateOfService < self.admissionDate:
                raise ValueError(f"Line {line.lineNumber} is dated before the admission date; the CMS pricer pays it $0 without an error.")
            if not self.fromDate <= line.dateOfService <= self.throughDate or last_day > self.throughDate:
                raise ValueError(f"Line {line.lineNumber} runs outside the claim's fromDate-throughDate.")
            if line.revenueCode == CONTINUOUS_HOME_CARE and line.units > CHC_MAXIMUM_UNITS:
                raise ValueError(f"Line {line.lineNumber}: continuous home care is billed per day, at most {CHC_MAXIMUM_UNITS} "
                                 "15-minute units; the CMS pricer applies no daily limit.")
            if line.revenueCode == INPATIENT_RESPITE_CARE and line.units > RESPITE_MAXIMUM_DAYS:
                raise ValueError(f"Line {line.lineNumber}: a respite period lasts at most {RESPITE_MAXIMUM_DAYS} days.")
        respite = sorted((line for line in self.claimLines if line.revenueCode == INPATIENT_RESPITE_CARE), key=lambda line: line.dateOfService)
        run = 0
        for previous, current in zip([None, *respite], respite):
            run = run + current.units if previous is not None and previous.last_day + timedelta(days=1) == current.dateOfService else current.units
            if run > RESPITE_MAXIMUM_DAYS:
                raise ValueError(f"Line {current.lineNumber}: consecutive respite lines exceed {RESPITE_MAXIMUM_DAYS} days.")
        # Routine home care days (the walk above) and inpatient days (0655/0656: the first date plus units - 1 days) must
        # not overlap.
        days: dict[date, HospiceLine] = dict(routine)
        for line in (line for line in self.claimLines if line.revenueCode in (INPATIENT_RESPITE_CARE, GENERAL_INPATIENT_CARE)):
            day = line.dateOfService
            while day <= line.last_day:
                if day in days:
                    raise ValueError(f"Lines {days[day].lineNumber} and {line.lineNumber} both cover {day.isoformat()}; each day has one level of care.")
                days[day] = line
                day += timedelta(days=1)
        continuous: dict[date, int] = {}
        for line in (line for line in self.claimLines if line.revenueCode == CONTINUOUS_HOME_CARE):
            if line.dateOfService in continuous:
                raise ValueError(f"Lines {continuous[line.dateOfService]} and {line.lineNumber} are both continuous home care on "
                                 f"{line.dateOfService.isoformat()}; bill one 0652 line per day.")
            continuous[line.dateOfService] = line.lineNumber
            inpatient = days.get(line.dateOfService)
            if inpatient is not None and inpatient.revenueCode != ROUTINE_HOME_CARE:
                raise ValueError(f"Line {line.lineNumber}: continuous home care is not paid during the inpatient stay on line {inpatient.lineNumber}.")
        revenue = {line.revenueCode for line in self.claimLines}
        if revenue & {ROUTINE_HOME_CARE, CONTINUOUS_HOME_CARE} and not self.patientCbsa:
            raise ValueError("patientCbsa (value code 61) is required with 0651/0652 lines; without it the CMS pricer uses a wage index of 0.")
        if revenue & {INPATIENT_RESPITE_CARE, GENERAL_INPATIENT_CARE} and not self.providerCbsa:
            raise ValueError("providerCbsa (value code G8) is required with 0655/0656 lines; without it the CMS pricer uses a wage index of 0.")
        if (self.dateOfDeath is None) != (self.endOfLifeAddOnUnits is None):
            raise ValueError("Send dateOfDeath and endOfLifeAddOnUnits together.")
        if self.dateOfDeath is not None:
            if self.dateOfDeath != self.throughDate:
                raise ValueError("dateOfDeath must be the claim's throughDate: Medicare takes the through date as the date of death "
                                 "for discharge status 40, 41 or 42.")
            if not any(self.endOfLifeAddOnUnits):
                raise ValueError("endOfLifeAddOnUnits needs at least one unit; omit the add-on fields when no visit qualifies.")
            for index, units in enumerate(self.endOfLifeAddOnUnits):
                day = self.dateOfDeath - timedelta(days=index)
                if units and day not in routine:
                    raise ValueError(f"endOfLifeAddOnUnits day {index + 1} ({day.isoformat()}) is not a routine home care day; "
                                     "the add-on applies only on routine home care days.")
        return self


class HospiceProviderRecord(BaseModel):
    """The provider file (OPSF) record the quality indicator came from."""

    model_config = ConfigDict(extra="forbid")

    ccn: str
    npi: str | None = None
    effectiveDate: date
    terminationDate: date | None = None
    providerType: str
    qualityIndicator: str = ""
    npiMatchesClaim: bool | None = None


class HospiceLineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimLineId: int | str
    lineNumber: int
    revenueCode: str
    hcpcs: str | None = None
    dateOfService: date
    units: int
    levelOfCare: str
    # The day of the election the line starts on, counting the admission date as day 1 and adding prior benefit days.
    electionDay: int
    payment: Decimal | None = None
    pricerReturnCode: str | None = None
    pricerReturnDescription: str | None = None
    highRoutineHomeCareDays: int = 0
    lowRoutineHomeCareDays: int = 0
    # Continuous home care under 32 units is paid as one routine home care day.
    paidAsRoutineHomeCareDay: bool = False


class HospiceEndOfLifeDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 1 is the date of death, 7 six days before it.
    day: int
    dateOfService: date
    units: int
    # At most 16 (4 hours) a day are paid.
    unitsPaid: int
    payment: Decimal


class HospiceEndOfLifeAddOn(BaseModel):
    """The CMS Hospice Pricer's service intensity add-on, priced with one routine home care line's call."""

    model_config = ConfigDict(extra="forbid")

    dateOfDeath: date
    attachedClaimLineId: int | str
    attachedLineNumber: int
    days: list[HospiceEndOfLifeDay]
    totalPayment: Decimal


class HospiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimId: str
    status: Literal["Success", "UnableToPrice"]
    reason: str
    reasonCode: str = ""
    bridgeVersion: str
    pricerRelease: str
    jarSha256: str
    calculationVersion: str | None = None
    fiscalYear: int | None = None
    admissionDate: date
    fromDate: date
    throughDate: date
    patientCbsa: str | None = None
    providerCbsa: str | None = None
    patientWageIndex: Decimal | None = None
    providerWageIndex: Decimal | None = None
    priorBenefitDays: int = 0
    qualityReduction: bool | None = None
    qualitySource: Literal["ProviderFile", "FullRatesProviderFileBeforeFiscalYear", "FullRatesProviderNotFound",
                           "FullRatesProviderAmbiguous"] | None = None
    qualityNote: str | None = None
    provider: HospiceProviderRecord | None = None
    # The latest effective date of any hospice record in the loaded provider file.
    providerFileHospiceRecordsThrough: date | None = None
    # The level-of-care payments plus the end-of-life add-on.
    totalPayment: Decimal | None = None
    highRoutineHomeCareDays: int | None = None
    lowRoutineHomeCareDays: int | None = None
    dateOfDeath: date | None = None
    endOfLifeAddOnUnits: list[int] | None = None
    endOfLifeAddOn: HospiceEndOfLifeAddOn | None = None
    lines: list[HospiceLineResult] = Field(default_factory=list)
