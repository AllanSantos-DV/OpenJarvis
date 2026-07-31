<#
.SYNOPSIS
    Launch the Jarvis conductor, or install a desktop shortcut for it.

.DESCRIPTION
    Activation is a deliberate act: a click, never an always-on microphone.

    The script probes the few things that silently produce a broken conductor
    and refuses to start when one of them is wrong, instead of failing later in
    a way that looks like "the agent ignored me":

      * the interpreter is the project's own venv, resolved by absolute path.
        The machine's default Python is outside the range OpenJarvis supports,
        so relying on PATH would pick the wrong one;
      * the Copilot CLI is installed and a subscription token is present,
        because the CLI authenticates from the ambient token;
      * only one conductor runs at a time -- the lock itself is enforced by the
        process, this is just an early, readable message.

    A missing voice engine is NOT fatal: orchestration still works, the results
    are only logged instead of spoken.

.EXAMPLE
    .\jarvis-conductor.ps1
    Conducts: answers trivial turns inside the default scope, queues the rest.

.EXAMPLE
    .\jarvis-conductor.ps1 -ReportOnly
    Shows which sessions would be resumed without sending anything.

.EXAMPLE
    .\jarvis-conductor.ps1 -InstallShortcut
    Creates "Jarvis Conductor" on the desktop.
#>
[CmdletBinding()]
param(
    [double]$IdleMinutes = 10,
    [int]$Limit = 10,
    [double]$Interval = 0,
    [string[]]$AllowedRoot = @(),
    # Observe without answering. `-DryRun` is kept as an alias because the
    # earlier name is in the owner's muscle memory.
    [Alias('DryRun')]
    [switch]$ReportOnly,
    [switch]$Quiet,
    [switch]$InstallShortcut,
    # Print the exact runner invocation and exit, so "what will this shortcut
    # do?" has an answer that does not require running it.
    [switch]$PrintPlan,
    [string]$LogLevel = 'INFO'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'

function Fail([string]$message) {
    Write-Host "jarvis-conductor: $message" -ForegroundColor Red
    exit 2
}

if ($InstallShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $linkPath = Join-Path $desktop 'Jarvis Conductor.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($linkPath)
    $shortcut.TargetPath = 'powershell.exe'
    # -NoExit keeps the window open so the report stays readable after a click.
    $shortcut.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.Description = 'Watch idle Copilot sessions and keep them moving'
    $shortcut.Save()
    Write-Host "jarvis-conductor: shortcut created at $linkPath" -ForegroundColor Green
    exit 0
}

# --- what this invocation will do ------------------------------------------
# Built before the probes so `-PrintPlan` can answer "what does this shortcut
# do?" on any machine, without a token or the CLI installed.
$arguments = @(
    '-m', 'openjarvis.conductor.runner',
    '--idle-minutes', $IdleMinutes,
    '--limit', $Limit,
    '--interval', $Interval,
    '--log-level', $LogLevel
)
# The shortcut carries no arguments, and a click on an icon named "Jarvis
# Conductor" IS the consent to conduct. Forcing report-mode there would not be
# a safety measure, it would be shipping a product that never does its job.
#
# What the default DOES restrain is scope and reach:
#   * scope -- without -AllowedRoot, only sessions under the projects folder
#     that holds this checkout. Scoping to the checkout itself would be tighter
#     and useless: the sessions worth conducting are the sibling projects;
#   * reach -- the tier gate still holds. Trivial turns are answered; anything
#     sensitive is queued for the owner's approval and announced out loud.
if (-not $AllowedRoot -or $AllowedRoot.Count -eq 0) {
    $AllowedRoot = @(Split-Path -Parent (Split-Path -Parent $repoRoot))
}
foreach ($root in $AllowedRoot) { $arguments += @('--allowed-root', $root) }
if ($ReportOnly) { $arguments += '--dry-run' }

if ($PrintPlan) {
    Write-Output "$python $($arguments -join ' ')"
    exit 0
}

$scope = $AllowedRoot -join ', '
if ($ReportOnly) {
    Write-Host 'jarvis-conductor: modo relatorio -- nada sera respondido.' -ForegroundColor Yellow
} else {
    Write-Host "jarvis-conductor: conduzindo em $scope. Trivial responde; o resto pede sua aprovacao." -ForegroundColor Green
    Write-Host 'jarvis-conductor: use -ReportOnly para so observar.' -ForegroundColor DarkGray
}

# --- probe: interpreter -----------------------------------------------------
if (-not (Test-Path $python)) {
    Fail "venv not found at $python. Create it with: uv venv --python 3.12"
}
$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($version -notin @('3.10','3.11','3.12','3.13')) {
    Fail "venv runs Python $version, outside the supported range >=3.10,<3.14."
}

# --- probe: Copilot CLI + credential ---------------------------------------
if (-not (Get-Command copilot -ErrorAction SilentlyContinue)) {
    Fail 'Copilot CLI not on PATH. Install it with: npm install -g @github/copilot'
}
if (-not $env:GH_TOKEN -and -not $env:GITHUB_TOKEN) {
    Fail 'No GH_TOKEN or GITHUB_TOKEN in this shell. The CLI needs the subscription token.'
}

# --- probe: singleton (early, readable message) -----------------------------
$lockFile = Join-Path $env:USERPROFILE '.jarvis-conductor\conductor.lock'
if (Test-Path $lockFile) {
    $ownerPid = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($ownerPid -and (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue)) {
        Fail "Another conductor is already running (pid $ownerPid)."
    }
    Write-Host 'jarvis-conductor: clearing a lock left by a crashed run.' -ForegroundColor Yellow
}

# --- probe: voice (degrade, never block) ------------------------------------
if (-not $Quiet) {
    $voxAlive = & $python -c "try:`n    from openjarvis.speech.vox_engine import VoxEngineTTSBackend`n    print(1 if VoxEngineTTSBackend().health() else 0)`nexcept Exception:`n    print(0)"
    if ($voxAlive.Trim() -ne '1') {
        Write-Host 'jarvis-conductor: vox-engine offline, results will be logged instead of spoken.' -ForegroundColor Yellow
        $Quiet = $true
    }
}
if ($Quiet) { $arguments += '--quiet' }

Push-Location $repoRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
