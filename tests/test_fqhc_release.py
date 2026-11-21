"""FQHC request contract, release pinning and route gating; no JVM or CMS jar execution."""
import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from fastapi import FastAPI
from pydantic import ValidationError

from bridge.fqhc_client import _version, telehealth_lines
from bridge.fqhc_models import FqhcClaimRequest, FqhcResponse
from bridge.fqhc_release import (
    FIRST_SERVICE_DATE,
    JAR_FILENAME,
    JAR_SHA256,
    LAST_SERVICE_DATE,
    SUPPORTED_YEARS,
)
from bridge.fqhc_routes import readiness, ready, router

ROOT = Path(__file__).resolve().parents[1]


def line(number, revenue, hcpcs, dos="2026-07-15", charge="300.00", modifiers=()):
    return dict(claimLineId=number, lineNumber=number, revenueCode=revenue, hcpcs=hcpcs, dateOfService=dos,
                units=1, chargeAmount=charge, modifiers=list(modifiers))


def request_data(**changes):
    data = dict(claimId="FQHC-TEST", billType="771", providerNumber="071801", patientAge=70, patientSex="F",
                principalDiagnosis=dict(code="E119"), carrierCode="13102", localityCode="00",
                claimLines=[line(1, "0521", "G0467"), line(2, "0521", "99214", charge="250.00")])
    data.update(changes)
    return data


def rejected(**changes):
    try:
        FqhcClaimRequest.model_validate(request_data(**changes))
    except ValidationError:
        return
    raise AssertionError(f"Accepted an invalid FQHC request: {changes}")


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


# The pinned jar on disk is the reconciled CMS release.
assert hashlib.sha256((ROOT / "jars" / "pricers" / JAR_FILENAME).read_bytes()).hexdigest() == JAR_SHA256
assert SUPPORTED_YEARS == (2025, 2026)
assert FIRST_SERVICE_DATE.isoformat() == "2025-01-01" and LAST_SERVICE_DATE.isoformat() == "2026-12-31"

# Contract: a valid 77X claim, and each rejected shape.
accepted = FqhcClaimRequest.model_validate(request_data())
assert accepted.carrierCode == "13102" and accepted.oppsFlag == 1 and accepted.medicareAdvantagePlanAmount is None
rejected(billType="131")
rejected(oppsFlag=2)
rejected(carrierCode="1310")
rejected(localityCode="0")
rejected(medicareAdvantagePlanAmount="0")
rejected(claimLines=[line(1, "0521", "G0467"), line(1, "0521", "99214")])
rejected(claimLines=[line(1, "0521", "G0467", charge="300.005")])
rejected(claimLines=[dict(line(1, "0521", "G0467"), editBypass=["12"])])
rejected(unexpected="value")
rejected(providerNumber="07-801")
without_ccn = dict(request_data(), npi="1234567893")
del without_ccn["providerNumber"]
assert FqhcClaimRequest.model_validate(without_ccn).providerNumber is None
assert FqhcClaimRequest.model_validate(request_data(providerNumber="07a801")).providerNumber == "07A801"
assert FqhcClaimRequest.model_validate(request_data(medicareAdvantagePlanAmount="150.00")).medicareAdvantagePlanAmount == Decimal("150.00")

# Distant-site telehealth under CR 14468: modifier 93/95, outside revenue 0900, from October 1, 2026.
telehealth = FqhcClaimRequest.model_validate(request_data(claimLines=[
    line(1, "0521", "99213", "2026-10-15", modifiers=["95"]),
    line(2, "0900", "90834", "2026-10-15", modifiers=["95"]),
    line(3, "0521", "99213", "2026-09-30", modifiers=["93"]),
    line(4, "0521", "99214", "2026-10-15")]))
assert telehealth_lines(telehealth) == [1]
assert _version("27.2.0") == (27, 2) and _version("27.3.1") == (27, 3) and _version("") is None

# Readiness reflects the switch and the release.
state = readiness(None)
assert state["enabled"] is False and state["ready"] is False and state["calculationVersion"] == "2026.1"
assert state["ioceJar"] is None and state["telehealthCodeLines"] is False
assert readiness(object())["enabled"] is True
assert readiness(SimpleNamespace(ioce_jar="ioce-standalone-27.3.0.8.jar"))["telehealthCodeLines"] is True
assert readiness(SimpleNamespace(ioce_jar="ioce-standalone-27.2.0.5.jar"))["telehealthCodeLines"] is False
assert readiness(SimpleNamespace(ioce_jar="ioce-standalone-28.0.0.1.jar"))["telehealthCodeLines"] is True
try:
    ready(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fqhc_processor=None))))
    raise AssertionError("Readiness succeeded with FQHC disabled.")
except Exception as error:
    assert getattr(error, "status_code", None) == 503

# Routes: disabled returns 503; a processor result is published with exact decimals.
app = FastAPI(version="test")
app.include_router(router)
status, body = asyncio.run(asgi(app, "POST", "/api/v1/fqhc/process", request_data()))
assert status == 503, body


class FakeIoce:
    pass


class FakeProcessor:
    def process(self, payload, ioce_client, lock, bridge_version):
        assert isinstance(payload, FqhcClaimRequest) and isinstance(ioce_client, FakeIoce) and bridge_version == "test"
        return FqhcResponse(claimId=str(payload.claimId), status="Success", reason="ok", bridgeVersion=bridge_version,
                            pricerRelease="2.10.0", jarSha256=JAR_SHA256, carrierCode=payload.carrierCode,
                            localityCode=payload.localityCode, totalPayment=Decimal("217.48"))


app.state.fqhc_processor, app.state.ioce_client, app.state.processing_lock = FakeProcessor(), FakeIoce(), Lock()
status, body = asyncio.run(asgi(app, "POST", "/api/v1/fqhc/process", request_data()))
assert status == 200 and Decimal(str(body["totalPayment"])) == Decimal("217.48") and body["status"] == "Success", body
status, body = asyncio.run(asgi(app, "POST", "/api/v1/fqhc/process", request_data(billType="721")))
assert status == 422, body


class FailingProcessor:
    def process(self, *args):
        raise RuntimeError("java.lang.IllegalStateException: claim detail that must not leak")


app.state.fqhc_processor = FailingProcessor()
status, body = asyncio.run(asgi(app, "POST", "/api/v1/fqhc/process", request_data()))
assert status == 503 and "claim detail" not in json.dumps(body), body
print("PASS FQHC release pinning, request contract, telehealth detection, readiness and route gating.")
