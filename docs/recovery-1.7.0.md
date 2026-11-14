# Quota recovery and diagnostics in 1.7.0

Version 1.7.0 improves recovery after rate limits and adds status/watch/export with temporary profiling. Detailed timing is off by default, and ordinary requests install no HTTPcore trace callbacks. Existing streaming, keep-alive, compression and cancellation cleanup remain enabled.

## Recovery examples

Two controlled examples compared the unchanged installed 1.6.0 handler with the frozen 1.7.0 handler. Each variant ran once using an in-memory HTTP/ASGI fixture or a direct quota gate. These examples expose particular defects; they are not Internet benchmarks or model-generation measurements.

| Trigger | 1.6.0 | 1.7.0 |
| --- | ---: | ---: |
| Sixteen queued callers cancel, then one caller survives | 9.630 s additional wait | 0.000205 s additional wait |
| Error says `Please retry after 150 milliseconds`, then the next attempt succeeds | 15.209 s recovery | 0.156 s recovery |

Cancelled callers no longer reserve future stagger intervals. Only actual launches spend them. Old successful attempts cannot clear a newer 429 cooldown. Explicit Retry-After hints retain their full duration, even if this request cannot retry within its 45-second recovery budget; subsequent callers sharing that quota still observe the cooldown. Forwarded Retry-After uses a whole-second ceiling for the strongest current cooldown.

`rate_limit_wait_seconds_total` measures elapsed quota gate/backoff caller-time, including cancellation. Ten simultaneous callers waiting two seconds add approximately twenty seconds. The counter is not the latency of one request.

## Ordinary request latency

Both servers stayed warm on one Windows PC, using the same installed package versions and synthetic payloads. Four epochs collected 90 adjacent AB/BA pairs per fixture: 360 samples per variant/fixture, 2,160 pairs in total. All decoded bodies were verified. Temporary/continuous profiling was off. No real provider calls were made.

Times include local upload, synthetic upstream validation and delivery of the first output SSE event. They include operating-system scheduling and are not proxy CPU timings.

| Fixture | 1.6.0 median, ms | 1.7.0 median, ms | 1.6.0 p95, ms | 1.7.0 p95, ms |
| --- | ---: | ---: | ---: | ---: |
| Short request | 10.248 | 10.086 | 14.771 | 13.939 |
| English history | 20.129 | 19.773 | 31.982 | 32.366 |
| Russian history | 28.126 | 28.400 | 43.697 | 41.151 |
| Tool history | 26.687 | 26.222 | 40.181 | 41.786 |
| Tool history after HTTP 415 | 26.373 | 26.105 | 38.737 | 39.493 |
| Thirty images | 60.386 | 59.702 | 94.580 | 89.620 |

Ordinary latency remains comparable. Pair-level bootstrap intervals include zero for five of six fixtures; the short-request interval suggests only a small difference in this run. The median of adjacent differences is distinct from the difference between the two pooled medians: for tool history it was -0.200 ms, with a 95% interval of -0.812 to +0.377 ms. This release does not establish a general normal-request speedup. Host load and serial correlation limit inference, and absolute times from different runs should not be combined.

The benefit is faster recovery in the affected scenarios and diagnostics that help identify where an actual delay occurs. Timing stages overlap, include provider waiting, and show histogram bucket upper bounds for p50/p95; see [timing definitions](diagnostics.md).

## Endurance and compatibility

The exact final source ran for 901.641 seconds in one synthetic TCP/SSE proxy process. It completed 5,570 successful requests and 12,454 intentional cancellations with zero unexpected test errors. Thirty bursts each cancelled 384 clients before response headers. Fifteen temporary profiling windows expired normally. Mixed traffic exercised HTTP 415/429, deliberate TCP resets, server errors, resource mismatches, slow readers and compression.

At the final check, pool requests, admission waiters, quota references, compression jobs, timing-active counters, upstream streams and pending header/JSON waits were zero. All 50 admission slots were available, and one healthy idle keep-alive connection remained. Both proxy.py and telemetry.py retained their initial hashes. Peak sampled RSS was 199.847 MiB; it is a workload measurement, not a memory limit. This 15-minute check complements the earlier [1.6.0 endurance result](runtime-tuning.md#endurance-results); its duration is not added to that earlier run.

All 125 regression tests passed on Python 3.10, Python 3.12 and the installed embedded Python 3.12. A separate portable probe checked module colocation, native dependencies, original-byte forwarding, default-off tracing, profiling expiry, quota cleanup and the 50-slot pool.

The [measured data](assets/recovery-1.7.0-data.json) contains fixed inputs, hashes, aggregates and recovery examples. Previous failed/interrupted local receipts remain separate from passing evidence.

## Install and diagnose

Update the complete checkout/package, including telemetry.py, doctor.py and diagnose.ps1. Copying only proxy.py is insufficient. Install all pinned dependencies and use the [Windows installation/restart guide](windows-installation.md).

~~~powershell
.\diagnose.ps1
.\diagnose.ps1 -ProfileSeconds 60 -Watch -Duration 60 -ExportPath proxy-report.json
~~~

Start profiling before reproducing a delay. It expires automatically without a restart or config edit. Reports exclude credentials, credential hashes, upstream URLs, raw errors/logs, conversations and local paths, and refuse to overwrite an existing report.

To reproduce ordinary latency using a full clone and the pinned dependencies:

~~~powershell
python benchmarks/compare.py --baseline-ref 47430812ef4a4a9f24957d676d23fc1fb0945028 --samples 90 --epochs 4
~~~

The comparison freezes each variant in its own directory, including the matching telemetry.py when required. Local baselines need their matching module alongside the selected source; Git baselines read both files from the same revision. Every runtime module hash is checked again at completion.
