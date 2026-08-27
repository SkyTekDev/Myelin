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

$LogGroupName = "/ecs/$RepositoryName"

Write-Host "Ensuring CloudWatch log group '$LogGroupName' exists..."

$previousErrorActionPreference = $ErrorActionPreference

try {
    $ErrorActionPreference = "Continue"

    $createOutput = & aws logs create-log-group `
        --region $Region `
        --log-group-name $LogGroupName `
        --no-cli-pager 2>&1

    $createExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}

if ($createExitCode -ne 0) {
    $errorText = $createOutput | Out-String

    if ($errorText -match "ResourceAlreadyExistsException") {
        Write-Host "CloudWatch log group already exists."
    }
    else {
        throw @"
Failed to create CloudWatch log group '$LogGroupName'.

AWS CLI output:
$errorText
"@
    }
}

$cliPath = $outputPath.Replace("\", "/")

# Validate the JSON before sending it to AWS.
try {
    $null = $TaskDefinitionJson | ConvertFrom-Json -ErrorAction Stop
}
catch {
    Write-Host "`nGenerated task-definition JSON is invalid:" -ForegroundColor Red
    Write-Host $TaskDefinitionJson
    throw "Task definition JSON validation failed: $($_.Exception.Message)"
}

$tempJsonPath = Join-Path `
    $env:TEMP `
    "ecs-task-definition-$([Guid]::NewGuid().ToString('N')).json"

try {
    # AWS CLI expects a normal UTF-8 JSON file.
    [System.IO.File]::WriteAllText(
        $tempJsonPath,
        $TaskDefinitionJson,
        [System.Text.UTF8Encoding]::new($false)
    )

    $jsonFileUri = "file:///" + ($tempJsonPath -replace '\\', '/')

    Write-Host "Registering ECS task definition..."
    Write-Host "Task definition file: $tempJsonPath"

    $registrationOutput = & aws ecs register-task-definition `
        --region $Region `
        --cli-input-json $jsonFileUri `
        --no-cli-pager 2>&1

    $registrationExitCode = $LASTEXITCODE

    if ($registrationExitCode -ne 0) {
        Write-Host "`nAWS CLI output:" -ForegroundColor Red
        $registrationOutput | ForEach-Object {
            Write-Host $_
        }

        throw "Task definition registration failed."
    }

    $registrationJson = $registrationOutput | Out-String | ConvertFrom-Json

    $taskDefinitionArn = $registrationJson.taskDefinition.taskDefinitionArn

    Write-Host ""
    Write-Host "Task definition registered successfully." -ForegroundColor Green
    Write-Host "Task definition ARN: $taskDefinitionArn"
}
finally {
    Remove-Item $tempJsonPath -Force -ErrorAction SilentlyContinue
}

if ($LASTEXITCODE -ne 0) {
    throw "Task definition registration failed."
}

Write-Host ""
Write-Host "Registered the task definition."
Write-Host "Generated definition: $outputPath"
