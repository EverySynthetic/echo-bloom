$url  = 'http://localhost:8090'
$task = 'EchoBloom'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Test-AppUp {
    try {
        $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return $r.StatusCode -lt 500
    } catch { return $false }
}

# Already running — open browser and exit quietly
if (Test-AppUp) { Start-Process $url; exit }

# Try to start the scheduled task
Start-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue

# ── UI ────────────────────────────────────────────────────────────────────────
$form                  = New-Object System.Windows.Forms.Form
$form.Text             = 'Echo Bloom'
$form.Size             = New-Object System.Drawing.Size(380, 145)
$form.StartPosition    = 'CenterScreen'
$form.FormBorderStyle  = 'FixedSingle'
$form.MaximizeBox      = $false
$form.BackColor        = [System.Drawing.Color]::FromArgb(15, 15, 18)

$title                 = New-Object System.Windows.Forms.Label
$title.Text            = 'ECHO BLOOM'
$title.ForeColor       = [System.Drawing.Color]::FromArgb(180, 180, 190)
$title.Font            = New-Object System.Drawing.Font('Consolas', 13, [System.Drawing.FontStyle]::Bold)
$title.AutoSize        = $false
$title.Size            = New-Object System.Drawing.Size(360, 28)
$title.TextAlign       = 'MiddleCenter'
$title.Location        = New-Object System.Drawing.Point(10, 12)
$form.Controls.Add($title)

$bar                        = New-Object System.Windows.Forms.ProgressBar
$bar.Style                  = 'Marquee'
$bar.MarqueeAnimationSpeed  = 25
$bar.Location               = New-Object System.Drawing.Point(20, 53)
$bar.Size                   = New-Object System.Drawing.Size(340, 14)
$bar.ForeColor              = [System.Drawing.Color]::FromArgb(100, 220, 140)
$form.Controls.Add($bar)

$sub                = New-Object System.Windows.Forms.Label
$sub.Text           = 'Starting local AI server...'
$sub.ForeColor      = [System.Drawing.Color]::FromArgb(90, 90, 100)
$sub.Font           = New-Object System.Drawing.Font('Consolas', 8)
$sub.AutoSize       = $false
$sub.Size           = New-Object System.Drawing.Size(360, 18)
$sub.TextAlign      = 'MiddleCenter'
$sub.Location       = New-Object System.Drawing.Point(10, 78)
$form.Controls.Add($sub)

$hint               = New-Object System.Windows.Forms.Label
$hint.Text          = 'Your browser will open automatically when ready.'
$hint.ForeColor     = [System.Drawing.Color]::FromArgb(60, 60, 70)
$hint.Font          = New-Object System.Drawing.Font('Consolas', 7)
$hint.AutoSize      = $false
$hint.Size          = New-Object System.Drawing.Size(360, 16)
$hint.TextAlign     = 'MiddleCenter'
$hint.Location      = New-Object System.Drawing.Point(10, 100)
$form.Controls.Add($hint)

# ── Poll timer ────────────────────────────────────────────────────────────────
$script:count = 0
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000

# Launch the browser BEFORE closing the form. The old order closed the form
# first, and any failure in Start-Process left the user staring at nothing —
# panel gone, no browser, no error. Now a failure keeps the panel open and
# names the problem.
function Open-Browser {
    try {
        Start-Process $url -ErrorAction Stop
        return $true
    } catch {
        try {
            Start-Process explorer.exe $url -ErrorAction Stop
            return $true
        } catch {
            $sub.Text  = "Could not open a browser: $($_.Exception.Message)"
            $hint.Text = "Open $url yourself — the server is running."
            $hint.ForeColor = [System.Drawing.Color]::FromArgb(220, 180, 100)
            return $false
        }
    }
}

$timer.Add_Tick({
    $script:count++
    $sub.Text = "Starting local AI server... ($($script:count)s)"

    if (Test-AppUp) {
        $timer.Stop()
        if (Open-Browser) { $form.Close() }
        return
    }

    if ($script:count -ge 45) {
        $timer.Stop()
        $sub.Text = 'Taking longer than expected — opening anyway...'
        Start-Sleep -Milliseconds 1200
        if (Open-Browser) { $form.Close() }
    }
})

$form.Add_Shown({ $timer.Start() })
[System.Windows.Forms.Application]::Run($form)
