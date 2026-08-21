<#
Echo Bloom - uninstall / reset (Windows)

PowerShell 5.1 (every Win10/11). No #Requires line: it is not needed, and
this file is also fetched with iwr | iex.

A 5.1 test on the A15 (2026-08-21) showed param() and #Requires both parse
and run when iex'd as a raw string at -Command scope. The original
param() one-liner would have started; what broke it was a 404, not parse.

Why there is still no script-level param(): iwr -useb URL | iex is a
pipeline. There is nowhere to hang -All. Switches are read from $args
when invoked with -File. Under iex, $args is empty; wiping memories is
the interactive ALL prompt, not a flag the one-liner cannot pass.

Keep the program, keep memories (Start Menu / Apps & Features do this;
Don's call 2026-08-21: no confirm on that door, -Yes, memories stay):
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\EchoBloom\app\uninstall.ps1" -Yes

Also delete config, memories, thoughts and the vault (irreversible):
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\EchoBloom\app\uninstall.ps1" -All

If you do not have the app on disk:
  powershell -ExecutionPolicy Bypass -Command "iwr -useb https://everysynthetic.org/uninstall.ps1 | iex"
  (prompts: y = remove program, ALL = also delete memories)

  -KeepVoices   don't delete downloaded voice models (they are large)
  -Yes          don't ask (Uninstall shortcut / Apps & Features; does not imply -All)

Nothing here touches Python, Ollama, ffmpeg or your models.
#>

$ErrorActionPreference = 'Continue'

# Bound from -File arguments (-All, -Yes, -KeepVoices). iwr | iex is a
# pipeline, so $args is empty on that path; the ALL prompt is the door.
$script:All        = $false
$script:KeepVoices = $false
$script:Yes        = $false
foreach ($a in $args) {
    if     ($a -match '^-All$')        { $script:All        = $true }
    elseif ($a -match '^-KeepVoices$') { $script:KeepVoices = $true }
    elseif ($a -match '^-Yes$')        { $script:Yes        = $true }
}

$INSTALL_DIR    = "$env:LOCALAPPDATA\EchoBloom"
$CONFIG_DIR     = "$env:USERPROFILE\.config\kin_app"
$DATA_DIR       = "$env:USERPROFILE\.local\share\echo_bloom"
$SCRIPTS_DIR    = "$DATA_DIR\scripts"
$VOICE_DIR      = "$env:USERPROFILE\piper"
$SHORTCUT       = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Echo Bloom.lnk"
$UNINSTALL_LNK  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Uninstall Echo Bloom.lnk"
$STARTUP_LNK    = "$([Environment]::GetFolderPath('Startup'))\Echo Bloom Server.lnk"
$UNINSTALL_KEY  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\EchoBloom'
$TASK_NAME      = 'EchoBloom'

function Say  { param($m, $c = 'Gray')  Write-Host "  $m" -ForegroundColor $c }
function Good { param($m) Say "[ok]   $m" 'Green' }
function Warn { param($m) Say "[warn] $m" 'Yellow' }

# End-state rows: what is true on the machine now, not which cmdlet we called.
$script:Report = @()

function Add-Report {
    param($Label, $State, $Detail = '')
    $script:Report += [pscustomobject]@{ Label = $Label; State = $State; Detail = $Detail }
}

function Assert-PathGone {
    param($Path, $Label, $WantGone = $true)
    $here = Test-Path $Path
    if ($WantGone) {
        if ($here) { Add-Report $Label 'still' $Path }
        else       { Add-Report $Label 'gone'  $Path }
    } else {
        if ($here) { Add-Report $Label 'kept'   $Path }
        else       { Add-Report $Label 'absent' $Path }
    }
}

function Remove-Thing {
    param($Path, $Label)
    if (-not (Test-Path $Path)) {
        Add-Report $Label 'absent' $Path
        return
    }
    try {
        Remove-Item $Path -Recurse -Force -ErrorAction Stop
    } catch {
        Warn "could not remove $Label - $($_.Exception.Message)"
    }
    Assert-PathGone $Path $Label $true
}

function Uninstall-EchoBloom {
    Write-Host ""
    Write-Host "  ECHO BLOOM - uninstall" -ForegroundColor Cyan
    Write-Host "  everysynthetic.org" -ForegroundColor DarkGray
    Write-Host ""

    if ($script:All) {
        Write-Host "  -All is set. This will DELETE your Kin memories and thoughts." -ForegroundColor Red
    } else {
        Say "Your Kin memories, thoughts, logs and vault will be KEPT unless you type ALL." 'DarkGray'
        Say "Program scripts (wander, bedtime, roundtable) are always removed." 'DarkGray'
    }
    Write-Host ""

    if (-not $script:Yes) {
        if ($script:All) {
            $answer = Read-Host "  Continue and delete memories? [y/N]"
            if ($answer -notmatch '^[Yy]') { Say "Nothing was changed."; return }
        } else {
            $answer = Read-Host "  Continue? [y/N]  (type ALL to also delete memories)"
            if ($answer -match '^[Aa][Ll][Ll]$') {
                $script:All = $true
                Write-Host "  Memories will be deleted." -ForegroundColor Red
            } elseif ($answer -notmatch '^[Yy]') {
                Say "Nothing was changed."
                return
            }
        }
        Write-Host ""
    }

    # -- Stop it running -------------------------------------------------------
    # A running uvicorn holds the app directory open; on Windows that turns
    # a clean removal into "being used by another process".
    $taskBefore = $null
    try { $taskBefore = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue } catch {}
    try {
        if ($taskBefore) {
            Stop-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
        }
    } catch {
        Warn "scheduled task: $($_.Exception.Message)"
    }
    $taskAfter = $null
    try { $taskAfter = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue } catch {}
    if ($taskAfter) {
        Add-Report "scheduled task '$TASK_NAME'" 'still' "still registered after Unregister-ScheduledTask"
    } elseif ($taskBefore) {
        Add-Report "scheduled task '$TASK_NAME'" 'gone' ''
    } else {
        Add-Report "scheduled task '$TASK_NAME'" 'absent' 'was not registered'
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
    # cloudflared.exe runs detached on purpose (remote access survives after
    # the wizard closes), which is exactly what let it survive into an
    # uninstall and lock its own .exe inside the install dir.
    try {
        Stop-Process -Name 'cloudflared' -Force -ErrorAction SilentlyContinue
    } catch {}
    if ($killed) { Say "stopped $killed running process(es)" 'DarkGray' }
    Start-Sleep -Seconds 2

    Remove-Thing $SHORTCUT      "Start Menu shortcut (Echo Bloom.lnk)"
    Remove-Thing $UNINSTALL_LNK "Start Menu uninstall shortcut"
    Remove-Thing $STARTUP_LNK   "Startup-folder launcher"
    Remove-Thing $UNINSTALL_KEY "Apps and Features registry key"
    Remove-Thing $INSTALL_DIR   "install dir"

    if (-not $script:KeepVoices) {
        Remove-Thing $VOICE_DIR "voice models"
    } else {
        Assert-PathGone $VOICE_DIR "voice models" $false
    }

    # scripts/ lives under DATA_DIR but it is program code (wander, bedtime,
    # roundtable), not user data. Leaving it on a keep-memories uninstall
    # lets a deleted-in-newer-release .py stay on the import path and shadow
    # a later install. Always remove it. Report it as its own row so the
    # program-vs-data split is visible, not accidental.
    Remove-Thing $SCRIPTS_DIR "lifecycle scripts (program code)"

    if ($script:All) {
        Remove-Thing $CONFIG_DIR "config, password and core memories"
        Remove-Thing $DATA_DIR   "memories, thoughts, vault and logs"
    } else {
        Assert-PathGone $CONFIG_DIR "config (kept unless ALL)" $false
        Assert-PathGone $DATA_DIR   "memories, vault and logs (kept unless ALL)" $false
    }

    Write-Host ""
    Write-Host "  On this machine now:" -ForegroundColor Cyan
    $anyStill = $false
    foreach ($row in $script:Report) {
        switch ($row.State) {
            'gone' {
                Good "$($row.Label): gone"
            }
            'absent' {
                Say "$($row.Label): was not present" 'DarkGray'
            }
            'kept' {
                Say "$($row.Label): kept" 'DarkGray'
                if ($row.Detail) { Say "  $($row.Detail)" 'DarkGray' }
            }
            'still' {
                $anyStill = $true
                Warn "$($row.Label): STILL THERE"
                if ($row.Detail) { Say "  $($row.Detail)" 'DarkGray' }
            }
        }
    }
    Write-Host ""
    if ($anyStill) {
        Warn "Uninstall finished with leftovers. Paths are above."
    } else {
        Good "Echo Bloom is not on this machine (program files / task / shortcuts / Apps entry)."
    }
    if (-not $script:All) {
        Say "Reinstalling will pick up where you left off." 'DarkGray'
    }
    Say "Python, Ollama, ffmpeg and your models were left installed." 'DarkGray'
    Write-Host ""
}

Uninstall-EchoBloom
