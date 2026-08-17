[CmdletBinding()]
param(
    [ValidateSet("Deterministic", "Live", "Regenerate")]
    [string]$Mode = "Deterministic",
    [string]$AgentUrl = $(if ($env:AGENT_BASE_URL) { $env:AGENT_BASE_URL } else { "http://127.0.0.1:8000" }),
    [string]$SpringUrl = $(if ($env:SPRING_BOOT_BASE_URL) { $env:SPRING_BOOT_BASE_URL } else { "http://127.0.0.1:8080" }),
    [string]$FixtureRoot,
    [string]$OutputDirectory,
    [string]$FrontendEvidencePath,
    [switch]$RequireFrontendEvidence,
    [switch]$RequireProductionFixtures
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
if (-not $FixtureRoot) {
    $FixtureRoot = Join-Path $repoRoot "tests\fixtures\agent-v1.1"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot "artifacts\agent-v1.1-acceptance"
}

$arguments = @(
    (Join-Path $PSScriptRoot "agent_v1_1_acceptance.py"),
    "--mode", $Mode,
    "--agent-url", $AgentUrl,
    "--spring-url", $SpringUrl,
    "--fixture-root", $FixtureRoot,
    "--output", $OutputDirectory
)
if ($FrontendEvidencePath) {
    $arguments += @("--frontend-evidence", $FrontendEvidencePath)
}
if ($RequireFrontendEvidence) {
    $arguments += "--require-frontend-evidence"
}
if ($RequireProductionFixtures) {
    $arguments += "--require-production-fixtures"
}

& $python @arguments
exit $LASTEXITCODE
