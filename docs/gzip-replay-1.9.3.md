# Gzip replay and bounded JSON processing (1.9.3)

Compared with 1.9.2, this release reduces retained memory while compressed
requests wait for quota or upstream response headers, avoids whole-document
Unicode strings during recovery of large contiguous values, reduces history
encoding work, and corrects the upload-rate estimate used by adaptive gzip.

## Request lifetime

After the proxy adds gzip, compressed bytes are the replay representation. The
plain request and receive cache are released before admission and header waits.
Transport, rate-limit and early SSE retries reuse the same gzip bytes. HTTP 415
and stateless recovery reconstruct the exact current plain bytes only when
needed, using the known size and existing ISA-L backend, with a zlib fallback.
Both reconstructors verify the gzip trailer. Client-provided encodings keep
their existing forwarding behavior.

An upstream gzip rejection still incurs one reconstruction; capability rejection
is cached using the existing credential/project/origin key and expiry policy.
The savings apply during waiting, not to the initial peak of receiving a healthy
large upload. Uncompressed healthy requests retain their existing memory path.

## JSON and long histories

For large documents with a long string found by a capped 128-string probe,
processing uses the stdlib C decoder in bounded groups. Long value strings use
64-KiB UTF-8 pieces. Valid unescaped pieces are already canonical JSON interiors;
escaped pieces are decoded and normalized. These copied byte fragments are sent
without assembling a large Python Unicode string or encoding it again.

Validation occurs before pruning or sanitization. Numeric precision, nonfinite
numbers, duplicate-key order, UTF-8, escape handling and the existing global
ASCII fallback for lone surrogates are preserved. Keys remain ordinary strings.
Dense histories and small bodies keep the whole-document C decoder; unusually
deep inputs fall back to it to preserve its acceptance boundary.

The span cache retains at most 128 pairs of offsets. History sizing handles
string leaves directly. The grouped C encoder is reused and skips circular
markers for these acyclic trees originating in JSON. No prompts, message counts
or image limits are reduced to obtain these results.

## Adaptive compression

Upload observations now measure HTTPcore `send_request_body.started` through
`send_request_body.complete`, excluding connection admission, DNS/TLS and server
header waiting. Only successful, large, unencoded real responses train the
existing bounded cache. A sample needs at least 512 KiB of plain input. Traces
are isolated per attempt and closed on failure or cancellation. HTTP/1.1 has
real TCP coverage; HTTP/2 event and diagnostic
mapping behavior has unit coverage.

Socket buffering and TLS work remain part of this estimate. It is not a link
bandwidth test. Automatic estimates are capped at 1,000 Mbit/s to keep buffered
writes from making moderately sized, highly compressible bodies look cheaper
to send plain. The existing multiple-sample minimum, expiry, conservative rate
selection and explicit `PROXY_GZIP_UPLOAD_MBIT_S` override remain available;
the explicit override bypasses this automatic ceiling. A fast write followed by
unaccounted header waiting cannot distinguish buffered delivery from upstream
work. When waiting exceeds both 50 ms and four times the write interval, the
automatic hint is cleared and gzip keeps its conservative default. This delay
checks confidence; it is never added to the measured upload rate.
Diagnostics keep their opt-in behavior; adaptive traces are installed only for
eligible large plain uploads when optional gzip is enabled without an override.

## Measurements

Eight adjacent AB/BA process pairs per case, ten untraced timing repetitions per worker, approximately 32 MiB input. Memory values below are medians of live Python allocations in MiB. Healthy paths are controls.

### Gzip disabled

| Case | Peak before / after, MiB | Waiting before / after, MiB | Ready before / after, ms | Paired median saved, ms |
| --- | ---: | ---: | ---: | ---: |
| One compressible image | 64.01 / 64.01 | 32.19 / 32.19 | 71.353 / 70.989 | +0.016 |
| 30 images | 64.01 / 64.01 | 32.19 / 32.19 | 72.930 / 70.312 | +3.066 |
| ASCII text | 64.00 / 64.00 | 32.19 / 32.19 | 86.514 / 88.760 | -2.005 |
| Recovery, one image | 96.19 / 64.40 | 32.34 / 32.34 | 290.554 / 197.251 | +92.073 |
| Recovery, Unicode text | 224.18 / 64.78 | 32.34 / 32.34 | 328.148 / 243.963 | +84.655 |
| Recovery, many messages | 100.70 / 100.70 | 32.35 / 32.35 | 381.295 / 378.010 | +5.402 |

### Gzip enabled

| Case | Peak before / after, MiB | Waiting before / after, MiB | Ready before / after, ms | Paired median saved, ms |
| --- | ---: | ---: | ---: | ---: |
| One compressible image | 64.01 / 64.01 | 32.26 / 0.26 | 122.183 / 122.040 | +0.201 |
| ASCII text | 64.00 / 64.00 | 32.25 / 0.26 | 137.945 / 137.674 | +0.106 |
| Recovery, one image | 96.22 / 64.43 | 32.44 / 0.44 | 389.879 / 319.243 | +70.112 |
| Recovery, Unicode text | 224.42 / 65.02 | 32.86 / 0.86 | 312.720 / 248.641 | +65.523 |
| Recovery, many messages | 100.86 / 100.86 | 32.71 / 0.70 | 398.519 / 412.299 | -14.956 |
| Upstream rejects gzip (415) | 64.01 / 64.21 | 32.38 / 32.38 | 181.323 / 202.753 | -22.397 |

Paired medians use the adjacent process pairs and can differ from the difference of variant medians. Negative saved values mean a slowdown in this run. Eight process pairs are not a general latency guarantee. A first HTTP 415 requires reconstructing the released body; the 32-MiB fixture added about 22.4 ms in this run. The existing rejection cache avoids repeating gzip for that capability key during its TTL (60 seconds by default). Highly compressible synthetic image strings are not representative of every PNG/JPEG; already compressed bodies often skip gzip and retain the plain lifetime.

Dense gzip histories still reconstruct their full plain body during recovery: this fixture added about 15.0 ms while releasing approximately 32 MiB during waiting. Healthy gzip requests replay compressed bytes without reconstruction.

### Ordinary loopback requests

First output SSE delivery, 360 adjacent AB/BA pairs per case across four epochs:

| Case | Before / after median, ms | Paired median saved, ms | 95% pair-bootstrap interval, ms |
| --- | ---: | ---: | ---: |
| Short input | 4.000 / 4.008 | -0.030 | -0.082 to +0.020 |
| English history | 7.810 / 7.760 | +0.065 | -0.008 to +0.157 |
| Russian history | 11.011 / 10.762 | +0.275 | +0.154 to +0.361 |
| Tool history | 10.207 / 10.048 | +0.157 | +0.062 to +0.235 |
| Tool history, cached gzip rejection | 9.714 / 9.635 | +0.069 | -0.013 to +0.159 |
| 30 images | 18.661 / 18.683 | +0.025 | -0.163 to +0.134 |

These ordinary-request differences are below a millisecond; several intervals include zero. No negative 95% interval excludes zero in this run. These loopback results do not establish faster model generation. Pair bootstrapping may retain serial correlation.

### Paced-reading policy control

A separate software control paces upstream reading at 20, 100 and 1,000 Mbit/s and adds 80, 25 and 0 ms before headers, respectively. It uses real HTTPX sockets and four adjacent pairs per fixture/setting. This is a policy control, not a measurement of an Internet route or a precise speedup.

| Tool history | Before / after median, ms | Before / after wire bytes |
| --- | ---: | ---: |
| 20 Mbit/s | 272.113 / 267.677 | 414656 / 414656 |
| 100 Mbit/s | 76.277 / 76.918 | 414656 / 414656 |
| 1000 Mbit/s | 20.808 / 19.562 | 414656, 1090565 / 1090565 |

All decoded bodies matched. Gzip stayed enabled for the healthy tool history on the 20/100 Mbit/s controls. Quick buffered writes followed by delayed headers cleared the automatic fast hint, without treating header waiting as bandwidth.

Allowlisted evidence with frozen runtime hashes: [plain bodies](evidence/1.9.3/body-plain.json), [gzip bodies](evidence/1.9.3/body-gzip.json), [loopback latency](evidence/1.9.3/latency.json), [paced-reading control](evidence/1.9.3/paced-control.json).

The benchmark uses real credential-free loopback TCP with synthetic bodies.
Memory means traced live Python allocations including the same-process fixture;
it is not process RSS or total system RAM. Timings run separately with tracing
disabled. Each sample verifies decoded upload hashes and the open SSE stream.
No provider or model calls occur.

Reproduce the large-body comparison against 1.9.2:

```powershell
python benchmarks/body_lifetime.py --baseline-ref f112857586c0ec01043ddd608b87d597c5e28da2 --samples 8 --timing-samples 10
python benchmarks/body_lifetime.py --baseline-ref f112857586c0ec01043ddd608b87d597c5e28da2 --samples 8 --timing-samples 10 --gzip on --cases large_image resource_mismatch_text gzip_rejected
python benchmarks/compare.py --baseline-ref f112857586c0ec01043ddd608b87d597c5e28da2 --samples 90 --epochs 4
python benchmarks/paced.py --baseline-ref f112857586c0ec01043ddd608b87d597c5e28da2 --samples 4
```

Use a new `--output` directory to preserve earlier evidence. Gzip benchmark
validation inflates in bounded pieces so its synthetic upstream cannot create
an extra full-body peak. These figures describe local proxy work and do not
measure model generation or remote provider latency.

## Installation

Existing managed installations can check, prepare and install 1.9.3 from the
[independent browser panel](http://127.0.0.1:8789/). Fresh installations use the
[Windows installation guide](windows-installation.md). Dependencies and the
protected bootstrap stay compatible with 1.9.2; the managed upgrade preserves
saved settings, Codex configuration and request history.
