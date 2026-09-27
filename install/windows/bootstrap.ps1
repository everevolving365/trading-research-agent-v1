# EverEvolving Trading Agent -- install on Windows without git.
#
# Paste this into PowerShell:
#   irm https://raw.githubusercontent.com/everevolving365/trading-research-agent-v1/main/install/windows/bootstrap.ps1 | iex
#
# It downloads the latest version from GitHub into %USERPROFILE%\EverEvolving\app
# and runs the normal installer from there. Run it again at any time to update.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Data = Join-Path $env:USERPROFILE "EverEvolving"
$App = Join-Path $Data "app"
$Zip = Join-Path $env:TEMP "everevolving-main.zip"
$Unpack = Join-Path $env:TEMP "everevolving-unpack"

Write-Host "  Downloading the EverEvolving Trading Agent..."
New-Item -ItemType Directory -Force -Path $Data | Out-Null
Invoke-WebRequest "https://github.com/everevolving365/trading-research-agent-v1/archive/refs/heads/main.zip" -OutFile $Zip
if (Test-Path $Unpack) { Remove-Item $Unpack -Recurse -Force }
Expand-Archive $Zip -DestinationPath $Unpack -Force
$source = Get-ChildItem $Unpack -Directory | Select-Object -First 1
if (-not (Test-Path (Join-Path $source.FullName "install\windows\install.ps1"))) {
    throw "the download did not contain the installer"
}
# $App holds program files only; your data lives beside it in $Data and is kept.
if (Test-Path $App) { Remove-Item $App -Recurse -Force }
Move-Item $source.FullName $App
Remove-Item $Zip -Force
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $App "install\windows\install.ps1")
