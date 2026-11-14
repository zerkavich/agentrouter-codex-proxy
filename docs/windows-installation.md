# Windows installation

This guide covers Windows 10/11, Codex configuration, background startup, browser management, verification and removal. Version 1.9.0 adds independent management, Russian/English interfaces, bounded request history and lower-memory payload processing. Node.js is needed only to rebuild the panel; running the proxy needs Python. See [validation and measurements](resilience-1.9.0.md) for the tested release.

## 1. Install Python and Git

Install [Python](https://www.python.org/downloads/windows/) and [Git for Windows](https://git-scm.com/download/win). Open a new PowerShell window and check:

```powershell
py --version
git --version
```

For startup under `LOCAL SERVICE`, install Python **for all users** in a machine-readable directory such as `C:\Program Files\Python312`. A Python installation inside another user's profile is unsuitable for that mode.

## 2. Download the proxy and install its dependencies

Clone this fork's `main` branch, which includes the browser panel and cancellation cleanup:

```powershell
$proxyRoot = Join-Path $env:LOCALAPPDATA 'AgentRouterCodexProxy'
git clone --branch main https://github.com/zerkavich/agentrouter-codex-proxy.git $proxyRoot
if ($LASTEXITCODE -ne 0) { throw 'Clone failed; check the destination and repository URL.' }
Set-Location -LiteralPath $proxyRoot
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-installer.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

Use an installed Python version instead of `-3.12` if necessary. When updating an existing checkout, also run `python -m pip install -r requirements.txt` with its virtual-environment interpreter so the native dependencies are installed.

`requirements-installer.txt` supplies the TOML editor used only while configuring Codex. It is not imported by the proxy or supervisor.

In `.env`, keep `AGENTROUTER_UPSTREAM=https://agentrouter.org`, `PROXY_PORT=8788` and `PROXY_VERBOSE=0`. Minimal logging suppresses per-request informational output while retaining warnings and errors. Set `PROXY_AUTO_UPDATE=0` when maintaining a reviewed installation. Codex normally forwards its own Authorization header, so no API key needs to be stored in the proxy's `.env`.

After verifying that your upstream accepts gzip request bodies, enable **Settings → Compress large requests** in the browser and apply the saved settings with a restart. The default threshold is 262144 bytes and poorly compressible payloads are skipped. Compression is optional and disabled by default; see [request forwarding and compression](../README.md#request-forwarding-and-optional-compression). Existing `.env` settings remain valid; browser overrides are saved separately.

An HTTP 415 rejection of proxy-added gzip triggers one automatic retry with the same payload sent uncompressed. The `gzip_fallbacks` counter in `/_stats` records this recovery attempt. Further requests with the same upstream, key, organization and project skip gzip for 60 seconds, avoiding repeated rejection until the next probe. Encodings supplied by Codex itself are preserved. See [runtime tuning](runtime-tuning.md) for the bounded worker pool, adaptive compression and stream buffers.

For independent keys on a shared PC, keep `PROXY_RATE_LIMIT_SCOPE=credential`. If different keys deliberately share the same upstream quota, choose **Shared quota** in the browser so a 429 pauses them together. Saved tuning settings apply after restarting the worker.

## 3. Start and check the proxy

From the repository directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure-codex-memory.ps1
if ($LASTEXITCODE -ne 0) { throw 'Codex memory setup failed; fix the reported configuration/access problem first.' }
& .\.venv\Scripts\python.exe .\service.py --host 127.0.0.1 --port 8788 --no-auto-update --skip-start-update
```

Leave that window open. In a second PowerShell window:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8788/_health'
```

Expect `status: ok`, `version: 1.9.0`, `listen: 127.0.0.1:8788`, `request_gzip_backend: isal`, `connection_pool_initialized: true`, `upstream_pool_cleanup_active: true` and a `stats` object. The gzip backend can be available even when request compression is disabled. Keep the default loopback address for a proxy shared by accounts on the same PC.

Open **http://127.0.0.1:8789/** in a browser. This supervisor panel remains accessible while the worker on 8788 hangs, and can recover it manually or automatically. Check `http://127.0.0.1:8789/_control/api/status` for `supervisor.independent: true` and `worker_responsive: true`. Select Russian or English in the header; the browser remembers the selection. The worker also serves management on 8788. Direct `proxy.py` or `uvicorn` launches expose only the worker panel and need an external restart. The `ExecutionPolicy Bypass` used by PowerShell scripts below applies only to their process.

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

### Codex memory models

The setup helper fills missing memory model settings with:

```toml
[memories]
extract_model = "gpt-6.1-sol"
consolidation_model = "gpt-6.1-sol"
```

This prevents background memory work from falling back to Terra on providers where Terra is unavailable. It configures remote API models, without downloading weights. It preserves existing explicit model choices, all other config values and comments, and whether memory is enabled. Use a Codex client supporting these two settings and a model routed by the selected provider.

`start-proxy.ps1` and `install-autostart.ps1` perform this setup automatically for the current Codex user. Supervised `start-proxy.ps1 -Service`, `service.py` and `machine_service.py` do not edit Codex profiles. If the proxy is already running, use the standalone helper:

```powershell
.\configure-codex-memory.ps1
.\configure-codex-memory.ps1 -WhatIf_
.\configure-codex-memory.ps1 -MemoryModel gpt-6.1-sol -OverwriteMemoryModels
.\configure-codex-memory.ps1 -CodexConfig 'D:\SharedCodex\config.toml'
```

`-WhatIf_` previews without writing files or installing packages; install `requirements-installer.txt` first if needed. `-OverwriteMemoryModels` explicitly replaces both existing choices. The launcher/installer accepts `-MemoryModel`, `-CodexConfig`, `-OverwriteMemoryModels` and `-SkipMemoryModelSetup`. Without an explicit path, `CODEX_HOME` takes priority over the current user's `~/.codex`.

Each actual change saves a private, byte-verified `config.toml.memory-*.bak` beside the resolved file. No backup is created on an unchanged or preview run. On Windows the helper preserves the existing config's access permissions; backups are readable only by their owner, administrators and SYSTEM. To undo the memory change, restore just these two fields from the backup while keeping newer config edits. Reload Codex / **Developer: Reload Window** afterward. Updating the proxy in the browser does not run this per-user helper.

## 5. Background startup for one Windows user

Stop the foreground proxy with `Ctrl+C`, then run from the repository directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-autostart.ps1 -NoAutoUpdate
Get-ScheduledTask -TaskName AgentRouterCodexProxy
Invoke-RestMethod 'http://127.0.0.1:8788/_health'
```

This task starts at that user's logon. Other accounts can reach the same loopback listener while it runs, but this setup depends on the owner being logged in. Logs are in `logs\proxy.log` next to the source, with a rotated `.1` file.

Use **Overview → Restart** in the browser. It stops accepting new API calls, lets current answers finish, restarts the worker and reconnects the panel. Only choose **Restart immediately** when you intend to interrupt current requests.

If the listener is unavailable, remove and recreate the logon task:

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
& $venvPython -m pip install -r (Join-Path $appRoot 'requirements.txt') -r (Join-Path $appRoot 'requirements-installer.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
New-Item -ItemType Directory -Path (Join-Path $appRoot 'logs') -Force | Out-Null
$proxyEnv = @'
AGENTROUTER_UPSTREAM=https://agentrouter.org
PROXY_PORT=8788
PROXY_VERBOSE=0
PROXY_AUTO_UPDATE=0
'@
[System.IO.File]::WriteAllText((Join-Path $appRoot '.env'), $proxyEnv, (New-Object System.Text.UTF8Encoding($false)))

# SYSTEM and administrators maintain the installation. Users and LOCAL SERVICE
# can read/run it. The helper below grants only the explicit browser update files.
icacls $machineRoot /inheritance:r
if ($LASTEXITCODE -ne 0) { throw 'Changing directory inheritance failed.' }
icacls $machineRoot /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' '*S-1-5-32-545:(OI)(CI)(RX)' '*S-1-5-19:(OI)(CI)(RX)'
if ($LASTEXITCODE -ne 0) { throw 'Setting installation permissions failed.' }
icacls (Join-Path $appRoot 'logs') /grant:r '*S-1-5-19:(OI)(CI)(M)'
if ($LASTEXITCODE -ne 0) { throw 'Setting log directory permissions failed.' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $appRoot 'enable-browser-updates.ps1') -MachineRoot $machineRoot
if ($LASTEXITCODE -ne 0) { throw 'Enabling browser update permissions failed.' }

$launcher = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
$service = Join-Path $appRoot 'machine_service.py'
$action = New-ScheduledTaskAction -Execute $launcher -Argument ('"{0}" --host 127.0.0.1 --port 8788 --no-auto-update' -f $service) -WorkingDirectory $appRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT5S'
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-19' -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
```

Allow several seconds for startup, then check `/_health`. Run the Codex test from each account after configuring its provider as described in step 4. The shared proxy receives each request's Authorization header; credentials stay with the individual Codex users.

Run memory setup once **in each Codex user's normal session**, including when two accounts share `CODEX_HOME` (a second unchanged run is harmless):

```powershell
$appRoot = Join-Path $env:ProgramData 'AgentRouterCodexProxy\app'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $appRoot 'configure-codex-memory.ps1')
```

The administrative install above prepares the helper dependency before protecting the virtual environment. For an existing protected installation that lacks it, an administrator can install `requirements-installer.txt` once, or use the helper with `-Python <your-own-writable-python.exe>`. Do not run profile setup as `LOCAL SERVICE` or assume an administrator's `CODEX_HOME` is another user's config.

For this installation, logs are at `$env:ProgramData\AgentRouterCodexProxy\app\logs\proxy.log`. Browser tuning and update state are stored in a protected sibling `control` directory. The service can modify only 13 explicitly listed update files, the installed manifest and that state directory. The wrapper, recovery bootstrap, `.env`, Python, packages and parent app directory remain read-only to the service.

Git auto-updates are disabled. Use **Updates** in the browser for compatible releases; dependency changes, recovery-bootstrap changes and added runtime files require an administrator installation. Future browser updates maintain the installed `manifest.json`. An old Git checkout or external staging copy is not the authoritative record of the running version.

Use **Overview → Restart** for normal restarts from any Windows account. If the browser cannot reach the listener, use an elevated PowerShell window. Capture the matching supervisor and workers before stopping the task, including a supervisor relaunched by a browser update:

```powershell
$appRoot = Join-Path $env:ProgramData 'AgentRouterCodexProxy\app'
$taskName = 'AgentRouterCodexProxyMachine'
$workerPython = Join-Path $appRoot '.venv\Scripts\python.exe'
$supervisorScript = Join-Path $appRoot 'machine_service.py'
$workerPattern = '^"?' + [regex]::Escape($workerPython) + '"?\s+-m\s+uvicorn\s+proxy:app(?:\s|$)'
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe','pythonw.exe') -and $_.ExecutablePath -and
    $_.ExecutablePath.StartsWith($appRoot + '\',[StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -and
    ($_.CommandLine.Contains($supervisorScript) -or $_.CommandLine -match $workerPattern)
} | Sort-Object @{Expression={ if ($_.CommandLine.Contains($supervisorScript)) { 0 } else { 1 } }})
Stop-ScheduledTask -TaskName $taskName
foreach ($process in $processes) {
    $current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $process.ProcessId)
    if ($current -and $current.CreationDate -eq $process.CreationDate -and $current.CommandLine -ceq $process.CommandLine) {
        Stop-Process -Id $current.ProcessId -ErrorAction Stop
    }
}
Start-ScheduledTask -TaskName $taskName
```

Wait for `/_health` to return `status: ok`, then retry interrupted Codex requests.

If an existing installation uses portable Python in `AgentRouterCodexProxy\runtime`, use its actual `python.exe` path and executable-root prefix in that emergency procedure. Do not delete an existing installation to apply this fresh-install example. Upgrade all runtime files together after backing up files, manifest and permissions.

## 7. Manage the proxy in a browser

Open **http://127.0.0.1:8789/**; `/_control/` opens the same independent panel. API traffic stays on 8788, with no additional management forwarding in its path. The five sections are:

| Section | Controls |
| --- | --- |
| Overview | Active requests including open streams, queues, pool state, pause/resume and graceful or forced restart. |
| History | Request times, outcomes, duration, byte counts and wait stages; daily RAM/queue graphs; recovery events and saved automatic reports. |
| Diagnostics | Start/stop an automatically expiring profile, view timing aggregates and download a sanitized JSON report. |
| Settings | Edit 30 tuning values, including retention and watchdog behavior, save them separately from `.env`, and apply them with a worker restart. |
| Updates | Explicitly check fork `main`, prepare a verified package, confirm installation and see success or rollback. |

Settings are shared by users of this proxy instance. A stale settings revision is rejected if another tab changed it. The panel does not edit Codex's model, credentials, provider, listener port or conversation history. Numeric history persists across restarts: defaults are 2000 requests over 24 hours, 1440 metric points and 16 automatic reports. Prompt/output text, headers and credentials are never recorded. Clearing history requires confirmation and preserves Codex chats. Worker RAM is shown separately from the supervisor and browser.

If the worker stops responding, **Recover proxy** replaces that owned process after confirmation; active calls must be retried in Codex. Automatic recovery requires three failed checks spaced ten seconds apart, a thirty-second startup grace, and no active managed update. Its limit is three automatic recoveries per ten minutes. A slow provider response does not trigger recovery when the worker's local health/status remains responsive. If both panels are unavailable, use the task restart procedure above.

The extra panel port defaults to the worker port plus one. If it is occupied, set a free `PROXY_PANEL_PORT` in `.env` and restart the supervisor. The service leaves other listeners untouched and logs a warning; automatic recovery requires the independent panel to start successfully.

The browser makes no CDN/font requests and does not automatically poll GitHub. Local status polling slows or pauses in hidden tabs. Controls require a loopback client, the correct local Host/Origin and a per-worker token; exposing the API on a LAN does not enable remote control access.

On a prepared machine installation, **Prepare update** checks four successful GitHub Actions jobs for the selected exact commit, extracts a bounded source package, verifies dependency pins and the protected bootstrap, and runs an isolated credential-free startup/API/assets probe. Preparation leaves the active worker running. Installation waits for current API requests, saves a verified backup and checks the new worker before admitting new calls. Startup failure triggers a verified rollback; the panel reconnects and shows the receipt. Keep the backup/state files for diagnosis.

To rebuild the committed browser assets during development:

```powershell
npm ci --prefix panel-src --no-audit --no-fund
npm run build --prefix panel-src
```

The Ubuntu/Python 3.12 CI job rebuilds these assets and checks that they match the committed `panel` files.

## 8. Troubleshooting and rollback

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
- **8788 hangs but 8789 works:** inspect retained history and use **Recover proxy**. Active calls interrupted by recovery are marked in history and can be retried in Codex. A series of 502 responses from a responsive worker remains an upstream/transport diagnosis, rather than a reason for watchdog restart.
- **502 with `PoolTimeout`:** install the complete current runtime. It releases abandoned sends before response headers arrive, partially read JSON and cancelled SSE responses. Version 1.8.0 receives client disconnect events during Responses waits/sends instead of periodically checking a self-cancelling scope. Admission waiters stay outside HTTPcore, and abandoned CONNECTING/ACTIVE pool entries are reclaimed under its normal lock. `pool_timeouts`, `upstream_capacity_waits`, `upstream_disconnect_cancellations` and `upstream_orphaned_connections_reclaimed` are cumulative counters. When abandoned requests finish cleanup, `upstream_slots_in_use` and `upstream_slot_waiters` in `/_health` should return to zero if no other requests are active. A generic 502 may still be an upstream/network failure. Use the browser restart or emergency procedure when needed; a Windows reboot is unnecessary.
- **`upstream_pool_cleanup_active: false`:** verify that all pinned `requirements.txt` packages were installed. The cleanup wrapper depends on the supported HTTPcore pool layout; rerun the regression suite before adopting dependency upgrades.
- **401/403:** check the affected Codex user's AgentRouter credentials. A successful local health check does not authenticate upstream requests.
- **A second Windows user fails:** configure that account's Codex provider and credentials; use machine-wide startup if the first account may log out.

To return Codex to a direct AgentRouter connection, restore only that provider's `base_url` to its previous value, normally `https://agentrouter.org/v1`, then reload Codex. Keep the rest of the current configuration.

For a per-user installation, remove startup with `uninstall-autostart.ps1`. For the machine-wide installation, run the restart procedure through the worker-stop step, then use `Unregister-ScheduledTask -TaskName AgentRouterCodexProxyMachine -Confirm:$false` instead of starting the task again. Keep the source, configuration backups and logs until you have verified the direct connection works.
