<#
.SYNOPSIS
    Sobe o Jarvis: o servidor por baixo e a janela por cima.

.DESCRIPTION
    Um clique. O app desktop precisa do backend rodando -- abrir só a janela dá
    uma interface bonita e morta, que foi exatamente o que aconteceu quando o
    unico atalho na area de trabalho era o do CONDUCTOR (um script de tick, que
    abre um terminal e faz outra coisa).

    Ordem importa: o servidor primeiro, e a janela so depois que ele responde.
    Um app que abre antes do backend mostra erro de conexao na primeira tela.

.EXAMPLE
    .\jarvis.ps1
    Sobe o que faltar e abre a janela.

.EXAMPLE
    .\jarvis.ps1 -InstallShortcut
    Cria o atalho "Jarvis" na area de trabalho.

.EXAMPLE
    .\jarvis.ps1 -Stop
    Fecha a janela e o servidor.

.EXAMPLE
    .\jarvis.ps1 -Watch
    Sobe e FICA vigiando: se o servidor cair, religa. Sem isto, um backend que
    morre depois do boot deixa a janela viva e inutil -- ela nao sabe que ficou
    sozinha.
#>
[CmdletBinding()]
param(
    [switch]$InstallShortcut,
    [switch]$Stop,
    [switch]$NoWindow,
    [switch]$Watch,
    # Set by the shortcut and the logon task, which run with no visible console.
    # A failure there has to reach the screen some other way.
    [switch]$Hidden
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$app = Join-Path $repoRoot 'frontend\src-tauri\target\release\openjarvis-desktop.exe'
$url = 'http://127.0.0.1:8000'

function Fail([string]$message) {
    Write-Host "jarvis: $message" -ForegroundColor Red
    # The shortcut runs this with -WindowStyle Hidden, so there is no console to
    # read: a failure would vanish and the owner would be left clicking an icon
    # that does nothing, with no idea why. Never fail silently -- if nobody can
    # see the console, put it on screen.
    if ($Hidden) {
        try {
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
            [System.Windows.Forms.MessageBox]::Show(
                "$message`n`nLog: $env:USERPROFILE\.openjarvis\server.log",
                'Jarvis nao subiu',
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            ) | Out-Null
        } catch {
            # Even the dialog can fail. Leave a file the owner can find.
            $note = Join-Path $env:USERPROFILE 'jarvis-nao-subiu.txt'
            "$([DateTime]::Now)  $message" | Set-Content $note -Encoding utf8
        }
    }
    exit 2
}

function Test-Port {
    # Cheap pre-check: is anything listening at all? Answers in microseconds
    # when nothing is there, so the wait loop does not pay for an HTTP timeout
    # on every attempt during the ~12s cold start.
    $probe = New-Object System.Net.Sockets.TcpClient
    try {
        $probe.Connect('127.0.0.1', 8000)
        return $probe.Connected
    } catch {
        return $false
    } finally {
        $probe.Close()
    }
}

function Test-Server {
    # An open port is not a ready app: uvicorn binds before the application
    # finishes starting, so a window opened on "port is up" can still meet a
    # backend that is not answering. The question is whether /health returns.
    #
    # [System.Net.WebRequest], not Invoke-WebRequest. Measured, after the
    # launcher declared a live server dead while its log showed six 200s:
    # Invoke-WebRequest throws NullReferenceException under `powershell -File`
    # with no profile -- which is how the shortcut runs it -- because it parses
    # the body for the legacy HTML DOM. It works interactively, which is what
    # makes it a trap. [System.Net.Http.HttpClient] is not loaded in Windows
    # PowerShell 5.1, so that was not the way out either.
    if (-not (Test-Port)) { return $false }
    try {
        $request = [System.Net.WebRequest]::Create("$url/health")
        $request.Timeout = 3000
        $response = $request.GetResponse()
        $code = [int]$response.StatusCode
        $response.Close()
        return ($code -ge 200 -and $code -lt 300)
    } catch {
        return $false
    }
}

function Wait-Server([int]$Seconds = 90) {
    # One probe is not readiness: the server answers seconds after the process
    # starts. Without waiting, the window opens onto a backend that is not there
    # yet and greets the owner with a connection error.
    #
    # Measured on this machine: start -> /health OK takes about 12s cold. The
    # budget is generous because the cost of waiting is a few seconds, while the
    # cost of giving up early is a dead window and an owner who kills the app.
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Server) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

if ($InstallShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $link = Join-Path $desktop 'Jarvis.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = 'powershell.exe'
    # -WindowStyle Hidden: the launcher has nothing to show. The Jarvis window is
    # the product; a console sitting behind it is noise.
    $shortcut.Arguments =
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Watch -Hidden"
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.Description = 'Abre o Jarvis'
    if (Test-Path $app) { $shortcut.IconLocation = $app }
    $shortcut.Save()
    Write-Host "jarvis: atalho criado em $link" -ForegroundColor Green

    # The conductor's shortcut sat right next to this one, called "Jarvis
    # Conductor" -- and it is not Jarvis, it is a session-watching script that
    # opens a terminal. The owner clicked it expecting the app and got a CMD
    # window. Two icons whose names differ by one word is a trap; rename it to
    # say what it does.
    $old = Join-Path $desktop 'Jarvis Conductor.lnk'
    if (Test-Path $old) {
        $renamed = Join-Path $desktop 'Vigia de Sessoes (conductor).lnk'
        Move-Item $old $renamed -Force
        Write-Host "jarvis: '$old' renomeado -- nao e o Jarvis." -ForegroundColor Yellow
    }
    exit 0
}

if ($Stop) {
    Get-Process -Name 'openjarvis-desktop' -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.Id -Force }
    & $python -m openjarvis.cli stop 2>&1 | Out-Null
    Write-Host 'jarvis: encerrado.' -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $python)) {
    Fail "venv nao encontrado em $python. Crie com: uv venv --python 3.12"
}

function Start-Server {
    Push-Location $repoRoot
    try {
        & $python -m openjarvis.cli start
        if ($LASTEXITCODE -ne 0) { return $false }
    } finally {
        Pop-Location
    }
    return (Wait-Server)
}

# --- o servidor -------------------------------------------------------------
if (Test-Server) {
    Write-Host 'jarvis: servidor ja estava de pe.' -ForegroundColor DarkGray
} else {
    # A visible sign that something IS happening. The owner killed the app twice
    # waiting on a silent start -- an impatient click is a symptom of a launcher
    # that says nothing, not of an impatient owner.
    Write-Host ''
    Write-Host '  Jarvis subindo...' -ForegroundColor Cyan
    Write-Host '  (o servidor leva alguns segundos; a janela abre sozinha)' -ForegroundColor DarkGray
    if (-not (Start-Server)) {
        Fail "o servidor nao subiu ou nao respondeu em $url. Veja o log."
    }
    Write-Host '  servidor pronto.' -ForegroundColor Green
}

# --- a janela ---------------------------------------------------------------
if ($NoWindow) {
    Write-Host "jarvis: pronto em $url" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $app)) {
    Write-Host 'jarvis: o app ainda nao foi compilado.' -ForegroundColor Yellow
    Write-Host '  Compile com: cd frontend; npx tauri build' -ForegroundColor DarkGray
    Write-Host "  Enquanto isso o backend responde em $url" -ForegroundColor DarkGray
    exit 0
}

$running = Get-Process -Name 'openjarvis-desktop' -ErrorAction SilentlyContinue
if ($running) {
    Write-Host 'jarvis: a janela ja estava aberta.' -ForegroundColor DarkGray
} else {
    Start-Process $app -WorkingDirectory $repoRoot
    Write-Host 'jarvis: janela aberta.' -ForegroundColor Green
}

# --- vigia ------------------------------------------------------------------
if (-not $Watch) { exit 0 }

# A backend that dies AFTER boot leaves the window alive and useless -- it has
# no way to know it is alone, so the owner gets a pretty, dead interface and no
# explanation. Watching costs one socket probe every few seconds.
#
# It follows the window: when the owner closes Jarvis, the watch ends with it.
# A supervisor that outlives the thing it supervises is a process to hunt down
# later.
Write-Host ''
Write-Host '  vigiando o servidor (Ctrl+C encerra o vigia, nao o Jarvis).' -ForegroundColor DarkGray

$failures = 0
while ($true) {
    Start-Sleep -Seconds 5

    if (-not (Get-Process -Name 'openjarvis-desktop' -ErrorAction SilentlyContinue)) {
        Write-Host '  janela fechada -- encerrando o vigia.' -ForegroundColor DarkGray
        exit 0
    }

    if (Test-Server) { $failures = 0; continue }

    # Two strikes: a single miss can be a restart in flight or a busy moment,
    # and restarting a server that is merely slow makes things worse.
    $failures++
    if ($failures -lt 2) { continue }

    Write-Host ''
    Write-Host '  servidor caiu -- religando...' -ForegroundColor Yellow
    if (Start-Server) {
        Write-Host '  servidor de volta.' -ForegroundColor Green
        $failures = 0
    } else {
        Write-Host '  nao consegui religar. Veja o log:' -ForegroundColor Red
        Write-Host "    $env:USERPROFILE\.openjarvis\server.log" -ForegroundColor DarkGray
        exit 2
    }
}
