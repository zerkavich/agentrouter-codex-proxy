# AgentRouter Codex Responses Compatibility Proxy

A lightweight local compatibility proxy that fixes cross-resource session errors when using **Codex Desktop** or **Codex CLI** with **AgentRouter**.

Version **1.10.0** reduces large incoming-body copies, adds configurable aggregate memory admission, and shows bounded retry/recovery details in Russian/English request history. See [changes, measurements and tradeoffs](docs/incoming-attempts-1.10.0.md) and the [previous gzip/recovery improvements](docs/gzip-replay-1.9.3.md).

The independent browser panel at **http://127.0.0.1:8789/** provides automatic worker recovery, bounded request/metrics history and Russian/English interfaces. API traffic stays on **127.0.0.1:8788**, with a default 256 KiB per-stream queue. See [browser features and measurements](docs/resilience-1.9.0.md).

---

## The Problem

When running Codex through AgentRouter with `wire_api = "responses"` (the OpenAI Responses API), multi-turn sessions frequently fail with:

```text
OpenAI Responses bad request: The requested item was created under a different **** OpenAI resource.
Use the same resource that created the item to access it. [trace_id=...]
```

### Why does this happen?
1. **Responses API is Stateful**: The OpenAI Responses API (`/v1/responses`) relies on server-stored conversation state (`previous_response_id`, server-minted item IDs, and encrypted reasoning content).
2. **Upstream Load Balancing**: AgentRouter load-balances traffic across multiple Azure OpenAI instances.
3. **Resource Isolation in Azure**: When Turn 1 runs on Azure Resource A, all response IDs and items are stored inside Resource A. When Turn 2 is routed by AgentRouter to Resource B, Resource B checks the IDs against its own store, fails to locate or decrypt them, and rejects the request with HTTP 400.

---

## How This Proxy Solves It

```
┌─────────────────┐       ┌───────────────────────────────┐       ┌───────────────────────┐
│  Codex Desktop  │  -->  │  agentrouter-codex-proxy      │  -->  │  AgentRouter Upstream │
│  or Codex CLI   │  <--  │  (127.0.0.1:8788)             │  <--  │  (https://...)        │
└─────────────────┘       └───────────────────────────────┘       └───────────────────────┘
                                     │
                    ┌────────────────┴────────────────┐
                    │ 1. Transparent Fast-Path        │
                    │    Attempts request as-is to    │
                    │    preserve server KV cache.    │
                    │                                 │
                    │ 2. Smart Fallback on 400:       │
                    │    Detects resource mismatch,   │
                    │    sanitizes payload:           │
                    │    - Strips previous_response_id│
                    │    - Strips server item IDs     │
                    │    - Drops un-decryptable CoT   │
                    │                                 │
                    │ 3. Instant Recovery:            │
                    │    Retries immediately with     │
                    │    sanitized conversation.      │
                    │    ANY upstream instance can    │
                    │    process it without error!    │
                    └─────────────────────────────────┘
```

1. **Transparent Normal Turns**: Requests preserve their conversation state and original bytes unless image pruning or resource-mismatch recovery requires a change, retaining upstream cache eligibility.
2. **Instant Self-Healing Fallback**: When Azure OpenAI returns a 400 Resource Mismatch (or an SSE `response.failed` mismatch), the proxy catches the error, converts the payload into a self-contained stateless format (retaining complete conversation history while pruning excess historical screenshots past 35 images), and immediately resubmits.
3. **Pre-Commit SSE Priming & Rate-Limit Recovery**: Inspects initial `200 OK` `text/event-stream` setup frames within byte/frame budgets to catch early `rate_limit_exceeded` / `response.failed` / empty streams before committing headers to Codex, coordinates cooldown and stagger across callers sharing a credential, and honors `Retry-After` / reset hints before retrying. Independent credentials keep independent gates by default; shared-quota mode is available.
4. **Windows & DNS Resilience**: Provides resilient DNS fallback (`8.214.160.125`, `8.214.161.192`), TCP keep-alive hooks, and a Python 3.12 Windows `ProactorEventLoop` `_call_connection_lost` guard (`WinError 10054`).
5. **Codex Keeps Streaming**: Codex receives clean `text/event-stream` tokens and `: ping` heartbeats with far fewer interruptions.
6. **Cancellation Cleanup**: Releases pending upstream sends, partially read JSON/error bodies and SSE responses when a client disconnects or downstream delivery fails, preventing abandoned connections from exhausting HTTPX's pool.
7. **Native Acceleration**: Uses ISA-L gzip and Uvicorn's native HTTP parser, prepares the connection pool during startup, and avoids redundant copying and timer allocation while draining buffered SSE data.
8. **Quota Recovery & Diagnostics**: Fair cancellable launches preserve newer cooldowns against older successes, honor explicit retry hints and expose sanitized live status plus temporary timing capture.
9. **Browser Management**: The bundled RU/English panel provides overview, request history with expandable attempts, diagnostics, 32 tuning settings and explicit update controls. An independent supervisor listener keeps history and recovery accessible when the worker hangs. A prepared shared installation verifies packages, waits for active requests, checks the new worker and rolls back startup failures.
10. **Large Request Memory**: Incoming bodies use single-chunk reuse or incremental assembly. A configurable 256 MiB default reservation budget coordinates large parallel requests; small calls bypass the queue. Queueing reduces peak memory at the cost of delaying some large calls. The budget is an estimate, not a strict process RAM cap.

---

## Setup & Usage

For a complete walkthrough, see the [Windows installation guide](docs/windows-installation.md): Python and Git, Codex configuration, per-user or machine-wide startup, health checks, restart, and rollback.

For a short Russian walkthrough, see [управление в браузере](docs/browser-control.ru.md).

### 1. Requirements
- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`, `httpx`, `isal`, `httptools` (installed automatically by `start-proxy.ps1` or `service.py` when missing)
- The optional Codex configuration helper uses `requirements-installer.txt`; it adds no proxy runtime dependency.

### 2. Quick Start (Interactive or Headless Background)
```powershell
cd D:\Projects\agentrouter-codex-proxy
.\start-proxy.ps1                    # interactive foreground
.\start-proxy.ps1 -Background        # headless background service (no console window)
.\start-proxy.ps1 -Update            # check/apply fast-forward git updates before starting
.\start-proxy.ps1 -BindHost 0.0.0.0  # bind non-loopback (Docker/LAN; restrict with firewall!)
```

Direct Python / Linux / Docker usage with optional `--host` and `--port`:
```bash
python proxy.py --host 0.0.0.0 --port 8788
# or via headless supervisor / uvicorn:
python service.py --host 0.0.0.0 --port 8788
uvicorn proxy:app --host 0.0.0.0 --port 8788
```

Open **http://127.0.0.1:8789/** for independent management when running `service.py` or the protected `machine_service.py` entrypoint. The worker also serves the panel on **8788**; direct `proxy.py`/`uvicorn` launches expose only that panel and need an external restart. Select Russian or English in the header; the browser remembers the choice. Browser package installation requires the prepared machine-wide permissions described in the installation guide. Normal use requires no Node.js, CDN or external fonts.
> [!IMPORTANT]
> By default the listener binds `127.0.0.1` (loopback only) and cannot be changed via `.env`. Only pass `--host 0.0.0.0` (or `-BindHost 0.0.0.0`) inside isolated Docker/Proxmox or firewalled LAN setups, and avoid setting a shared fallback API key if other machines can reach the port.

### 3. Autostart as a Headless Background Task (Recommended)
Registers a Windows Task Scheduler logon task running `service.py` via GUI-subsystem `pythonw.exe` (no console or Windows Terminal window opens) with automatic rotating logs (`logs/proxy.log`) and a safe fast-forward auto-updater (`updater.py`):
```powershell
cd D:\Projects\agentrouter-codex-proxy
.\install-autostart.ps1
```
To pass `--no-auto-update` or check/apply updates manually:
```powershell
.\install-autostart.ps1 -NoAutoUpdate
python updater.py --check
python updater.py --apply
```
To remove the background task:
```powershell
.\uninstall-autostart.ps1
```

---

## Configuring Codex

In `C:\Users\<username>\.codex\config.toml`:

```toml
model = "gpt-6-astra"
model_provider = "Agentrouter"
model_reasoning_effort = "high"

[model_providers.Agentrouter]
name = "Agentrouter"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
env_key = "CODEX_GATEWAY_API_KEY"

[memories]
extract_model = "gpt-6.1-sol"
consolidation_model = "gpt-6.1-sol"
```

> [!NOTE]
> Ensure your `CODEX_GATEWAY_API_KEY` (or `AGENTROUTER_API_KEY`) is configured in your environment or in `.codex/.env`.

### Codex memory models

`start-proxy.ps1` (except `-Service`) and `install-autostart.ps1` automatically set **missing** memory extraction/consolidation models to `gpt-6.1-sol`. This avoids Codex's default Terra model when your AgentRouter group has no Terra channel. This selects a remote model; no model weights are downloaded. Existing explicit choices, the main model, reasoning effort, provider, credentials and memory enable/disable settings are preserved.

To configure an existing installation without restarting the proxy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure-codex-memory.ps1
# Preview only; requires the installer dependency to be present:
.\configure-codex-memory.ps1 -WhatIf_
# Replace both existing memory choices explicitly:
.\configure-codex-memory.ps1 -MemoryModel gpt-6.1-sol -OverwriteMemoryModels
```

The helper follows `CODEX_HOME`, otherwise the current user's `~/.codex/config.toml`; `-CodexConfig <path>` overrides it. Before a change, it saves a byte-verified private `config.toml.memory-*.bak` beside the resolved config. Repeated runs create no extra backups when nothing changes. Reload Codex or use **Developer: Reload Window** in VS Code afterward. The same model/path/overwrite options are available in both Windows launch/install scripts; use `-SkipMemoryModelSetup` to skip this step.

For Linux/macOS or direct Python startup, configure memory once before starting the service:

```bash
python -m pip install -r requirements-installer.txt
python configure-codex-memory.py
# Optional: --config /path/to/config.toml --model gpt-6.1-sol --overwrite --dry-run
```

Use a Codex version supporting `memories.extract_model` and `memories.consolidation_model`, and a model available through that user's provider. The helper does not enable disabled memories. A machine-wide proxy still needs Codex configuration in each user's own session; `LOCAL SERVICE` never edits user profiles. See the [installation guide](docs/windows-installation.md#codex-memory-models).

---

## Request forwarding and optional compression

Responses requests keep their original bytes until image pruning or resource-mismatch recovery changes the payload. Retries reuse the prepared body. This avoids repeated JSON parsing, copying, and serialization, and preserves UTF-8 text and numeric precision for unchanged requests.

For an upstream that accepts gzip request bodies, enable compression in the proxy's `.env`:

```dotenv
PROXY_REQUEST_GZIP=1
PROXY_REQUEST_GZIP_MIN_BYTES=262144
```

Compression is disabled by default. Large requests are sampled for compressibility before ISA-L gzip level 1 runs in a dedicated worker pool; poorly compressible data keeps its original encoding. Four workers handle at most 16 submitted jobs by default, leaving the event loop's default executor available for DNS and other work. Saturation forwards the original body. Direct Python usage falls back to standard-library gzip when ISA-L is unavailable. The compressed body is reused across retries and rebuilt after recovery changes the payload. Existing `Content-Encoding` headers are preserved. Restart the proxy after changing these settings.

If the upstream rejects proxy-added gzip with HTTP 415, the proxy retries once using the same decoded payload and original headers. That request stays uncompressed during subsequent retries and resource-mismatch recovery. For the next 60 seconds, subsequent requests to that upstream with the same credential, organization and project skip rejected gzip. Other credentials remain independent. `PROXY_GZIP_REJECT_TTL_S=0` restores a new probe for each request. Client-provided encodings are preserved and a second 415 is forwarded.

When enough real large unencoded requests are available, compression also considers CPU cost against a conservative estimate of upload savings. On a fast link it can keep a compressible body plain when compression would take longer than the upload time it saves; on a slow link gzip remains useful. `PROXY_GZIP_UPLOAD_MBIT_S=0` learns this hint from recent requests without making speed-test requests. A positive value supplies an explicit rate in Mbit/s. The measured interval includes upstream processing and RTT, so the automatic hint is a lower bound rather than an Internet speed measurement.

`/_health` reports whether compression is enabled, its minimum body size, `request_gzip_backend` (`isal` or `stdlib`), and `connection_pool_initialized`. `/_stats` includes `gzip_payloads`, `gzip_input_bytes`, and `gzip_output_bytes`: these count newly prepared compressed payload versions, so retry traffic is excluded. `gzip_fallbacks` counts HTTP 415 retries without proxy-added gzip; a successful recovery also increments `successful_recoveries`.

## Streaming buffers and independent quotas

Each SSE queue holds at most 1 MiB of data by default, split into pieces no larger than 64 KiB. Small chunks are delivered immediately. When a reader falls behind, the producer waits for room instead of growing the queue. Initial SSE inspection is also bounded by bytes and frame count; the proxy forwards a partial frame verbatim when inspection reaches its budget. Socket, HTTPX and request-body memory are additional to the queue budget.

Disconnect cleanup also covers a pending upstream send before response headers arrive and incomplete JSON/error bodies. The proxy checks for disconnect while those operations are waiting and cancels only the abandoned work, releasing its pool capacity. A response that finishes during the disconnect check is closed if it was never handed to the caller. Cancelled partial uploads are handled before starting upstream work.

A rate limit pauses requests using the same effective credential. This prevents one Windows user's key from delaying another user's independent key, while callers sharing a key still share recovery cooldown and launch staggering. Use `PROXY_RATE_LIMIT_SCOPE=shared` if different keys intentionally share one upstream quota. Credential fingerprints and caches are bounded; raw API keys are not retained in those caches.

See [runtime settings and measured results](docs/runtime-tuning.md) for limits, counters, and a paired comparison with the preceding `main` version.

## Latency tuning and validation

Version **1.8.0** receives client disconnect events after the Responses body is consumed, reuses one receive owner across retries and joins it before handing the response to Starlette. This removes the 350 ms upstream polling interval and avoids a Python 3.10 cancellation race in a self-cancelling disconnect check. Gzip policy, dependency pins, model settings and upstream request semantics are preserved.

The 154-test regression suite covers browser access guards, full stream lifetime accounting, settings conflicts, pause/drain/restart and verified updates/rollback, as well as the previous real-pool recovery tests. Four disposable Windows supervisor scenarios check successful browser installation, early worker exit, cold supervisor import failure and health timeout. See [1.8.0 browser control and measured validation](docs/browser-control-1.8.0.md).

Version 1.7.0 adds cancellation-safe 429 queues and a diagnostic CLI. Cancelled callers reserve no future launch intervals; old successes cannot erase newer rate limits. Millisecond hints are parsed correctly, authoritative hints are not shortened by the generated-backoff cap, and a cooldown exceeding the recovery budget returns 429 without an early retry.

~~~powershell
python doctor.py
python doctor.py --watch
python doctor.py --profile 60 --watch --duration 60
python doctor.py --export proxy-report.json
.\diagnose.ps1 -ProfileSeconds 60 -Watch -Duration 60
~~~

Detailed timing is off by default. `--profile` enables it for new operations without restart/config edits and expires automatically. `PROXY_TIMINGS=1` enables continuous capture after restart. Fixed aggregates distinguish upload, connection, quota/connection queues, upstream headers and SSE inspection/delivery. See [diagnostics and timing definitions](docs/diagnostics.md) for interval overlap and histogram upper bounds.

The [1.7.0 measured results](docs/recovery-1.7.0.md) show comparable ordinary latency, two controlled recovery examples, 125 passing regressions on three Python runtimes, and a 15-minute cancellation/profiling endurance run. Recovery from a scripted `150 milliseconds` hint improved from 15.209 s to 0.156 s; no general model-generation speedup is claimed.

Install the complete runtime and bundled `panel` assets; copying only `proxy.py` is insufficient. Browser updates maintain the installed manifest. Bootstrap/dependency changes require a reviewed administrator installation; do not run a separate Git updater alongside a browser installation.

Install all pinned dependencies with `python -m pip install -r requirements.txt`, including when updating an existing checkout. Uvicorn selects `httptools` with its default `--http auto` setting. The supplied launchers use this setting and prepare the asynchronous HTTP client before accepting requests; startup preparation does not send an upstream request or store credentials.

Keep `PROXY_VERBOSE=0` for normal use to suppress per-request informational logging. This is the example configuration's default; warnings and errors remain available. Set it to `1` while diagnosing a problem. For an upstream that accepts compressed requests, the optional gzip settings above reduce large uploads without changing the decoded request body.

Version 1.6.0 adds bounded SSE buffering, dedicated bounded gzip workers, scoped compression hints and cancellation cleanup before response headers arrive. Independent credentials share a proxy without sharing unrelated 429 cooldowns. Upstream admission keeps the existing 50-connection cap and repairs abandoned HTTPcore pool entries while preserving healthy keep-alive connections.

A fresh adjacent-pair comparison against preceding `main` (a5621cec) collected 360 samples per variant/scenario, with the same installed packages and no real provider calls:

| Scenario | Preceding main median | 1.6.0 median |
| --- | ---: | ---: |
| Tool-output history | 23.372 ms | 14.528 ms |
| Repeated gzip rejection | 22.932 ms | 13.791 ms |
| Short request | 5.414 ms | 5.527 ms |

These synthetic whole-loopback times include upstream validation. The tool-history gain trades extra plain upload bytes for less compression CPU on the learned fast link; slower-link checks retained gzip. Small healthy requests have a measurable fractional-millisecond overhead. See [runtime settings and measured results](docs/runtime-tuning.md) for p95, original-upstream comparison, source hashes, controlled recovery, endurance evidence and the command to reproduce the benchmark.

## Health, Diagnostics & Tests

[GitHub Actions](https://github.com/zerkavich/agentrouter-codex-proxy/actions/workflows/tests.yml) runs the test suite on Windows and Linux with Python 3.10 and 3.12 for pushes to `main`, pull requests, and manual runs. Tests use mock or loopback upstreams and require no API credentials.

- **Health check**: `http://127.0.0.1:8788/_health`
- **Browser panel**: `http://127.0.0.1:8788/` (also `/_control/`).
- **Proxy stats**: `http://127.0.0.1:8788/_stats`
  - Returns counts for total requests, mismatches caught, fallbacks applied, rate limits caught, pre-commit stream retries, successful recoveries, and pool timeouts.
- **Live diagnostics**: `http://127.0.0.1:8788/_diagnostics`; doctor.py / diagnose.ps1 support watch, temporary profiling and sanitized JSON export.
- **Logs**: When running as a service (`-Service` or via scheduled task), logs are stored in `logs/proxy.log` (automatically rotates at 1MB).
- **Tests**:
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-installer.txt
  .\.venv\Scripts\python.exe -m unittest discover -s tests -v
  ```
