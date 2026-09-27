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

Write-Host 'Codex Local Project Recovery - REPAIR'
Write-Host ''
Write-Host 'IMPORTANT: Fully exit Codex/ChatGPT Desktop before continuing.' -ForegroundColor Yellow
Write-Host 'The utility will refuse to write if ChatGPT.exe or Codex.exe still appears to be running.'
Write-Host 'It creates a timestamped backup before replacing the global-state JSON.'
Write-Host 'SQLite and your source repositories are never modified.'
Write-Host ''

$toolArgs = @('repair') + $args
$code = Invoke-PythonTool -ToolArgs $toolArgs
exit $code
