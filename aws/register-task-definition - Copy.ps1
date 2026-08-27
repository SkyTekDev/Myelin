param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutionRoleArn,

    [string]$Region = "us-west-2",
    [string]$RepositoryName = "qon-ioce-opps-api",

    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$templatePath = Join-Path $scriptDirectory "ecs-task-definition.template.json"
$outputPath = Join-Path $scriptDirectory "ecs-task-definition.generated.json"

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

$rendered = Get-Content $templatePath -Raw
$rendered = $rendered.Replace("__ACCOUNT_ID__", $accountId)
$rendered = $rendered.Replace("__REGION__", $Region)
$rendered = $rendered.Replace("__REPOSITORY__", $RepositoryName)
$rendered = $rendered.Replace("__TAG__", $Tag)
$rendered = $rendered.Replace("__EXECUTION_ROLE_ARN__", $ExecutionRoleArn)

Set-Content `
    -Path $outputPath `
    -Value $rendered `
    -Encoding UTF8

aws logs create-log-group `
    --log-group-name "/ecs/qon-ioce-opps-api" `
    --region $Region `
    @awsProfileArgs *> $null

$cliPath = $outputPath.Replace("\", "/")

aws ecs register-task-definition `
    --cli-input-json "file://$cliPath" `
    --region $Region `
    @awsProfileArgs

if ($LASTEXITCODE -ne 0) {
    throw "Task definition registration failed."
}

Write-Host ""
Write-Host "Registered the task definition."
Write-Host "Generated definition: $outputPath"
