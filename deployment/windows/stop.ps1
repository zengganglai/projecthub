param(
    [string]$Python = "python",
    [string]$ProjectRoot,
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
if (-not $ProjectRoot) { $ProjectRoot = $SourceRoot }
$Interpreter = (Get-Command -Name $Python -CommandType Application -ErrorAction Stop).Source
Push-Location -LiteralPath $SourceRoot
try {
    & $Interpreter -X utf8 -B -m scripts.service_control stop --project-root $ProjectRoot --timeout "$TimeoutSeconds"
    $ExitStatus = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitStatus

