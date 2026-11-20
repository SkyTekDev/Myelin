"""Live CMS I/OCE + FQHC Pricer reconciliation on synthetic claims; no database writes.

Run explicitly: python -m tests.test_fqhc_integration
Expected amounts follow CMS's published base rates (CY 2025 $202.65, CY 2026 $207.72)
times the jar's GAF for Connecticut (carrier 13102, locality 00: 1.054 in 2025, 1.047 in 2026).
"""
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from threading import Lock

import jpype
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


def line(number, revenue, hcpcs, dos="2026-07-15", charge="300.00", modifiers=()):
    return dict(claimLineId=f"L{number}", lineNumber=number, revenueCode=revenue, hcpcs=hcpcs, dateOfService=dos,
                units=1, chargeAmount=charge, modifiers=list(modifiers))


def claim(claim_id, lines, **changes):
    data = dict(claimId=claim_id, billType="771", providerNumber="071801", patientAge=70, patientSex="F",
                principalDiagnosis=dict(code="E119"), secondaryDiagnoses=[dict(code="F329")],
                carrierCode="13102", localityCode="00", claimLines=lines)
    data.update(changes)
    return data


def run():
    if not jpype.isJVMStarted():
        jpype.startJVM(classpath=[str(ROOT / "jars" / "*")])
    from bridge.fqhc_client import FqhcProcessor
    from bridge.fqhc_models import FqhcClaimRequest
    from bridge.fqhc_routes import router
    from myelin.ioce import IoceClient

    processor, ioce, lock = FqhcProcessor(ROOT / "jars"), IoceClient(), Lock()

    def price(data):
        return processor.process(FqhcClaimRequest.model_validate(data), ioce, lock, "test")

    def amounts(result, number):
        priced = next(item for item in result.lines if item.lineNumber == number)
        return priced.payment, priced.coinsuranceAmount, priced.addOnPayment, priced.pricerReturnCode

    # Medical visit at the PPS rate; same-day mental health visit at its lower charge; qualifying visits packaged.
    a = price(claim("A", [line(1, "0521", "G0467"), line(2, "0521", "99214", charge="250.00"),
                          line(3, "0900", "G0470", charge="200.00"), line(4, "0900", "90834", charge="180.00")]))
    assert a.status == "Success" and a.calculationVersion == "2026.1" and a.ioceVersion.startswith("27."), a
    assert a.geographicAdjustmentFactor == Decimal("1.047") and a.pricerReturnCode == "01", a
    assert amounts(a, 1) == (Decimal("217.48"), Decimal("43.50"), Decimal("0"), "01"), a.lines[0]
    assert amounts(a, 2)[0] == 0 and amounts(a, 2)[3] == "09"
    assert amounts(a, 3) == (Decimal("200.00"), Decimal("40.00"), Decimal("0"), "02"), a.lines[2]
    assert a.totalPayment == Decimal("417.48") and a.totalCoinsurance == Decimal("83.50"), a
    assert type(a.lines[0].payment) is Decimal and a.lines[0].paymentIndicator == "10" and a.lines[0].compositeAdjustmentFlag == "01"
    assert not any([a.claimEdits.returnToProvider, a.claimEdits.suspension, a.claimEdits.rejection, a.claimEdits.denial])

    # New patient: the 1.3416 rate exceeds the charge, so the charge is paid.
    b = price(claim("B", [line(1, "0521", "G0466", charge="150.00"), line(2, "0521", "99204", charge="150.00")]))
    assert amounts(b, 1) == (Decimal("150.00"), Decimal("30.00"), Decimal("0"), "02"), b.lines[0]
    assert b.lines[0].paymentIndicator == "13"

    # IPPE: rate x 1.3416 with the add-on reported and coinsurance waived for the preventive visit.
    c = price(claim("C", [line(1, "0521", "G0468", charge="350.00"), line(2, "0521", "G0402")]))
    assert amounts(c, 1) == (Decimal("291.77"), Decimal("0.00"), Decimal("74.29"), "03"), c.lines[0]
    assert c.lines[1].packageFlag == "6"

    # Lines CMS pays outside the pricer: care management (fee schedule) and vaccines (cost report).
    d = price(claim("D", [line(1, "0521", "99490", charge="90.00"), line(2, "0636", "90686", charge="40.00"),
                          line(3, "0771", "G0008", charge="25.00")]))
    assert [amounts(d, n)[3] for n in (1, 2, 3)] == ["08", "11", "11"] and d.totalPayment == 0, d
    assert d.lines[0].paymentIndicator == "2" and d.lines[1].paymentIndicator == "4"

    # Medicare Advantage supplemental claim: PPS rate minus the plan payment; zero without the plan amount.
    ma_lines = [line(1, "0519", "G0467"), line(2, "0519", "99213", charge="200.00")]
    e = price(claim("E", ma_lines, medicareAdvantagePlanAmount="150.00"))
    assert amounts(e, 1) == (Decimal("67.48"), Decimal("0.00"), Decimal("0"), "06"), e.lines[0]
    assert amounts(price(claim("E2", ma_lines)), 1)[3] == "17"

    # CY 2025 uses its own base rate and GAF.
    f = price(claim("F", [line(1, "0521", "G0467", "2025-07-15"), line(2, "0521", "99214", "2025-07-15", "250.00")]))
    assert f.ioceVersion.startswith("26.") and f.geographicAdjustmentFactor == Decimal("1.054"), f
    assert amounts(f, 1)[:2] == (Decimal("213.59"), Decimal("42.72")), f.lines[0]

    # Refusals: dates beyond the reconciled release, and October 2026 telehealth on an older I/OCE.
    g = price(claim("G", [line(1, "0521", "G0467", "2027-01-15"), line(2, "0521", "99214", "2027-01-15")]))
    assert g.status == "UnableToPrice" and g.reasonCode == "FQHC_DATES_NOT_SUPPORTED" and not g.lines, g
    h = price(claim("H", [line(1, "0521", "99213", "2026-10-15", "200.00", ["95"])]))
    if h.ioceVersion and h.ioceVersion < "27.3":
        assert h.status == "UnableToPrice" and h.reasonCode == "FQHC_TELEHEALTH_IOCE_UNAVAILABLE", h

    # The editor's return-to-provider edits are published for the API to act on.
    i = price(claim("I", [line(1, "0521", "99213", charge="200.00")]))
    assert [edit.edit for edit in i.claimEdits.returnToProvider] == ["088"], i.claimEdits

    # A carrier/locality absent from the CMS GAF table is reported per line, never priced.
    k = price(claim("K", [line(1, "0521", "G0467"), line(2, "0521", "99214")], carrierCode="99999", localityCode="99"))
    assert amounts(k, 1)[3] == "19" and amounts(k, 1)[0] == 0, k.lines[0]

    # An NPI-only claim (no CCN) prices the same visit.
    npi_only = claim("NPI", [line(1, "0521", "G0467"), line(2, "0521", "99214")], npi="1234567893")
    del npi_only["providerNumber"]
    assert amounts(price(npi_only), 1)[:2] == (Decimal("217.48"), Decimal("43.50"))

    # The HTTP route publishes the same result.
    app = FastAPI(version="test")
    app.include_router(router)
    app.state.fqhc_processor, app.state.ioce_client, app.state.processing_lock = processor, ioce, lock
    from tests.test_fqhc_release import asgi
    status, body = asyncio.run(asgi(app, "POST", "/api/v1/fqhc/process", claim("HTTP", [line(1, "0521", "G0467"),
                                                                                        line(2, "0521", "99214")])))
    assert status == 200 and Decimal(str(body["lines"][0]["payment"])) == Decimal("217.48"), json.dumps(body)[:500]
    print("JSON amount form:", repr(body["lines"][0]["payment"]), "| GAF:", repr(body["geographicAdjustmentFactor"]))
    print("PASS FQHC I/OCE + CMS Pricer reconciliation: visits, add-on, preventive waiver, MA, 2025 rates, refusals, edits, GAF gaps.")


if __name__ == "__main__":
    run()
