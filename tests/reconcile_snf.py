"""Compare embedded bridge results to the standalone pinned CMS REST executable."""
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
import jpype
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from pydantic_core import to_json
from bridge.snf_client import SnfProcessor
from bridge.snf_release import CALCULATION_VERSIONS
from bridge.snf_provider import provider_for_segment, SnfInputError
from myelin.pricers.ipsf import IPSF

root = Path(__file__).resolve().parents[1]
engine = create_engine(f"sqlite:///file:{(root / 'data/myelin.db').as_posix()}?mode=ro&uri=true")
jpype.startJVM(convertStrings=True)
processor = SnfProcessor(engine, root / 'jars')
report = []
for year, month in [(2023, 1)] + [(year, 6) for year in CALCULATION_VERSIONS]:
    start, end = date(year, month, 1), date(year, month, 3)
    with Session(engine) as session:
        ccns = session.scalars(select(IPSF.provider_ccn).where(IPSF.provider_type == '38',
            IPSF.effective_date <= int(start.strftime('%Y%m%d'))).distinct().order_by(IPSF.provider_ccn)).all()
    provider = None
    for ccn in ccns:
        try:
            provider = provider_for_segment(engine, ccn, start, end)
            if year >= 2027:
                processor.validate_provider_year(provider, start)
            break
        except SnfInputError:
            continue
    assert provider is not None, year
    for prior in (0, 3, 20):
        payload = dict(providerData=provider, claimData=dict(providerCcn=ccn, hippsCode='BARD1',
            serviceFromDate=start.isoformat(), serviceThroughDate=end.isoformat(), serviceUnits=3,
            pdpmPriorDays=prior, diagnosisCodes=['Z4789']))
        embedded = processor.calculate(payload)
        with urlopen(Request('http://127.0.0.1:18086/v2/price-claim',
                             data=to_json(payload), headers={'Content-Type':'application/json'}), timeout=30) as response:
            standalone = json.load(response, parse_float=Decimal)
        assert embedded['returnCodeData']['code'] == standalone['returnCodeData']['code'] == '00'
        assert embedded['calculationVersion'] == standalone['calculationVersion']
        for field in ('totalPayment', 'finalWageIndex', 'aidsAddOnIndicator',
                      'qualityReportingProgramIndicator', 'regionIndicator', 'valueBasedPurchasingPaymentDifference'):
            assert embedded['paymentData'][field] == standalone['paymentData'][field], field
        report.append(dict(year=year, serviceFromDate=start.isoformat(), priorDays=prior, provider=provider,
                           result=standalone, matched=True))
Path(sys.argv[1]).write_bytes(to_json(report, indent=2))
print(f'PASS {len(report)} real-provider synthetic-claim comparisons across FY 2023–2026.')
