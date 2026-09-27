$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tool = Join-Path $Root 'codex_project_recovery.py'

function Invoke-PythonTool {
    param([string[]]$ToolArgs)
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & py -3 $Tool @ToolArgs
        return $LASTEXITCODE
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & python $Tool @ToolArgs
        return $LASTEXITCODE
    }
    Write-Host ''
    Write-Host 'Python 3 was not found.' -ForegroundColor Red
    Write-Host 'Install Python 3, then rerun this script. No third-party Python packages are required.'
    return 9009
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$report = Join-Path ([Environment]::GetFolderPath('Desktop')) "codex-project-recovery-report-$stamp.txt"

Write-Host 'Codex Local Project Recovery - READ-ONLY DIAGNOSIS'
Write-Host 'This does not modify Codex data.'
Write-Host ''

$toolArgs = @('diagnose', '--redact', '--report', $report) + $args
$code = Invoke-PythonTool -ToolArgs $toolArgs
Write-Host ''
if ($code -eq 0) {
    Write-Host "Redacted report saved to: $report" -ForegroundColor Green
}
exit $code
