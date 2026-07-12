param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AuralisArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Виртуальное окружение не найдено. Сначала выполните .\install.ps1"
}
Set-Location $ProjectRoot
& $VenvPython -m browser.main @AuralisArgs

