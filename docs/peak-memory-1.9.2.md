# Recovery memory and background updates: 1.9.2

Large modified requests use less temporary memory. Update checks and package preparation return promptly as background jobs, so their completion no longer depends on one long browser HTTP request.

## Modified request encoding

Healthy requests keep their original bytes and conversation-state fields. Only image pruning and stateless recovery rebuild a payload. Small rebuilt bodies retain the existing C JSON encoder.

For source bodies of at least 1 MiB, the serializer writes to a byte buffer. Groups of containers still use the C encoder; long strings are escaped in 64-Ki-character slices. A capped structural probe sizes small replacements; larger outputs reserve from the source size plus rounding, avoiding a second full message traversal. This also avoids repeatedly reallocating and copying the growing output. Group delimiters are removed through a memory view rather than another string copy.

Once pruning/recovery has a replacement tree, the handler releases original replay bytes and removed state items before serialization. After recovery, retries reuse the corrected bytes and release the parsed trees. An open committed SSE response releases its large upload as in 1.9.1.

Key order, numeric formatting, escaping and Unicode remain compatible with the previous serializer. Escaped lone surrogates retain the previous whole-body ASCII fallback. This reduces transient allocations; it does not impose prompt limits or a total process-memory cap. Parsing a large JSON body still has its own allocation peak.

## Background update jobs

`POST /_control/api/updates/check` and a new `updates/prepare` return HTTP 202 with an `action`, `job_id`, `started_at` and `state`. Follow the existing status endpoint's `management.update` until `checking/preparing` becomes `checked/prepared/failed`. An already prepared same commit returns HTTP 200. Repeated clicks join a pending identical job; conflicting actions return HTTP 409. Restart/install cannot overlap preparation.

The worker owns one task and its lock until threaded filesystem/network work finishes, including normal shutdown. Navigation or a closed tab does not cancel it. The independent panel retains its two-second status cache; it does not add a separate polling loop or automatic GitHub checks. RU/English pending states, failure messages and a preparation retry appear in the panel. Installation keeps its separate confirmation.

Packages are extracted and probed in a disposable staging folder. Only a verified complete package is atomically renamed to its commit folder. Failed attempts leave no committed cache entry, and retry can download a fresh package. Existing corrupted cached packages are still refused. Dependency pins, the protected recovery bootstrap, manifest hashes and all four exact-commit CI checks remain required.

## Measurement method

Baseline: `418e4e3588068ddca14b5c0e1816a2b505d3038f` (1.9.1). Frozen credential-free runtimes send approximately 32 MiB over actual loopback TCP. The synthetic upstream hashes each upload, deliberately returns a resource mismatch in the recovery cases, waits before headers, and holds an SSE response open. An ASGI callback supplies the incoming body. No provider/model calls are made.

Memory is measured with tracemalloc, including the synthetic incoming body and Python allocations; it is separate from RSS, private bytes and OS socket buffers. Timings disable tracing. Six adjacent AB/BA process pairs each run ten timing repetitions after warming the HTTPcore backend. The cases include ASCII images, a long Unicode text and thousands of text messages. These measurements cover local payload/transport work, not Internet or model generation latency.

## Measured results

Live Python allocations in MiB; medians of six runs per variant. Healthy paths are included as controls.

| Case | Peak before | Peak after | Waiting for headers before / after |
| --- | ---: | ---: | ---: |
| One image | 64.01 | 64.01 | 32.19 / 32.19 |
| 30 images | 64.01 | 64.01 | 32.19 / 32.19 |
| Plain text | 64.00 | 64.00 | 32.19 / 32.19 |
| Recovery, one image | 144.19 | 96.19 | 64.34 / 32.34 |
| Recovery, Unicode text | 269.36 | 224.18 | 64.34 / 32.34 |
| Recovery, thousands of messages | 134.90 | 100.70 | 64.36 / 32.35 |

Response-ready handler time in milliseconds, with tracing disabled:

| Case | Before median | After median | Paired median saved |
| --- | ---: | ---: | ---: |
| One image | 77.669 | 75.868 | +0.823 |
| 30 images | 83.079 | 77.177 | +1.579 |
| Plain text | 92.775 | 92.863 | -0.117 |
| Recovery, one image | 315.863 | 301.420 | +12.257 |
| Recovery, Unicode text | 375.258 | 347.190 | +24.071 |
| Recovery, thousands of messages | 393.848 | 409.394 | -11.866 |

The paired median uses each adjacent process pair and need not equal the difference of the variant medians. Negative saved values indicate a slowdown in this run. Windows scheduling varied substantially; these six pairs do not establish an improvement for every workload.

Whole-loopback first-output delivery with gzip enabled, 180 adjacent AB/BA pairs per case:

| Case | Before median, ms | After median, ms | Paired median saved, ms | 95% pair-bootstrap interval, ms |
| --- | ---: | ---: | ---: | ---: |
| Short input | 5.153 | 5.246 | -0.044 | -0.229 to +0.055 |
| English history | 9.429 | 9.625 | -0.139 | -0.720 to +0.177 |
| Russian history | 13.630 | 13.578 | -0.137 | -0.556 to +0.080 |
| Tool history | 13.160 | 13.471 | -0.211 | -0.636 to +0.315 |
| Tool history, gzip rejected | 12.411 | 12.350 | -0.071 | -0.513 to +0.288 |
| 30 images | 24.454 | 24.344 | -0.027 | -0.629 to +0.271 |

These local timings are separate from provider latency. Pair bootstrapping can retain serial correlation. Memory improvements in this release apply to modified/recovered bodies; normal streams retain the lifetime improvements already shipped in 1.9.1.

An isolated real browser passed 12 update scenarios: HTTP 202 acceptance, reload/navigation, API responsiveness, pending controls and restart blocking, failure/retry, RU/English persistence, mobile layout and installation confirmation. Synthetic check/preparation tasks took 5/6 seconds; their immediate response times were check: 68.0 ms, prepare: 47.8 ms, prepare-retry: 35.3 ms. These measure acceptance, not completion or GitHub download speed.

Allowlisted evidence with runtime hashes: [large-body comparison](evidence/1.9.2/body-lifetime.json), [loopback latency](evidence/1.9.2/latency.json), [browser checks](evidence/1.9.2/browser.json).

## Reproduce

Install the repository requirements, then use a new output directory:

```powershell
python benchmarks/body_lifetime.py --baseline-ref 418e4e3588068ddca14b5c0e1816a2b505d3038f --samples 6 --timing-samples 10 --size-mib 32 --output benchmark-results/recovery-1.9.2
python benchmarks/compare.py --baseline-ref 418e4e3588068ddca14b5c0e1816a2b505d3038f --samples 90 --epochs 2
python -m unittest discover -s tests -v
```

`body_lifetime.py --cases ...` can select individual fixtures. Both comparison tools preserve sources, raw measurements and hashes in the new output folder.
