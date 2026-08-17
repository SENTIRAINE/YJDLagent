param(
    [Parameter(Mandatory = $true)][string]$MongoUri,
    [string]$Database = "yjdl_agent",
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$target = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $target | Out-Null

$backupId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$archive = Join-Path $target ("yjdl-agent-{0}.archive.gz" -f $backupId)
$manifest = Join-Path $target ("yjdl-agent-{0}.manifest.json" -f $backupId)

& mongodump --uri=$MongoUri --db=$Database --archive=$archive --gzip
if ($LASTEXITCODE -ne 0) { throw "mongodump failed with exit code $LASTEXITCODE" }

$hello = & mongosh $MongoUri --quiet --eval "JSON.stringify(db.adminCommand({hello:1}))"
$metadata = [ordered]@{
    backupId = $backupId
    database = $Database
    archive = [IO.Path]::GetFileName($archive)
    createdAt = (Get-Date).ToUniversalTime().ToString("o")
    applicationSchemaVersion = 2
    mongoHello = ($hello | ConvertFrom-Json)
}
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifest -Encoding utf8
Write-Output $manifest
