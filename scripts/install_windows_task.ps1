param(
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 60,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$taskName = "MementoMori APK Tracker"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed scheduled task: $taskName"
    exit 0
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scheduledScript = Join-Path $PSScriptRoot "run_scheduled.ps1"
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source

$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scheduledScript) `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Checks MementoMori updates, extracts resources, and publishes images to OSS." `
    -Force | Out-Null

Write-Output "Installed scheduled task: $taskName"
Write-Output "Project: $projectRoot"
Write-Output "Interval: $IntervalMinutes minutes"
Write-Output "Log: $(Join-Path $projectRoot 'reports\scheduled-task.log')"
