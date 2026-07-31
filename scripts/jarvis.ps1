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
#>
[CmdletBinding()]
param(
    [switch]$InstallShortcut,
    [switch]$Stop,
    [switch]$NoWindow
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$app = Join-Path $repoRoot 'frontend\src-tauri\target\release\openjarvis-desktop.exe'
$url = 'http://127.0.0.1:8000'

function Fail([string]$message) {
    Write-Host "jarvis: $message" -ForegroundColor Red
    exit 2
}

function Test-Server {
    # /health, not /docs: the docs UI can be turned off in a hardened setup, and
    # then the launcher would report "offline" with a perfectly live backend.
    #
    # And a raw TcpClient, not Invoke-WebRequest. Measured twice, after the
    # launcher reported the server down while its log showed six 200s:
    #   * Invoke-WebRequest throws NullReferenceException under
    #     `powershell -File` (no profile, which is how the shortcut runs it) --
    #     it parses the body for the legacy HTML DOM and falls over on a
    #     perfectly good response. It works interactively, which is what makes
    #     it a trap.
    #   * [System.Net.Http.HttpClient] is not loaded in Windows PowerShell 5.1,
    #     so reaching for it just moved the failure.
    #
    # The question is only "is something listening and accepting". A socket
    # answers exactly that, with nothing in between to misbehave.
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
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
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
    Push-Location $repoRoot
    try {
        & $python -m openjarvis.cli start
        if ($LASTEXITCODE -ne 0) {
            Fail 'o servidor nao subiu (a saida acima diz por que).'
        }
    } finally {
        Pop-Location
    }
    if (-not (Wait-Server)) {
        Fail "o servidor subiu mas nao respondeu em $url. Veja o log."
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
