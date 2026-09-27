param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$BackupPath
)

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
    Write-Host 'Python 3 was not found.' -ForegroundColor Red
    return 9009
}

Write-Host 'Codex Local Project Recovery - RESTORE BACKUP'
Write-Host 'Fully exit Codex/ChatGPT Desktop first.' -ForegroundColor Yellow
Write-Host ''
$toolArgs = @('restore', $BackupPath) + $args
$code = Invoke-PythonTool -ToolArgs $toolArgs
exit $code
