# EverEvolving Trading Agent -- Windows uninstaller.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install\windows\uninstall.ps1
#
# Removes the shortcuts and the private Python environment. Your data -- the
# research index, the signal ledger, your settings -- stays in
# %USERPROFILE%\EverEvolving unless you add -RemoveData. Keys you stored live in
# Windows Credential Manager; remove them first with `ee-agent secrets delete NAME`
# if you want them gone.

param([switch]$RemoveData)

$ErrorActionPreference = "Stop"
$AppName = "EverEvolving Trading Agent"
$Data = Join-Path $env:USERPROFILE "EverEvolving"

foreach ($place in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $link = Join-Path $place "$AppName.lnk"
    if (Test-Path $link) { Remove-Item $link -Force; Write-Host "  removed $link" }
}
$venv = Join-Path $Data "venv"
if (Test-Path $venv) { Remove-Item $venv -Recurse -Force; Write-Host "  removed $venv" }
if ($RemoveData -and (Test-Path $Data)) {
    Remove-Item $Data -Recurse -Force
    Write-Host "  removed $Data"
} else {
    Write-Host "  kept your data in $Data"
}
Write-Host "  Uninstalled."
