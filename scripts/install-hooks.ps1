<#
.SYNOPSIS
    Install the pre-push gate that runs the checks CI cannot.

.DESCRIPTION
    The contract tests exercise the real Copilot CLI, the owner's mcp-bridge and
    his subscription. A hosted runner has none of those, so those tests never run
    in CI -- a green pipeline is silent about exactly the failures that have
    shipped here more than once.

    This wires a git pre-push hook that runs them on the machine that has
    everything, right before the work leaves it. Skipped automatically when the
    push does not touch the Jarvis layer, so a docs commit costs nothing.

.EXAMPLE
    .\install-hooks.ps1
    .\install-hooks.ps1 -Remove
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$hookPath = Join-Path $repoRoot '.git\hooks\pre-push'
$runner = Join-Path $PSScriptRoot 'hooks\pre-push.cjs'

if ($Remove) {
    if (Test-Path $hookPath) {
        Remove-Item $hookPath -Force
        Write-Host 'pre-push removido.' -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    Write-Host "hook nao encontrado em $runner" -ForegroundColor Red
    exit 2
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host 'node nao esta no PATH -- o hook precisa dele.' -ForegroundColor Red
    exit 2
}

New-Item -ItemType Directory -Path (Split-Path $hookPath) -Force | Out-Null
# Git runs hooks through sh even on Windows, so the shim is a shell script that
# hands over to node. Forward every argument: git passes the remote and URL.
@"
#!/bin/sh
exec node "$($runner -replace '\\', '/')" "`$@"
"@ | Set-Content $hookPath -NoNewline -Encoding utf8

Write-Host "pre-push instalado em $hookPath" -ForegroundColor Green
Write-Host '  Antes de cada push que toque a camada do Jarvis, os contract tests' -ForegroundColor DarkGray
Write-Host '  rodam contra o CLI real. Pule com: git push --no-verify' -ForegroundColor DarkGray
