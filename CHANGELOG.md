# Changelog

All notable changes to Myelin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Bridge 0.9.1: hospice end-of-life add-on.** Hospice requests may carry
  `dateOfDeath` (the claim's through date) and `endOfLifeAddOnUnits`, the
  qualifying nurse and social worker visit units on each of the last 7 days of
  life (day 1 the date of death, routine home care days only). The units ride on
  the latest routine home care line's call, which returns 74 or 77, and the
  response's `endOfLifeAddOn` lists each day's units, units paid (at most 16)
  and the CMS jar's payment; `totalPayment` includes it. Routine home care lines
  now skip continuous home care days inside them when their days are counted.
  Readiness reports `endOfLifeAddOn: true`.
- **Bridge 0.9.0: hospice pricing.** `POST /api/v1/hospice/process` prices the
  0651, 0652, 0655 and 0656 lines of 81X/82X claims with the unmodified CMS
  Hospice Pricer 2.7.1 (calculation versions 2026.0 and 2027.0), one call per
  line, for dates of service in FY 2026-2027. The quality reporting reduction
  comes from the OPSF record in effect; years the provider file cannot cover
  price at full rates. `GET /api/v1/hospice/ready` reports the pinned release and
  the provider file's quality coverage. Off unless `HOSPICE_ENABLED=true`; the
  jar hash is checked at startup. The ECS deploy script gains `-EnableHospice`.
  The FY 2026 jar (2.5.1) moves to `jars/retired`. See `README-bridge.md`.
- **Bridge 0.8.1: October 2026 FQHC telehealth.** With the October 2026 I/OCE
  (27.3.0.8) in `jars/`, `/api/v1/fqhc/process` accepts distant-site telehealth
  billed with the service's own code and modifier 93 or 95 (CR 14468); the lines
  come back with payment indicator 2 for the caller to price outside the PPS.
  `GET /api/v1/fqhc/ready` reports the loaded `ioceJar` and `telehealthCodeLines`.
  `.dockerignore` keeps replaced jars in `jars/retired` out of images.
- **Bridge 0.8.0: FQHC PPS pricing.** `POST /api/v1/fqhc/process` runs the CMS
  I/OCE and the unmodified CMS FQHC Pricer 2.10.0 (calculation version 2026.1)
  on 77X claims with a caller-supplied Medicare carrier and locality, for dates
  of service in CY 2025-2026. `GET /api/v1/fqhc/ready` reports the pinned release.
  Off unless `FQHC_ENABLED=true`; the jar hash is checked at startup. Amounts
  cross into Java as JSON through the jar's own Jackson mapper, so they stay
  exact decimals. The ECS deploy script gains `-EnableFqhc`. See
  `README-bridge.md`.
- **Bridge 0.7.0: supplied OPPS provider context.** `/api/v1/opps/process`
  accepts an optional claim-level `providerContext` (CCN, provider type, SSA
  state, effective date, CBSA, an optional reclassification and the quality
  indicator) that the unmodified CMS OPPS Pricer receives in place of the OPSF
  record. The deployment allowlist `OPPS_PROVIDER_CONTEXT_TYPES` (empty by
  default, which turns the feature off) limits which provider types may be
  supplied; other requests get HTTP 422. The response reports
  `providerContextSource` as `Supplied` or `OPSF`. See `README-bridge.md`.
- **Bridge 0.6.0: contractor edit bypass.** Claim lines on `/api/v1/ioce/process`
  and `/api/v1/opps/process` accept an optional `editBypass` list of I/OCE edit
  numbers, passed to the unmodified CMS editor for that line only. The
  deployment allowlist `IOCE_EDIT_BYPASS_ALLOWED` (empty by default, which turns
  the feature off) limits which edits may be bypassed; other requests get HTTP
  422. Line results echo the applied bypass. See `README-bridge.md`.

### Fixed

- `FqhcClient` now sets a supplied carrier and locality with the jar's
  `setCarrierCode`/`setLocalityCode`; it called setters the jar does not have.

## [1.0.0] - 2026-07-10

First stable release of Myelin. Provides a unified Python interface to the
official CMS (Centers for Medicare & Medicaid Services) Java-based reimbursement
tools via JPype1.

### Added

- **MS-DRG Grouper** (`DrgClient`) - assigns inpatient claims to DRGs for IPPS,
  IPF, and LTCH payment determination.
- **MCE Editor** (`MceClient`) - validates inpatient claims against the Medicare
  Code Editor.
- **IOCE Editor** (`IoceClient`) - processes outpatient claims through the
  Integrated Outpatient Code Editor, assigning APCs and applying edits.
- **HHA Grouper** (`HhagClient`) - groups home health claims using OASIS
  assessment data.
- **IRF Grouper** (`IrfgClient`) - groups inpatient rehabilitation facility
  claims into Case-Mix Groups (CMGs) using IRF-PAI data.
- **Pricer suite** for IPPS, OPPS, IPF (Psych), IRF, LTCH, SNF, HHA, Hospice,
  ESRD, and FQHC.
- **ASC Pricer** - pure-Python Ambulatory Surgical Center pricer with support
  for code-pair offsets, MUE limits, device discounts, and wage index lookup.
- **`Myelin` orchestrator** with a `process(claim)` entry point that wires the
  correct grouper, editor, and pricer together.
- **`AUTO` module** - automatic detection of required modules based on bill
  type, revenue codes, and provider data.
- **IPSF / OPSF provider data** - automatic lookup from CMS provider-specific
  files with per-claim override support for what-if analysis.
- **ICD-10 code conversion** (`ICDConverter`) - forward/backward mapping of
  diagnosis and procedure codes between fiscal years.
- **UB-04 (CMS-1450) PDF read/write** - generate a filled UB-04 PDF from a
  `Claim` and parse a filled UB-04 PDF back into a `Claim` object, plus a
  calibration PDF helper for verifying field mappings.
- **Excel export** - `MyelinOutput.to_excel()` and `to_excel_bytes()` for
  producing human-readable summaries of pricing/editing decisions.
- **Pydantic input/output models** throughout for type-safe claim construction
  and result handling.
- **Plugin system** via `pluggy` (`client_load_classes`, `client_methods`
  hookspecs) for custom Java class loading and client method extension.
- **CMSDownloader** - async, robust downloader for CMS grouper, editor, and
  pricer JARs with version tracking and extraction.
- **SQLite and PostgreSQL** database backends for provider data and ICD-10
  conversion tables.
- **JVM lifecycle management** - thread-safe startup/shutdown, context-manager
  support (`with Myelin(...) as m:`), and clean integration with multi-process
  workloads.
- ~~**Java stub generation** (`create_stubs.py`) for IDE type checking of JPype
  interop.~~ This effort was abandoned, may revisit in the future.
- **New IOCE fields added**
  - IoceOutputHcpcsModifier.description — for modifier descriptions on both input and output modifier lists
  - IoceOutputLineItem: action_flag_output_description, rejection_denial_flag_description, payment_method_flag_description, payment_indicator_description, revenue_code_description, discounting_formula_description
  - IoceOutput: claim_rejection_edit_disposition_description, claim_denial_edit_disposition_description, claim_return_to_provider_edit_disposition_description, claim_suspension_edit_disposition_description, line_rejection_edit_disposition_description, line_denial_edit_disposition_description
- **New descriptions added to ICOE output**
  - _enrich_disposition_and_edits now calls getEditDispositionDescription per disposition group when edits are present
  - Line items now also call: getLineItemActionFlagDescription, getLineItemDenialRejectionFlagDescription, getPaymentMethodFlagDescription, getPaymentIndicatorDescription, getRevenueCodeDescription, getDiscountFormulaDescription
  - Both input and output HCPCS modifier lists now call getHcpcsModifierDescription per modifier in addition to the existing edit descriptions
- **Provider data patching** - `IPSFDatabase.patch()` and `OPSFDatabase.patch()` methods for incremental updates to provider-specific data. Queries the maximum `last_updated` date, downloads only new records from that point forward, and upserts them (updating existing records with the same provider_ccn + effective_date, inserting new ones). Supports both YYYYMMDD and YYYY-MM-DD date formats.
- **MS-DRG diagnosis output codes** - `MsdrgOutputDxCode` now includes the
  original diagnosis code value, making principal and secondary diagnosis
  outputs easier to trace back to claim input.
- **IOCE description enrichment** - IOCE output now fills additional claim
  disposition, edit, line-item flag, modifier, payment indicator, revenue code,
  discount formula, and APC descriptions, with latest-description fallbacks when
  version-specific lookup text is unavailable.
- **IRF wage-index outputs** - `IrfOutput` now exposes inherited final CBSA and
  final wage-index values from the CMS payment data.


### Changed

- Centralized IPSF / OPSF provider lookups and exposed them on `MyelinOutput`
  alongside pricer results.
- Refactored `Myelin.process()` to make provider lookup errors non-halting and
  standardize error returns to `ReturnCode` values.
- Improved type safety across the library with `Protocol` types for Java
  interop surfaces.
- LTCH pricer now accepts an optional review code via claim `additional_data`
  and defaults the blend indicator to `0` when not provided.
- OPPS pricer tolerates a missing discount formula from IOCE.
- Hospice pricer tolerates a missing admit date and an optional IOCE output
  pass-through.
- IPSF / OPSF data is whitespace-stripped before being passed to Java pricers.
- Default `county_code` is now `0` instead of an empty string.
- Excel exporter strips nested-structure prefixes from column names and can
  optionally include the input claim on its own sheet.
- ASC Pricer no longer requires an OPSF provider (ASCs do not appear in OPSF).
- MCE input construction validates the discharge status.
- **CMSDownloader** - rewritten for clearer component discovery, jar inventory
  validation, missing-jar detection, bounded concurrent pricer downloads, shared
  request timeout handling, and cleaner extraction of only the required CMS
  runtime jars.


### Fixed

- Duplicate IRFG client initialization and duplicate ASC client setup in
  `Myelin` removed.
- Duplicate `IPSF` definition in `core.py` removed.
- LTCH-specific updates now applied prior to the Java call.
- HHA claim input construction.
- ICD converter no longer raises on a `None` check.
- Minor formatting issue on the Excel exporter summary tab.
- `IoceReturnCode` renamed from `ReturnCode` to avoid duplicate naming with the
  shared utility.
- **IPPS procedure codes** - inpatient procedure codes are now passed to the
  IPPS Java claim object as Java strings and assigned with `setProcedureCodes`.
- **IPSF HRR participant indicator** - preserves `0` values instead of treating
  them as missing, preventing invalid return codes and incorrect pricing.
- **Supplemental wage index mapping** - LTCH, SNF, and IRF now opt in to sending
  IPSF supplemental wage-index fields to the CMS Java provider object while IPF
  and other shared IPSF call sites keep the previous default behavior.
