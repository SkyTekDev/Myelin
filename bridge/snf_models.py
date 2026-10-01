from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from bridge.snf_release import FIRST_SERVICE_DATE, LAST_SERVICE_DATE, UNSUPPORTED_DATES_REASON


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lineId: str = Field(min_length=1)
    hippsCode: str = Field(pattern=r"^(?:[A-P][A-L][A-Y][A-F][01]|ZZZZZ)$")
    fromDate: date
    throughDate: date
    units: StrictInt = Field(ge=1, le=99)
    pdpmPriorDays: StrictInt = Field(ge=0, le=999)

    @model_validator(mode="after")
    def dates(self):
        if not FIRST_SERVICE_DATE <= self.fromDate <= self.throughDate <= LAST_SERVICE_DATE:
            raise ValueError(UNSUPPORTED_DATES_REASON)
        if (self.throughDate - self.fromDate).days + 1 != self.units:
            raise ValueError("Use inclusive covered-day segments; split noncovered gaps.")
        fy = lambda d: d.year + (d.month >= 10)
        if fy(self.fromDate) != fy(self.throughDate):
            raise ValueError("Split segments at the fiscal-year boundary.")
        if self.pdpmPriorDays + self.units > 100:
            raise ValueError("This integration supports PDPM days 1 through 100.")
        return self


class SnfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claimId: str = Field(min_length=1)
    submitterId: str = Field(min_length=1, max_length=128)
    patientId: str = Field(min_length=1, max_length=128)
    billType: str = Field(pattern=r"^21[12347]$")
    providerCcn: str = Field(pattern=r"^[A-Z0-9]{6,13}$")
    diagnosisCodes: list[str] = Field(min_length=1, max_length=25)
    segments: list[Segment] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def segments_valid(self):
        import re
        if any(not x.strip() for x in (self.claimId, self.submitterId, self.patientId)):
            raise ValueError("Identifiers must not be blank.")
        if any(not re.fullmatch(r"[A-Z0-9]{2,7}", x) for x in self.diagnosisCodes):
            raise ValueError("Diagnosis codes must be normalized alphanumeric codes.")
        if len({x.lineId for x in self.segments}) != len(self.segments):
            raise ValueError("Duplicate segment identity.")
        ordered = sorted(self.segments, key=lambda x: x.fromDate)
        for previous, current in zip(ordered, ordered[1:]):
            if current.fromDate <= previous.throughDate:
                raise ValueError("HIPPS segments overlap.")
            if (current.fromDate - previous.throughDate).days != 1:
                raise ValueError("Interrupted segments in a single claim are not supported.")
            if current.pdpmPriorDays != previous.pdpmPriorDays + previous.units:
                raise ValueError("Inconsistent prior days between segments in this stay.")
        return self


class SegmentResult(BaseModel):
    lineId: str
    providerEffectiveDate: date
    providerData: dict
    cms: dict


class SnfResponse(BaseModel):
    claimId: str
    status: str
    reason: str
    totalPayment: Decimal | None = None
    segments: list[SegmentResult] = Field(default_factory=list)
