"""Preparation/import integrity checks with fabricated test-only CMS-shaped files."""

from contextlib import closing
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from datetime import date
from sqlalchemy import create_engine
from bridge.hha_provider import HhaInputError, provider_for_period

from reportlab.pdfgen import canvas

from bridge.hha_provider_annual import CATALOG_FILENAME, canonical_json, load_catalog
from bridge.hha_provider_reference import (
    SOURCE_SCHEMA, file_sha256, import_reference_package, prepare_reference_package,
    provider_csv_header, read_provider_history,
    read_hhvbp_source, normalized_provider_record, provider_reference_row_sha256,
    CONFLICT_REVIEW_SCHEMA,
)
from myelin.pricers.ipsf import DATATYPES


def retained_fixture(root):
    """No fabricated artifact from this helper is a production reference."""
    sources = []
    titles = [("Compliant", "CY 2026 HHAs Compliant for APU", "compliant", ["017999", "017996"]),
              ("NonCompliant", "CY 2026 HHAs Non-Compliant for APU", "non-compliant", ["017998"]),
              ("Excluded", "CY 2026 HHAs Excluded from APU", "excluded", ["017997"])]
    for classification, title, file_kind, ccns in titles:
        path = root / f"test-only-{classification}.pdf"
        document = canvas.Canvas(str(path))
        document.drawString(50, 800, title)
        document.drawString(50, 780, "CCN Facility Name City State")
        for number, ccn in enumerate(ccns):
            document.drawString(50, 760 - number * 20, ccn + " FABRICATED TEST AGENCY TEST CITY AL")
        document.save()
        sources.append(dict(classification=classification, file=path.name,
            sourceUrl=f"https://www.cms.gov/files/document/pac-hha-cy2026-{file_kind}-apu-20260316.pdf",
            sourceSha256=file_sha256(path), publishedDate="2026-03-16"))
    vbp_path = root / "test-only-hhvbp.csv"
    with vbp_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["CMS Certification Number (CCN)", "Payment Adjustment Year", "Adjusted Payment Percentage"])
        writer.writerows([["017999", "2025", "1.000%"], ["017999", "2026", "2.345%"],
                         ["017998", "2026", "-3.000%"], ["017997", "2026", "0.000%"],
                         ["017996", "2026", "0.000%"]])
    ccns = ["017996", "017997", "017998", "017999"]
    url = "https://pds.mps.cms.gov/fiss/v2/inpatient/export?" + urlencode(dict(
        fromDate="1980-01-01", toDate="2026-11-01", oscarNumber=ccns, format="csv"), doseq=True)
    records = []
    for ccn, effective, qrp, factor, provider_type in [
        ("017999", 20230101, "0", "1.02345", "36"),
        ("017999", 20240101, "0", "1.02345", "36"),
        ("017999", 20260701, "0", "1.02345", "38"),
        ("017998", 20250101, "2", "0.97000", "36"),
        ("017997", 20250101, "0", "1.00000", "36"),
        ("017996", 20250101, "0", "1.00000", "36"),
        ("017996", 20260101, "0", "1.00000", "36"),
    ]:
        record = dict.fromkeys(DATATYPES, "")
        record.update(provider_ccn=ccn, effective_date=effective, provider_type=provider_type,
                      federal_pps_blend=qrp, special_provider_update_factor=factor, export_date=20261101)
        records.append(record)
    history_path = root / "test-only-provider-history.csv"
    write_history(history_path, records)
    manifest = dict(schemaVersion=SOURCE_SCHEMA, paymentYear=2026, qualityReportingSources=sources,
        valueBasedPurchasingSource=dict(file=vbp_path.name,
            sourceUrl="https://data.cms.gov/provider-data/sites/default/files/resources/test-only-hhvbp.csv",
            sourceSha256=file_sha256(vbp_path), publishedDate="2026-01-14", datasetIdentifier="56d7-4994"),
        providerHistorySources=[dict(file=history_path.name, sourceUrl=url,
            sourceSha256=file_sha256(history_path), cutoffDate="2026-11-01")])
    manifest_path = root / "test-only-sources.json"
    write_manifest(manifest_path, manifest)
    return manifest_path, manifest, records


def write_history(path, records):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(provider_csv_header())
        writer.writerows([[record[key] for key in DATATYPES] for record in records])


def write_manifest(path, manifest):
    path.write_bytes(canonical_json(manifest))


class ProviderReferencePackageChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest_path, self.manifest, self.records = retained_fixture(self.root)
        self.output = self.root / "prepared"
        self.database = self.root / "reference.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE ipsf(id INTEGER PRIMARY KEY AUTOINCREMENT," +
                ",".join(key + " " + metadata["type"] for key, metadata in DATATYPES.items()) + ")")
            connection.execute("INSERT INTO ipsf(provider_ccn,effective_date,provider_type) VALUES('999999',20260101,'37')")
            connection.execute("CREATE TABLE opsf(id INTEGER PRIMARY KEY,marker TEXT)")
            connection.execute("INSERT INTO opsf VALUES(1,'retain unrelated provider facts')")

    def prepare(self):
        self.report = prepare_reference_package(self.manifest_path, self.output)
        self.sha = self.report["catalogSha256"]
        return self.report

    def import_package(self, **options):
        return import_reference_package(self.output, self.database,
            expected_catalog_sha256=self.sha, **options)

    def count(self, table="ipsf"):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def add_conflicting_source_row(self, ccn="017998", effective=20250101, **changes):
        # Read the normalized prepared CSV rather than retain the test generator's blanks.
        manifest = json.loads((self.output / "sources-manifest.json").read_bytes())
        rows = read_provider_history(self.output, manifest["providerHistorySources"][0])["records"]
        values = normalized_provider_record(next(item["record"] for item in rows if item["record"]["provider_ccn"] == ccn and item["record"]["effective_date"] == effective))
        values.update(county_code="OLD", export_date=20250101, **changes)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            return connection.execute("INSERT INTO ipsf (" + ",".join(DATATYPES) + ") VALUES (" + ",".join("?" for _ in DATATYPES) + ")", tuple(values[key] for key in DATATYPES)).lastrowid

    def reviewed_conflicts(self):
        manifest = json.loads((self.output / "sources-manifest.json").read_bytes())
        rows = [item for source in manifest["providerHistorySources"] for item in read_provider_history(self.output, source)["records"]]
        approved = []
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            for item in rows:
                values = normalized_provider_record(item["record"])
                found = connection.execute("SELECT * FROM ipsf WHERE provider_ccn=? AND effective_date=?", (values["provider_ccn"], values["effective_date"])).fetchall()
                if not found or all(found[0][key] == value for key,value in values.items() if key != "export_date"):
                    continue
                old = dict(found[0])
                approved.append(dict(providerCcn=values["provider_ccn"], effectiveDate=values["effective_date"],providerRowId=old["id"],
                    existingRowSha256=provider_reference_row_sha256(old),sourceRowSha256=provider_reference_row_sha256(values),
                    providerHistorySource=item["source"],changes={key:dict(before=old[key],after=value) for key,value in values.items() if old[key]!=value}))
        package = json.loads((self.output / "package-manifest.json").read_bytes())
        return dict(schemaVersion=CONFLICT_REVIEW_SCHEMA,paymentYear=2026,catalogSha256=self.sha,
            packageManifestSha256=file_sha256(self.output / "package-manifest.json"),sourcesManifestSha256=package["sourcesManifestSha256"],
            databaseSha256=file_sha256(self.database),approvedRows=approved)

    def review_options(self, document, name="review.json"):
        path = self.root/name
        path.write_bytes(canonical_json(document))
        return dict(conflict_review_path=path,expected_conflict_review_sha256=file_sha256(path))

    @staticmethod
    def snapshot(path):
        with closing(sqlite3.connect(path)) as connection:
            return tuple(connection.iterdump())

    def test_real_pdf_csv_parsers_prepare_only_supported_verified_older_entries(self):
        report = self.prepare()
        catalog = load_catalog(self.output / CATALOG_FILENAME, self.sha).document
        self.assertEqual(set(catalog["entries"]), {"017998", "017999"})
        self.assertEqual(catalog["entries"]["017998"]["qualityReportingProgramIndicator"], "2")
        self.assertEqual(catalog["entries"]["017998"]["vbpAdjustment"], "0.97")
        self.assertEqual(catalog["entries"]["017999"]["providerNextEffectiveDate"], "2026-07-01")
        self.assertEqual(report["providerTypeCounts"], {"36": 6, "38": 1})
        states = {row["providerCcn"]: row["status"] for row in report["readiness"]}
        self.assertEqual(states["017997"], "ExcludedAnnualQrp")
        self.assertEqual(states["017996"], "Existing2026ProviderRecord")
        self.assertFalse(report["originalProviderDatabaseAccessed"])

    def test_preview_is_readonly_then_apply_is_additive_backed_up_and_idempotent(self):
        self.prepare()
        original = file_sha256(self.database)
        original_dump = self.snapshot(self.database)
        preview = self.import_package()
        self.assertEqual((preview["rowsBefore"], preview["rowsToInsert"], preview["rowsAfter"]), (1, 7, 1))
        self.assertEqual(file_sha256(self.database), original)
        backup = self.root / "backup.db"
        applied = self.import_package(apply=True, backup_path=backup)
        self.assertEqual((applied["rowsBefore"], applied["rowsAfter"]), (1, 8))
        self.assertEqual(self.snapshot(backup), original_dump)
        self.assertTrue(applied["completeBackupVerified"])
        self.assertTrue(applied["existingProviderRowsUnchanged"] and applied["otherTablesUnchanged"])
        self.assertEqual(self.count("opsf"), 1)
        self.assertEqual(self.import_package()["rowsToInsert"], 0)

    def test_same_reviewed_source_bytes_produce_the_same_catalog_independent_of_output_path(self):
        self.prepare()
        second = self.root / "second-prepared"
        report = prepare_reference_package(self.manifest_path, second)
        self.assertEqual(report["catalogSha256"], self.sha)
        self.assertEqual((second / CATALOG_FILENAME).read_bytes(), (self.output / CATALOG_FILENAME).read_bytes())

    def test_source_hash_tampering_and_rebound_manifest_cannot_bypass_catalog_pin(self):
        self.prepare()
        manifest_path = self.output / "sources-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        path = self.output / manifest["providerHistorySources"][0]["file"]
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.import_package()
        manifest["providerHistorySources"][0]["sourceSha256"] = file_sha256(path)
        write_manifest(manifest_path, manifest)
        package_path = self.output / "package-manifest.json"
        package = json.loads(package_path.read_bytes())
        package["sourcesManifestSha256"] = file_sha256(manifest_path)
        write_manifest(package_path, package)
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.import_package()
        self.assertEqual(self.count(), 1)

    def test_existing_fact_conflict_rolls_back_and_keeps_complete_verified_backup(self):
        self.prepare()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO ipsf(provider_ccn,effective_date,provider_type,special_provider_update_factor) VALUES('017998',20250101,'36',0.98)")
        original = file_sha256(self.database)
        original_dump = self.snapshot(self.database)
        backup = self.root / "conflict-backup.db"
        with self.assertRaisesRegex(ValueError, "facts conflict"):
            self.import_package(apply=True, backup_path=backup)
        self.assertEqual(self.count(), 2)
        self.assertEqual(file_sha256(self.database), original)
        self.assertEqual(self.snapshot(backup), original_dump)

    def test_failure_after_partial_batch_inserts_is_atomic(self):
        self.prepare()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TRIGGER test_import_abort BEFORE INSERT ON ipsf WHEN NEW.provider_ccn='017998' BEGIN SELECT RAISE(ABORT,'test-only batch failure'); END")
        original = file_sha256(self.database)
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_package(apply=True, backup_path=self.root / "atomic-backup.db")
        self.assertEqual(self.count(), 1)
        self.assertEqual(file_sha256(self.database), original)

    def test_apply_requires_new_distinct_backup_and_never_reuses_it(self):
        self.prepare()
        for backup in [None, self.database]:
            with self.subTest(backup=backup), self.assertRaises(ValueError):
                self.import_package(apply=True, backup_path=backup)
        path = self.root / "existing-backup.db"
        path.write_bytes(b"preserved old backup")
        with self.assertRaises(FileExistsError):
            self.import_package(apply=True, backup_path=path)
        self.assertEqual(path.read_bytes(), b"preserved old backup")
        self.assertEqual(self.count(), 1)

    def test_explicit_import_subset_keeps_complete_history_and_rejects_unknown_scope(self):
        self.prepare()
        preview = self.import_package(provider_ccns=["017999"])
        self.assertEqual(preview["selectedSourceRows"], 3)
        self.assertEqual(preview["verifiedSourceRows"], 7)
        self.assertEqual(preview["requestedCcns"], ["017999"])
        applied = self.import_package(provider_ccns=["017999"], apply=True, backup_path=self.root / "subset-backup.db")
        self.assertEqual(applied["rowsAfter"], 4)
        self.assertEqual(applied["providerTypeCounts"], {"36": 2, "38": 1})
        for cohort in [[], ["017999", "017999"], ["000001"], [False]]:
            with self.subTest(cohort=cohort), self.assertRaises(ValueError):
                self.import_package(provider_ccns=cohort)

    def test_readonly_preview_lists_all_conflicts_and_blocks_apply_without_changing_facts(self):
        self.prepare()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany("INSERT INTO ipsf(provider_ccn,effective_date,provider_type) VALUES(?,?,'36')",
                                   [("017998", 20250101), ("017999", 20240101)])
        original = file_sha256(self.database)
        report = self.import_package()
        self.assertEqual(report["status"], "BlockedByConflicts")
        self.assertFalse(report["safeToApply"])
        self.assertEqual(len(report["conflicts"]), 2)
        self.assertEqual(file_sha256(self.database), original)
        self.assertTrue(all("special_provider_update_factor" in row["fields"] for row in report["conflicts"]))

    def test_exact_review_corrects_complete_hh_source_rows_and_appends_history_in_one_transaction(self):
        self.prepare()
        row_id = self.add_conflicting_source_row(federal_pps_blend="0")
        before_dump = self.snapshot(self.database)
        before_hash = file_sha256(self.database)
        self.assertFalse(self.import_package()["safeToApply"])
        review = self.reviewed_conflicts()
        self.assertIn("export_date", review["approvedRows"][0]["changes"])
        options = self.review_options(review)
        preview = self.import_package(**options)
        self.assertEqual((preview["rowsToUpdate"],preview["rowsUpdated"],preview["rowsToInsert"]),(1,0,6))
        self.assertTrue(preview["safeToApply"])
        self.assertEqual(file_sha256(self.database),before_hash)
        applied = self.import_package(**options,apply=True,backup_path=self.root/"review-backup.db")
        self.assertEqual((applied["rowsUpdated"],applied["rowsAfter"]),(1,8))
        self.assertFalse(applied["existingProviderRowsUnchanged"])
        self.assertTrue(applied["unreviewedProviderRowsUnchanged"] and applied["otherTablesUnchanged"])
        self.assertEqual(self.snapshot(self.root/"review-backup.db"),before_dump)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT id,federal_pps_blend,county_code,export_date FROM ipsf WHERE id=?",(row_id,)).fetchone(),(row_id,"2",None,20261101))
        self.assertEqual(self.import_package()["rowsToInsert"],0)
        # The consumed approval is stale and cannot be replayed to overwrite again.
        with self.assertRaisesRegex(ValueError,"state"):
            self.import_package(**options)

    def test_review_rejects_tampered_bindings_exact_old_source_values_and_changed_field_scope(self):
        self.prepare(); self.add_conflicting_source_row()
        valid = self.reviewed_conflicts()
        before = file_sha256(self.database)
        mutations = [
            lambda doc:doc.update(catalogSha256="a"*64),
            lambda doc:doc.update(packageManifestSha256="b"*64),
            lambda doc:doc.update(sourcesManifestSha256="c"*64),
            lambda doc:doc.update(databaseSha256="d"*64),
            lambda doc:doc["approvedRows"][0].update(providerRowId=99999),
            lambda doc:doc["approvedRows"][0].update(existingRowSha256="e"*64),
            lambda doc:doc["approvedRows"][0].update(sourceRowSha256="f"*64),
            lambda doc:doc["approvedRows"][0]["providerHistorySource"].update(rowLocator="another source row"),
            lambda doc:doc["approvedRows"][0]["changes"].pop("export_date"),
            lambda doc:doc["approvedRows"][0]["changes"]["county_code"].update(before="not the old value"),
            lambda doc:doc["approvedRows"][0]["changes"]["county_code"].update(after="invented new fact"),
            lambda doc:doc["approvedRows"][0]["changes"].update(provider_type=dict(before="36",after="37")),
        ]
        for index, mutate in enumerate(mutations):
            invalid=deepcopy(valid);mutate(invalid)
            with self.subTest(mutation=index),self.assertRaises(ValueError):
                self.import_package(**self.review_options(invalid))
            self.assertEqual(file_sha256(self.database),before)
        options=self.review_options(valid)
        options["expected_conflict_review_sha256"]="0"*64
        with self.assertRaisesRegex(ValueError,"SHA-256"):self.import_package(**options)

    def test_review_rejects_missing_extra_duplicate_and_export_stamp_only_approvals(self):
        self.prepare(); self.add_conflicting_source_row(); self.add_conflicting_source_row("017999",20240101)
        valid=self.reviewed_conflicts()
        variants=[]
        missing=deepcopy(valid);missing["approvedRows"].pop();variants.append(missing)
        duplicate=deepcopy(valid);duplicate["approvedRows"].append(deepcopy(duplicate["approvedRows"][0]));variants.append(duplicate)
        extra=deepcopy(valid);extra_row=deepcopy(extra["approvedRows"][0]);extra_row.update(providerCcn="999999",effectiveDate=20260101,providerRowId=1);extra["approvedRows"].append(extra_row);variants.append(extra)
        empty=deepcopy(valid);empty["approvedRows"]=[];variants.append(empty)
        for index,invalid in enumerate(variants):
            with self.subTest(variant=index),self.assertRaises(ValueError):self.import_package(**self.review_options(invalid))
        # A different actual DB, even changed only outside IPSF, invalidates review.
        with closing(sqlite3.connect(self.database)) as connection,connection:connection.execute("UPDATE opsf SET marker='changed after review'")
        with self.assertRaisesRegex(ValueError,"state"):self.import_package(**self.review_options(valid))

    def test_review_never_allows_non_hh_rows_or_duplicate_local_identities(self):
        self.prepare(); self.add_conflicting_source_row("017999",20260701)
        with self.assertRaisesRegex(ValueError,"HH row"):
            self.import_package(**self.review_options(self.reviewed_conflicts()))
        # Even an otherwise matching source cannot authorize duplicated identities.
        with closing(sqlite3.connect(self.database)) as connection,connection:
            connection.execute("DELETE FROM ipsf WHERE provider_ccn='017999'")
        self.add_conflicting_source_row();self.add_conflicting_source_row()
        with self.assertRaisesRegex(ValueError,"HH row"):
            self.import_package(**self.review_options(self.reviewed_conflicts()))

    def test_review_corrections_roll_back_when_later_insert_or_preservation_check_fails(self):
        self.prepare();self.add_conflicting_source_row()
        with closing(sqlite3.connect(self.database)) as connection,connection:
            connection.execute("CREATE TRIGGER test_review_abort BEFORE INSERT ON ipsf WHEN NEW.provider_ccn='017999' BEGIN SELECT RAISE(ABORT,'test-only later insert failure'); END")
        before=self.snapshot(self.database);before_hash=file_sha256(self.database)
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_package(**self.review_options(self.reviewed_conflicts()),apply=True,backup_path=self.root/"rollback-review.db")
        self.assertEqual(self.snapshot(self.database),before)
        self.assertEqual(file_sha256(self.database),before_hash)
        with closing(sqlite3.connect(self.database)) as connection,connection:
            connection.execute("DROP TRIGGER test_review_abort")
            connection.execute("CREATE TRIGGER test_review_side_effect AFTER UPDATE ON ipsf BEGIN UPDATE opsf SET marker='must rollback'; END")
        before=self.snapshot(self.database)
        with self.assertRaisesRegex(ValueError,"preservation"):
            self.import_package(**self.review_options(self.reviewed_conflicts()),apply=True,backup_path=self.root/"side-effect-review.db")
        self.assertEqual(self.snapshot(self.database),before)

    def test_review_requires_paired_pin_unique_json_properties_and_complete_backup(self):
        self.prepare();self.add_conflicting_source_row()
        options=self.review_options(self.reviewed_conflicts())
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            self.import_package(conflict_review_path=options["conflict_review_path"])
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            self.import_package(expected_conflict_review_sha256=options["expected_conflict_review_sha256"])
        with self.assertRaisesRegex(ValueError,"backup"):
            self.import_package(**options,apply=True)
        path=options["conflict_review_path"]
        path.write_text(path.read_text().replace('"paymentYear":2026','"paymentYear":2026,"paymentYear":2026'))
        options["expected_conflict_review_sha256"]=file_sha256(path)
        with self.assertRaisesRegex(ValueError,"duplicate JSON"):
            self.import_package(**options)

    def test_review_rejects_unfingerprinted_pending_wal_or_journal_state(self):
        self.prepare();self.add_conflicting_source_row()
        options=self.review_options(self.reviewed_conflicts())
        original=file_sha256(self.database)
        for suffix in ("-wal","-journal"):
            pending=Path(str(self.database)+suffix)
            pending.write_bytes(b"test-only pending state outside the pinned main database")
            try:
                with self.subTest(sidecar=suffix),self.assertRaisesRegex(ValueError,"WAL/journal"):
                    self.import_package(**options)
                self.assertEqual(file_sha256(self.database),original)
            finally:pending.unlink()

    def test_review_retains_source_qrp_one_and_termination_and_runtime_rejects_termination_first(self):
        record=next(row for row in self.records if row["provider_ccn"]=="017997")
        record.update(federal_pps_blend="1",termination_date=20251231)
        source=self.manifest["providerHistorySources"][0]
        write_history(self.root/source["file"],self.records)
        source["sourceSha256"]=file_sha256(self.root/source["file"])
        write_manifest(self.manifest_path,self.manifest)
        self.prepare()
        row_id=self.add_conflicting_source_row("017997",20250101,federal_pps_blend="0",termination_date=None)
        result=self.import_package(**self.review_options(self.reviewed_conflicts()),apply=True,backup_path=self.root/"terminated-review.db")
        self.assertEqual(result["rowsUpdated"],1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT federal_pps_blend,termination_date FROM ipsf WHERE id=?",(row_id,)).fetchone(),("1",20251231))
        engine=create_engine("sqlite:///"+str(self.database))
        try:
            with self.assertRaises(HhaInputError) as caught:
                provider_for_period(engine,"017997",date(2026,6,1),date(2026,6,30))
            self.assertEqual(caught.exception.code,"HHA_PROVIDER_TERMINATED")
        finally:engine.dispose()

    def test_raw_no_apr_rows_require_both_literal_footnotes_and_unique_payment_year_context(self):
        from bridge.hha_provider_annual import NO_APR_FOOTNOTE
        fixture = json.loads((Path(__file__).parent / "fixtures/hha-2026-annual-provider-contract.json").read_text())
        policies = fixture["noAprCases"][0]["annualPaymentEvidence"]["valueBasedPurchasingSource"]["policySources"]
        source = self.manifest["valueBasedPurchasingSource"]
        source["sourceUrl"] = "https://data.cms.gov/provider-data/sites/default/files/resources/test-only/HHVBP_Provider_PerformanceYear_2024.csv"
        path = self.root / source["file"]
        header = ["CMS Certification Number (CCN)", "Payment Year", "Adjusted Payment Percentage (APP)",
                  "Footnote Payment Year", "Footnote Adjusted Payment Percentage (APP)"]
        def source_rows(first, second, extra_year=None):
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream); writer.writerow(header)
                writer.writerow(["017996", "2026", "0.000%", "", ""])
                writer.writerow(["017999", "NA", "NA", first, second])
                if extra_year: writer.writerow(["017995", extra_year, "0.000%", "", ""])
            source["sourceSha256"] = file_sha256(path)
        for first, second, expected in [(NO_APR_FOOTNOTE, NO_APR_FOOTNOTE, True),
                                        ("", NO_APR_FOOTNOTE, False), (NO_APR_FOOTNOTE, "", False),
                                        ("Different annual note", NO_APR_FOOTNOTE, False), ("", "", False)]:
            source_rows(first, second)
            with self.subTest(first=first, second=second), patch("bridge.hha_provider_reference._policy_metadata", return_value=policies):
                rows, years = read_hhvbp_source(self.root, source)
                self.assertEqual(rows.get("017999") is not None, expected)
                self.assertEqual(years, {"2026": 1, "NA": 1})
        source_rows(NO_APR_FOOTNOTE, NO_APR_FOOTNOTE, "2025")
        with patch("bridge.hha_provider_reference._policy_metadata", return_value=policies), self.assertRaises(ValueError):
            read_hhvbp_source(self.root, source)

    def test_export_stamp_difference_is_preserved_instead_of_rewriting_provider_facts(self):
        self.prepare()
        self.import_package(apply=True, backup_path=self.root / "first-backup.db")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE ipsf SET export_date=20250101 WHERE provider_ccn='017999'")
        original = file_sha256(self.database)
        result = self.import_package(apply=True, backup_path=self.root / "repeat-backup.db")
        self.assertEqual(result["rowsToInsert"], 0)
        self.assertEqual(len(result["exportStampDifferencesPreserved"]), 3)
        self.assertEqual(file_sha256(self.database), original)

    def test_truncated_filtered_or_wrong_ccn_history_is_rejected_before_output(self):
        source = self.manifest["providerHistorySources"][0]
        original = source["sourceUrl"]
        for url in [original.replace("1980-01-01", "2023-01-01"), original + "&providerType=36",
                    original.replace("017999", "017995")]:
            with self.subTest(url=url):
                source["sourceUrl"] = url
                write_manifest(self.manifest_path, self.manifest)
                with self.assertRaises(ValueError): self.prepare()
                self.assertFalse(self.output.exists())
        source["sourceUrl"] = original

    def test_conflicting_qrp_or_hhvbp_facts_remain_out_of_verified_catalog(self):
        changed = deepcopy(self.records)
        next(row for row in changed if row["provider_ccn"] == "017998")["federal_pps_blend"] = "0"
        history = self.root / self.manifest["providerHistorySources"][0]["file"]
        write_history(history, changed)
        self.manifest["providerHistorySources"][0]["sourceSha256"] = file_sha256(history)
        write_manifest(self.manifest_path, self.manifest)
        report = self.prepare()
        states = {row["providerCcn"]: row["status"] for row in report["readiness"]}
        self.assertEqual(states["017998"], "ConflictingAnnualQrp")
        self.assertEqual(report["verifiedProviders"], 1)

    def test_missing_or_changed_retained_sources_and_output_overwrite_are_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError): self.prepare()
        source = self.manifest["qualityReportingSources"][0]
        path = self.root / source["file"]
        path.unlink()
        with self.assertRaises(ValueError):
            prepare_reference_package(self.manifest_path, self.root / "new-output")
        self.assertFalse((self.root / "new-output").exists())


if __name__ == "__main__":
    unittest.main()
