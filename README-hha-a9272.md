# CY 2026 A9272 devices

The HH bridge supports a separate national device allowance for original `329`/`0329` claims using supplied HIPPS. It uses the approved, immutable Myelin DB release, cached at processor startup. Serving claims never reads the JSON package or writes rate data. Ordinary HH does not require the optional device reference. `/api/v1/hha/ready` reports `a9272Reference` independently of existing PPS readiness.

Public MedicareServiceAPI requests submit actual A9272 claim lines and `homeHealthPricing.a9272` benefit context. The API maps them to private bridge `a9272Lines`: each line has `claimLineNumber`, ISO `serviceDate`, `revenueCode` (`0270`, `0271`, `0272`, `0273`, `0279`), `hcpcsCode: A9272`, positive integral `units` (1–1000) and positive cent-precision `billedCharge`. The context and device list must either both be present or both absent. Device line numbers must be unique, and dates must fall within the statement period. They do not represent covered visits or outlier units.

The `a9272` context requires `benefitYear: 2026`, `asOfDate` within 2026 and on/after Through, `remainingPartBDeductible` in cents from 0 to 283, `basis: SubmittedRemainingPartBDeductible`, `excludesCurrentClaim: true` and `medicarePrimary: true`. It is a caller-supplied benefit snapshot, not a beneficiary ledger or a balance reserved across claims. Coverage and final remittance remain unverified. Extra caller rate fields are forbidden.

The national rate is $282.10 per device, without a submitted-charge cap or PPS wage, QRP, HHVBP, partial-period or outlier adjustment. Device application services remain in PPS. Apply remaining deductible in service-date/line-number order, then 20% coinsurance on each line's balance, rounded HALF_UP to cents. `totalPayment` is PPS net allowance plus gross device allowance. The response's optional `a9272` records the PPS component, benefit snapshot, complete reviewed rate proof, line and aggregate device allowance/deductible/coinsurance/beneficiary liability, Medicare payment before sequestration and remaining deductible. One unit with deductible met yields $282.10 allowance, $56.42 beneficiary coinsurance and $225.68 Medicare share. Raw CMS PPS output remains unchanged.

Missing or invalid approved DB rates fail the entire device claim with `HHA_A9272_RATE_REFERENCE_INVALID`. A device furnished before acceptance of a **late** NOA fails with `HHA_A9272_PRE_NOA_UNSUPPORTED` pending a separate coverage policy pass. No accepted financial detail is returned on either failure. A timely NOA does not invoke this device guard. On/after late-NOA acceptance the separate fee applies, including alongside a documented approved NOA waiver. Pre-acceptance devices remain unsupported even with an approved exception; see [approved NOA exceptions](README-hha-noa-exceptions.md).

## Reviewed annual DB load

Use `scripts/prepare_hha_separate_payment_rates.py --sources <retained-source-directory> --output <fresh-package.json>` for offline reproducible package preparation. The current preparation code pins the four CMS/CFR sources and verifies the CY 2026 fee, cost sharing and deductible. It uses optional pypdf. Runtime/imports do not need PDF libraries.

The approved package is `bridge/data/hha-separate-payment-rates-2026-v1.json`, SHA-256 `9c952692c6c27afb4c2357e9b417f0333c6de18e67968097655fd7f3f3e4a67e`. The schema is `bridge/references/hha-separate-payment-schema-v1.sql`: `hha_rate_releases` retains year/revision/effective dates, source JSON, hash, load/operator metadata and sealing status; `hha_payment_rates` retains the code, exact decimal rates/deductible and row fingerprint under that release. Five triggers prevent mutation of sealed releases. New versions append; approved runtime registries control activation.

```powershell
$hhRatePackage = '.\bridge\data\hha-separate-payment-rates-2026-v1.json'
$hhRateHash = '9c952692c6c27afb4c2357e9b417f0333c6de18e67968097655fd7f3f3e4a67e'
$hhRateRun = Join-Path '.\artifacts\hh-rate-loads' ([datetime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $hhRateRun | Out-Null
python -B scripts/import_hha_separate_payment_rates.py --catalog $hhRatePackage --database '.\data\myelin.db' --expected-catalog-sha256 $hhRateHash --revision 1 --report (Join-Path $hhRateRun 'preview.json')
python -B scripts/import_hha_separate_payment_rates.py --catalog $hhRatePackage --database '.\data\myelin.db' --expected-catalog-sha256 $hhRateHash --revision 1 --apply --backup (Join-Path $hhRateRun 'before.db') --report (Join-Path $hhRateRun 'apply.json')
python -B scripts/import_hha_separate_payment_rates.py --catalog $hhRatePackage --database '.\data\myelin.db' --expected-catalog-sha256 $hhRateHash --revision 1 --report (Join-Path $hhRateRun 'repeat.json')
```

Expect one release and one rate row. Review the target/preview, complete backup and preservation of unrelated provider/geography data. Exact repeat returns `AlreadyLoaded` without writes. Report/backup paths must be fresh. Failures roll back the new schema/rows. Restart Myelin to reload the approved snapshot. Run `tests.test_hha_a9272`, `tests.test_hha_separate_payment_reference`, all existing geography/HH tests, and the full regression suite. Keep the same package bytes and verifier in both repositories.

This is a **recurring annual load** from the CMS HH PPS claims-processing update; monitor corrections during the year. Keep multiple year/revision packages when rates change. Review new source bytes/rates and explicitly promote matching runtime/API pins. Quarterly geography/wage corrections do not by themselves create a new device fee. Preserve sources, hashes, diffs, backups, load reports and API/SQL evidence. For QA, apply to a staging copy of QA's own current DB, retain unrelated data and prior versions, bake that staged DB into the Myelin image, and deploy the matching API verifier. Local synthetic claims are not reference data. Rollback must retain compatible source versions for already accepted receipts.

Sources: [CY 2026 CR14304, Table 8](https://www.cms.gov/files/document/r13488cp.pdf), [CR13244](https://www.cms.gov/files/document/r12382cp.pdf), [claims manual chapter 10 section 40.2](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c10.pdf), [42 CFR 409.50(b)](https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol2/pdf/CFR-2024-title42-vol2-part409.pdf), [CY 2026 Part B deductible](https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles).
