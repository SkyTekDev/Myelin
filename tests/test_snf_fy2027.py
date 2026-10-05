"""FY 2027 calculations, split stays and rejection of stale provider wage inputs."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import jpype
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from myelin.pricers.ipsf import Base, IPSF
from bridge.snf_client import SnfProcessor
from bridge.snf_models import SnfRequest


def run():
    db = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(db)
    common = dict(provider_ccn='015009', provider_type='38', federal_pps_blend='4',
                  cbsa_actual_geographic_location='01', special_payment_indicator='', termination_date=0)
    with Session(db) as session:
        session.add(IPSF(**common, effective_date=20251001, vbp_adjustment=0.9972571246,
            supplemental_wage_index_indicator='1', supplemental_wage_index=0.6471))
        session.add(IPSF(**common, effective_date=20261001, vbp_adjustment=0.9834112442,
            supplemental_wage_index_indicator='1', supplemental_wage_index=0.6367))
        session.commit()
    jpype.startJVM(convertStrings=True)
    processor = SnfProcessor(db, Path(__file__).resolve().parents[1] / 'jars')
    def request(start='2026-10-01', end='2026-10-03', prior=0):
        return dict(claimId='FY2027', submitterId='1', patientId='SYNTHETIC', billType='211',
            providerCcn='015009', diagnosisCodes=['Z4789'], segments=[dict(lineId='1', hippsCode='BARD1',
            fromDate=start, throughDate=end, units=(date.fromisoformat(end)-date.fromisoformat(start)).days+1,
            pdpmPriorDays=prior)])
    for prior, expected in ((0,'2008.86'),(3,'1466.16'),(20,'1454.34')):
        result = processor.process(SnfRequest.model_validate(request(prior=prior)))
        assert result.status == 'Success' and result.totalPayment == Decimal(expected), result
        assert result.segments[0].cms['calculationVersion'] == '2027.0'
    assert processor.process(SnfRequest.model_validate(request('2027-09-30','2027-09-30'))).totalPayment == Decimal('669.62')
    boundary = request('2026-09-30','2026-09-30',20)
    boundary['segments'].append(dict(lineId='2',hippsCode='BARD1',fromDate='2026-10-01',
        throughDate='2026-10-02',units=2,pdpmPriorDays=21))
    result = processor.process(SnfRequest.model_validate(boundary))
    assert result.status == 'Success', result
    assert [segment.cms['calculationVersion'] for segment in result.segments] == ['2026.0','2027.0']
    assert result.totalPayment == sum(segment.cms['paymentData']['totalPayment'] for segment in result.segments)
    with Session(db) as session:
        current = session.query(IPSF).filter(IPSF.effective_date == 20261001).one()
        current.supplemental_wage_index = 0.55
        session.commit()
    result = processor.process(SnfRequest.model_validate(boundary))
    assert result.status == 'UnableToPrice' and result.totalPayment is None and not result.segments
    assert 'prior fiscal-year CMS final wage index' in result.reason
    with Session(db) as session:
        session.query(IPSF).filter(IPSF.effective_date == 20261001).delete()
        session.commit()
    result = processor.process(SnfRequest.model_validate(request()))
    assert result.status == 'UnableToPrice' and 'Current fiscal-year' in result.reason
    # Historical claims still use their own provider inputs.
    assert processor.process(SnfRequest.model_validate(request('2026-06-01','2026-06-03'))).totalPayment == Decimal('1961.78')
    # CMS specifies blank supplemental fields for a genuinely new facility.
    with Session(db) as session:
        session.query(IPSF).delete()
        session.add(IPSF(**common, effective_date=20261001, vbp_adjustment=1,
            supplemental_wage_index_indicator='', supplemental_wage_index=None))
        session.commit()
    assert processor.process(SnfRequest.model_validate(request())).status == 'Success'
    with Session(db) as session:
        current = session.query(IPSF).one()
        current.supplemental_wage_index_indicator='1'
        current.supplemental_wage_index=0.6367
        session.commit()
    result = processor.process(SnfRequest.model_validate(request()))
    assert result.status == 'UnableToPrice' and 'unexplained' in result.reason
    db.dispose()
    jpype.shutdownJVM()
    print('PASS FY 2027 prices, split-year prior days, stale/mismatched wage rejection and new-provider handling.')


if __name__ == '__main__':
    run()
