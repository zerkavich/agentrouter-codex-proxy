# Windows installation

This guide covers Windows 10/11, Codex configuration, background startup, verification, restart and removal. Use Python 3.10 or newer; the 86-test suite has been checked with Python 3.10 and 3.12. The installed portable Python 3.12 runtime was checked separately with its native packages.

## 1. Install Python and Git

Install [Python](https://www.python.org/downloads/windows/) and [Git for Windows](https://git-scm.com/download/win). Open a new PowerShell window and check:

```powershell
py --version
git --version
```

For startup under `LOCAL SERVICE`, install Python **for all users** in a machine-readable directory such as `C:\Program Files\Python312`. A Python installation inside another user's profile is unsuitable for that mode.

## 2. Download the proxy and install its dependencies

Clone this fork's `main` branch, which includes cancellation cleanup and native latency improvements:

```powershell
$proxyRoot = Join-Path $env:LOCALAPPDATA 'AgentRouterCodexProxy'
git clone --branch main https://github.com/zerkavich/agentrouter-codex-proxy.git $proxyRoot
if ($LASTEXITCODE -ne 0) { throw 'Clone failed; check the destination and repository URL.' }
Set-Location -LiteralPath $proxyRoot
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

Use an installed Python version instead of `-3.12` if necessary. When updating an existing checkout, also run `python -m pip install -r requirements.txt` with its virtual-environment interpreter so the native dependencies are installed.

In `.env`, keep `AGENTROUTER_UPSTREAM=https://agentrouter.org`, `PROXY_PORT=8788` and `PROXY_VERBOSE=0`. Minimal logging suppresses per-request informational output while retaining warnings and errors. Set `PROXY_AUTO_UPDATE=0` when maintaining a reviewed installation. Codex normally forwards its own Authorization header, so no API key needs to be stored in the proxy's `.env`.

After verifying that your upstream accepts gzip request bodies, you can set `PROXY_REQUEST_GZIP=1` to reduce upload size for large text histories. The default threshold is 262144 bytes and poorly compressible payloads are skipped. Compression is optional and disabled by default; see [request forwarding and compression](../README.md#request-forwarding-and-optional-compression). For the machine-wide installation below, add these settings to the installed app's `.env` and restart its scheduled task.

An HTTP 415 rejection of proxy-added gzip triggers one automatic retry with the same payload sent uncompressed. The `gzip_fallbacks` counter in `/_stats` records this recovery attempt. Further requests with the same upstream, key, organization and project skip gzip for 60 seconds, avoiding repeated rejection until the next probe. Encodings supplied by Codex itself are preserved. See [runtime tuning](runtime-tuning.md) for the bounded worker pool, adaptive compression and stream buffers.

For independent keys on a shared PC, keep `PROXY_RATE_LIMIT_SCOPE=credential`. If different keys deliberately share the same upstream quota, use `shared` so a 429 pauses all of them together. Restart the proxy after editing its `.env`.

## 3. Start and check the proxy

From the repository directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-proxy.ps1
```

Leave that window open. In a second PowerShell window:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8788/_health'
```

Expect `status: ok`, `version: 1.6.0`, `listen: 127.0.0.1:8788`, `request_gzip_backend: isal`, `connection_pool_initialized: true`, `upstream_pool_cleanup_active: true` and a `stats` object. The gzip backend can be available even when request compression is disabled. Keep the default loopback address for a proxy shared by accounts on the same PC.

The `ExecutionPolicy Bypass` above applies to this PowerShell process. It does not change the machine's execution policy.

## 4. Connect Codex

Find the active Codex configuration, usually `$env:USERPROFILE\.codex\config.toml`. If `CODEX_HOME` is set, use that directory instead. Back up the configuration before editing it.

For an existing AgentRouter provider, change its `base_url` to:

```toml
base_url = "http://127.0.0.1:8788/v1"
```

Keep the existing model, reasoning effort, provider identifier and credential settings. The table name must match your `model_provider`; names such as `agentrouter` and `Agentrouter` are different identifiers.

If adding a provider from scratch, merge these entries into the existing configuration:

```toml
model_provider = "agentrouter"

[model_providers.agentrouter]
name = "AgentRouter via local proxy"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
env_key = "CODEX_GATEWAY_API_KEY"
```

Set `CODEX_GATEWAY_API_KEY` in the **Codex user's environment**, using their own AgentRouter key. Keep credentials out of screenshots, commits and shared proxy configuration. Each Windows account needs its own Codex configuration and credentials, unless those accounts deliberately share a Codex home.

Restart Codex Desktop or use **Developer: Reload Window** in VS Code. A new Codex CLI invocation reads the new configuration. Ask Codex to reply `CODEX_PROXY_OK`; a successful reply checks the authenticated upstream path as well as the local listener.

## 5. Background startup for one Windows user

Stop the foreground proxy with `Ctrl+C`, then run from the repository directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-autostart.ps1 -NoAutoUpdate
Get-ScheduledTask -TaskName AgentRouterCodexProxy
Invoke-RestMethod 'http://127.0.0.1:8788/_health'
```

This task starts at that user's logon. Other accounts can reach the same loopback listener while it runs, but this setup depends on the owner being logged in. Logs are in `logs\proxy.log` next to the source, with a rotated `.1` file.

To restart this installation, remove and recreate its logon task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-autostart.ps1 -NoAutoUpdate
Invoke-RestMethod 'http://127.0.0.1:8788/_health'
```

Restart interrupts active requests; retry them in Codex after the health check succeeds.

## 6. Optional startup for all Windows users

For a proxy that starts at Windows boot, use one machine-wide task under `LOCAL SERVICE`. Choose this mode instead of the per-user logon task. First remove the previous per-user task with `uninstall-autostart.ps1` from its original repository directory.

Install Python for all users, then open **PowerShell as administrator**. The following example creates a fresh installation; it stops if its destination, task name or port is already in use. Adjust `$machinePython` to the actual machine-wide interpreter path.

```powershell
$ErrorActionPreference = 'Stop'
$machinePython = 'C:\Program Files\Python312\python.exe'
$machineRoot = Join-Path $env:ProgramData 'AgentRouterCodexProxy'
$appRoot = Join-Path $machineRoot 'app'
$taskName = 'AgentRouterCodexProxyMachine'
if (-not (Test-Path -LiteralPath $machinePython)) { throw 'Install Python for all users and set its actual path.' }
if (Test-Path -LiteralPath $machineRoot) { throw 'Destination already exists; preserve the existing installation before proceeding.' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'The machine task already exists.' }
if (Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8788 is occupied; stop the previous installation first.' }

git clone --branch main https://github.com/zerkavich/agentrouter-codex-proxy.git $appRoot
if ($LASTEXITCODE -ne 0) { throw 'Clone failed.' }
& $machinePython -m venv (Join-Path $appRoot '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
$venvPython = Join-Path $appRoot '.venv\Scripts\python.exe'
& $venvPython -m pip install -r (Join-Path $appRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
New-Item -ItemType Directory -Path (Join-Path $appRoot 'logs') -Force | Out-Null
$proxyEnv = @'
AGENTROUTER_UPSTREAM=https://agentrouter.org
PROXY_PORT=8788
PROXY_VERBOSE=0
PROXY_AUTO_UPDATE=0
'@
[System.IO.File]::WriteAllText((Join-Path $appRoot '.env'), $proxyEnv, (New-Object System.Text.UTF8Encoding($false)))

# SYSTEM and administrators can maintain the installation. Users and LOCAL
# SERVICE can read/run it; LOCAL SERVICE can write only the logs directory.
icacls $machineRoot /inheritance:r
if ($LASTEXITCODE -ne 0) { throw 'Changing directory inheritance failed.' }
icacls $machineRoot /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' '*S-1-5-32-545:(OI)(CI)(RX)' '*S-1-5-19:(OI)(CI)(RX)'
if ($LASTEXITCODE -ne 0) { throw 'Setting installation permissions failed.' }
icacls (Join-Path $appRoot 'logs') /grant:r '*S-1-5-19:(OI)(CI)(M)'
if ($LASTEXITCODE -ne 0) { throw 'Setting log directory permissions failed.' }

$launcher = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
$service = Join-Path $appRoot 'service.py'
$action = New-ScheduledTaskAction -Execute $launcher -Argument ('"{0}" --host 127.0.0.1 --port 8788 --no-auto-update' -f $service) -WorkingDirectory $appRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT5S'
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-19' -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
```

Allow several seconds for startup, then check `/_health`. Run the Codex test from each account after configuring its provider as described in step 4. The shared proxy receives each request's Authorization header; credentials stay with the individual Codex users.

For this installation, logs are at `$env:ProgramData\AgentRouterCodexProxy\app\logs\proxy.log`. Automatic updates are disabled because the service account has read/execute access to the source. An administrator can install reviewed updates while the task is stopped.

To restart the machine-wide task, use an elevated PowerShell window. Identify its worker before stopping the task and recheck its identity before terminating a leftover worker:

```powershell
$appRoot = Join-Path $env:ProgramData 'AgentRouterCodexProxy\app'
$taskName = 'AgentRouterCodexProxyMachine'
$workerPython = Join-Path $appRoot '.venv\Scripts\python.exe'
$workerPattern = '^"?' + [regex]::Escape($workerPython) + '"?\s+-m\s+uvicorn\s+proxy:app(?:\s|$)'
$workers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match $workerPattern })
Stop-ScheduledTask -TaskName $taskName
foreach ($worker in $workers) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($worker.ProcessId)"
    if ($current -and $current.CreationDate -eq $worker.CreationDate -and $current.CommandLine -eq $worker.CommandLine) {
        Stop-Process -Id $current.ProcessId -ErrorAction Stop
    }
}
Start-ScheduledTask -TaskName $taskName
```

Wait for `/_health` to return `status: ok`, then retry interrupted Codex requests.

## 7. Troubleshooting and rollback

Version 1.7.0 adds a local diagnostic tool. Run it from the complete checkout or installed app directory; the wrapper selects its virtual environment or the machine's portable Python. Update telemetry.py, doctor.py and diagnose.ps1 together with proxy.py.

```powershell
Set-Location 'C:\ProgramData\AgentRouterCodexProxy\app'
.\diagnose.ps1
.\diagnose.ps1 -Watch
.\diagnose.ps1 -ProfileSeconds 60 -Watch -Duration 60 -ExportPath "$env:USERPROFILE\Desktop\proxy-report.json"
```

For a per-user checkout, use that checkout's path instead. Profiling starts without restart/config changes and expires automatically. The export contains sanitized counters and timing aggregates; choose a new file for each report. See [diagnostic interpretation](diagnostics.md) before treating overlapping timings as proxy overhead.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8788/_stats'
Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
```

- **Connection refused:** check that the selected task is running, that Python and dependencies are readable by its account, and that the configured port is 8788.
- **Port occupied:** use one proxy instance. Inspect the owning process before changing its state.
- **502 with `PoolTimeout`:** check that version 1.6.0 or newer is installed. It releases abandoned sends before response headers arrive, partially read JSON and cancelled SSE responses. Admission waiters stay outside HTTPcore, and abandoned CONNECTING/ACTIVE pool entries are reclaimed under its normal lock. `pool_timeouts`, `upstream_capacity_waits`, `upstream_disconnect_cancellations` and `upstream_orphaned_connections_reclaimed` are cumulative diagnostic counters. When abandoned requests finish cleanup, `upstream_slots_in_use` and `upstream_slot_waiters` in `/_health` should return to zero if no other requests are active. A generic 502 may still be an upstream/network failure; these counters help identify the reproduced pool-exhaustion case. Use the restart procedure above when needed; a Windows reboot is unnecessary.
- **`upstream_pool_cleanup_active: false`:** verify that all pinned `requirements.txt` packages were installed. The cleanup wrapper depends on the supported HTTPcore pool layout; rerun the regression suite before adopting dependency upgrades.
- **401/403:** check the affected Codex user's AgentRouter credentials. A successful local health check does not authenticate upstream requests.
- **A second Windows user fails:** configure that account's Codex provider and credentials; use machine-wide startup if the first account may log out.

To return Codex to a direct AgentRouter connection, restore only that provider's `base_url` to its previous value, normally `https://agentrouter.org/v1`, then reload Codex. Keep the rest of the current configuration.

For a per-user installation, remove startup with `uninstall-autostart.ps1`. For the machine-wide installation, run the restart procedure through the worker-stop step, then use `Unregister-ScheduledTask -TaskName AgentRouterCodexProxyMachine -Confirm:$false` instead of starting the task again. Keep the source, configuration backups and logs until you have verified the direct connection works.
