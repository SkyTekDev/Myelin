"""Runs the CMS Hospice Pricer once per level-of-care line.

The jar mirrors FISS's fixed four-slot record: its rules read only the first billing group for each
revenue code, and it returns four amounts in the order 0651, 0652, 0655, 0656 whatever revenue codes
it labels them with. Calling it once per line reproduces the manual's rules (the high/low day count
runs from the admission date to each line's date, the 8-hour minimum applies per CHC day). Requests
and responses cross into Java as JSON through the jar's own Jackson mapper, so amounts stay exact
decimals. Myelin's HospiceClient merges lines by revenue code and is not used here.

The end-of-life service intensity add-on goes with exactly one call: the jar prices it from the seven daily unit totals
only inside its routine home care rules, so the totals ride on the latest routine home care line's call. That call
returns 74 or 77, and its total payment is the line's amount plus the add-on.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import jpype
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bridge.hospice_models import (
    HospiceClaimRequest,
    HospiceEndOfLifeAddOn,
    HospiceEndOfLifeDay,
    HospiceLineResult,
    HospiceProviderRecord,
    HospiceResponse,
)
from bridge.hospice_release import (
    CALCULATION_VERSIONS,
    CHC_MINIMUM_UNITS,
    CONTINUOUS_HOME_CARE,
    END_OF_LIFE_DAYS,
    END_OF_LIFE_MAXIMUM_DAILY_UNITS,
    END_OF_LIFE_RETURN_CODES,
    FIRST_SERVICE_DATE,
    HOSPICE_PROVIDER_TYPE,
    INVALID_CBSA_RETURN_CODE,
    INVALID_UNITS_RETURN_CODE,
    JAR_FILENAME,
    JAR_SHA256,
    LAST_SERVICE_DATE,
    LEVELS_OF_CARE,
    PAYMENT_SLOTS,
    PRICED_RETURN_CODES,
    PRICER_RELEASE,
    ROUTINE_HOME_CARE,
    SUPPORTED_FISCAL_YEARS,
    UNSUPPORTED_DATES_REASON,
    fiscal_year,
    fiscal_year_start,
)
from myelin.pricers.opsf import OPSF
from myelin.pricers.url_loader import UrlLoader

PACKAGE = "gov.cms.fiss.pricers.hospice"
# Termination dates the provider file uses for "not terminated".
NOT_TERMINATED = (None, 0, 19000101, 99991231)


class HospiceProviderError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HospiceQuality:
    reduction: bool
    source: str
    note: str
    provider: HospiceProviderRecord | None


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _date(value: int | None) -> date | None:
    if value in NOT_TERMINATED:
        return None
    text = str(value)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _int(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def _type(row) -> str:
    return (row.provider_type or "").strip()


def _indicator(row) -> str:
    return (row.hospital_quality_indicator or "").strip()


def _latest(session: Session, ccn: str, as_of: int) -> list:
    """Every record on the CCN's latest effective date on or before as_of (normally one)."""
    rows = session.scalars(select(OPSF).where(OPSF.provider_ccn == ccn, OPSF.effective_date <= as_of)
                           .order_by(OPSF.effective_date.desc())).all()
    return [row for row in rows if row.effective_date == rows[0].effective_date] if rows else []


def _record(row, claim_npi: str | None) -> HospiceProviderRecord:
    npi = (row.national_provider_identifier or "").strip() or None
    return HospiceProviderRecord(ccn=row.provider_ccn, npi=npi, effectiveDate=_date(row.effective_date),
                                 terminationDate=_date(row.termination_date), providerType=_type(row),
                                 qualityIndicator=_indicator(row),
                                 npiMatchesClaim=None if claim_npi is None or npi is None else npi == claim_npi)


def provider_file_export_date(engine) -> date | None:
    """The provider file's export date (every row of one CMS export carries it), or None without one."""
    with Session(engine) as session:
        return _date(session.scalar(select(func.max(OPSF.export_date))))


def hospice_records_through(engine) -> date | None:
    """The latest effective date of any hospice (provider type 35) record in the provider file, up to its export date.

    CMS's export can hold records dated after the export, such as a provider's scheduled changes. They say nothing
    about how current the file is, so they would make a fiscal year look loaded before its quality results are; only
    records effective on or before the export date count. Without an export date every record counts.
    """
    with Session(engine) as session:
        exported = session.scalar(select(func.max(OPSF.export_date)))
        query = select(func.max(OPSF.effective_date)).where(func.trim(OPSF.provider_type) == HOSPICE_PROVIDER_TYPE)
        if exported:
            query = query.where(OPSF.effective_date <= exported)
        return _date(session.scalar(query))


def resolve_quality(session: Session, payload: HospiceClaimRequest, records_through: date | None) -> HospiceQuality:
    """The hospice quality reporting reduction from the provider file record in effect on the claim's through date.

    Medicare reads it from the provider file (blank = no reduction, 1 = reduction). A provider file without
    hospice records for the claim's fiscal year cannot hold that year's results, so full rates apply then.
    Raises HospiceProviderError when the record shows the provider is not a hospice or was terminated.
    """
    as_of = _int(payload.throughDate)
    rows, ambiguous = [], False
    if payload.providerNumber:
        rows = _latest(session, payload.providerNumber, as_of)
        ambiguous = len({(_type(row), _indicator(row)) for row in rows}) > 1
    elif payload.npi:
        ccns = session.scalars(select(OPSF.provider_ccn).where(OPSF.national_provider_identifier == payload.npi,
                                                               OPSF.effective_date <= as_of).distinct()).all()
        candidates = [latest[0] for latest in (_latest(session, ccn, as_of) for ccn in ccns)
                      if latest and (latest[0].national_provider_identifier or "").strip() == payload.npi
                      and _type(latest[0]) == HOSPICE_PROVIDER_TYPE]
        active = [row for row in candidates if row.termination_date in NOT_TERMINATED or row.termination_date > _int(payload.fromDate)]
        rows = active if len(active) == 1 else candidates if len(candidates) == 1 else []
        ambiguous = len(active) > 1 or (not active and len(candidates) > 1)

    row = rows[0] if rows and not ambiguous else None
    provider = _record(row, payload.npi) if row is not None else None
    if row is not None:
        if _type(row) != HOSPICE_PROVIDER_TYPE:
            raise HospiceProviderError("HOSPICE_PROVIDER_NOT_HOSPICE",
                f"Provider {row.provider_ccn} is not a hospice in the provider file: its record effective "
                f"{provider.effectiveDate.isoformat()} has provider type {_type(row) or 'blank'}, not 35.")
        if row.termination_date not in NOT_TERMINATED and row.termination_date <= _int(payload.fromDate):
            raise HospiceProviderError("HOSPICE_PROVIDER_TERMINATED",
                f"Hospice {row.provider_ccn} is terminated in the provider file as of {provider.terminationDate.isoformat()}, "
                "before the claim's dates of service.")

    year = fiscal_year(payload.throughDate)
    if records_through is None or records_through < fiscal_year_start(year):
        loaded = records_through.isoformat() if records_through else "no date"
        return HospiceQuality(False, "FullRatesProviderFileBeforeFiscalYear",
            f"The loaded provider file holds hospice records effective through {loaded}, before FY {year}, so it cannot "
            f"show FY {year} quality reporting results; full rates were applied.", provider)
    if ambiguous:
        identifier = f"CCN {payload.providerNumber}" if payload.providerNumber else f"NPI {payload.npi}"
        return HospiceQuality(False, "FullRatesProviderAmbiguous",
            f"The provider file has conflicting hospice records for {identifier}; full rates were applied.", None)
    if provider is None:
        identifier = f"CCN {payload.providerNumber}" if payload.providerNumber else f"NPI {payload.npi}" if payload.npi else "the claim (no CCN or NPI)"
        return HospiceQuality(False, "FullRatesProviderNotFound",
            f"The provider file has no hospice record for {identifier} in effect on the claim's dates; full rates were applied.", None)
    reduction = provider.qualityIndicator == "1"
    return HospiceQuality(reduction, "ProviderFile",
        f"Provider file record for hospice {provider.ccn} effective {provider.effectiveDate.isoformat()}: "
        + ("quality indicator 1, so the rates for hospices that did not submit the required quality data apply."
           if reduction else "no quality reporting reduction, so full rates apply."), provider)


class HospiceProcessor:
    def __init__(self, jar_root, engine):
        jar = Path(os.getenv("HOSPICE_JAR_PATH", str(Path(jar_root) / "pricers" / JAR_FILENAME))).resolve()
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        if digest != JAR_SHA256:
            raise RuntimeError("Hospice Pricer release has not been reconciled.")
        if not jpype.isJVMStarted():
            raise RuntimeError("Start the Myelin JVM before the hospice processor.")

        self.engine = engine
        self.records_through = hospice_records_through(engine)
        self.export_date = provider_file_export_date(engine)
        self.url_loader = UrlLoader()
        self.url_loader.load_urls([jar.as_uri()])
        loader = self.url_loader.class_loader

        def java(name):
            return jpype.JClass(name, loader=loader)

        config = java(PACKAGE + ".HospicePricerConfiguration")()
        config.setCsvIngestionConfiguration(java("gov.cms.fiss.pricers.common.csv.CsvIngestionConfiguration")())
        years = java("java.util.ArrayList")()
        for year in SUPPORTED_FISCAL_YEARS:
            years.add(java("java.lang.Integer")(year))
        config.setSupportedYears(years)
        java(PACKAGE + ".core.tables.DataTables").loadDataTables(config)
        self.dispatch = java(PACKAGE + ".core.HospicePricerDispatch")(config)
        self.request_class = java(PACKAGE + ".api.v2.HospiceClaimPricingRequest")
        self.mapper = java("com.fasterxml.jackson.databind.ObjectMapper")()
        self.mapper.registerModule(java("com.fasterxml.jackson.datatype.jsr310.JavaTimeModule")())

    def calculate(self, claim_data: dict) -> dict:
        request = self.mapper.readValue(json.dumps({"claimData": claim_data}, default=str), self.request_class)
        response = self.dispatch.process(request)
        return json.loads(str(self.mapper.writeValueAsString(response)), parse_float=Decimal)

    @staticmethod
    def claim_data(payload: HospiceClaimRequest, line, quality: HospiceQuality, end_of_life_units: list[int] | None = None) -> dict:
        ccn = payload.providerNumber or (quality.provider.ccn if quality.provider else "")
        data = {
            "providerCcn": ccn,
            "admissionDate": payload.admissionDate.isoformat(),
            # FISS sends the statement's from date; the jar picks the fiscal year from it.
            "serviceFromDate": payload.fromDate.isoformat(),
            "priorBenefitDayUnits": payload.priorBenefitDays,
            "reportingQualityData": "1" if quality.reduction else "0",
            "patientCbsa": payload.patientCbsa or "",
            "providerCbsa": payload.providerCbsa or "",
            "billingGroups": [{"revenueCode": line.revenueCode, "hcpcsCode": line.hcpcs,
                               "dateOfService": line.dateOfService.isoformat(), "units": line.units}],
        }
        if end_of_life_units is not None:
            # FISS's EOL day 1-7 add-on units: day 1 is the date of death.
            data["endOfLifeAddOnDaysUnits"] = list(end_of_life_units)
        return data

    def process(self, payload: HospiceClaimRequest, lock, bridge_version: str) -> HospiceResponse:
        base = dict(claimId=str(payload.claimId), bridgeVersion=bridge_version, pricerRelease=PRICER_RELEASE, jarSha256=JAR_SHA256,
                    admissionDate=payload.admissionDate, fromDate=payload.fromDate, throughDate=payload.throughDate,
                    patientCbsa=payload.patientCbsa, providerCbsa=payload.providerCbsa, priorBenefitDays=payload.priorBenefitDays,
                    providerFileHospiceRecordsThrough=self.records_through, dateOfDeath=payload.dateOfDeath,
                    endOfLifeAddOnUnits=payload.endOfLifeAddOnUnits)

        def unable(code: str, reason: str, **extra) -> HospiceResponse:
            return HospiceResponse(**{**base, **extra}, status="UnableToPrice", reasonCode=code, reason=reason)

        if payload.fromDate < FIRST_SERVICE_DATE or payload.throughDate > LAST_SERVICE_DATE:
            return unable("HOSPICE_DATES_NOT_SUPPORTED", UNSUPPORTED_DATES_REASON)

        year = fiscal_year(payload.fromDate)
        try:
            with Session(self.engine) as session:
                quality = resolve_quality(session, payload, self.records_through)
        except HospiceProviderError as error:
            return unable(error.code, str(error), fiscalYear=year)

        context = dict(fiscalYear=year, qualityReduction=quality.reduction, qualitySource=quality.source,
                       qualityNote=quality.note, provider=quality.provider)
        units = payload.endOfLifeAddOnUnits
        # The request model guarantees a routine home care line on every day with add-on units.
        attached = max((line for line in payload.claimLines if line.revenueCode == ROUTINE_HOME_CARE),
                       key=lambda line: (line.dateOfService, line.lineNumber)) if units else None
        with lock:
            outputs = [self.calculate(self.claim_data(payload, line, quality, units if line is attached else None))
                       for line in payload.claimLines]

        lines, wage_indexes, add_on = [], set(), None
        for line, cms in zip(payload.claimLines, outputs):
            code = (cms.get("returnCodeData") or {}).get("code")
            description = (cms.get("returnCodeData") or {}).get("description")
            if code == INVALID_CBSA_RETURN_CODE:
                return unable("HOSPICE_CBSA_INVALID",
                    f"CMS's FY {year} hospice wage index has no entry for a submitted CBSA "
                    f"(value code 61: {payload.patientCbsa or 'none'}; value code G8: {payload.providerCbsa or 'none'}).", **context)
            if code == INVALID_UNITS_RETURN_CODE:
                return unable("HOSPICE_UNITS_INVALID", f"The CMS Hospice Pricer refused line {line.lineNumber}'s {line.units} units.", **context)
            if code not in PRICED_RETURN_CODES | END_OF_LIFE_RETURN_CODES:
                return unable("HOSPICE_PRICER_LINE_REJECTED",
                    f"The CMS Hospice Pricer returned code {code} ({description}) for line {line.lineNumber}.", **context)
            if (code in END_OF_LIFE_RETURN_CODES) != (line is attached):
                raise RuntimeError("The CMS Hospice Pricer's end-of-life add-on return code does not match the call that carried the units.")
            if cms.get("calculationVersion") != CALCULATION_VERSIONS[year]:
                return unable("HOSPICE_PRICER_VERSION_MISMATCH",
                    f"The CMS Hospice Pricer applied calculation version {cms.get('calculationVersion')}, not "
                    f"{CALCULATION_VERSIONS[year]} for FY {year}.", **context)

            payment = cms.get("paymentData") or {}
            add_on_total = Decimal("0")
            if line is attached:
                add_on = self.end_of_life_add_on(payload, line, payment.get("endOfLifeAddOnDaysPayments") or [])
                add_on_total = add_on.totalPayment
            amounts = [_decimal(item.get("amount")) for item in payment.get("billPayments") or []]
            slot = PAYMENT_SLOTS.index(line.revenueCode)
            if len(amounts) != len(PAYMENT_SLOTS) or any(amount is None for amount in amounts) \
                    or any(amount != 0 for index, amount in enumerate(amounts) if index != slot) \
                    or _decimal(payment.get("totalPayment")) != amounts[slot] + add_on_total:
                raise RuntimeError("The CMS Hospice Pricer returned amounts outside the line's level-of-care slot.")
            high, low = int(payment.get("highRoutineHomeCareDays") or 0), int(payment.get("lowRoutineHomeCareDays") or 0)
            if (line.revenueCode == ROUTINE_HOME_CARE and high + low != line.units) or (line.revenueCode != ROUTINE_HOME_CARE and high + low):
                raise RuntimeError("The CMS Hospice Pricer's high and low routine home care days do not match the line.")
            wage_indexes.add((_decimal(payment.get("patientWageIndex")), _decimal(payment.get("providerWageIndex"))))
            lines.append(HospiceLineResult(
                claimLineId=line.claimLineId, lineNumber=line.lineNumber, revenueCode=line.revenueCode, hcpcs=line.hcpcs,
                dateOfService=line.dateOfService, units=line.units, levelOfCare=LEVELS_OF_CARE[line.revenueCode],
                electionDay=(line.dateOfService - payload.admissionDate).days + payload.priorBenefitDays + 1,
                payment=amounts[slot], pricerReturnCode=code, pricerReturnDescription=description,
                highRoutineHomeCareDays=high, lowRoutineHomeCareDays=low,
                paidAsRoutineHomeCareDay=line.revenueCode == CONTINUOUS_HOME_CARE and line.units < CHC_MINIMUM_UNITS))

        if len(wage_indexes) != 1:
            raise RuntimeError("The CMS Hospice Pricer reported different wage indexes for one claim.")
        patient_index, provider_index = wage_indexes.pop()
        return HospiceResponse(
            **base, **context, status="Success", reason=f"CMS Hospice Pricer processing completed for {len(lines)} level-of-care line(s).",
            calculationVersion=CALCULATION_VERSIONS[year],
            patientWageIndex=patient_index if payload.patientCbsa else None,
            providerWageIndex=provider_index if payload.providerCbsa else None,
            totalPayment=sum((line.payment for line in lines), Decimal("0")) + (add_on.totalPayment if add_on else Decimal("0")),
            highRoutineHomeCareDays=sum(line.highRoutineHomeCareDays for line in lines),
            lowRoutineHomeCareDays=sum(line.lowRoutineHomeCareDays for line in lines),
            endOfLifeAddOn=add_on,
            lines=lines)

    @staticmethod
    def end_of_life_add_on(payload: HospiceClaimRequest, line, payments: list) -> HospiceEndOfLifeAddOn:
        """The jar's seven daily add-on payments for the units sent, checked against them day by day."""
        units = payload.endOfLifeAddOnUnits
        amounts = [_decimal(item.get("payment")) for item in payments]
        if len(payments) != END_OF_LIFE_DAYS or [item.get("index") for item in payments] != list(range(1, END_OF_LIFE_DAYS + 1)) \
                or any(amount is None or amount < 0 for amount in amounts) \
                or any((amount == 0) != (count == 0) for amount, count in zip(amounts, units)):
            raise RuntimeError("The CMS Hospice Pricer's end-of-life add-on does not match the units sent.")
        days = [HospiceEndOfLifeDay(day=index + 1, dateOfService=payload.dateOfDeath - timedelta(days=index), units=count,
                                    unitsPaid=min(count, END_OF_LIFE_MAXIMUM_DAILY_UNITS), payment=amount)
                for index, (count, amount) in enumerate(zip(units, amounts))]
        return HospiceEndOfLifeAddOn(dateOfDeath=payload.dateOfDeath, attachedClaimLineId=line.claimLineId,
                                     attachedLineNumber=line.lineNumber, days=days,
                                     totalPayment=sum(amounts, Decimal("0")))
