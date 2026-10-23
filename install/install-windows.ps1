# One-step installer for Windows 10/11.
# Run in PowerShell from the project folder:
#   powershell -ExecutionPolicy Bypass -File install\install-windows.ps1            install, then start
#   powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Service   also start at every login
# Safe to run again: it only installs what is missing.
param([switch]$Service)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$Root = (Get-Location).Path
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again."
}

# 1. System tools: Python 3.12 and FFmpeg ------------------------------------------------------
$py = $null
try { $py = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $py) {
  Say "Installing Python 3.12..."
  winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent
  Refresh-Path
  $py = (& py -3.12 -c "import sys; print(sys.executable)")
}
Say "Python: $py"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Say "Installing FFmpeg..."
  winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements --silent
  Refresh-Path
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Warning "FFmpeg was installed but this window cannot see it yet. Close PowerShell, open a new one and run this script again."
  exit 1
}

# 2. Python environment ----------------------------------------------------------------------------
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Say "Creating the Python environment..."
  & $py -m venv .venv
}
Say "Installing Python packages (first time takes a few minutes)..."
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt

# 3. Keys copied from another computer (optional) -> Windows Credential Manager ----------------
if (Test-Path "secrets\api_keys.env") {
  Say "Found secrets\api_keys.env - loading keys into Windows Credential Manager..."
  & $venvPy scripts\import_keys.py
}

# 4. Check ---------------------------------------------------------------------------------------------
& $venvPy -c "from studio import bootstrap, config; bootstrap.init(); print('Studio ready. Data folder:', config.DATA_DIR)"

# 5. Start ----------------------------------------------------------------------------------------------
if ($Service) {
  Say "Starting the studio at every login (Task Scheduler)..."
  $action = New-ScheduledTaskAction -Execute $venvPy -Argument "-m studio" -WorkingDirectory $Root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName "YT Automation Studio" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Start-ScheduledTask -TaskName "YT Automation Studio"
  Say "Keep this PC from sleeping (Settings > System > Power) so scheduled videos are made on time."
  Start-Sleep -Seconds 4
  Start-Process "http://127.0.0.1:8765"
} else {
  Say "Done. Start it any time by double-clicking run.bat"
  Say "Start automatically at login:  powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Service"
}
