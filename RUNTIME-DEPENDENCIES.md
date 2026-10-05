# Pinned Docker runtime dependencies

The Dockerfile pins Python 3.11.16 to an immutable image digest and installs two
hash-verified lock files:

- `requirements-runtime.lock` covers Myelin, the FastAPI bridge and their runtime dependencies.
- `requirements-build.lock` covers pip, setuptools and wheel.

Both locks target **Linux amd64 / CPython 3.11**. They are not Windows development
requirements. Docker installs the local project with `--no-deps --no-build-isolation`
and finishes with `pip check`, so neither project installation nor the bridge can
silently upgrade the locked packages. `pyproject.toml`, `requirements.txt`, `uv.lock`
and `requirements-bridge.txt` retain their existing development roles. Changes to
those declarations require deliberately regenerating the Docker locks as well.

The runtime comparison uses the existing `requirements.txt` versions for
Pydantic (2.12.4), pydantic-core (2.41.5), JPype (1.6.0) and SQLAlchemy (2.0.44).
Bridge and PDF packages retain exact versions from the earlier image.
`typing-extensions==4.16.0` is retained because AnyIO 4.15.1 requires it; forcing
the development lock's 4.15.0 fails dependency resolution.

Build a separate local image from PowerShell:

```powershell
Set-Location C:\Users\Brad\Myelin
docker.exe build --platform linux/amd64 -f Dockerfile `
    -t qon-ioce-opps-api:runtime-pinned-20261003 .
```

The final dot is the build context. Building alone does not update an existing
container. The image copies the database in `data/myelin.db` and the existing CMS
JARs; it does not promote an external prepared database or enable another fiscal
year. The Debian package installation remains based on the package repository;
these pins do not promise a byte-identical image across future OS updates.

## Verification on October 3, 2026

The isolated comparison image `qon-ioce-opps-api:runtime-candidate-20261003`
retained the original image's Python/Java environment. All 310 Myelin source,
bridge, CMS JAR and data files matched their original SHA-256 values. Six fixed
SNF runs passed (three each against the original and prepared databases), each
with four historical fixtures and 15 loaded-provider cases. The original image
had one Python segmentation fault in its six fixed runs; this was preserved.

The fresh normal Docker build `qon-ioce-opps-api:runtime-pinned-20261003` passed
`pip check` and matched all locked package versions and all 310 original assets.
It passed 21 existing ASC regressions, the SNF contract and release checks, an
actual I/OCE -> OPSF -> OPPS calculation, and 11 provider-refresh checks. SNF and
OPPS results matched the earlier image wherever the comparisons completed.

However, the fresh build's prepared-database pricing check failed with
`MemoryError` during the SNF bridge import, before JVM startup or calculation.
**Dependency pinning fixes build drift; it did not resolve that pre-update
failure.** The post-firmware checks below establish the current verification
result. The original and prepared databases remain unchanged, and the prepared
database has not been copied into this image.

Docker had about 69 GiB of available memory, no recent Docker OOM events, and no
memory limit on the probe container. Sixteen fresh-process checks in the plain
Python base image passed without Myelin, third-party packages, a database or Java.
Those passing checks do not exclude an intermittent interpreter, native-library
or host issue. Before the firmware update, the host reported an i9-14900K, an
ASUS Z790-AYW WIFI W motherboard, and BIOS version 1805 dated October 30, 2024.
A read-only Windows CPU registry
check confirmed microcode `0x12B` (binary `2B-01-00-00`, decoded little-endian).
Intel's [instability advisory](https://www.intel.com/content/www/us/en/support/articles/000102331/processors.html)
recommends Intel Default Settings and a current BIOS containing microcode `0x12F`
or later. The [exact model's ASUS BIOS page](https://www.asus.com/me-en/supportonly/z790-ayw%20wifi%20w/helpdesk_bios/)
lists non-beta BIOS 1836 dated May 14, 2026; BIOS 1820 introduced `0x12F`.
A hardware cause has not been confirmed. The host owner was advised to follow
ASUS's firmware update instructions, use Intel Default Settings, verify the
resulting microcode, and repeat three fixed checks on the original host. Those
read-only diagnostic checks changed no firmware, BIOS settings, host services
or running containers.

The API checkout records the compact results in
`verification/SNF/runtime-verification.json`; full reports and logs are under
`artifacts/snf-runtime-candidate/20261003-082911-c5ed281e/`.

## Repeat on another Docker host

Testing the same image and prepared database on a second machine helps separate
host behavior from software behavior. Export the already-built image without
rebuilding it:

```powershell
docker.exe image save --output .\myelin-runtime-pinned-20261003.tar `
    qon-ioce-opps-api:runtime-pinned-20261003
```

Transfer the archive, the API checkout and the prepared staging folder to the
second machine, then load the image:

```powershell
docker.exe image load --input .\myelin-runtime-pinned-20261003.tar
```

From that machine's API checkout, substitute its actual staging path and perform
three fixed runs. Every attempt saves its own reports; any failure fails the set:

```powershell
$snfStage = 'C:\path\to\prepared-staging-folder'
$snfFailures = 0
foreach ($snfAttempt in 1..3) {
    try {
        .\scripts\Test-MyelinSnfPreparedDatabase.ps1 `
            -StagingDirectory $snfStage `
            -Image qon-ioce-opps-api:runtime-pinned-20261003
    }
    catch {
        $snfFailures++
        Write-Warning "Run $snfAttempt failed: $($_.Exception.Message)"
    }
}
if ($snfFailures -gt 0) { throw "$snfFailures of 3 fixed runs failed." }
```

Keep failed reports and `console.log` files. Do not discard failures after a later
pass or promote the database solely because a subsequent run happens to pass.

On October 3, 2026, the caller reported that all three fixed second-machine
checks passed. Each check used four historical fixtures and 15 loaded-provider
cases; the reported database checksum was unchanged. The raw reports and image
ID have not been received for independent verification. These passes support
investigating the original host, but do not prove a cause or resolve its earlier
intermittent failures. Preserve the second-host reports alongside the original
failed results.

## Checks after the BIOS update

On October 3, 2026, the caller updated BIOS and ran three checks on the original
host. Read-only diagnostics confirmed BIOS 1836 (firmware date April 16, 2026)
and CPU microcode `0x133`, compared with BIOS 1805 and microcode `0x12B` before.
BIOS settings themselves were not independently verified.

The three saved reports used `qon-ioce-opps-api:snf-20260930`. All passed four
historical fixtures and 15 loaded-provider cases, with successful exit codes and
unchanged database checksums. Three additional fixed checks used
`qon-ioce-opps-api:runtime-pinned-20261003` and also passed. All six pricing and
execution reports were inspected, and payments matched between the images.
The API manifest links every report; the pinned run summary is under
`artifacts/snf-runtime-candidate/20261003-082911-c5ed281e/post-bios-674002cf/`.

The intermittent failures were not reproduced in this fixed set after the BIOS
update. This supports a firmware contribution but does not establish the exact
cause or guarantee the failure cannot recur. Earlier failed results remain in
the record. Local image preparation can resume: back up the current build
database, copy the accepted prepared database into `data/myelin.db`, rebuild
with the pinned dependencies, and verify the image's embedded data and pricing
before deployment. As of October 3, that database copy, rebuild and deployment
had not occurred.

On October 4, the caller backed up the existing build database with the verified
backup script, then copied the accepted refreshed database into `data/myelin.db`.
Read-only SHA-256 checks confirmed it matches the prepared database
(`f92b4083d4b0cc950f802edd8381979e021a650427d101501a964fe6561d0dff`).
The rollback copy and matching preparation report remain in the API checkout
under `artifacts/snf-refresh/20261004-065438-3980b655/`. The next step is a new
local image build followed by checks of its embedded database and pricing;
that refreshed image has not yet been built, pushed or deployed.

The caller then built `qon-ioce-opps-api:snf-refreshed-20261004` (image ID
`sha256:c5b9ddd3afe72c94e107c82e3e9aaeafcf4ed85e97f7fe025f4f2af7494891d8`).
A read-only probe verified its embedded database hash, SQLite integrity, provider
counts and locked dependency versions. Embedded pricing passed four historical
fixtures and 15 loaded-provider cases. The existing staging mode also passed,
with matching payments. A deliberately mismatched applied refresh checksum was
correctly rejected on an isolated copy before pricing.

Step 8 can be replayed from PowerShell using the API checkout's updated script:

```powershell
Set-Location C:\Git\QON\MedicareServiceAPI
.\scripts\Test-MyelinSnfPreparedDatabase.ps1 `
    -StagingDirectory 'C:\Git\QON\MedicareServiceAPI\artifacts\snf-refresh\20261001-104137-3e5fc31c' `
    -Image 'qon-ioce-opps-api:snf-refreshed-20261004' `
    -UseImageDatabase
```

This mode verifies `/app/data/myelin.db` packaged in the image against the
accepted staging refresh report and reports `DatabaseSource: Image`. Reports
remain under the staging folder's `pricing-checks`. The script and checker are
mounted from the API checkout; changing them does not require another Myelin
image build. Report links and the retained rejection case are in the runtime
manifest's `refreshedImageVerification`. The image has not been pushed or deployed.

The caller's Step 8 replay passed against the same verified image ID. Step 9
uses the original `aws/build-push-ecr.ps1` with its new `-SourceImage` option:

```powershell
Set-Location C:\Users\Brad\Myelin
.\aws\build-push-ecr.ps1 `
    -Region us-west-2 `
    -RepositoryName qon-ioce-opps-api `
    -Tag snf-refreshed-20261004 `
    -SourceImage 'sha256:c5b9ddd3afe72c94e107c82e3e9aaeafcf4ed85e97f7fe025f4f2af7494891d8' `
    -UseTemporaryDockerConfig
```

The source is resolved to a local image ID, tagged for ECR, and pushed without a
build or pull. The default mode still builds and pushes when `-SourceImage` is
omitted. The existing temporary Docker-config option bypasses the prior Windows
credential-helper failure and removes its generated credentials afterward.
Four offline checks with mocked AWS/Docker commands passed, covering existing
image publishing, the default build flow, missing-image rejection and stopping
after a tag failure. These checks performed no actual registry push. For later
ECS deployment, select `-UseExistingImage -ImageTag snf-refreshed-20261004`.

The caller completed Step 9. AWS ECR confirmed that tag
`snf-refreshed-20261004` has the exact digest of the tested local image, and
Docker's local ECR repository digest agrees. Step 10 deploys the existing image
to the current Myelin QA service:

```powershell
Set-Location C:\Users\Brad\Myelin
.\aws\deploy-ioce-opps-ecs.ps1 `
    -ClusterName 'QON-Medicare-OPPS-QA' `
    -ServiceName 'qon-ioce-opps-api-qa-service' `
    -RepositoryName 'qon-ioce-opps-api' `
    -Region us-west-2 `
    -ImageTag 'snf-refreshed-20261004' `
    -UseExistingImage `
    -EnableSnf `
    -HealthCheckUrl 'http://QON-Medicare-OPPS-QA-ALB-1578910498.us-west-2.elb.amazonaws.com/api/v1/snf/ready'
```

Read-only ECS checks confirmed the service was active with one running task,
zero pending tasks, and previous revision `qon-ioce-opps-api:4` using image `v2`.
Its Linux/X86_64 platform matches the verified new image. The prior service and
task-definition metadata are saved in the API artifacts and linked in the
verification manifests. The deployment script waits for service stability and
prints target health and a rollback command. Retain that rollback command.
ECS deployment and deployed readiness/pricing checks are still pending.


The caller ran the initial Step 10 deployment without `-EnableSnf` on October 4. Read-only AWS checks verified revision
`qon-ioce-opps-api:5` reached `COMPLETED` with one running task and no pending
tasks. The running image digest matches both the tested image and ECR. The new
load balancer target is healthy and `/health/ready` returns 200. SNF readiness
returns 503 because `SNF_ENABLED` is absent from the ECS task. General readiness
only checks I/OCE/OPPS; deployed SNF pricing verification remains pending.

The original `aws/deploy-ioce-opps-ecs.ps1` now has an explicit `-EnableSnf`
option. Six offline PowerShell 5.1 checks with mocked AWS/Docker passed: missing
or false environment settings, duplicate flags, preserving the default behavior,
repeated enablement, and early rejection when the flag comes from an ECS secret.
Other environment entries, secrets, sidecars, ports, roles, logs and tags were
preserved. These tests performed no actual AWS/Docker operations.

SNF enablement remains part of Step 10. The command above includes `-EnableSnf`
and the SNF readiness URL. Rerun it if the initial deployment omitted the flag.

No rebuild or push is required. Retain the rollback command to revision 5.
Require an SNF readiness response with `status: ready`; an external health
warning does not pass this step. FY 2027 is still unsupported. Step 11 in the
API's `docs/SNF-loaders.md` then runs `verification/SNF/Test-LocalSnf.ps1` against
the QA bridge without LoginJson, checking historical provider payments,
multiple HIPPS segments and rejection cases without creating .NET claims.


Step 10 was subsequently completed with `-EnableSnf` on October 4. Read-only
checks verified ECS revision `qon-ioce-opps-api:6` reached `COMPLETED`, with one
running task, no pending tasks and `SNF_ENABLED=true`. Its running digest still
matches the tested image and ECR. The new load balancer target is healthy; both
`/health/ready` and `/api/v1/snf/ready` return 200. SNF readiness reports release
2.5.1, calculation years 2023–2026 and last service date September 30, 2026.

The Step 11 stateless bridge smoke test passed all eight cases in Windows
PowerShell 5.1: five historical provider periods, two HIPPS segments totaling
3101.78, and 422 rejection of missing bridge prior days and 22X. Saved responses
also matched yearly calculation versions and provider effective dates. No
LoginJson was supplied and no .NET claim submissions were created. Reports are
under the API checkout's `artifacts/snf-deployed/20261004-134733-9a871766/`, with
`verification/SNF/deployed-pricing-verification.json` providing the compact
record. Step 11 can be replayed using the command in `docs/SNF-loaders.md`.
This checks one provider through the QA Myelin bridge; deployed .NET claim
flows and production rollout were not tested. FY 2027 remains unsupported.


Step 12 subsequently verified the local Docker .NET API configured for this QA
Myelin service. The combined PowerShell 7 workflow passed ten phases: schema,
readiness, bridge/API payment matching, prior-day history, duplicate retries,
replacements/voids, trusted ownership, concurrent patients, API rejection of
22X and FY 2027, and diagnosis/payment persistence. New-stay and continuation
claims 10433/10434 persisted AssumedNewStay/0 and ClaimHistory/6. Eight patients
with sixteen concurrent requests retained one claim and attempt per patient.
Reports are under the API checkout's
`artifacts/snf-qa/20261004-195250-9437b5ef/`; the compact record is
`verification/SNF/qa-workflow-verification.json`. Replay commands are in Step 12
of `docs/SNF-loaders.md`. The runner prompts for the login without saving it.
Synthetic claims remain in MCService. This used one active submitter; testing
live isolation between two submitters requires a second authenticated test
account. The production .NET deployment remains unverified.
