$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m slice_helper
