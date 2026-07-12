param(
    [string]$Python = "python",
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

& $Python -c "import sys; assert sys.version_info >= (3, 12), 'Auralis requires Python 3.12+'"
if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip setuptools wheel
$Requirements = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
& $VenvPython -m pip install -r $Requirements
& $VenvPython -m pip install -e .

Write-Host "Auralis установлен. Запуск: .\run.ps1" -ForegroundColor Green

