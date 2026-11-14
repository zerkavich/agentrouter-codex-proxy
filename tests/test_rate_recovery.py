"""429 concurrency and retry-budget regressions using synthetic requests."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
import sys
import time
import unittest
from unittest import mock
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


async def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("Condition did not become true")
        await asyncio.sleep(0.002)


class RetryHintTests(unittest.TestCase):
    def test_authoritative_retry_after_is_not_shortened_by_backoff_cap(self):
        with mock.patch.object(proxy, "RATE_LIMIT_MAX_DELAY_S", 15), mock.patch.object(proxy.random, "uniform", return_value=0):
            delay, hint = proxy._extract_retry_delay_s({"Retry-After": "120.1"}, "", 1)
        self.assertGreaterEqual(delay, 120.1)
        self.assertGreaterEqual(hint, 121)

    def test_milliseconds_text_is_parsed_as_milliseconds(self):
        with mock.patch.object(proxy.random, "uniform", return_value=0):
            delay, hint = proxy._extract_retry_delay_s({}, "Please retry after 150 milliseconds.", 1)
        self.assertLessEqual(delay, 1)
        self.assertEqual(hint, 1)

    def test_nonfinite_and_negative_hints_do_not_poison_cooldowns(self):
        for raw in ("NaN", "Infinity", "-1"):
            with self.subTest(raw=raw), mock.patch.object(proxy.random, "uniform", return_value=0):
                delay, hint = proxy._extract_retry_delay_s({"retry-after": raw}, "", 1)
                self.assertTrue(math.isfinite(delay))
                self.assertIsNone(hint)

    def test_invalid_millisecond_header_does_not_hide_valid_retry_after(self):
        with mock.patch.object(proxy.random, "uniform", return_value=0):
            delay, hint = proxy._extract_retry_delay_s({"retry-after-ms": "NaN", "retry-after": "2"}, "", 1)
        self.assertGreaterEqual(delay, 2)
        self.assertEqual(hint, 2)


class RateGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        proxy.reset_rate_limit_state()
        self.patch = mock.patch.multiple(proxy, RECOVERY_STAGGER_S=0.15)
        self.patch.start()
        self.disconnect_patch = mock.patch.object(proxy, "_client_disconnected", return_value=False)
        self.disconnect_patch.start()

    async def asyncTearDown(self):
        self.disconnect_patch.stop()
        self.patch.stop()
        proxy.reset_rate_limit_state()

    def state_with_future_slot(self):
        state = proxy._new_rate_limit_state()
        now = time.monotonic()
        state.update(streak=1, last_limit_at=now, next_launch_after=now + 0.2)
        return state

    async def test_cancelled_waiter_does_not_spend_a_launch_slot(self):
        state = self.state_with_future_slot()
        original = state["next_launch_after"]
        task = asyncio.create_task(proxy._wait_for_rate_limit_gate(object(), state))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state["next_launch_after"], original)

    async def test_disconnected_waiter_does_not_spend_a_launch_slot(self):
        state = self.state_with_future_slot()
        original = state["next_launch_after"]
        disconnected = False

        async def check(request):
            return disconnected

        with mock.patch.object(proxy, "_client_disconnected", side_effect=check):
            task = asyncio.create_task(proxy._wait_for_rate_limit_gate(object(), state))
            await asyncio.sleep(0.02)
            disconnected = True
            self.assertFalse(await asyncio.wait_for(task, 1))
        self.assertEqual(state["next_launch_after"], original)

    async def test_new_429_during_stagger_is_rechecked_before_launch(self):
        state = self.state_with_future_slot()
        task = asyncio.create_task(proxy._wait_for_rate_limit_gate(object(), state))
        try:
            await asyncio.sleep(0.02)
            proxy._record_rate_limit_cooldown(0.35, state)
            new_deadline = state["cooldown_until"]
            self.assertTrue(await asyncio.wait_for(task, 2))
            self.assertGreaterEqual(time.monotonic(), new_deadline)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_immediate_disconnect_counts_actual_sleep_only(self):
        before = proxy.STATS["rate_limit_wait_seconds_total"]
        with mock.patch.object(proxy, "_client_disconnected", return_value=True):
            self.assertFalse(await proxy._sleep_unless_disconnected(10, object()))
        self.assertLess(proxy.STATS["rate_limit_wait_seconds_total"] - before, 0.1)

    async def test_cancelled_sleep_counts_elapsed_wait(self):
        before = proxy.STATS["rate_limit_wait_seconds_total"]
        entered = asyncio.Event()

        async def mark_started(request):
            entered.set()
            return False

        with mock.patch.object(proxy, "_client_disconnected", side_effect=mark_started):
            task = asyncio.create_task(proxy._sleep_unless_disconnected(10, object()))
            await asyncio.wait_for(entered.wait(), 2)
            observed_start = time.perf_counter()
            await asyncio.sleep(0.025)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            observed_elapsed = time.perf_counter() - observed_start
        elapsed = proxy.STATS["rate_limit_wait_seconds_total"] - before
        self.assertGreater(elapsed, 0.01)
        self.assertLess(elapsed, observed_elapsed + 0.02)


class RateRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    headers = {"authorization": "Bearer synthetic-rate-recovery"}

    async def asyncSetUp(self):
        self.previous_client = proxy._SHARED_CLIENT
        proxy.reset_rate_limit_state()
        self.patch = mock.patch.multiple(
            proxy, REQUEST_GZIP_ENABLED=False, UPSTREAM="http://synthetic.invalid",
            RATE_LIMIT_BASE_DELAY_S=0.05, RATE_LIMIT_MAX_DELAY_S=0.5,
            MAX_RATE_LIMIT_WAIT_S=2, MAX_TRANSIENT_RETRIES=1, RECOVERY_STAGGER_S=0.01,
        )
        self.patch.start()
        self.disconnect_patch = mock.patch.object(proxy, "_client_disconnected", return_value=False)
        self.disconnect_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy")
        self.upstream = None

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.upstream is not None:
            await self.upstream.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.disconnect_patch.stop()
        self.patch.stop()
        proxy.reset_rate_limit_state()

    def use_upstream(self, responder):
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        proxy._SHARED_CLIENT = self.upstream

    def state(self):
        return proxy._RATE_LIMIT_STATES[proxy._credential_fingerprint(self.headers)]

    async def post(self, text="hi"):
        return await self.client.post("/v1/responses", headers=self.headers, json={"input": text})

    async def test_older_success_cannot_clear_newer_429(self):
        old_started, release_old = asyncio.Event(), asyncio.Event()
        calls = 0

        async def responder(request):
            nonlocal calls
            if b'"old"' in request.content:
                old_started.set()
                await release_old.wait()
                return httpx.Response(200, json={"output": "old complete"})
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"retry-after": "0.4"}, json={"error": {"message": "rate limit"}})
            return httpx.Response(200, json={"output": "recovered"})

        self.use_upstream(responder)
        old_task = asyncio.create_task(self.post("old"))
        limited_task = None
        try:
            await asyncio.wait_for(old_started.wait(), 1)
            limited_task = asyncio.create_task(self.post("limited"))
            await until(lambda: self.state()["streak"] > 0)
            cooldown = self.state()["cooldown_until"]
            release_old.set()
            self.assertEqual((await old_task).status_code, 200)
            self.assertEqual(self.state()["cooldown_until"], cooldown)
            self.assertGreater(self.state()["streak"], 0)
            self.assertEqual((await limited_task).status_code, 200)
        finally:
            release_old.set()
            for task in (old_task, limited_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[t for t in (old_task, limited_task) if t is not None], return_exceptions=True)

    async def test_hint_beyond_retry_budget_returns_without_early_retry(self):
        calls = []

        def responder(request):
            calls.append(time.monotonic())
            return httpx.Response(429, headers={"retry-after": "120"}, json={"error": {"message": "rate limit"}})

        self.use_upstream(responder)
        with mock.patch.multiple(proxy, MAX_RATE_LIMIT_WAIT_S=0.1, RATE_LIMIT_MAX_DELAY_S=0.025):
            result = await asyncio.wait_for(self.post(), 1)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(self.state()["cooldown_until"] - time.monotonic(), 119)
        self.assertGreaterEqual(int(result.headers["retry-after"]), 120)

    async def test_exhausted_retry_count_still_protects_shared_cooldown(self):
        self.use_upstream(lambda request: httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "rate limit"}}))
        with mock.patch.object(proxy, "MAX_TRANSIENT_RETRIES", 0):
            result = await self.post()
        self.assertEqual(result.status_code, 429)
        self.assertGreater(self.state()["cooldown_until"], time.monotonic())

    async def test_existing_gate_wait_obeys_request_retry_budget(self):
        calls = []
        self.use_upstream(lambda request: calls.append(request) or httpx.Response(200, json={"output": "ok"}))
        state = proxy._acquire_rate_limit_state(self.headers)
        proxy._record_rate_limit_cooldown(10, state)
        proxy._release_rate_limit_state(state)
        with mock.patch.object(proxy, "MAX_RATE_LIMIT_WAIT_S", 0.08):
            try:
                result = await asyncio.wait_for(self.post(), 0.5)
            except asyncio.TimeoutError:
                self.fail("A request remained in the rate-limit gate beyond its retry budget")
        self.assertEqual(result.status_code, 429)
        self.assertEqual(calls, [])
        self.assertGreaterEqual(int(result.headers["retry-after"]), 9)


if __name__ == "__main__":
    unittest.main()
