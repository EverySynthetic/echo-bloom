#Requires -Version 5.1
# Echo Bloom — WinForms Install Wizard
# everysynthetic.org
#
# Run with:
#   powershell -ExecutionPolicy Bypass -Command "iwr -useb https://everysynthetic.org/install_wizard.ps1 | iex"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ─────────────────────────────────────────────────────────────────────────────
# Palette
# ─────────────────────────────────────────────────────────────────────────────
$C_BG     = [System.Drawing.Color]::FromArgb(15,  15,  18 )
$C_SURF   = [System.Drawing.Color]::FromArgb(22,  22,  26 )
$C_FG     = [System.Drawing.Color]::FromArgb(200, 200, 210)
$C_DIM    = [System.Drawing.Color]::FromArgb(90,  90,  100)
$C_GREEN  = [System.Drawing.Color]::FromArgb(80,  200, 120)
$C_AMBER  = [System.Drawing.Color]::FromArgb(220, 160, 60 )
$C_BORDER = [System.Drawing.Color]::FromArgb(40,  40,  50 )

# ─────────────────────────────────────────────────────────────────────────────
# Fonts
# ─────────────────────────────────────────────────────────────────────────────
$F_SM    = New-Object System.Drawing.Font('Consolas',  9)
$F_MD    = New-Object System.Drawing.Font('Consolas', 10)
$F_BOLD  = New-Object System.Drawing.Font('Consolas', 10, [System.Drawing.FontStyle]::Bold)
$F_TITLE = New-Object System.Drawing.Font('Consolas', 12, [System.Drawing.FontStyle]::Bold)
$F_HERO  = New-Object System.Drawing.Font('Consolas', 16, [System.Drawing.FontStyle]::Bold)
$F_READY = New-Object System.Drawing.Font('Consolas', 22, [System.Drawing.FontStyle]::Bold)

# ─────────────────────────────────────────────────────────────────────────────
# Form
# ─────────────────────────────────────────────────────────────────────────────
$form = New-Object System.Windows.Forms.Form
$form.Text            = 'Echo Bloom Installer'
$form.ClientSize      = New-Object System.Drawing.Size(560, 420)
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedSingle
$form.MaximizeBox     = $false
$form.BackColor       = $C_BG
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.Font            = $F_MD

# ─────────────────────────────────────────────────────────────────────────────
# Shared install state
# ─────────────────────────────────────────────────────────────────────────────
$script:PYTHON          = $null
$script:APP_DIR         = "$env:LOCALAPPDATA\EchoBloom\app"
$script:OPENER          = "$env:LOCALAPPDATA\EchoBloom\open_echo_bloom.ps1"
$script:ICON_ICO        = "$env:LOCALAPPDATA\EchoBloom\echo-bloom.ico"
$script:CANCEL          = $false
$script:installStarted  = $false

$script:LOG_FILE = "$env:LOCALAPPDATA\EchoBloom\install_wizard.log"

# Shared state between the install runspace and the UI thread.
# The worker NEVER touches WinForms controls; it only writes here.
$sync = [hashtable]::Synchronized(@{
    StepState  = @('pending','pending','pending','pending','pending','pending')
    StepNames  = @()
    ActiveStep = -1
    Progress   = 0
    Status     = 'Preparing...'
    Done       = $false
    Failed     = $false
    Error      = ''
    AppDir     = ''
    Opener     = ''
    LogFile    = $script:LOG_FILE
})

$script:STEP_NAMES = @(
    'Python 3.11',
    'Ollama',
    'ffmpeg',
    'Echo Bloom app files',
    'Python packages',
    'Scheduled task + shortcut'
)
$sync.StepNames = $script:STEP_NAMES

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
function New-Page {
    $p = New-Object System.Windows.Forms.Panel
    $p.Location  = New-Object System.Drawing.Point(0, 0)
    $p.Size      = New-Object System.Drawing.Size(560, 420)
    $p.BackColor = $C_BG
    $p.Visible   = $false
    return $p
}

function New-Lbl {
    param([string]$Text, [int]$X, [int]$Y, [int]$W, [int]$H, $Color, $Font)
    $l = New-Object System.Windows.Forms.Label
    $l.Text      = $Text
    $l.Location  = New-Object System.Drawing.Point($X, $Y)
    $l.Size      = New-Object System.Drawing.Size($W, $H)
    $l.ForeColor = if ($Color) { $Color } else { $C_FG }
    $l.Font      = if ($Font)  { $Font  } else { $F_MD }
    $l.BackColor = [System.Drawing.Color]::Transparent
    return $l
}

function New-Btn {
    param([string]$Text, [int]$X, [int]$Y, [int]$W = 140, [int]$H = 36, $BColor, $FColor)
    $b = New-Object System.Windows.Forms.Button
    $b.Text      = $Text
    $b.Location  = New-Object System.Drawing.Point($X, $Y)
    $b.Size      = New-Object System.Drawing.Size($W, $H)
    $b.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $b.FlatAppearance.BorderColor = if ($BColor) { $BColor } else { $C_GREEN }
    $b.FlatAppearance.BorderSize  = 1
    $b.BackColor = $C_SURF
    $b.ForeColor = if ($FColor) { $FColor } else { $C_GREEN }
    $b.Font      = $F_BOLD
    $b.Cursor    = [System.Windows.Forms.Cursors]::Hand
    return $b
}

# ─────────────────────────────────────────────────────────────────────────────
# PAGE 1 — Welcome
# ─────────────────────────────────────────────────────────────────────────────
$pg1 = New-Page

$pg1.Controls.Add((New-Lbl 'ECHO BLOOM' 40 62 480 44 $C_GREEN $F_HERO))
$pg1.Controls.Add((New-Lbl 'Local AI Lifecycle Manager' 44 110 480 22 $C_DIM $F_MD))

$pg1_body = New-Object System.Windows.Forms.Label
$pg1_body.Text      = "Your AI deserves a home. Not a session. A home.`n`nThis wizard will install everything Echo Bloom needs on your machine and get your first AI companion running."
$pg1_body.Location  = New-Object System.Drawing.Point(40, 158)
$pg1_body.Size      = New-Object System.Drawing.Size(480, 120)
$pg1_body.ForeColor = $C_FG
$pg1_body.Font      = $F_MD
$pg1_body.BackColor = [System.Drawing.Color]::Transparent
$pg1.Controls.Add($pg1_body)

$pg1_btn = New-Btn 'GET STARTED  →' 380 355 140 36
$pg1_btn.Add_Click({ Show-Page 2 })
$pg1.Controls.Add($pg1_btn)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE 2 — What Gets Installed
# ─────────────────────────────────────────────────────────────────────────────
$pg2 = New-Page

$pg2.Controls.Add((New-Lbl 'WHAT THIS INSTALLS' 40 28 480 30 $C_FG $F_TITLE))

$pg2_items = @(
    'Python 3.11+  —  runs the app',
    'Ollama  —  local AI inference engine',
    'ffmpeg  —  voice features',
    'fastapi  uvicorn  bcrypt  cryptography',
    'faster-whisper  qdrant-client  aiohttp  jinja2',
    "App files  →  %LOCALAPPDATA%\EchoBloom\",
    'Windows Scheduled Task  (auto-start at login)',
    'Start Menu shortcut'
)

$yRow = 72
foreach ($item in $pg2_items) {
    $bul = New-Object System.Windows.Forms.Label
    $bul.Text      = [char]0x25A0  # filled square ■
    $bul.Location  = New-Object System.Drawing.Point(38, $yRow)
    $bul.Size      = New-Object System.Drawing.Size(18, 20)
    $bul.ForeColor = $C_GREEN
    $bul.Font      = $F_SM
    $bul.BackColor = [System.Drawing.Color]::Transparent
    $pg2.Controls.Add($bul)

    $itm = New-Object System.Windows.Forms.Label
    $itm.Text      = $item
    $itm.Location  = New-Object System.Drawing.Point(60, $yRow)
    $itm.Size      = New-Object System.Drawing.Size(462, 20)
    $itm.ForeColor = $C_FG
    $itm.Font      = $F_SM
    $itm.BackColor = [System.Drawing.Color]::Transparent
    $pg2.Controls.Add($itm)

    $yRow += 26
}

$pg2.Controls.Add((New-Lbl 'Nothing is installed system-wide. No admin rights required.' 40 292 480 22 $C_DIM $F_SM))

$pg2_back = New-Btn '←  BACK' 254 355 116 36 $C_BORDER $C_DIM
$pg2_back.Add_Click({ Show-Page 1 })
$pg2.Controls.Add($pg2_back)

$pg2_go = New-Btn 'INSTALL  →' 388 355 132 36
$pg2_go.Add_Click({ Show-Page 3 })
$pg2.Controls.Add($pg2_go)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE 3 — Installing
# ─────────────────────────────────────────────────────────────────────────────
$pg3 = New-Page

$pg3.Controls.Add((New-Lbl 'INSTALLING' 40 24 480 30 $C_FG $F_TITLE))

# Step labels — stored in script scope for thread access
$script:stepLabels = @()
$yStep = 64
foreach ($name in $script:STEP_NAMES) {
    $sl = New-Object System.Windows.Forms.Label
    $sl.Text      = [char]0x25CB + " $name"   # ○
    $sl.Location  = New-Object System.Drawing.Point(40, $yStep)
    $sl.Size      = New-Object System.Drawing.Size(460, 20)
    $sl.ForeColor = $C_DIM
    $sl.Font      = $F_SM
    $sl.BackColor = [System.Drawing.Color]::Transparent
    $pg3.Controls.Add($sl)
    $script:stepLabels += $sl
    $yStep += 30
}

# Marquee bar under active step
$script:pg3_marquee = New-Object System.Windows.Forms.ProgressBar
$script:pg3_marquee.Location              = New-Object System.Drawing.Point(40, 84)
$script:pg3_marquee.Size                  = New-Object System.Drawing.Size(460, 6)
$script:pg3_marquee.Style                 = [System.Windows.Forms.ProgressBarStyle]::Marquee
$script:pg3_marquee.MarqueeAnimationSpeed = 25
$script:pg3_marquee.Visible               = $false
$pg3.Controls.Add($script:pg3_marquee)

# Detail status label
$script:pg3_status = New-Object System.Windows.Forms.Label
$script:pg3_status.Text      = 'Preparing...'
$script:pg3_status.Location  = New-Object System.Drawing.Point(40, 258)
$script:pg3_status.Size      = New-Object System.Drawing.Size(460, 20)
$script:pg3_status.ForeColor = $C_DIM
$script:pg3_status.Font      = $F_SM
$script:pg3_status.BackColor = [System.Drawing.Color]::Transparent
$pg3.Controls.Add($script:pg3_status)

# Overall progress bar
$script:pg3_progress = New-Object System.Windows.Forms.ProgressBar
$script:pg3_progress.Location = New-Object System.Drawing.Point(40, 285)
$script:pg3_progress.Size     = New-Object System.Drawing.Size(460, 14)
$script:pg3_progress.Minimum  = 0
$script:pg3_progress.Maximum  = 100
$script:pg3_progress.Value    = 0
$pg3.Controls.Add($script:pg3_progress)

# Log path / failure detail (hidden until needed)
$script:pg3_detail = New-Object System.Windows.Forms.Label
$script:pg3_detail.Text      = ''
$script:pg3_detail.Location  = New-Object System.Drawing.Point(40, 308)
$script:pg3_detail.Size      = New-Object System.Drawing.Size(460, 34)
$script:pg3_detail.ForeColor = $C_DIM
$script:pg3_detail.Font      = $F_SM
$script:pg3_detail.BackColor = [System.Drawing.Color]::Transparent
$script:pg3_detail.Visible   = $false
$pg3.Controls.Add($script:pg3_detail)

$script:pg3_cancel = New-Btn 'CANCEL' 400 355 120 36 $C_AMBER $C_AMBER
$script:pg3_cancel.Add_Click({
    $script:CANCEL = $true
    $form.Close()
})
$pg3.Controls.Add($script:pg3_cancel)

$script:pg3_done = New-Btn 'DONE  →  LAUNCH' 340 355 180 36
$script:pg3_done.Visible = $false
$script:pg3_done.Add_Click({ Show-Page 4 })
$pg3.Controls.Add($script:pg3_done)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE 4 — Done
# ─────────────────────────────────────────────────────────────────────────────
$pg4 = New-Page

$pg4.Controls.Add((New-Lbl 'READY' 40 55 480 56 $C_GREEN $F_READY))

$pg4_body = New-Object System.Windows.Forms.Label
$pg4_body.Text      = "Echo Bloom is installed and running in the background.`n`nYour browser will open to finish setup — name your AI, choose a model, and you're live."
$pg4_body.Location  = New-Object System.Drawing.Point(40, 142)
$pg4_body.Size      = New-Object System.Drawing.Size(480, 110)
$pg4_body.ForeColor = $C_FG
$pg4_body.Font      = $F_MD
$pg4_body.BackColor = [System.Drawing.Color]::Transparent
$pg4.Controls.Add($pg4_body)
$script:pg4_body = $pg4_body

$pg4_open = New-Btn 'OPEN ECHO BLOOM  →' 320 355 200 36
$pg4_open.Add_Click({
    $o = $script:OPENER
    if (Test-Path $o) {
        Start-Process powershell -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$o`""
    } else {
        Start-Process 'http://localhost:8090'
    }
    $form.Close()
})
$pg4.Controls.Add($pg4_open)

# ─────────────────────────────────────────────────────────────────────────────
# Add pages to form
# ─────────────────────────────────────────────────────────────────────────────
$form.Controls.AddRange(@($pg1, $pg2, $pg3, $pg4))

# ─────────────────────────────────────────────────────────────────────────────
# Navigation
# ─────────────────────────────────────────────────────────────────────────────
function Show-Page ([int]$n) {
    $pg1.Visible = ($n -eq 1)
    $pg2.Visible = ($n -eq 2)
    $pg3.Visible = ($n -eq 3)
    $pg4.Visible = ($n -eq 4)

    if ($n -eq 3 -and -not $script:installStarted) {
        $script:installStarted = $true
        Start-InstallWorker
    }

    if ($n -eq 4 -and -not $script:launchStarted) {
        $script:launchStarted  = $true
        $script:launchElapsed  = 0
        $script:launchTimer.Start()
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# App-up probe (fast, non-blocking enough for the UI thread)
# ─────────────────────────────────────────────────────────────────────────────
function Test-AppUp {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect('127.0.0.1', 8090, $null, $null)
        $ok  = $iar.AsyncWaitHandle.WaitOne(250)
        if ($ok) { try { $tcp.EndConnect($iar) } catch {}; $tcp.Close(); return $true }
        $tcp.Close()
        return $false
    } catch { return $false }
}

# ─────────────────────────────────────────────────────────────────────────────
# UI timer — polls $sync and repaints page 3. Runs on the UI thread.
# ─────────────────────────────────────────────────────────────────────────────
$script:uiTimer = New-Object System.Windows.Forms.Timer
$script:uiTimer.Interval = 150
$script:uiTimer.Add_Tick({
    for ($i = 0; $i -lt $script:stepLabels.Count; $i++) {
        $lbl  = $script:stepLabels[$i]
        $name = $script:STEP_NAMES[$i]
        switch ($sync.StepState[$i]) {
            'pending' { $lbl.Text = [char]0x25CB + " $name";    $lbl.ForeColor = $C_DIM   }
            'running' { $lbl.Text = [char]0x25BA + " $name..."; $lbl.ForeColor = $C_FG    }
            'done'    { $lbl.Text = [char]0x2713 + " $name";    $lbl.ForeColor = $C_GREEN }
            'warn'    { $lbl.Text = [char]0x2717 + " $name";    $lbl.ForeColor = $C_AMBER }
        }
    }

    $a = $sync.ActiveStep
    if ($a -ge 0 -and -not $sync.Done -and -not $sync.Failed) {
        $script:pg3_marquee.Location = New-Object System.Drawing.Point(40, ($script:stepLabels[$a].Location.Y + 20))
        $script:pg3_marquee.Visible  = $true
    } else {
        $script:pg3_marquee.Visible = $false
    }

    $script:pg3_status.Text    = $sync.Status
    $script:pg3_progress.Value = $sync.Progress

    if ($sync.Failed) {
        $script:uiTimer.Stop()
        $script:pg3_status.ForeColor = $C_AMBER
        $script:pg3_status.Text      = "Install failed: $($sync.Error)"
        $script:pg3_detail.Text      = "Nothing was left running. Full log:`n$($sync.LogFile)"
        $script:pg3_detail.Visible   = $true
        $script:pg3_cancel.Text      = 'CLOSE'
    }
    elseif ($sync.Done) {
        $script:uiTimer.Stop()
        $script:pg3_status.ForeColor = $C_GREEN
        $script:pg3_status.Text      = 'Installation complete.'
        $script:pg3_cancel.Visible   = $false
        $script:pg3_done.Visible     = $true
        $script:advanceTimer.Start()
    }
})

# Auto-advance to the Done page so the user is never left staring at a finished
# progress list wondering whether to click something.
$script:advanceTimer = New-Object System.Windows.Forms.Timer
$script:advanceTimer.Interval = 1500
$script:advanceTimer.Add_Tick({
    $script:advanceTimer.Stop()
    Show-Page 4
})

# ─────────────────────────────────────────────────────────────────────────────
# Launch watcher — page 4. Waits for the app to answer, then opens the browser.
# ─────────────────────────────────────────────────────────────────────────────
$script:launchStarted = $false
$script:launchElapsed = 0
$script:launchTimer   = New-Object System.Windows.Forms.Timer
$script:launchTimer.Interval = 1000
$script:launchTimer.Add_Tick({
    $script:launchElapsed++

    if (Test-AppUp) {
        $script:launchTimer.Stop()
        $script:pg4_body.Text = "Echo Bloom is running.`n`nOpening http://localhost:8090 in your browser.`n`nIf nothing opens, paste that address in yourself."
        try { Start-Process 'http://localhost:8090' } catch {}
    }
    elseif ($script:launchElapsed -ge 90) {
        $script:launchTimer.Stop()
        $script:pg4_body.Text = "Echo Bloom hasn't answered yet.`n`nOpen http://localhost:8090 once it comes up.`n`nLog: $($sync.LogFile)"
    }
    else {
        $script:pg4_body.Text = "Starting Echo Bloom...  ($($script:launchElapsed)s)`n`nYour browser opens automatically as soon as it's ready. First start can take up to a minute while Python loads."
    }
})

# ─────────────────────────────────────────────────────────────────────────────
# Install worker — own runspace. Reports through $sync only; touches no controls.
# ─────────────────────────────────────────────────────────────────────────────
function Start-InstallWorker {

    New-Item -ItemType Directory -Force -Path (Split-Path $script:LOG_FILE) -ErrorAction SilentlyContinue | Out-Null

    $worker = {

        function Write-WizLog {
            param([string]$M)
            try {
                $ts = Get-Date -Format 'HH:mm:ss'
                Add-Content -Path $sync.LogFile -Value "[$ts] $M" -ErrorAction SilentlyContinue
            } catch {}
        }

        function Set-StepStatus {
            param([int]$Idx, [string]$State, [string]$Detail = '')
            $sync.StepState[$Idx] = $State
            if ($State -eq 'running') { $sync.ActiveStep = $Idx }
            if ($Detail) {
                $sync.Status = $Detail
                Write-WizLog "$($sync.StepNames[$Idx]) - $Detail"
            }
        }

        function Set-Progress {
            param([int]$Pct)
            $sync.Progress = [Math]::Min([Math]::Max($Pct, 0), 100)
        }

        function Refresh-Path {
            $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                        [System.Environment]::GetEnvironmentVariable('PATH', 'User')
        }

        function Get-Python {
            foreach ($cmd in @('python', 'python3', 'py')) {
                try {
                    $v = & $cmd --version 2>&1
                    if ($v -match '3\.(1[0-9]|[2-9]\d)') { return $cmd }
                } catch {}
            }
            return $null
        }

        try {
            Write-WizLog "=== Echo Bloom wizard install started ==="

            $ebDir  = "$env:LOCALAPPDATA\EchoBloom"
            $opener = "$ebDir\open_echo_bloom.ps1"
            $iconIco = "$ebDir\echo-bloom.ico"
            $appDir = "$ebDir\app"
            $sync.Opener = $opener

            # ── Step 0: Python ────────────────────────────────────────────────
            Set-StepStatus 0 'running' 'Looking for Python 3.11+...'
            $python = Get-Python

            if (-not $python) {
                Set-StepStatus 0 'running' 'Installing Python 3.11 via winget...'
                try {
                    & winget install --id Python.Python.3.11 --silent `
                        --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
                    Start-Sleep 5
                    Refresh-Path
                    $python = Get-Python
                } catch { Write-WizLog "winget python: $($_.Exception.Message)" }
            }

            if (-not $python) {
                Set-StepStatus 0 'running' 'Downloading Python 3.11.9 installer...'
                try {
                    $tmp = "$env:TEMP\python-3.11.9-amd64.exe"
                    Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' `
                        -OutFile $tmp -UseBasicParsing
                    Set-StepStatus 0 'running' 'Running Python installer (this takes a moment)...'
                    Start-Process $tmp -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1' -Wait
                    Start-Sleep 5
                    Refresh-Path
                    $python = Get-Python
                } catch { Write-WizLog "python direct install: $($_.Exception.Message)" }
            }

            if (-not $python) {
                Set-StepStatus 0 'warn' 'Python not found.'
                $sync.Failed = $true
                $sync.Error  = 'Python 3.11+ could not be installed. Install it from python.org, then run this installer again.'
                Write-WizLog 'FATAL: no Python'
                return
            }

            Set-StepStatus 0 'done' "Python OK  (command: $python)"
            Set-Progress 15

            # ── Step 1: Ollama ────────────────────────────────────────────────
            Set-StepStatus 1 'running' 'Looking for Ollama...'
            $ollamaOK = [bool](Get-Command ollama -ErrorAction SilentlyContinue)

            if (-not $ollamaOK) {
                Set-StepStatus 1 'running' 'Installing Ollama via winget...'
                try {
                    & winget install --id Ollama.Ollama --silent `
                        --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
                    Start-Sleep 5
                    Refresh-Path
                    $ollamaOK = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
                } catch { Write-WizLog "winget ollama: $($_.Exception.Message)" }
            }

            if (-not $ollamaOK) {
                Set-StepStatus 1 'running' 'Downloading Ollama installer...'
                try {
                    $tmp = "$env:TEMP\OllamaSetup.exe"
                    Invoke-WebRequest 'https://ollama.com/download/OllamaSetup.exe' `
                        -OutFile $tmp -UseBasicParsing
                    Set-StepStatus 1 'running' 'Running Ollama installer...'
                    Start-Process $tmp -ArgumentList '/SILENT' -Wait
                    Start-Sleep 5
                    Refresh-Path
                    $ollamaOK = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
                    if (-not $ollamaOK) {
                        Set-StepStatus 1 'running' 'Waiting for Ollama service to settle...'
                        Start-Sleep 8
                        Refresh-Path
                        $ollamaOK = [bool](Get-Command ollama -ErrorAction SilentlyContinue)
                    }
                } catch { Write-WizLog "ollama direct install: $($_.Exception.Message)" }
            }

            if ($ollamaOK) {
                Set-StepStatus 1 'done' 'Ollama ready.'
            } else {
                Set-StepStatus 1 'warn' 'Ollama not found - install manually from ollama.com if needed.'
            }
            Set-Progress 30

            # ── Step 2: ffmpeg ────────────────────────────────────────────────
            Set-StepStatus 2 'running' 'Looking for ffmpeg...'
            $ffmpegOK = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)

            if (-not $ffmpegOK) {
                Set-StepStatus 2 'running' 'Installing ffmpeg via winget...'
                try {
                    & winget install --id Gyan.FFmpeg --silent `
                        --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
                    Start-Sleep 3
                    Refresh-Path
                    $ffmpegOK = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)
                } catch { Write-WizLog "winget ffmpeg: $($_.Exception.Message)" }
            }

            if ($ffmpegOK) {
                Set-StepStatus 2 'done' 'ffmpeg ready.'
            } else {
                Set-StepStatus 2 'warn' 'ffmpeg not found - voice features will be limited.'
            }
            Set-Progress 42

            # ── Step 3: Echo Bloom app files ──────────────────────────────────
            Set-StepStatus 3 'running' 'Downloading Echo Bloom from GitHub...'
            try {
                $zipUrl = 'https://github.com/EverySynthetic/echo-bloom/archive/refs/heads/main.zip'
                $zipTmp = "$env:TEMP\echo-bloom-main.zip"
                $extTmp = "$env:TEMP\echo-bloom-extract"

                New-Item -ItemType Directory -Force -Path $ebDir | Out-Null

                Invoke-WebRequest $zipUrl -OutFile $zipTmp -UseBasicParsing

                Set-StepStatus 3 'running' 'Extracting app files...'
                if (Test-Path $extTmp) { Remove-Item $extTmp -Recurse -Force }
                Expand-Archive -Path $zipTmp -DestinationPath $extTmp -Force

                $srcDir = Join-Path $extTmp 'echo-bloom-main'
                if (Test-Path $appDir) { Remove-Item $appDir -Recurse -Force }
                Move-Item $srcDir $appDir

                $sync.AppDir = $appDir
                Remove-Item $zipTmp -Force -ErrorAction SilentlyContinue

                Set-StepStatus 3 'done' "Installed to $appDir"
            } catch {
                Set-StepStatus 3 'warn' "App files: $($_.Exception.Message)"
                $sync.Failed = $true
                $sync.Error  = "Could not install app files: $($_.Exception.Message)"
                Write-WizLog "FATAL: app files - $($_.Exception.Message)"
                return
            }
            Set-Progress 58

            # ── Step 4: Python packages ───────────────────────────────────────
            Set-StepStatus 4 'running' 'Installing Python packages...'
            $pkgs   = @('fastapi','uvicorn[standard]','aiohttp','jinja2','python-multipart',
                        'bcrypt','cryptography','faster-whisper','qdrant-client','psutil')
            $failed = @()

            foreach ($pkg in $pkgs) {
                Set-StepStatus 4 'running' "pip install $pkg..."
                try {
                    & $python -m pip install $pkg --quiet --disable-pip-version-check 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { $failed += $pkg }
                } catch {
                    $failed += $pkg
                }
            }

            if ($failed.Count -gt 0) {
                Set-StepStatus 4 'warn' "Finished (failed: $($failed -join ', '))"
                Write-WizLog "pip failures: $($failed -join ', ')"
            } else {
                Set-StepStatus 4 'done' 'All packages installed.'
            }
            Set-Progress 78

            # ── Step 5: Scheduled task + shortcut ─────────────────────────────
            Set-StepStatus 5 'running' 'Setting up launcher...'
            try {
                New-Item -ItemType Directory -Force -Path $ebDir | Out-Null

                Set-StepStatus 5 'running' 'Downloading browser opener...'
                try {
                    Invoke-WebRequest `
                        'https://raw.githubusercontent.com/EverySynthetic/echo-bloom/main/open_echo_bloom.ps1' `
                        -OutFile $opener -UseBasicParsing
                } catch {
                    [System.IO.File]::WriteAllText($opener, "Start-Process 'http://localhost:8090'",
                        (New-Object System.Text.UTF8Encoding($false)))
                }

                $pyFull = try { (Get-Command $python -ErrorAction Stop).Source } catch { $python }

                Set-StepStatus 5 'running' 'Registering scheduled task...'
                $action    = New-ScheduledTaskAction `
                                -Execute $pyFull `
                                -Argument '-m uvicorn main:app --host 0.0.0.0 --port 8090' `
                                -WorkingDirectory $appDir
                $trigger   = New-ScheduledTaskTrigger -AtLogOn
                $settings  = New-ScheduledTaskSettingsSet `
                                -AllowStartIfOnBatteries `
                                -DontStopIfGoingOnBatteries `
                                -ExecutionTimeLimit 0 `
                                -RestartInterval (New-TimeSpan -Minutes 2) `
                                -RestartCount 5
                $principal = New-ScheduledTaskPrincipal `
                                -UserId $env:USERNAME `
                                -LogonType Interactive `
                                -RunLevel Highest

                Unregister-ScheduledTask -TaskName 'EchoBloom' -Confirm:$false -ErrorAction SilentlyContinue
                Register-ScheduledTask `
                    -TaskName  'EchoBloom' `
                    -Action    $action `
                    -Trigger   $trigger `
                    -Settings  $settings `
                    -Principal $principal | Out-Null

                Start-ScheduledTask -TaskName 'EchoBloom' -ErrorAction SilentlyContinue

                Set-StepStatus 5 'running' 'Converting icon...'
                $iconSrc = Join-Path $appDir 'static\icons\icon-512.png'
                if (Test-Path $iconSrc) {
                    try {
                        Add-Type -AssemblyName System.Drawing
                        $bmp  = [System.Drawing.Bitmap]::new($iconSrc)
                        $hico = $bmp.GetHicon()
                        $ico  = [System.Drawing.Icon]::FromHandle($hico)
                        $fs   = [System.IO.FileStream]::new($iconIco, [System.IO.FileMode]::Create)
                        $ico.Save($fs)
                        $fs.Close()
                        $ico.Dispose()
                        $bmp.Dispose()
                    } catch {
                        $iconIco = ''
                        Write-WizLog "icon conversion skipped: $($_.Exception.Message)"
                    }
                }

                Set-StepStatus 5 'running' 'Creating Start Menu shortcut...'
                $smDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
                $wsh   = New-Object -ComObject WScript.Shell
                $lnk   = $wsh.CreateShortcut("$smDir\Echo Bloom.lnk")
                $lnk.TargetPath       = 'powershell.exe'
                $lnk.Arguments        = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$opener`""
                $lnk.WorkingDirectory = $ebDir
                $lnk.Description      = 'Echo Bloom  -  Local AI Lifecycle Manager'
                if ($iconIco -and (Test-Path $iconIco)) {
                    $lnk.IconLocation = "$iconIco,0"
                }
                $lnk.Save()

                Set-StepStatus 5 'done' 'Scheduled task registered. Shortcut created.'
            } catch {
                Set-StepStatus 5 'warn' "Setup: $($_.Exception.Message)"
                Write-WizLog "step 5 warning: $($_.Exception.Message)"
            }

            Set-Progress 100
            $sync.Status = 'Installation complete.'
            Write-WizLog '=== install complete ==='
            $sync.Done = $true
        }
        catch {
            $sync.Failed = $true
            $sync.Error  = $_.Exception.Message
            Write-WizLog "FATAL: $($_.Exception.Message)"
            Write-WizLog $_.ScriptStackTrace
        }
    }

    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = 'STA'
    $rs.ThreadOptions  = 'ReuseThread'
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('sync', $sync)

    $script:psInstall = [powershell]::Create()
    $script:psInstall.Runspace = $rs
    $null = $script:psInstall.AddScript($worker)
    $script:psHandle = $script:psInstall.BeginInvoke()

    $script:uiTimer.Start()
}

# ─────────────────────────────────────────────────────────────────────────────
# Launch
# ─────────────────────────────────────────────────────────────────────────────
$form.Add_FormClosing({
    try { if ($script:uiTimer)     { $script:uiTimer.Stop() } }     catch {}
    try { if ($script:launchTimer) { $script:launchTimer.Stop() } } catch {}
    try { if ($script:psInstall)   { $script:psInstall.Dispose() } } catch {}
})

Show-Page 1
[System.Windows.Forms.Application]::Run($form)
