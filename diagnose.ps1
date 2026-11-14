[CmdletBinding()]
param(
    [int]$Port = 8788,
    [string]$ProxyHost = '127.0.0.1',
    [switch]$Watch,
    [switch]$Json,
    [double]$Interval = 2,
    [double]$Duration = 0,
    [double]$ProfileSeconds = 0,
    [string]$ExportPath
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskPythonCandidates = @(
    (Join-Path $taskRoot '.venv\Scripts\python.exe'),
    (Join-Path (Split-Path -Parent $taskRoot) 'runtime\python.exe')
)
$taskPython = $null
foreach ($taskCandidate in $taskPythonCandidates) {
    if (Test-Path -LiteralPath $taskCandidate -PathType Leaf) { $taskPython = $taskCandidate; break }
}
if (-not $taskPython) {
    $taskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $taskCommand) { $taskCommand = Get-Command py.exe -ErrorAction SilentlyContinue }
    if ($taskCommand) { $taskPython = $taskCommand.Source }
}
if (-not $taskPython) { throw 'Python 3.10+ was not found. Run this script from the installed app or a Python checkout.' }
$taskUrlHost = if ($ProxyHost.Contains(':') -and -not $ProxyHost.StartsWith('[')) { '[' + $ProxyHost + ']' } else { $ProxyHost }
$taskArguments = @((Join-Path $taskRoot 'doctor.py'), '--url', ('http://{0}:{1}' -f $taskUrlHost, $Port), '--interval', [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Interval))
if ($Watch) { $taskArguments += '--watch' }
if ($Json) { $taskArguments += '--json' }
if ($ProfileSeconds -gt 0) {
    $taskArguments += @('--profile', [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $ProfileSeconds))
}
if ($Duration -gt 0) {
    $taskArguments += @('--duration', [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Duration))
}
if ($ExportPath) { $taskArguments += @('--export', $ExportPath) }
& $taskPython @taskArguments
exit $LASTEXITCODE
