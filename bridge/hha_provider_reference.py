"""Prepare reviewed CMS HH annual catalogs and append provider history safely.

Preparation consumes retained official source files, not caller attestations or
download age. Imports are dry-run by default. Corrections require an explicitly
hash-pinned, exact-row review manifest; ordinary imports never overwrite facts.
"""

from collections import Counter
from contextlib import closing
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from urllib.parse import parse_qs, urlsplit

from bridge.hha_provider_annual import (
    CATALOG_FILENAME, CATALOG_SCHEMA, canonical_decimal, canonical_json,
    load_catalog, provider_row_content_sha256, NO_APR_FOOTNOTE, validate_policy_sources,
)
from myelin.pricers.ipsf import DATATYPES


SOURCE_SCHEMA = "HHAProviderAnnualSources/1"
PACKAGE_SCHEMA = "HHAProviderReferencePackage/1"
CONFLICT_REVIEW_SCHEMA = "HHAProviderConflictReview/1"


class ProviderReferenceConflictError(ValueError):
    def __init__(self, conflicts):
        first = conflicts[0]
        super().__init__(f"Existing provider facts conflict: {first['providerCcn']}/{first['effectiveDate']}.")
        self.conflicts = conflicts


_ALIASES = {
    "pps_facility_specific_rate": "caseMixAdjustedCostPerDischarge_PpsFacilitySpecificRate",
    "hosp_quality_indicator": "hospitalQualityIndicator",
    "cbsa_wi_location": "cbsaWageIndexLocation",
    "vpb_participant_indicator": "vbpParticipantIndicator",
    "bundle_model_discount": "bundleModel1Discount",
}
_QRP = {
    "Compliant": ("0", "CY 2026 HHAs Compliant for APU", "compliant"),
    "NonCompliant": ("2", "CY 2026 HHAs Non-Compliant for APU", "non-compliant"),
    "Excluded": (None, "CY 2026 HHAs Excluded from APU", "excluded"),
}


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_provider_record(record):
    """Exact values stored by the SQLite importer, without ORM defaults."""
    return {key: float(value) if isinstance(value, Decimal) else value
            for key, value in record.items()}


def provider_reference_row_sha256(row):
    """Bind every actual local column (including id), or every source field."""
    return hashlib.sha256(canonical_json(dict(row))).hexdigest()


def _require_stable_review_database(database):
    # The approval pins the main database bytes. Pending WAL/journal state is
    # outside that fingerprint and must be settled before an exact-state review.
    if any(path.exists() and path.stat().st_size for path in
           (Path(str(database) + "-wal"), Path(str(database) + "-journal"))):
        raise ValueError("Reviewed corrections require a stable database with no pending WAL/journal bytes.")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Conflict review contains duplicate JSON properties.")
        result[key] = value
    return result


def _load_conflict_review(path, expected_sha256, *, catalog_sha256,
                          package_sha256, sources_manifest_sha256, database_sha256):
    if path is None and expected_sha256 is None:
        return None
    if (path is None or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or file_sha256(path) != expected_sha256):
        raise ValueError("Conflict review requires its exact independently reviewed SHA-256.")
    review = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_json_object)
    fields = {"schemaVersion", "paymentYear", "catalogSha256", "packageManifestSha256",
              "sourcesManifestSha256", "databaseSha256", "approvedRows"}
    if (not isinstance(review, dict) or set(review) != fields
            or review["schemaVersion"] != CONFLICT_REVIEW_SCHEMA
            or type(review["paymentYear"]) is not int or review["paymentYear"] != 2026
            or review["catalogSha256"] != catalog_sha256
            or review["packageManifestSha256"] != package_sha256
            or review["sourcesManifestSha256"] != sources_manifest_sha256
            or review["databaseSha256"] != database_sha256
            or not isinstance(review["approvedRows"], list) or not review["approvedRows"]):
        raise ValueError("Conflict review differs from the exact database/package/catalog state.")
    identities, row_ids = set(), set()
    approved = {}
    row_fields = {"providerCcn", "effectiveDate", "providerRowId", "existingRowSha256",
                  "sourceRowSha256", "providerHistorySource", "changes"}
    for item in review["approvedRows"]:
        if (not isinstance(item, dict) or set(item) != row_fields
                or not isinstance(item["providerCcn"], str) or not re.fullmatch(r"[0-9]{6}", item["providerCcn"])
                or type(item["effectiveDate"]) is not int
                or type(item["providerRowId"]) is not int or item["providerRowId"] <= 0
                or any(not isinstance(item[key], str) or not re.fullmatch(r"[0-9a-f]{64}", item[key])
                       for key in ("existingRowSha256", "sourceRowSha256"))
                or not isinstance(item["changes"], dict) or not item["changes"]
                or not set(item["changes"]) <= set(DATATYPES)):
            raise ValueError("Conflict review contains an invalid exact-row approval.")
        _provider_day(item["effectiveDate"], "review effective date")
        for change in item["changes"].values():
            if not isinstance(change, dict) or set(change) != {"before", "after"}:
                raise ValueError("Every reviewed field requires exact before and after values.")
        identity = item["providerCcn"], item["effectiveDate"]
        if identity in identities or item["providerRowId"] in row_ids:
            raise ValueError("Conflict review repeats a provider identity or local row id.")
        identities.add(identity)
        row_ids.add(item["providerRowId"])
        approved[identity] = item
    return dict(sha256=expected_sha256, rows=approved)


def _calendar(value):
    if not isinstance(value, str):
        raise ValueError("Source dates must be ISO calendar dates.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Source dates must be ISO calendar dates.")
    return parsed


def _source_file(root, source):
    relative = source.get("file")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Retained source files must use package-relative paths.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Retained source paths must stay inside the source package.")
    expected = source.get("sourceSha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Every retained source requires a lowercase SHA-256.")
    if file_sha256(path) != expected:
        raise ValueError("A retained CMS source differs from the reviewed source manifest.")
    return path


def _metadata(source, kind):
    keys = ["sourceUrl", "sourceSha256", "cutoffDate" if kind == "providerHistory" else "publishedDate"]
    result = {key: source[key] for key in keys}
    if kind == "valueBasedPurchasing":
        result.update(factorAuthority="CMS_IPSF_FIELD_30",
                      annualCorroboration="CMS_HHVBP_PAYMENT_YEAR_ROUNDED_APP")
    return result


def _policy_metadata(root, source):
    if "policySources" not in source:
        return None
    policies = source["policySources"]
    if not isinstance(policies, list):
        raise ValueError("Retained CMS policy sources are required for ineligible APR corroboration.")
    result = []
    for policy in policies:
        _source_file(root, policy)
        result.append({key: policy[key] for key in ("basis", "sourceUrl", "sourceSha256", "rowLocator")})
    validate_policy_sources(result)
    return result


def _no_apr_metadata(source, policies):
    return dict(_metadata(source, "valueBasedPurchasing"),
                annualCorroboration="CMS_HHVBP_CY2026_INELIGIBLE_FOR_APR",
                performanceYear=2024, observedPaymentYears=[2026], policySources=policies)


def _vbp_source_metadata(root, source):
    result = [_metadata(source, "valueBasedPurchasing")]
    policies = _policy_metadata(root, source)
    if policies:
        result.append(_no_apr_metadata(source, policies))
    return result


def read_qrp_source(root, source):
    """Read only the first-column CCNs from the named official CY2026 APU PDF."""
    from pypdf import PdfReader
    classification = source.get("classification")
    if classification not in _QRP:
        raise ValueError("QRP sources must identify compliant, noncompliant or excluded lists.")
    indicator, title, file_kind = _QRP[classification]
    published = _calendar(source["publishedDate"])
    expected_url = ("https://www.cms.gov/files/document/pac-hha-cy2026-"
                    f"{file_kind}-apu-{published:%Y%m%d}.pdf")
    if source["sourceUrl"] != expected_url or published.year != 2026:
        raise ValueError("QRP source must be the named official CY2026 APU list and revision.")
    path = _source_file(root, source)
    reader = PdfReader(path)
    if not reader.pages or reader.pages[0].extract_text().splitlines()[0].strip() != title:
        raise ValueError("QRP PDF does not identify the expected CY2026 list.")
    rows = {}
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        for line in text.splitlines():
            match = re.match(r"^\s*([0-9]{6})\s+\S", line)
            if not match:
                continue
            ccn = match[1]
            if ccn in rows:
                raise ValueError("A QRP list repeats a provider CCN.")
            rows[ccn] = dict(indicator=indicator, classification=classification,
                source=dict(_metadata(source, "qualityReporting"),
                            rowLocator=f"page={page_number};CCN={ccn};classification={classification}"))
    if not rows:
        raise ValueError("QRP source contains no first-column provider CCNs.")
    return rows


def _column(header, candidates):
    found = [name for name in header if name in candidates]
    if len(found) != 1:
        raise ValueError("The HHVBP CSV does not have one unambiguous supported payment column.")
    return found[0]


def read_hhvbp_source(root, source):
    if source.get("datasetIdentifier") != "56d7-4994":
        raise ValueError("HHVBP source must identify CMS's agency-level dataset.")
    parsed = urlsplit(source["sourceUrl"])
    if (parsed.scheme != "https" or parsed.hostname != "data.cms.gov" or parsed.query
            or parsed.fragment or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.lower().endswith(".csv")):
        raise ValueError("HHVBP source must be the official retained CMS agency CSV.")
    _calendar(source["publishedDate"])
    path = _source_file(root, source)
    policies = _policy_metadata(root, source)
    if policies and not parsed.path.endswith("/HHVBP_Provider_PerformanceYear_2024.csv"):
        raise ValueError("Ineligible APR corroboration requires the reviewed CY2024 performance dataset.")
    rows, observed_years = {}, Counter()
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(set(header)) != len(header):
            raise ValueError("HHVBP source has duplicate columns.")
        ccn_column = _column(header, {"CMS Certification Number (CCN)", "CMS Certification Number"})
        year_column = _column(header, {"Payment Year", "Payment Adjustment Year"})
        percentage_column = _column(header, {"Adjusted Payment Percentage (APP)", "Adjusted Payment Percentage"})
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("HHVBP source has an inconsistent CSV row width.")
            year = row[year_column].strip()
            observed_years[year] += 1
            if year not in {"2026", "NA"}:
                continue
            ccn = row[ccn_column].strip()
            if not re.fullmatch(r"[0-9]{6}", ccn) or ccn in rows:
                raise ValueError("HHVBP payment year contains an invalid or duplicate CCN.")
            if year == "NA":
                rows[ccn] = None
                if (policies and row[year_column] == "NA" and row[percentage_column] == "NA"
                        and row.get("Footnote Payment Year") == NO_APR_FOOTNOTE
                        and row.get("Footnote Adjusted Payment Percentage (APP)") == NO_APR_FOOTNOTE):
                    rows[ccn] = dict(_no_apr_metadata(source, policies),
                        rowLocator=f"CSV row={number};CCN={ccn};paymentYear=NA",
                        publishedPaymentYear="NA", publishedAdjustedPaymentPercentage="NA",
                        annualPerformanceReportFootnote=NO_APR_FOOTNOTE,
                        adjustedPaymentPercentageFootnote=NO_APR_FOOTNOTE)
                continue
            if (row.get("Footnote Payment Year", "").strip()
                    or row.get("Footnote Adjusted Payment Percentage (APP)", "").strip()):
                rows[ccn] = None
                continue
            percentage = row[percentage_column].strip()
            if percentage.endswith("%"):
                percentage = percentage[:-1].strip()
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", percentage):
                # Missing/not-applicable annual facts cannot establish a payment factor.
                rows[ccn] = None
                continue
            places = len(percentage.partition(".")[2])
            if places > 5 or not Decimal("-5") <= Decimal(percentage) <= Decimal("5"):
                raise ValueError("HHVBP published percentage is outside the supported precision or range.")
            rows[ccn] = dict(_metadata(source, "valueBasedPurchasing"),
                rowLocator=f"CSV row={number};CCN={ccn};paymentYear=2026",
                publishedAdjustedPaymentPercentage=percentage,
                publishedPercentageDecimalPlaces=places)
    if not rows:
        raise ValueError("The retained HHVBP source contains no CY2026 annual rows.")
    numeric_years = sorted(int(year) for year in observed_years if re.fullmatch(r"[0-9]{4}", year))
    if policies and numeric_years != [2026]:
        raise ValueError("Ineligible APR rows cannot be assigned to an ambiguous annual payment dataset.")
    return rows, dict(observed_years)


def provider_csv_header():
    return [_ALIASES.get(key, key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:]))
            for key in DATATYPES]


def _provider_day(value, field):
    if type(value) is not int or len(str(value)) != 8:
        raise ValueError(f"CMS provider {field} is invalid.")
    return datetime.strptime(str(value), "%Y%m%d").date()


def read_provider_history(root, source):
    cutoff = _calendar(source["cutoffDate"])
    if cutoff.year != 2026 or cutoff > datetime.now(timezone.utc).date():
        raise ValueError("Provider history cutoff must be an actual supported CY2026 date.")
    parsed = urlsplit(source["sourceUrl"])
    query = parse_qs(parsed.query)
    ccns = query.get("oscarNumber", [])
    if (parsed.scheme != "https" or parsed.hostname != "pds.mps.cms.gov"
            or parsed.path != "/fiss/v2/inpatient/export" or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)
            or set(query) != {"fromDate", "toDate", "format", "oscarNumber"}
            or query.get("fromDate") != ["1980-01-01"] or query.get("toDate") != [cutoff.isoformat()]
            or query.get("format") != ["csv"] or not ccns or len(ccns) > 15
            or len(set(ccns)) != len(ccns) or any(not re.fullmatch(r"[0-9]{6}", ccn) for ccn in ccns)):
        raise ValueError("Provider data requires an explicit, full-history CMS CSV cohort URL.")
    path = _source_file(root, source)
    records, source_rows = {}, 0
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.reader(stream)
        if next(reader, None) != provider_csv_header():
            raise ValueError("CMS provider CSV header differs from the supported IPSF layout.")
        for number, row in enumerate(reader, 2):
            source_rows += 1
            if len(row) != len(DATATYPES):
                raise ValueError("CMS provider CSV row has an unsupported width.")
            record = {}
            for key, metadata in DATATYPES.items():
                value = row[metadata["position"]]
                if value == "":
                    value = None
                elif metadata["type"] == "INT":
                    value = int(value)
                elif metadata["type"] == "REAL":
                    value = Decimal(value)
                    if not value.is_finite():
                        raise ValueError("CMS provider CSV contains a nonfinite factor.")
                record[key] = value
            ccn = record["provider_ccn"]
            effective = _provider_day(record["effective_date"], "effective date")
            if ccn not in ccns or not date(1980, 1, 1) <= effective <= cutoff:
                raise ValueError("Provider record is outside the reviewed CCN/date source cohort.")
            termination = record["termination_date"]
            if termination not in (None, 0, 19000101, 99991231):
                _provider_day(termination, "termination date")
            identity = (ccn, record["effective_date"])
            if identity in records and records[identity]["record"] != record:
                raise ValueError("CMS provider history contains conflicting same-date records.")
            records[identity] = dict(record=record, csvRowNumber=number,
                                    source=dict(_metadata(source, "providerHistory"),
                                        rowLocator=f"CSV row={number};CCN={ccn};effectiveDate={record['effective_date']}"))
    if {identity[0] for identity in records} != set(ccns):
        raise ValueError("Full provider history did not return every reviewed CCN.")
    return dict(requestedCcns=ccns, records=[records[key] for key in sorted(records)], sourceRows=source_rows)


def _used_provider_facts(record):
    return dict(providerCcn=record["provider_ccn"],
                effectiveDate=_provider_day(record["effective_date"], "effective date").isoformat(),
                rawTerminationDate=record["termination_date"], providerType=(record["provider_type"] or "").strip(),
                federalPpsBlend=(record["federal_pps_blend"] or "").strip(),
                specialProviderUpdateFactor=canonical_decimal(record["special_provider_update_factor"]))


def prepare_reference_package(manifest_path, output_directory):
    manifest_path, output_directory = Path(manifest_path).resolve(), Path(output_directory).resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if (manifest.get("schemaVersion") != SOURCE_SCHEMA or manifest.get("paymentYear") != 2026
            or not isinstance(manifest.get("qualityReportingSources"), list)
            or {row.get("classification") for row in manifest["qualityReportingSources"]} != set(_QRP)
            or len(manifest["qualityReportingSources"]) != 3
            or not isinstance(manifest.get("providerHistorySources"), list) or not manifest["providerHistorySources"]):
        raise ValueError("The annual source manifest is incomplete or unsupported.")
    if output_directory.exists():
        raise ValueError("A prepared reference package requires a new output directory.")
    classifications, qrp_counts = {}, {}
    for source in manifest["qualityReportingSources"]:
        rows = read_qrp_source(root, source)
        qrp_counts[source["classification"]] = len(rows)
        if set(rows) & set(classifications):
            raise ValueError("The official QRP lists contain overlapping CCN classifications.")
        classifications.update(rows)
    vbp, observed_years = read_hhvbp_source(root, manifest["valueBasedPurchasingSource"])
    histories, seen_ccns = [], set()
    for source in manifest["providerHistorySources"]:
        history = read_provider_history(root, source)
        if set(history["requestedCcns"]) & seen_ccns:
            raise ValueError("Each reviewed CCN must belong to exactly one complete-history snapshot.")
        histories.append(history)
        seen_ccns.update(history["requestedCcns"])
    entries, readiness = {}, []
    for ccn in sorted(seen_ccns):
        rows = [item for history in histories for item in history["records"] if item["record"]["provider_ccn"] == ccn]
        ordered = sorted(rows, key=lambda item: item["record"]["effective_date"])
        older = [item for item in ordered if item["record"]["effective_date"] < 20260101]
        status = "NoOlderProviderRecord"
        entry = None
        if older:
            selected = older[-1]
            record = selected["record"]
            following = next((item for item in ordered if item["record"]["effective_date"] > record["effective_date"]), None)
            qrp, annual_vbp = classifications.get(ccn), vbp.get(ccn)
            status = "VerifiedAnnualReference"
            if following and following["record"]["effective_date"] <= 20260101:
                status = "Existing2026ProviderRecord"
            elif (record["provider_type"] or "").strip() != "36":
                status = "WrongProviderType"
            elif qrp is None:
                status = "MissingAnnualQrp"
            elif qrp["indicator"] is None:
                status = "ExcludedAnnualQrp"
            elif (record["federal_pps_blend"] or "").strip() != qrp["indicator"]:
                status = "ConflictingAnnualQrp"
            elif annual_vbp is None:
                status = "MissingAnnualHhvbp"
            else:
                try:
                    facts = _used_provider_facts(record)
                    factor = Decimal(facts["specialProviderUpdateFactor"])
                    if (len(facts["specialProviderUpdateFactor"].partition(".")[2]) > 5
                            or not Decimal("0.95") <= factor <= Decimal("1.05")):
                        status = "ConflictingAnnualHhvbp"
                    else:
                        no_apr = annual_vbp["annualCorroboration"] == "CMS_HHVBP_CY2026_INELIGIBLE_FOR_APR"
                        corroborated = factor == 1 if no_apr else (
                            abs((factor - 1) * 100 - Decimal(annual_vbp["publishedAdjustedPaymentPercentage"]))
                            < Decimal(5).scaleb(-annual_vbp["publishedPercentageDecimalPlaces"] - 1))
                        if not corroborated:
                            status = "ConflictingAnnualHhvbp"
                            continue_entry = False
                        else:
                            continue_entry = True
                        entry = dict(providerCcn=ccn, providerEffectiveDate=facts["effectiveDate"],
                            providerNextEffectiveDate=(_provider_day(following["record"]["effective_date"], "next effective date").isoformat() if following else None),
                            rawTerminationDate=record["termination_date"], providerType="36",
                            qualityReportingProgramIndicator=qrp["indicator"], vbpAdjustment=facts["specialProviderUpdateFactor"],
                            providerRowContentSha256=provider_row_content_sha256(facts),
                            providerHistoryCutoffDate=selected["source"]["cutoffDate"],
                            qualityReportingSource=qrp["source"], valueBasedPurchasingSource=annual_vbp,
                            providerHistorySource=selected["source"]) if continue_entry else None
                except (ValueError, InvalidOperation, TypeError):
                    status = "InvalidProviderFactor"
        if entry is not None:
            entries[ccn] = entry
        readiness.append(dict(providerCcn=ccn, status=status))
    catalog = dict(schemaVersion=CATALOG_SCHEMA, paymentYear=2026, entries=entries,
        sources=dict(qualityReporting=[_metadata(row, "qualityReporting") for row in manifest["qualityReportingSources"]],
                     valueBasedPurchasing=_vbp_source_metadata(root, manifest["valueBasedPurchasingSource"]),
                     providerHistory=[_metadata(row, "providerHistory") for row in manifest["providerHistorySources"]]))
    raw = canonical_json(catalog)
    catalog_sha256 = hashlib.sha256(raw).hexdigest()
    # Validate before writing a package; no unverified/empty catalog is emitted.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as temporary:
        candidate = Path(temporary) / CATALOG_FILENAME
        candidate.write_bytes(raw)
        load_catalog(candidate, catalog_sha256)
    output_directory.mkdir(parents=True)
    (output_directory / "sources").mkdir()
    copied = json.loads(json.dumps(manifest))
    source_rows = (copied["qualityReportingSources"] + [copied["valueBasedPurchasingSource"]]
                   + copied["valueBasedPurchasingSource"].get("policySources", []) + copied["providerHistorySources"])
    for number, source in enumerate(source_rows, 1):
        original = _source_file(root, source)
        relative = f"sources/{number:02d}-{original.name}"
        shutil.copy2(original, output_directory / relative)
        source["file"] = relative
    (output_directory / "sources-manifest.json").write_bytes(canonical_json(copied))
    (output_directory / CATALOG_FILENAME).write_bytes(raw)
    package = dict(schemaVersion=PACKAGE_SCHEMA, paymentYear=2026, catalogFile=CATALOG_FILENAME,
                   catalogSha256=catalog_sha256, sourcesManifestFile="sources-manifest.json",
                   sourcesManifestSha256=file_sha256(output_directory / "sources-manifest.json"))
    (output_directory / "package-manifest.json").write_bytes(canonical_json(package))
    report = dict(status="PreparedAndHashVerified", catalogSha256=catalog_sha256,
                  verifiedProviders=len(entries), requestedProviders=len(seen_ccns), readiness=readiness,
                  qrpClassificationCounts=qrp_counts, hhvbpPaymentYearCounts=observed_years,
                  providerTypeCounts=dict(Counter((item["record"]["provider_type"] or "").strip()
                      for history in histories for item in history["records"])),
                  originalProviderDatabaseAccessed=False)
    (output_directory / "preparation-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _rows_hash(connection, table, where="", parameters=()):
    quoted = '"' + table.replace('"', '""') + '"'
    digest = hashlib.sha256()
    for row in connection.execute(f"SELECT * FROM {quoted} {where} ORDER BY rowid", parameters):
        digest.update(json.dumps(tuple(row), separators=(",", ":"), default=str).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def import_reference_package(package_directory, database, *, expected_catalog_sha256, apply=False,
                             backup_path=None, provider_ccns=None, conflict_review_path=None,
                             expected_conflict_review_sha256=None):
    package_directory, database = Path(package_directory).resolve(), Path(database).resolve()
    package = json.loads((package_directory / "package-manifest.json").read_bytes())
    if (package.get("schemaVersion") != PACKAGE_SCHEMA or package.get("paymentYear") != 2026
            or package.get("catalogFile") != CATALOG_FILENAME
            or package.get("catalogSha256") != expected_catalog_sha256
            or package.get("sourcesManifestFile") != "sources-manifest.json"
            or package.get("sourcesManifestSha256") != file_sha256(package_directory / "sources-manifest.json")):
        raise ValueError("The provider reference package differs from its reviewed manifest.")
    catalog = load_catalog(package_directory / CATALOG_FILENAME, expected_catalog_sha256)
    manifest = json.loads((package_directory / "sources-manifest.json").read_bytes())
    if manifest.get("schemaVersion") != SOURCE_SCHEMA or manifest.get("paymentYear") != 2026:
        raise ValueError("Provider reference source manifest is unsupported.")
    # Verify every retained source, not merely the provider CSV used by this import.
    for source in (manifest["qualityReportingSources"] + [manifest["valueBasedPurchasingSource"]]
                   + manifest["valueBasedPurchasingSource"].get("policySources", []) + manifest["providerHistorySources"]):
        _source_file(package_directory, source)
    expected_sources = dict(
        qualityReporting=[_metadata(row, "qualityReporting") for row in manifest["qualityReportingSources"]],
        valueBasedPurchasing=_vbp_source_metadata(package_directory, manifest["valueBasedPurchasingSource"]),
        providerHistory=[_metadata(row, "providerHistory") for row in manifest["providerHistorySources"]])
    if expected_sources != catalog.document["sources"]:
        raise ValueError("Source package provenance differs from the pinned annual catalog.")
    histories = [read_provider_history(package_directory, source) for source in manifest["providerHistorySources"]]
    cohorts = [ccn for history in histories for ccn in history["requestedCcns"]]
    if len(cohorts) != len(set(cohorts)):
        raise ValueError("Provider source snapshots have overlapping reviewed cohorts.")
    records = [item["record"] for history in histories for item in history["records"]]
    record_sources = {(item["record"]["provider_ccn"], item["record"]["effective_date"]): item["source"]
                      for history in histories for item in history["records"]}
    identities = [(row["provider_ccn"], row["effective_date"]) for row in records]
    if len(identities) != len(set(identities)):
        raise ValueError("The reviewed provider package contains duplicate record identities.")
    # Bind source facts and interval coverage to every committed annual entry.
    for ccn, entry in catalog.document["entries"].items():
        ordered = sorted((row for row in records if row["provider_ccn"] == ccn), key=lambda row: row["effective_date"])
        effective = int(entry["providerEffectiveDate"].replace("-", ""))
        selected = [row for row in ordered if row["effective_date"] == effective]
        following = next((row["effective_date"] for row in ordered if row["effective_date"] > effective), None)
        expected_next = str(following)[:4] + "-" + str(following)[4:6] + "-" + str(following)[6:] if following else None
        if (len(selected) != 1 or provider_row_content_sha256(_used_provider_facts(selected[0])) != entry["providerRowContentSha256"]
                or expected_next != entry["providerNextEffectiveDate"]):
            raise ValueError("Catalog provider identity/interval differs from the retained complete history.")
    source_record_count = len(records)
    source_ccns = sorted({row["provider_ccn"] for row in records})
    if provider_ccns is not None:
        if (not isinstance(provider_ccns, (list, tuple)) or not provider_ccns
                or any(not isinstance(ccn, str) or not re.fullmatch(r"[0-9]{6}", ccn) for ccn in provider_ccns)
                or len(set(provider_ccns)) != len(provider_ccns)
                or not set(provider_ccns) <= set(source_ccns)):
            raise ValueError("An import subset must be an explicit unique cohort inside the fully verified package.")
        records = [row for row in records if row["provider_ccn"] in provider_ccns]
    backup_path = Path(backup_path).resolve() if backup_path is not None else None
    if not database.is_file() or (backup_path is not None and backup_path == database):
        raise ValueError("A real provider database and a distinct new backup are required.")
    before_file_hash = file_sha256(database)
    review = _load_conflict_review(conflict_review_path, expected_conflict_review_sha256,
        catalog_sha256=catalog.sha256, package_sha256=file_sha256(package_directory / "package-manifest.json"),
        sources_manifest_sha256=package["sourcesManifestSha256"], database_sha256=before_file_hash)
    if review:
        _require_stable_review_database(database)
    with closing(sqlite3.connect(database.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Provider database integrity check failed.")
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        if apply:
            if backup_path is None:
                raise ValueError("Applying provider history requires a new complete SQLite backup.")
            with backup_path.open("xb"):
                pass
            with closing(sqlite3.connect(backup_path)) as backup, backup:
                connection.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Provider backup integrity check failed.")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA data_version").fetchone()[0] != version:
                raise ValueError("Provider database changed while creating its backup; retry from a stable source.")
        else:
            connection.execute("BEGIN")
        try:
            if review:
                _require_stable_review_database(database)
            if review and file_sha256(database) != before_file_hash:
                raise ValueError("Provider database changed after the exact conflict review was checked.")
            before = connection.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM ipsf").fetchone()
            old_provider_hash = _rows_hash(connection, "ipsf")
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'ipsf'")]
            old_table_hashes = {table: _rows_hash(connection, table) for table in tables}
            if apply:
                # SQLite backup may change file-header counters; compare the full
                # logical schema, every reference table and autoincrement state.
                with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as verified_backup:
                    schema_query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                    if ([tuple(row) for row in connection.execute(schema_query)] != list(verified_backup.execute(schema_query))
                            or _rows_hash(verified_backup, "ipsf") != old_provider_hash
                            or any(_rows_hash(verified_backup, table) != value for table, value in old_table_hashes.items())):
                        raise ValueError("Complete provider backup differs from the pre-import reference state.")
                    has_sequence = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()[0]
                    if has_sequence and (list(map(tuple, connection.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name")))
                                         != list(verified_backup.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name"))):
                        raise ValueError("Provider backup autoincrement state differs from the source.")
            pending, export_stamp_differences, conflicts, corrections = [], [], [], []
            for record in records:
                existing = connection.execute("SELECT * FROM ipsf WHERE provider_ccn=? AND effective_date=?",
                    (record["provider_ccn"], record["effective_date"])).fetchall()
                values = normalized_provider_record(record)
                if existing:
                    changed = ([key for key, value in values.items() if key != "export_date" and existing[0][key] != value]
                               if len(existing) == 1 else ["duplicateExistingIdentity"])
                    if changed:
                        identity = record["provider_ccn"], record["effective_date"]
                        conflict = dict(providerCcn=identity[0], effectiveDate=identity[1], fields=changed)
                        approval = review["rows"].get(identity) if review else None
                        if approval is not None:
                            old = dict(existing[0])
                            source = record_sources[identity]
                            changes = {key: dict(before=old[key], after=value)
                                       for key, value in values.items() if old[key] != value}
                            if (len(existing) != 1 or old["provider_type"] != "36" or values["provider_type"] != "36"
                                    or approval["providerRowId"] != old["id"]
                                    or approval["existingRowSha256"] != provider_reference_row_sha256(old)
                                    or approval["sourceRowSha256"] != provider_reference_row_sha256(values)
                                    or canonical_json(approval["providerHistorySource"]) != canonical_json(source)
                                    or canonical_json(approval["changes"]) != canonical_json(changes)):
                                raise ValueError("Conflict approval differs from the exact old/source HH row or complete field changes.")
                            corrections.append(dict(approval=approval, old=old, values=values))
                        else:
                            conflicts.append(conflict)
                        continue
                    if existing[0]["export_date"] != values["export_date"]:
                        export_stamp_differences.append(dict(providerCcn=record["provider_ccn"], effectiveDate=record["effective_date"]))
                else:
                    pending.append(tuple(values[key] for key in DATATYPES))
            if review:
                matched = {(item["approval"]["providerCcn"], item["approval"]["effectiveDate"]) for item in corrections}
                if matched != set(review["rows"]) or conflicts:
                    raise ValueError("Conflict review must cover exactly every selected conflict, without missing, stale or extra approvals.")
            if apply and conflicts:
                raise ProviderReferenceConflictError(conflicts)
            correction_ids = {item["old"]["id"] for item in corrections}
            preserved_where = "WHERE id<=?" + (" AND id NOT IN (" + ",".join("?" for _ in correction_ids) + ")" if correction_ids else "")
            preserved_parameters = (before[1], *sorted(correction_ids))
            unreviewed_hash = _rows_hash(connection, "ipsf", preserved_where, preserved_parameters)
            if apply:
                for correction in corrections:
                    values = correction["values"]
                    updated = connection.execute("UPDATE ipsf SET " + ",".join(key + "=?" for key in DATATYPES) + " WHERE id=?",
                        (*[values[key] for key in DATATYPES], correction["old"]["id"]))
                    if updated.rowcount != 1:
                        raise ValueError("A reviewed provider correction did not update exactly its approved local row.")
                connection.executemany("INSERT INTO ipsf (" + ",".join(DATATYPES) + ") VALUES (" + ",".join("?" for _ in DATATYPES) + ")", pending)
            after = connection.execute("SELECT COUNT(*) FROM ipsf").fetchone()[0]
            if (after != before[0] + (len(pending) if apply else 0)
                    or _rows_hash(connection, "ipsf", preserved_where, preserved_parameters) != unreviewed_hash
                    or any(_rows_hash(connection, table) != value for table, value in old_table_hashes.items())
                    or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                raise ValueError("Existing provider/reference data preservation checks failed.")
            for correction in corrections:
                actual = dict(connection.execute("SELECT * FROM ipsf WHERE id=?", (correction["old"]["id"],)).fetchone())
                expected = dict(correction["old"], **correction["values"]) if apply else correction["old"]
                if provider_reference_row_sha256(actual) != provider_reference_row_sha256(expected):
                    raise ValueError("A reviewed provider correction differs from its complete approved source row.")
            if apply:
                connection.commit()
            report = dict(status="Applied" if apply else "BlockedByConflicts" if conflicts else "Preview", applied=apply,
                          safeToApply=not conflicts, conflicts=conflicts,
                          catalogSha256=catalog.sha256, requestedCcns=sorted({row["provider_ccn"] for row in records}),
                          verifiedSourceCcns=source_ccns, verifiedSourceRows=source_record_count,
                          selectedSourceRows=len(records), verifiedCatalogProviders=len(catalog.document["entries"]),
                          providerTypeCounts=dict(Counter((row["provider_type"] or "").strip() for row in records)),
                          rowsBefore=before[0], rowsToInsert=len(pending), rowsAfter=after,
                          existingProviderRowsUnchanged=not (apply and corrections), unreviewedProviderRowsUnchanged=True,
                          otherTablesUnchanged=True, rowsToUpdate=len(corrections), rowsUpdated=len(corrections) if apply else 0,
                          conflictReviewSha256=review["sha256"] if review else None,
                          reviewedCorrections=[item["approval"] for item in corrections],
                          exportStampDifferencesPreserved=export_stamp_differences,
                          databaseSha256Before=before_file_hash)
        except BaseException:
            connection.rollback()
            raise
    report["databaseSha256After"] = file_sha256(database)
    if apply:
        report.update(backupPath=str(backup_path), backupSha256=file_sha256(backup_path), completeBackupVerified=True)
    return report
