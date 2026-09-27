$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & py -3 -m unittest discover -s (Join-Path $Root 'tests') -v
    exit $LASTEXITCODE
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & python -m unittest discover -s (Join-Path $Root 'tests') -v
    exit $LASTEXITCODE
}
Write-Host 'Python 3 was not found.' -ForegroundColor Red
exit 9009
