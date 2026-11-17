"""Status/watch/report CLI with temporary local profiling; no .env or provider calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Allow the same CLI to run from the machine's isolated portable interpreter.
_APP_ROOT = str(Path(__file__).resolve().parent)
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
from telemetry import STAGES

MAX_REPORT_BYTES = 256 * 1024
COUNTERS = (
    "requests_total", "responses_requests", "resource_mismatches_caught",
    "stateless_fallbacks_applied", "successful_recoveries", "transport_retries",
    "pool_timeouts", "upstream_capacity_waits", "upstream_disconnect_cancellations",
    "upstream_unstarted_connections_reclaimed", "upstream_orphaned_connections_reclaimed",
    "upstream_orphaned_requests_reclaimed",
    "client_disconnects_during_upload", "gzip_payloads", "gzip_fallbacks",
    "gzip_cached_rejection_skips", "gzip_busy_skips", "gzip_cost_skips",
    "sse_backpressure_events", "rate_limit_overflow_requests", "rate_limits_caught",
    "stream_precommit_retries", "rate_limit_gate_waits", "rate_limit_gate_timeouts",
    "rate_limit_cancelled_waiters", "rate_limit_stale_successes_ignored",
    "rate_limit_budget_exhaustions", "rate_limit_hint_responses",
)
FINDINGS = {
    "upstream_rate_limit": "Upstream cooldown is active; callers sharing that quota wait.",
    "upstream_capacity_wait": "Requests wait for an upstream connection slot.",
    "upstream_headers_wait": "Upstream attempts are waiting for response headers.",
    "pool_timeouts_observed": "Pool timeouts occurred since startup; check current queue and new events.",
    "credential_partition_overflow": "Some callers used the bounded credential-overflow gate.",
    "gzip_rejection_cached": "Recent HTTP 415 responses temporarily disabled proxy-added gzip.",
}
STAGE_LABELS = {
    "client_upload": "Client upload",
    "payload_inspect": "Payload inspection",
    "payload_prepare": "Payload encoding/gzip",
    "rate_limit_gate": "Quota/launch queue",
    "retry_backoff": "Recovery backoff",
    "upstream_admission": "Connection slot",
    "upstream_connect": "TCP connect (incl. DNS)",
    "upstream_tls": "TLS handshake",
    "upstream_upload": "Upstream body upload",
    "upstream_headers": "Upstream headers wait",
    "upstream_body": "JSON/error body read",
    "sse_prime": "Initial SSE inspection",
    "response_ready": "Response ready",
    "sse_delivery": "SSE delivery/cleanup",
}


class ReportError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _number(value, default=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value >= 0 and math.isfinite(value):
                return value
        except OverflowError:
            pass
    return default


def _mapping(value):
    return value if isinstance(value, dict) else {}


def validate_url(raw):
    parsed = urllib.parse.urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid port") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Use a base HTTP URL without credentials, paths or query parameters")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _read_json(opener, url, timeout, data=None):
    try:
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        with opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
            body = response.read(MAX_REPORT_BYTES + 1)
        if len(body) > MAX_REPORT_BYTES:
            raise ReportError("report_too_large")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ReportError("invalid_report")
        return result
    except ReportError:
        raise
    except (urllib.error.URLError, OSError, ValueError):
        # Raw exception text can contain URLs or server output; do not export it.
        raise ReportError("unreachable_or_invalid_response") from None


def sanitize_snapshot(health, data=None):
    """Explicit allowlists keep keys/prompts out even when a server adds fields."""
    if health.get("proxy") != "agentrouter-codex-proxy" or health.get("status") != "ok":
        raise ReportError("unexpected_service")
    version = str(health.get("version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?", version):
        version = "unknown"
    extended = isinstance(data, dict) and data.get("proxy") == "agentrouter-codex-proxy" and data.get("schema_version") == 1
    if data is not None and not extended:
        raise ReportError("invalid_diagnostics_schema")
    data = data if extended else {}
    raw_counts = _mapping(data.get("counts", health.get("stats")))
    counts = {name: _number(raw_counts.get(name), 0) for name in COUNTERS}
    raw_pool = _mapping(data.get("connection_pool"))
    raw_rate = _mapping(data.get("rate_limits"))
    raw_gzip = _mapping(data.get("compression"))
    raw_body = _mapping(data.get("body_memory", health.get("body_memory")))
    raw_timings = _mapping(data.get("timings"))
    elapsed_wait = extended and raw_rate.get("wait_seconds_semantics") == "elapsed_gate_and_rate_limit_backoff"
    snapshot = {
        "schema_version": 1, "proxy": "agentrouter-codex-proxy", "version": version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": data.get("status") if data.get("status") in ("ready", "busy", "rate_limited") else "ready",
        "process_id": _number(data.get("process_id")),
        "uptime_seconds": _number(data.get("uptime_seconds", health.get("uptime_seconds")), 0),
        "extended_diagnostics": extended, "timings_enabled": extended and data.get("timings_enabled") is True,
        "timing_mode": data.get("timing_mode") if data.get("timing_mode") in ("off", "temporary", "always") else "off",
        "profile_remaining_s": _number(data.get("profile_remaining_s"), 0),
        "connection_pool": {
            "max_inflight": _number(raw_pool.get("max_inflight", health.get("upstream_max_inflight")), 50),
            "in_use": _number(raw_pool.get("in_use", health.get("upstream_slots_in_use")), 0),
            "waiters": _number(raw_pool.get("waiters", health.get("upstream_slot_waiters")), 0),
            "connections": _number(raw_pool.get("connections")),
            "idle_connections": _number(raw_pool.get("idle_connections")),
            "requests": _number(raw_pool.get("requests"), 0),
            "queued_requests": _number(raw_pool.get("queued_requests"), 0),
            "released_requests": _number(raw_pool.get("released_requests"), 0),
            "cleanup_active": raw_pool.get("cleanup_active", health.get("upstream_pool_cleanup_active")) is True,
        },
        "rate_limits": {
            "scope": raw_rate.get("scope", health.get("rate_limit_scope")) if raw_rate.get("scope", health.get("rate_limit_scope")) in ("shared", "credential") else "unknown",
            "partitions": _number(raw_rate.get("partitions", health.get("rate_limit_partitions")), 0),
            "cooling_partitions": _number(raw_rate.get("cooling_partitions")),
            "queued_requests": _number(raw_rate.get("queued_requests")),
            "longest_cooldown_remaining_s": _number(raw_rate.get("longest_cooldown_remaining_s")),
            "wait_seconds_total": _number(raw_rate.get("wait_seconds_total")) if elapsed_wait else None,
            "wait_seconds_semantics": "elapsed_gate_and_rate_limit_backoff" if elapsed_wait else "unavailable",
        },
        "compression": {
            "enabled": raw_gzip.get("enabled", health.get("request_gzip_enabled")) is True,
            "backend": raw_gzip.get("backend", health.get("request_gzip_backend")) if raw_gzip.get("backend", health.get("request_gzip_backend")) in ("isal", "stdlib") else "unknown",
            "jobs_inflight": _number(raw_gzip.get("jobs_inflight", health.get("gzip_jobs_inflight")), 0),
        },
        "body_memory": {name: _number(raw_body.get(name), 0) for name in (
            "limit_bytes", "reserved_bytes", "peak_reserved_bytes", "waiters", "waits", "timeouts", "reservation_factor")},
        "counts": counts, "timings": {}, "findings": [],
    }
    for stage in STAGES:
        timing = _mapping(raw_timings.get(stage))
        snapshot["timings"][stage] = {
            key: _number(timing.get(key)) for key in
            ("count", "active", "failed", "cancelled", "total_ms", "mean_ms", "max_ms", "p50_bucket_upper_ms", "p95_bucket_upper_ms")
        }
    for finding in data.get("findings", ()) if isinstance(data.get("findings"), list) else ():
        finding = _mapping(finding)
        code = finding.get("code")
        if isinstance(code, str) and code in FINDINGS:
            snapshot["findings"].append({
                "code": code, "kind": "current" if finding.get("kind") == "current" else "since_start",
                "message": FINDINGS[code],
            })
    return snapshot


def start_profile(base_url, seconds, timeout=3):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    health = _read_json(opener, base_url + "/_health", timeout)
    sanitize_snapshot(health)
    if health.get("on_demand_profiling_supported") is not True:
        raise ReportError("profiling_unsupported")
    data = _read_json(opener, base_url + "/_diagnostics/profile", timeout,
                      json.dumps({"duration_s": seconds}, allow_nan=False).encode())
    if (data.get("proxy") != "agentrouter-codex-proxy" or data.get("schema_version") != 1
            or data.get("timings_enabled") is not True):
        raise ReportError("profiling_failed")


def collect_snapshot(base_url, timeout=3):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    health = _read_json(opener, base_url + "/_health", timeout)
    sanitize_snapshot(health)
    data = None
    if health.get("diagnostics_schema_version") == 1:
        data = _read_json(opener, base_url + "/_diagnostics", timeout)
    # Older installations never receive a request to an unsupported passthrough path.
    return sanitize_snapshot(health, data)


def snapshot_delta(previous, current):
    if previous is None:
        return None
    changed = (previous.get("process_id") != current.get("process_id")
               or current["uptime_seconds"] < previous["uptime_seconds"])
    delta = {name: current["counts"][name] - previous["counts"][name] for name in COUNTERS}
    if changed or any(value < 0 for value in delta.values()):
        return None
    return delta


def print_snapshot(snapshot, previous=None):
    pool, rate = snapshot["connection_pool"], snapshot["rate_limits"]
    print("Proxy {version} | {status} | uptime {uptime_seconds:.1f}s".format(**snapshot))
    print("Upstream slots: {in_use}/{max_inflight}; connection waiters: {waiters}".format(**pool))
    if snapshot["extended_diagnostics"]:
        print("Quota queue: {}; cooling partitions: {}; longest cooldown: {:.2f}s".format(
            rate["queued_requests"], rate["cooling_partitions"], rate["longest_cooldown_remaining_s"]))
        print("Timing mode: {}; temporary profile remaining: {:.1f}s".format(
            snapshot["timing_mode"], snapshot["profile_remaining_s"]))
    counts = snapshot["counts"]
    delta = snapshot_delta(previous, snapshot)
    print("Since start: {} requests; {} rate limits; {} pool timeouts; {} recoveries".format(
        counts["requests_total"], counts["rate_limits_caught"], counts["pool_timeouts"], counts["successful_recoveries"]))
    if delta is not None:
        print("Since previous sample: +{} requests; +{} rate limits; +{} pool timeouts".format(
            delta["requests_total"], delta["rate_limits_caught"], delta["pool_timeouts"]))
    if snapshot["timings_enabled"] or any((stage["count"] or 0) > 0 for stage in snapshot["timings"].values()):
        print("{:<28} {:>6} {:>6} {:>10} {:>14}".format("Stage (completed samples)", "count", "active", "mean ms", "p95 upper ms"))
        for stage, values in snapshot["timings"].items():
            if not values["count"] and not values["active"]:
                continue
            mean = "{:.3f}".format(values["mean_ms"]) if values["mean_ms"] is not None else "-"
            upper = "<={:.3f}".format(values["p95_bucket_upper_ms"]) if values["p95_bucket_upper_ms"] is not None else "-"
            print("{:<28} {:>6} {:>6} {:>10} {:>14}".format(
                STAGE_LABELS.get(stage, stage), values["count"], values["active"], mean, upper))
        print("Timings include failed/cancelled samples; stages overlap. p95 is a bucket upper bound.")
        if not snapshot["timings_enabled"]:
            print("Profiling is off; previously captured timing samples are shown.")
    elif not snapshot["extended_diagnostics"]:
        print("This version has basic health only; its scheduled-sleep counter is not elapsed waiting.")
    else:
        print("Detailed timing is off. Use --profile 60 to capture new operations for 60 seconds.")
    for finding in snapshot["findings"]:
        print("[{}] {}".format(finding["kind"], finding["message"]))
    print(flush=True)


def write_report(path, snapshot):
    # Exclusive create prevents overwriting an earlier report or another file.
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(snapshot, output, ensure_ascii=True, indent=2, allow_nan=False)
        output.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8788", help="Proxy base URL.")
    parser.add_argument("--watch", action="store_true", help="Refresh until Ctrl+C or --duration.")
    parser.add_argument("--profile", nargs="?", const=30, type=float,
                        help="Enable temporary local profiling for 1-300 seconds (default: 30).")
    parser.add_argument("--interval", type=float, default=2, help="Refresh interval in seconds.")
    parser.add_argument("--duration", type=float, help="Stop watching after this many seconds.")
    parser.add_argument("--timeout", type=float, default=3, help="Timeout for each local HTTP request.")
    parser.add_argument("--json", action="store_true", help="Print JSON (one object per watch sample).")
    parser.add_argument("--export", type=Path, help="Save the last valid sanitized snapshot to a new JSON file.")
    args = parser.parse_args(argv)
    try:
        base_url = validate_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    if not math.isfinite(args.interval) or args.interval < 0.1:
        parser.error("--interval must be finite and at least 0.1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0 or not args.watch):
        parser.error("--duration requires --watch and a finite positive value")
    if args.profile is not None and (not math.isfinite(args.profile) or not 0 < args.profile <= 300):
        parser.error("--profile must be finite and between 0 and 300 seconds")
    if args.export is not None and args.export.exists():
        parser.error("--export already exists; choose a new report file")
    previous, last = None, None
    started = time.monotonic()
    ok = False
    try:
        if args.profile is not None:
            try:
                start_profile(base_url, args.profile, args.timeout)
            except ReportError as exc:
                print(json.dumps({"status": "unavailable", "error_code": str(exc)}) if args.json
                      else "Could not enable profiling: " + str(exc), flush=True)
                return 1
        while True:
            try:
                last = collect_snapshot(base_url, args.timeout)
                ok = True
                if args.json:
                    print(json.dumps(last, allow_nan=False), flush=True)
                else:
                    print_snapshot(last, previous)
                previous = last
            except ReportError as exc:
                ok = False
                error = {"status": "unavailable", "error_code": str(exc)}
                print(json.dumps(error) if args.json else "Proxy unavailable: " + str(exc), flush=True)
            if not args.watch:
                break
            remaining = args.duration - (time.monotonic() - started) if args.duration else args.interval
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        pass
    if args.export is not None:
        if last is None:
            print("No valid snapshot was available to export.", file=sys.stderr)
            return 1
        try:
            write_report(args.export, last)
        except OSError:
            print("Could not create the report; existing files were preserved.", file=sys.stderr)
            return 1
        if not args.json:
            print("Saved sanitized report: " + str(args.export))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
