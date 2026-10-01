"""Read-only aggregate audit. No claim/patient records are queried."""
import json
import sqlite3
from pathlib import Path
db = Path(__file__).resolve().parents[1] / "data/myelin.db"
conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
report = {"database": "data/myelin.db", "providerType": "38"}
report["allRecords"] = conn.execute("select count(*),count(distinct provider_ccn),min(effective_date),max(effective_date) from ipsf where provider_type='38'").fetchone()
report["byDate"] = []
for day in (20230101, 20231001, 20241001, 20251001, 20260925):
    rows = conn.execute("""select p.* from ipsf p join
      (select provider_ccn,max(effective_date) d from ipsf where effective_date<=? group by provider_ccn) x
      on p.provider_ccn=x.provider_ccn and p.effective_date=x.d where p.provider_type='38'""", (day,))
    names = [x[0] for x in rows.description]
    data = [dict(zip(names, r)) for r in rows]
    active = [r for r in data if r['termination_date'] in (None, 0, 19000101, 99991231) or r['termination_date'] > day]
    complete = [r for r in active if (r['federal_pps_blend'] or '').strip() in ('0','1','4')
                and (r['cbsa_actual_geographic_location'] or '').strip()
                and (r['vbp_adjustment'] or 0) > 0]
    report['byDate'].append(dict(date=day, records=len(data), active=len(active), corePaymentInputs=len(complete),
                                duplicateCcnRows=len(active)-len({r['provider_ccn'] for r in active})))
report['blendCounts'] = conn.execute("select federal_pps_blend,count(*) from ipsf where provider_type='38' group by federal_pps_blend").fetchall()
print(json.dumps(report, indent=2))
