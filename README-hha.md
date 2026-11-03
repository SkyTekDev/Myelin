# Supplied-HIPPS Home Health first pass

The optional HH bridge prices ordinary, LUPA, partial-period, shortened discharge/death, late-NOA and outlier CY 2025 and CY 2026 originals (`0329`) and adjustments (`0327`)
using a supplied HIPPS code and the pinned CMS HHA 2.6.0 executable. It uses
the existing Myelin JVM and HHA Java bindings with a dedicated validated mapper. For 0327, MedicareServiceAPI owns the replacement links, receipts, and signed allowance difference.
It does not use the generic `HhaClient.create_input_claim()` defaults.

[CY 2026 A9272 separate devices](README-hha-a9272.md) adds versioned DB fees, explicit deductible/coinsurance assumptions, independent device detail and a reusable annual load process alongside the existing PPS estimate.

The statement Through date selects CMS calculation version `2025.1` or `2026.0`.
Ordinary full 30-day original `0329` statements with timely NOA can start in December
2025 and end in January 2026. The January 1 IPSF row supplies payment facts and the
prior row must establish continuous provider coverage. CY 2025 uses an eligible IPSF
row effective in 2025. Load the reviewed `hha-geography-2025-v1.json` and
`hha-separate-payment-rates-2025-v1.json` packages into the Myelin SQLite DB
alongside CY 2026 before enabling the updated bridge. The existing geography
and separate-payment loaders accept each versioned package. The offline
`scripts/build_hha_2025_reference.py` script verifies the retained CMS sources
and rebuilds both 2025 packages.

## Enable locally

From the Myelin project directory:

```powershell
$env:HHA_ENABLED = 'true'
.\start_api.ps1
```

The default is disabled. Optional `HHA_JAR_PATH` must resolve to the verified
`hha-pricer-application-2.6.0.jar`; other releases fail startup. The existing
Dockerfile already copies `bridge`, its reference CSV, the CMS JARs, and the
provider database. Compose accepts `HHA_ENABLED` from the environment. ECS
requires a newly built image and the task setting `HHA_ENABLED=true`; no
deployment or environment enablement is performed merely by installing source.

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/v1/hha/ready
$body = Get-Content .\requests\sample_hha_claim.json -Raw
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/v1/hha/process `
    -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 30
```

General `/health/ready` continues to check the existing I/OCE/OPPS runtime. Its
new `hha` section and `/api/v1/version` expose the HH enabled/ready state and
release/date scope. `/api/v1/hha/ready` requires both the HH processor and JVM.
Existing I/OCE/OPPS/IPSF startup prerequisites still apply to the combined bridge.

## Required request and supported scope

`requests/sample_hha_claim.json` is a synthetic service scenario using public
provider CCN `017009`, not a real patient or submitted claim. Its service county
and visits are example inputs, not assertions about that agency's patients.
The provider must exist with usable dated reference data in the running image.

- `claimId`: nonblank correlation identifier, at most 128 characters.
- `providerCcn`: explicit normalized CCN. Tax ID, NPI and name inference are not
  performed by this endpoint.
- `billType`: string `329` or `0329`, normalized to `329`.
- `hippsCode`: five-character supplied code present in the pinned 2026 HHRG table.
- `admissionDate`: actual start of care. `fromDate`/`throughDate`: explicit
  statement dates, both within CY 2026. Full periods span 30 inclusive days;
  supported partial periods and shortened discharge/death statements span 1-29 inclusive days.
- `noticeReceiptDate`: accepted NOA date. Receipts later than admission plus five
  days require the explicit `lateNoa` exception context described below.
- `periodType`: `Full30Day`, confirming the period has no partial-period event,
  `Partial30Day` with the explicit `partialPeriod` facts below, or `Shortened30Day`
  with explicit `shortenedPeriod` discharge/death facts.
- `serviceCountyFips`: five-digit patient service-county FIPS from CMS value code
  85; `cbsaCode`: CMS value-code 61 wage area, preserving leading zeros. The
  two-digit CMS rural state code or full `999xx` rural code is accepted.
- `geographyBasis`: `SubmittedCmsValueCodes`.
- `visits`: explicit complete covered in-person visits. Each has a unique
  `visitId`, ISO `serviceDate`, discipline `PT`, `OT`, `SLP`, `SN`, `MSW` or
  `HHA`, and positive integer eligible 15-minute `outlierUnits`. Each visit
  must be within the statement period; combined eligible units may not exceed
  32 per patient/day across disciplines. These are visits, not arbitrary
  submitted claim lines. Callers must apply the applicable time/unit rules.

For a transfer to another HHA or discharge/readmission to the same HHA, submit
`periodType: "Partial30Day"` with all of these `partialPeriod` fields:

```json
{
  "eventType": "TransferToAnotherHha",
  "patientStatus": "06",
  "nextCareStartDate": "2026-06-16",
  "isCommonOwnershipTransfer": false
}
```

The other supported event is `DischargeReadmissionSameHha`. The caller attests
to the qualifying event, including completion of the original plan of care
before same-HHA readmission. Subsequent care must start after the submitted
Through date and within the original 30-day window. Common-ownership transfers
are rejected because CMS exempts them from this adjustment. A short statement
alone, death, or an inpatient interruption does not establish a qualifying event.

The adapter derives covered days inclusively from the first through the last
billable visit, even when statement dates extend beyond those visits. It sends
those days and CMS partial-payment indicator `Y` to the pinned executable.
The observed CMS 2.6.0 calculation rounds `days / 30` to two decimal places,
then rounds the wage/case-mix payment times that ratio, and finally applies
HHVBP. Seven days therefore uses `0.23`; 29 days uses `0.97`. The response
retains the event facts, first/last billable dates, covered/period days, exact
CMS payment ratio and `adjustmentApplied`. Supported non-outlier partial
payments return CMS `09` and `HHA_PARTIAL_PERIOD_ESTIMATED`.

These rules follow [CMS Claims Processing Manual, Chapter 10, §10.1.15](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c10.pdf)
and [CMS Medicare Payment Systems, Partial-Period Payments](https://www.cms.gov/outreach-and-education/medicare-learning-network-mln/mlnproducts/html/medicare-payment-systems.html).
The ratio and intermediate rounding are verified against the pinned executable,
not an approximation of the published proportional-payment description.

Periods below their HIPPS-specific visit threshold can receive LUPA estimates
when the request includes an explicit `lupa` context:

```json
"lupa": {
  "sourceAdmissionIndicator": "1",
  "isFirstOrOnlyPeriod": true
}
```

Both fields are required inside the context. `sourceAdmissionIndicator` is
`B` when condition code 47 applies and `1` otherwise. `isFirstOrOnlyPeriod`
must be an actual JSON boolean attesting whether this is the first or only
period in the sequence; it is never inferred from HIPPS. A true value requires
the admission date to equal the statement From date. A missing context leaves
a below-threshold period `UnableToPrice` with `HHA_LUPA_CONTEXT_REQUIRED`.
For a qualifying shortened period use `Partial30Day` with both contexts. CMS
LUPA payment takes precedence over partial-period proration: the same per-visit
payment and eligible add-on apply, CMS returns `06`/`14`, and partial-period
evidence records `adjustmentApplied: false`.
An initial or only period without a covered SN, PT, OT or SLP visit is outside
this implementation and returns `HHA_LUPA_CONTEXT_UNSUPPORTED`. Continuing
periods may contain only MSW or aide visits.

Separately paid services, 034X,
revisions/cancellations, and OASIS grouping remain outside this pass.
Claim/visit facts and assessment-derived HIPPS remain the submitting system's
responsibility. A `Success` result is a PPS payment estimate, not coverage,
beneficiary entitlement, or final Medicare remittance.

## Reference and mapping rules

Provider inputs are read from actual effective-dated IPSF type-36 rows:

- `federal_pps_blend` supplies the final-payment HH QRP indicator, `0` or `2`.
- `special_provider_update_factor` supplies HHVBP, constrained to the CY 2026
  supported 0.95–1.05 range. The generic SNF/hospital `vbp_adjustment` is ignored.

These field meanings follow [CMS's IPSF addendum, fields 18 and 30](https://www.cms.gov/files/document/r12452cp.pdf).
A unique latest effective record, no reference change within the period, and no
elapsed termination date remain required. Existing CY 2026 effective rows retain
their original pricing/evidence contract. Older effective rows require a matching
entry in the reviewed, SHA-256-pinned annual catalog described below. Missing
facts never become favorable defaults; reference readiness is not an enrollment
determination.

### Reviewed CY 2026 annual provider references

`bridge/data/hha-provider-annual-2026-v1.json` is a canonical JSON catalog, pinned
by `bridge/hha_provider_annual.py` and independently by MedicareServiceAPI. It
binds an older row's used facts, its complete retained CMS history, the history
cutoff and next effective date, and two annual source facts:

- The provider must occur on CMS's positive CY 2026 compliant or noncompliant APU
  list, establishing QRP `0` or `2`. The excluded list does not supply an automatic
  favorable default or exception.
- Exact HHVBP continues to come from IPSF field 30. The agency-level HHVBP dataset's
  CY 2026 payment-year percentage corroborates annual applicability at its published
  precision. It never supplies extra precision or replaces the exact factor.
  Rounding-boundary ties and conflicting percentages remain unsupported.

The reviewed catalog also recognizes an explicit ineligible-APR outcome. This
requires an actual row in the retained CY2024-performance/CY2026-payment dataset,
literal `NA` payment year and percentage, both exact published footnotes saying
the HHA is ineligible for an APR, and the two pinned CMS policy documents linking
performance/payment years and confirming no payment adjustment when fewer than
five measures produce no TPS. The retained exact IPSF factor must already be 1.
An absent row, bare `NA`, another footnote, a non-neutral raw factor, or unknown
QRP cannot establish this evidence. QRP remains independently `0` or `2`; the
ineligible-APR rule does not imply QRP compliance or an APU exemption.
The pinned policy roles are `PerformanceToPaymentYear` ([CMS model guide,
PDF page 10](https://www.cms.gov/priorities/innovation/media/document/hhvbp-exp-model-guide))
and `NoAprNoAdjustment` ([CMS FAQ Q4006, PDF pages 38-39](https://www.cms.gov/priorities/innovation/media/document/hhvbp-exp-faqs)).

The first reviewed release contains 1,485 older-provider entries (1,424 QRP `0`
and 61 QRP `2`), with full-history cutoff November 1, 2026. Its SHA-256 is
`f422f6459620f453c31b17ae412b8966a3e90b952bd886256367dc552471a461`.
All entries use the explicit ineligible-APR branch with an exact source factor
of 1. Nine requested providers were excluded because the retained IPSF QRP
indicator conflicted with their annual list. Catalog membership alone does not
establish local readiness: selected local facts and the entire statement interval
must still match. Existing per-CCN failures are returned at pricing time;
generic HH service readiness does not attest annual coverage for every provider.
Operators can verify the actual pinned catalog before deployment:

```powershell
.\.venv\Scripts\python.exe -B -c "from bridge.hha_provider_annual import load_annual_catalog; c = load_annual_catalog(); print(c.sha256, len(c.document['entries']))"
```

The selected local used facts must exactly match the retained CMS provider row.
The actual statement must end on or before the history cutoff and before its
next effective record, including a later record that changes provider type.
Downloading a recent file, changing its export stamp, or copying an older factor
does not establish annual payment evidence.

Successful older-row results add `providerEvidence.annualPaymentEvidence`, with
the catalog/entry/content fingerprints, local row identity, payment year,
source URLs/hashes/row locators, annual factor facts, and history interval.
The existing `sourceRowFingerprint` remains unchanged and binds the actual local
row. This optional proof is absent on existing CY 2026 row results, preserving
their saved response and receipt identities. Catalog corruption, missing annual
facts, conflicting local facts, and uncovered periods remain unpriced.

The package preparation tool reads retained official sources from a
`HHAProviderAnnualSources/1` manifest: `paymentYear: 2026`, all three
`qualityReportingSources` classifications (`Compliant`, `NonCompliant`,
`Excluded`), `valueBasedPurchasingSource` (`datasetIdentifier: "56d7-4994"`),
and `providerHistorySources`. Every source has a package-relative `file`, official
`sourceUrl`, lowercase `sourceSha256`, and `publishedDate` or `cutoffDate`.
For the explicit ineligible-APR branch, `valueBasedPurchasingSource.policySources`
also retains both policy PDFs, each with its `basis` role, package-relative `file`,
official `sourceUrl`, pinned `sourceSha256`, and reviewed `rowLocator`.
Provider snapshots must request each CCN's full history from 1980 through an actual
cutoff date using the official CSV export; filtering to type 36 is not permitted.
The tool parses the actual PDF/CSV files, reports excluded/conflicting references,
and writes a new self-contained package. It does not access a provider database.

```powershell
.\.venv\Scripts\python.exe -B scripts/prepare_hha_provider_reference.py `
    --sources-manifest retained-sources/sources-manifest.json `
    --output prepared-hha-reference
$catalogSha256 = .\.venv\Scripts\python.exe -B -c "from bridge.hha_provider_annual import CATALOG_SHA256; print(CATALOG_SHA256)"
.\.venv\Scripts\python.exe -B scripts/import_hha_provider_reference.py `
    --package prepared-hha-reference --database runtime/myelin.db `
    --expected-catalog-sha256 $catalogSha256 --report preview.json
```

Review the prepared catalog, per-CCN readiness, source/type counts, and preview
before applying. Applying requires `--apply --backup <new-backup.db>` and a new
report path. It verifies the complete logical backup, appends only missing IPSF
history, preserves existing facts and export-stamp differences, verifies every
other reference table, and commits atomically. Existing conflicts or ambiguous
records stop an ordinary import; no truncate/update fallback is used. Catalog releases
must be reviewed and shipped with matching pins in both applications. Annual
proof needs no new MCService columns or runtime annual-fact table.
An optional repeated `--ccn` limits the import to a reviewed subset after the
entire source package and catalog have been verified. Each selected CCN retains
its complete history, including other provider types. Dry runs list every
existing-fact conflict and return `BlockedByConflicts`; applying any conflict is
refused. No source or conflict is silently overwritten.

An explicitly reviewed correction is supported with the paired
`--conflict-review <review.json>` and
`--expected-conflict-review-sha256 <independently-reviewed-hash>` options. The
default remains append-only. The `HHAProviderConflictReview/1` manifest must bind
the exact database SHA-256, package-manifest SHA-256, source-manifest SHA-256 and
catalog SHA-256. Its `approvedRows` must cover exactly every selected conflict:
CCN, effective date, retained local row id, complete old-row fingerprint, complete
normalized source-row fingerprint, exact retained history source identity, and
every changed field's exact `before`/`after` values. Export-date changes are
explicitly included for a corrected row; export-stamp-only differences on other
rows remain preserved. Duplicate, stale, missing or extra approvals fail closed.
Both the old and replacement row must have raw provider type `36`.
The exact reviewed database must be stable with no pending WAL/journal bytes,
which are outside the pinned main database file's fingerprint.

Review the actual source values rather than coercing them to the annual catalog.
An authoritative correction to QRP `1` or a real termination date remains that
raw value and can keep the provider unpriced. The annual catalog is not changed
by the database import. Preview with the option pair, then apply with the same
reviewed manifest, a new report path and the mandatory complete backup:

```powershell
.\.venv\Scripts\python.exe -B scripts/import_hha_provider_reference.py `
    --package prepared-hha-reference --database data/myelin.db `
    --expected-catalog-sha256 $catalogSha256 `
    --conflict-review reviewed-corrections.json `
    --expected-conflict-review-sha256 $reviewSha256 `
    --apply --backup backups/myelin-before-reviewed-import.db `
    --report reviewed-import-applied.json
```

Corrections retain each local row id and replace only its explicitly reviewed
source fields. They share one atomic transaction with missing-history inserts.
The importer verifies the complete backup, every unreviewed existing IPSF row,
every corrected full row, every other reference table, and SQLite integrity
before committing. Reports distinguish `rowsToUpdate` from `rowsUpdated`, retain
the exact reviewed field changes and source hashes, and report
`unreviewedProviderRowsUnchanged`. They do not claim all existing rows were
unchanged when a reviewed correction was applied.

### QA reference-data rollout

`data/myelin.db` is Git-ignored, but the Dockerfile copies the build-context file
into `/app/data/myelin.db`. A code commit alone does not deliver the verified
provider history. Start with QA's current effective database as the release seed
and preserve its other reference tables; copying the local database over QA could
replace unrelated reference data. Check the task's database path and mounts,
which can override the file baked into the image.

Use the retained verified source package to dry-run the importer against that
seed. If there are conflicts, independently review its exact old/source rows and
create a QA-specific hash-pinned correction manifest. The local 17-row review is
bound to database SHA-256
`78b46bfbf8b6dabf7eb43150d81668fdbeb7284d648f49e01d617ed32a0e5a15`
and exact old-row fingerprints; reuse it only if the current QA file and every
approved row match. Apply with a complete verified backup, then run the default
no-review preview again and require zero conflicts, inserts and updates. Record
the resulting reference database hash as part of the data release.

Package that prepared seed in a new private image, verify its database hash and
the actual serving task's effective data, and redeploy it with the matching code.
Both Myelin and MedicareServiceAPI must retain the same reviewed catalog pin
`f422f6459620f453c31b17ae412b8966a3e90b952bd886256367dc552471a461`.
The deployment helper preserves existing environment variables and mounts and
has no HH enable switch: explicitly set `HHA_ENABLED=true` in Myelin and enable
and configure HH in MedicareServiceAPI. Retain the previous private image/task
definition and reference backup for rollback.

This rollout copies reference data only; local MCService claims and synthetic
test results are not release data, and no additional MCService migration is
needed. The local SQL verification harness accepts loopback services only and
does not establish QA readiness. Check the actual QA HH readiness, submit QA
claims through its .NET API, and verify the results in its own MCService database
as a separate deployment validation.

Rates, 432 HIPPS weights/thresholds, and 489 wage rows come directly from the
hash-verified JAR. CMS's wage CSV uses Windows-1252 encoding. Both accepted
runtime years and returned calculation version are explicitly pinned to 2026.

New calculations validate service-county membership and the exact submitted HH
wage area against the approved release in Myelin's reference DB. The reviewed
import package is `bridge/data/hha-geography-2026-v1.json`, SHA-256
`b4d5e582fa73d2eb15f6ee02781ad8bfccab9db8a780eba006df3b0d5063688d`.
The reviewed sources cover 3,222 county entries (50 states, DC and Puerto Rico),
with 3,221 accepted mappings. CMS's FY2026 county register establishes county
identity; Census July2023 delineations and the CY2026 HH workbook corroborate
metro/division assignments. Published HH wages and the 41 transition/expired
transition rows supply the payment areas. Every accepted wage is reconciled
with the HHA JAR. ZIP and agency addresses never select the service county.
The submitter still attests to the patient's actual location.

One county, Stanly NC (`37167`), has conflicting source classifications and
returns `HHA_GEOGRAPHY_SOURCE_CONFLICT` rather than choosing an interpretation.
Unknown/obsolete FIPS use `HHA_COUNTY_NOT_FOUND`; inconsistent county/area pairs
use `HHA_COUNTY_CBSA_MISMATCH` or the existing specific state/transition guard.
Metropolitan divisions and Connecticut's current planning regions are explicit;
the CMS rural state code is not the FIPS state number. All successful new
responses have `countyMappingVerified=true` and `countyMappingEvidence` binding
catalog/entry hashes, exact area/wage, year interval, complete source hashes and
source row locators. The .NET API independently checks its own bundled copy.

Regenerate only from the four retained, hash-pinned files using the optional
Excel preparation dependency and `scripts/prepare_hha_geography.py --sources
<retained-directory> --output <new-catalog.json>`. The directory must contain
`fy2026-ipps-fr-county-cbsa-crosswalk-file.zip`,
`cy-2026-final-hh-pps-wage-index.zip`, `list1_2023.xlsx` and `r13488cp.pdf`.
Preparation requires exact source bytes, expected counts and complete
cross-source reconciliation; a changed source requires review. The FY2026
register's CBSA column is labeled FY2025, so it is not used without the
independent Census/HH checks. SourceConflict entries cannot authorize payment.
The JSON catalog is the reviewed import package. Load it into the Myelin DB
before enabling HH using the recurring process below. At startup, Myelin
reconstructs and verifies the complete DB release against its original catalog
hash and reconciles all accepted wages with the CMS JAR; pricing uses the cached
snapshot. There is no runtime JSON fallback, automatic schema creation or
download. Ship the matching .NET verifier/pin and retain prior DB releases and
approved API resources for historical receipts. No MCService schema change is
needed.

The November 2 applied pass passed 352 full pytest tests, 162 supplemental
unittest tests and both standalone SNF checks. The .NET API saved 23 geographic
scenarios to local MCService: 15 accepted payments and eight unpriced retries.
The API runner passed 765 checks and a separate SELECT-only SQL audit passed
2,051. All 139 prior HH claims and the original provider DB were preserved.
All 13 .NET regression suites also passed, including 1,815 HH checks. Rebuilding
the catalog from retained sources reproduced the exact reviewed checksum.

The official transition-source ZIP SHA-256 is
`c8734767077549704faf9627a8c92b34cb8ce13626379500239e07defc2aeca3`.
The LF-normalized extracted CSV SHA-256 is
`0a5c37ae6d44bad3550671925c34a8874498b6192812d4b7f1fdefe5f6a89bda`.
Startup validates its integrity and reconciles all 41 rates with the JAR.

CMS return code `00` is accepted for ordinary periods; `06` and `14` are accepted
for LUPA periods without and with an add-on, respectively. The classification
must agree with the exact HIPPS-specific visit threshold. Internal zero prior-agency totals prevent
cap clipping so any uncapped outlier is detected and rejected; these zeros are
not asserted as actual agency history. No outlier result can become a published
ordinary payment. The processor validates calculation version, exact weight,
visit count, six discipline outputs, zero unsupported adjustments, and totals
including CMS intermediate rounding and the resolved HHVBP factor.

LUPA per-visit and QRP-reduced rates are loaded from the pinned `rev-2026.csv`.
The initial skilled-visit add-on uses the 2026 factors SN 1.7200, PT 1.6225,
OT 1.7238 and SLP 1.6696. A transfer source or a non-initial period suppresses
the add-on. CMS selects the earliest skilled-discipline date, breaking same-day
ties in the order SN, PT, OT, SLP. The adapter reconciles every discipline's
base cost and add-on using the separate CMS labor/nonlabor rounding and separate
HHVBP rounding of each component. It also verifies standardized payment,
zero HRG allowance, zero outlier allowance, zero late penalty and the final total.

These rules follow the [2026 rate update](https://www.cms.gov/files/document/mm14304-home-health-prospective-payment-system-cy-2026-rate-update.pdf)
and [CMS Pricer input layout](https://www.cms.gov/files/document/r10839CP.pdf).

## Recurring geography database load

Load one immutable release per calendar payment year/revision. The initial
release has 3,222 counties, 3,221 verified mappings and source conflict `37167`.
The importer creates `hha_geography_releases` and `hha_geography_counties` plus
five sealing triggers from `bridge/references/hha-geography-schema-v1.sql` in
the same transaction as the data. Release rows retain exact source manifests,
dates, counts, operator and timestamp; county rows retain exact four-decimal
wage text, source-row locators and entry hashes. Sealed releases cannot be
edited, deleted or extended. Corrections append a new revision.

Run from the Myelin checkout with its prepared Python environment:

```powershell
$hhCatalog = '.\bridge\data\hha-geography-2026-v1.json'
$hhDatabase = '.\data\myelin.db'
$hhHash = 'b4d5e582fa73d2eb15f6ee02781ad8bfccab9db8a780eba006df3b0d5063688d'
$hhRun = Join-Path '.\artifacts\hh-geography-loads' ([datetime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $hhRun | Out-Null
python -B scripts/import_hha_geography.py --catalog $hhCatalog --database $hhDatabase --expected-catalog-sha256 $hhHash --revision 1 --report (Join-Path $hhRun 'preview.json')
python -B scripts/import_hha_geography.py --catalog $hhCatalog --database $hhDatabase --expected-catalog-sha256 $hhHash --revision 1 --apply --backup (Join-Path $hhRun 'before.db') --report (Join-Path $hhRun 'apply.json')
python -B scripts/import_hha_geography.py --catalog $hhCatalog --database $hhDatabase --expected-catalog-sha256 $hhHash --revision 1 --report (Join-Path $hhRun 'repeat.json')
```

Review the preview before applying to the intended existing SQLite DB. Add
`--operator` if the OS user does not identify the responsible operator. Reports
and backups require fresh paths. A new apply verifies a complete online backup,
all unrelated table fingerprints and earlier releases, and DB integrity before
commit. A failure rolls back the new schema/data. An exact repeat returns
`AlreadyLoaded` and makes no DB changes. Conflicting version/hash identities,
corrupt data or incomplete schemas are refused rather than repaired silently.

Restart Myelin to refresh its cache. `/api/v1/hha/ready` exposes
`geographyReference` with the DB store, year/revision, hash and county counts.
Missing, unsealed or altered required data prevents HH readiness. The .NET API
continues independently validating the identical payment proof using its own
reviewed catalog. Loading future years/revisions does not activate new payment
support; review the CMS rules/pricer and both approved registries first.

Treat geography as a recurring reference load when documenting load processes:
prepare each annual final CMS HH release and applicable corrections. Monitor
monthly, weekly during October–December (our recommended cadence). Use the
[CMS final HH wage-index files](https://www.cms.gov/medicare/payment/prospective-payment-systems/home-health-pps/home-health-pps-wage-index),
corroborating county/Census sources, and the OMB definitions actually adopted
by CMS for that payment year. Retain raw downloads, hashes, diffs, unresolved
conflicts, load reports and API/SQL/regression evidence.

For QA, load the same reviewed package into a staging copy of QA's own DB,
preserving its other reference data. Build a new Myelin image with that DB and
matching code/schema assets, then validate readiness, priced/unpriced claims,
saved evidence and historical replay through the deployed API. Keep the prior
image/task revision and complete DB backup for rollback. External DB mounts
need coordinated restoration while services are stopped. Provider-history
imports remain a separate process; no claims or receipts belong in this data
package. MedicareServiceAPI's `docs/HH-reference-loads.md` contains the full
operator and QA runbook.

The November 2 DB-backed local load added one release and 3,222 counties;
all 242,266 IPSF and 132,823 OPSF rows were preserved. Independent reference-DB
verification passed 3,237 checks and unchanged repeats returned AlreadyLoaded.
The full suite passed 366 pytest tests and 176 supplemental unittest tests;
both standalone SNF checks and all 13 .NET suites passed. The actual .NET API
passed 765 live checks for 23 retained claims; independent MCService SQL passed
2,051 checks. All 162 earlier HH claims were unchanged and 134 distinct prior
successful receipts replayed without new calculations.

## Response and failures

Successful results contain `pricingMethod: "HH_PPS"`, the exact decimal
`totalPayment`, calculation release, actual CMS provider payload, IPSF row
evidence/fingerprint, selected wage/transition evidence, raw CMS components,
and estimate limitations. Money is serialized as decimal strings, matching
the existing SNF bridge. CMS's generic `finalCbsa` and `finalWageIndex` can be
null; the bridge's separate geography evidence retains the selected values.

A successful LUPA uses `reasonCode: "HHA_LUPA_ESTIMATED"` and adds `lupa`
with the submitted source/period facts and one payment row per visit. Each row
contains visit identity, discipline, revenue code, service date, base payment,
add-on payment and total. Exact final CMS discipline costs are divided equally
in cents across that discipline's visits; residual cents go first by service
date and ordinal visit ID. The complete CMS add-on goes once to its selected
skilled visit. All visit rows are returned by service date and ordinal visit ID.
Their totals equal the accepted CMS total. The optional `lupa` field is omitted
on ordinary and unpriced responses, preserving the previous response shape and
required null fields on failures.

Invalid requests return HTTP 422. Disabled/stopped/backend-unavailable requests
return HTTP 503 with a safe error. Missing references and unsupported payment
outcomes return `UnableToPrice`, a specific `reasonCode`, and a null
`totalPayment`. Raw `cms` data on an unsupported outcome is diagnostic only;
its calculated components are not an accepted allowance. Full claim payloads
and underlying SQL/Java exception details are not included in error messages.

## Verification

Run from the project root using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_hha_models tests.test_hha_provider `
    tests.test_hha_geography tests.test_hha_routes tests.test_hha_integration `
    tests.test_hha_lupa tests.test_hha_partial tests.test_hha_provider_annual `
    tests.test_hha_provider_reference -v
```

These tests need no new test dependencies. They use isolated synthetic provider
databases and the actual pinned CMS JAR. Host-side ASGI tests require local
socket support from the Windows execution environment.

For an independent transport comparison, start the unmodified CMS executable
in a separate terminal, binding both interfaces to localhost:

```powershell
java '-Ddw.supportedYears=2026' `
    '-Ddw.server.applicationConnectors[0].port=18087' `
    '-Ddw.server.applicationConnectors[0].bindHost=127.0.0.1' `
    '-Ddw.server.adminConnectors[0].port=18088' `
    '-Ddw.server.adminConnectors[0].bindHost=127.0.0.1' `
    -jar .\jars\pricers\hha-pricer-application-2.6.0.jar server
```

Then run:

```powershell
.\.venv\Scripts\python.exe -B -m tests.reconcile_hha `
    --standalone-url http://127.0.0.1:18087 --output hha-reconciliation.json
```

Stop the standalone process when finished. Its effective default resource path
is `/v2/price-claim`; supplying a separate config may change the root prefix.
The comparison permits only the null/omitted transport difference for optional
`finalCbsa`/`finalWageIndex`. All amounts, other components, counts, return codes,
and versions compare exactly. Captured synthetic results are retained in
`tests/fixtures/hha-2026-ordinary.json`.

The ten captured public-provider synthetic LUPA scenarios in
`tests/fixtures/hha-2026-lupa.json` retain the standalone CMS input/output,
exact independent Decimal expectation and complete bridge response. They cover
all six disciplines, all four skilled add-ons, transfer/non-initial suppression,
same-day priority, mixed visits and a four-visit HIPPS with threshold five.


## CY 2026 late Notice of Admission

Late accepted NOA dates (more than five calendar days after the actual admission)
require explicit `lateNoa: {"exceptionStatus":"NoException"}` or a documented
`ApprovedException` as described in [approved NOA exceptions](README-hha-noa-exceptions.md).
Timely notices omit this context. Missing/malformed facts, requested/pending decisions
and partial waivers are rejected; KX alone is never an approval. The source notice date remains unchanged in the
CMS payload and raw CMS response. Complete visits remain the basis of the original
supplied-HIPPS threshold; exclusions never reclassify the period.

The pinned CMS HHA executable computes ordinary and partial-period penalties
before HHVBP, caps its nominal-period day count at 30, and preserves return 00/09
even when payment is zero. `lateNoa.cmsPayment` and `cmsPenaltyAmount` retain those
raw values. `grossPayment`, `penaltyAmount`, and `netPayment` reconcile the final
provider outcome after all supported policy adjustments. Zero is a successful
nonpayment estimate with provider liability; beneficiary liability is always zero.

Two explicit adapter rules implement current NOA policy beyond the executable's
legacy branches. A single NOA covers contiguous periods; a globally late NOA
received1�5days into a later period incurs the prorated `SubsequentPeriodPenalty`
instead of a new five-day grace. For LUPA, `LupaVisitExclusion` excludes visits
before the accepted receipt date; visits on that date remain payable. The
original eligible add-on is excluded with its visit and never reassigned.
Payable discipline counts are recalculated with exact CMS wage/QRP/HHVBP rounding;
gross allocation reserves the payable amount first, distributing excluded
residual cents deterministically. Gross, base/add-on exclusions, and net amounts
are retained for every visit. Raw CMS output remains intact and can exceed the
policy-adjusted estimate for these two methods.

Rules: [42 CFR 484.205(j)](https://www.ecfr.gov/current/title-42/chapter-IV/subchapter-G/part-484/subpart-E/section-484.205)
and [CMS Claims Processing Manual chapter 10,10.1.10.3](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c10.pdf).
The 20-case fixture includes independent raw standalone CMS outputs, published-rate
Decimal policy expectations, and additional eligible-count standalone LUPA probes.
No provider reference data is changed. Late-NOA support uses existing settings;
readiness reports `OrdinaryPeriodLupaPartialPeriodLateNoaAndOutlier`.


## CY 2026 outlier payments

The explicit `outlier` context supplies a caller-attested CY 2026 provider snapshot:
`{"calendarYear":2026,"asOfDate":"2026-06-30","priorPaymentTotal":100000,"priorOutlierTotal":1,"totalsExcludeCurrentClaim":true,"basis":"SubmittedProviderYtdTotals"}`.
All fields are required. Totals must be nonnegative two-decimal amounts within 999999999.99, outliers cannot exceed total payments, and the snapshot must be dated in 2026 on or after the statement Through date. These are estimator facts for the submitted CCN, not independently verified ledger entries. The service does not reserve annual capacity or change provider totals.

Outliers use the pinned per-unit rates, capped eligible daily units, a 0.37 fixed-dollar loss ratio, 0.80 loss-sharing, and a 10% agency pool. CMS compares the whole calculated outlier with the available pool; it does not clip payment to the remaining pool. Equality pays. Ordinary paid outliers use 01, partial paid outliers 11, and cap-limited outcomes 02. Below-threshold LUPA cannot receive a period outlier.

`outlier` evidence retains the submitted snapshot, imputed cost, period basis, fixed loss and threshold, uncapped candidate, pool, cap flags, raw CMS outlier, final gross/net outlier components, and effective return code. The executable bypasses its cap when either prior total is zero. The adapter preserves that raw calculation, then explicitly enforces the submitted pool via `ZeroHistoryCapEnforcement`; it never invents sentinel inputs. Missing history on an otherwise qualifying outlier returns `UnableToPrice/HHA_OUTLIER_CONTEXT_REQUIRED`.

Partial-period and late-NOA calculations apply in CMS order: period adjustment, outlier and cap, NOA reduction on both components, then HHVBP. The existing later-period NOA application adjustment also includes the allowed outlier. Raw CMS remains separate from policy-adjusted net amounts. Existing requests without outlier context retain their input/response shapes.

The 25-case fixture contains complete unmodified standalone CMS output and independent published-rate Decimal expectations, including cap equality/denial, zero history, all six disciplines, urban geography, QRP/VBP, partial periods, late NOA, zero payment, LUPA non-applicability, and the three-visit standardized-payment branch. Twenty public API cases are supplied for retained local verification. Rules: [CY 2026 CR14304](https://www.cms.gov/files/document/r13488cp.pdf) and [CMS pricer decision sequence](https://www.cms.gov/files/document/r10919cp.pdf).

## Shortened discharge/death statements without proration

Use `periodType: "Shortened30Day"` for an original CY 2026 statement of 1-29
inclusive days ending in discharge to home/self-care or death, with all four facts:

```json
"shortenedPeriod": {
  "eventType": "Discharge",
  "patientStatus": "01",
  "eventDate": "2026-06-15",
  "noSubsequentCareWithin30Days": true
}
```

For death, use `eventType: "Death"` and `patientStatus: "20"`. The event date
must equal the actual statement Through date, and all covered visits must be
within that statement. No `partialPeriod` context may be present. The caller
attests that there is no subsequent home health care within the original 30-day
window. A transfer or qualifying discharge/readmission needs the existing partial
payment path; an unspecified short statement never implies either payment rule.

The real statement Through date is submitted unchanged to CMS. The payment input
retains `hhrgNumberOfDays: 30` and `partialEpisodePaymentIndicator: "N"`. LUPA
thresholds, per-visit/add-on rules, outlier thresholds, annual cap and late-NOA
rules continue to apply. In particular, shortening the statement does not cap the
existing admission-based NOA delay at the event date: raw CMS and policy evidence
retain the original sequence and 30-day penalty denominator, even if the accepted
NOA is later than discharge/death. No artificial period extension is submitted.

A successful response includes `shortenedPeriod` with the original event facts,
first/last billable dates, actual `statementDays`, `periodDays: 30`,
`paymentRatio: "1.00"` and `adjustmentApplied: false`. Non-LUPA pricing uses
`HHA_SHORTENED_PERIOD_ESTIMATED`; LUPA retains `HHA_LUPA_ESTIMATED`. The optional
field is omitted from historical ordinary/partial requests and responses and from
unpriced outcomes. There is no database schema change.

These boundaries follow [CMS Claims Processing Manual chapter 10](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c10.pdf),
sections 10.1.5, 10.1.15-17 and 40.2, with partial-payment triggers defined in
[42 CFR 484.235](https://www.ecfr.gov/current/title-42/chapter-IV/subchapter-G/part-484/subpart-E/section-484.235).
The captured synthetic fixtures in `tests/fixtures/hha-2026-shortened.json` retain
complete unmodified standalone CMS outputs for all payment branches, including
late NOA after the event date. Run `tests.test_hha_shortened` with the other HH tests.
