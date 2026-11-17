# Incoming body memory and request attempts (1.10.0)

Large incoming Responses requests no longer retain a list of every received
chunk before joining it. Single chunks are reused; multi-chunk bodies use a
BytesIO buffer. Known sizes up to 64 MiB are preallocated, while unknown or larger
sizes grow geometrically and then in bounded 8 MiB steps. Normal requests retain
their exact JSON bytes, including Unicode, number precision and formatting.

## Memory admission and its cost

Requests of at least 1 MiB reserve an estimate of three times their plain-body
size before their main receive/transform work. FIFO admission coordinates large
requests across users of one worker. Smaller requests bypass this queue.

| Setting | Default | Meaning |
| --- | ---: | --- |
| `PROXY_BODY_BUDGET_BYTES` | `268435456` | 256 MiB aggregate reservation estimate; `0` disables admission. |
| `PROXY_BODY_BUDGET_WAIT_S` | `60` | Maximum admission wait; browser range 1–300 seconds. |

Set these in **Settings → Connection**, save and restart the worker. Existing
saved values and `.env` remain the base configuration. The overview shows reserved
bytes and queue length. `/_health`, `/_diagnostics` and sanitized reports include
`body_memory` with the limit, current/peak reservation, waiters, waits and timeouts.

This is **not a process RSS limit**. An oversized request runs alone; unknown
lengths become exclusive after a bounded prefix crosses 1 MiB. HTTP parser/socket
buffers, small calls, decoded JSON, compression jobs, browser and supervisor
memory are outside a strict cap. Multiple worker processes have independent
budgets. Queueing may intentionally increase the latency of large parallel calls.

After proxy-added gzip or pruning, the reservation shrinks. Gzip recovery reserves
plain-body capacity again before inflation; upgrades release partial credits to
avoid deadlock. Completion, cancellation and admission timeouts release credits.
Incoming admission prefetches at most one body event and replays it once, rather
than buffering a queued upload. A completed prefetched body can wait on its next
disconnect event. Partial uploads use transport backpressure; an unobserved peer
disconnect may keep a waiter until admission or its timeout.

A timed-out wait returns HTTP 503 with `error.type = proxy_memory_busy` and
`Retry-After: 2`. Increase the budget or set it to zero if maximum large-request
parallelism matters more than peak memory. This is separate from upstream quota
503 responses and existing pool admission.

## Attempts in browser history

Expand **Attempts and recovery** under a completed request to see upstream HTTP
responses, transport failures, retry/quota waits, gzip fallback, resource recovery
and memory waits. Russian and English use the same fixed event codes. Details are
loaded only on expansion; ordinary request pages keep their existing payload size.

Each record retains the latest 16 events and a total event count. Stored events
contain a fixed reason code, status and numeric duration/scheduled wait. They store
no raw upstream error text, headers, credentials or prompt/response content. Event
durations can overlap the existing request stages; scheduled wait is not elapsed
time and the event list is not a sum of pure proxy CPU. Active-call details appear
after completion, and requests from older versions can have no details.

The additive `request_attempts` SQLite table keeps schema version 2. The previous
1.9.3 writer was tested against this database: old reads/writes work and existing
candidate details survive rollback. On upgrade, stale details from old-runtime
pruning are removed. History row, disk, queue and detail bounds remain in effect.
The independent 8789 panel can read saved details while the worker is unavailable.

`session binding invalidated` is recorded with guidance to start a new Codex task.
The gateway can no longer serve that model/session binding; this release does not
rotate session IDs automatically. HTTP 400, JSON 200 errors and inspected early
SSE errors are covered. It is different from the cross-resource mismatch for
which the existing stateless-payload recovery applies. Project files survive a
new task; the new task still needs the relevant context from the previous one.

## Measured incoming allocation

Frozen 1.9.3 and candidate runtimes used the same pinned installed packages on
Windows 11 / Python 3.12.6, with synthetic fresh 64 KiB receive chunks. Five runs
per size/length used tracemalloc for allocation and separate runs without it for
timing. SHA-256 checked every assembled body. These measure body assembly only.

| Body | Python allocation peak: 1.9.3 / 1.10.0 | Assembly median: 1.9.3 / 1.10.0 |
| --- | ---: | ---: |
| 1 MiB / known length | 2.00 / 1.19 MiB | 0.91 / 0.45 ms |
| 1 MiB / unknown length | 2.00 / 1.19 MiB | 0.85 / 0.47 ms |
| 4 MiB / known length | 8.01 / 4.19 MiB | 3.45 / 1.75 ms |
| 4 MiB / unknown length | 8.01 / 4.19 MiB | 3.36 / 2.42 ms |
| 16 MiB / known length | 32.03 / 16.19 MiB | 14.30 / 7.09 ms |
| 16 MiB / unknown length | 32.03 / 16.19 MiB | 14.73 / 12.07 ms |
| 32 MiB / known length | 64.06 / 32.19 MiB | 29.84 / 14.39 ms |
| 32 MiB / unknown length | 64.06 / 32.19 MiB | 29.61 / 35.24 ms |

At 32 MiB with a known length, peak Python allocation halved and assembly time
fell by about 52%. The unknown-length 32 MiB case retained half the memory but
was about 19% slower in this final run. Smaller unknown-length cases improved.
Allocator behavior and Windows background load affect timings; there is no
universal latency improvement for every body shape.

## Concurrent real TCP control

A separate worker received eight concurrent 32 MiB plain JSON uploads over real
loopback HTTP and forwarded them to a synthetic streaming hash reader. The
upstream held each large request for one second; short calls ran throughout.
History was enabled and gzip disabled, making uncompressed retention visible.
The client ran outside the measured worker. Three epochs reversed variant order
in the middle epoch; each variant used a fresh process. Memory and timing runs
were separate. Values below are medians of three per-run peaks/p95s/makespans.

| Runtime | Python allocation peak, MiB | Sampled worker RSS peak, MiB | Short-call p95, ms | Whole batch, s |
| --- | ---: | ---: | ---: | ---: |
| 1.9.3 | 289.20 | 326.52 | 31.94 | 3.22 |
| 1.10.0, admission off | 259.99 | 322.05 | 35.15 | 2.93 |
| 1.10.0, default budget | 67.24 | 128.73 | 16.02 | 6.83 |

Default admission lowered Python allocation peak by about 77% and sampled worker
RSS by about 61%, with two large requests admitted at a time. Short-call p95 was
about 50% lower, while the whole batch took about twice as long. Admission-off
1.10.0 kept eight-way parallelism and finished about 9% sooner in this control;
sampled RSS was similar to 1.9.3. RSS is a sampled process measure and Python
allocation peaks are not interchangeable with RAM savings in arbitrary workloads.

## Ordinary HTTP/SSE latency

Two continuously warm servers ran 2,160 adjacent AB/BA pairs: six fixtures,
four epochs and 90 pairs per fixture/epoch. Every decoded upstream body was
hash-verified. Optional gzip was enabled for both versions with the same adaptive
policy; the incoming budget used its candidate default. No real model calls.
Times include the entire loopback upload, synthetic validation and first output.

| Scenario | Median, ms: 1.9.3 / 1.10.0 | p95, ms: 1.9.3 / 1.10.0 |
| --- | ---: | ---: |
| Short request | 5.065 / 5.170 | 7.346 / 7.795 |
| English history | 9.231 / 9.163 | 14.968 / 15.754 |
| Russian history | 13.143 / 12.857 | 23.175 / 22.947 |
| Tool outputs | 12.757 / 12.767 | 23.526 / 23.698 |
| Tool outputs / gzip rejected | 12.289 / 12.240 | 21.055 / 23.378 |
| Thirty images | 24.152 / 22.954 | 41.873 / 36.051 |

The short-request median increased by roughly 0.1 ms; larger fixtures ranged from
almost unchanged to about 1.2 ms lower for thirty images. These figures do not
measure Internet/model generation and do not establish a major speedup for normal
small calls. The main change is less retained/peak memory and clearer diagnosis.

## Upstream HTTP/2

Two certificate-verified TLS 1.3 probes to `agentrouter.org:443` on 2026-11-17
selected `http/1.1` both when offering `h2,http/1.1` and when offering only
`http/1.1`. No HTTP requests, API credentials or model calls were used. This
checks the reached frontend's current ALPN capability, not every edge, route or
HTTP/2 performance. HTTP/1.1 and dependency pins remain in place; enabling HTTP/2
has no demonstrated benefit for this reached route.

## Reproduce and inspect

Install the repository's pinned packages in an isolated environment, then run:

```powershell
python benchmarks/incoming.py --baseline-ref bbad7506f5cda85df5fad5781c508bdb78005854 --output benchmark-results/incoming-new
python benchmarks/compare.py --baseline-ref bbad7506f5cda85df5fad5781c508bdb78005854 --samples 90 --epochs 4 --output benchmark-results/latency-new
python -m unittest discover -s tests -v
npm ci --prefix panel-src --no-audit --no-fund
npm run build --prefix panel-src
```

Select new output directories; existing evidence is preserved. The incoming
harness stores frozen runtime hashes, allocator/worker RSS measurements, queue
cleanup and exact upstream hash checks. Default settings are part of the tested
candidate. [Raw incoming evidence](evidence/1.10.0/incoming.json),
[summary](evidence/1.10.0/incoming-summary.json),
[paired latency](evidence/1.10.0/latency.json),
[TLS capability](evidence/1.10.0/http2-alpn.json),
[actual rollback writer](evidence/1.10.0/rollback-history.json) and
[browser smoke](evidence/1.10.0/browser.json) are credential-free.

For installation/restart/rollback use the [Windows guide](windows-installation.md).
Existing models, reasoning settings, provider credentials, dependency pins and
the protected update bootstrap are unchanged by this release.
