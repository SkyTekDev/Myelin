"""Transactional, append-only geography imports into the Myelin SQLite DB."""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from bridge.hha_geography_catalog import read_catalog_package, validate_document
from bridge.hha_provider_annual import canonical_json

SCHEMA_PATH = Path(__file__).parent / 'references/hha-geography-schema-v1.sql'
TABLES = {'hha_geography_releases', 'hha_geography_counties'}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


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


def table_fingerprints(connection, exclude=()):
    result = {}
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table in exclude:
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        digest, count = hashlib.sha256(), 0
        for row in connection.execute('SELECT * FROM ' + quoted + ' ORDER BY rowid'):
            digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':'), default=lambda value: value.hex()).encode() + b'\n')
            count += 1
        result[table] = dict(rows=count, sha256=digest.hexdigest())
    return result


def import_catalog(catalog_path, database, *, expected_catalog_sha256, revision, operator,
                   apply=False, backup_path=None):
    catalog_path, database = Path(catalog_path).resolve(), Path(database).resolve()
    document = read_catalog_package(catalog_path, expected_catalog_sha256)
    if type(revision) is not int or revision <= 0 or not isinstance(operator, str) or not operator.strip() or len(operator) > 128:
        raise ValueError('A positive revision and operator name are required.')
    if not database.is_file() or database == catalog_path:
        raise ValueError('Select an existing Myelin SQLite database.')
    before_hash = file_sha256(database)
    report = dict(status='Preview', applied=False, safeToApply=True, paymentYear=document['paymentYear'], revision=revision,
        catalogSha256=expected_catalog_sha256, countyRows=len(document['entries']),
        verifiedCountyRows=sum(row['status'] == 'Verified' for row in document['entries'].values()),
        conflictCounties=[county for county, row in document['entries'].items() if row['status'] == 'SourceConflict'],
        databasePath=str(database), databaseSha256Before=before_hash, schemaCreated=False, newReleases=0, newCountyRows=0)
    with closing(sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=30, isolation_level=None)) as connection:
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ipsf'").fetchone():
                raise ValueError('The target is not a prepared Myelin provider database.')
            exists = check_schema(connection)
            if exists:
                collisions = connection.execute('SELECT catalog_sha256,payment_year,revision FROM hha_geography_releases '
                    'WHERE catalog_sha256=? OR (payment_year=? AND revision=?)',
                    (expected_catalog_sha256, document['paymentYear'], revision)).fetchall()
                if collisions:
                    if collisions != [(expected_catalog_sha256, document['paymentYear'], revision)]:
                        raise ValueError('An immutable year/revision or catalog hash already identifies another release.')
                    restored, _ = read_release(connection, expected_catalog_sha256)
                    if canonical_json(restored) != canonical_json(document):
                        raise ValueError('Existing geography release differs from the reviewed package.')
                    report['status'] = 'AlreadyLoaded'
            report['newReleases'] = int(report['status'] != 'AlreadyLoaded')
            report['newCountyRows'] = len(document['entries']) * report['newReleases']
            if apply and report['newReleases']:
                if backup_path is None:
                    raise ValueError('Applying a new release requires a fresh complete backup path.')
                backup = Path(backup_path).resolve()
                if backup in {database, catalog_path}:
                    raise ValueError('Backup must be separate from the database and catalog.')
                before_all = table_fingerprints(connection)
                preserved = {table: value for table, value in before_all.items() if table not in TABLES}
                with backup.open('xb'):
                    pass
                # Another read connection can back up the pre-write snapshot
                # while BEGIN IMMEDIATE excludes concurrent writers.
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if table_fingerprints(target) != before_all or target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('The complete pre-load backup could not be verified.')
                old_releases = [row[0] for row in connection.execute('SELECT catalog_sha256 FROM hha_geography_releases')] if exists else []
                old_documents = {digest: canonical_json(read_release(connection, digest)[0]) for digest in old_releases}
                install_release(connection, document, expected_catalog_sha256, revision, operator)
                if (table_fingerprints(connection, TABLES) != preserved
                        or any(canonical_json(read_release(connection, digest)[0]) != raw for digest, raw in old_documents.items())
                        or connection.execute('PRAGMA foreign_key_check').fetchone()
                        or connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'):
                    raise ValueError('Geography import preservation or integrity check failed.')
                report.update(status='Applied', applied=True, schemaCreated=not exists, unrelatedTablesUnchanged=True,
                    existingReleasesUnchanged=True, preservedTableFingerprints=preserved,
                    backupPath=str(backup), backupSha256=file_sha256(backup), completeBackupVerified=True)
                connection.commit()
            else:
                connection.rollback()
        except BaseException:
            connection.rollback()
            raise
    report['databaseSha256After'] = file_sha256(database)
    return report
