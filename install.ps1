#Requires -Version 5.1
# Echo Bloom — Windows Installer
# everysynthetic.org
#
# Run from CMD:
#   powershell -ExecutionPolicy Bypass -Command "iwr -useb https://everysynthetic.org/install.ps1 | iex"

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# Older Win10 builds negotiate TLS 1.0 by default, which fails against
# github.com and python.org.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor 3072
} catch {}

$INSTALL_DIR  = "$env:LOCALAPPDATA\EchoBloom"
$APP_DIR      = "$INSTALL_DIR\app"
$CONFIG_DIR   = "$env:USERPROFILE\.config\kin_app"
$SCRIPTS_DIR  = "$env:USERPROFILE\.local\share\echo_bloom\scripts"
$SHORTCUT_DIR = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$LAUNCHER     = "$INSTALL_DIR\start_echo_bloom.bat"
$LOG_FILE     = "$INSTALL_DIR\install.log"

# ── Transparency dialog ───────────────────────────────────────────────────────

Add-Type -AssemblyName System.Windows.Forms

$preview = @"
ECHO BLOOM — What This Installer Will Do
everysynthetic.org

The following will be checked and installed if missing:

  Python 3.11+    required to run the app
  Ollama          runs local AI models on your machine
  ffmpeg          required for voice features

  Python packages (via pip):
    fastapi          web framework
    uvicorn          app server
    aiohttp          async HTTP
    jinja2           templating
    python-multipart file uploads
    bcrypt           password hashing
    cryptography     license verification
    faster-whisper   speech-to-text
    qdrant-client    memory search
    psutil           process control (start/stop background work)

  Echo Bloom app files
    Installed to:  $INSTALL_DIR
    Config at:     $CONFIG_DIR
    Start Menu:    Echo Bloom shortcut

No administrator rights required.
Nothing is installed system-wide.
A log is written to:  $LOG_FILE

Press OK to begin, or Cancel to exit.
"@

$choice = [System.Windows.Forms.MessageBox]::Show(
    $preview,
    'Echo Bloom — Installation Preview',
    [System.Windows.Forms.MessageBoxButtons]::OKCancel,
    [System.Windows.Forms.MessageBoxIcon]::Information
)

if ($choice -ne 'OK') {
    Write-Host "`n  Nothing was installed. Goodbye." -ForegroundColor Yellow
    exit 0
}

# ── Helpers ───────────────────────────────────────────────────────────────────

function Write-Step {
    param([string]$Msg, [string]$Color = 'Cyan')
    $ts = Get-Date -Format 'HH:mm:ss'
    Write-Host "  [$ts] $Msg" -ForegroundColor $Color
    Add-Content -Path $LOG_FILE -Value "[$ts] $Msg" -ErrorAction SilentlyContinue
}

function Test-Cmd {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Refresh-Path {
    $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH','Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('PATH','User')
}

function Install-Winget {
    param([string]$Id, [string]$Label)
    Write-Step "Installing $Label via winget..."
    winget install --id $Id --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    Refresh-Path
}

function Get-File {
    param([string]$Url, [string]$Dest, [string]$Label = '')
    if ($Label) { Write-Step "Downloading $Label..." }
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

# ── Setup ─────────────────────────────────────────────────────────────────────

Clear-Host
Write-Host ""
Write-Host "  ECHO BLOOM INSTALLER" -ForegroundColor Cyan
Write-Host "  everysynthetic.org" -ForegroundColor DarkGray
Write-Host ""

foreach ($d in @($INSTALL_DIR, $APP_DIR, $CONFIG_DIR, $SCRIPTS_DIR)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
New-Item -Path $LOG_FILE -ItemType File -Force | Out-Null
Write-Step "Directories ready" 'Green'

# ── Python ────────────────────────────────────────────────────────────────────

$PYTHON = $null
foreach ($cmd in @('python', 'python3', 'py')) {
    try {
        $v = & $cmd --version 2>&1
        if ($v -match '3\.(1[0-9]|[2-9]\d)') { $PYTHON = $cmd; break }
    } catch {}
}

if (-not $PYTHON) {
    if (Test-Cmd 'winget') {
        Install-Winget 'Python.Python.3.11' 'Python 3.11'
        Write-Step "Waiting for Python to settle..." 'DarkGray'
        Start-Sleep -Seconds 5
        Refresh-Path
    } else {
        Write-Step "winget not found — downloading Python 3.11 directly..." 'Yellow'
        $tmp = "$env:TEMP\python_setup.exe"
        Get-File 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' $tmp 'Python 3.11'
        Start-Process -FilePath $tmp -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1' -Wait
        Start-Sleep -Seconds 5
        Refresh-Path
    }
    # Re-detect after install — winget may have registered a different command name
    $PYTHON = $null
    foreach ($cmd in @('python', 'python3', 'py')) {
        try {
            $v = & $cmd --version 2>&1
            if ($v -match '3\.(1[0-9]|[2-9]\d)') { $PYTHON = $cmd; break }
        } catch {}
    }
    if (-not $PYTHON) { $PYTHON = 'python' }
}

Write-Step "Python: OK  ($(& $PYTHON --version 2>&1))" 'Green'

# ── Ollama ────────────────────────────────────────────────────────────────────

if (-not (Test-Cmd 'ollama')) {
    if (Test-Cmd 'winget') {
        Install-Winget 'Ollama.Ollama' 'Ollama'
        Write-Step "Waiting for Ollama to settle..." 'DarkGray'
        Start-Sleep -Seconds 5
        Refresh-Path
    } else {
        Write-Step "Downloading Ollama installer..." 'Yellow'
        $tmp = "$env:TEMP\OllamaSetup.exe"
        Get-File 'https://ollama.com/download/OllamaSetup.exe' $tmp 'Ollama'
        Start-Process -FilePath $tmp -ArgumentList '/SILENT' -Wait
        Start-Sleep -Seconds 5
        Refresh-Path
    }
}

# Give Ollama's background service a moment to start after a fresh install
if (-not (Test-Cmd 'ollama')) {
    Write-Step "Ollama not yet on PATH — waiting a bit more..." 'Yellow'
    Start-Sleep -Seconds 8
    Refresh-Path
}

Write-Step "Ollama: OK" 'Green'

# ── ffmpeg ────────────────────────────────────────────────────────────────────

if (-not (Test-Cmd 'ffmpeg')) {
    if (Test-Cmd 'winget') {
        Install-Winget 'Gyan.FFmpeg' 'ffmpeg'
        Start-Sleep -Seconds 3
        Refresh-Path
    } else {
        Write-Step "ffmpeg not found and winget unavailable — voice features may be limited" 'Yellow'
    }
} else {
    Write-Step "ffmpeg: OK" 'Green'
}

# ── Download Echo Bloom ───────────────────────────────────────────────────────

Write-Step "Downloading Echo Bloom..."
$zipPath = "$INSTALL_DIR\echo-bloom.zip"
Get-File 'https://github.com/EverySynthetic/echo-bloom/archive/refs/heads/main.zip' $zipPath

Write-Step "Extracting..."
if (Test-Path "$INSTALL_DIR\echo-bloom-main") {
    Remove-Item "$INSTALL_DIR\echo-bloom-main" -Recurse -Force
}
if (Test-Path $APP_DIR) { Remove-Item $APP_DIR -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $INSTALL_DIR -Force
Rename-Item "$INSTALL_DIR\echo-bloom-main" $APP_DIR
Remove-Item $zipPath -Force

# $SCRIPTS_DIR was created empty and never populated, so the vault, bedtime,
# wander and roundtable had nothing to run on Windows. Mirrors deploy_scripts()
# in install.sh.
if (Test-Path "$APP_DIR\scripts") {
    try {
        Copy-Item "$APP_DIR\scripts\*" $SCRIPTS_DIR -Recurse -Force
        Write-Step "Lifecycle scripts: deployed" 'Green'
    } catch {
        Write-Step "Lifecycle scripts: $($_.Exception.Message)" 'Yellow'
    }
}
Write-Step "Echo Bloom: ready" 'Green'

# ── pip packages ─────────────────────────────────────────────────────────────

Write-Step "Installing Python packages (this may take a minute)..."
$packages = @(
    'fastapi',
    'uvicorn[standard]',
    'aiohttp',
    'jinja2',
    'python-multipart',
    'bcrypt',
    'cryptography',
    'faster-whisper',
    'qdrant-client',
    'psutil'
)

foreach ($pkg in $packages) {
    Write-Step "  $pkg" 'DarkGray'
    & $PYTHON -m pip install $pkg --quiet --disable-pip-version-check 2>&1 | Out-Null
}

Write-Step "Python packages: installed" 'Green'

# ── Default config ────────────────────────────────────────────────────────────

$configFile = "$CONFIG_DIR\kin_config.json"
if (-not (Test-Path $configFile)) {
    @{
        nodes     = @(@{ name = 'Local'; ip = 'localhost'; ollama_port = 11434; role = 'primary' })
        kin       = @()
        owner     = @{}
        vault_url = 'http://localhost:8765'
    # -Encoding UTF8 writes a BOM on PowerShell 5.1, and Python reads this
    # file with plain read_text() → JSONDecodeError → "kin_config.json
    # unreadable" on first boot. Same family as the .bat BOM bug.
    } | ConvertTo-Json -Depth 5 | ForEach-Object {
        [System.IO.File]::WriteAllText($configFile, $_, (New-Object System.Text.UTF8Encoding($false)))
    }
    Write-Step "Config: created" 'Green'
} else {
    Write-Step "Config: exists, leaving it alone" 'DarkGray'
}

# ── Icon ──────────────────────────────────────────────────────────────────────

$ICON_PNG = "$APP_DIR\static\icons\icon-512.png"
$ICON_ICO = "$INSTALL_DIR\echo-bloom.ico"
try {
    Add-Type -AssemblyName System.Drawing
    $bmp  = [System.Drawing.Bitmap]::new($ICON_PNG)
    $hico = $bmp.GetHicon()
    $ico  = [System.Drawing.Icon]::FromHandle($hico)
    $fs   = [System.IO.FileStream]::new($ICON_ICO, [System.IO.FileMode]::Create)
    $ico.Save($fs)
    $fs.Close()
    $bmp.Dispose()
    Write-Step "Icon: converted" 'Green'
} catch {
    Write-Step "Icon conversion skipped: $_" 'Yellow'
    $ICON_ICO = ""
}

# ── Launcher (manual / fallback — opens a visible window with uvicorn output) ─

$bat = @"
@echo off
chcp 65001 >nul
title Echo Bloom
cd /d "$APP_DIR"
echo Starting Echo Bloom on http://localhost:8090 ...
$PYTHON -m uvicorn main:app --host 0.0.0.0 --port 8090
pause
"@
[System.IO.File]::WriteAllText($LAUNCHER, $bat, (New-Object System.Text.UTF8Encoding($false)))
Write-Step "Launcher: $LAUNCHER" 'Green'

# ── Browser opener (what the icon runs — shows a progress window while waiting) ─

$OPENER = "$INSTALL_DIR\open_echo_bloom.ps1"
Write-Step "Downloading browser opener..."
Get-File 'https://raw.githubusercontent.com/EverySynthetic/echo-bloom/main/open_echo_bloom.ps1' $OPENER
Write-Step "Browser opener: $OPENER" 'Green'

# ── Scheduled task (auto-start at login, survives window close) ───────────────

try {
    $action    = New-ScheduledTaskAction -Execute $PYTHON `
                   -Argument "-m uvicorn main:app --host 0.0.0.0 --port 8090" `
                   -WorkingDirectory $APP_DIR
    $trigger   = New-ScheduledTaskTrigger -AtLogon
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
                   -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 5
    # Limited, not Highest: Highest requires an elevated shell to register.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
                   -LogonType Interactive -RunLevel Limited
    Unregister-ScheduledTask -TaskName 'EchoBloom' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'EchoBloom' -Action $action -Trigger $trigger `
                           -Settings $settings -Principal $principal | Out-Null
    Write-Step "Scheduled task: registered (auto-starts at login)" 'Green'
} catch {
    Write-Step "Scheduled task skipped: $_" 'Yellow'
}

# ── Start Menu shortcut (runs hidden opener, no CMD window) ──────────────────

try {
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut("$SHORTCUT_DIR\Echo Bloom.lnk")
    $lnk.TargetPath       = 'powershell.exe'
    $lnk.Arguments        = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$OPENER`""
    $lnk.WorkingDirectory = $INSTALL_DIR
    $lnk.Description      = 'Echo Bloom — Local AI Lifecycle Manager'
    if ($ICON_ICO -and (Test-Path $ICON_ICO)) { $lnk.IconLocation = "$ICON_ICO, 0" }
    $lnk.Save()
    Write-Step "Start Menu shortcut: created" 'Green'
} catch {
    Write-Step "Shortcut skipped: $_" 'Yellow'
}

# ── Done ──────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ──────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  ECHO BLOOM INSTALLED SUCCESSFULLY" -ForegroundColor Green
Write-Host ""
Write-Host "  Start Menu  →  Echo Bloom" -ForegroundColor Cyan
Write-Host "  Then open   →  http://localhost:8090" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Install log: $LOG_FILE" -ForegroundColor DarkGray
Write-Host "  ──────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

$launch = [System.Windows.Forms.MessageBox]::Show(
    'Echo Bloom is installed.' + "`n`n" + 'Launch it now?',
    'Echo Bloom',
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question
)

if ($launch -eq 'Yes') {
    Start-Process -FilePath $LAUNCHER -WindowStyle Minimized
    Write-Host "  Waiting for Echo Bloom to start..." -ForegroundColor DarkGray
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri 'http://localhost:8090' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($r.StatusCode -lt 500) { $ready = $true; break }
        } catch {}
    }
    if ($ready) {
        Start-Process 'http://localhost:8090'
    } else {
        Write-Host "  App is taking longer than expected." -ForegroundColor Yellow
        Write-Host "  Once the Echo Bloom window shows 'Application startup complete', open:" -ForegroundColor Yellow
        Write-Host "  http://localhost:8090" -ForegroundColor Cyan
    }
}
