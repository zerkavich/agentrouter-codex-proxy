"""Real-HTTP timing, gate cancellation and sanitized diagnostic reports."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import doctor
import proxy
from telemetry import RuntimeTimings


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        proxy.reset_rate_limit_state()
        self.previous_client = proxy._SHARED_CLIENT
        self.timings = RuntimeTimings()
        self.patch = mock.patch.multiple(proxy, TIMINGS=self.timings, REQUEST_GZIP_ENABLED=False)
        self.patch.start()

    async def asyncTearDown(self):
        if proxy._SHARED_CLIENT is not self.previous_client and proxy._SHARED_CLIENT is not None:
            await proxy._SHARED_CLIENT.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.patch.stop()
        proxy.reset_rate_limit_state()

    async def test_diagnostics_has_no_upstream_url_headers_or_credential_fingerprints(self):
        secret = "synthetic-private-marker-abcdef123456"
        state = proxy._acquire_rate_limit_state({"authorization": "Bearer " + secret})
        proxy._record_rate_limit_cooldown(1, state)
        proxy._release_rate_limit_state(state)
        with mock.patch.object(proxy, "UPSTREAM", "https://user:" + secret + "@synthetic.invalid/?key=" + secret):
            data = await proxy.diagnostics()
        self.assertEqual(data["status"], "rate_limited")
        self.assertEqual(data["rate_limits"]["cooling_partitions"], 1)
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn("synthetic.invalid", json.dumps(data))
        self.assertNotIn("authorization", json.dumps(data))
        self.assertEqual(data["schema_version"], 1)

    async def test_expired_gzip_rejection_is_not_reported_as_current(self):
        proxy.reset_compression_state()
        try:
            key = proxy._gzip_capability_key({"authorization": "Bearer synthetic"})
            proxy._GZIP_REJECTIONS[key] = time.monotonic() - 1
            data = await proxy.diagnostics()
            self.assertEqual(data["compression"]["rejection_cache_entries"], 0)
            self.assertNotIn("gzip_rejection_cached", [entry["code"] for entry in data["findings"]])
        finally:
            proxy.reset_compression_state()

    async def test_plain_gate_sleep_is_included_but_nonquota_backoff_is_not(self):
        state = proxy._new_rate_limit_state()
        proxy._record_rate_limit_cooldown(0.06, state)
        before = proxy.STATS["rate_limit_wait_seconds_total"]
        with mock.patch.object(proxy, "RECOVERY_STAGGER_S", 0), mock.patch.object(proxy, "_client_disconnected", return_value=False):
            self.assertTrue(await proxy._wait_for_rate_limit_gate(object(), state))
            waited = proxy.STATS["rate_limit_wait_seconds_total"] - before
            self.assertGreaterEqual(waited, 0.05)
            before = proxy.STATS["rate_limit_wait_seconds_total"]
            await proxy._sleep_unless_disconnected(0.02, object(), is_rate_limit=False)
        self.assertEqual(proxy.STATS["rate_limit_wait_seconds_total"], before)
        self.assertEqual(self.timings.snapshot()["rate_limit_gate"]["active"], 0)

    async def test_cancelled_queued_waiters_preserve_fifo_and_leave_lock_free(self):
        state = proxy._new_rate_limit_state()
        proxy._record_rate_limit_cooldown(0.08, state)
        launches = []

        async def launch(index):
            if await proxy._wait_for_rate_limit_gate(object(), state):
                launches.append((index, time.monotonic()))

        with mock.patch.object(proxy, "RECOVERY_STAGGER_S", 0.03), mock.patch.object(proxy, "_client_disconnected", return_value=False):
            tasks = [asyncio.create_task(launch(index)) for index in range(4)]
            try:
                await asyncio.sleep(0.015)
                tasks[1].cancel()
                tasks[3].cancel()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
                self.assertEqual([index for index, stamp in launches], [0, 2])
                self.assertGreaterEqual(launches[1][1] - launches[0][1], 0.029)
                self.assertEqual(state["waiters"], 0)
                self.assertFalse(proxy._get_launch_lock(state).locked())
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_lock_acquired_during_disconnect_is_released(self):
        lock = asyncio.Lock()
        with mock.patch.object(proxy, "_client_disconnected", return_value=True):
            self.assertFalse(await proxy._acquire_launch_lock(lock, object(), None))
        self.assertFalse(lock.locked())

    async def test_real_http_separates_header_wait_from_body_read_and_reuses_tcp(self):
        connections = 0
        handlers = set()

        async def upstream(reader, writer):
            nonlocal connections
            connections += 1
            handlers.add(asyncio.current_task())
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                   if line.lower().startswith(b"content-length:")), 0)
                    await reader.readexactly(length)
                    await asyncio.sleep(0.08)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.04)
                    writer.write(b"{}")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        upstream_client = httpx.AsyncClient(trust_env=False, timeout=2)
        proxy._SHARED_CLIENT = upstream_client
        try:
            with mock.patch.object(proxy, "UPSTREAM", f"http://127.0.0.1:{port}"):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy") as client:
                    for _ in range(2):
                        response = await client.post("/v1/responses", json={"input": "synthetic"})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.content, b"{}")
                    data = (await client.get("/_diagnostics")).json()
            timings = data["timings"]
            self.assertEqual(connections, 1)
            self.assertEqual(timings["upstream_connect"]["count"], 1)
            self.assertEqual(timings["upstream_upload"]["count"], 2)
            self.assertEqual(timings["upstream_headers"]["count"], 2)
            self.assertGreaterEqual(timings["upstream_headers"]["mean_ms"], 65)
            self.assertGreaterEqual(timings["upstream_body"]["mean_ms"], 30)
            self.assertLess(timings["payload_prepare"]["max_ms"], 50)
            self.assertEqual(timings["response_ready"]["count"], 2)
            self.assertTrue(all(stage["active"] == 0 for stage in timings.values()))
        finally:
            await upstream_client.aclose()
            server.close()
            await server.wait_closed()
            await asyncio.sleep(0)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    async def test_trace_cleanup_and_previous_hook_survive_cancellation(self):
        events = []
        trace = self.timings.upstream_trace(lambda event, info: events.append(event))
        await trace("connection.connect_tcp.started", {"headers": {"authorization": "synthetic-secret"}})
        await trace("http11.receive_response_headers.started", {})
        trace.close()
        snapshot = self.timings.snapshot()
        self.assertEqual(snapshot["upstream_connect"]["active"], 0)
        self.assertEqual(snapshot["upstream_headers"]["cancelled"], 1)
        self.assertNotIn("synthetic-secret", repr(trace.started))
        self.assertEqual(len(events), 2)


class DoctorTests(unittest.TestCase):
    health = {"status": "ok", "proxy": "agentrouter-codex-proxy", "version": "1.6.0",
              "uptime_seconds": 100, "stats": {"requests_total": 10, "rate_limit_wait_seconds_total": 9999}}

    def test_legacy_scheduled_counter_is_not_exported_as_elapsed_time(self):
        data = doctor.sanitize_snapshot(self.health)
        self.assertFalse(data["extended_diagnostics"])
        self.assertIsNone(data["rate_limits"]["wait_seconds_total"])
        self.assertNotIn("9999", json.dumps(data))

    def test_report_whitelist_removes_secrets_even_in_extra_server_fields(self):
        secret = "synthetic-secret-for-redaction"
        data = {
            "schema_version": 1, "proxy": "agentrouter-codex-proxy", "status": "ready",
            "authorization": "Bearer " + secret, "upstream": "https://" + secret,
            "counts": {"requests_total": 3, secret: 1},
            "timings": {secret: {"count": 10}, "upstream_headers": {"count": 1, "mean_ms": 40, "prompt": secret}},
            "rate_limits": {"wait_seconds_semantics": "elapsed_gate_and_rate_limit_backoff", "wait_seconds_total": 0.12},
            "findings": [{"code": "upstream_headers_wait", "message": secret, "kind": "current"}],
        }
        snapshot = doctor.sanitize_snapshot({**self.health, "stats": {"api_key": secret}}, data)
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(snapshot["timings"]["upstream_headers"]["mean_ms"], 40)
        self.assertEqual(snapshot["rate_limits"]["wait_seconds_total"], 0.12)

    def test_legacy_collection_requests_health_only(self):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(json.dumps(self.health).encode())
        with mock.patch.object(doctor.urllib.request, "build_opener", return_value=opener):
            snapshot = doctor.collect_snapshot("http://127.0.0.1:8788")
        self.assertFalse(snapshot["extended_diagnostics"])
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(opener.open.call_args[0][0].full_url.endswith("/_health"))

    def test_counter_reset_or_process_change_does_not_produce_negative_deltas(self):
        first = doctor.sanitize_snapshot(self.health)
        second = doctor.sanitize_snapshot({**self.health, "uptime_seconds": 1, "stats": {"requests_total": 1}})
        self.assertIsNone(doctor.snapshot_delta(first, second))

    def test_export_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text("preserve me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                doctor.write_report(path, doctor.sanitize_snapshot(self.health))
            self.assertEqual(path.read_text(encoding="utf-8"), "preserve me")

    def test_cli_json_watch_and_export(self):
        snapshot = doctor.sanitize_snapshot(self.health)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(doctor, "collect_snapshot", return_value=snapshot), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            path = Path(folder) / "report.json"
            self.assertEqual(doctor.main(["--watch", "--duration", "0.03", "--interval", "0.1", "--json", "--export", str(path)]), 0)
            self.assertEqual(json.loads(path.read_text())["counts"]["requests_total"], 10)
            self.assertGreaterEqual(len(output.getvalue().splitlines()), 1)

    def test_url_rejects_embedded_credentials_and_queries(self):
        for url in ("http://user:secret@localhost:8788", "http://localhost:8788/?key=secret", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                doctor.validate_url(url)

    def test_unreachable_cli_returns_structured_error_without_raw_server_text(self):
        with mock.patch.object(doctor, "collect_snapshot", side_effect=doctor.ReportError("unreachable_or_invalid_response")), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(doctor.main(["--json"]), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")


class ExtraHintTests(unittest.TestCase):
    def test_explicit_subsecond_hint_has_proportional_jitter(self):
        with mock.patch.object(proxy, "RATE_LIMIT_MAX_DELAY_S", 15):
            delay, hint = proxy._extract_retry_delay_s({"retry-after-ms": "150"}, "", 1)
        self.assertGreaterEqual(delay, 0.15)
        self.assertLessEqual(delay, 0.165)
        self.assertEqual(hint, 1)

    def test_generated_429_uses_stronger_current_cooldown_and_integer_header(self):
        result = proxy._rate_limit_error_response(None, "rate limit", "rate_limit_exceeded",
                                                  60, {"Retry-After": "2", "Content-Type": "text/plain"})
        self.assertEqual(result.headers["retry-after"], "60")
        self.assertEqual(result.headers["content-type"], "application/json")

    def test_http_date_and_compound_reset_durations(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        delay, hint = proxy._extract_retry_delay_s({"retry-after": format_datetime(future, usegmt=True)}, "", 1)
        self.assertGreater(delay, 58)
        self.assertGreaterEqual(hint, 59)
        self.assertEqual(proxy._parse_reset_duration("1h2m3.5s"), 3723.5)
        self.assertEqual(proxy._parse_reset_duration("250ms"), 0.25)

    def test_nonempty_quota_dimension_does_not_add_unnecessary_wait(self):
        delay, hint = proxy._extract_retry_delay_s({
            "x-ratelimit-reset-tokens": "2s", "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-requests": "1m", "x-ratelimit-remaining-requests": "50",
        }, "", 1)
        self.assertEqual(hint, 2)
        self.assertLess(delay, 3)


class OnDemandProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.timings = RuntimeTimings(enabled=False)
        self.previous_client = proxy._SHARED_CLIENT
        proxy.reset_rate_limit_state()
        self.patch = mock.patch.multiple(proxy, TIMINGS=self.timings, REQUEST_GZIP_ENABLED=False)
        self.patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 12000)), base_url="http://proxy")

    async def asyncTearDown(self):
        await self.client.aclose()
        if proxy._SHARED_CLIENT is not self.previous_client and proxy._SHARED_CLIENT is not None:
            await proxy._SHARED_CLIENT.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.patch.stop()
        proxy.reset_rate_limit_state()

    async def test_fast_mode_installs_no_httpcore_trace_callbacks(self):
        observed = []

        def upstream(request):
            observed.append(request.extensions.get("trace"))
            return httpx.Response(200, json={"output": "synthetic"})

        proxy._SHARED_CLIENT = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await self.client.post("/v1/responses", json={"input": "synthetic"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [None])
        self.assertTrue(all(stage["count"] == 0 for stage in self.timings.snapshot().values()))
        self.assertIs(self.timings.measure("payload_prepare"), self.timings.measure("payload_prepare"))

    async def test_temporary_profile_expires_and_finishes_existing_intervals(self):
        response = await self.client.post("/_diagnostics/profile", json={"duration_s": 0.04})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["timing_mode"], "temporary")
        with self.timings.measure("sse_delivery"):
            await asyncio.sleep(0.06)
            self.assertFalse(self.timings.enabled)
        data = (await self.client.get("/_diagnostics")).json()
        self.assertEqual(data["timing_mode"], "off")
        self.assertEqual(data["profile_remaining_s"], 0)
        self.assertEqual(data["timings"]["sse_delivery"]["count"], 1)
        self.assertEqual(data["timings"]["sse_delivery"]["active"], 0)

    async def test_remote_and_browser_requests_cannot_enable_profiling(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app, client=("192.0.2.9", 12000)), base_url="http://proxy") as remote:
            response = await remote.post("/_diagnostics/profile", json={"duration_s": 30})
            self.assertEqual(response.status_code, 403)
        response = await self.client.post("/_diagnostics/profile", json={"duration_s": 30}, headers={"origin": "https://example.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.timings.enabled)

    async def test_profile_duration_and_body_are_bounded(self):
        for seconds in (0, -1, 301, True, "secret"):
            with self.subTest(seconds=seconds):
                response = await self.client.post("/_diagnostics/profile", json={"duration_s": seconds})
                self.assertEqual(response.status_code, 422)
        response = await self.client.post("/_diagnostics/profile", content=b"x" * 2048)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.timings.enabled)

    async def test_shorter_profile_does_not_shorten_an_existing_window(self):
        self.timings.profile(2)
        original = self.timings.profile_until
        self.timings.profile(0.05)
        self.assertEqual(self.timings.profile_until, original)


class DoctorProfileTests(unittest.TestCase):
    def test_old_installation_is_never_sent_a_profile_post(self):
        health = {"status": "ok", "proxy": "agentrouter-codex-proxy", "version": "1.6.0"}
        with mock.patch.object(doctor, "_read_json", return_value=health) as read:
            with self.assertRaisesRegex(doctor.ReportError, "profiling_unsupported"):
                doctor.start_profile("http://127.0.0.1:8788", 30)
        self.assertEqual(read.call_count, 1)

    def test_cli_can_enable_profiling_without_restarting_or_writing_config(self):
        snapshot = doctor.sanitize_snapshot(DoctorTests.health)
        with mock.patch.object(doctor, "start_profile") as start, mock.patch.object(doctor, "collect_snapshot", return_value=snapshot), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(doctor.main(["--profile", "60", "--json"]), 0)
        start.assert_called_once_with("http://127.0.0.1:8788", 60, 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")

if __name__ == "__main__":
    unittest.main()
