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
$hooksDir = Join-Path $repoRoot '.githooks'

if ($Remove) {
    git -C $repoRoot config --unset core.hooksPath 2>&1 | Out-Null
    Write-Host 'core.hooksPath local removido (volta ao global da maquina).' -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path (Join-Path $hooksDir 'pre-push.cjs'))) {
    Write-Host "hooks nao encontrados em $hooksDir" -ForegroundColor Red
    exit 2
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host 'node nao esta no PATH -- os hooks precisam dele.' -ForegroundColor Red
    exit 2
}

# Point Git at the repo's own hooks directory.
#
# Writing to .git/hooks would be useless here: this machine sets core.hooksPath
# GLOBALLY, so Git ignores .git/hooks entirely. The global dispatcher tries to
# delegate to the local hook, but on Windows it spawns the shell script directly
# and gets ENOENT -- status comes back null, `null ?? 0` becomes exit 0, and the
# local gate silently never runs. Measured, after shipping a gate that could
# never fire.
#
# Taking over loses nothing: .githooks/pre-push.cjs runs the machine-wide
# dispatcher first and honours its veto.
git -C $repoRoot config core.hooksPath '.githooks'

Write-Host "core.hooksPath -> .githooks" -ForegroundColor Green
Write-Host '  Antes de cada push que toque a camada do Jarvis, os contract tests' -ForegroundColor DarkGray
Write-Host '  rodam contra o CLI real. As regras globais da maquina continuam valendo.' -ForegroundColor DarkGray
Write-Host '  Pular: git push --no-verify' -ForegroundColor DarkGray
