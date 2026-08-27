[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ClusterName,

    [string]$ServiceName = "qon-ioce-opps-api",

    # Task definition family, family:revision, or full task-definition ARN.
    [string]$TaskDefinition = "qon-ioce-opps-api",

    [Parameter(Mandatory = $true)]
    [string]$TargetGroupArn,

    [Parameter(Mandatory = $true)]
    [string[]]$SubnetIds,

    [Parameter(Mandatory = $true)]
    [string[]]$SecurityGroupIds,

    [string]$Region = "us-west-2",

    [string]$Profile = "",

    [int]$DesiredCount = 1,

    [string]$ContainerName = "qon-ioce-opps-api",

    [int]$ContainerPort = 8080,

    [ValidateSet("ENABLED", "DISABLED")]
    [string]$AssignPublicIp = "DISABLED",

    [int]$HealthCheckGracePeriodSeconds = 180,

    [ValidateRange(0, 100)]
    [int]$MinimumHealthyPercent = 100,

    [ValidateRange(100, 1000)]
    [int]$MaximumPercent = 200,

    [string]$PlatformVersion = "LATEST",

    [ValidateSet("Preserve", "Enable", "Disable")]
    [string]$ExecuteCommand = "Preserve",

    [string]$Environment = "Production",

    [switch]$CreateClusterIfMissing,

    [switch]$ConfigureTargetGroupHealthCheck,

    [string]$HealthCheckPath = "/health/ready",

    [bool]$WaitForStable = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-AwsCli {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$AllowFailure
    )

    $commonArguments = @(
        "--region", $Region,
        "--no-cli-pager"
    )

    if (-not [string]::IsNullOrWhiteSpace($Profile)) {
        $commonArguments += @("--profile", $Profile)
    }

    $allArguments = @($Arguments) + $commonArguments

    # Windows PowerShell 5.1 turns native stderr output into PowerShell error
    # records. Because this script uses ErrorActionPreference=Stop, an AWS CLI
    # error could terminate here before we capture $LASTEXITCODE and the actual
    # AWS error message. Temporarily use Continue for the native invocation.
    $previousErrorActionPreference = $ErrorActionPreference
    $nativePreferenceVariable = Get-Variable `
        -Name PSNativeCommandUseErrorActionPreference `
        -ErrorAction SilentlyContinue

    $previousNativePreference = $null
    $output = @()
    $exitCode = -1

    try {
        $ErrorActionPreference = "Continue"

        if ($null -ne $nativePreferenceVariable) {
            $previousNativePreference = $PSNativeCommandUseErrorActionPreference
            $PSNativeCommandUseErrorActionPreference = $false
        }

        $output = & aws @allArguments 2>&1
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
        throw @"
AWS CLI command failed with exit code $exitCode.

Command:
aws $($allArguments -join " ")

Output:
$text
"@
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Text = $text
    }
}

function Invoke-AwsJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $result = Invoke-AwsCli -Arguments (@($Arguments) + @("--output", "json"))

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
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).Replace("\", "/")

    if ($fullPath -match "^[A-Za-z]:/") {
        return "file:///$fullPath"
    }

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

function Get-ServiceDescription {
    $response = Invoke-AwsJson -Arguments @(
        "ecs", "describe-services",
        "--cluster", $ClusterName,
        "--services", $ServiceName
    )

    if ($null -eq $response -or @($response.services).Count -eq 0) {
        return $null
    }

    $service = @($response.services)[0]

    if ($service.status -eq "INACTIVE") {
        return $null
    }

    return $service
}

function Show-FailureDiagnostics {
    Write-Host ""
    Write-Host "Recent ECS service events:" -ForegroundColor Yellow

    try {
        $description = Invoke-AwsJson -Arguments @(
            "ecs", "describe-services",
            "--cluster", $ClusterName,
            "--services", $ServiceName
        )

        $events = @($description.services[0].events) |
            Select-Object -First 10 |
            Select-Object createdAt, message

        if ($events.Count -gt 0) {
            $events | Format-Table -Wrap -AutoSize
        }
    }
    catch {
        Write-Warning "Unable to retrieve ECS service events: $($_.Exception.Message)"
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
        Write-Warning "Unable to retrieve stopped-task diagnostics: $($_.Exception.Message)"
    }
}

if ($DesiredCount -lt 0) {
    throw "DesiredCount cannot be negative."
}

if ($HealthCheckGracePeriodSeconds -lt 0) {
    throw "HealthCheckGracePeriodSeconds cannot be negative."
}

if ($SubnetIds.Count -eq 0) {
    throw "At least one subnet ID is required."
}

if ($SecurityGroupIds.Count -eq 0) {
    throw "At least one security group ID is required."
}

Write-Step "Checking AWS CLI and credentials"

$identity = Invoke-AwsJson -Arguments @(
    "sts", "get-caller-identity"
)

Write-Host "AWS Account: $($identity.Account)"
Write-Host "AWS Principal: $($identity.Arn)"
Write-Host "AWS Region: $Region"

Write-Step "Validating or creating the ECS cluster"

$clusterResponse = Invoke-AwsJson -Arguments @(
    "ecs", "describe-clusters",
    "--clusters", $ClusterName
)

$cluster = @($clusterResponse.clusters) |
    Where-Object { $_.status -eq "ACTIVE" } |
    Select-Object -First 1

if ($null -eq $cluster) {
    if (-not $CreateClusterIfMissing) {
        throw @"
The ECS cluster '$ClusterName' was not found or is not ACTIVE.
Use -CreateClusterIfMissing to create it.
"@
    }

    Write-Host "Creating ECS cluster '$ClusterName'..."

    $clusterCreateResponse = Invoke-AwsJson -Arguments @(
        "ecs", "create-cluster",
        "--cluster-name", $ClusterName,
        "--settings", "name=containerInsights,value=enabled"
    )

    $cluster = $clusterCreateResponse.cluster
}

Write-Host "Cluster ARN: $($cluster.clusterArn)"

Write-Step "Validating the task definition"

$taskDefinitionResponse = Invoke-AwsJson -Arguments @(
    "ecs", "describe-task-definition",
    "--task-definition", $TaskDefinition
)

$taskDefinitionObject = $taskDefinitionResponse.taskDefinition
$resolvedTaskDefinitionArn = $taskDefinitionObject.taskDefinitionArn

if ($taskDefinitionObject.status -ne "ACTIVE") {
    throw "Task definition is not ACTIVE: $resolvedTaskDefinitionArn"
}

if ($taskDefinitionObject.networkMode -ne "awsvpc") {
    throw @"
The task definition must use networkMode 'awsvpc' for Fargate.
Current value: $($taskDefinitionObject.networkMode)
"@
}

if (@($taskDefinitionObject.requiresCompatibilities) -notcontains "FARGATE") {
    throw "The task definition is not registered as FARGATE compatible."
}

$containerDefinition = @($taskDefinitionObject.containerDefinitions) |
    Where-Object { $_.name -eq $ContainerName } |
    Select-Object -First 1

if ($null -eq $containerDefinition) {
    $availableContainers = (
        @($taskDefinitionObject.containerDefinitions) |
        ForEach-Object { $_.name }
    ) -join ", "

    throw @"
Container '$ContainerName' was not found in the task definition.
Available containers: $availableContainers
"@
}

$matchingPort = @($containerDefinition.portMappings) |
    Where-Object { [int]$_.containerPort -eq $ContainerPort } |
    Select-Object -First 1

if ($null -eq $matchingPort) {
    throw @"
Container '$ContainerName' does not expose container port $ContainerPort
in the task definition.
"@
}

Write-Host "Task definition: $resolvedTaskDefinitionArn"
Write-Host "Container mapping: $ContainerName`:$ContainerPort"

Write-Step "Validating subnets and security groups"

$subnetResponse = Invoke-AwsJson -Arguments (
    @("ec2", "describe-subnets", "--subnet-ids") + $SubnetIds
)

$returnedSubnetIds = @($subnetResponse.Subnets) |
    ForEach-Object { $_.SubnetId }

$missingSubnets = @($SubnetIds) |
    Where-Object { $returnedSubnetIds -notcontains $_ }

if ($missingSubnets.Count -gt 0) {
    throw "Subnets not found: $($missingSubnets -join ', ')"
}

$subnetVpcIds = @(
    @($subnetResponse.Subnets) |
    ForEach-Object { $_.VpcId } |
    Select-Object -Unique
)

if ($subnetVpcIds.Count -ne 1) {
    throw "All selected subnets must be in the same VPC."
}

$vpcId = $subnetVpcIds[0]

$unavailableSubnets = @($subnetResponse.Subnets) |
    Where-Object { $_.State -ne "available" }

if ($unavailableSubnets.Count -gt 0) {
    throw (
        "These subnets are not available: " +
        (($unavailableSubnets | ForEach-Object { $_.SubnetId }) -join ", ")
    )
}

if ($SubnetIds.Count -lt 2) {
    Write-Warning (
        "Only one subnet was supplied. Two or more subnets in different " +
        "Availability Zones are recommended for an ECS service."
    )
}

$securityGroupResponse = Invoke-AwsJson -Arguments (
    @("ec2", "describe-security-groups", "--group-ids") + $SecurityGroupIds
)

$returnedSecurityGroupIds = @($securityGroupResponse.SecurityGroups) |
    ForEach-Object { $_.GroupId }

$missingSecurityGroups = @($SecurityGroupIds) |
    Where-Object { $returnedSecurityGroupIds -notcontains $_ }

if ($missingSecurityGroups.Count -gt 0) {
    throw "Security groups not found: $($missingSecurityGroups -join ', ')"
}

$wrongVpcSecurityGroups = @($securityGroupResponse.SecurityGroups) |
    Where-Object { $_.VpcId -ne $vpcId }

if ($wrongVpcSecurityGroups.Count -gt 0) {
    throw (
        "These security groups are not in VPC $vpcId`: " +
        (($wrongVpcSecurityGroups | ForEach-Object { $_.GroupId }) -join ", ")
    )
}

Write-Host "VPC: $vpcId"
Write-Host "Subnets: $($SubnetIds -join ', ')"
Write-Host "Security groups: $($SecurityGroupIds -join ', ')"

Write-Step "Validating the load-balancer target group"

$targetGroupResponse = Invoke-AwsJson -Arguments @(
    "elbv2", "describe-target-groups",
    "--target-group-arns", $TargetGroupArn
)

$targetGroup = @($targetGroupResponse.TargetGroups)[0]

if ($null -eq $targetGroup) {
    throw "Target group was not found: $TargetGroupArn"
}

if ($targetGroup.TargetType -ne "ip") {
    throw @"
The target group must use target type 'ip' for an awsvpc/Fargate service.
Current target type: $($targetGroup.TargetType)
"@
}

if ($targetGroup.VpcId -ne $vpcId) {
    throw @"
The target group and ECS task subnets are in different VPCs.
Target group VPC: $($targetGroup.VpcId)
Subnet VPC: $vpcId
"@
}

if ($ConfigureTargetGroupHealthCheck) {
    Write-Host "Configuring target-group health check: $HealthCheckPath"

    [void](Invoke-AwsCli -Arguments @(
        "elbv2", "modify-target-group",
        "--target-group-arn", $TargetGroupArn,
        "--health-check-enabled",
        "--health-check-protocol", "HTTP",
        "--health-check-path", $HealthCheckPath,
        "--health-check-port", "traffic-port",
        "--health-check-interval-seconds", "30",
        "--health-check-timeout-seconds", "10",
        "--healthy-threshold-count", "2",
        "--unhealthy-threshold-count", "3",
        "--matcher", "HttpCode=200-399"
    ))
}

Write-Host "Target group: $($targetGroup.TargetGroupName)"
Write-Host "Target type: $($targetGroup.TargetType)"
Write-Host "Health path: $($targetGroup.HealthCheckPath)"

Write-Step "Preparing ECS service configuration"

$tempDirectory = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("qon-ecs-service-" + [Guid]::NewGuid().ToString("N"))

[void](New-Item -ItemType Directory -Path $tempDirectory -Force)

$networkPath = Join-Path $tempDirectory "network-configuration.json"
$loadBalancersPath = Join-Path $tempDirectory "load-balancers.json"
$deploymentPath = Join-Path $tempDirectory "deployment-configuration.json"

try {
    $networkConfiguration = @{
        awsvpcConfiguration = @{
            subnets = @($SubnetIds)
            securityGroups = @($SecurityGroupIds)
            assignPublicIp = $AssignPublicIp
        }
    }

    $loadBalancers = @(
        @{
            targetGroupArn = $TargetGroupArn
            containerName = $ContainerName
            containerPort = $ContainerPort
        }
    )

    $deploymentConfiguration = @{
        maximumPercent = $MaximumPercent
        minimumHealthyPercent = $MinimumHealthyPercent
        deploymentCircuitBreaker = @{
            enable = $true
            rollback = $true
        }
    }

    Write-Utf8NoBom `
        -Path $networkPath `
        -Content (
            $networkConfiguration |
            ConvertTo-Json -Depth 10 -Compress
        )

    Write-Utf8NoBom `
        -Path $loadBalancersPath `
        -Content (
            $loadBalancers |
            ConvertTo-Json -Depth 10 -Compress
        )

    Write-Utf8NoBom `
        -Path $deploymentPath `
        -Content (
            $deploymentConfiguration |
            ConvertTo-Json -Depth 10 -Compress
        )

    $networkUri = ConvertTo-AwsFileUri -Path $networkPath
    $loadBalancersUri = ConvertTo-AwsFileUri -Path $loadBalancersPath
    $deploymentUri = ConvertTo-AwsFileUri -Path $deploymentPath

    $existingService = Get-ServiceDescription

    if ($null -eq $existingService) {
        Write-Step "Creating ECS service '$ServiceName'"

        $createArguments = @(
            "ecs", "create-service",
            "--cluster", $ClusterName,
            "--service-name", $ServiceName,
            "--task-definition", $resolvedTaskDefinitionArn,
            "--desired-count", $DesiredCount.ToString(),
            "--launch-type", "FARGATE",
            "--platform-version", $PlatformVersion,
            "--scheduling-strategy", "REPLICA",
            "--network-configuration", $networkUri,
            "--load-balancers", $loadBalancersUri,
            "--deployment-configuration", $deploymentUri,
            "--deployment-controller", "type=ECS",
            "--health-check-grace-period-seconds",
                $HealthCheckGracePeriodSeconds.ToString(),
            "--enable-ecs-managed-tags",
            "--propagate-tags", "TASK_DEFINITION"
        )

        if ($ExecuteCommand -eq "Enable") {
            $createArguments += "--enable-execute-command"
        }
        elseif ($ExecuteCommand -eq "Disable") {
            $createArguments += "--disable-execute-command"
        }

        $serviceResponse = Invoke-AwsJson -Arguments $createArguments
        $service = $serviceResponse.service
    }
    else {
        Write-Step "Updating existing ECS service '$ServiceName'"

        $updateArguments = @(
            "ecs", "update-service",
            "--cluster", $ClusterName,
            "--service", $ServiceName,
            "--task-definition", $resolvedTaskDefinitionArn,
            "--desired-count", $DesiredCount.ToString(),
            "--platform-version", $PlatformVersion,
            "--network-configuration", $networkUri,
            "--load-balancers", $loadBalancersUri,
            "--deployment-configuration", $deploymentUri,
            "--health-check-grace-period-seconds",
                $HealthCheckGracePeriodSeconds.ToString(),
            "--enable-ecs-managed-tags",
            "--propagate-tags", "TASK_DEFINITION",
            "--force-new-deployment"
        )

        if ($ExecuteCommand -eq "Enable") {
            $updateArguments += "--enable-execute-command"
        }
        elseif ($ExecuteCommand -eq "Disable") {
            $updateArguments += "--disable-execute-command"
        }

        $serviceResponse = Invoke-AwsJson -Arguments $updateArguments
        $service = $serviceResponse.service
    }

    $serviceArn = $service.serviceArn

    Write-Host "Service ARN: $serviceArn"

    Write-Step "Applying ECS service tags"

    [void](Invoke-AwsCli -Arguments @(
        "ecs", "tag-resource",
        "--resource-arn", $serviceArn,
        "--tags",
            "key=Application,value=QON",
            "key=Component,value=CMS-IOCE-OPPS",
            "key=Environment,value=$Environment"
    ))

    if ($WaitForStable) {
        Write-Step "Waiting for the ECS service to become stable"

        $waitResult = Invoke-AwsCli `
            -Arguments @(
                "ecs", "wait", "services-stable",
                "--cluster", $ClusterName,
                "--services", $ServiceName
            ) `
            -AllowFailure

        if ($waitResult.ExitCode -ne 0) {
            Show-FailureDiagnostics

            throw @"
The ECS service did not become stable.

AWS waiter output:
$($waitResult.Text)
"@
        }
    }

    Write-Step "Retrieving final service status"

    $finalResponse = Invoke-AwsJson -Arguments @(
        "ecs", "describe-services",
        "--cluster", $ClusterName,
        "--services", $ServiceName
    )

    $finalService = @($finalResponse.services)[0]

    [pscustomobject]@{
        ServiceName = $finalService.serviceName
        Status = $finalService.status
        DesiredCount = $finalService.desiredCount
        RunningCount = $finalService.runningCount
        PendingCount = $finalService.pendingCount
        TaskDefinition = $finalService.taskDefinition
        LaunchType = $finalService.launchType
        PlatformVersion = $finalService.platformVersion
        HealthGraceSeconds = $finalService.healthCheckGracePeriodSeconds
        ServiceArn = $finalService.serviceArn
    } | Format-List

    Write-Step "Retrieving target health"

    $targetHealthResponse = Invoke-AwsJson -Arguments @(
        "elbv2", "describe-target-health",
        "--target-group-arn", $TargetGroupArn
    )

    $targetHealthRows = foreach (
        $targetDescription in @(
            $targetHealthResponse.TargetHealthDescriptions
        )
    ) {
        [pscustomobject]@{
            Target = $targetDescription.Target.Id
            Port = $targetDescription.Target.Port
            AvailabilityZone = $targetDescription.Target.AvailabilityZone
            State = $targetDescription.TargetHealth.State
            Reason = $targetDescription.TargetHealth.Reason
            Description = $targetDescription.TargetHealth.Description
        }
    }

    if (@($targetHealthRows).Count -gt 0) {
        $targetHealthRows | Format-Table -AutoSize
    }
    else {
        Write-Host "No registered targets were returned."
    }

    Write-Host ""
    Write-Host "ECS service deployment completed." -ForegroundColor Green
}
finally {
    if (Test-Path $tempDirectory) {
        Remove-Item $tempDirectory -Recurse -Force
    }
}
