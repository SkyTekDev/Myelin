"""Add the last pre-2023 CMS SNF row per CCN. Dry-run unless --apply is supplied.

Run as `python -m scripts.backfill_snf_history --help` from Myelin.
The source must be the CMS v2 SNF CSV export, filtered through 2022-12-31.
Never replaces existing records. A full SQLite backup is required before writes.
"""
import argparse
import csv
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from myelin.pricers.ipsf import DATATYPES

SOURCE_URL = 'https://pds.mps.cms.gov/fiss/v2/inpatient/export?facilityType=SNF&fromDate=1980-01-01&toDate=2022-12-31&format=csv'
# CMS uses these names rather than a direct camel-case version of our columns.
ALIASES = {
    'pps_facility_specific_rate': 'caseMixAdjustedCostPerDischarge_PpsFacilitySpecificRate',
    'hosp_quality_indicator': 'hospitalQualityIndicator',
    'cbsa_wi_location': 'cbsaWageIndexLocation',
    'vpb_participant_indicator': 'vbpParticipantIndicator',
    'bundle_model_discount': 'bundleModel1Discount',
}


def baselines(path):
    latest = {}
    count = 0
    expected = [ALIASES.get(k, k.split('_')[0] + ''.join(p.title() for p in k.split('_')[1:])) for k in DATATYPES]
    with path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.reader(stream)
        if next(reader) != expected:
            raise ValueError('CSV header differs from the supported CMS v2 layout.')
        for number, row in enumerate(reader, 2):
            if len(row) != len(expected):
                raise ValueError(f'Incorrect CSV width at line {number}.')
            count += 1
            # v2 may include swing beds despite facilityType=SNF; PPS here is type 38.
            if row[DATATYPES['provider_type']['position']] != '38':
                continue
            record = {}
            for key, meta in DATATYPES.items():
                value = row[meta['position']]
                # Preserve explicit whitespace status indicators; empty is missing.
                if value == '':
                    value = None
                elif meta['type'] == 'INT':
                    value = int(value)
                elif meta['type'] == 'REAL':
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError(f'Nonfinite field at line {number}.')
                record[key] = value
            ccn, effective = record['provider_ccn'], record['effective_date']
            if record['provider_type'] != '38' or not ccn or not effective or effective >= 20230101:
                raise ValueError(f'Unexpected provider/date at line {number}.')
            datetime.strptime(str(effective), '%Y%m%d')
            previous = latest.get(ccn)
            if previous and previous['effective_date'] == effective and previous != record:
                raise ValueError(f'Conflicting source rows for {ccn}/{effective}.')
            if not previous or previous['effective_date'] < effective:
                latest[ccn] = record
    if not latest:
        raise ValueError('Empty historical source.')
    return count, list(latest.values())


def run(args):
    count, records = baselines(args.csv)
    source_hash = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    mode = 'rw' if args.apply else 'ro'
    conn = sqlite3.connect(args.database.resolve().as_uri() + '?mode=' + mode, uri=True)
    conn.row_factory = sqlite3.Row
    report = dict(sourceUrl=SOURCE_URL, sha256=source_hash, sourceRows=count,
                  baselineRows=len(records), applied=args.apply,
                  timestamp=datetime.now(timezone.utc).isoformat())
    try:
        if args.apply:
            if not args.backup:
                raise ValueError('--backup is required with --apply.')
            # Exclusive creation prevents accidental replacement of an earlier backup.
            with args.backup.open('xb'):
                pass
            with sqlite3.connect(args.backup) as backup:
                conn.backup(backup)
                if backup.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed.')
            report['backup'] = str(args.backup.resolve())
            conn.execute('begin immediate')
        pending = []
        for record in records:
            existing = conn.execute('select * from ipsf where provider_ccn=? and effective_date=?',
                                    (record['provider_ccn'], record['effective_date'])).fetchall()
            if existing:
                if len(existing) != 1 or any(existing[0][k] != v for k, v in record.items()):
                    raise ValueError(f"Existing row conflict: {record['provider_ccn']}/{record['effective_date']}")
            else:
                pending.append(tuple(record.values()))
        report['alreadyPresent'] = len(records) - len(pending)
        report['rowsToInsert'] = len(pending)
        report['rowsBefore'] = conn.execute('select count(*) from ipsf').fetchone()[0]
        if args.apply:
            columns = ','.join(DATATYPES)
            marks = ','.join('?' for _ in DATATYPES)
            conn.executemany(f'insert into ipsf ({columns}) values ({marks})', pending)
            if conn.execute('pragma integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Database integrity check failed; rolling back.')
            report['rowsAfter'] = conn.execute('select count(*) from ipsf').fetchone()[0]
            assert report['rowsAfter'] == report['rowsBefore'] + len(pending)
            conn.commit()
        print(json.dumps(report, indent=2))
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--database', type=Path, default=Path('data/myelin.db'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--report', type=Path)
    run(parser.parse_args())
