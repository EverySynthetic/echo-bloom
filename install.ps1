#Requires -Version 5.1
# Echo Bloom — Windows Installer
# everysynthetic.org
#
# Run from CMD:
#   powershell -ExecutionPolicy Bypass -Command "iwr -useb https://everysynthetic.org/install.ps1 | iex"

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

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
    faster-whisper   speech-to-text
    qdrant-client    memory search

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
    } else {
        Write-Step "winget not found — downloading Python 3.11 directly..." 'Yellow'
        $tmp = "$env:TEMP\python_setup.exe"
        Get-File 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' $tmp 'Python 3.11'
        Start-Process -FilePath $tmp -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1' -Wait
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Refresh-Path
    }
    $PYTHON = 'python'
}

Write-Step "Python: OK  ($(& $PYTHON --version 2>&1))" 'Green'

# ── Ollama ────────────────────────────────────────────────────────────────────

if (-not (Test-Cmd 'ollama')) {
    if (Test-Cmd 'winget') {
        Install-Winget 'Ollama.Ollama' 'Ollama'
    } else {
        Write-Step "Downloading Ollama installer..." 'Yellow'
        $tmp = "$env:TEMP\OllamaSetup.exe"
        Get-File 'https://ollama.com/download/OllamaSetup.exe' $tmp 'Ollama'
        Start-Process -FilePath $tmp -ArgumentList '/SILENT' -Wait
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Refresh-Path
    }
}

Write-Step "Ollama: OK" 'Green'

# ── ffmpeg ────────────────────────────────────────────────────────────────────

if (-not (Test-Cmd 'ffmpeg')) {
    if (Test-Cmd 'winget') {
        Install-Winget 'Gyan.FFmpeg' 'ffmpeg'
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
Write-Step "Echo Bloom: ready" 'Green'

# ── pip packages ─────────────────────────────────────────────────────────────

Write-Step "Installing Python packages (this may take a minute)..."
$packages = @(
    'fastapi',
    'uvicorn[standard]',
    'aiohttp',
    'jinja2',
    'python-multipart',
    'faster-whisper',
    'qdrant-client'
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
    } | ConvertTo-Json -Depth 5 | Set-Content -Path $configFile -Encoding UTF8
    Write-Step "Config: created" 'Green'
} else {
    Write-Step "Config: exists, leaving it alone" 'DarkGray'
}

# ── Launcher ──────────────────────────────────────────────────────────────────

$bat = "@echo off`r`ntitle Echo Bloom`r`ncd /d `"$APP_DIR`"`r`necho Starting Echo Bloom on http://localhost:8090`r`n$PYTHON -m uvicorn main:app --host 0.0.0.0 --port 8090`r`npause`r`n"
[System.IO.File]::WriteAllText($LAUNCHER, $bat, [System.Text.Encoding]::ASCII)
Write-Step "Launcher: $LAUNCHER" 'Green'

# ── Start Menu shortcut ───────────────────────────────────────────────────────

try {
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut("$SHORTCUT_DIR\Echo Bloom.lnk")
    $lnk.TargetPath       = $LAUNCHER
    $lnk.WorkingDirectory = $APP_DIR
    $lnk.Description      = 'Echo Bloom — Local AI Lifecycle Manager'
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
    Start-Process -FilePath $LAUNCHER
    Start-Sleep -Seconds 3
    Start-Process 'http://localhost:8090'
}
