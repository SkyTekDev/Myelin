"""Shared preview/backup/append engine for reviewed Myelin reference releases."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


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


def import_versioned_catalog(catalog_path, database, *, expected_catalog_sha256, revision, operator,
        apply, backup_path, read_package, check_schema, read_release, install_release, tables, release_table):
    catalog_path, database = Path(catalog_path).resolve(), Path(database).resolve()
    document = read_package(catalog_path, expected_catalog_sha256)
    if type(revision) is not int or revision <= 0 or not isinstance(operator, str) or not operator.strip() or len(operator) > 128:
        raise ValueError('A positive revision and operator name are required.')
    if not database.is_file() or database == catalog_path:
        raise ValueError('Select an existing Myelin SQLite database.')
    before_hash = file_sha256(database)
    report = dict(status='Preview', applied=False, safeToApply=True, paymentYear=document['paymentYear'], revision=revision,
        catalogSha256=expected_catalog_sha256, rowCount=len(document['entries']), databasePath=str(database),
        databaseSha256Before=before_hash, schemaCreated=False, newReleases=0, newRows=0)
    table = '"' + release_table.replace('"', '""') + '"'
    with closing(sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=30, isolation_level=None)) as connection:
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ipsf'").fetchone():
                raise ValueError('The target is not a prepared Myelin provider database.')
            exists = check_schema(connection)
            if exists:
                collisions = connection.execute('SELECT catalog_sha256,payment_year,revision FROM ' + table +
                    ' WHERE catalog_sha256=? OR (payment_year=? AND revision=?)',
                    (expected_catalog_sha256, document['paymentYear'], revision)).fetchall()
                if collisions:
                    if collisions != [(expected_catalog_sha256, document['paymentYear'], revision)]:
                        raise ValueError('An immutable year/revision or catalog hash already identifies another release.')
                    restored, _ = read_release(connection, expected_catalog_sha256)
                    if restored != document:
                        raise ValueError('Existing release differs from the reviewed package.')
                    report['status'] = 'AlreadyLoaded'
            report['newReleases'] = int(report['status'] != 'AlreadyLoaded')
            report['newRows'] = len(document['entries']) * report['newReleases']
            if apply and report['newReleases']:
                if backup_path is None:
                    raise ValueError('Applying a new release requires a fresh complete backup path.')
                backup = Path(backup_path).resolve()
                if backup in {database, catalog_path}:
                    raise ValueError('Backup must be separate from the database and catalog.')
                before_all = table_fingerprints(connection)
                preserved = {name: value for name, value in before_all.items() if name not in tables}
                with backup.open('xb'):
                    pass
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if table_fingerprints(target) != before_all or target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('The complete pre-load backup could not be verified.')
                old_releases = [row[0] for row in connection.execute('SELECT catalog_sha256 FROM ' + table)] if exists else []
                old_documents = {digest: read_release(connection, digest)[0] for digest in old_releases}
                install_release(connection, document, expected_catalog_sha256, revision, operator)
                if (table_fingerprints(connection, tables) != preserved
                        or any(read_release(connection, digest)[0] != old for digest, old in old_documents.items())
                        or connection.execute('PRAGMA foreign_key_check').fetchone()
                        or connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'):
                    raise ValueError('Reference import preservation or integrity check failed.')
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
