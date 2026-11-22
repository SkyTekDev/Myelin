"""Hospice request contract, release pinning, provider-file quality lookup and route gating; no JVM or CMS jar execution.

Run: python -m tests.test_hospice_release
"""
import asyncio
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bridge.hospice_client import HospiceProviderError, hospice_records_through, resolve_quality
from bridge.hospice_models import HospiceClaimRequest, HospiceResponse
from bridge.hospice_release import (
    CALCULATION_VERSIONS,
    FIRST_SERVICE_DATE,
    JAR_FILENAME,
    JAR_SHA256,
    LAST_SERVICE_DATE,
    PAYMENT_SLOTS,
    SUPPORTED_FISCAL_YEARS,
    fiscal_year,
    fiscal_year_start,
)
from bridge.hospice_routes import readiness, ready, router
from myelin.pricers.opsf import OPSF

ROOT = Path(__file__).resolve().parents[1]


def line(number, revenue, dos, units, hcpcs="Q5001"):
    return dict(claimLineId=number, lineNumber=number, revenueCode=revenue, hcpcs=hcpcs, dateOfService=dos, units=units)


def request_data(**changes):
    data = dict(claimId="HOSPICE-TEST", billType="811", providerNumber="141513", npi="1972322725",
                admissionDate="2026-05-17", fromDate="2026-07-01", throughDate="2026-07-31",
                patientCbsa="16984", providerCbsa="99914",
                claimLines=[line(1, "0651", "2026-07-01", 9), line(2, "0652", "2026-07-10", 40),
                            line(3, "0656", "2026-07-11", 3), line(4, "0655", "2026-07-14", 5), line(5, "0651", "2026-07-19", 13)])
    data.update(changes)
    return data


def rejected(**changes):
    try:
        HospiceClaimRequest.model_validate(request_data(**changes))
    except ValidationError:
        return
    raise AssertionError(f"Accepted an invalid hospice request: {changes}")


async def asgi(app, method, path, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Event().wait()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
               "server": ("test", 80), "root_path": ""}, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))


# The pinned jar on disk is the reconciled CMS release (FY 2026 and FY 2027 only).
assert hashlib.sha256((ROOT / "jars" / "pricers" / JAR_FILENAME).read_bytes()).hexdigest() == JAR_SHA256
assert SUPPORTED_FISCAL_YEARS == (2026, 2027) and CALCULATION_VERSIONS == {2026: "2026.0", 2027: "2027.0"}
assert FIRST_SERVICE_DATE.isoformat() == "2025-10-01" and LAST_SERVICE_DATE.isoformat() == "2027-09-30"
assert PAYMENT_SLOTS == ("0651", "0652", "0655", "0656")
assert fiscal_year(date(2026, 9, 30)) == 2026 and fiscal_year(date(2026, 10, 1)) == 2027 and fiscal_year_start(2027) == date(2026, 10, 1)

# Contract: a valid monthly claim, and each shape that the CMS jar would misprice or that CMS returns.
accepted = HospiceClaimRequest.model_validate(request_data())
assert accepted.priorBenefitDays == 0 and accepted.claimLines[0].last_day == date(2026, 7, 9)
assert accepted.claimLines[1].last_day == date(2026, 7, 10), "a CHC line covers its own day only"
for bill in ("812", "813", "814", "817", "821"):
    HospiceClaimRequest.model_validate(request_data(billType=bill))
for bill in ("810", "818", "81A", "815", "721", "8111"):
    rejected(billType=bill)
rejected(throughDate="2026-08-01")                                                  # spans two months
rejected(fromDate="2026-07-02")                                                     # a line before fromDate
rejected(admissionDate="2026-07-05")                                                # a line before admission
rejected(claimLines=[line(1, "0651", "2026-07-25", 8)])                             # runs past throughDate
rejected(claimLines=[line(1, "0652", "2026-07-03", 97)])                            # CHC over 24 hours
rejected(claimLines=[line(1, "0655", "2026-07-03", 6)], providerCbsa="99914")       # respite over 5 days
rejected(claimLines=[line(1, "0655", "2026-07-03", 3), line(2, "0655", "2026-07-06", 3)])   # consecutive respite
HospiceClaimRequest.model_validate(request_data(claimLines=[line(1, "0655", "2026-07-03", 3), line(2, "0655", "2026-07-07", 3)]))
rejected(claimLines=[line(1, "0651", "2026-07-01", 10), line(2, "0656", "2026-07-10", 2)])  # overlapping days
rejected(claimLines=[line(1, "0652", "2026-07-03", 40), line(2, "0652", "2026-07-03", 8)])  # two CHC lines one day
rejected(claimLines=[line(1, "0656", "2026-07-01", 5), line(2, "0652", "2026-07-03", 40)])  # CHC during GIP
# CHC days may sit inside a routine home care period: its units count only the routine days.
HospiceClaimRequest.model_validate(request_data(claimLines=[line(1, "0651", "2026-07-01", 29), line(2, "0652", "2026-07-10", 40)]))
rejected(patientCbsa=None)
rejected(providerCbsa=None)
HospiceClaimRequest.model_validate(request_data(providerCbsa=None, claimLines=[line(1, "0651", "2026-07-01", 31)]))
HospiceClaimRequest.model_validate(request_data(patientCbsa=None, claimLines=[line(1, "0656", "2026-07-01", 3)]))
rejected(patientCbsa="169845")
rejected(patientCbsa="16984.0")
rejected(claimLines=[line(1, "0651", "2026-07-01", 3), line(1, "0651", "2026-07-05", 3)])   # duplicate ids
rejected(claimLines=[line(1, "0657", "2026-07-01", 1)])                             # physician services are not sent
rejected(claimLines=[line(1, "0651", "2026-07-01", 0)])
rejected(claimLines=[line(1, "0651", "2026-07-01", 3, hcpcs="Q501")])
rejected(claimLines=[])
rejected(priorBenefitDays=-1)
rejected(providerNumber="14-513")
rejected(npi="123")
rejected(unexpected="value")
assert HospiceClaimRequest.model_validate(request_data(providerNumber="14a513")).providerNumber == "14A513"

# A routine home care line's units count its routine days only, so continuous home care days inside the period push
# its last day out: 29 routine days from July 1 around CHC on July 10 and 11 end on July 31.
HospiceClaimRequest.model_validate(request_data(providerCbsa=None, claimLines=[
    line(1, "0651", "2026-07-01", 29), line(2, "0652", "2026-07-10", 40), line(3, "0652", "2026-07-11", 36)]))
rejected(providerCbsa=None, claimLines=[line(1, "0651", "2026-07-02", 30), line(2, "0652", "2026-07-10", 40)])   # 30 routine days end Aug 1
rejected(claimLines=[line(1, "0651", "2026-07-01", 9), line(2, "0652", "2026-07-05", 40), line(3, "0656", "2026-07-10", 2)])  # routine day 9 is July 10

# End-of-life add-on units: seven daily totals, day 1 being the date of death (the through date), on routine home care days only.
death = dict(providerCbsa=None, claimLines=[line(1, "0651", "2026-07-01", 31)], dateOfDeath="2026-07-31")
with_units = HospiceClaimRequest.model_validate(request_data(**death, endOfLifeAddOnUnits=[8, 16, 20, 0, 0, 0, 4]))
assert with_units.endOfLifeAddOnUnits == [8, 16, 20, 0, 0, 0, 4] and with_units.dateOfDeath == date(2026, 7, 31)
rejected(**death)                                                                   # a date of death without units
rejected(providerCbsa=None, claimLines=[line(1, "0651", "2026-07-01", 31)], endOfLifeAddOnUnits=[4, 0, 0, 0, 0, 0, 0])
rejected(**{**death, "dateOfDeath": "2026-07-30"}, endOfLifeAddOnUnits=[4, 0, 0, 0, 0, 0, 0])   # not the through date
rejected(**death, endOfLifeAddOnUnits=[0, 0, 0, 0, 0, 0, 0])                        # no units at all
rejected(**death, endOfLifeAddOnUnits=[4, 0, 0, 0, 0, 0])                           # six days
rejected(**death, endOfLifeAddOnUnits=[-1, 4, 0, 0, 0, 0, 0])
inpatient_end = dict(claimLines=[line(1, "0651", "2026-07-01", 27), line(2, "0656", "2026-07-28", 4)], dateOfDeath="2026-07-31")
rejected(**inpatient_end, endOfLifeAddOnUnits=[4, 0, 0, 0, 0, 0, 0])                # July 31 is a GIP day
HospiceClaimRequest.model_validate(request_data(**inpatient_end, endOfLifeAddOnUnits=[0, 0, 0, 0, 4, 0, 0]))   # July 27 is routine
rejected(providerCbsa=None, claimLines=[line(1, "0651", "2026-07-01", 30), line(2, "0652", "2026-07-31", 40)],
         dateOfDeath="2026-07-31", endOfLifeAddOnUnits=[4, 0, 0, 0, 0, 0, 0])      # July 31 is a CHC day

# Provider-file quality lookup on a synthetic OPSF table.
engine = create_engine("sqlite://")
OPSF.__table__.create(engine)


def opsf(ccn, effective, provider_type="35", quality=" ", npi=None, termination=None):
    return OPSF(provider_ccn=ccn, effective_date=effective, provider_type=provider_type, hospital_quality_indicator=quality,
                national_provider_identifier=npi, termination_date=termination)


with Session(engine) as session:
    session.add_all([
        opsf("141513", 20241001, npi="1972322725"), opsf("141513", 20251001, npi="1972322725"),
        opsf("141524", 20241001, quality="1", npi="1467410282"), opsf("141524", 20251001, quality="1", npi="1467410282"),
        opsf("141745", 20250814, npi="1111111111"),                                   # no FY 2026 record: its latest stands
        opsf("140001", 20251001, provider_type="00", npi="2222222222"),               # a hospital
        opsf("291605", 20241001, npi="3333333333", termination=20250101),             # terminated hospice
        opsf("141800", 20251001, npi="4444444444"), opsf("141801", 20251001, quality="1", npi="4444444444"),
        opsf("141900", 20251001, quality="1"), opsf("141900", 20251001, quality=" "),  # conflicting records
        opsf("141600", 20260507, npi="5555555555"),
    ])
    session.commit()

through = hospice_records_through(engine)
assert through == date(2026, 5, 7), through


def quality(**changes):
    payload = HospiceClaimRequest.model_validate(request_data(**changes))
    with Session(engine) as session:
        return resolve_quality(session, payload, through)


def provider_error(**changes):
    try:
        quality(**changes)
    except HospiceProviderError as error:
        return error.code
    raise AssertionError(f"No provider error for {changes}")


full = quality()
assert (full.reduction, full.source, full.provider.ccn, full.provider.effectiveDate) == (False, "ProviderFile", "141513", date(2025, 10, 1))
assert full.provider.npiMatchesClaim is True and "full rates apply" in full.note
reduced = quality(providerNumber="141524", npi="1467410282")
assert (reduced.reduction, reduced.source, reduced.provider.qualityIndicator) == (True, "ProviderFile", "1")
assert quality(providerNumber="141524", npi="1972322725").provider.npiMatchesClaim is False, "an NPI mismatch is reported, not refused"
older = quality(providerNumber="141745", npi=None)
assert (older.source, older.provider.effectiveDate) == ("ProviderFile", date(2025, 8, 14)), "the record in effect is used, as FISS does"
assert quality(providerNumber="149999").source == "FullRatesProviderNotFound"
assert quality(providerNumber=None, npi="1467410282").provider.ccn == "141524", "an NPI alone finds its single hospice"
assert quality(providerNumber=None, npi="4444444444").source == "FullRatesProviderAmbiguous"
assert quality(providerNumber="141900", npi=None).source == "FullRatesProviderAmbiguous"
assert quality(providerNumber=None, npi=None).source == "FullRatesProviderNotFound"
assert provider_error(providerNumber="140001") == "HOSPICE_PROVIDER_NOT_HOSPICE"
assert provider_error(providerNumber="291605", npi=None) == "HOSPICE_PROVIDER_TERMINATED"
october = dict(admissionDate="2026-10-01", fromDate="2026-10-01", throughDate="2026-10-31",
               claimLines=[line(1, "0651", "2026-10-01", 31)])
fy2027 = quality(providerNumber="141524", npi="1467410282", **october)
assert (fy2027.reduction, fy2027.source, fy2027.provider.ccn) == (False, "FullRatesProviderFileBeforeFiscalYear", "141524")
assert "before FY 2027" in fy2027.note and "2026-05-07" in fy2027.note
assert provider_error(providerNumber="140001", **october) == "HOSPICE_PROVIDER_NOT_HOSPICE", "identity checks apply whatever the fiscal year"
with Session(engine) as session:
    later = resolve_quality(session, HospiceClaimRequest.model_validate(request_data(providerNumber="141524", **october)), date(2026, 10, 1))
assert (later.reduction, later.source) == (True, "ProviderFile"), "a provider file with FY 2027 records supplies FY 2027 flags"

# Readiness reflects the switch, the release and the provider file's quality coverage.
state = readiness(None)
assert state["enabled"] is False and state["ready"] is False and state["calculationVersions"] == {"2026": "2026.0", "2027": "2027.0"}
assert state["endOfLifeAddOn"] is True, "readiness says the bridge takes end-of-life add-on units (0.9.1+)"
assert state["qualityIndicatorFiscalYears"] == [] and state["providerFileHospiceRecordsThrough"] is None
coverage = readiness(SimpleNamespace(records_through=date(2026, 5, 7)))
assert coverage["enabled"] is True and coverage["qualityIndicatorFiscalYears"] == [2026]
assert readiness(SimpleNamespace(records_through=date(2026, 10, 1)))["qualityIndicatorFiscalYears"] == [2026, 2027]
try:
    ready(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hospice_processor=None))))
    raise AssertionError("Readiness succeeded with hospice disabled.")
except Exception as error:
    assert getattr(error, "status_code", None) == 503

# Routes: disabled returns 503; a processor result is published with exact decimals; invalid requests get 422.
app = FastAPI(version="test")
app.include_router(router)
status, body = asyncio.run(asgi(app, "POST", "/api/v1/hospice/process", request_data()))
assert status == 503, body


class FakeProcessor:
    def process(self, payload, lock, bridge_version):
        assert isinstance(payload, HospiceClaimRequest) and bridge_version == "test"
        return HospiceResponse(claimId=str(payload.claimId), status="Success", reason="ok", bridgeVersion=bridge_version,
                               pricerRelease="2.7.1", jarSha256=JAR_SHA256, admissionDate=payload.admissionDate,
                               fromDate=payload.fromDate, throughDate=payload.throughDate, totalPayment=Decimal("7263.41"))


app.state.hospice_processor, app.state.processing_lock = FakeProcessor(), Lock()
status, body = asyncio.run(asgi(app, "POST", "/api/v1/hospice/process", request_data()))
assert status == 200 and Decimal(str(body["totalPayment"])) == Decimal("7263.41") and body["status"] == "Success", body
status, body = asyncio.run(asgi(app, "POST", "/api/v1/hospice/process", request_data(billType="818")))
assert status == 422, body


class FailingProcessor:
    def process(self, *args):
        raise RuntimeError("java.lang.IllegalStateException: claim detail that must not leak")


app.state.hospice_processor = FailingProcessor()
status, body = asyncio.run(asgi(app, "POST", "/api/v1/hospice/process", request_data()))
assert status == 503 and "claim detail" not in json.dumps(body), body
print("PASS hospice release pinning, request contract, provider-file quality lookup, readiness and route gating.")
