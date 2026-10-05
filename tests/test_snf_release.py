"""Fiscal year boundaries and readiness must agree with the verified release."""
from types import SimpleNamespace
from pydantic import ValidationError
from bridge.snf_models import Segment, SnfRequest
from bridge.snf_release import CALCULATION_VERSIONS, LAST_SERVICE_DATE
from bridge.snf_routes import ready

def segment(start, end, units, prior=0, identity='1'):
    return dict(lineId=identity, hippsCode='BARD1', fromDate=start, throughDate=end, units=units, pdpmPriorDays=prior)

def rejected(payload):
    try:
        Segment.model_validate(payload)
    except ValidationError:
        return
    raise AssertionError('Unverified fiscal year or unsplit boundary accepted.')

Segment.model_validate(segment('2027-09-30', '2027-09-30', 1))
Segment.model_validate(segment('2026-10-01', '2026-10-01', 1))
rejected(segment('2027-10-01', '2027-10-01', 1))
rejected(segment('2026-09-30', '2026-10-01', 2))
rejected(segment('2026-09-30', '2026-10-01', 2))
payload = dict(claimId='TEST', submitterId='TEST', patientId='TEST', billType='211',
               providerCcn='015999', diagnosisCodes=['Z4789'], segments=[
                   segment('2026-09-30', '2026-09-30', 1, 20),
                   segment('2026-10-01', '2026-10-02', 2, 21, '2')])
assert SnfRequest.model_validate(payload).segments[1].pdpmPriorDays == 21
state = ready(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(snf_processor=object()))))
assert state['lastServiceDate'] == LAST_SERVICE_DATE.isoformat() == '2027-09-30'
assert state['calculationYears'] == list(CALCULATION_VERSIONS)
assert state['calculationVersions'][2024] == '2024.1'
assert state['calculationVersions'][2027] == '2027.0'
assert 2028 not in state['calculationYears']
print('PASS SNF release readiness, date limits and continuous prior days across a supported fiscal-year boundary.')
