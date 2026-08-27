<#
.SYNOPSIS
Registers a Linux AWS ECS Fargate task definition for an image stored in Amazon ECR.

.DESCRIPTION
This script:

1. Verifies that the AWS CLI is available and that the active AWS account
   matches the account in the execution-role ARN.
2. Optionally verifies that the requested ECR image tag exists.
3. Creates the CloudWatch Logs log group if it does not already exist.
4. Optionally applies a CloudWatch Logs retention policy.
5. Builds the ECS task definition from PowerShell objects.
6. Writes valid UTF-8 JSON without a BOM.
7. Registers the task definition using --cli-input-json file://...
8. Displays the registered task definition ARN and revision.

.EXAMPLE
.\aws\register-task-definition.ps1 `
    -Region us-west-2 `
    -RepositoryName qon-ioce-opps-api `
    -Tag v1 `
    -ExecutionRoleArn "arn:aws:iam::997438683866:role/ecsTaskExecutionRole"

.EXAMPLE
.\aws\register-task-definition.ps1 `
    -Region us-west-2 `
    -RepositoryName qon-ioce-opps-api `
    -Tag v1 `
    -ExecutionRoleArn "arn:aws:iam::997438683866:role/ecsTaskExecutionRole" `
    -TaskRoleArn "arn:aws:iam::997438683866:role/qon-ioce-opps-task-role" `
    -Cpu 2048 `
    -Memory 4096 `
    -ContainerPort 8080 `
    -EnvironmentVariables @{
        "ASPNETCORE_ENVIRONMENT" = "Production"
    }

.NOTES
The execution role is used by ECS to pull the image from ECR and send logs
to CloudWatch Logs. A task role is separate and is only needed when the
application inside the container calls AWS services.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Region,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Tag,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExecutionRoleArn,

    [Parameter(Mandatory = $false)]
    [string]$TaskRoleArn = "",

    [Parameter(Mandatory = $false)]
    [string]$Family = "",

    [Parameter(Mandatory = $false)]
    [string]$ContainerName = "",

    [Parameter(Mandatory = $false)]
    [ValidateRange(1, 65535)]
    [int]$ContainerPort = 8080,

    [Parameter(Mandatory = $false)]
    [ValidateSet(256, 512, 1024, 2048, 4096, 8192, 16384)]
    [int]$Cpu = 1024,

    [Parameter(Mandatory = $false)]
    [ValidateRange(512, 122880)]
    [int]$Memory = 2048,

    [Parameter(Mandatory = $false)]
    [ValidateSet("X86_64", "ARM64")]
    [string]$CpuArchitecture = "X86_64",

    [Parameter(Mandatory = $false)]
    [string]$LogGroupName = "",

    [Parameter(Mandatory = $false)]
    [ValidateSet(
        0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180,
        365, 400, 545, 731, 1096, 1827, 2192, 2557,
        2922, 3288, 3653
    )]
    [int]$LogRetentionDays = 30,

    [Parameter(Mandatory = $false)]
    [ValidateRange(20, 200)]
    [int]$EphemeralStorageGiB = 20,

    [Parameter(Mandatory = $false)]
    [hashtable]$EnvironmentVariables = @{},

    [Parameter(Mandatory = $false)]
    [hashtable]$Secrets = @{},

    [Parameter(Mandatory = $false)]
    [switch]$SkipImageCheck,

    [Parameter(Mandatory = $false)]
    [switch]$KeepJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-AwsCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $false)]
        [switch]$AllowFailure
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $output = @()
    $exitCode = -1

    try {
        # Windows PowerShell converts native stderr into NativeCommandError.
        # Continue temporarily so that we can inspect the AWS CLI exit code.
        $ErrorActionPreference = "Continue"
        $output = & $script:AwsExecutable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $outputText = (
        @($output) |
            ForEach-Object { $_.ToString() }
    ) -join [Environment]::NewLine

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $displayCommand = "aws " + ($Arguments -join " ")
        throw @"
AWS CLI command failed with exit code $exitCode.

Command:
$displayCommand

AWS CLI output:
$outputText
"@
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output   = $outputText
    }
}

function Assert-ValidFargateSize {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [int]$CpuValue,

        [Parameter(Mandatory = $true)]
        [int]$MemoryValue
    )

    $isValid = $false

    switch ($CpuValue) {
        256 {
            $isValid = $MemoryValue -in @(512, 1024, 2048)
        }

        512 {
            $isValid = $MemoryValue -in @(1024, 2048, 3072, 4096)
        }

        1024 {
            $isValid =
                $MemoryValue -ge 2048 -and
                $MemoryValue -le 8192 -and
                ($MemoryValue % 1024 -eq 0)
        }

        2048 {
            $isValid =
                $MemoryValue -ge 4096 -and
                $MemoryValue -le 16384 -and
                ($MemoryValue % 1024 -eq 0)
        }

        4096 {
            $isValid =
                $MemoryValue -ge 8192 -and
                $MemoryValue -le 30720 -and
                ($MemoryValue % 1024 -eq 0)
        }

        8192 {
            $isValid =
                $MemoryValue -ge 16384 -and
                $MemoryValue -le 61440 -and
                ($MemoryValue % 4096 -eq 0)
        }

        16384 {
            $isValid =
                $MemoryValue -ge 32768 -and
                $MemoryValue -le 122880 -and
                ($MemoryValue % 8192 -eq 0)
        }
    }

    if (-not $isValid) {
        throw "The Fargate CPU/memory combination is invalid: CPU=$CpuValue, Memory=$MemoryValue MiB."
    }
}

function Get-RoleAccountId {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RoleArn
    )

    if ($RoleArn -notmatch '^arn:(aws|aws-us-gov|aws-cn):iam::(?<AccountId>\d{12}):role\/.+$') {
        throw "The IAM role ARN is not valid: $RoleArn"
    }

    return $Matches.AccountId
}

try {
    Write-Host ""
    Write-Host "Registering ECS Fargate task definition" -ForegroundColor Cyan
    Write-Host "---------------------------------------" -ForegroundColor Cyan

    $awsCommand = Get-Command aws -CommandType Application -ErrorAction Stop
    $script:AwsExecutable = $awsCommand.Source

    if ([string]::IsNullOrWhiteSpace($Family)) {
        $Family = $RepositoryName
    }

    if ([string]::IsNullOrWhiteSpace($ContainerName)) {
        $ContainerName = $RepositoryName
    }

    if ([string]::IsNullOrWhiteSpace($LogGroupName)) {
        $LogGroupName = "/ecs/$RepositoryName"
    }

    Assert-ValidFargateSize -CpuValue $Cpu -MemoryValue $Memory

    Write-Host "AWS CLI:          $script:AwsExecutable"
    Write-Host "Region:           $Region"
    Write-Host "Repository:       $RepositoryName"
    Write-Host "Image tag:        $Tag"
    Write-Host "Task family:      $Family"
    Write-Host "Container name:   $ContainerName"
    Write-Host "Container port:   $ContainerPort"
    Write-Host "Task CPU:         $Cpu"
    Write-Host "Task memory:      $Memory MiB"
    Write-Host "Architecture:     $CpuArchitecture"
    Write-Host "Log group:        $LogGroupName"

    Write-Host ""
    Write-Host "Reading the active AWS identity..."

    $identityResult = Invoke-AwsCli -Arguments @(
        "sts",
        "get-caller-identity",
        "--region", $Region,
        "--output", "json",
        "--no-cli-pager"
    )

    try {
        $identity = $identityResult.Output | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "AWS returned invalid identity JSON: $($identityResult.Output)"
    }

    $accountId = [string]$identity.Account

    if ([string]::IsNullOrWhiteSpace($accountId)) {
        throw "AWS STS did not return an account ID."
    }

    Write-Host "AWS account:      $accountId"
    Write-Host "AWS principal:    $($identity.Arn)"

    $executionRoleAccountId = Get-RoleAccountId -RoleArn $ExecutionRoleArn

    if ($executionRoleAccountId -ne $accountId) {
        throw @"
The active AWS account does not match the execution role.

Active account:        $accountId
Execution-role account: $executionRoleAccountId
"@
    }

    if (-not [string]::IsNullOrWhiteSpace($TaskRoleArn)) {
        $taskRoleAccountId = Get-RoleAccountId -RoleArn $TaskRoleArn

        if ($taskRoleAccountId -ne $accountId) {
            throw @"
The active AWS account does not match the task role.

Active account:   $accountId
Task-role account: $taskRoleAccountId
"@
        }
    }

    $imageUri = "$accountId.dkr.ecr.$Region.amazonaws.com/${RepositoryName}:$Tag"

    if (-not $SkipImageCheck) {
        Write-Host ""
        Write-Host "Checking that ECR image '$imageUri' exists..."

        $imageResult = Invoke-AwsCli -Arguments @(
            "ecr",
            "describe-images",
            "--region", $Region,
            "--repository-name", $RepositoryName,
            "--image-ids", "imageTag=$Tag",
            "--query", "imageDetails[0].imageDigest",
            "--output", "text",
            "--no-cli-pager"
        ) -AllowFailure

        if ($imageResult.ExitCode -ne 0) {
            throw @"
The ECR image could not be found or queried.

Image:
$imageUri

AWS CLI output:
$($imageResult.Output)
"@
        }

        $imageDigest = $imageResult.Output.Trim()

        if ([string]::IsNullOrWhiteSpace($imageDigest) -or $imageDigest -eq "None") {
            throw "ECR did not return an image digest for '$imageUri'."
        }

        Write-Host "ECR image found:  $imageDigest"
    }
    else {
        Write-Warning "ECR image validation was skipped."
    }

    Write-Host ""
    Write-Host "Ensuring CloudWatch log group '$LogGroupName' exists..."

    $createLogGroupResult = Invoke-AwsCli -Arguments @(
        "logs",
        "create-log-group",
        "--region", $Region,
        "--log-group-name", $LogGroupName,
        "--no-cli-pager"
    ) -AllowFailure

    if ($createLogGroupResult.ExitCode -eq 0) {
        Write-Host "CloudWatch log group created."
    }
    elseif ($createLogGroupResult.Output -match "ResourceAlreadyExistsException") {
        Write-Host "CloudWatch log group already exists."
    }
    else {
        throw @"
CloudWatch log group creation failed.

AWS CLI output:
$($createLogGroupResult.Output)
"@
    }

    if ($LogRetentionDays -gt 0) {
        Write-Host "Setting log retention to $LogRetentionDays days..."

        $null = Invoke-AwsCli -Arguments @(
            "logs",
            "put-retention-policy",
            "--region", $Region,
            "--log-group-name", $LogGroupName,
            "--retention-in-days", $LogRetentionDays.ToString(),
            "--no-cli-pager"
        )
    }
    else {
        Write-Host "Log retention policy was not changed."
    }

    $containerDefinition = [ordered]@{
        name      = $ContainerName
        image     = $imageUri
        essential = $true

        portMappings = @(
            [ordered]@{
                containerPort = $ContainerPort
                hostPort      = $ContainerPort
                protocol      = "tcp"
            }
        )

        logConfiguration = [ordered]@{
            logDriver = "awslogs"

            options = [ordered]@{
                "awslogs-group"         = $LogGroupName
                "awslogs-region"        = $Region
                "awslogs-stream-prefix" = "ecs"
            }
        }
    }

    if ($EnvironmentVariables.Count -gt 0) {
        $environmentList = @(
            foreach ($name in ($EnvironmentVariables.Keys | Sort-Object)) {
                [ordered]@{
                    name  = [string]$name
                    value = [string]$EnvironmentVariables[$name]
                }
            }
        )

        $containerDefinition["environment"] = $environmentList
    }

    if ($Secrets.Count -gt 0) {
        $secretList = @(
            foreach ($name in ($Secrets.Keys | Sort-Object)) {
                [ordered]@{
                    name      = [string]$name
                    valueFrom = [string]$Secrets[$name]
                }
            }
        )

        $containerDefinition["secrets"] = $secretList
    }

    $taskDefinition = [ordered]@{
        family                  = $Family
        networkMode             = "awsvpc"
        requiresCompatibilities = @("FARGATE")
        cpu                     = $Cpu.ToString()
        memory                  = $Memory.ToString()
        executionRoleArn        = $ExecutionRoleArn

        runtimePlatform = [ordered]@{
            cpuArchitecture        = $CpuArchitecture
            operatingSystemFamily = "LINUX"
        }

        containerDefinitions = @(
            $containerDefinition
        )
    }

    if (-not [string]::IsNullOrWhiteSpace($TaskRoleArn)) {
        $taskDefinition["taskRoleArn"] = $TaskRoleArn
    }

    if ($EphemeralStorageGiB -gt 20) {
        $taskDefinition["ephemeralStorage"] = [ordered]@{
            sizeInGiB = $EphemeralStorageGiB
        }
    }

    $taskDefinitionJson = $taskDefinition | ConvertTo-Json -Depth 30

    # Validate the generated JSON before calling AWS.
    try {
        $null = $taskDefinitionJson | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Generated task-definition JSON is invalid: $($_.Exception.Message)"
    }

    $temporaryJsonPath = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        "ecs-task-definition-$([Guid]::NewGuid().ToString('N')).json"

    $savedJsonPath = Join-Path `
        $PSScriptRoot `
        "$Family-task-definition.json"

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)

    [System.IO.File]::WriteAllText(
        $temporaryJsonPath,
        $taskDefinitionJson,
        $utf8WithoutBom
    )

    if ($KeepJson) {
        [System.IO.File]::WriteAllText(
            $savedJsonPath,
            $taskDefinitionJson,
            $utf8WithoutBom
        )

        Write-Host ""
        Write-Host "Task-definition JSON saved to:"
        Write-Host $savedJsonPath
    }

    # AWS CLI removes the file:// prefix and reads the remaining Windows path.
    $jsonFileArgument = "file://$($temporaryJsonPath -replace '\\', '/')"

    Write-Host ""
    Write-Host "Registering task definition with Amazon ECS..."

    $registrationResult = Invoke-AwsCli -Arguments @(
        "ecs",
        "register-task-definition",
        "--region", $Region,
        "--cli-input-json", $jsonFileArgument,
        "--output", "json",
        "--no-cli-pager"
    )

    try {
        $registration = $registrationResult.Output | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw @"
The task definition may have registered, but the AWS response could not be parsed.

AWS CLI output:
$($registrationResult.Output)
"@
    }

    $registeredTaskDefinition = $registration.taskDefinition

    if ($null -eq $registeredTaskDefinition) {
        throw "AWS did not return the registered task-definition details."
    }

    Write-Host ""
    Write-Host "Task definition registered successfully." -ForegroundColor Green
    Write-Host "Family:            $($registeredTaskDefinition.family)"
    Write-Host "Revision:          $($registeredTaskDefinition.revision)"
    Write-Host "Status:            $($registeredTaskDefinition.status)"
    Write-Host "Task definition:   $($registeredTaskDefinition.taskDefinitionArn)"
    Write-Host "Image:             $imageUri"
    Write-Host ""

    [pscustomobject]@{
        Family            = [string]$registeredTaskDefinition.family
        Revision          = [int]$registeredTaskDefinition.revision
        Status            = [string]$registeredTaskDefinition.status
        TaskDefinitionArn = [string]$registeredTaskDefinition.taskDefinitionArn
        ImageUri          = $imageUri
        LogGroupName      = $LogGroupName
    }
}
catch {
    Write-Host ""
    Write-Host "Task definition registration failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    if (
        (Get-Variable -Name temporaryJsonPath -Scope Local -ErrorAction SilentlyContinue) -and
        -not [string]::IsNullOrWhiteSpace($temporaryJsonPath) -and
        (Test-Path -LiteralPath $temporaryJsonPath)
    ) {
        Remove-Item -LiteralPath $temporaryJsonPath -Force -ErrorAction SilentlyContinue
    }
}
