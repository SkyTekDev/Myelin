"""Offline schema/provider checks and live CMS JAR reconciliation; no database writes."""
import copy
import asyncio
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
import jpype
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from myelin.pricers.ipsf import Base, IPSF
from bridge.snf_client import SnfProcessor
from bridge.snf_models import SnfRequest
from bridge.snf_provider import provider_for_segment, SnfInputError
from bridge.snf_routes import router

ROOT = Path(__file__).resolve().parents[1]


def request_data():
    return dict(claimId="SNF-TEST", submitterId="TEST", patientId="TEST-P", billType="211",
        providerCcn="015999", diagnosisCodes=["Z4789"], segments=[dict(lineId="1", hippsCode="BARD1",
            fromDate="2026-06-01", throughDate="2026-06-03", units=3, pdpmPriorDays=0),
            dict(lineId="2", hippsCode="BARD1", fromDate="2026-06-04", throughDate="2026-06-06", units=3, pdpmPriorDays=3)])


def expect_error(fn):
    try:
        fn()
    except (ValueError, SnfInputError):
        return
    raise AssertionError("Expected rejection")


def run():
    db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(db)
    with Session(db) as session:
        session.add(IPSF(provider_ccn="015999", effective_date=20230101, provider_type="38", federal_pps_blend="4",
            cbsa_actual_geographic_location="13820", vbp_adjustment=1, termination_date=0,
            special_payment_indicator="", supplemental_wage_index_indicator=""))
        session.commit()
    for change in (lambda x: x['segments'][0].pop('pdpmPriorDays'),
                   lambda x: x.update(billType='221'),
                   lambda x: x['segments'][1].update(pdpmPriorDays=0),
                   lambda x: x['segments'][1].update(fromDate='2026-06-03'),
                   lambda x: x['segments'][0].update(pdpmPriorDays=True),
                   lambda x: x['segments'][0].update(fromDate='2022-12-31'),
                   lambda x: x['segments'][0].update(fromDate='2026-10-01', throughDate='2026-10-03'),
                   lambda x: x['segments'][1].update(lineId='1'),
                   lambda x: x.update(submitterId=' ')):
        payload = request_data()
        change(payload)
        expect_error(lambda: SnfRequest.model_validate(payload))
    expect_error(lambda: provider_for_segment(db, "999999", date(2026, 6, 1), date(2026, 6, 3)))
    if not jpype.isJVMStarted():
        jpype.startJVM(convertStrings=True)
    processor = SnfProcessor(db, ROOT / "jars")
    output = processor.process(SnfRequest.model_validate(request_data()))
    assert output.status == 'Success', output
    assert output.totalPayment == Decimal('4030.14'), output
    assert len(output.segments) == 2
    # The previously captured standalone CMS result and the embedded JAR agree.
    assert output.segments[0].cms['paymentData']['totalPayment'] == Decimal('2353.51')
    assert output.segments[1].cms['paymentData']['totalPayment'] == Decimal('1676.63')
    api = FastAPI()
    api.include_router(router)
    api.state.snf_processor = processor
    async def post(payload):
        messages = []
        async def receive():
            return {'type':'http.request', 'body':json.dumps(payload).encode(), 'more_body':False}
        async def send(message):
            messages.append(message)
        await api({'type':'http', 'asgi':{'version':'3.0'}, 'http_version':'1.1', 'method':'POST',
                   'scheme':'http', 'path':'/api/v1/snf/process', 'raw_path':b'/api/v1/snf/process',
                   'query_string':b'', 'headers':[(b'content-type',b'application/json')],
                   'client':('127.0.0.1',1), 'server':('test',80), 'root_path':''}, receive, send)
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        body = b''.join(m.get('body',b'') for m in messages if m['type'] == 'http.response.body')
        return status, json.loads(body)
    status, reply = asyncio.run(post(request_data()))
    assert status == 200 and Decimal(reply['totalPayment']) == Decimal('4030.14')
    bad = request_data()
    bad['segments'][0].pop('pdpmPriorDays')
    assert asyncio.run(post(bad))[0] == 422
    api.state.snf_processor = None
    assert asyncio.run(post(request_data()))[0] == 503
    captured = []
    boundary = request_data()
    boundary['segments'] = [dict(lineId='1', hippsCode='BARD1', fromDate='2023-09-30', throughDate='2023-09-30', units=1, pdpmPriorDays=0),
                            dict(lineId='2', hippsCode='BARD1', fromDate='2023-10-01', throughDate='2023-10-02', units=2, pdpmPriorDays=1)]
    boundary_result = processor.process(SnfRequest.model_validate(boundary))
    assert boundary_result.status == 'Success'
    assert [s.cms['calculationVersion'] for s in boundary_result.segments] == ['2023.0', '2024.1']
    january = request_data()
    january['segments'] = [dict(lineId='1', hippsCode='BARD1', fromDate='2023-01-01', throughDate='2023-01-03', units=3, pdpmPriorDays=0)]
    assert processor.process(SnfRequest.model_validate(january)).status == 'Success'
    for year in (2023, 2024, 2025, 2026):
        req = request_data()
        req['segments'] = [req['segments'][0]]
        req['segments'][0].update(fromDate=f'{year}-06-01', throughDate=f'{year}-06-03')
        result = processor.process(SnfRequest.model_validate(req))
        assert result.status == 'Success', result
        assert result.segments[0].cms['calculationVersion'].startswith(str(year)), result
        captured.append(dict(request=req, response=result.model_dump(mode='json')))
    # Changing a provider within the service period requires segment splitting.
    with Session(db) as session:
        session.add(IPSF(provider_ccn='015999', effective_date=20260602, provider_type='38'))
        session.commit()
    rejected = processor.process(SnfRequest.model_validate(request_data()))
    assert rejected.status == 'UnableToPrice' and rejected.totalPayment is None
    with Session(db) as session:
        session.query(IPSF).filter(IPSF.effective_date == 20260602).delete()
        row = session.query(IPSF).one()
        row.vbp_adjustment = None
        session.commit()
    assert processor.process(SnfRequest.model_validate(request_data())).status == 'UnableToPrice'
    with Session(db) as session:
        row = session.query(IPSF).one()
        row.vbp_adjustment = 1
        row.termination_date = 20260601
        session.commit()
    assert processor.process(SnfRequest.model_validate(request_data())).status == 'UnableToPrice'
    print(json.dumps(dict(checks='passed', historical=captured), indent=2))


if __name__ == '__main__':
    run()
