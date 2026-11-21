# QON CMS I/OCE + OPPS Container

Copy this package into the root of the cloned Myelin repository. The Docker
build context must contain:

```text
Myelin/
├── myelin/
├── bridge/
├── jars/
├── data/
│   └── myelin.db
├── pyproject.toml
├── requirements-bridge.txt
├── Dockerfile
├── docker-compose.yml
├── container/
└── aws/
```

The image contains the CMS JARs and populated OPSF database. It does not
download CMS files when an ECS task starts.

## Before building

Confirm the database and runtime files exist:

```powershell
Test-Path .\data\myelin.db
Get-ChildItem .\jars\*.jar
Get-ChildItem .\jars\pricers\*opps-pricer*.jar
```

The API startup fails deliberately if these files are missing or if the OPSF
table is empty.

## Build locally

```powershell
cd C:\Users\Brad\Myelin

docker build -t qon-ioce-opps-api:local .
```

Or:

```powershell
docker compose build
```

## Run locally

```powershell
docker run --rm `
    --name qon-ioce-opps-api `
    -p 8080:8080 `
    qon-ioce-opps-api:local
```

If the image contains multiple OPPS Pricer JARs, specify the exact one:

```powershell
docker run --rm `
    --name qon-ioce-opps-api `
    -p 8080:8080 `
    -e OPPS_JAR_PATH="/app/jars/pricers/opps-pricer-application-2.16.0.jar" `
    qon-ioce-opps-api:local
```

## Test locally

```powershell
Invoke-RestMethod `
    -Method Get `
    -Uri "http://127.0.0.1:8080/health/ready"
```

```powershell
$body = Get-Content .\requests\sample_opps_claim.json -Raw

$result = Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8080/api/v1/opps/process" `
    -ContentType "application/json" `
    -Body $body

$result | ConvertTo-Json -Depth 100
```

## Enable ESRD

ESRD benchmark references are off unless the container has `ESRD_ENABLED=true`.
With it on, the API startup fails deliberately unless `data/myelin.db` in the
image holds a sealed ESRD reference release, which stops every endpoint, so
check the built image first:

```powershell
docker run --rm --network none --entrypoint python `
    qon-ioce-opps-api:local -m bridge.esrd_check
```

It lists the releases the image would serve and the optional sections each one
carries, or exits non-zero with the reason the bridge would refuse to start.
When the database holds only the first release, the API prices its benchmark
without the body-size, facility-level and final-payment steps.

Then run with the setting:

```powershell
docker run --rm `
    --name qon-ioce-opps-api `
    -p 8080:8080 `
    -e ESRD_ENABLED=true `
    qon-ioce-opps-api:local
```

Compose reads the same variable: `$env:ESRD_ENABLED = "true"` before
`docker compose up`. Verify with `GET /api/v1/esrd/ready`; `/health/ready` says
nothing about ESRD.

For ECS, add `-EnableEsrd` to `aws\deploy-ioce-opps-ecs.ps1`:

```powershell
.\aws\deploy-ioce-opps-ecs.ps1 `
    -ClusterName '<cluster>' `
    -ServiceName '<service>' `
    -EnableEsrd `
    -HealthCheckUrl 'http://<bridge>/api/v1/esrd/ready'
```

- It runs the check above in the newly built image, against the database path
  the task sets, before anything is pushed. If the check fails it stops there:
  nothing is pushed, registered or deployed.
- It sets `ESRD_ENABLED=true` in the new task-definition revision and keeps
  every other setting. Omitting the option leaves the task's current ESRD
  setting as it is, on or off.
- An image promoted with `-UseExistingImage` is not checked, and the script
  says so. Check it yourself with the `docker run` command above.
- It refuses to run when `ESRD_ENABLED` is supplied by an ECS secret.

## Enable FQHC

FQHC PPS pricing is off unless the container has `FQHC_ENABLED=true`. The jar
it uses is already in the image; startup fails if its hash does not match the
reconciled release.

```powershell
docker run --rm `
    --name qon-ioce-opps-api `
    -p 8080:8080 `
    -e FQHC_ENABLED=true `
    qon-ioce-opps-api:local
```

Verify with `GET /api/v1/fqhc/ready`. For ECS, add `-EnableFqhc` to
`aws\deploy-ioce-opps-ecs.ps1`; omitting it leaves the task's current setting.

## Push to private ECR

```powershell
.\aws\build-push-ecr.ps1 `
    -Region us-west-2 `
    -RepositoryName qon-ioce-opps-api `
    -Tag v1
```

Do not publish this image to a public registry because it contains the CMS
runtime files and provider database.

## Register the Fargate task definition

The ECS task execution role should have the
`AmazonECSTaskExecutionRolePolicy` managed policy so Fargate can pull from ECR
and write CloudWatch logs.

```powershell
.\aws\register-task-definition.ps1 `
    -Region us-west-2 `
    -RepositoryName qon-ioce-opps-api `
    -Tag v1 `
    -ExecutionRoleArn "arn:aws:iam::<ACCOUNT_ID>:role/ecsTaskExecutionRole"
```

The template starts with:

- 1 vCPU
- 3 GB memory
- Linux/x86-64
- One Uvicorn worker
- Port 8080
- 120-second container health-check start period
- CloudWatch log group `/ecs/qon-ioce-opps-api`

Increase to 2 vCPU and 4 GB if startup, throughput, or JVM memory testing shows
that it is needed.

## Create or update the ECS service

Recommended initial settings:

- Launch type: Fargate
- Desired tasks: 1
- Private subnets
- Assign public IP: Disabled
- Application Load Balancer
- Target type: IP
- Container port: 8080
- Target-group health path: `/health/ready`
- Health-check grace period: 120 seconds
- ECS task security group inbound TCP 8080 from the ALB security group only

The task must be able to pull from ECR and send logs. In private subnets, use
either a working NAT route or VPC endpoints for ECR API, ECR DKR, S3, and
CloudWatch Logs.

## Important operational notes

- Keep one worker per container. Each worker is a separate process and would
  start a separate JVM.
- Scale horizontally by increasing ECS desired task count.
- The current API serializes I/OCE and OPPS processing inside each task.
- The SQLite provider database is baked into the image. Rebuild and redeploy
  the image when the OPSF data is refreshed.
- The initial task template leaves the root filesystem writable because
  SQLite/SQLAlchemy may create temporary journal files. Read-only root
  hardening should be done after moving the writable database runtime path to
  an explicit ECS volume or configuring truly read-only SQLite access.
