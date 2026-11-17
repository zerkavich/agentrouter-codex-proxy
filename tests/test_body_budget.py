"""Admission, cancellation and incremental incoming-body allocation controls."""
import asyncio
import gc
import hashlib
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest import mock

from starlette.requests import Request, ClientDisconnect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


def request(chunks, hint=None, disconnect=False):
    chunks = iter(chunks)
    async def receive():
        try:
            body = next(chunks)
            return {'type': 'http.request', 'body': body, 'more_body': True}
        except StopIteration:
            return {'type': 'http.disconnect'} if disconnect else {'type': 'http.request', 'body': b'', 'more_body': False}
    headers = [] if hint is None else [(b'content-length', str(hint).encode())]
    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses', 'headers': headers}, receive)


class BodyBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_oversized_and_cancelled_waiters_leave_no_reservations(self):
        budget = proxy._BodyBudget(10)
        self.assertEqual(await budget.acquire(8), 8)
        oversized = asyncio.create_task(budget.acquire(15))
        cancelled = asyncio.create_task(budget.acquire(1))
        later = asyncio.create_task(budget.acquire(2))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        budget.release(8)
        self.assertEqual(await asyncio.wait_for(oversized, 1), 15)
        self.assertFalse(later.done())
        budget.release(15)
        self.assertEqual(await asyncio.wait_for(later, 1), 2)
        budget.release(2)
        self.assertEqual(budget.used, 0)
        self.assertFalse(budget.queue)

    async def test_cancel_after_grant_releases_the_granted_weight(self):
        budget = proxy._BodyBudget(10)
        await budget.acquire(10)
        waiting = asyncio.create_task(budget.acquire(10))
        await asyncio.sleep(0)
        budget.release(10)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(budget.used, 0)
        self.assertFalse(budget.queue)

    async def test_timeout_cleans_up_and_next_request_can_enter(self):
        budget = proxy._BodyBudget(10)
        await budget.acquire(10)
        with mock.patch.object(proxy, 'BODY_BUDGET_WAIT_S', 0.02):
            with self.assertRaises(proxy._BodyBudgetExpired):
                await budget.acquire(1)
        self.assertEqual(budget.timeouts, 1)
        budget.release(10)
        self.assertEqual(await budget.acquire(1), 1)
        budget.release(1)

    async def test_known_size_waits_before_reading_the_large_body(self):
        budget = proxy._BodyBudget(3 * 2**20)
        await budget.acquire(budget.limit)
        body_request = request([b'A' * 2**20], 2**20)
        lease = proxy._BodyLease()
        with mock.patch.object(proxy, '_body_budget', return_value=budget):
            task = asyncio.create_task(proxy._read_request_body(body_request, lease))
            await asyncio.sleep(0)
            self.assertFalse(body_request._stream_consumed)
            self.assertFalse(task.done())
            budget.release(budget.limit)
            self.assertEqual(await asyncio.wait_for(task, 1), b'A' * 2**20)
        lease.release()
        self.assertEqual(budget.used, 0)

    async def test_multichunk_bytes_preserved_with_missing_or_inexact_size_hint(self):
        chunks = [b'{ "input":"', 'Привет 🌍'.encode() * 200000, b'", "n":0.1234567890123456789 }\n']
        expected = b''.join(chunks)
        for hint in (None, len(expected), len(expected) + 100000, 2**20, 100):
            with self.subTest(hint=hint):
                body_request, lease = request(chunks, hint), proxy._BodyLease()
                try:
                    self.assertEqual(await proxy._read_request_body(body_request, lease), expected)
                    self.assertEqual(await body_request.body(), expected)
                    self.assertEqual(lease.weight, len(expected) * proxy.BODY_RESERVATION_FACTOR)
                finally:
                    lease.release()
        self.assertEqual(proxy._body_budget().used, 0)

    async def test_unknown_irregular_chunks_keep_exact_bytes_across_growth_boundaries(self):
        chunks = [b'A' * 3, b'B' * 1000000, b'C' * (9 * 2**20), b'D' * 17]
        expected = b''.join(chunks)
        lease = proxy._BodyLease()
        try:
            self.assertEqual(await proxy._read_request_body(request(chunks), lease), expected)
            self.assertEqual(lease.weight, len(expected) * proxy.BODY_RESERVATION_FACTOR)
        finally:
            lease.release()

    async def test_prefetched_body_replays_once_after_cancelled_or_granted_admission(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                budget = proxy._BodyBudget(3 * 2**20)
                await budget.acquire(budget.limit)
                reads = 0
                messages = [{'type': 'http.request', 'body': b'A' * 2**20, 'more_body': False}]
                async def receive():
                    nonlocal reads
                    reads += 1
                    if messages:
                        return messages.pop(0)
                    await asyncio.Event().wait()
                req = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
                               'headers': [(b'content-length', str(2**20).encode())]}, receive)
                lease = proxy._BodyLease()
                with mock.patch.object(proxy, '_body_budget', return_value=budget):
                    task = asyncio.create_task(lease.reserve_incoming(req, 2**20))
                    while reads == 0:
                        await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        budget.release(budget.limit)
                        await asyncio.wait_for(task, 1)
                    self.assertEqual(await req.body(), b'A' * 2**20)
                    # At most one extra read watches disconnect after the final
                    # body event; cached bytes must never be read a second time.
                    self.assertLessEqual(reads, 2)
                lease.release()
                if cancel:
                    budget.release(budget.limit)
                self.assertEqual(budget.used, 0)
                self.assertFalse(budget.queue)

    async def test_upgrading_compressed_leases_cannot_deadlock_each_other(self):
        budget = proxy._BodyBudget(6 * 2**20)
        leases = [proxy._BodyLease(), proxy._BodyLease()]
        with mock.patch.object(proxy, '_body_budget', return_value=budget):
            for lease in leases:
                await lease.reserve(2**20)
                lease.shrink(100)
            first = asyncio.create_task(leases[0].reserve(2 * 2**20))
            second = asyncio.create_task(leases[1].reserve(2 * 2**20))
            await asyncio.sleep(0)
            await asyncio.wait_for(first, 1)
            self.assertFalse(second.done())
            leases[0].release()
            await asyncio.wait_for(second, 1)
        leases[1].release()
        self.assertEqual(budget.used, 0)
        self.assertFalse(budget.queue)

    async def test_timed_out_handler_returns_retryable_busy_and_releases_waiter(self):
        budget = proxy._BodyBudget(3 * 2**20)
        await budget.acquire(budget.limit)
        body_request = request([b'A' * 2**20], 2**20)
        with mock.patch.object(proxy, '_body_budget', return_value=budget), mock.patch.object(proxy, 'BODY_BUDGET_WAIT_S', .02):
            response = await proxy.handle_responses(body_request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '2')
        self.assertIn(b'proxy_memory_busy', response.body)
        self.assertEqual(budget.used, budget.limit)
        self.assertFalse(budget.queue)
        budget.release(budget.limit)

    async def test_disabled_admission_still_preserves_large_body_bytes(self):
        lease = proxy._BodyLease()
        with mock.patch.object(proxy, 'BODY_BUDGET_BYTES', 0):
            self.assertEqual(await proxy._read_request_body(request([b'A' * 2**20], 2**20), lease), b'A' * 2**20)
        self.assertEqual(lease.weight, 0)
        self.assertIsNone(lease.budget)

    async def test_unknown_oversized_upload_keeps_an_accurate_exclusive_estimate(self):
        budget = proxy._BodyBudget(3 * 2**20)
        lease = proxy._BodyLease()
        with mock.patch.object(proxy, '_body_budget', return_value=budget):
            body = await proxy._read_request_body(request([b'A' * 2**20, b'B' * 2**20]), lease)
        self.assertEqual(lease.weight, len(body) * proxy.BODY_RESERVATION_FACTOR)
        self.assertEqual(budget.used, lease.weight)
        self.assertGreater(budget.used, budget.limit)
        lease.release()
        self.assertEqual(budget.used, 0)

    async def test_disconnect_after_complete_prefetched_body_cancels_admission(self):
        budget = proxy._BodyBudget(3 * 2**20)
        await budget.acquire(budget.limit)
        messages = [{'type': 'http.request', 'body': b'A' * 2**20, 'more_body': False},
                    {'type': 'http.disconnect'}]
        async def receive():
            return messages.pop(0)
        req = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
                       'headers': [(b'content-length', str(2**20).encode())]}, receive)
        lease = proxy._BodyLease()
        with mock.patch.object(proxy, '_body_budget', return_value=budget):
            with self.assertRaises(ClientDisconnect):
                await asyncio.wait_for(proxy._read_request_body(req, lease), 1)
        lease.release()
        self.assertFalse(budget.queue)
        self.assertEqual(budget.used, budget.limit)
        budget.release(budget.limit)

    async def test_disconnect_during_body_does_not_cache_or_leak(self):
        body_request, lease = request([b'A' * 2**20], 2**20, True), proxy._BodyLease()
        try:
            with self.assertRaises(ClientDisconnect):
                await proxy._read_request_body(body_request, lease)
            self.assertFalse(hasattr(body_request, '_body'))
        finally:
            lease.release()
        self.assertEqual(proxy._body_budget().used, 0)

    async def test_disconnect_while_waiting_for_incoming_admission_releases_queue(self):
        budget = proxy._BodyBudget(3 * 2**20)
        await budget.acquire(budget.limit)
        lease = proxy._BodyLease()
        with mock.patch.object(proxy, '_body_budget', return_value=budget):
            with self.assertRaises(ClientDisconnect):
                await asyncio.wait_for(proxy._read_request_body(request([], 2**20, True), lease), 1)
        lease.release()
        self.assertFalse(budget.queue)
        self.assertEqual(budget.used, budget.limit)
        budget.release(budget.limit)

    async def test_incremental_body_peak_avoids_a_second_full_copy(self):
        size = 16 * 2**20
        unit = b'0123456789ABCDEF' * 4096
        expected = hashlib.sha256(unit * (size // len(unit))).hexdigest()
        gc.collect()
        tracemalloc.start(1)
        lease = proxy._BodyLease()
        try:
            # New bytes for every receive event, not references to one fixture.
            chunks = (bytes(bytearray(unit)) for _ in range(size // len(unit)))
            body = await proxy._read_request_body(request(chunks, size), lease)
            peak = tracemalloc.get_traced_memory()[1]
            self.assertEqual(hashlib.sha256(body).hexdigest(), expected)
            self.assertLess(peak, size + 2 * 2**20)
        finally:
            tracemalloc.stop()
            lease.release()


if __name__ == '__main__':
    unittest.main()
