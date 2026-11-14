# Runtime diagnosis and temporary profiling

Version 1.7.0 adds live status, accurate quota waiting counters and optional timing capture. Status, watch and export do not load credentials or contact the model provider. Detailed timing is off by default; normal requests install no HTTPcore trace callbacks.

Run from the checkout or installed app directory:

~~~powershell
python doctor.py
python doctor.py --watch
python doctor.py --profile 60 --watch --duration 60
python doctor.py --export proxy-report.json
~~~

The Windows wrapper locates the virtual environment or the machine's portable Python:

~~~powershell
.\diagnose.ps1
.\diagnose.ps1 -Watch
.\diagnose.ps1 -ProfileSeconds 60 -Watch -Duration 60 -ExportPath proxy-report.json
~~~

Temporary profiling captures new operations in this proxy process. Start it before reproducing a delay. It expires automatically without a restart/config edit, and retains captured aggregates after expiration. A shorter activation does not cut short an existing longer window. The maximum window is 300 seconds.

Profiling is process-wide; results aggregate callers and never expose credential fingerprints. Activation accepts local non-browser tools. Status/watch/export remain read-only. Set `PROXY_TIMINGS=1` in .env and restart for continuous capture; the default is `0`.

The report is at `/_diagnostics`. Health and stats remain compatible with earlier clients. Against 1.6.0 the CLI reads basic health only, avoiding unsupported passthrough requests.

## Interpreting status

- `ready`: capacity is available; requests may still be streaming.
- `rate_limited`: at least one credential partition has a cooldown; independent credentials retain independent gates.
- `busy`: callers are waiting for launch or connection slots.
- `connection_pool.waiters`: callers waiting for the existing 50-connection admission cap.
- `rate_limits.queued_requests`: callers in quota/launch gates.
- `counts.pool_timeouts`: events since startup, not proof of a current connection leak.

Watch shows differences between samples and detects process/counter resets. Findings marked `current` describe live observations; `since_start` describes historical counters.

## Timing definitions

Capture uses 14 fixed histograms with 21 buckets each. No per-request history, prompts, outputs or headers are retained. Count includes completed intervals, including failed/cancelled operations; active counts captured intervals still in progress.

| Stage | Interval |
| --- | --- |
| client_upload | Reading the incoming Responses body. |
| payload_inspect | Initial inspection and historical-image pruning. |
| payload_prepare | Preparing outgoing bytes, including optional gzip work. |
| rate_limit_gate | Quota cooldown and the fair launch queue. |
| retry_backoff | Recovery sleeps for rate limits and other transient failures. |
| upstream_admission | Waiting for an upstream connection slot. |
| upstream_connect | TCP connection operation, including DNS when applicable. |
| upstream_tls | TLS handshake. |
| upstream_upload | Writing the body to the transport; not a physical link-speed measurement. |
| upstream_headers | Receiving headers, including RTT and provider processing before headers. |
| upstream_body | Reading Responses JSON/error bodies. |
| sse_prime | Initial inspection, including waiting for upstream SSE setup/output. |
| response_ready | Incoming handling until a downstream response is ready, including recovery. |
| sse_delivery | Downstream SSE delivery/cleanup, including provider generation while streaming. |

Intervals overlap; do not sum their means or interpret response_ready/sse_delivery as proxy CPU time. Keep-alive requests add no fictitious TCP/TLS samples.

Mean/max are measured elapsed values. Displayed p50/p95 values are histogram bucket upper bounds, not exact percentiles. A null upper bound with completed samples denotes overflow above 600 seconds. Samples accumulate across capture windows until restart.

## Rate-limit recovery

Cancelled FIFO waiters spend no future stagger slots. A newer 429 is rechecked before a waiting caller launches. Older successes cannot clear newer cooldowns.

Finite Retry-After, retry-after-ms, reset-duration hints and supported error-text hints are honored. Explicit hints may exceed the generated-backoff cap. Millisecond text is parsed correctly, with proportional jitter for short hints. Forwarded 429 responses carry a whole-second ceiling for the strongest current cooldown.

The default recovery wait budget is 45 seconds. A required cooldown that cannot fit returns 429 without an early retry. Its full cooldown still protects callers sharing that quota, even after that request exhausts its retries. Healthy long generation retains the existing read timeout.

The wait counter measures elapsed quota gate/backoff caller-time, including cancellation. Ten concurrent callers waiting two seconds contribute about twenty seconds. Unrelated transient backoff is excluded. The CLI does not label 1.6.0's scheduled-sleep counter as elapsed waiting.

## Export

`--json` prints a snapshot, or one object per watch sample. `--export` saves the last valid, dated snapshot to a new file and refuses to overwrite an existing one.

Explicit allowlists retain numeric counters, known timing stages and findings. Reports exclude upstream URLs, raw log/error text, credentials/hashes, configuration, conversations and local paths.

Update the complete runtime, including telemetry.py and doctor.py. Use the full package or a fast-forward Git update; copying only proxy.py is insufficient for 1.7.0.
