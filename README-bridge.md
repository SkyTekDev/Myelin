# QON CMS I/OCE + OPPS API

Copy these files into the root of the cloned Myelin repository:

```text
C:\Users\Brad\Myelin\
├── bridge\
│   ├── __init__.py
│   ├── api_models.py
│   ├── claim_mapper.py
│   ├── main.py
│   └── opps_client.py
├── requests\
│   └── sample_opps_claim.json
├── requirements-bridge.txt
└── start_api.ps1
```

The bridge initializes:

- CMS I/OCE
- CMS OPPS Pricer
- CMS OPSF provider database
- CMS IPPS and IPSF provider lookup

SNF is enabled with `SNF_ENABLED=true`. The supplied-HIPPS HH first pass is
enabled with `HHA_ENABLED=true` and remains disabled by default. See
[README-hha.md](README-hha.md) for its CY 2026 original 0329 scope, required
inputs, reference policy, sample request, and reconciliation commands.

ESRD benchmark references are enabled with `ESRD_ENABLED=true` and remain
disabled by default; `start_api.ps1` turns them on for the local Windows bridge.
See [ESRD benchmark references](#esrd-benchmark-references).

FQHC PPS pricing (77X claims through the CMS I/OCE and FQHC Pricer) is enabled
with `FQHC_ENABLED=true` and remains disabled by default. See
[FQHC PPS pricing](#fqhc-pps-pricing).

It does not invoke `Myelin.__enter__()`, so the unrelated CMS pricers are
not initialized.

## Prerequisite

The OPSF provider table must already be populated. The existing
`run_ioce.py` can do this:

```powershell
.\.venv\Scripts\python.exe .\run_ioce.py --refresh-opsf
```

## Install API dependencies

```powershell
cd C:\Users\Brad\Myelin

.\.venv\Scripts\python.exe -m pip install `
    -r .\requirements-bridge.txt
```

## Run the API

```powershell
.\start_api.ps1
```

Equivalent command:

```powershell
.\.venv\Scripts\python.exe -m uvicorn bridge.main:app `
    --host 127.0.0.1 `
    --port 8080 `
    --workers 1
```

Do not use `--reload` and keep `--workers 1` during validation.

## Select an OPPS JAR when multiple versions exist

```powershell
$env:OPPS_JAR_PATH = `
    "C:\Users\Brad\Myelin\jars\pricers\opps-pricer-application-2.16.0.jar"

.\start_api.ps1
```

## Test readiness

```powershell
Invoke-RestMethod `
    -Method Get `
    -Uri "http://127.0.0.1:8080/health/ready"
```

## Run I/OCE and OPPS pricing

```powershell
$body = Get-Content .\requests\sample_opps_claim.json -Raw

$result = Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8080/api/v1/opps/process" `
    -ContentType "application/json" `
    -Body $body

$result | ConvertTo-Json -Depth 100
```

## Run I/OCE only

```powershell
$body = Get-Content .\requests\sample_opps_claim.json -Raw

$result = Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8080/api/v1/ioce/process" `
    -ContentType "application/json" `
    -Body $body

$result | ConvertTo-Json -Depth 100
```

## Contractor edit bypass

Bridge 0.6.0 adds an optional `editBypass` list to each claim line on both endpoints. It passes the CMS editor's contractor edit bypass for those edits on that line only, using the unmodified CMS I/OCE (`addContractorEditBypass`). CMS added the line-level bypass for Medicare contractors in change request 10988, effective April 2019.

```json
{ "claimLineId": 2, "lineNumber": 2, "revenueCode": "0636", "hcpcs": "J0586",
  "dateOfService": "2026-07-30", "units": 120, "chargeAmount": 3000.00, "editBypass": ["012"] }
```

- Each entry is an I/OCE edit number of one to three digits, normalized to three digits (`"12"` becomes `"012"`). A line can list up to four.
- The deployment decides which edits may be bypassed with `IOCE_EDIT_BYPASS_ALLOWED`, a comma-separated list such as `12`. It is empty by default, which turns the feature off. A request asking to bypass any other edit gets HTTP 422 before the editor runs.
- `/health/ready` and `/api/v1/version` report the allowlist as `ioceEditBypassAllowed`.
- Every entry in `ioceLineResults` (and `lineResults` on the I/OCE endpoint) echoes the bypass applied to that line as `editBypass`, an empty list when none was requested.

A bypass removes the editor's hold for that edit. It does not make a line payable: a line CMS cannot pay keeps its status and stays unpaid.

## Supplied provider context

Bridge 0.7.0 lets `/api/v1/opps/process` take an optional claim-level `providerContext` in place of the OPSF lookup. It is meant for a provider that CMS does not list in the OPSF, or lists without a wage-index location, such as a critical access hospital (type 37) priced for comparison only. The unmodified CMS OPPS Pricer receives these fields as the provider record and looks up the wage index in its own dated tables.

```json
"providerContext": {
  "providerCcn": "161322", "providerType": "37", "stateCode": "16", "effectiveDate": "2026-07-01",
  "cbsaActualGeographicLocation": "19780", "specialPaymentIndicator": "Y",
  "cbsaWageIndexLocation": "16", "hospitalQualityIndicator": "1"
}
```

- `providerCcn` must equal the claim's `providerNumber`, and `stateCode` (the SSA state code) must equal the CCN's first two characters. `effectiveDate` cannot follow the claim's `fromDate`.
- Area codes are the Pricer's wage-table keys: a five-digit CBSA, or a two-digit state code for a rural area (`"16"`, not `"99916"`).
- `specialPaymentIndicator` is blank or `"Y"`. Blank prices at `cbsaActualGeographicLocation`. `"Y"` requires `cbsaWageIndexLocation` and applies that reclassification, as the OPSF does for a hospital treated as rural.
- `hospitalQualityIndicator` is blank or `"1"`. No other provider fields are accepted. The record has no cost-to-charge ratio, so the Pricer calculates no outlier payment. It also has no county, so no out-migration adjustment applies, and no special or supplemental wage index.
- The deployment decides which provider types may be supplied with `OPPS_PROVIDER_CONTEXT_TYPES`, a comma-separated list such as `37`. It is empty by default, which turns the feature off. A context for any other type gets HTTP 422 before the editor runs. The I/OCE endpoint rejects any `providerContext`.
- `/health/ready` and `/api/v1/version` report the list as `oppsProviderContextTypes`.
- The response's `providerContextSource` is `"Supplied"` when the context was used and `"OPSF"` otherwise. `opsfProvider` shows the record the Pricer received in both cases.

A supplied context never comes from CMS. The caller owns its source and must label results priced with it.

## ESRD benchmark references

With `ESRD_ENABLED=true` the bridge serves the sealed ESRD reference releases in the Myelin database. It calculates no ESRD payment: the API prices the benchmark from what these endpoints return.

- `GET /api/v1/esrd/ready` lists the loaded releases with their catalog and policy fingerprints and any supersessions. It returns 503 while ESRD is disabled. `/health/ready` says nothing about ESRD.
- `POST /api/v1/esrd/references` returns the release, the facility's provider-file fields and the wage index for each service date.

Releases are loaded with `scripts/import_esrd_rates.py`. The reviewed packages and the loading runbook are kept with the API, in `Data/ESRD` and `docs/ESRD-reference-data.md` of MedicareServiceAPI.

**The bridge will not start with ESRD enabled unless the database holds a sealed release**, and that stops the I/OCE, OPPS and other endpoints too. Check a database before enabling it:

```powershell
.\.venv\Scripts\python.exe -m bridge.esrd_check --database .\data\myelin.db
```

The check opens the database read-only, runs the bridge's own startup checks and lists each release with its period, revision, fingerprint and optional sections (`bodySize`, `facilityAdjustments`, `finalPayment`). It exits non-zero with the reason when the bridge would refuse to start. Without `--database` it reads `MYELIN_DB_PATH`, as the bridge does. For a container image and ECS, see [README-container.md](README-container.md#enable-esrd).

## FQHC PPS pricing

With `FQHC_ENABLED=true` the bridge prices Federally Qualified Health Center
claims (bill type 77X). It runs the CMS I/OCE and then the unmodified CMS FQHC
Pricer 2.10.0 (calculation version 2026.1), whose jar hash is checked at startup.

- `GET /api/v1/fqhc/ready` reports the pinned release and the supported dates
  (January 1, 2025 through December 31, 2026). It returns 503 while FQHC is
  disabled. `/health/ready` and `/api/v1/version` also carry an `fqhc` block.
- `POST /api/v1/fqhc/process` takes the `/api/v1/ioce/process` claim plus the
  FQHC site's Medicare `carrierCode` (5 digits) and `localityCode` (2 digits),
  which select the CMS geographic adjustment factor. The caller resolves the
  locality from the site's ZIP; the bridge holds no ZIP crosswalk. An optional
  `medicareAdvantagePlanAmount` applies only to Medicare Advantage supplemental
  (revenue code 0519) claims.

The bridge sends FQHC claims to the I/OCE with `oppsFlag` 1 (with 2 the I/OCE
assigns no FQHC flags) and rejects contractor edit bypass and supplied provider
context. It returns every line's I/OCE flags and edits, the claim's
return-to-provider, suspension, rejection and denial edits, and the pricer's
line payment (the full allowed amount: the PPS rate or the charge, whichever is
lower), coinsurance and add-on. Amounts are exact decimals. Acting on the
editor's dispositions is the caller's decision, as for OPPS.

Distant-site telehealth billed from October 1, 2026 with the service's own code
and modifier 93 or 95 (CR 14468) needs the October 2026 I/OCE, 27.3 or later
(`ioce-standalone-27.3.0.8.jar` from CMS's V273.R0 "Java Standalone Jar"). That
I/OCE gives those lines payment indicator 2, bypasses edits 088/089, flags
preventive telehealth with line item action flag 7, and returns codes not on the
telehealth list with edit 139. The FQHC Pricer leaves payment-indicator-2 lines
unpaid (care coordination, G2025, Q3014 and telehealth are paid outside the PPS);
the caller prices them. `GET /api/v1/fqhc/ready` reports the loaded `ioceJar` and
whether it supports this billing (`telehealthCodeLines`). Replace the I/OCE jar in
`jars/` (only one `ioce-standalone-*.jar` may sit there; keep old ones in
`jars/retired`, which is not on the classpath and not copied into images).

It answers `UnableToPrice` (HTTP 200) with a reason code when:

- a date falls outside the reconciled years (`FQHC_DATES_NOT_SUPPORTED`): a 2027
  claim needs CMS's 2027 FQHC Pricer;
- a line is distant-site telehealth billed with modifier 93 or 95 outside
  revenue code 0900 on or after October 1, 2026 and the I/OCE predates 27.3
  (`FQHC_TELEHEALTH_IOCE_UNAVAILABLE`);
- the I/OCE or the pricer does not return one result per line, or the pricer
  does not process the claim.

Run the checks with:

```powershell
.\.venv\Scripts\python.exe -m tests.test_fqhc_release
.\.venv\Scripts\python.exe -m tests.test_fqhc_integration
```

The second runs the real I/OCE and FQHC jar on synthetic claims and checks the
amounts against CMS's published base rates and the jar's GAF.

## Swagger

```text
http://127.0.0.1:8080/docs
```

The combined OPPS endpoint returns:

- I/OCE return code and complete I/OCE result
- I/OCE line results mapped to the submitted `claimLineId`
- OPSF provider data used by the Pricer
- OPPS claim payment totals
- OPPS line payments mapped to the submitted `claimLineId`
- Complete OPPS Pricer output
