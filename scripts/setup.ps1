<#
  Windows 10 / 11 setup.  Run from the repo folder in PowerShell:

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
        create venv, install packages, create .env

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Service
        ...and register a startup task that runs in the background as SYSTEM
        (no login needed, auto-restarts). Needs an Administrator PowerShell.

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -RemoveService
        remove the startup task (Administrator)

  Safe to re-run after `git pull`: it updates packages and restarts the task.
#>
param(
    [switch]$Service,
    [switch]$RemoveService
)

$ErrorActionPreference = "Stop"
$AppDir   = Split-Path -Parent $PSScriptRoot
$TaskName = "HeartbeatPublisher"
$VenvPy   = Join-Path $AppDir "venv\Scripts\python.exe"
$VenvPyW  = Join-Path $AppDir "venv\Scripts\pythonw.exe"
Set-Location $AppDir

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($RemoveService) {
    if (-not (Test-Admin)) { throw "Open PowerShell as Administrator to remove the startup task." }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$AppDir*publisher.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "[setup] Startup task removed."
    exit 0
}

# --- 1. Virtual environment ------------------------------------------------
if (-not (Test-Path $VenvPy)) {
    Write-Host "[setup] Creating virtual environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv venv
    }
    if (-not (Test-Path $VenvPy)) {
        throw ("Python 3 not found (or it is the Microsoft Store stub). Install it with:`n" +
               "  winget install -e --id Python.Python.3.12`nthen open a NEW PowerShell window and re-run.")
    }
}

# --- 2. Packages ------------------------------------------------------------
Write-Host "[setup] Installing packages..."
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $VenvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Package install failed (check internet / proxy)." }

# --- 3. .env -----------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[setup] Created .env from .env.example -> edit it now:  notepad $AppDir\.env"
}

Write-Host ""
& $VenvPy publisher.py --show-id
Write-Host ""

# --- 4. Optional: background startup task ------------------------------------
if (-not $Service) {
    Write-Host "[setup] Done. Test with:  venv\Scripts\python.exe publisher.py --once"
    Write-Host "[setup] Run at boot:      (Admin PowerShell) powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Service"
    exit 0
}

if (-not (Test-Admin)) { throw "Open PowerShell as Administrator to install the startup task." }
if ((Get-Content ".env" -Raw) -notmatch "(?m)^HIVEMQ_HOST=\S+") {
    throw "Fill in .env (HIVEMQ_HOST etc.) first, then re-run with -Service."
}

Write-Host "[setup] Registering startup task '$TaskName' (runs as SYSTEM, no window)..."

# Stop any copy that is already running so the new code is picked up
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$AppDir*publisher.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$action = New-ScheduledTaskAction -Execute $VenvPyW `
    -Argument "`"$AppDir\publisher.py`"" -WorkingDirectory $AppDir

# Start at boot, plus a 5-minute watchdog trigger that relaunches it if it ever died
# (MultipleInstances IgnoreNew means it does nothing while the publisher is running).
$triggers = @(New-ScheduledTaskTrigger -AtStartup)
try {
    $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
} catch {
    Write-Host "[setup] Watchdog trigger not supported here; using startup trigger only."
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description "MQTT heartbeat publisher ($AppDir)" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$info = Get-ScheduledTask -TaskName $TaskName
Write-Host "[setup] Task state: $($info.State)"
Write-Host "[setup] Logs: $AppDir\logs\publisher.log"
Write-Host "        Get-Content `"$AppDir\logs\publisher.log`" -Tail 20 -Wait"
