[CmdletBinding()]
param([string]$MachineRoot = (Join-Path $env:ProgramData 'AgentRouterCodexProxy'))

$ErrorActionPreference = 'Stop'
$machineBase = [IO.Path]::GetFullPath($MachineRoot).TrimEnd('\')
$appBase = Join-Path $machineBase 'app'
$stateBase = Join-Path $machineBase 'control'
$manifestPath = Join-Path $machineBase 'manifest.json'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator permissions are required to enable browser updates for the shared service.'
}
$targets = @('proxy.py','telemetry.py','control.py','panel_updates.py','service.py','updater.py',
             'doctor.py','diagnose.ps1','requirements.txt','panel/index.html','panel/panel.js',
             'panel/panel.css','panel/mark.svg')
$required = @($targets) + @('update_guard.py','machine_service.py')
foreach ($base in @($machineBase,$appBase)) {
    if (-not (Test-Path -LiteralPath $base -PathType Container) -or
        ((Get-Item -LiteralPath $base).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The shared installation must use ordinary directories.'
    }
}
foreach ($name in $required) {
    $path = [IO.Path]::GetFullPath((Join-Path $appBase $name))
    if (-not $path.StartsWith($appBase + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $path -PathType Leaf) -or
        ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw ('Missing or unexpected control runtime: ' + $name)
    }
    $cursor = Split-Path -Parent $path
    while ($cursor -and $cursor -cne $machineBase) {
        if ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw ('Runtime directory cannot be a reparse point: ' + $name)
        }
        $cursor = Split-Path -Parent $cursor
    }
}
$source = [IO.File]::ReadAllText((Join-Path $appBase 'proxy.py'))
$version = [regex]::Match($source, '(?m)^VERSION\s*=\s*"(\d+\.\d+\.\d+)"\s*$').Groups[1].Value
if (-not $version -or [version]$version -lt [version]'1.8.0') { throw 'Install the complete 1.8.0 or newer runtime first.' }
$writeRights = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor
    [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
$protected = @($machineBase,$appBase,(Join-Path $appBase 'update_guard.py'),(Join-Path $appBase 'machine_service.py'))
if (Test-Path -LiteralPath (Join-Path $appBase '.env')) { $protected += Join-Path $appBase '.env' }
foreach ($path in $protected) {
    foreach ($entry in (Get-Acl -LiteralPath $path).Access) {
        $sid = $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -in @('S-1-5-19','S-1-5-32-545','S-1-1-0','S-1-5-11') -and
            $entry.AccessControlType -eq 'Allow' -and ($entry.FileSystemRights -band $writeRights)) {
            throw 'Protect the installation, bootstrap and .env before granting individual update files.'
        }
    }
}
if (Test-Path -LiteralPath $manifestPath) {
    if ((Get-Item -LiteralPath $manifestPath).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Manifest cannot be a reparse point.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    $files = @()
    foreach ($name in $required) {
        $path = Join-Path $appBase $name
        $files += [pscustomobject]@{path=('app/' + $name);size=(Get-Item -LiteralPath $path).Length;
                    sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
    }
    $manifest = [pscustomobject]@{version=1;proxy_version=$version;files=$files}
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $commit = & git -c ("safe.directory=" + $appBase) -C $appBase rev-parse --verify HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $commit -match '^[0-9a-f]{40}$') {
            $manifest | Add-Member -NotePropertyName local_patch_commit -NotePropertyValue $commit -Force
        }
    }
    $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}
if ($manifest.proxy_version -cne $version) { throw 'Installed manifest version differs from the runtime.' }
foreach ($name in $required) {
    $entries = @($manifest.files | Where-Object path -ceq ('app/' + $name))
    if ($entries.Count -ne 1 -or (Get-FileHash -LiteralPath (Join-Path $appBase $name) -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entries[0].sha256) {
        throw ('Installed manifest differs from runtime: ' + $name)
    }
}
if (-not (Test-Path -LiteralPath $stateBase)) { New-Item -ItemType Directory -Path $stateBase | Out-Null }
if ((Get-Item -LiteralPath $stateBase).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Control state cannot be a reparse point.' }
$stateAcl = New-Object Security.AccessControl.DirectorySecurity
$stateAcl.SetAccessRuleProtection($true,$false)
foreach ($entry in @(@('S-1-5-18','FullControl'),@('S-1-5-32-544','FullControl'),@('S-1-5-19','Modify'))) {
    $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid,$entry[1],'ContainerInherit,ObjectInherit','None','Allow')
    $stateAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $stateBase -AclObject $stateAcl
$serviceSid = New-Object Security.Principal.SecurityIdentifier('S-1-5-19')
foreach ($path in @($targets | ForEach-Object { Join-Path $appBase $_ }) + @($manifestPath)) {
    $acl = Get-Acl -LiteralPath $path
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($serviceSid,'Modify','Allow')
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $path -AclObject $acl
}
Write-Output 'Browser update permissions enabled: only the explicit runtime files, manifest and protected control state.'
