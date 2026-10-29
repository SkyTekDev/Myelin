# Supplied-HIPPS Home Health first pass

The optional HH bridge prices ordinary, LUPA, partial-period, late-NOA and outlier CY 2026 original `329`/`0329` claims
using a supplied HIPPS code and the pinned CMS HHA 2.6.0 executable. It uses
the existing Myelin JVM and HHA Java bindings with a dedicated validated mapper.
It does not use the generic `HhaClient.create_input_claim()` defaults.

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
  supported partial periods span 1-29 inclusive days.
- `noticeReceiptDate`: accepted NOA date, from admission through admission plus
  five days. Missing/late notices and waiver processing are outside this pass.
- `periodType`: `Full30Day`, confirming the period has no partial-period event,
  or `Partial30Day` with the explicit `partialPeriod` facts below.
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

Outliers, late NOA, separately paid services, 034X,
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
The first pass requires a CY 2026 effective row, no reference change within the
period, a unique latest effective record, and no elapsed termination date.
This is a conservative reference-readiness policy, not a Medicare enrollment
determination. Missing facts never become favorable defaults.

Rates, 432 HIPPS weights/thresholds, and 489 wage rows come directly from the
hash-verified JAR. CMS's wage CSV uses Windows-1252 encoding. Both accepted
runtime years and returned calculation version are explicitly pinned to 2026.

The bridge also pins the 41 county transition rows from the
[official CY 2026 HH wage-index release](https://www.cms.gov/files/zip/cy-2026-final-hh-pps-wage-index.zip).
It checks current transition assignments, including expired transition codes,
and validates rural state names against FIPS; CMS rural state numbers differ
from FIPS state numbers. The release does not contain a complete county-FIPS
crosswalk. Other county-to-CBSA/rural assignments remain caller supplied and
return `countyMappingVerified=false` plus an explicit limitation. Patient ZIP
and agency geography are never substituted.

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
    tests.test_hha_lupa tests.test_hha_partial -v
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
require explicit `lateNoa: {"exceptionStatus":"NoException"}`. Timely notices
must omit this context. Missing or malformed facts and unsupported requested or
granted exceptions are rejected. The source notice date remains unchanged in the
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
