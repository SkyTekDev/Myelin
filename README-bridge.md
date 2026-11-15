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
