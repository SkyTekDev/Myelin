[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ClusterName,

    [Parameter(Mandatory = $true)]
    [string]$ServiceName,

    [string]$RepositoryName = "qon-ioce-opps-api",

    # Leave blank to infer it from the ECS service load-balancer mapping or
    # from a single-container task definition.
    [string]$ContainerName = "",

    [string]$Region = "us-west-2",

    [string]$Profile = "",

    [string]$EnvironmentName = "qa",

    # When omitted, a unique tag is generated from the environment, UTC time,
    # and the current Git commit when available.
    [string]$ImageTag = "",

    [string]$BuildContext = ".",

    [string]$Dockerfile = "Dockerfile",

    # Leave blank to infer linux/amd64 or linux/arm64 from the current task
    # definition's runtimePlatform.
    [string]$BuildPlatform = "",

    # Use this to promote an image that already exists in ECR. No local Docker
    # build or push is performed.
    [switch]$UseExistingImage,

    [switch]$NoCache,

    [switch]$SkipWait,

    # Optional externally reachable endpoint to test after ECS is stable.
    [string]$HealthCheckUrl = "",

    [ValidateRange(1, 600)]
    [int]$HealthCheckTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptVersion = "4.0-windows-paramfile-fix"
Write-Host "Deployment script version: $ScriptVersion" -ForegroundColor DarkGray

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function ConvertTo-CommandText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $quoted = foreach ($argument in $Arguments) {
        if ($argument -match '[\s"]') {
            '"' + $argument.Replace('"', '\"') + '"'
        }
        else {
            $argument
        }
    }

    return "$FilePath $($quoted -join ' ')"
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$AllowFailure
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $nativePreferenceVariable = Get-Variable `
        -Name PSNativeCommandUseErrorActionPreference `
        -ErrorAction SilentlyContinue

    $previousNativePreference = $null
    $output = @()
    $exitCode = -1

    try {
        # Windows PowerShell 5.1 converts native stderr lines into PowerShell
        # ErrorRecord objects. Use Continue here so the real process exit code
        # and message can be captured.
        $ErrorActionPreference = "Continue"

        if ($null -ne $nativePreferenceVariable) {
            $previousNativePreference = $PSNativeCommandUseErrorActionPreference
            $PSNativeCommandUseErrorActionPreference = $false
        }

        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference

        if ($null -ne $nativePreferenceVariable) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }

    $text = (
        $output |
        ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            }
            else {
                $_.ToString()
            }
        }
    ) -join [Environment]::NewLine

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $commandText = ConvertTo-CommandText `
            -FilePath $FilePath `
            -Arguments $Arguments

        throw @"
Native command failed with exit code $exitCode.

Command:
$commandText

Output:
$text
"@
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Text = $text
    }
}

function Invoke-NativeLive {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    # Docker BuildKit writes normal progress output to stderr. Windows
    # PowerShell 5.1 converts native stderr into NativeCommandError records,
    # which makes a successful Docker build look like a PowerShell failure.
    # Run the command through a temporary .cmd file and merge stderr into
    # stdout inside cmd.exe so PowerShell receives ordinary output while the
    # native exit code is still preserved.
    $commandText = ConvertTo-CommandText `
        -FilePath $FilePath `
        -Arguments $Arguments

    $temporaryCommandFile = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("qon-native-" + [Guid]::NewGuid().ToString("N") + ".cmd")

    $commandFileContent = @"
@echo off
$commandText 2>&1
exit /b %ERRORLEVEL%
"@

    Write-Utf8NoBom `
        -Path $temporaryCommandFile `
        -Content $commandFileContent

    $previousErrorActionPreference = $ErrorActionPreference
    $nativePreferenceVariable = Get-Variable `
        -Name PSNativeCommandUseErrorActionPreference `
        -ErrorAction SilentlyContinue

    $previousNativePreference = $null
    $exitCode = -1

    try {
        $ErrorActionPreference = "Continue"

        if ($null -ne $nativePreferenceVariable) {
            $previousNativePreference = $PSNativeCommandUseErrorActionPreference
            $PSNativeCommandUseErrorActionPreference = $false
        }

        & cmd.exe /d /c $temporaryCommandFile
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference

        if ($null -ne $nativePreferenceVariable) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }

        if (Test-Path -LiteralPath $temporaryCommandFile) {
            Remove-Item `
                -LiteralPath $temporaryCommandFile `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }

    if ($exitCode -ne 0) {
        throw @"
Native command failed with exit code $exitCode.

Command:
$commandText
"@
    }
}

function Get-AwsCommonArguments {
    $arguments = @(
        "--region", $Region,
        "--no-cli-pager"
    )

    if (-not [string]::IsNullOrWhiteSpace($Profile)) {
        $arguments += @("--profile", $Profile)
    }

    return $arguments
}

function Invoke-AwsCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$AllowFailure
    )

    $allArguments = @($Arguments) + @(Get-AwsCommonArguments)

    return Invoke-NativeCapture `
        -FilePath "aws" `
        -Arguments $allArguments `
        -AllowFailure:$AllowFailure
}

function Invoke-AwsJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $result = Invoke-AwsCapture -Arguments (
        @($Arguments) + @("--output", "json")
    )

    if ([string]::IsNullOrWhiteSpace($result.Text)) {
        return $null
    }

    try {
        return $result.Text | ConvertFrom-Json
    }
    catch {
        throw @"
AWS CLI returned output that could not be parsed as JSON.

Command:
aws $($Arguments -join " ")

Output:
$($result.Text)
"@
    }
}

function ConvertTo-AwsFileUri {
    param([Parameter(Mandatory = $true)][string]$Path)

    # AWS CLI's documented Windows format is:
    #     file://C:\path\to\file.json
    #
    # Keep the normal Windows drive path and backslashes. Do not produce
    # file:///C:/..., because AWS CLI interprets that as /C:/...
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    return "file://$fullPath"
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Remove-ObjectProperty {
    param(
        [Parameter(Mandatory = $true)]
        [object]$InputObject,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if ($null -ne $InputObject.PSObject.Properties[$Name]) {
        $InputObject.PSObject.Properties.Remove($Name)
    }
}

function Get-GeneratedImageTag {
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $prefix = $EnvironmentName.Trim().ToLowerInvariant()

    if ([string]::IsNullOrWhiteSpace($prefix)) {
        $prefix = "build"
    }

    $gitResult = Invoke-NativeCapture `
        -FilePath "git" `
        -Arguments @("rev-parse", "--short", "HEAD") `
        -AllowFailure

    if (
        $gitResult.ExitCode -eq 0 -and
        -not [string]::IsNullOrWhiteSpace($gitResult.Text)
    ) {
        $commit = $gitResult.Text.Trim()
        return "$prefix-$timestamp-$commit"
    }

    return "$prefix-$timestamp"
}

function Show-DeploymentDiagnostics {
    Write-Host ""
    Write-Host "Recent ECS service events:" -ForegroundColor Yellow

    try {
        $serviceResponse = Invoke-AwsJson -Arguments @(
            "ecs", "describe-services",
            "--cluster", $ClusterName,
            "--services", $ServiceName
        )

        $events = @($serviceResponse.services[0].events) |
            Select-Object -First 12 |
            Select-Object createdAt, message

        if ($events.Count -gt 0) {
            $events | Format-Table -Wrap -AutoSize
        }
    }
    catch {
        Write-Warning (
            "Unable to retrieve ECS service events: " +
            $_.Exception.Message
        )
    }

    Write-Host ""
    Write-Host "Recently stopped tasks:" -ForegroundColor Yellow

    try {
        $listResponse = Invoke-AwsJson -Arguments @(
            "ecs", "list-tasks",
            "--cluster", $ClusterName,
            "--service-name", $ServiceName,
            "--desired-status", "STOPPED",
            "--max-results", "10"
        )

        $taskArns = @($listResponse.taskArns)

        if ($taskArns.Count -eq 0) {
            Write-Host "No stopped tasks were returned."
            return
        }

        $describeArguments = @(
            "ecs", "describe-tasks",
            "--cluster", $ClusterName,
            "--tasks"
        ) + $taskArns

        $tasksResponse = Invoke-AwsJson -Arguments $describeArguments

        foreach ($task in @($tasksResponse.tasks)) {
            Write-Host ""
            Write-Host "Task: $($task.taskArn)"
            Write-Host "Stopped reason: $($task.stoppedReason)"

            foreach ($container in @($task.containers)) {
                Write-Host (
                    "Container {0}: exitCode={1}; reason={2}" -f
                    $container.name,
                    $container.exitCode,
                    $container.reason
                )
            }
        }
    }
    catch {
        Write-Warning (
            "Unable to retrieve stopped-task diagnostics: " +
            $_.Exception.Message
        )
    }
}

function Show-TargetHealth {
    param([object]$Service)

    $loadBalancers = @($Service.loadBalancers)

    if ($loadBalancers.Count -eq 0) {
        return
    }

    foreach ($loadBalancer in $loadBalancers) {
        if ([string]::IsNullOrWhiteSpace($loadBalancer.targetGroupArn)) {
            continue
        }

        try {
            Write-Host ""
            Write-Host (
                "Target health for {0}:" -f
                $loadBalancer.targetGroupArn
            ) -ForegroundColor Yellow

            $targetResponse = Invoke-AwsJson -Arguments @(
                "elbv2", "describe-target-health",
                "--target-group-arn",
                $loadBalancer.targetGroupArn
            )

            $rows = foreach (
                $description in @(
                    $targetResponse.TargetHealthDescriptions
                )
            ) {
                [pscustomobject]@{
                    Target = $description.Target.Id
                    Port = $description.Target.Port
                    State = $description.TargetHealth.State
                    Reason = $description.TargetHealth.Reason
                    Description = $description.TargetHealth.Description
                }
            }

            if (@($rows).Count -gt 0) {
                $rows | Format-Table -Wrap -AutoSize
            }
            else {
                Write-Host "No targets were returned."
            }
        }
        catch {
            Write-Warning (
                "Unable to retrieve target health: " +
                $_.Exception.Message
            )
        }
    }
}

Write-Step "Checking required command-line tools"

$awsVersion = Invoke-NativeCapture `
    -FilePath "aws" `
    -Arguments @("--version")

Write-Host $awsVersion.Text

if (-not $UseExistingImage) {
    $dockerVersion = Invoke-NativeCapture `
        -FilePath "docker" `
        -Arguments @("--version")

    Write-Host $dockerVersion.Text
}

Write-Step "Checking AWS credentials"

$identity = Invoke-AwsJson -Arguments @(
    "sts", "get-caller-identity"
)

Write-Host "AWS Account: $($identity.Account)"
Write-Host "AWS Principal: $($identity.Arn)"
Write-Host "AWS Region: $Region"

Write-Step "Reading the existing ECS service"

$serviceResponse = Invoke-AwsJson -Arguments @(
    "ecs", "describe-services",
    "--cluster", $ClusterName,
    "--services", $ServiceName
)

$serviceFailures = @($serviceResponse.failures)

if ($serviceFailures.Count -gt 0) {
    $failureText = (
        $serviceFailures |
        ForEach-Object {
            "$($_.arn): $($_.reason) $($_.detail)"
        }
    ) -join [Environment]::NewLine

    throw "Unable to read the ECS service:`n$failureText"
}

$service = @($serviceResponse.services)[0]

if ($null -eq $service -or $service.status -ne "ACTIVE") {
    throw (
        "ECS service '$ServiceName' was not found or is not ACTIVE " +
        "in cluster '$ClusterName'."
    )
}

if (
    $null -ne $service.deploymentController -and
    $service.deploymentController.type -ne "ECS"
) {
    throw @"
This script supports the standard ECS rolling deployment controller.
The service currently uses: $($service.deploymentController.type)
"@
}

$currentTaskDefinitionArn = $service.taskDefinition

Write-Host "Service ARN: $($service.serviceArn)"
Write-Host "Current task definition: $currentTaskDefinitionArn"
Write-Host (
    "Current tasks: desired={0}, running={1}, pending={2}" -f
    $service.desiredCount,
    $service.runningCount,
    $service.pendingCount
)

Write-Step "Reading the current task definition"

$taskResponse = Invoke-AwsJson -Arguments @(
    "ecs", "describe-task-definition",
    "--task-definition", $currentTaskDefinitionArn,
    "--include", "TAGS"
)

$taskDefinition = $taskResponse.taskDefinition

if ($null -eq $taskDefinition) {
    throw "The current ECS task definition could not be read."
}

if ([string]::IsNullOrWhiteSpace($ContainerName)) {
    $loadBalancerContainerNames = @(
        @($service.loadBalancers) |
        ForEach-Object { $_.containerName } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
    )

    if ($loadBalancerContainerNames.Count -eq 1) {
        $ContainerName = $loadBalancerContainerNames[0]
    }
    elseif (@($taskDefinition.containerDefinitions).Count -eq 1) {
        $ContainerName = $taskDefinition.containerDefinitions[0].name
    }
    else {
        $availableNames = (
            @($taskDefinition.containerDefinitions) |
            ForEach-Object { $_.name }
        ) -join ", "

        throw @"
ContainerName could not be inferred.
Specify -ContainerName explicitly.
Available containers: $availableNames
"@
    }
}

$containerDefinition = @($taskDefinition.containerDefinitions) |
    Where-Object { $_.name -eq $ContainerName } |
    Select-Object -First 1

if ($null -eq $containerDefinition) {
    $availableNames = (
        @($taskDefinition.containerDefinitions) |
        ForEach-Object { $_.name }
    ) -join ", "

    throw @"
Container '$ContainerName' was not found in the current task definition.
Available containers: $availableNames
"@
}

Write-Host "Task family: $($taskDefinition.family)"
Write-Host "Container: $ContainerName"
Write-Host "Current image: $($containerDefinition.image)"

if ([string]::IsNullOrWhiteSpace($BuildPlatform)) {
    $architecture = "X86_64"

    if (
        $null -ne $taskDefinition.runtimePlatform -and
        -not [string]::IsNullOrWhiteSpace(
            $taskDefinition.runtimePlatform.cpuArchitecture
        )
    ) {
        $architecture = $taskDefinition.runtimePlatform.cpuArchitecture
    }

    switch ($architecture.ToUpperInvariant()) {
        "ARM64" {
            $BuildPlatform = "linux/arm64"
        }
        default {
            $BuildPlatform = "linux/amd64"
        }
    }
}

Write-Host "Build platform: $BuildPlatform"

Write-Step "Reading the ECR repository"

$repositoryResponse = Invoke-AwsJson -Arguments @(
    "ecr", "describe-repositories",
    "--repository-names", $RepositoryName
)

$repository = @($repositoryResponse.repositories)[0]

if ($null -eq $repository) {
    throw "ECR repository '$RepositoryName' was not found."
}

$repositoryUri = $repository.repositoryUri
$registryUri = "$($identity.Account).dkr.ecr.$Region.amazonaws.com"

if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    if ($UseExistingImage) {
        throw (
            "-ImageTag is required when -UseExistingImage is specified."
        )
    }

    $ImageTag = Get-GeneratedImageTag
}

if ($ImageTag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
    throw "ImageTag is not a valid Docker/ECR tag: $ImageTag"
}

$imageUri = "$repositoryUri`:$ImageTag"

Write-Host "Repository: $repositoryUri"
Write-Host "Release image: $imageUri"

if ($UseExistingImage) {
    Write-Step "Verifying the existing ECR image"

    $imageResponse = Invoke-AwsJson -Arguments @(
        "ecr", "describe-images",
        "--repository-name", $RepositoryName,
        "--image-ids", "imageTag=$ImageTag"
    )

    $imageDetails = @($imageResponse.imageDetails)

    if ($imageDetails.Count -eq 0) {
        throw "The ECR image does not exist: $imageUri"
    }

    $selectedImage = $imageDetails[0]

    Write-Host "Image digest: $($selectedImage.imageDigest)"
    Write-Host "Image pushed: $($selectedImage.imagePushedAt)"
}
else {
    Write-Step "Logging Docker into Amazon ECR"

    $passwordResult = Invoke-AwsCapture -Arguments @(
        "ecr", "get-login-password"
    )

    if ([string]::IsNullOrWhiteSpace($passwordResult.Text)) {
        throw "AWS CLI did not return an ECR login password."
    }

    $previousErrorActionPreference = $ErrorActionPreference
    $nativePreferenceVariable = Get-Variable `
        -Name PSNativeCommandUseErrorActionPreference `
        -ErrorAction SilentlyContinue
    $previousNativePreference = $null

    try {
        $ErrorActionPreference = "Continue"

        if ($null -ne $nativePreferenceVariable) {
            $previousNativePreference = $PSNativeCommandUseErrorActionPreference
            $PSNativeCommandUseErrorActionPreference = $false
        }

        $passwordResult.Text |
            & docker login `
                --username AWS `
                --password-stdin `
                $registryUri

        $dockerLoginExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference

        if ($null -ne $nativePreferenceVariable) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }

    if ($dockerLoginExitCode -ne 0) {
        throw "Docker login to Amazon ECR failed."
    }

    Write-Step "Building the Docker image"

    $resolvedBuildContext = (
        Resolve-Path -LiteralPath $BuildContext
    ).Path

    $resolvedDockerfile = $Dockerfile

    if (-not [System.IO.Path]::IsPathRooted($resolvedDockerfile)) {
        $resolvedDockerfile = Join-Path `
            $resolvedBuildContext `
            $resolvedDockerfile
    }

    if (-not (Test-Path -LiteralPath $resolvedDockerfile -PathType Leaf)) {
        throw "Dockerfile was not found: $resolvedDockerfile"
    }

    $buildArguments = @(
        "build",
        "--platform", $BuildPlatform,
        "--file", $resolvedDockerfile,
        "--tag", $imageUri
    )

    if ($NoCache) {
        $buildArguments += "--no-cache"
    }

    $buildArguments += $resolvedBuildContext

    Invoke-NativeLive `
        -FilePath "docker" `
        -Arguments $buildArguments

    Write-Step "Pushing the Docker image to Amazon ECR"

    Invoke-NativeLive `
        -FilePath "docker" `
        -Arguments @("push", $imageUri)

    Write-Step "Verifying the pushed ECR image"

    $imageResponse = Invoke-AwsJson -Arguments @(
        "ecr", "describe-images",
        "--repository-name", $RepositoryName,
        "--image-ids", "imageTag=$ImageTag"
    )

    $selectedImage = @($imageResponse.imageDetails)[0]

    Write-Host "Image digest: $($selectedImage.imageDigest)"
    Write-Host "Image pushed: $($selectedImage.imagePushedAt)"
}

Write-Step "Creating a new task-definition revision"

# Keep every registerable property from the current task definition and remove
# the read-only properties returned by DescribeTaskDefinition.
foreach ($propertyName in @(
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
    "deregisteredAt"
)) {
    Remove-ObjectProperty `
        -InputObject $taskDefinition `
        -Name $propertyName
}

$targetContainer = @($taskDefinition.containerDefinitions) |
    Where-Object { $_.name -eq $ContainerName } |
    Select-Object -First 1

$targetContainer.image = $imageUri

# Preserve task-definition tags in the newly registered revision.
if (
    $null -ne $taskResponse.tags -and
    @($taskResponse.tags).Count -gt 0
) {
    $taskDefinition |
        Add-Member `
            -MemberType NoteProperty `
            -Name tags `
            -Value @($taskResponse.tags) `
            -Force
}

$tempDirectory = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("qon-ecs-deploy-" + [Guid]::NewGuid().ToString("N"))

[void](New-Item -ItemType Directory -Path $tempDirectory -Force)
$taskJsonPath = Join-Path $tempDirectory "task-definition.json"

try {
    $taskJson = $taskDefinition |
        ConvertTo-Json -Depth 100

    Write-Utf8NoBom `
        -Path $taskJsonPath `
        -Content $taskJson

    $taskJsonUri = ConvertTo-AwsFileUri -Path $taskJsonPath

    $registerResponse = Invoke-AwsJson -Arguments @(
        "ecs", "register-task-definition",
        "--cli-input-json", $taskJsonUri
    )

    $newTaskDefinitionArn = (
        $registerResponse.taskDefinition.taskDefinitionArn
    )

    if ([string]::IsNullOrWhiteSpace($newTaskDefinitionArn)) {
        throw "AWS did not return the new task-definition ARN."
    }

    Write-Host "New task definition: $newTaskDefinitionArn"

    Write-Step "Updating the existing ECS service"

    $updateResponse = Invoke-AwsJson -Arguments @(
        "ecs", "update-service",
        "--cluster", $ClusterName,
        "--service", $ServiceName,
        "--task-definition", $newTaskDefinitionArn,
        "--force-new-deployment"
    )

    $deploymentId = @(
        $updateResponse.service.deployments |
        Where-Object {
            $_.taskDefinition -eq $newTaskDefinitionArn
        }
    )[0].id

    Write-Host "Deployment ID: $deploymentId"

    if (-not $SkipWait) {
        Write-Step "Waiting for the ECS service to become stable"

        $waitResult = Invoke-AwsCapture `
            -Arguments @(
                "ecs", "wait", "services-stable",
                "--cluster", $ClusterName,
                "--services", $ServiceName
            ) `
            -AllowFailure

        if ($waitResult.ExitCode -ne 0) {
            Show-DeploymentDiagnostics

            $latestServiceResponse = Invoke-AwsJson -Arguments @(
                "ecs", "describe-services",
                "--cluster", $ClusterName,
                "--services", $ServiceName
            )

            Show-TargetHealth `
                -Service $latestServiceResponse.services[0]

            throw @"
The ECS service did not become stable.

AWS waiter output:
$($waitResult.Text)
"@
        }
    }

    Write-Step "Reading the completed deployment"

    $finalResponse = Invoke-AwsJson -Arguments @(
        "ecs", "describe-services",
        "--cluster", $ClusterName,
        "--services", $ServiceName
    )

    $finalService = @($finalResponse.services)[0]

    $finalDeployment = @($finalService.deployments) |
        Where-Object {
            $_.taskDefinition -eq $newTaskDefinitionArn
        } |
        Select-Object -First 1

    [pscustomobject]@{
        Cluster = $ClusterName
        Service = $ServiceName
        Status = $finalService.status
        DesiredCount = $finalService.desiredCount
        RunningCount = $finalService.runningCount
        PendingCount = $finalService.pendingCount
        DeploymentStatus = $finalDeployment.status
        RolloutState = $finalDeployment.rolloutState
        Image = $imageUri
        ImageTag = $ImageTag
        TaskDefinition = $newTaskDefinitionArn
        PreviousTaskDefinition = $currentTaskDefinitionArn
    } | Format-List

    Show-TargetHealth -Service $finalService

    if (-not [string]::IsNullOrWhiteSpace($HealthCheckUrl)) {
        Write-Step "Testing the deployed API health endpoint"

        try {
            $healthResponse = Invoke-RestMethod `
                -Method Get `
                -Uri $HealthCheckUrl `
                -TimeoutSec $HealthCheckTimeoutSeconds

            Write-Host (
                $healthResponse |
                ConvertTo-Json -Depth 20
            )
        }
        catch {
            Write-Warning @"
The ECS deployment is stable, but the external health check failed.

URL:
$HealthCheckUrl

Error:
$($_.Exception.Message)
"@
        }
    }

    Write-Host ""
    Write-Host "Deployment completed successfully." -ForegroundColor Green
    Write-Host ""
    Write-Host "Rollback command:" -ForegroundColor Yellow

    $profileText = ""

    if (-not [string]::IsNullOrWhiteSpace($Profile)) {
        $profileText = " --profile `"$Profile`""
    }

    Write-Host (
        "aws ecs update-service" +
        " --cluster `"$ClusterName`"" +
        " --service `"$ServiceName`"" +
        " --task-definition `"$currentTaskDefinitionArn`"" +
        " --force-new-deployment" +
        " --region `"$Region`"" +
        $profileText +
        " --no-cli-pager"
    )
}
catch {
    Write-Host ""
    Write-Host "Deployment failed." -ForegroundColor Red
    Write-Host "The previous task definition was:" -ForegroundColor Yellow
    Write-Host $currentTaskDefinitionArn
    throw
}
finally {
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item `
            -LiteralPath $tempDirectory `
            -Recurse `
            -Force
    }
}
