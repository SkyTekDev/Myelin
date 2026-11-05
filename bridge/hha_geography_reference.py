"""Transactional, append-only geography imports into the Myelin SQLite DB."""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from bridge.hha_geography_catalog import read_catalog_package, validate_document
from bridge.hha_provider_annual import canonical_json
from bridge.immutable_reference import file_sha256, table_fingerprints, import_versioned_catalog

SCHEMA_PATH = Path(__file__).parent / 'references/hha-geography-schema-v1.sql'
TABLES = {'hha_geography_releases', 'hha_geography_counties'}


def schema_statements():
    statement = ''
    for line in SCHEMA_PATH.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete HH geography schema.')


def _schema(connection):
    return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'hha_geography_*' ORDER BY name"))


def check_schema(connection):
    existing = _schema(connection)
    if not existing:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in schema_statements():
            expected.execute(statement)
        if existing != _schema(expected):
            raise ValueError('Unexpected or incomplete HH geography schema; no changes made.')
    return True


def _dict_rows(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def read_release(connection, digest):
    releases = _dict_rows(connection.execute('SELECT * FROM hha_geography_releases WHERE catalog_sha256=?', (digest,)))
    if len(releases) != 1 or releases[0]['sealed'] != 1:
        raise ValueError('The requested HH geography release is missing or unsealed.')
    release = releases[0]
    entries = {}
    for item in _dict_rows(connection.execute('SELECT * FROM hha_geography_counties WHERE catalog_sha256=? ORDER BY county_fips', (digest,))):
        row = dict(serviceCountyFips=item['county_fips'], countyName=item['county_name'], stateFips=item['state_fips'],
            stateAbbreviation=item['state_abbreviation'], status=item['mapping_status'],
            baseWageIndexCode=item['base_wage_index_code'], cmsWageIndexCode=item['cms_wage_index_code'],
            wageIndex=item['wage_index_exact'], sourceRows=dict(county=item['county_source_row'], census=item['census_source_row'],
                hhUrban=item['hh_urban_source_row'], transition=item['transition_source_row'], wage=item['wage_source_row']))
        if hashlib.sha256(canonical_json(row)).hexdigest() != item['entry_sha256'] or item['county_fips'] in entries:
            raise ValueError('HH geography county fingerprint mismatch.')
        entries[item['county_fips']] = row
    document = dict(schemaVersion=release['schema_version'], paymentYear=release['payment_year'],
        effectiveFrom=release['effective_from'], effectiveThrough=release['effective_through'],
        sources=json.loads(release['sources_json']), entries=entries)
    validate_document(canonical_json(document), digest)
    if (len(entries) != release['county_count']
            or sum(row['status'] == 'Verified' for row in entries.values()) != release['verified_count']):
        raise ValueError('HH geography release coverage mismatch.')
    return document, release


def install_release(connection, document, digest, revision, operator):
    """Caller owns the transaction; also used to seed isolated test DBs."""
    validate_document(canonical_json(document), digest)
    if type(revision) is not int or revision <= 0 or not isinstance(operator, str) or not operator.strip() or len(operator) > 128:
        raise ValueError('Supply a positive revision and an operator name of at most 128 characters.')
    if not check_schema(connection):
        for statement in schema_statements():
            connection.execute(statement)
    entries = document['entries']
    connection.execute('INSERT INTO hha_geography_releases '
        '(catalog_sha256,schema_version,payment_year,revision,effective_from,effective_through,sources_json,'
        'county_count,verified_count,loaded_at_utc,loaded_by,sealed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
        digest, document['schemaVersion'], document['paymentYear'], revision, document['effectiveFrom'],
        document['effectiveThrough'], canonical_json(document['sources']).decode(), len(entries),
        sum(row['status'] == 'Verified' for row in entries.values()), datetime.now(timezone.utc).isoformat(), operator, 0))
    connection.executemany('INSERT INTO hha_geography_counties VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [(
        digest, county, row['countyName'], row['stateFips'], row['stateAbbreviation'], row['status'],
        row['baseWageIndexCode'], row['cmsWageIndexCode'], row['wageIndex'], row['sourceRows']['county'],
        row['sourceRows']['census'], row['sourceRows']['hhUrban'], row['sourceRows']['transition'], row['sourceRows']['wage'],
        hashlib.sha256(canonical_json(row)).hexdigest()) for county, row in entries.items()])
    connection.execute('UPDATE hha_geography_releases SET sealed=1 WHERE catalog_sha256=?', (digest,))
    restored, _ = read_release(connection, digest)
    if canonical_json(restored) != canonical_json(document):
        raise ValueError('Imported geography does not exactly reconstruct the reviewed package.')


def import_catalog(catalog_path, database, *, expected_catalog_sha256, revision, operator,
                   apply=False, backup_path=None):
    document = read_catalog_package(catalog_path, expected_catalog_sha256)
    report = import_versioned_catalog(catalog_path, database, expected_catalog_sha256=expected_catalog_sha256,
        revision=revision, operator=operator, apply=apply, backup_path=backup_path,
        read_package=lambda *_: document, check_schema=check_schema, read_release=read_release,
        install_release=install_release, tables=TABLES, release_table='hha_geography_releases')
    report.update(countyRows=report['rowCount'], newCountyRows=report['newRows'],
        verifiedCountyRows=sum(row['status'] == 'Verified' for row in document['entries'].values()),
        conflictCounties=[county for county,row in document['entries'].items() if row['status'] == 'SourceConflict'])
    return report
