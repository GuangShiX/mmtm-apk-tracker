$ErrorActionPreference = "Continue"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$reportsDir = Join-Path $projectRoot "reports"
$logPath = Join-Path $reportsDir "scheduled-task.log"
$python = (Get-Command python -ErrorAction Stop).Source
$runScript = Join-Path $projectRoot "run.py"

New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null
Add-Content -LiteralPath $logPath -Value ("`n[{0:yyyy-MM-dd HH:mm:ss}] scheduled run" -f (Get-Date))

$exitCode = 1
Push-Location $projectRoot
try {
    & $python $runScript *>> $logPath
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

Add-Content -LiteralPath $logPath -Value ("[{0:yyyy-MM-dd HH:mm:ss}] exit code {1}" -f (Get-Date), $exitCode)
exit $exitCode
