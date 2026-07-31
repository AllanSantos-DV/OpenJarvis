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
    try {
        Invoke-WebRequest "$url/docs" -TimeoutSec 3 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        # Any HTTP answer means it is serving; only a refused connection is down.
        return ($_.Exception.Response -ne $null)
    }
}

if ($InstallShortcut) {
    $link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk'
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
    Write-Host 'jarvis: subindo o servidor...' -ForegroundColor Cyan
    Push-Location $repoRoot
    try {
        & $python -m openjarvis.cli start
        if ($LASTEXITCODE -ne 0) {
            Fail 'o servidor nao subiu (a saida acima diz por que).'
        }
    } finally {
        Pop-Location
    }
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
