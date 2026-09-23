<#
.SYNOPSIS
Move this checkout from `jev-ultrafast` to `AgentJevUltraFast` and update the
MCP registrations and skills that point at the old folder name.

.DESCRIPTION
The Python package and product name are already `AgentJevUltraFast`; only the
folder is still `jev-ultrafast`. Windows refuses to rename a folder that is the
working directory of a running process, so close opencode, Codex, Claude Code,
Command Code and every terminal that sits inside the repository first, then run
this script from somewhere else (for example `C:\Users\aksha`).

The data directory `~/.jev-ultrafast/` (modes, profile) is deliberately NOT
renamed: it lives outside the checkout and keeps working after the move.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\migrate_folder.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\migrate_folder.ps1
#>
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ((Split-Path -Leaf $source) -ne "jev-ultrafast") {
    throw "This script expects the folder 'jev-ultrafast'; found '$(Split-Path -Leaf $source)'."
}
$parent = Split-Path -Parent $source
$target = Join-Path $parent "AgentJevUltraFast"
if (Test-Path -LiteralPath $target) { throw "Already exists: $target" }

# Only the checkout path is rewritten. `~/.jev-ultrafast` (the data directory) is a
# different string and is intentionally left alone.
$edits = @(
    (Join-Path $env:USERPROFILE ".config\opencode\opencode.jsonc"),
    (Join-Path $env:USERPROFILE ".codex\config.toml"),
    (Join-Path $env:USERPROFILE ".claude.json"),
    (Join-Path $env:USERPROFILE ".agents\skills\jev-browser\SKILL.md"),
    (Join-Path $env:USERPROFILE ".codex\skills\jev-browser\SKILL.md"),
    (Join-Path $env:USERPROFILE ".codex\skills\jev-knowledge\SKILL.md"),
    (Join-Path $env:USERPROFILE ".commandcode\mcp.json")
)
$pairs = @(
    @("My Projects\\jev-ultrafast", "My Projects\\AgentJevUltraFast"),
    @("My Projects\jev-ultrafast", "My Projects\AgentJevUltraFast")
)

Write-Host "Checkout : $source"
Write-Host "Target   : $target"
foreach ($file in $edits) {
    if (-not (Test-Path -LiteralPath $file)) { Write-Host "skip (missing)  $file"; continue }
    $raw = Get-Content -LiteralPath $file -Raw
    $updated = $raw
    foreach ($pair in $pairs) { $updated = $updated.Replace($pair[0], $pair[1]) }
    if ($updated -eq $raw) { Write-Host "unchanged       $file"; continue }
    $count = ([regex]::Matches($raw, "My Projects\\+jev-ultrafast")).Count
    Write-Host "update ($count refs) $file"
    if (-not $DryRun) {
        Copy-Item -LiteralPath $file -Destination "$file.bak-agentjev" -Force
        [System.IO.File]::WriteAllText($file, $updated)
    }
}
if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run only. Re-run without -DryRun to move the folder."
    exit 0
}
Move-Item -LiteralPath $source -Destination $target
Write-Host ""
Write-Host "Moved to $target"
Write-Host "Next steps:"
Write-Host "  1. cd `"$target`""
Write-Host "  2. uv sync    # rebuilds .venv for the new path; if it complains, delete .venv and run uv sync again"
Write-Host "  3. Reopen opencode / Codex / Claude Code / Command Code"
