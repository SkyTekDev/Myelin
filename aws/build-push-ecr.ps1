param(
    [string]$Region = "us-west-2",
    [string]$RepositoryName = "qon-ioce-opps-api",
    [string]$Tag = "",
    [string]$Profile = "",
    # Bypass a broken Windows/Desktop credential store for this invocation only.
    [switch]$UseTemporaryDockerConfig,
    # Resolve and push an existing local image without rebuilding it.
    [string]$SourceImage = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Tag)) {
    $Tag = Get-Date -Format "yyyyMMdd-HHmmss"
}

$sourceImageId = $null
if (-not [string]::IsNullOrWhiteSpace($SourceImage)) {
    $sourceImageId = docker image inspect --format '{{.Id}}' $SourceImage
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceImageId)) {
        throw "SourceImage must identify an existing local Docker image. No image will be pulled."
    }
    $sourceImageId = $sourceImageId.Trim()
}

$awsProfileArgs = @()

if (-not [string]::IsNullOrWhiteSpace($Profile)) {
    $awsProfileArgs = @("--profile", $Profile)
}

$accountId = (
    aws sts get-caller-identity `
        --query Account `
        --output text `
        --region $Region `
        @awsProfileArgs
).Trim()

if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($accountId)) {
    throw "Unable to determine the AWS account ID."
}

$registry = "$accountId.dkr.ecr.$Region.amazonaws.com"
$imageUri = "$registry/$RepositoryName`:$Tag"

aws ecr describe-repositories `
    --repository-names $RepositoryName `
    --region $Region `
    @awsProfileArgs *> $null

if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating ECR repository $RepositoryName..."

    aws ecr create-repository `
        --repository-name $RepositoryName `
        --image-scanning-configuration scanOnPush=true `
        --region $Region `
        @awsProfileArgs | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the ECR repository."
    }
}

Write-Host "Authenticating to $registry..."

$password = aws ecr get-login-password `
    --region $Region `
    @awsProfileArgs

if ($LASTEXITCODE -ne 0) {
    throw "Unable to retrieve an ECR login password."
}

$temporaryConfig = $null
$dockerArgs = @()
$savedDockerContext = $env:DOCKER_CONTEXT
try {
    if ($UseTemporaryDockerConfig) {
        # Resolve the current endpoint before selecting an isolated config. Merely
        # removing credsStore is insufficient: Docker may auto-detect wincred.
        $endpoint = docker context inspect --format '{{.Endpoints.docker.Host}}'
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($endpoint)) {
            throw 'Unable to resolve the active Docker endpoint.'
        }
        $endpoint = $endpoint.Trim()
        if (![string]::IsNullOrWhiteSpace($env:DOCKER_HOST) -and
            [string]::IsNullOrWhiteSpace($env:DOCKER_CONTEXT)) {
            $endpoint = $env:DOCKER_HOST
        }
        if ($endpoint -notmatch '^(npipe|unix)://') {
            throw 'Temporary authentication currently supports local Docker named-pipe or Unix-socket endpoints only.'
        }
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        $temporaryConfig = Join-Path $tempRoot ('myelin-ecr-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temporaryConfig | Out-Null
        $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$password"))
        @{ auths = @{ $registry = @{ auth = $auth } } } |
            ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath (Join-Path $temporaryConfig 'config.json') -Encoding Ascii
        $auth = $null
        # Pin the original local engine; desktop-linux context metadata is not
        # present in the isolated directory. Clear and restore the context env.
        $env:DOCKER_CONTEXT = $null
        $dockerArgs = @('--config', $temporaryConfig, '--host', $endpoint)
        Write-Host 'Using temporary ECR authentication; Docker credential helpers are bypassed.'
    }
    else {
        $password | docker login --username AWS --password-stdin $registry
        if ($LASTEXITCODE -ne 0) {
            throw 'Docker login to ECR failed. For a local credential-store failure, retry with -UseTemporaryDockerConfig.'
        }
    }
    $password = $null

    if ($sourceImageId) {
        Write-Host "Tagging existing image $sourceImageId as $imageUri..."
        docker @dockerArgs tag $sourceImageId $imageUri
        if ($LASTEXITCODE -ne 0) { throw 'Docker image tagging failed.' }
    }
    else {
        Write-Host "Building $imageUri..."
        $buildContext = Split-Path -Parent $PSScriptRoot
        docker @dockerArgs build --pull -t $imageUri $buildContext
        if ($LASTEXITCODE -ne 0) { throw 'Docker build failed.' }
    }

    Write-Host "Pushing $imageUri..."
    docker @dockerArgs push $imageUri
    if ($LASTEXITCODE -ne 0) { throw 'Docker push failed.' }
}
finally {
    $password = $null
    $auth = $null
    $env:DOCKER_CONTEXT = $savedDockerContext
    if ($temporaryConfig) {
        # Verify the absolute deletion target remains the uniquely created temp
        # child, never a caller-supplied path or the user's Docker config folder.
        $resolvedTarget = [IO.Path]::GetFullPath($temporaryConfig)
        if ([IO.Path]::GetDirectoryName($resolvedTarget) -ne $tempRoot.TrimEnd('\', '/') -or
            [IO.Path]::GetFileName($resolvedTarget) -notmatch '^myelin-ecr-[a-f0-9]{32}$') {
            throw 'Refusing cleanup outside the generated ECR temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}

Write-Host ""
Write-Host "Image pushed successfully:"
Write-Host $imageUri
