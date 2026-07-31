<#
.SYNOPSIS
    Make Jarvis come back on its own after a reboot.

.DESCRIPTION
    Registers ONE logon task in Task Scheduler -- no Windows service wrapper,
    no admin rights, nothing to maintain.

    The task runs the same launcher a desktop click runs, so there is exactly
    one path that brings Jarvis up. An earlier version registered the server and
    the window as two independent tasks, which is two ways to come up half-alive:
    a window whose backend is not ready shows a connection error on its first
    screen.

    Task Scheduler rather than a service because a service runs in session 0 and
    cannot show a window: the desktop app would start and be invisible. It also
    means this never needs elevation.

    Registering twice replaces, never duplicates.

.EXAMPLE
    .\jarvis-autostart.ps1
    Registers the task and starts Jarvis now.

.EXAMPLE
    .\jarvis-autostart.ps1 -Remove
    Unregisters it.
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'jarvis.ps1'

$TASK = 'Jarvis'
# The old layout registered the server and the window as two independent tasks.
# That is two ways to be half-up: a window with no backend shows a connection
# error on its first screen, and a backend with no window is invisible. One task
# runs the launcher, which orders them and waits for the server to answer.
$LEGACY = @('Jarvis Server')

function Fail([string]$message) {
    Write-Host "jarvis-autostart: $message" -ForegroundColor Red
    exit 2
}

if ($Remove) {
    foreach ($name in @($TASK) + $LEGACY) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "removida: $name" -ForegroundColor Yellow
        }
    }
    exit 0
}

if (-not (Test-Path $launcher)) {
    Fail "launcher nao encontrado em $launcher"
}

# Drop the old split tasks so they do not race the single one.
foreach ($name in $LEGACY) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removida a tarefa antiga: $name" -ForegroundColor DarkGray
    }
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Defaults built for laptops would stop Jarvis on battery and never start it
# while unplugged -- on a desktop that is simply a service that does not run.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -Watch" `
    -WorkingDirectory $repoRoot

Register-ScheduledTask `
    -TaskName $TASK `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -Description 'Sobe o Jarvis: servidor e janela' `
    -Force | Out-Null
Write-Host "registrada: $TASK" -ForegroundColor Green

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TASK
    Write-Host 'jarvis-autostart: iniciado agora tambem.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'O Jarvis volta sozinho no proximo logon.' -ForegroundColor Cyan
Write-Host '  conferir : Get-ScheduledTask -TaskName "Jarvis"' -ForegroundColor DarkGray
Write-Host '  desfazer : .\jarvis-autostart.ps1 -Remove' -ForegroundColor DarkGray
