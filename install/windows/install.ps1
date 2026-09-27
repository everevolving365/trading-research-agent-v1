# EverEvolving Trading Agent -- Windows installer.
#
# Double-click Install.bat in the folder you downloaded, or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install\windows\install.ps1
#
# What it does, in order, and nothing else:
#   1. finds Python 3.11 or newer (installs Python 3.12 for this user only if there is none)
#   2. makes a private Python environment in %USERPROFILE%\EverEvolving
#   3. installs the agent into it from this folder
#   4. puts "EverEvolving Trading Agent" on the Desktop and in the Start menu
#   5. opens the app
#
# It never asks for a password or an API key. The app asks for your own keys
# later, on its Keys page, and stores them in Windows Credential Manager.

param(
    [switch]$NoLaunch,
    [switch]$NoShortcuts
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Data = Join-Path $env:USERPROFILE "EverEvolving"
$Venv = Join-Path $Data "venv"
$AppName = "EverEvolving Trading Agent"

function Say([string]$Message) { Write-Host "  $Message" }

# Windows PowerShell 5.1 turns anything a program prints to stderr into an error
# record, and with "Stop" that aborts the script -- pip prints harmless notices
# there. Native programs are judged by their exit code instead.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
}
function Step([string]$Message) { Write-Host ""; Write-Host "  > $Message" -ForegroundColor Green }

function Test-Python([string]$Exe, [string[]]$Prefix) {
    try {
        $out = & $Exe @Prefix -c "import sys; print('ok' if sys.version_info >= (3, 11) else 'old')" 2>$null
        return ($out -eq "ok")
    } catch { return $false }
}

function Find-Python {
    # The py launcher is the reliable way on Windows; "python" may be the Store stub.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.13", "-3.12", "-3.11", "-3")) {
            if (Test-Python "py" @($v)) {
                $exe = & py $v -c "import sys; print(sys.executable)"
                return $exe.Trim()
            }
        }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch "WindowsApps" -and (Test-Python $cmd.Source @())) {
            return $cmd.Source
        }
    }
    foreach ($dir in Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python") -Directory -ErrorAction SilentlyContinue) {
        $exe = Join-Path $dir.FullName "python.exe"
        if ((Test-Path $exe) -and (Test-Python $exe @())) { return $exe }
    }
    return $null
}

function Install-Python {
    Say "Python was not found. Installing Python 3.12 for your user account (no admin needed)..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Invoke-Native "winget" @("install", "-e", "--id", "Python.Python.3.12", "--scope", "user", "--silent",
            "--accept-package-agreements", "--accept-source-agreements") | Out-Null
    } else {
        $installer = Join-Path $env:TEMP "python-3.12.7-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $installer
        Start-Process $installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1" -Wait
    }
}

Write-Host ""
Write-Host "  $AppName -- installing" -ForegroundColor Cyan
Write-Host "  from   $Repo"
Write-Host "  into   $Data"

Step "1 of 5  Python"
$Python = Find-Python
if (-not $Python) {
    Install-Python
    $Python = Find-Python
}
if (-not $Python) {
    throw "Python 3.11 or newer could not be found or installed. Install it from https://www.python.org/downloads/ and run this again."
}
Say "using $Python"

Step "2 of 5  A private environment for the agent"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    if ((Invoke-Native $Python @("-m", "venv", $Venv)) -ne 0) { throw "could not create the environment in $Venv" }
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
Say $Venv

Step "3 of 5  Installing the agent (a minute or two the first time)"
Invoke-Native $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "--quiet", "--disable-pip-version-check") | Out-Null
# [all] adds the OS keychain (so your keys never sit in a file), web requests and charts.
$code = Invoke-Native $VenvPython @("-m", "pip", "install", "-e", ($Repo + "[all]"), "--quiet", "--disable-pip-version-check")
if ($code -ne 0) { throw "pip could not install the agent (exit code $code)." }
if ((Invoke-Native $VenvPython @("-c", "import ee_agent.desktop")) -ne 0) { throw "the agent installed but does not import." }
Say "installed"

Step "4 of 5  The desktop icon"
$Icon = (& $VenvPython -m ee_agent.ui.icon $Data).Trim()
$Launcher = Join-Path $Venv "Scripts\ee-agent-desktop.exe"
if (-not (Test-Path $Launcher)) { $Launcher = Join-Path $Venv "Scripts\pythonw.exe"; $LaunchArgs = "-m ee_agent.desktop" } else { $LaunchArgs = "" }
if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $places = @(
        [Environment]::GetFolderPath("Desktop"),
        [Environment]::GetFolderPath("Programs")
    )
    foreach ($place in $places) {
        $link = $shell.CreateShortcut((Join-Path $place "$AppName.lnk"))
        $link.TargetPath = $Launcher
        $link.Arguments = $LaunchArgs
        $link.WorkingDirectory = $Data
        $link.IconLocation = "$Icon,0"
        $link.Description = "Describe your strategy, backtest it, and prove it is the same everywhere."
        $link.Save()
        Say (Join-Path $place "$AppName.lnk")
    }
    # Run from inside a packaged app (the Claude desktop app is one), Windows
    # quietly keeps anything written under AppData -- the Start menu included --
    # inside that app's private sandbox. The Desktop is never redirected.
    $startLink = Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"
    $check = "import os,sys; p=os.path.abspath(sys.argv[1]); print('redirected' if os.path.normcase(os.path.realpath(p)) != os.path.normcase(p) else 'ok')"
    $where = (& $VenvPython -c $check $startLink 2>$null)
    if ($where -eq "redirected") {
        Say "Note: this installer ran inside another app's sandbox, so Windows keeps the"
        Say "Start menu entry there. The Desktop icon works normally -- use that one."
    }
}
@{ repo = $Repo; venv = $Venv; launcher = $Launcher; installed = (Get-Date).ToString("s") } |
    ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Data "install.json")

Step "5 of 5  Opening the app"
if (-not $NoLaunch) {
    if ($LaunchArgs) { Start-Process $Launcher -ArgumentList $LaunchArgs -WorkingDirectory $Data }
    else { Start-Process $Launcher -WorkingDirectory $Data }
    Say "The app window is opening. Next time, double-click '$AppName' on your Desktop."
} else {
    Say "Skipped (-NoLaunch). Double-click '$AppName' on your Desktop to open it."
}
Write-Host ""
Write-Host "  Done." -ForegroundColor Cyan
Write-Host ""
