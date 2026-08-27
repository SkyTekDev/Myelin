param(
    [string]$Region = "us-west-2",
    [string]$RepositoryName = "qon-ioce-opps-api",
    [string]$Tag = "",
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Tag)) {
    $Tag = Get-Date -Format "yyyyMMdd-HHmmss"
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

Write-Host "Logging in to $registry..."

$password = aws ecr get-login-password `
    --region $Region `
    @awsProfileArgs

if ($LASTEXITCODE -ne 0) {
    throw "Unable to retrieve an ECR login password."
}

$password | docker login `
    --username AWS `
    --password-stdin $registry

if ($LASTEXITCODE -ne 0) {
    throw "Docker login to ECR failed."
}

Write-Host "Building $imageUri..."
docker build --pull -t $imageUri .

if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed."
}

Write-Host "Pushing $imageUri..."
docker push $imageUri

if ($LASTEXITCODE -ne 0) {
    throw "Docker push failed."
}

Write-Host ""
Write-Host "Image pushed successfully:"
Write-Host $imageUri
