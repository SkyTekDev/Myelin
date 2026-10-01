$ErrorActionPreference = 'Stop'
$scriptUnderTest = Join-Path $PSScriptRoot '../aws/build-push-ecr.ps1'
$originalContext = $env:DOCKER_CONTEXT
function global:aws {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'sts') { '123456789012' }
    elseif ($args[1] -eq 'get-login-password') { 'fake-test-token' }
}
function global:docker {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'context') { 'npipe:////./pipe/dockerDesktopLinuxEngine'; return }
    if ($args[0] -eq 'login') { $global:loginCalled = $true; return }
    if ($args[0] -eq '--config') {
        $global:seenConfig = [string]$args[1]
        $config = Get-Content (Join-Path $global:seenConfig 'config.json') -Raw | ConvertFrom-Json
        $encoded = $config.auths.'123456789012.dkr.ecr.us-west-2.amazonaws.com'.auth
        if ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) -ne 'AWS:fake-test-token') { throw 'Wrong auth' }
        if ($config.credsStore -or $config.credHelpers) { throw 'Unexpected helper' }
        if ($args[3] -ne 'npipe:////./pipe/dockerDesktopLinuxEngine') { throw 'Engine changed' }
    }
    if ($args -contains 'build' -and $global:failBuild) { $global:LASTEXITCODE = 1 }
    if ($args -contains 'push') { $global:pushCalled = $true }
}
try {
    foreach ($failure in @($false, $true)) {
        $global:seenConfig = $null
        $global:loginCalled = $false
        $global:pushCalled = $false
        $global:failBuild = $failure
        $env:DOCKER_CONTEXT = 'desktop-linux'
        $caught = $false
        try { & $scriptUnderTest -Tag v2 -UseTemporaryDockerConfig }
        catch { if (!$failure -or $_.Exception.Message -ne 'Docker build failed.') { throw }; $caught = $true }
        if ($caught -ne $failure -or $global:loginCalled -or $global:pushCalled -eq $failure) { throw 'Incorrect control flow' }
        if (!$global:seenConfig -or (Test-Path -LiteralPath $global:seenConfig)) { throw 'Temporary token not cleaned up' }
        if ($env:DOCKER_CONTEXT -ne 'desktop-linux') { throw 'Context not restored' }
    }
    $global:failBuild = $false
    $global:loginCalled = $false
    & $scriptUnderTest -Tag v2
    if (!$global:loginCalled) { throw 'Default login path changed' }
    Write-Host 'PASS: isolated auth, engine selection, success/failure cleanup, context restoration and default login.'
}
finally {
    $env:DOCKER_CONTEXT = $originalContext
    Remove-Item Function:\aws, Function:\docker
}

