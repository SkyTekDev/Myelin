"""Explicit inputs for supplied-HIPPS ordinary-period and LUPA pricing."""

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
    model_serializer,
)
from bridge.hha_release import CY_2026, HhaRelease, release_for_through, require_statement_release


Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


def _calendar_date(value: Any) -> Any:
    """Accept calendar dates/ISO strings, never epoch numbers or datetimes."""
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use an ISO calendar date (yyyy-MM-dd).")
    return value


def _identifier(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("Identifiers must be nonblank and have no surrounding whitespace.")
    return value


class HhaVisit(BaseModel):
    """One covered in-person visit, not an arbitrary claim/revenue line."""

    model_config = ConfigDict(extra="forbid")

    visitId: StrictStr = Field(min_length=1, max_length=128)
    serviceDate: date
    discipline: Literal["PT", "OT", "SLP", "SN", "MSW", "HHA"]
    outlierUnits: StrictInt = Field(ge=1, le=32)

    _visit_identity = field_validator("visitId")(_identifier)
    _visit_date = field_validator("serviceDate", mode="before")(_calendar_date)


class HhaLupaContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceAdmissionIndicator: Literal["1", "B"]
    isFirstOrOnlyPeriod: StrictBool


class HhaOutlierContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendarYear: StrictInt
    asOfDate: date
    priorPaymentTotal: Annotated[Decimal, Field(ge=0, le=Decimal("999999999.99"), decimal_places=2, allow_inf_nan=False)]
    priorOutlierTotal: Annotated[Decimal, Field(ge=0, le=Decimal("999999999.99"), decimal_places=2, allow_inf_nan=False)]
    totalsExcludeCurrentClaim: StrictBool
    basis: Literal["SubmittedProviderYtdTotals"]
    _asof = field_validator("asOfDate", mode="before")(_calendar_date)
    @model_validator(mode="after")
    def valid_snapshot(self):
        if (release_for_through(self.asOfDate) is None or self.asOfDate.year != self.calendarYear
                or not self.totalsExcludeCurrentClaim or self.priorOutlierTotal > self.priorPaymentTotal):
            raise ValueError("Outlier history must use an approved calendar year and exclude this claim, with outliers within total payments.")
        return self


class HhaOutlierPayment(HhaOutlierContext):
    fixedDollarLossRatio: Money
    lossSharingRatio: Annotated[Decimal, Field(ge=Decimal(".8"), le=Decimal(".8"), allow_inf_nan=False)]
    capRatio: Annotated[Decimal, Field(ge=Decimal(".1"), le=Decimal(".1"), allow_inf_nan=False)]
    imputedCost: Money
    periodPaymentBeforeVbp: Money
    fixedDollarLossAmount: Money
    thresholdAmount: Money
    candidateOutlierBeforeVbp: Money
    availableOutlierPool: Money
    capLimited: StrictBool
    capBypassedByCms: StrictBool
    cmsOutlierPayment: Money
    grossOutlierPayment: Money
    netOutlierPayment: Money
    effectiveReturnCode: Literal["00","01","02","06","09","11","14"]
    adjustmentMethod: Literal["CmsOutlierCalculation","ZeroHistoryCapEnforcement","LupaNotApplicable"]
    appliesToPeriod: StrictBool

    @model_validator(mode="after")
    def reconcile(self):
        release = release_for_through(self.asOfDate)
        if release is None or self.fixedDollarLossRatio != release.fixed_dollar_loss_ratio:
            raise ValueError('Outlier fixed dollar loss ratio must match the approved release.')
        pool = max(Decimal(0), (self.priorPaymentTotal*Decimal('.1')).quantize(Decimal('.01'),rounding='ROUND_HALF_UP')-self.priorOutlierTotal)
        candidate = (max(Decimal(0),self.imputedCost-self.thresholdAmount)*Decimal('.8')).quantize(Decimal('.01'),rounding='ROUND_HALF_UP') if self.appliesToPeriod else Decimal(0)
        if (self.availableOutlierPool != pool or self.candidateOutlierBeforeVbp != candidate
                or self.capLimited != (candidate>pool) or self.grossOutlierPayment < self.netOutlierPayment
                or self.capBypassedByCms != (candidate>0 and (self.priorPaymentTotal==0 or self.priorOutlierTotal==0))):
            raise ValueError('Outlier candidate, annual pool, cap, and payment evidence must reconcile.')
        if self.appliesToPeriod:
            if (self.thresholdAmount != self.periodPaymentBeforeVbp+self.fixedDollarLossAmount
                    or self.adjustmentMethod != ('ZeroHistoryCapEnforcement' if self.capLimited and self.capBypassedByCms else 'CmsOutlierCalculation')
                    or (candidate>0 and self.effectiveReturnCode not in (['02'] if self.capLimited else ['01','11']))
                    or (candidate==0 and self.effectiveReturnCode not in ['00','09'])):
                raise ValueError('Period outlier status and threshold evidence must reconcile.')
        elif (self.adjustmentMethod!='LupaNotApplicable' or self.effectiveReturnCode not in ['06','14']
                or any(value!=0 for value in [self.periodPaymentBeforeVbp,self.fixedDollarLossAmount,self.thresholdAmount,self.cmsOutlierPayment,self.grossOutlierPayment,self.netOutlierPayment])):
            raise ValueError('LUPA cannot receive a period outlier payment.')
        return self


class HhaNoaApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis: Literal["SubmittedMacApproval"]
    scope: Literal["EntireAdmission"]
    decisionReference: StrictStr = Field(min_length=1, max_length=128)
    decisionDate: date
    providerCcn: StrictStr = Field(pattern=r"^[A-Z0-9]{6,13}$")
    admissionDate: date
    noticeReceiptDate: date
    _dates = field_validator("decisionDate", "admissionDate", "noticeReceiptDate", mode="before")(_calendar_date)

    @field_validator("decisionReference")
    @classmethod
    def reference(cls, value):
        import unicodedata
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("Decision references cannot contain control characters.")
        return _identifier(value)

    @model_validator(mode="after")
    def dates(self):
        if self.decisionDate < self.noticeReceiptDate or (self.noticeReceiptDate-self.admissionDate).days <= 5:
            raise ValueError("An approval must identify a late accepted NOA and a decision on or after receipt.")
        return self


class HhaLateNoaContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exceptionStatus: Literal["NoException", "ApprovedException"]
    approval: HhaNoaApproval | None = None

    @model_validator(mode="after")
    def explicit_approval(self):
        if (self.exceptionStatus == "ApprovedException") != (self.approval is not None):
            raise ValueError("ApprovedException requires a submitted MAC approval; NoException must omit approval.")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_approval(self, handler):
        result = handler(self)
        if self.approval is None:
            result.pop("approval", None)
        return result


class HhaLateNoaVisitPenalty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visitId: StrictStr = Field(min_length=1, max_length=128)
    grossPayment: Money
    penaltyAmount: Money
    netPayment: Money
    grossBasePayment: Money
    grossAddOnPayment: Money
    basePenaltyAmount: Money
    addOnPenaltyAmount: Money

    @model_validator(mode="after")
    def reconcile(self):
        if (self.grossPayment - self.penaltyAmount != self.netPayment
                or self.grossBasePayment + self.grossAddOnPayment != self.grossPayment
                or self.basePenaltyAmount + self.addOnPenaltyAmount != self.penaltyAmount):
            raise ValueError("NOA visit exclusion must reconcile to gross and net payment.")
        return self


class HhaLateNoaPayment(HhaLateNoaContext):
    admissionDate: date
    noticeReceiptDate: date
    periodFromDate: date
    periodThroughDate: date
    noncoveredDays: StrictInt = Field(ge=0, le=30)
    grossPayment: Money
    penaltyAmount: Money
    netPayment: Money
    cmsPayment: Money
    cmsPenaltyAmount: Money
    adjustmentMethod: Literal["CmsPeriodPenalty", "SubsequentPeriodPenalty", "LupaVisitExclusion", "ApprovedExceptionWaiver"]
    providerLiability: StrictBool
    beneficiaryLiabilityAmount: Annotated[Decimal, Field(ge=0, le=0)]
    nonPayment: StrictBool
    visitPenalties: list[HhaLateNoaVisitPenalty] | None = None

    _dates = field_validator("admissionDate", "noticeReceiptDate", "periodFromDate", "periodThroughDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def reconcile(self):
        if (self.grossPayment-self.penaltyAmount != self.netPayment
                or self.nonPayment != (self.netPayment == 0) or self.providerLiability != (self.exceptionStatus == "NoException")
                or (self.noticeReceiptDate-self.admissionDate).days <= 5
                or self.periodFromDate < self.admissionDate or self.periodThroughDate < self.periodFromDate
                or self.noncoveredDays != (0 if self.approval else max(0, min(30, (self.noticeReceiptDate-self.periodFromDate).days)))):
            raise ValueError("NOA penalty and nonpayment must reconcile to gross and net payment.")
        if self.approval:
            if (self.adjustmentMethod != "ApprovedExceptionWaiver" or self.penaltyAmount != 0 or self.cmsPenaltyAmount != 0
                    or self.approval.admissionDate != self.admissionDate or self.approval.noticeReceiptDate != self.noticeReceiptDate):
                raise ValueError("Approved waiver requires matching dates, zero penalty and explicit waiver evidence.")
        elif ((self.adjustmentMethod == "LupaVisitExclusion") != (self.visitPenalties is not None)
                or (self.adjustmentMethod != "LupaVisitExclusion"
                    and (self.adjustmentMethod == "SubsequentPeriodPenalty") != (0 < self.noncoveredDays <= 5))):
            raise ValueError("NOA adjustment method must match its visit or period evidence.")
        if self.visitPenalties is not None and (len({v.visitId for v in self.visitPenalties}) != len(self.visitPenalties)
                or sum(v.grossPayment for v in self.visitPenalties) != self.grossPayment
                or sum(v.penaltyAmount for v in self.visitPenalties) != self.penaltyAmount
                or sum(v.netPayment for v in self.visitPenalties) != self.netPayment):
            raise ValueError("NOA visit exclusions must reconcile to the aggregate penalty.")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_visits(self, handler):
        result = handler(self)
        if self.visitPenalties is None:
            result.pop("visitPenalties", None)
        if self.approval is None:
            result.pop("approval", None)
        return result


class HhaPartialPeriodContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eventType: Literal["TransferToAnotherHha", "DischargeReadmissionSameHha"]
    patientStatus: Literal["06"]
    nextCareStartDate: date
    isCommonOwnershipTransfer: StrictBool

    _next_date = field_validator("nextCareStartDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def supported_event(self):
        if self.isCommonOwnershipTransfer:
            raise ValueError("Common-ownership transfers are outside partial-period support.")
        return self


class HhaPartialPeriodPayment(HhaPartialPeriodContext):
    firstBillableDate: date
    lastBillableDate: date
    coveredDays: StrictInt = Field(ge=1, le=29)
    periodDays: Literal[30]
    paymentRatio: Annotated[Decimal, Field(ge=Decimal("0.03"), le=Decimal("0.97"))]
    adjustmentApplied: StrictBool

    _billable_dates = field_validator("firstBillableDate", "lastBillableDate", mode="before")(_calendar_date)


class HhaShortenedPeriodContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eventType: Literal["Discharge", "Death"]
    patientStatus: Literal["01", "20"]
    eventDate: date
    noSubsequentCareWithin30Days: StrictBool

    _event_date = field_validator("eventDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def supported_event(self):
        if (self.patientStatus != ("01" if self.eventType == "Discharge" else "20")
                or not self.noSubsequentCareWithin30Days):
            raise ValueError("Shortened nonprorated periods require matching discharge/death status and no subsequent care within the original 30-day period.")
        return self


class HhaShortenedPeriodPayment(HhaShortenedPeriodContext):
    firstBillableDate: date
    lastBillableDate: date
    statementDays: StrictInt = Field(ge=1, le=29)
    periodDays: Literal[30]
    paymentRatio: Annotated[Decimal, Field(ge=1, le=1, allow_inf_nan=False)]
    adjustmentApplied: StrictBool

    _billable_dates = field_validator("firstBillableDate", "lastBillableDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def unprorated_statement(self):
        start = self.eventDate - timedelta(days=self.statementDays - 1)
        if (not start <= self.firstBillableDate <= self.lastBillableDate <= self.eventDate
                or self.adjustmentApplied):
            raise ValueError("Shortened statement evidence must preserve the original 30-day payment without proration.")
        return self


class HhaLupaVisitPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visitId: StrictStr = Field(min_length=1, max_length=128)
    discipline: Literal["PT", "OT", "SLP", "SN", "MSW", "HHA"]
    revenueCode: Literal["0420", "0430", "0440", "0550", "0560", "0570"]
    serviceDate: date
    basePayment: Money
    addOnPayment: Money
    totalPayment: Money

    _visit_identity = field_validator("visitId")(_identifier)
    _visit_date = field_validator("serviceDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def reconcile(self):
        if self.basePayment + self.addOnPayment != self.totalPayment:
            raise ValueError("LUPA visit payment must reconcile to its components.")
        return self


class HhaLupaPayment(HhaLupaContext):
    visitPayments: list[HhaLupaVisitPayment] = Field(min_length=1, max_length=1000)


class HhaA9272Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    benefitYear: StrictInt
    asOfDate: date
    remainingPartBDeductible: Annotated[Decimal, Field(ge=0, decimal_places=2, allow_inf_nan=False)]
    basis: Literal["SubmittedRemainingPartBDeductible"]
    excludesCurrentClaim: StrictBool
    medicarePrimary: StrictBool
    _asof = field_validator("asOfDate", mode="before")(_calendar_date)

    @model_validator(mode="after")
    def explicit_benefit_basis(self):
        release = release_for_through(self.asOfDate)
        if (release is None or self.asOfDate.year != self.benefitYear
                or self.remainingPartBDeductible > release.part_b_deductible
                or not self.excludesCurrentClaim or not self.medicarePrimary):
            raise ValueError("Device benefits require an approved benefit year and a primary Medicare snapshot excluding this claim.")
        return self


class HhaA9272Line(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claimLineNumber: StrictInt = Field(ge=1)
    serviceDate: date
    revenueCode: Literal["0270", "0271", "0272", "0273", "0279"]
    hcpcsCode: Literal["A9272"]
    units: StrictInt = Field(ge=1, le=1000)
    billedCharge: Annotated[Decimal, Field(gt=0, le=Decimal("9999999999999999.99"), decimal_places=2, allow_inf_nan=False)]
    _service = field_validator("serviceDate", mode="before")(_calendar_date)


class HhaA9272LinePayment(HhaA9272Line):
    unitRate: Money
    allowedAmount: Money
    appliedDeductible: Money
    coinsuranceAmount: Money
    beneficiaryLiabilityAmount: Money
    medicarePaymentBeforeSequestration: Money


class HhaA9272Payment(HhaA9272Context):
    ppsPayment: Money
    rateEvidence: dict[str, Any]
    linePayments: list[HhaA9272LinePayment] = Field(min_length=1, max_length=1000)
    totalDeviceAllowance: Money
    totalAppliedDeductible: Money
    totalCoinsurance: Money
    totalBeneficiaryLiability: Money
    totalDeviceMedicarePaymentBeforeSequestration: Money
    totalMedicarePaymentBeforeSequestration: Money
    remainingPartBDeductibleAfterClaim: Money
    allowanceBasis: Literal["NationalDeviceFeeBeforeCostSharing"]

    @model_validator(mode="after")
    def reconcile_device_amounts(self):
        sums = {key: sum(getattr(row,key) for row in self.linePayments) for key in
            ("allowedAmount", "appliedDeductible", "coinsuranceAmount", "beneficiaryLiabilityAmount", "medicarePaymentBeforeSequestration")}
        if (self.totalDeviceAllowance != sums["allowedAmount"] or self.totalAppliedDeductible != sums["appliedDeductible"]
                or self.totalCoinsurance != sums["coinsuranceAmount"] or self.totalBeneficiaryLiability != sums["beneficiaryLiabilityAmount"]
                or self.totalDeviceMedicarePaymentBeforeSequestration != sums["medicarePaymentBeforeSequestration"]
                or self.totalDeviceAllowance != self.totalBeneficiaryLiability+self.totalDeviceMedicarePaymentBeforeSequestration
                or self.totalMedicarePaymentBeforeSequestration != self.ppsPayment+self.totalDeviceMedicarePaymentBeforeSequestration
                or self.remainingPartBDeductibleAfterClaim != self.remainingPartBDeductible-self.totalAppliedDeductible):
            raise ValueError("Device allowances, beneficiary liability and Medicare payment must reconcile.")
        return self


class HhaRequest(BaseModel):
    """Original 329 and replacement 327 periods with supplied HIPPS.

    Geography values attest to submitted CMS billing values. The API validates
    their format and available references; it does not geocode an address or
    establish that a supplied county/CBSA pair is the patient's correct location.
    """

    model_config = ConfigDict(extra="forbid")

    claimId: StrictStr = Field(min_length=1, max_length=128)
    providerCcn: StrictStr = Field(pattern=r"^[A-Z0-9]{6,13}$")
    billType: Literal["329", "327"]
    hippsCode: StrictStr = Field(pattern=r"^[A-Z0-9]{5}$")
    admissionDate: date
    fromDate: date
    throughDate: date
    noticeReceiptDate: date
    periodType: Literal["Full30Day", "Partial30Day", "Shortened30Day"]
    serviceCountyFips: StrictStr = Field(pattern=r"^[0-9]{5}$")
    cbsaCode: StrictStr = Field(pattern=r"^(?:[0-9]{2}|[0-9]{5})$")
    geographyBasis: Literal["SubmittedCmsValueCodes"]
    visits: list[HhaVisit] = Field(min_length=1, max_length=1000)
    lupa: HhaLupaContext | None = None
    partialPeriod: HhaPartialPeriodContext | None = None
    lateNoa: HhaLateNoaContext | None = None
    outlier: HhaOutlierContext | None = None
    shortenedPeriod: HhaShortenedPeriodContext | None = None
    a9272: HhaA9272Context | None = None
    a9272Lines: list[HhaA9272Line] | None = Field(default=None, min_length=1, max_length=1000)

    _claim_identity = field_validator("claimId")(_identifier)
    _dates = field_validator(
        "admissionDate", "fromDate", "throughDate", "noticeReceiptDate", mode="before"
    )(_calendar_date)

    @field_validator("billType", mode="before")
    @classmethod
    def supported_bill(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in ("329", "0329", "327", "0327"):
            raise ValueError("Only HH original 0329 and adjustment 0327 claims are supported.")
        return value.removeprefix("0")

    @property
    def release(self) -> HhaRelease:
        return require_statement_release(self.fromDate, self.throughDate)

    @property
    def is_crossover(self) -> bool:
        return self.fromDate.year != self.throughDate.year

    @model_validator(mode="after")
    def period_and_visits(self):
        release = self.release
        if self.is_crossover and (self.billType != "329"
                or any(context is not None for context in (self.lateNoa, self.outlier, self.a9272, self.a9272Lines))):
            raise ValueError("Cross-year HH pricing requires an original 0329 with timely NOA and no outlier or A9272 context.")
        if bool(self.a9272Lines) != (self.a9272 is not None):
            raise ValueError("A9272 lines require matching explicit beneficiary context; context without devices is invalid.")
        if self.a9272Lines:
            if (len({row.claimLineNumber for row in self.a9272Lines}) != len(self.a9272Lines)
                    or any(not self.fromDate <= row.serviceDate <= self.throughDate for row in self.a9272Lines)
                    or self.a9272.benefitYear != self.fromDate.year or self.a9272.benefitYear != release.payment_year
                    or self.a9272.asOfDate < self.throughDate):
                raise ValueError("Device lines require unique bindings, dates within the statement and a benefit snapshot as of Through or later.")
        statement_days = (self.throughDate - self.fromDate).days + 1
        if self.periodType == "Full30Day":
            if statement_days != 30 or self.partialPeriod is not None or self.shortenedPeriod is not None:
                raise ValueError("Full periods require 30 inclusive days and no partial-period context.")
        elif self.periodType == "Shortened30Day":
            if (self.shortenedPeriod is None or self.partialPeriod is not None or not 1 <= statement_days <= 29
                    or self.shortenedPeriod.eventDate != self.throughDate):
                raise ValueError("Shortened periods require explicit discharge/death facts, an event matching Through, 1-29 statement days and no partial-period adjustment.")
        elif (self.partialPeriod is None or self.shortenedPeriod is not None or not 1 <= statement_days <= 29
              or not self.throughDate < self.partialPeriod.nextCareStartDate <= self.fromDate + timedelta(days=29)):
            raise ValueError("A partial period requires explicit context, 1-29 statement days and subsequent care within the original period.")
        if self.outlier is not None and (self.outlier.calendarYear != release.payment_year
                                         or self.outlier.asOfDate < self.throughDate):
            raise ValueError("Outlier history must be as of the statement Through date or later.")
        if self.admissionDate > self.fromDate:
            raise ValueError("Admission/start of care cannot follow the statement From date.")
        if self.lupa is not None and self.lupa.isFirstOrOnlyPeriod and self.admissionDate != self.fromDate:
            raise ValueError("A first or only LUPA period must begin on the admission/start-of-care date.")
        notice_days = (self.noticeReceiptDate - self.admissionDate).days
        if notice_days < 0:
            raise ValueError("An accepted NOA date on or after admission is required.")
        if (notice_days > 5) != (self.lateNoa is not None):
            raise ValueError("Late NOA requires explicit exception facts; timely NOA must omit late context.")
        if self.lateNoa and self.lateNoa.approval:
            approval = self.lateNoa.approval
            if (approval.providerCcn != self.providerCcn or approval.admissionDate != self.admissionDate
                    or approval.noticeReceiptDate != self.noticeReceiptDate):
                raise ValueError("The MAC approval must bind this provider, admission and accepted NOA.")
        identities = [visit.visitId for visit in self.visits]
        if len(set(identities)) != len(identities):
            raise ValueError("Visit identities must be unique within the claim.")
        daily_units: dict[date, int] = defaultdict(int)
        for visit in self.visits:
            if not self.fromDate <= visit.serviceDate <= self.throughDate:
                raise ValueError("Every visit must be within the statement period.")
            daily_units[visit.serviceDate] += visit.outlierUnits
        if any(units > 32 for units in daily_units.values()):
            raise ValueError("Eligible outlier units may not exceed 32 across disciplines per day.")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_shortened_period(self, handler):
        result = handler(self)
        if self.shortenedPeriod is None:
            result.pop("shortenedPeriod", None)
        return result


class HhaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimId: StrictStr = Field(min_length=1, max_length=128)
    status: Literal["Success", "UnableToPrice"]
    reasonCode: StrictStr = Field(min_length=1)
    reason: StrictStr = Field(min_length=1)
    pricingMethod: Literal["HH_PPS"] = "HH_PPS"
    totalPayment: Money | None = None
    pricerRelease: StrictStr = CY_2026.pricer_release
    calculationVersion: StrictStr | None = None
    providerData: dict[str, Any] = Field(default_factory=dict)
    providerEvidence: dict[str, Any] = Field(default_factory=dict)
    geographyEvidence: dict[str, Any] = Field(default_factory=dict)
    cms: dict[str, Any] = Field(default_factory=dict)
    lupa: HhaLupaPayment | None = None
    partialPeriod: HhaPartialPeriodPayment | None = None
    lateNoa: HhaLateNoaPayment | None = None
    outlier: HhaOutlierPayment | None = None
    shortenedPeriod: HhaShortenedPeriodPayment | None = None
    a9272: HhaA9272Payment | None = None
    limitations: list[str] = Field(default_factory=lambda: [
        "Payment estimate only; no eligibility or coverage determination."
    ])

    @model_validator(mode="after")
    def complete_outcome(self):
        pps_total = self.totalPayment if self.a9272 is None else self.a9272.ppsPayment
        if self.a9272 is not None and (self.status != "Success" or self.totalPayment != pps_total+self.a9272.totalDeviceAllowance):
            raise ValueError("Combined allowance must reconcile to PPS and separately evidenced device allowance.")
        if self.status == "UnableToPrice":
            if self.totalPayment is not None or self.lupa is not None or self.partialPeriod is not None or self.lateNoa is not None or self.outlier is not None or self.shortenedPeriod is not None:
                raise ValueError("An unpriced outcome must not contain a total payment.")
        elif (
            self.totalPayment is None
            or not self.calculationVersion
            or not self.providerData
            or not self.providerEvidence
            or not self.geographyEvidence
            or not self.cms
        ):
            raise ValueError("A successful payment requires calculation and reference evidence.")
        if self.reasonCode == "HHA_LUPA_ESTIMATED":
            if (self.lupa is None
                    or len({visit.visitId for visit in self.lupa.visitPayments}) != len(self.lupa.visitPayments)
                    or sum(visit.totalPayment for visit in self.lupa.visitPayments) != pps_total):
                raise ValueError("A LUPA estimate requires complete, unique, reconciled visit payments.")
        if self.reasonCode == "HHA_PARTIAL_PERIOD_ESTIMATED":
            if self.partialPeriod is None or not self.partialPeriod.adjustmentApplied or self.lupa is not None:
                raise ValueError("A partial-period estimate requires explicit applied adjustment evidence.")
        if self.reasonCode == "HHA_SHORTENED_PERIOD_ESTIMATED":
            if self.shortenedPeriod is None or self.lupa is not None or self.partialPeriod is not None:
                raise ValueError("A shortened-period estimate requires nonprorated terminal-event evidence.")
        if self.shortenedPeriod is not None and (self.partialPeriod is not None
                or self.reasonCode != ("HHA_LUPA_ESTIMATED" if self.lupa is not None else "HHA_SHORTENED_PERIOD_ESTIMATED")):
            raise ValueError("Shortened-period evidence cannot be mixed with a partial-period adjustment or another pricing reason.")
        if self.partialPeriod is not None:
            detail = self.partialPeriod
            if (detail.coveredDays != (detail.lastBillableDate - detail.firstBillableDate).days + 1
                    or detail.paymentRatio != (Decimal(detail.coveredDays) / 30).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
                    or detail.adjustmentApplied == (self.lupa is not None)):
                raise ValueError("Partial-period evidence must reconcile to dates, days and payment method.")
        if self.lateNoa is not None and self.lateNoa.netPayment != pps_total:
            raise ValueError("Late NOA net payment must match the accepted estimate.")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_lupa(self, handler):
        # Keep existing required null totals/calculation fields on failures.
        # Only the new optional field is omitted for historical ordinary shape.
        result = handler(self)
        for field in ("lupa", "partialPeriod", "lateNoa", "outlier", "shortenedPeriod", "a9272"):
            if getattr(self, field) is None:
                result.pop(field, None)
        return result
