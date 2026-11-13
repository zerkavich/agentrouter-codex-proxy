# AgentRouter Codex Responses Compatibility Proxy

A lightweight local compatibility proxy that fixes cross-resource session errors when using **Codex Desktop** or **Codex CLI** with **AgentRouter**.

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
3. **Pre-Commit SSE Priming & Rate-Limit Recovery**: Buffers initial `200 OK` `text/event-stream` setup frames long enough to catch early `rate_limit_exceeded` / `response.failed` / empty streams before committing headers to Codex, coordinates a shared cooldown + stagger across concurrent subagent calls, and honors `Retry-After` / reset hints before retrying.
4. **Windows & DNS Resilience**: Provides resilient DNS fallback (`8.214.160.125`, `8.214.161.192`), TCP keep-alive hooks, and a Python 3.12 Windows `ProactorEventLoop` `_call_connection_lost` guard (`WinError 10054`).
5. **Codex Keeps Streaming**: Codex receives clean `text/event-stream` tokens and `: ping` heartbeats with far fewer interruptions.
6. **Cancellation Cleanup**: Releases upstream responses even when a client disconnects or downstream delivery fails, preventing leaked connections from exhausting HTTPX's pool.
7. **Native Acceleration**: Uses ISA-L gzip and Uvicorn's native HTTP parser, prepares the connection pool during startup, and avoids redundant copying and timer allocation while draining buffered SSE data.

---

## Setup & Usage

For a complete walkthrough, see the [Windows installation guide](docs/windows-installation.md): Python and Git, Codex configuration, per-user or machine-wide startup, health checks, restart, and rollback.

### 1. Requirements
- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`, `httpx`, `isal`, `httptools` (installed automatically by `start-proxy.ps1` or `service.py` when missing)

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
```

> [!NOTE]
> Ensure your `CODEX_GATEWAY_API_KEY` (or `AGENTROUTER_API_KEY`) is configured in your environment or in `.codex/.env`.

---

## Request forwarding and optional compression

Responses requests keep their original bytes until image pruning or resource-mismatch recovery changes the payload. Retries reuse the prepared body. This avoids repeated JSON parsing, copying, and serialization, and preserves UTF-8 text and numeric precision for unchanged requests.

For an upstream that accepts gzip request bodies, enable compression in the proxy's `.env`:

```dotenv
PROXY_REQUEST_GZIP=1
PROXY_REQUEST_GZIP_MIN_BYTES=262144
```

Compression is disabled by default. Large requests are sampled for compressibility before ISA-L gzip level 1 runs in a worker thread; poorly compressible data keeps its original encoding. Direct Python usage falls back to standard-library gzip when ISA-L is unavailable. The compressed body is reused across retries and rebuilt after recovery changes the payload. Existing `Content-Encoding` headers are preserved. Restart the proxy after changing these settings.

If the upstream rejects proxy-added gzip with HTTP 415, the proxy retries once using the same decoded payload and original headers. That request stays uncompressed during subsequent retries and resource-mismatch recovery. Client-provided encodings are preserved, a second 415 is forwarded, and compression remains enabled for other requests. Set `PROXY_REQUEST_GZIP=0` and restart if the upstream consistently rejects compression, to avoid the extra rejected attempt.

`/_health` reports whether compression is enabled, its minimum body size, `request_gzip_backend` (`isal` or `stdlib`), and `connection_pool_initialized`. `/_stats` includes `gzip_payloads`, `gzip_input_bytes`, and `gzip_output_bytes`: these count newly prepared compressed payload versions, so retry traffic is excluded. `gzip_fallbacks` counts HTTP 415 retries without proxy-added gzip; a successful recovery also increments `successful_recoveries`.

## Latency tuning and validation

Install all pinned dependencies with `python -m pip install -r requirements.txt`, including when updating an existing checkout. Uvicorn selects `httptools` with its default `--http auto` setting. The supplied launchers use this setting and prepare the asynchronous HTTP client before accepting requests; startup preparation does not send an upstream request or store credentials.

Keep `PROXY_VERBOSE=0` for normal use to suppress per-request informational logging. This is the example configuration's default; warnings and errors remain available. Set it to `1` while diagnosing a problem. For an upstream that accepts compressed requests, the optional gzip settings above reduce large uploads without changing the decoded request body.

The following measurements compare the previous payload-reuse implementation with native acceleration on one Windows PC, using the complete loopback HTTP/SSE path and a synthetic upstream. Both variants used optional request gzip; the native variant also used minimal logging. The first request was measured once per server launch; other rows are medians of 40 requests after warm-up.

| Scenario | Previous implementation | Native acceleration |
| --- | ---: | ---: |
| First request after server startup | 688.4 ms | 18.8 ms |
| Large tool history | 49.5 ms | 28.9 ms |
| Repeated short request | 11.4 ms | 12.0 ms |

These numbers measure local time to the first output event, not Internet or AgentRouter response time, and are not a guarantee for another machine. Cancellation checks also covered 60 interrupted streams with no remaining upstream requests or pool timeouts, followed by a successful request.

## Health, Diagnostics & Tests

[GitHub Actions](https://github.com/zerkavich/agentrouter-codex-proxy/actions/workflows/tests.yml) runs the test suite on Windows and Linux with Python 3.10 and 3.12 for pushes to `main`, pull requests, and manual runs. Tests use mock or loopback upstreams and require no API credentials.

- **Health check**: `http://127.0.0.1:8788/_health`
- **Proxy stats**: `http://127.0.0.1:8788/_stats`
  - Returns counts for total requests, mismatches caught, fallbacks applied, rate limits caught, pre-commit stream retries, successful recoveries, and pool timeouts.
- **Logs**: When running as a service (`-Service` or via scheduled task), logs are stored in `logs/proxy.log` (automatically rotates at 1MB).
- **Tests**:
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -v
  ```
