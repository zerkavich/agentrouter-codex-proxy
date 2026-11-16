# Large request memory: 1.9.1

Large uploads no longer stay attached to an open upstream SSE response. A conservative image check also avoids decoding an entire JSON body when the image count is below the configured limit.

## Request lifetime

For bodies of at least 1 MiB, each upstream attempt uses an upload stream that releases its reference after sending. HTTPX and HTTPcore can retain that stream's metadata without retaining the uploaded bytes for the duration of the response. Smaller uploads retain the previous bytes path.

The handler keeps the replayable bytes until SSE priming commits the response. Connection errors, resource mismatches and HTTP 415 gzip recovery can still rebuild or resend the request. After commit, the handler releases its data and clears Starlette's large body cache while retaining the attribute marking body consumption.

Image inspection counts an upper bound from quoted types and keys. Adjacent type and `image_url` fields in the same object count together. String values cannot contain additional image objects, so inspection skips them without copying their contents. An escaped quote ends this shortcut early; the rest of the bytes are inspected conservatively. Unicode escapes in types or keys still cause full parsing. Ambiguous or over-limit requests keep the existing pruning path and oldest-image replacement order.

When pruning removes images, recovery retains the current pruned tree instead of the original tree containing removed images. After stateless recovery is serialized, subsequent attempts reuse the bytes and release both decoded trees. Healthy requests retain their original bytes, Unicode, number precision and upstream cache eligibility.

## Measurement method

The comparison uses baseline commit `4353d3c4cf7a37fc552a0309247f688f63a35fa5` and identical synthetic requests of approximately 32 MiB. An actual loopback TCP upstream reads in 64 KiB pieces, verifies upload SHA-256 hashes, waits before sending headers and then keeps an SSE stream open. The incoming Starlette request is supplied by a synthetic ASGI receive callback; these measurements are not an entire browser-to-provider path.

Memory measurements track live Python allocations, including the synthetic incoming body, after warming HTTPcore's backend. They are separate from RSS, private bytes and operating-system socket buffers. Timing runs disable tracemalloc and release completed requests between repetitions. Each of six adjacent AB/BA process pairs performs ten untraced uploads per case; timings summarize those worker medians.

Proxy-added gzip is disabled for these large-body measurements. The image fixtures repeat base64-shaped ASCII and do not make real image/model calls. The resource-mismatch fixture receives a deliberate HTTP 400 and verifies the sanitized second upload. These cases measure transport and payload handling, not provider generation or Internet latency. Full JSON parsing and serialization still have temporary allocation peaks when recovery or pruning is required.


## Measured results

Live Python allocations in MiB; medians of six isolated runs per variant:

| Request | Waiting for headers, before / after | Open SSE, before / after |
| --- | ---: | ---: |
| One image | 64.17 / 32.19 | 32.17 / 0.19 |
| 30 images | 64.18 / 32.19 | 32.18 / 0.19 |
| Plain text | 32.17 / 32.19 | 32.17 / 0.19 |
| Resource mismatch | 96.32 / 64.34 | 64.24 / 0.25 |

For the one-image and 30-image cases, Python allocation peaks fell from about 96 to 64 MiB. Recovery peaks stayed around 144 MiB because that path still decodes and serializes the JSON. The change reduces how long allocations remain live; it does not impose an overall process-memory cap.

Untraced handler response-ready time in milliseconds for the same 32-MiB fixtures:

| Request | Before median | After median | Paired median saved |
| --- | ---: | ---: | ---: |
| One image | 108.785 | 73.908 | +33.491 |
| 30 images | 109.995 | 74.880 | +35.005 |
| Plain text | 93.846 | 90.475 | -1.640 |
| Resource mismatch | 312.329 | 315.962 | -9.430 |

The paired median is computed from each process pair and need not equal the difference of the two variant medians. Image requests improved in this run; plain text and recovery primarily improved memory. Recovery did not show a speedup: its paired results ranged from about 39 ms faster to 15 ms slower, with a 9.43 ms paired median slowdown. Six process pairs are insufficient to promise unchanged speed for every workload.

Whole-loopback first-output delivery with gzip enabled, 180 adjacent AB/BA pairs per case:

| Request | Before median, ms | After median, ms | Paired median saved, ms |
| --- | ---: | ---: | ---: |
| Short input | 10.815 | 10.684 | +0.054 |
| English history | 17.499 | 17.448 | -0.125 |
| Russian history | 22.794 | 22.860 | -0.161 |
| Tool history | 21.585 | 22.503 | -0.472 |
| Tool history, gzip rejected | 20.887 | 21.303 | -0.386 |
| 30 images | 43.268 | 36.369 | +6.418 |

The 30-image paired median improved by 6.42 ms (16.01%), with a pair-bootstrap 95% interval of 5.82-6.86 ms. Short/English/Russian intervals included zero. Tool history and cached gzip-rejection cases added paired medians of about 0.47 and 0.39 ms; their intervals excluded zero in this run. These local differences are separate from provider latency, and bootstrap samples can have serial correlation.

Allowlisted results with runtime hashes: [body lifetimes](evidence/1.9.1/body-lifetime.json) and [loopback latency](evidence/1.9.1/latency.json).

## Reproduce

From a full clone with `requirements.txt` installed:

```powershell
python benchmarks/body_lifetime.py --baseline-ref 4353d3c4cf7a37fc552a0309247f688f63a35fa5 --samples 6 --timing-samples 10 --size-mib 32 --output benchmark-results/body-check
```

The output must be a new directory. It retains both runtime snapshots, the benchmark source, raw paired results, logs and a receipt. The controller clears inherited API credentials and proxy tuning in its isolated child environment. Temporary ports avoid the installed proxy, and frozen source hashes are checked at completion.

For the existing whole-loopback short/history comparison:

```powershell
python benchmarks/compare.py --baseline-ref 4353d3c4cf7a37fc552a0309247f688f63a35fa5 --samples 90 --epochs 2
```

Regression coverage includes real TCP replay after a reset, HTTP and SSE resource mismatches, gzip rejection, release of decoded trees before response headers and release of uploaded bodies during an open SSE stream. A deterministic 600-fixture differential check ensures the image shortcut never skips pruning required by the existing implementation, including mixed content/output fields, top-level screenshots, escaped keys/types and quoted text.
