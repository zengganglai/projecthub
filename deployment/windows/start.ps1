param(
    [string]$Python = "python",
    [Nullable[int]]$Port,
    [string]$BindHost,
    [string]$ProjectRoot,
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
if (-not $ProjectRoot) { $ProjectRoot = $SourceRoot }
$Interpreter = (Get-Command -Name $Python -CommandType Application -ErrorAction Stop).Source
$Arguments = @("-X", "utf8", "-B", "-m", "scripts.service_control", "start",
    "--project-root", $ProjectRoot, "--timeout", "$TimeoutSeconds")
if ($PSBoundParameters.ContainsKey("Port")) { $Arguments += @("--port", "$Port") }
if ($PSBoundParameters.ContainsKey("BindHost")) { $Arguments += @("--host", $BindHost) }
Push-Location -LiteralPath $SourceRoot
try {
    & $Interpreter @Arguments
    $ExitStatus = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitStatus

