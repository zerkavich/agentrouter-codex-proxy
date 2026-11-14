<#
    Configure the current Codex user's memory models. No elevation required.
    Explicit memory models are preserved unless -OverwriteMemoryModels is set.
    -WhatIf_ creates no config, backups, directories or installed packages.
#>
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$MemoryModel = 'gpt-6.1-sol',
    [string]$CodexConfig,
    [switch]$OverwriteMemoryModels,
    [switch]$WhatIf_,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $candidate = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate) { $Python = $candidate }
    else {
        $command = Get-Command python.exe, python, py.exe, py -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $Python = $command.Source }
    }
}
if (-not $Python) { throw 'Python 3.10+ not found. Install Python or pass -Python <path>.' }

# This parser belongs to the installer, not requirements.txt or the runtime.
$savedPreference = $ErrorActionPreference
$parserCheck = @'
import tomlkit
assert tomlkit.__version__ == "0.13.2"
'@
try {
    $ErrorActionPreference = 'Continue'
    $parserCheck | & $Python - *> $null
    $dependencyExit = $LASTEXITCODE
} finally { $ErrorActionPreference = $savedPreference }
if ($dependencyExit -ne 0) {
    if ($WhatIf_) {
        throw 'Preview needs requirements-installer.txt installed first; no packages or files were changed.'
    }
    Write-Host '[memory] Installing the configuration parser (installer only).'
    & $Python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-installer.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Installing the memory helper dependency failed. Use a writable Python environment or install requirements-installer.txt first.'
    }
}

$arguments = @((Join-Path $root 'configure-codex-memory.py'), '--model', $MemoryModel)
if ($CodexConfig) { $arguments += @('--config', $CodexConfig) }
if ($OverwriteMemoryModels) { $arguments += '--overwrite' }
if ($WhatIf_) { $arguments += '--dry-run' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw 'Codex memory setup failed. See the safe error above, fix the config/access, or skip setup with -SkipMemoryModelSetup in the installer.'
}
