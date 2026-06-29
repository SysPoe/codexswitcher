[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "codexswitcher.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "codexswitcher.py was not found beside this launcher."
}

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source -3 $scriptPath @Arguments
    exit $LASTEXITCODE
}

$python = Get-Command python3 -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $scriptPath @Arguments
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $scriptPath @Arguments
    exit $LASTEXITCODE
}

throw "Python 3 was not found on PATH. Install Python or run codexswitcher.py with a Python 3 interpreter."
