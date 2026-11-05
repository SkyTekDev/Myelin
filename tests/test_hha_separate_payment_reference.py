"""Separate-payment DB versions, source checksums and safe reusable imports."""
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
from bridge.hha_provider import HhaInputError
from bridge.hha_provider_annual import canonical_json
from bridge.hha_separate_payment_rates import CATALOG_FILENAME, CATALOG_SHA256, read_package, load_rates
from bridge.hha_separate_payment_reference import import_catalog, read_release, schema_statements, TABLES
from bridge.immutable_reference import file_sha256, table_fingerprints

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'bridge/data'/CATALOG_FILENAME


class SeparatePaymentReferenceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.database=self.root/'myelin.db'
        with closing(sqlite3.connect(self.database)) as c:
            c.execute('CREATE TABLE ipsf (id INTEGER PRIMARY KEY, provider_ccn TEXT)')
            c.execute("INSERT INTO ipsf VALUES(1,'017009')")
            c.execute('CREATE TABLE existing_reference (value TEXT)')
            c.execute("INSERT INTO existing_reference VALUES('preserve byte for byte')"); c.commit()
        self.engine=create_engine('sqlite:///'+self.database.as_posix()); self.addCleanup(self.engine.dispose)

    def invoke(self,**options):
        args=dict(expected_catalog_sha256=CATALOG_SHA256,revision=1,operator='isolated-test')
        args.update(options); package=args.pop('catalog',PACKAGE)
        return import_catalog(package,self.database,**args)

    def apply(self,name='backup.db',**options):
        return self.invoke(apply=True,backup_path=self.root/name,**options)

    def test_preview_apply_complete_backup_exact_repeat(self):
        before=file_sha256(self.database)
        self.assertEqual(self.invoke()['newRateRows'],1)
        self.assertEqual(file_sha256(self.database),before)
        with closing(sqlite3.connect(self.database)) as c: old=table_fingerprints(c)
        result=self.apply(); self.assertEqual(result['status'],'Applied'); self.assertTrue(result['completeBackupVerified'])
        with closing(sqlite3.connect(self.root/'backup.db')) as c: self.assertEqual(table_fingerprints(c),old)
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(table_fingerprints(c,TABLES),old)
            doc,release=read_release(c,CATALOG_SHA256)
            self.assertEqual(canonical_json(doc),PACKAGE.read_bytes()); self.assertEqual(release['loaded_by'],'isolated-test')
        after=file_sha256(self.database)
        self.assertEqual(self.invoke(apply=True)['status'],'AlreadyLoaded')
        self.assertEqual(file_sha256(self.database),after)
        proof=load_rates(self.engine).a9272_evidence()
        self.assertEqual(proof['unitRate'],'282.10'); self.assertEqual(proof['revision'],1)

    def test_retained_versions_do_not_activate_and_cannot_overwrite(self):
        self.apply(); expected=load_rates(self.engine).a9272_evidence()
        for year,revision in [(2026,2),(2027,1)]:
            doc=read_package(PACKAGE,CATALOG_SHA256); doc.update(paymentYear=year,effectiveFrom=f'{year}-01-01',effectiveThrough=f'{year}-12-31')
            doc['entries']['A9272']['unitRate']='999.00'
            raw=canonical_json(doc); digest=hashlib.sha256(raw).hexdigest(); package=self.root/f'{year}.json'; package.write_bytes(raw)
            if year==2026:
                with self.assertRaisesRegex(ValueError,'immutable'): self.apply('conflict.db',catalog=package,expected_catalog_sha256=digest)
            self.assertTrue(self.apply(f'{year}.db',catalog=package,expected_catalog_sha256=digest,revision=revision)['existingReleasesUnchanged'])
        self.assertEqual(load_rates(self.engine).a9272_evidence(),expected)
        with self.assertRaises(HhaInputError): load_rates(self.engine,2027)

    def test_failed_verification_rolls_back_and_requires_fresh_backup(self):
        before=file_sha256(self.database)
        with self.assertRaisesRegex(ValueError,'backup'): self.invoke(apply=True)
        with patch('bridge.hha_separate_payment_reference.read_release',side_effect=ValueError('injected failure')):
            with self.assertRaisesRegex(ValueError,'injected'): self.apply()
        self.assertEqual(file_sha256(self.database),before)
        with self.assertRaises(FileExistsError): self.apply()

    def test_sealed_rows_and_metadata_are_immutable(self):
        self.apply()
        with closing(sqlite3.connect(self.database)) as c:
            for sql in ["UPDATE hha_payment_rates SET unit_rate_exact='1.00'",'DELETE FROM hha_payment_rates',
                'DELETE FROM hha_rate_releases','UPDATE hha_rate_releases SET revision=2',
                "INSERT INTO hha_payment_rates SELECT catalog_sha256,'A9999',unit_rate_exact,coinsurance_rate_exact,annual_part_b_deductible_exact,entry_sha256 FROM hha_payment_rates"]:
                with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'): c.execute(sql)
                c.rollback()

    def test_missing_or_tampered_db_cannot_fall_back_to_json(self):
        with patch('bridge.hha_separate_payment_rates.read_package',side_effect=AssertionError('file access')):
            with self.assertRaises(HhaInputError): load_rates(self.engine)
        self.apply()
        with closing(sqlite3.connect(self.database)) as c:
            trigger=next(sql for sql in schema_statements() if sql.startswith('CREATE TRIGGER hha_rate_update '))
            c.execute('DROP TRIGGER hha_rate_update'); c.execute("UPDATE hha_payment_rates SET unit_rate_exact='1.00'")
            c.execute(trigger); c.commit()
        with self.assertRaises(HhaInputError): load_rates(self.engine)

    def test_cached_snapshot_has_no_query_or_file_reads(self):
        self.apply(); rates=load_rates(self.engine); expected=rates.a9272_evidence()
        def deny(*args): raise AssertionError('unexpected DB query')
        event.listen(self.engine,'before_cursor_execute',deny)
        self.addCleanup(lambda:event.remove(self.engine,'before_cursor_execute',deny))
        with patch('pathlib.Path.read_bytes',side_effect=AssertionError('unexpected file read')):
            self.assertEqual(rates.a9272_evidence(),expected)
        changed=rates.a9272_evidence(); changed['sources'].clear()
        self.assertEqual(rates.a9272_evidence(),expected)

    def test_concurrent_imports_install_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda n:self.apply(f'{n}.db'),(1,2)))
        self.assertEqual(sorted(result['status'] for result in results),['AlreadyLoaded','Applied'])

    def test_cli_preview_apply_repeat_reports_are_not_overwritten(self):
        command=[sys.executable,'-B',str(ROOT/'scripts/import_hha_separate_payment_rates.py'),'--catalog',str(PACKAGE),
            '--database',str(self.database),'--expected-catalog-sha256',CATALOG_SHA256,'--revision','1']
        for phase,options,status in [('preview',[],'Preview'),('apply',['--apply','--backup',str(self.root/'cli.db')],'Applied'),('repeat',['--apply'],'AlreadyLoaded')]:
            report=self.root/f'{phase}.json'
            result=subprocess.run(command+options+['--report',str(report)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(json.loads(report.read_text())['status'],status)
        before=file_sha256(self.database); report=self.root/'apply.json'; digest=file_sha256(report)
        result=subprocess.run(command+['--apply','--report',str(report)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0); self.assertEqual(file_sha256(report),digest); self.assertEqual(file_sha256(self.database),before)


if __name__=='__main__': unittest.main()
