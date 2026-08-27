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

The API initializes only:

- CMS I/OCE
- CMS OPPS Pricer
- CMS OPSF provider database

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
