"""Adapter for immutable annual HH separate-payment rate releases."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from bridge.hha_provider_annual import canonical_json
from bridge.hha_separate_payment_rates import read_package, validate_document
from bridge.immutable_reference import import_versioned_catalog

SCHEMA_PATH = Path(__file__).parent / 'references/hha-separate-payment-schema-v1.sql'
TABLES = {'hha_rate_releases','hha_payment_rates'}


def schema_statements():
    statement = ''
    for line in SCHEMA_PATH.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete separate-payment schema.')


def _schema(connection):
    return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name IN ('hha_rate_releases','hha_payment_rates') AND type IN ('table','trigger') ORDER BY name"))


def check_schema(connection):
    existing = _schema(connection)
    if not existing:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in schema_statements():
            expected.execute(statement)
        if existing != _schema(expected):
            raise ValueError('Unexpected or incomplete separate-payment schema.')
    return True


def rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names,row)) for row in cursor]


def read_release(connection, digest):
    releases = rows(connection.execute('SELECT * FROM hha_rate_releases WHERE catalog_sha256=?',(digest,)))
    if len(releases) != 1 or releases[0]['sealed'] != 1:
        raise ValueError('The requested rate release is missing or unsealed.')
    release,entries = releases[0],{}
    for item in rows(connection.execute('SELECT * FROM hha_payment_rates WHERE catalog_sha256=? ORDER BY hcpcs_code',(digest,))):
        row = dict(hcpcsCode=item['hcpcs_code'],unitRate=item['unit_rate_exact'],
            coinsuranceRate=item['coinsurance_rate_exact'],annualPartBDeductible=item['annual_part_b_deductible_exact'])
        if hashlib.sha256(canonical_json(row)).hexdigest() != item['entry_sha256']:
            raise ValueError('Separate-payment row fingerprint mismatch.')
        entries[item['hcpcs_code']] = row
    document = dict(schemaVersion=release['schema_version'],paymentYear=release['payment_year'],
        effectiveFrom=release['effective_from'],effectiveThrough=release['effective_through'],
        sources=json.loads(release['sources_json']),entries=entries)
    validate_document(canonical_json(document),digest)
    if len(entries) != release['rate_count']:
        raise ValueError('Rate release count mismatch.')
    return document,release


def install_release(connection, document, digest, revision, operator):
    validate_document(canonical_json(document),digest)
    if not check_schema(connection):
        for statement in schema_statements():
            connection.execute(statement)
    connection.execute('INSERT INTO hha_rate_releases VALUES (?,?,?,?,?,?,?,?,?,?,?)',(
        digest,document['schemaVersion'],document['paymentYear'],revision,document['effectiveFrom'],document['effectiveThrough'],
        canonical_json(document['sources']).decode(),len(document['entries']),datetime.now(timezone.utc).isoformat(),operator,0))
    connection.executemany('INSERT INTO hha_payment_rates VALUES (?,?,?,?,?,?)',[
        (digest,code,row['unitRate'],row['coinsuranceRate'],row['annualPartBDeductible'],hashlib.sha256(canonical_json(row)).hexdigest())
        for code,row in document['entries'].items()])
    connection.execute('UPDATE hha_rate_releases SET sealed=1 WHERE catalog_sha256=?',(digest,))
    restored,_ = read_release(connection,digest)
    if restored != document:
        raise ValueError('The DB release does not reconstruct the exact rate package.')


def import_catalog(catalog_path, database, *, expected_catalog_sha256, revision, operator, apply=False, backup_path=None):
    result = import_versioned_catalog(catalog_path,database,expected_catalog_sha256=expected_catalog_sha256,
        revision=revision,operator=operator,apply=apply,backup_path=backup_path,read_package=read_package,
        check_schema=check_schema,read_release=read_release,install_release=install_release,
        tables=TABLES,release_table='hha_rate_releases')
    result.update(rateRows=result['rowCount'],newRateRows=result['newRows'])
    return result
