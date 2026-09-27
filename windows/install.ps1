param(
    [string]$WorkingDir = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'
$repoDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$projectDir = (Resolve-Path $WorkingDir).Path
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    $python = & $launcher.Source -3 -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
} else {
    $launcher = Get-Command python -ErrorAction Stop
    $python = & $launcher.Source -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
}
if ($LASTEXITCODE -ne 0 -or -not $python) {
    throw 'Python 3.11 以上が必要です。'
}
$python = $python.Trim()
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw 'Codex CLI が見つかりません。'
}

$taskName = 'ClaudeBridge'
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
}
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    $listener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { break }
    Start-Sleep -Seconds 1
}
if ($listener) {
    throw 'ポート 8787 は別のプロセスが使用中です。停止せずに終了します。'
}

& codex app-server daemon start | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Codex app-server を起動できません。'
}

$arguments = '-m claude_bridge --working-dir "{0}" --upstream-base-url https://chatgpt.com/backend-api/codex --enable-codex-mcp --passthrough-tools "*,create_thread,list_projects" --tool-search-query "create_thread fork_thread list_threads"' -f $projectDir
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $repoDir
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

$routeTaskName = 'CodexMobileBrowserRoute'
$existingRouteTask = Get-ScheduledTask -TaskName $routeTaskName -ErrorAction SilentlyContinue
if ($existingRouteTask -and $existingRouteTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $routeTaskName
}
$routeArguments = '-m claude_bridge.mobile_browser_patch'
$routeAction = New-ScheduledTaskAction -Execute $python -Argument $routeArguments -WorkingDirectory $repoDir
Register-ScheduledTask -TaskName $routeTaskName -Action $routeAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $routeTaskName

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 2 | Out-Null
        Write-Host 'ready: http://127.0.0.1:8787'
        return
    } catch {
        Start-Sleep -Seconds 1
    }
}
throw '健康チェックに失敗しました。タスク スケジューラの ClaudeBridge を確認してください。'
