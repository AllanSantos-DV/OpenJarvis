<#
.SYNOPSIS
    Make Jarvis come back on its own after a reboot.

.DESCRIPTION
    Registers two logon tasks in Task Scheduler -- no Windows service wrapper,
    no admin rights, nothing to maintain:

      * the API server (the brain's backend), started headless;
      * the desktop app, which is the window the owner actually talks to.

    Task Scheduler rather than a service because a service runs in session 0 and
    cannot show a window: the desktop app would start and be invisible. It also
    means this never needs elevation.

    Both tasks are idempotent -- registering twice replaces, never duplicates.

.EXAMPLE
    .\jarvis-autostart.ps1
    Registers both tasks and starts them now.

.EXAMPLE
    .\jarvis-autostart.ps1 -ServerOnly
    Only the backend (for a machine where the window is not wanted).

.EXAMPLE
    .\jarvis-autostart.ps1 -Remove
    Unregisters both.
#>
[CmdletBinding()]
param(
    [switch]$ServerOnly,
    [switch]$Remove,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
$app = Join-Path $repoRoot 'frontend\src-tauri\target\release\openjarvis-desktop.exe'

$SERVER_TASK = 'Jarvis Server'
$APP_TASK = 'Jarvis'

function Fail([string]$message) {
    Write-Host "jarvis-autostart: $message" -ForegroundColor Red
    exit 2
}

if ($Remove) {
    foreach ($name in @($SERVER_TASK, $APP_TASK)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "removida: $name" -ForegroundColor Yellow
        }
    }
    exit 0
}

# pythonw.exe, not python.exe: the console variant flashes a window on every
# logon and leaves one sitting in the taskbar.
if (-not (Test-Path $python)) {
    Fail "interpretador nao encontrado em $python. Crie o venv com: uv venv --python 3.12"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Defaults built for laptops would stop Jarvis on battery and never start it
# while unplugged -- on a desktop that is simply a service that does not run.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$serverAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument '-m openjarvis.cli serve' `
    -WorkingDirectory $repoRoot

Register-ScheduledTask `
    -TaskName $SERVER_TASK `
    -Trigger $trigger `
    -Action $serverAction `
    -Settings $settings `
    -Description 'Backend do Jarvis (API local)' `
    -Force | Out-Null
Write-Host "registrada: $SERVER_TASK" -ForegroundColor Green

if (-not $ServerOnly) {
    if (-not (Test-Path $app)) {
        Write-Host "jarvis-autostart: o app ainda nao foi compilado ($app)." -ForegroundColor Yellow
        Write-Host "  Compile com: cd frontend; npx tauri build" -ForegroundColor DarkGray
    } else {
        $appAction = New-ScheduledTaskAction -Execute $app -WorkingDirectory $repoRoot
        Register-ScheduledTask `
            -TaskName $APP_TASK `
            -Trigger $trigger `
            -Action $appAction `
            -Settings $settings `
            -Description 'A janela do Jarvis' `
            -Force | Out-Null
        Write-Host "registrada: $APP_TASK" -ForegroundColor Green
    }
}

if (-not $NoStart) {
    foreach ($name in @($SERVER_TASK, $APP_TASK)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $name
        }
    }
    Write-Host 'jarvis-autostart: iniciado agora tambem.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'O Jarvis volta sozinho no proximo logon.' -ForegroundColor Cyan
Write-Host '  conferir : Get-ScheduledTask -TaskName "Jarvis*"' -ForegroundColor DarkGray
Write-Host '  desfazer : .\jarvis-autostart.ps1 -Remove' -ForegroundColor DarkGray
