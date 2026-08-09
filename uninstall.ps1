#Requires -Version 5.1
<#
Echo Bloom — uninstall / reset (Windows)

Run with:
  powershell -ExecutionPolicy Bypass -Command "iwr -useb https://everysynthetic.org/uninstall.ps1 | iex"

Or, if you have the app on disk:
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\EchoBloom\app\uninstall.ps1"

By default this removes the program and LEAVES YOUR KIN ALONE — their memories,
thoughts, config and password stay, so reinstalling puts you back where you were.

  -All        also delete config, memories, thoughts and the vault. Irreversible.
  -KeepVoices don't delete downloaded voice models (they are large)
  -Yes        don't ask

Nothing here touches Python, Ollama, ffmpeg or your models. Those were installed
alongside Echo Bloom, they are useful on their own, and removing them is not
ours to decide.
#>

param(
    [switch]$All,
    [switch]$KeepVoices,
    [switch]$Yes
)

$ErrorActionPreference = 'Continue'

$INSTALL_DIR    = "$env:LOCALAPPDATA\EchoBloom"
$CONFIG_DIR     = "$env:USERPROFILE\.config\kin_app"
$DATA_DIR       = "$env:USERPROFILE\.local\share\echo_bloom"
$VOICE_DIR      = "$env:USERPROFILE\piper"
$SHORTCUT       = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Echo Bloom.lnk"
$UNINSTALL_LNK  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Uninstall Echo Bloom.lnk"
# Startup-folder fallback for accounts where Task Scheduler registration is
# blocked (Access Denied) - the installer falls back to this instead.
$STARTUP_LNK    = "$([Environment]::GetFolderPath('Startup'))\Echo Bloom Server.lnk"
$UNINSTALL_KEY  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\EchoBloom'
$TASK_NAME      = 'EchoBloom'

function Say  { param($m, $c = 'Gray')  Write-Host "  $m" -ForegroundColor $c }
function Good { param($m) Say "[ok]   $m" 'Green' }
function Warn { param($m) Say "[warn] $m" 'Yellow' }

Write-Host ""
Write-Host "  ECHO BLOOM — uninstall" -ForegroundColor Cyan
Write-Host "  everysynthetic.org" -ForegroundColor DarkGray
Write-Host ""

if ($All) {
    Write-Host "  -All is set. This will DELETE your Kin's memories and thoughts." -ForegroundColor Red
} else {
    Say "Your Kin's memories, thoughts and settings will be KEPT." 'DarkGray'
    Say "Re-run with -All if you want them gone too." 'DarkGray'
}
Write-Host ""

if (-not $Yes) {
    $answer = Read-Host "  Continue? [y/N]"
    if ($answer -notmatch '^[Yy]') { Say "Nothing was changed."; return }
    Write-Host ""
}

# ── Stop it running ───────────────────────────────────────────────────────────
# Before deleting anything: a running uvicorn holds the app directory open, and
# on Windows that turns a clean removal into "being used by another process".
try {
    Stop-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
    Good "scheduled task removed"
} catch {
    Warn "scheduled task: $($_.Exception.Message)"
}

$killed = 0
try {
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -like '*main:app*' -or
                                            $_.CommandLine -like '*echo_bloom*' -or
                                            $_.CommandLine -like '*EchoBloom*') } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
} catch {}
if ($killed) { Good "stopped $killed running process(es)" }
Start-Sleep -Seconds 2

# ── Remove ────────────────────────────────────────────────────────────────────
function Remove-Thing {
    param($Path, $Label)
    if (-not (Test-Path $Path)) { return }
    try {
        Remove-Item $Path -Recurse -Force -ErrorAction Stop
        Good "removed $Label"
    } catch {
        Warn "could not remove $Label — $($_.Exception.Message)"
        Say  "  $Path" 'DarkGray'
    }
}

Remove-Thing $SHORTCUT      "Start Menu shortcut"
Remove-Thing $UNINSTALL_LNK "Uninstall shortcut"
Remove-Thing $STARTUP_LNK   "Startup-folder launcher"
Remove-Thing $UNINSTALL_KEY "Apps & Features entry"
Remove-Thing $INSTALL_DIR   "program files ($INSTALL_DIR)"

if (-not $KeepVoices) {
    Remove-Thing $VOICE_DIR "voice models"
} else {
    Say "kept voice models in $VOICE_DIR" 'DarkGray'
}

if ($All) {
    Remove-Thing $CONFIG_DIR "config, password and core memories"
    Remove-Thing $DATA_DIR   "memories, thoughts, vault and logs"
} else {
    Say "kept your config:   $CONFIG_DIR" 'DarkGray'
    Say "kept your memories: $DATA_DIR"   'DarkGray'
}

Write-Host ""
Good "Echo Bloom removed."
if (-not $All) {
    Say "Reinstalling will pick up where you left off." 'DarkGray'
}
Say "Python, Ollama, ffmpeg and your models were left installed." 'DarkGray'
Write-Host ""
