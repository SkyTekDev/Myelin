"""DB loader safety, provenance, version retention and cached runtime reads."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event

from bridge.hha_geography_catalog import CATALOG_FILENAME, CATALOG_SHA256, load_catalog, read_catalog_package
from bridge.hha_geography_reference import import_catalog, file_sha256, read_release, schema_statements, table_fingerprints
from bridge.hha_provider import HhaInputError
from bridge.hha_provider_annual import canonical_json

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'bridge/data' / CATALOG_FILENAME


class GeographyReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'myelin.db'
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('CREATE TABLE ipsf (id INTEGER PRIMARY KEY,provider_ccn TEXT,factor REAL)')
            connection.execute("INSERT INTO ipsf VALUES (1,'017009',1.00487)")
            connection.execute('CREATE TABLE unrelated (value TEXT)')
            connection.execute("INSERT INTO unrelated VALUES ('preserve exactly')")
            connection.commit()
        self.engine = create_engine('sqlite:///' + self.database.as_posix())
        self.addCleanup(self.engine.dispose)

    def invoke(self, **options):
        settings = dict(expected_catalog_sha256=CATALOG_SHA256, revision=1, operator='isolated-test')
        settings.update(options)
        catalog = settings.pop('catalog', PACKAGE)
        return import_catalog(catalog, self.database, **settings)

    def apply(self, name='backup.db', **options):
        return self.invoke(apply=True, backup_path=self.root / name, **options)

    def fixture(self, name, *, year=2026, change=True):
        # Fabricated import packages test storage/versioning only. They are
        # never registered as reviewed runtime payment references.
        doc = read_catalog_package(PACKAGE, CATALOG_SHA256)
        doc['paymentYear'] = year
        doc['effectiveFrom'], doc['effectiveThrough'] = f'{year}-01-01', f'{year}-12-31'
        if change:
            doc['entries']['01023']['countyName'] = 'SYNTHETIC TEST REVISION'
        raw = canonical_json(doc)
        path = self.root / name
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_preview_is_read_only_and_does_not_create_schema(self):
        self.assertEqual(file_sha256(PACKAGE), CATALOG_SHA256)
        before = file_sha256(self.database)
        result = self.invoke()
        self.assertEqual(result['status'], 'Preview')
        self.assertEqual(result['newCountyRows'], 3222)
        self.assertEqual(result['verifiedCountyRows'], 3221)
        self.assertEqual(result['conflictCounties'], ['37167'])
        self.assertEqual(file_sha256(self.database), before)
        with closing(sqlite3.connect(self.database)) as c:
            self.assertFalse(c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'hha_geography_%'").fetchall())

    def test_apply_reconstructs_exact_package_and_preserves_complete_backup(self):
        with closing(sqlite3.connect(self.database)) as c:
            before = table_fingerprints(c)
        result = self.apply()
        self.assertEqual(result['status'], 'Applied')
        self.assertTrue(result['completeBackupVerified'])
        self.assertTrue(result['schemaCreated'])
        with closing(sqlite3.connect(self.root / 'backup.db')) as c:
            self.assertEqual(table_fingerprints(c), before)
        with closing(sqlite3.connect(self.database)) as c:
            doc, release = read_release(c, CATALOG_SHA256)
            self.assertEqual(canonical_json(doc), PACKAGE.read_bytes())
            self.assertEqual(release['loaded_by'], 'isolated-test')
            self.assertEqual(release['revision'], 1)
            self.assertEqual(table_fingerprints(c, {'hha_geography_releases','hha_geography_counties'}), before)
        catalog = load_catalog(self.engine)
        self.assertEqual(catalog.document, read_catalog_package(PACKAGE, CATALOG_SHA256))
        self.assertEqual(catalog.mapping_evidence('01023')['catalogSha256'], CATALOG_SHA256)
        with self.assertRaises(HhaInputError) as caught:
            catalog.county_entry('37167')
        self.assertEqual(caught.exception.code, 'HHA_GEOGRAPHY_SOURCE_CONFLICT')

    def test_exact_repeated_load_is_byte_identical_without_another_backup(self):
        self.apply()
        before = file_sha256(self.database)
        for apply in (False, True):
            result = self.invoke(apply=apply)
            self.assertEqual(result['status'], 'AlreadyLoaded')
            self.assertFalse(result['applied'])
            self.assertEqual(result['newCountyRows'], 0)
            self.assertEqual(file_sha256(self.database), before)

    def test_additional_revision_and_year_preserve_original_and_do_not_activate(self):
        self.apply()
        proof = load_catalog(self.engine).mapping_evidence('01023')
        for year, revision in [(2026, 2), (2027, 1)]:
            package, digest = self.fixture(f'{year}.json', year=year)
            result = self.apply(f'{year}.db', catalog=package, expected_catalog_sha256=digest, revision=revision)
            self.assertTrue(result['existingReleasesUnchanged'])
        self.assertEqual(load_catalog(self.engine).mapping_evidence('01023'), proof)
        with self.assertRaises(HhaInputError):
            load_catalog(self.engine, 2027)
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM hha_geography_releases').fetchone()[0], 3)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM hha_geography_counties').fetchone()[0], 9666)

    def test_conflicting_year_revision_or_hash_cannot_overwrite(self):
        self.apply()
        package, digest = self.fixture('conflict.json')
        before = file_sha256(self.database)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.apply('conflict.db', catalog=package, expected_catalog_sha256=digest)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.apply('hash.db', revision=2)
        self.assertEqual(file_sha256(self.database), before)
        self.assertFalse((self.root / 'conflict.db').exists())

    def test_new_load_requires_fresh_backup(self):
        before = file_sha256(self.database)
        with self.assertRaisesRegex(ValueError, 'backup'):
            self.invoke(apply=True)
        taken = self.root / 'taken.db'
        taken.write_bytes(b'preserve this backup')
        with self.assertRaises(FileExistsError):
            self.invoke(apply=True, backup_path=taken)
        self.assertEqual(taken.read_bytes(), b'preserve this backup')
        self.assertEqual(file_sha256(self.database), before)

    def test_failed_import_rolls_back_schema_and_all_rows(self):
        before = file_sha256(self.database)
        with patch('bridge.hha_geography_reference.read_release', side_effect=ValueError('injected verification failure')):
            with self.assertRaisesRegex(ValueError, 'injected'):
                self.apply()
        self.assertEqual(file_sha256(self.database), before)
        with closing(sqlite3.connect(self.database)) as original, closing(sqlite3.connect(self.root / 'backup.db')) as backup:
            self.assertEqual(table_fingerprints(backup), table_fingerprints(original))
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        with self.assertRaises(HhaInputError):
            load_catalog(self.engine)

    def test_missing_tables_never_fall_back_to_existing_json_file(self):
        self.assertTrue(PACKAGE.exists())
        with patch('bridge.hha_geography_catalog.read_catalog_package', side_effect=AssertionError('runtime file access')):
            with self.assertRaises(HhaInputError) as caught:
                load_catalog(self.engine)
        self.assertEqual(caught.exception.code, 'HHA_GEOGRAPHY_REFERENCE_INVALID')

    def test_cached_snapshot_does_not_query_db_or_read_package(self):
        self.apply()
        catalog = load_catalog(self.engine)
        expected = catalog.mapping_evidence('01023')
        def deny(*args):
            raise AssertionError('Unexpected database query')
        event.listen(self.engine, 'before_cursor_execute', deny)
        try:
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('Unexpected file read')):
                self.assertEqual(catalog.mapping_evidence('01023'), expected)
                changed = catalog.document
                changed['entries']['01023']['wageIndex'] = '9.9999'
                self.assertEqual(catalog.mapping_evidence('01023'), expected)
        finally:
            event.remove(self.engine, 'before_cursor_execute', deny)

    def test_sealed_versions_reject_sql_updates_deletes_and_extra_counties(self):
        self.apply()
        statements = [
            "UPDATE hha_geography_releases SET revision=2",
            'DELETE FROM hha_geography_releases',
            "UPDATE hha_geography_counties SET wage_index_exact='9.9999'",
            'DELETE FROM hha_geography_counties',
            'INSERT INTO hha_geography_counties SELECT * FROM hha_geography_counties LIMIT 1',
        ]
        with closing(sqlite3.connect(self.database)) as c:
            for statement in statements:
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                    c.execute(statement)
                c.rollback()

    def test_tampered_rows_or_metadata_are_rejected_even_with_original_schema(self):
        self.apply()
        for table, trigger, column, value, where in [
            ('hha_geography_counties', 'hha_geography_county_update', 'wage_index_exact', '9.9999', " WHERE county_fips='01023'"),
            ('hha_geography_releases', 'hha_geography_release_update', 'sources_json', '{}', ''),
        ]:
            with self.subTest(table=table), closing(sqlite3.connect(self.database)) as c:
                original = c.execute('SELECT ' + column + ' FROM ' + table + where).fetchone()[0]
                create_trigger = next(sql for sql in schema_statements() if sql.startswith('CREATE TRIGGER ' + trigger + ' '))
                c.execute('DROP TRIGGER ' + trigger)
                c.execute('UPDATE ' + table + ' SET ' + column + '=?' + where, (value,))
                c.execute(create_trigger)
                c.commit()
                with self.assertRaises(HhaInputError):
                    load_catalog(self.engine)
                c.execute('DROP TRIGGER ' + trigger)
                c.execute('UPDATE ' + table + ' SET ' + column + '=?' + where, (original,))
                c.execute(create_trigger)
                c.commit()

    def test_incomplete_schema_is_refused_without_repair_or_data_writes(self):
        with closing(sqlite3.connect(self.database)) as c:
            c.execute('CREATE TABLE hha_geography_releases (wrong_column TEXT)')
            c.commit()
        before = file_sha256(self.database)
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.apply()
        self.assertEqual(file_sha256(self.database), before)

    def test_invalid_package_or_missing_database_cannot_write(self):
        before = file_sha256(self.database)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.apply(expected_catalog_sha256='0' * 64)
        self.assertEqual(file_sha256(self.database), before)
        self.engine.dispose()
        self.database = self.root / 'does-not-exist.db'
        with self.assertRaisesRegex(ValueError, 'existing'):
            self.apply()
        self.assertFalse(self.database.exists())

    def test_two_concurrent_imports_produce_one_immutable_release(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n: self.apply(f'concurrent-{n}.db'), (1, 2)))
        self.assertEqual(sorted(row['status'] for row in results), ['AlreadyLoaded','Applied'])
        self.assertEqual(sum(path.exists() for path in [self.root/'concurrent-1.db', self.root/'concurrent-2.db']), 1)

    def test_cli_retains_preview_apply_repeat_reports_and_never_overwrites_them(self):
        base = [sys.executable, '-B', str(ROOT / 'scripts/import_hha_geography.py'), '--catalog', str(PACKAGE),
            '--database', str(self.database), '--expected-catalog-sha256', CATALOG_SHA256, '--revision', '1']
        for phase, options, status in [('preview', [], 'Preview'),
            ('apply', ['--apply','--backup',str(self.root/'cli-backup.db')], 'Applied'), ('repeat',['--apply'],'AlreadyLoaded')]:
            report = self.root / f'{phase}.json'
            result = subprocess.run(base + options + ['--report',str(report)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text())['status'], status)
        before = file_sha256(self.database)
        report_hash = file_sha256(self.root / 'apply.json')
        result = subprocess.run(base + ['--apply','--report',str(self.root/'apply.json')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(file_sha256(self.root / 'apply.json'), report_hash)
        self.assertEqual(file_sha256(self.database), before)


if __name__ == '__main__':
    unittest.main()
