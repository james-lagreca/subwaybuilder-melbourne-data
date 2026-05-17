<#
    Subway Builder Melbourne data-pipeline prerequisites — Windows side.

    Installs Docker Desktop and the Ubuntu WSL distro. The heavy Linux-side
    tooling (depot conda env, tippecanoe, osmium, planetiler, mapshaper,
    pmtiles, ...) is installed by the companion bash script
    `setup-prereqs.sh`, which is staged into the new Ubuntu home directory
    so you can run it the first time you launch Ubuntu.

    Run from an elevated PowerShell:
        Set-ExecutionPolicy -Scope Process Bypass
        .\setup-prereqs.ps1

    Idempotent — re-runs only install what's missing.
#>

[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Skip($msg)  { Write-Host "    --  $msg" -ForegroundColor DarkGray }
function Write-Warn2($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------
Write-Step "Checking host"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin) {
    Write-Warn2 "Not running as administrator. WSL + Docker Desktop installs may fail."
    Write-Warn2 "Right-click PowerShell -> Run as administrator, then re-run."
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is required but not found. Install 'App Installer' from the Microsoft Store."
}
Write-Ok "winget present"

# ---------------------------------------------------------------------------
# 1. Docker Desktop
# ---------------------------------------------------------------------------
Write-Step "Docker Desktop"

$dockerInstalled = (winget list --exact --id Docker.DockerDesktop 2>$null | `
    Select-String -SimpleMatch "Docker.DockerDesktop")
if ($dockerInstalled) {
    Write-Skip "Docker Desktop already installed"
} else {
    winget install --exact --id Docker.DockerDesktop `
        --accept-package-agreements --accept-source-agreements `
        --silent
    Write-Ok "Docker Desktop installed"
    Write-Warn2 "After this script finishes: launch Docker Desktop once, accept the EULA, and enable the WSL2 backend (Settings -> General)."
}

# ---------------------------------------------------------------------------
# 2. WSL2 + Ubuntu distro
# ---------------------------------------------------------------------------
Write-Step "WSL2 + $Distro distro"

$distros = (wsl -l -q 2>$null) -replace "`0", "" |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }

if ($distros -contains $Distro) {
    Write-Skip "$Distro already installed"
} else {
    Write-Ok "Installing $Distro (no launch). This downloads ~500 MB."
    # --no-launch leaves first-time user setup until the user runs `wsl -d Ubuntu` themselves.
    wsl --install -d $Distro --no-launch
    Write-Ok "$Distro installed"
}

# Make sure default WSL version is 2.
wsl --set-default-version 2 | Out-Null

# ---------------------------------------------------------------------------
# 3. Stage the Linux-side bootstrap script into the Ubuntu home
# ---------------------------------------------------------------------------
Write-Step "Staging Linux-side setup script"

$bashScriptHost = Join-Path $PSScriptRoot "setup-prereqs.sh"
if (-not (Test-Path $bashScriptHost)) {
    throw "Cannot find setup-prereqs.sh next to this PowerShell script."
}

# Translate "D:\foo\bar\setup-prereqs.sh" -> "/mnt/d/foo/bar/setup-prereqs.sh"
# on the Windows side, so the bash command we hand to wsl.exe is a single
# safe line and doesn't depend on wslpath being callable through the
# multi-line argument shim.
$drive   = $bashScriptHost.Substring(0, 1).ToLower()
$rest    = $bashScriptHost.Substring(2).Replace("\", "/")
$wslSrc  = "/mnt/$drive$rest"
$wslDest = "/opt/subwaybuilder-melbourne/setup-prereqs.sh"

# Single-line bash, single-quoted paths. sed strips CRLF in case the file
# was checked out with Windows line endings.
$bashCmd = "set -e; mkdir -p /opt/subwaybuilder-melbourne && " + `
           "cp '$wslSrc' '$wslDest' && " + `
           "chmod +x '$wslDest' && " + `
           "sed -i 's/\r`$//' '$wslDest'"

wsl -d $Distro -u root -- bash -c $bashCmd
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stage setup-prereqs.sh into $Distro (exit $LASTEXITCODE)."
}

Write-Ok "Staged at $wslDest inside $Distro"

# ---------------------------------------------------------------------------
# 4. Next-steps message
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Windows-side prerequisites are in place." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Launch Docker Desktop once to accept its EULA and enable the"
Write-Host "     WSL2 integration (Settings -> Resources -> WSL Integration ->"
Write-Host "     toggle on for $Distro). Quit and relaunch Docker."
Write-Host ""
Write-Host "  2. Open Ubuntu for the first time and set a username + password:"
Write-Host "         wsl -d $Distro" -ForegroundColor Yellow
Write-Host ""
Write-Host "  3. Inside Ubuntu, run the Linux-side installer:"
Write-Host "         sudo bash /opt/subwaybuilder-melbourne/setup-prereqs.sh" -ForegroundColor Yellow
Write-Host ""
Write-Host "  4. Smoke-test from inside Ubuntu:"
Write-Host "         docker --version && osmium --version && tippecanoe --version"
Write-Host "         java -version && mapshaper --version"
Write-Host "         conda --version && pmtiles version"
Write-Host ""
