# Interrupted response cleanup and pool ownership (1.10.1)

When cancellation or an exception interrupts protocol cleanup, HTTPcore can
retain an ACTIVE connection together with its assigned pool request. Its
PoolByteStream and the HTTPX response have already marked themselves closed, so
calling `response.aclose()` again does not finish removing the pool request.
Enough abandoned records fill the connection pool: the local health endpoint
continues responding, while upstream sends repeatedly fail with `PoolTimeout`.

Version 1.10.1 records a private per-send capacity lease in request extensions.
When HTTPcore next assigns its queue, the proxy removes records whose own lease
has been released and reclaims their unowned busy connections under the pool's
normal lock and close path. A fresh extensions dictionary keeps ownership
separate if an HTTPX Request is reused. Normal keep-alive reuse is preserved.
Still-running response closes, active streams, legitimate TCP/TLS connects and
untracked callers retain their assignments. No worker/client-wide reset is used.

The marker retains a small lease object, not an additional body, header or trace
copy. Existing large-body buffers, memory admission, compression settings,
dependency pins and provider configuration are unchanged. This fix repairs the
reproduced ownership fault; a genuine upstream/network failure or a pool full of
legitimate active requests can still produce a timeout.

## History and diagnostics

Request attempts include `pool_recovery`, displayed as **Abandoned connection
cleanup** / **Освобождение брошенных соединений**. The existing latest-16-events
bound and lazy loading remain. A final PoolTimeout-generated HTTP 502 has the
fixed history error code `upstream_pool_timeout`; successful retries do not keep
that failure code.

Sanitized diagnostics/reports expose these numeric fields:

- `connection_pool.requests`: all HTTPcore request records in the shared pool.
- `connection_pool.queued_requests`: records waiting for a connection.
- `connection_pool.released_requests`: proxy-owned records with released leases
  which the next queue assignment can reclaim.
- `counts.upstream_orphaned_requests_reclaimed`: cumulative reclaimed records.
- Existing reclaimed connecting/connection counters now survive report export.

These are process-lifetime counters and current snapshots; they do not measure
RAM savings or prove that the worker is currently blocked. They contain no
request bodies, headers, credentials or raw upstream errors. The history schema
and settings format are unchanged.

The saved metrics' `errors` field now uses the worker's recorded final failures
instead of an absent upstream counter. It counts final HTTP 5xx and failed
streams while history is enabled; recovered attempts, client cancellations and
controlled `proxy_paused` responses are excluded. The counter starts with each
worker and remains independent of automatic profiling. Previously stored metric
points are preserved; their missing error counts are not reconstructed.

The payload sanitizer previously wrote one informational log line for each
dropped server-state item, even with verbose logging disabled. It now writes at
most one count-only summary when verbose logging is enabled. Session-pointer
values are omitted from that summary. Warnings and transport errors remain.

## Reproducing the fault

`tests/test_pool_ownership.py` uses the pinned HTTPX/HTTPcore packages and a real
local TCP server. It interrupts the awaited protocol close rather than manually
deleting or inventing a pool request. The initial five-test control on 1.10.0
had four errors (`PoolTimeout`); the still-running close control passed.

The final regression controls verify:

- A full pool of abandoned request records becomes usable with the same client.
- A failed close leaves a different active response connected.
- Legitimate untracked HTTPX callers keep their connections.
- Sixty interrupted closes leave no accumulated requests or open server sockets.
- A close still in progress retains its ownership.
- Reusing one HTTPX Request does not retire an earlier live response.
- An exhausted pool error gets a fixed history code.

Run in an isolated environment with the repository's pinned packages:

```powershell
python -m unittest discover -s tests -p test_pool_ownership.py -v
python -m unittest discover -s tests -v
python benchmarks/compare.py --baseline-ref 57a28f2816cd5732d3d658a1584e3508786165bd --samples 60 --epochs 3 --output benchmark-results/pool-ownership-new
```

Choose a fresh benchmark directory. Local synthetic controls do not measure
Internet latency or model generation. Use [browser management](browser-control.ru.md)
or the [Windows installation guide](windows-installation.md) to update/restart.

## Ordinary HTTP/SSE control

Two continuously warm local workers ran 1,080 adjacent AB/BA pairs against
frozen 1.10.0: six payload fixtures, three epochs and 60 pairs per fixture per
epoch. Every synthetic upstream body was hash-verified; pinned packages and
settings matched. The measured runtime hashes match the release source.

| Scenario | Median, ms: 1.10.0 / 1.10.1 | p95, ms: 1.10.0 / 1.10.1 |
| --- | ---: | ---: |
| Short request | 3.890 / 3.896 | 5.093 / 5.395 |
| English Responses history | 7.617 / 7.578 | 13.890 / 10.794 |
| Russian Responses history | 10.402 / 10.427 | 19.369 / 15.636 |
| Tool outputs | 9.938 / 9.945 | 16.453 / 16.398 |
| Tool outputs / gzip rejected | 9.391 / 9.508 | 18.535 / 18.322 |
| Thirty images | 17.270 / 17.163 | 30.639 / 28.170 |

The short-request median was 3.890 / 3.896 ms, essentially unchanged.
Other fixture medians changed by about -0.11 to +0.12 ms; some p95s increased.
These figures measure the complete local synthetic path, including upstream
validation, not Internet or model generation. There is no universal ordinary
request speedup claim. The benefit is recovery from the reproduced pool fault.

The final isolated Python 3.10.11 scoped suite passed 65 pool/cancellation/
history tests. The browser control passed RU/English labels and native
keyboard expansion. See [regression summary](evidence/1.10.1/regressions.json),
[paired latency](evidence/1.10.1/latency.json),
[measurement receipt](evidence/1.10.1/latency-receipt.json),
[Python 3.10](evidence/1.10.1/python310.json) and
[browser control](evidence/1.10.1/browser.json). Detailed local raw pairs and
logs are retained separately; the published JSON contains numeric aggregates,
source hashes and synthetic control results. The repository
[CI workflow](../.github/workflows/tests.yml) covers Windows/Linux with
Python 3.10/3.12 and reproduces the committed panel assets.
