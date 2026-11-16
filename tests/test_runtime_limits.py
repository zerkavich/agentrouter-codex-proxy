"""Slow clients, cancelled compression and independent upstream quotas."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Condition did not become true")
        await asyncio.sleep(0.005)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, release=None):
        self.chunks = chunks
        self.release = release
        self.reads = 0

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index and self.release is not None:
                await self.release.wait()
            self.reads += 1
            yield chunk


def response(stream):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


class SSEBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reader_bounds_queue_and_preserves_oversized_chunks(self):
        body = b"a" * (2 * 1024 * 1024) + b"b" * 1024
        stream = _Stream([body, b"tail"])
        upstream = response(stream)
        with mock.patch.object(proxy, "SSE_BUFFER_MAX_BYTES", 65536):
            queue = proxy._new_sse_queue()
            task = asyncio.create_task(proxy._sse_producer(upstream, queue, []))
            try:
                await until(lambda: queue.full())
                await asyncio.sleep(0.02)
                self.assertEqual(queue.qsize(), 1)
                self.assertEqual(stream.reads, 1)
                self.assertFalse(task.done())
                delivered = []
                while True:
                    chunk = await asyncio.wait_for(queue.get(), timeout=2)
                    if chunk is None:
                        break
                    self.assertLessEqual(len(chunk), 65536)
                    delivered.append(chunk)
                await task
                self.assertEqual(b"".join(delivered), body + b"tail")
            finally:
                await proxy._close_sse_producer(upstream, task)

    async def test_small_chunk_arrives_while_next_chunk_is_still_withheld(self):
        release = asyncio.Event()
        upstream = response(_Stream([b"first token\n", b"last token\n"], release))
        queue = proxy._new_sse_queue()
        task = asyncio.create_task(proxy._sse_producer(upstream, queue, []))
        try:
            first = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(first, b"first token\n")
            self.assertFalse(release.is_set())
            release.set()
            self.assertEqual(await queue.get(), b"last token\n")
            self.assertIsNone(await queue.get())
        finally:
            release.set()
            await proxy._close_sse_producer(upstream, task)

    async def test_cancelled_cleanup_closes_producer_blocked_by_backpressure(self):
        upstream = response(_Stream([b"x" * 200000]))
        with mock.patch.object(proxy, "SSE_BUFFER_MAX_BYTES", 65536):
            queue = proxy._new_sse_queue()
            task = asyncio.create_task(proxy._sse_producer(upstream, queue, []))
            await until(lambda: queue.full())
            with anyio.CancelScope() as scope:
                scope.cancel()
                await proxy._close_sse_producer(upstream, task)
            self.assertTrue(task.done())
            self.assertTrue(upstream.is_closed)

    async def prime_and_drain(self, body, **limits):
        upstream = response(_Stream([body]))
        with mock.patch.multiple(proxy, **limits), mock.patch.object(proxy, "_client_disconnected", return_value=False):
            prime = await proxy._prime_responses_sse(upstream, object(), first_frame_timeout_s=2, created_grace_s=1)
            self.assertEqual(prime.status, "commit")
            delivered = b"".join([chunk async for chunk in proxy._stream_generator(
                upstream, primed_chunks=prime.primed_chunks, remainder=prime.remainder,
                queue=prime.queue, producer_task=prime.producer_task, producer_exc=prime.producer_exc,
            )])
        self.assertEqual(delivered, body)
        self.assertTrue(upstream.is_closed)
        return prime

    async def test_large_partial_sse_frame_caps_priming_without_losing_bytes(self):
        body = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"' + b"x" * 600000 + b'"}\n\n'
        prime = await self.prime_and_drain(body, SSE_PRIME_MAX_BYTES=65536)
        self.assertLessEqual(sum(map(len, prime.primed_chunks)) + len(prime.remainder), 65536)

    async def test_many_small_comments_cap_frame_allocations_without_losing_bytes(self):
        body = b": ping\n\n" * 2000 + b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        prime = await self.prime_and_drain(body, SSE_PRIME_MAX_FRAMES=32)
        self.assertEqual(len(prime.primed_chunks), 32)


class GzipPoolTests(unittest.IsolatedAsyncioTestCase):
    body = b'{"input":"' + b"synthetic " * 40000 + b'"}'
    headers = {"content-type": "application/json", "authorization": "Bearer synthetic"}

    async def asyncSetUp(self):
        proxy.reset_compression_state()
        self.pool = proxy._GzipPool(1, 1)
        self.patch = mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, _GZIP_POOL=self.pool)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.pool.shutdown()
        proxy.reset_compression_state()

    async def test_saturated_pool_forwards_plain_instead_of_queuing_indefinitely(self):
        release = threading.Event()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def compress(body):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return gzip.compress(body, mtime=0)

        with mock.patch.object(proxy, "_gzip_if_worthwhile", side_effect=compress):
            first = asyncio.create_task(proxy._prepare_upstream_content(self.body, self.headers))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                before = proxy.STATS["gzip_busy_skips"]
                plain, headers = await proxy._prepare_upstream_content(self.body, self.headers)
                self.assertIs(plain, self.body)
                self.assertIs(headers, self.headers)
                self.assertEqual(proxy.STATS["gzip_busy_skips"], before + 1)
                self.assertEqual(self.pool.inflight, 1)
            finally:
                release.set()
                encoded, _ = await first
                self.assertEqual(gzip.decompress(encoded), self.body)
                self.assertEqual(self.pool.inflight, 0)

    async def test_cancelled_running_job_keeps_slot_until_compressor_really_finishes(self):
        release = threading.Event()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def compress(body):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return gzip.compress(body, mtime=0)

        with mock.patch.object(proxy, "_gzip_if_worthwhile", side_effect=compress):
            first = asyncio.create_task(proxy._prepare_upstream_content(self.body, self.headers))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertEqual(self.pool.inflight, 1)
                plain, _ = await proxy._prepare_upstream_content(self.body, self.headers)
                self.assertIs(plain, self.body)
            finally:
                release.set()
                await until(lambda: self.pool.inflight == 0)
            encoded, _ = await proxy._prepare_upstream_content(self.body, self.headers)
            self.assertEqual(gzip.decompress(encoded), self.body)

    async def test_cancelled_queued_job_releases_its_capacity(self):
        self.pool.shutdown()
        self.pool = proxy._GzipPool(1, 2)
        proxy._GZIP_POOL = self.pool
        release = threading.Event()
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def compress(body):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return gzip.compress(body, mtime=0)

        with mock.patch.object(proxy, "_gzip_if_worthwhile", side_effect=compress):
            first = asyncio.create_task(proxy._prepare_upstream_content(self.body, self.headers))
            second = None
            try:
                await started.wait()
                second = asyncio.create_task(proxy._prepare_upstream_content(self.body, self.headers))
                await until(lambda: self.pool.inflight == 2)
                second.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await second
                await until(lambda: self.pool.inflight == 1)
            finally:
                release.set()
                await first
                if second is not None and not second.done():
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
                await until(lambda: self.pool.inflight == 0)

    async def test_compression_does_not_wait_for_busy_default_executor(self):
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        occupied = loop.run_in_executor(None, release.wait, 3)
        try:
            encoded, _ = await asyncio.wait_for(proxy._prepare_upstream_content(self.body, self.headers), timeout=1)
            self.assertEqual(gzip.decompress(encoded), self.body)
        finally:
            release.set()
            await occupied
            executor.shutdown(wait=False)


class GzipCapabilityTests(unittest.TestCase):
    def setUp(self):
        proxy.reset_compression_state()

    def tearDown(self):
        proxy.reset_compression_state()

    def test_negative_hint_expires_and_allows_a_new_probe(self):
        headers = {"authorization": "Bearer test-key"}
        with mock.patch.object(proxy, "REQUEST_GZIP_REJECT_TTL_S", 1):
            with mock.patch.object(proxy.time, "monotonic", return_value=1000):
                proxy._record_gzip_rejection(headers)
            with mock.patch.object(proxy.time, "monotonic", return_value=1000.5):
                self.assertTrue(proxy._gzip_rejected_recently(headers))
            with mock.patch.object(proxy.time, "monotonic", return_value=1001.1):
                self.assertFalse(proxy._gzip_rejected_recently(headers))
                self.assertEqual(len(proxy._GZIP_REJECTIONS), 0)

    def test_hints_are_separated_by_origin_credentials_and_project(self):
        headers = {"authorization": "Bearer test-key", "openai-project": "project-a"}
        proxy._record_gzip_rejection(headers)
        self.assertTrue(proxy._gzip_rejected_recently(headers))
        self.assertFalse(proxy._gzip_rejected_recently({**headers, "authorization": "Bearer other-key"}))
        self.assertFalse(proxy._gzip_rejected_recently({**headers, "openai-project": "project-b"}))
        with mock.patch.object(proxy, "UPSTREAM", "https://different-synthetic-upstream.invalid"):
            self.assertFalse(proxy._gzip_rejected_recently(headers))

    def test_cache_is_bounded_and_retains_no_api_keys(self):
        for index in range(proxy.REQUEST_GZIP_CACHE_SIZE + 30):
            proxy._record_gzip_rejection({"authorization": f"Bearer secret-synthetic-key-{index}"})
        self.assertLessEqual(len(proxy._GZIP_REJECTIONS), proxy.REQUEST_GZIP_CACHE_SIZE)
        self.assertNotIn("secret-synthetic-key", repr(proxy._GZIP_REJECTIONS))

    def test_cost_policy_keeps_compression_for_slow_links_and_skips_full_work_for_fast_links(self):
        body = b"synthetic text for comparison " * 40000
        first = proxy._gzip_if_worthwhile(body)
        self.assertEqual(gzip.decompress(first), body)
        before = proxy.STATS["gzip_cost_skips"]
        with mock.patch.object(proxy, "_compress_request_gzip", wraps=proxy._compress_request_gzip) as codec:
            fast = proxy._gzip_if_worthwhile(body, upload_mbit_s=100000)
            self.assertIs(fast, body)
            self.assertEqual(codec.call_count, 3)  # Samples only, no whole-body compression.
        self.assertEqual(proxy.STATS["gzip_cost_skips"], before + 1)
        slow = proxy._gzip_if_worthwhile(body, upload_mbit_s=1)
        self.assertEqual(gzip.decompress(slow), body)

    def test_upload_hint_requires_real_large_requests_and_expires_per_credential(self):
        body = b"x" * 1000000
        headers = {"authorization": "Bearer synthetic"}
        fake = httpx.Response(200)
        proxy._record_upload_observation(body, headers, 0.1, fake, header_wait_s=0.01)
        self.assertEqual(proxy._upload_estimate(headers), 0)
        real_metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0), mock.patch.object(proxy.time, "monotonic", return_value=1000):
            proxy._record_upload_observation(body, headers, 0.1, real_metadata, header_wait_s=0.01)
            self.assertEqual(proxy._upload_estimate(headers), 0)
            proxy._record_upload_observation(body, headers, 0.2, real_metadata, header_wait_s=0.01)
            self.assertEqual(proxy._upload_estimate(headers), 40)
            self.assertEqual(proxy._upload_estimate({"authorization": "Bearer other"}), 0)
        with mock.patch.object(proxy, "REQUEST_GZIP_UPLOAD_MBIT_S", 0), mock.patch.object(proxy.time, "monotonic", return_value=1000 + proxy.GZIP_PROFILE_TTL_S + 1):
            self.assertEqual(proxy._upload_estimate(headers), 0)

    def test_disabled_gzip_or_explicit_bandwidth_does_not_collect_upload_samples(self):
        body = b"x" * 1000000
        headers = {"authorization": "Bearer synthetic"}
        metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=False, REQUEST_GZIP_UPLOAD_MBIT_S=0):
            proxy._record_upload_observation(body, headers, 0.1, metadata, header_wait_s=0.01)
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=100):
            proxy._record_upload_observation(body, headers, 0.1, metadata, header_wait_s=0.01)
        self.assertFalse(proxy._UPLOAD_OBSERVATIONS)

    def test_small_uploads_do_not_train_from_socket_buffer_timing(self):
        metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
            proxy._record_upload_observation(b"x" * 256000, {}, 0.001, metadata, header_wait_s=0.01)
        self.assertFalse(proxy._UPLOAD_OBSERVATIONS)

    def test_buffered_writes_have_a_conservative_ceiling_without_limiting_an_override(self):
        body = b"x" * 1000000
        metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
            for _ in range(2):
                proxy._record_upload_observation(body, {}, 0.0001, metadata, header_wait_s=0.01)
            self.assertEqual(proxy._upload_estimate({}), 1000)
        with mock.patch.object(proxy, "REQUEST_GZIP_UPLOAD_MBIT_S", 10000):
            self.assertEqual(proxy._upload_estimate({}), 10000)

    def test_long_unaccounted_header_wait_clears_fast_hints_without_training_a_slow_rate(self):
        body = b"x" * 1000000
        metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
            for _ in range(2):
                proxy._record_upload_observation(body, {}, 0.001, metadata, header_wait_s=0.01)
            self.assertEqual(proxy._upload_estimate({}), 1000)
            proxy._record_upload_observation(body, {}, 0.001, metadata, header_wait_s=1)
            self.assertEqual(proxy._upload_estimate({}), 0)
            self.assertFalse(proxy._UPLOAD_OBSERVATIONS)
            for _ in range(2):
                proxy._record_upload_observation(body, {}, 0.001, metadata)
            self.assertFalse(proxy._UPLOAD_OBSERVATIONS)

    def test_body_backpressure_supplies_a_rate_without_adding_header_wait(self):
        metadata = httpx.Response(200, extensions={"network_stream": object()})
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
            for _ in range(2):
                proxy._record_upload_observation(b"x" * 1000000, {}, 0.2, metadata, header_wait_s=0.4)
            self.assertEqual(proxy._upload_estimate({}), 40)


class RatePartitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_client = proxy._SHARED_CLIENT
        proxy.reset_rate_limit_state()
        self.patches = mock.patch.multiple(
            proxy, RATE_LIMIT_SCOPE="credential", RATE_LIMIT_MAX_PARTITIONS=256,
            RATE_LIMIT_BASE_DELAY_S=0.05, RATE_LIMIT_MAX_DELAY_S=0.4,
            MAX_RATE_LIMIT_WAIT_S=8.0, MAX_TRANSIENT_RETRIES=2, RECOVERY_STAGGER_S=0.02,
            REQUEST_GZIP_ENABLED=False,
        )
        self.patches.start()
        self.calls = []
        self.a_calls = 0

        async def upstream(request):
            auth = request.headers.get("authorization", "")
            self.calls.append((auth, time.monotonic()))
            if auth == "Bearer quota-a":
                self.a_calls += 1
                if self.a_calls == 1:
                    # Keep the controlled gate alive while inspecting/cancelling
                    # it, even on a loaded Windows 3.10 debug event loop.
                    return httpx.Response(429, headers={"retry-after": "4"},
                                          json={"error": {"message": "too many requests"}})
            return httpx.Response(200, json={"status": "completed", "output": []})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        proxy._SHARED_CLIENT = self.upstream
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://synthetic")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.patches.stop()
        proxy.reset_rate_limit_state()

    async def begin_limited_request(self):
        task = asyncio.create_task(self.client.post("/v1/responses", json={"input": "A"},
                                                   headers={"authorization": "Bearer quota-a"}))
        key = proxy._credential_fingerprint({"authorization": "Bearer quota-a"})

        def has_cooldown():
            state = proxy._RATE_LIMIT_STATE if proxy.RATE_LIMIT_SCOPE == "shared" else proxy._RATE_LIMIT_STATES.get(key)
            return state is not None and state["cooldown_until"] > time.monotonic()

        await until(has_cooldown)
        state = proxy._RATE_LIMIT_STATE if proxy.RATE_LIMIT_SCOPE == "shared" else proxy._RATE_LIMIT_STATES[key]
        return task, state

    async def test_one_keys_429_does_not_delay_or_clear_another_keys_gate(self):
        task, limited = await self.begin_limited_request()
        try:
            other = await asyncio.wait_for(self.client.post("/v1/responses", json={"input": "B"},
                                             headers={"authorization": "Bearer quota-b"}), timeout=0.25)
            self.assertEqual(other.status_code, 200)
            self.assertGreater(limited["cooldown_until"], time.monotonic())
            self.assertEqual(self.a_calls, 1)
        finally:
            self.assertEqual((await task).status_code, 200)
        self.assertTrue(all(state["active"] == 0 for state in proxy._RATE_LIMIT_STATES.values()))

    async def test_shared_quota_setting_still_holds_other_credentials(self):
        with mock.patch.object(proxy, "RATE_LIMIT_SCOPE", "shared"):
            task, state = await self.begin_limited_request()
            other = asyncio.create_task(self.client.post("/v1/responses", json={"input": "B"},
                                                        headers={"authorization": "Bearer quota-b"}))
            try:
                await until(lambda: state["active"] == 2)
                self.assertFalse(any(key == "Bearer quota-b" for key, _ in self.calls))
                self.assertGreater(state["cooldown_until"], time.monotonic())
            finally:
                self.assertEqual((await task).status_code, 200)
                self.assertEqual((await other).status_code, 200)
            self.assertEqual(state["active"], 0)

    async def test_cancelling_a_waiter_releases_its_partition_reference(self):
        task, state = await self.begin_limited_request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state["active"], 0)
        self.assertEqual(self.a_calls, 1)

    async def test_active_and_cooling_gates_are_never_evicted_at_capacity(self):
        with mock.patch.object(proxy, "RATE_LIMIT_MAX_PARTITIONS", 2):
            a = proxy._acquire_rate_limit_state({"authorization": "Bearer synthetic-a"})
            b = proxy._acquire_rate_limit_state({"authorization": "Bearer synthetic-b"})
            proxy._record_rate_limit_cooldown(10, a)
            overflow = proxy._acquire_rate_limit_state({"authorization": "Bearer synthetic-c"})
            self.assertIs(overflow, proxy._OVERFLOW_RATE_LIMIT_STATE)
            proxy._release_rate_limit_state(overflow)
            proxy._release_rate_limit_state(b)
            d = proxy._acquire_rate_limit_state({"authorization": "Bearer synthetic-d"})
            same_a = proxy._acquire_rate_limit_state({"authorization": "Bearer synthetic-a"})
            self.assertIs(same_a, a)
            self.assertEqual(len(proxy._RATE_LIMIT_STATES), 2)
            self.assertNotIn("synthetic-a", repr(proxy._RATE_LIMIT_STATES))
            proxy._release_rate_limit_state(same_a)
            proxy._release_rate_limit_state(a)
            proxy._release_rate_limit_state(d)


if __name__ == "__main__":
    unittest.main()
