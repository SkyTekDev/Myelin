# SNF release: FY 2023–2027

Verified October 5, 2026 using the official CMS SNF 2027.0 executable 2.7.1.
Service dates: January 1, 2023 through September 30, 2027. The SNF bridge loads
years 2023–2027 explicitly, independently of calendar-based default year selection
in other Myelin clients. Split HIPPS segments at fiscal-year/provider boundaries;
prior PDPM days remain continuous across October 1. FY 2028 is excluded.

The runtime asset is `jars/pricers/snf-pricer-application-2.7.1.jar`, SHA-256
`9ea9f4763f28bff5902a9266d4e5f7971745a3309b36d1d5d3b7f9a08549b2c5`.
The [official executable archive](https://www.cms.gov/files/zip/snf-pricer-2027-0-v2-7-1-executable-jar.zip)
has SHA-256 `bd40da4e677aabd8c97f1671f74b7fc80dbd25ef0e2fbf9fce7c55e66b2f864a`.
CMS requires Java 17. Rate/wage tables are bundled in the JAR and initialize
automatically; no separate rate-table loader or startup provider download is needed.

For FY 2027, the bridge requires current fiscal-year provider inputs. It compares
the supplemental wage field/indicator with the CMS FY 2026 final wage calculated
from dated provider history. Discrepant, stale or unresolved inputs return
`UnableToPrice` without payment. A provider without prior history must have blank
supplemental fields. Provider data is never silently corrected. These checks run
before pricing any claim segment and do not change the claim submission contract.
See [CMS CR 14545](https://www.cms.gov/files/document/r13850cp.pdf).

From the MedicareServiceAPI repository, install the pinned asset for a checked-out
Myelin release policy with PowerShell:

```powershell
.\scripts\Install-MyelinSnfPricer.ps1 -MyelinRoot 'C:\Users\Brad\Myelin' -WhatIf
.\scripts\Install-MyelinSnfPricer.ps1 -MyelinRoot 'C:\Users\Brad\Myelin'
```

The installer verifies hashes and backs up recognized old SNF JARs. Keep only one
active SNF JAR in `jars/pricers`. JARs/databases are ignored by Git; update them
separately on another machine. Rebuild the image after source or asset changes.
Keep `SNF_ENABLED=true` in a release intended to price SNF claims.

From Myelin with its Python dependencies and Java configured:

```powershell
python -m tests.test_snf_release
python -m tests.test_snf_integration
python -m tests.test_snf_fy2027
```

`python -m tests.reconcile_snf <new-report.json>` checks actual providers for all
enabled years against the standalone CMS process on `127.0.0.1:18086`; start it
with MedicareServiceAPI's `verification/SNF/Start-CmsSnfPricer.ps1`. Use a fresh
report path so historical evidence is retained.

Full release evidence, provider coverage limits and the PowerShell build/push/QA
sequence are in MedicareServiceAPI's `docs/SNF-FY2027-readiness.md` and
`verification/SNF/fy2027-release-status.json`. The accepted October 1 database is
unchanged. This release was verified locally; QA deployment and the authenticated
FY 2027 persistence workflow remain separate rollout steps.
