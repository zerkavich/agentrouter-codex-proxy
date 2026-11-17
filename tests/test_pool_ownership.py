"""Interrupted real HTTPcore response closes must not strand pool ownership."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy
import doctor
from telemetry import REQUEST_RECORD, RequestRecord


class PoolOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers = []
        self.responses = []
        self.pending = set()
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        self.client = httpx.AsyncClient(trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
            timeout=httpx.Timeout(3, pool=.15))

    async def asyncTearDown(self):
        for response in self.responses:
            await response.aclose()
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def serve(self, reader, writer):
        self.writers.append(writer)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            if headers.startswith(b'GET /ok '):
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
                await writer.drain()
                return
            self.pending.add(writer)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 65536\r\n\r\n')
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.pending.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def send(self, path, *, tracked=True):
        request = self.client.build_request('GET', self.url + path)
        response = (await proxy._send_with_capacity(self.client, request, stream=True)
                    if tracked else await self.client.send(request, stream=True))
        self.responses.append(response)
        return response

    @staticmethod
    def core_stream(response):
        # This fixture targets the repository's pinned HTTPX/HTTPcore versions.
        bound = response.stream.stream if isinstance(response.stream, proxy._CapacityStream) else response.stream
        return bound._stream._httpcore_stream

    async def interrupt_close(self, response, *, failure=False):
        # HTTPcore marks its PoolByteStream closed before awaiting the protocol
        # close. Interrupt that await: the ACTIVE connection and owning pool
        # request remain, while HTTPX will never enter aclose a second time.
        core = self.core_stream(response)
        entered = asyncio.Event()

        async def stalled_close():
            entered.set()
            if failure:
                raise RuntimeError('Synthetic protocol close failure')
            await asyncio.Event().wait()

        with mock.patch.object(core._stream, 'aclose', side_effect=stalled_close):
            task = asyncio.create_task(response.aclose())
            await asyncio.wait_for(entered.wait(), 3)
            if failure:
                with self.assertRaisesRegex(RuntimeError, 'Synthetic protocol close failure'):
                    await task
            else:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(response.is_closed)
        self.assertTrue(core._closed)

    async def assert_recovered(self):
        pool = self.client._transport._pool
        followup = await self.send('/ok')
        self.assertEqual(await followup.aread(), b'OK')
        self.assertIs(self.client._transport._pool, pool)
        self.assertEqual(pool._requests, [])
        capacity = proxy._get_upstream_capacity(self.client)
        self.assertEqual((capacity.in_use, capacity.waiters), (0, 0))
        for _ in range(100):
            if not self.pending:
                break
            await asyncio.sleep(.005)
        self.assertFalse(self.pending)

    async def test_cancelled_closes_cannot_exhaust_the_real_pool(self):
        responses = [await self.send('/body') for _ in range(2)]
        for response in responses:
            await self.interrupt_close(response)
        pool = self.client._transport._pool
        self.assertEqual(len(pool._requests), 2)
        self.assertTrue(all(not connection.is_idle() and not connection.is_closed()
                            for connection in pool.connections))
        self.assertEqual(proxy._get_upstream_capacity(self.client).in_use, 0)
        with mock.patch.object(proxy, '_SHARED_CLIENT', self.client):
            snapshot = await proxy.diagnostics()
            self.assertEqual(snapshot['connection_pool']['released_requests'], 2)
            report = doctor.sanitize_snapshot(await proxy.health(), snapshot)
            self.assertEqual(report['connection_pool']['released_requests'], 2)
            record = RequestRecord('POST', '/v1/responses')
            token = REQUEST_RECORD.set(record)
            try:
                await self.assert_recovered()
            finally:
                REQUEST_RECORD.reset(token)
            self.assertEqual([event[0] for event in record.attempts], ['pool_recovery'])
            report = doctor.sanitize_snapshot(await proxy.health(), await proxy.diagnostics())
            self.assertEqual(report['connection_pool']['released_requests'], 0)
            self.assertGreaterEqual(report['counts']['upstream_orphaned_requests_reclaimed'], 2)

    async def test_failed_close_is_reclaimed_without_interrupting_an_owned_response(self):
        healthy = await self.send('/body')
        healthy_core = self.core_stream(healthy)
        healthy_connection = healthy_core._pool_request.connection
        abandoned = await self.send('/body')
        await self.interrupt_close(abandoned, failure=True)
        followup = await self.send('/ok')
        self.assertEqual(await followup.aread(), b'OK')
        self.assertFalse(healthy.is_closed)
        self.assertFalse(healthy_connection.is_closed())
        self.assertEqual(len(self.client._transport._pool._requests), 1)
        self.assertEqual(proxy._get_upstream_capacity(self.client).in_use, 1)
        await healthy.aclose()
        await self.assert_recovered()

    async def test_untracked_legitimate_pool_request_is_preserved(self):
        healthy = await self.send('/body', tracked=False)
        healthy_connection = self.core_stream(healthy)._pool_request.connection
        abandoned = await self.send('/body')
        await self.interrupt_close(abandoned)
        followup = await self.send('/ok')
        self.assertEqual(await followup.aread(), b'OK')
        self.assertFalse(healthy.is_closed)
        self.assertFalse(healthy_connection.is_closed())
        self.assertEqual(len(self.client._transport._pool._requests), 1)
        await healthy.aclose()
        await self.assert_recovered()

    async def test_sixty_interrupted_closes_do_not_accumulate_requests_or_sockets(self):
        # More abandoned responses than the production pool's 50-slot limit.
        for _ in range(30):
            for _ in range(2):
                await self.interrupt_close(await self.send('/body'))
            await self.assert_recovered()

    async def test_close_still_in_progress_retains_its_ownership(self):
        healthy = await self.send('/body')
        closing = await self.send('/body')
        core = self.core_stream(closing)
        connection = core._pool_request.connection
        entered, release = asyncio.Event(), asyncio.Event()
        original_close = core._stream.aclose

        async def slow_close():
            entered.set()
            await release.wait()
            await original_close()

        with mock.patch.object(core._stream, 'aclose', side_effect=slow_close):
            task = asyncio.create_task(closing.aclose())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                with self.assertRaises(httpx.PoolTimeout):
                    await self.client.get(self.url + '/ok', timeout=httpx.Timeout(3, pool=.05))
                self.assertFalse(connection.is_closed())
                self.assertEqual(proxy._get_upstream_capacity(self.client).in_use, 2)
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        await healthy.aclose()
        await self.assert_recovered()

    async def test_reusing_a_request_preserves_the_earlier_responses_ownership(self):
        request = self.client.build_request('GET', self.url + '/body')
        healthy = await proxy._send_with_capacity(self.client, request, stream=True)
        self.responses.append(healthy)
        healthy_connection = self.core_stream(healthy)._pool_request.connection
        abandoned = await proxy._send_with_capacity(self.client, request, stream=True)
        self.responses.append(abandoned)
        await self.interrupt_close(abandoned)
        followup = await self.send('/ok')
        self.assertEqual(await followup.aread(), b'OK')
        self.assertFalse(healthy.is_closed)
        self.assertFalse(healthy_connection.is_closed())
        self.assertEqual(len(self.client._transport._pool._requests), 1)
        await healthy.aclose()
        await self.assert_recovered()

    async def test_final_pool_error_keeps_a_fixed_history_code(self):
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': b'{"input":"synthetic","stream":false}', 'more_body': False}
            await asyncio.sleep(100)

        request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
            'query_string': b'', 'headers': [(b'content-type', b'application/json')]}, receive)
        record = RequestRecord('POST', '/v1/responses')
        token = REQUEST_RECORD.set(record)
        try:
            with (mock.patch.object(proxy, 'get_shared_client', return_value=self.client),
                  mock.patch.object(proxy, '_send_with_transport_retry', side_effect=httpx.PoolTimeout('synthetic')),
                  mock.patch.object(proxy, 'MAX_TRANSIENT_RETRIES', 0)):
                response = await proxy.handle_responses(request)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(record.error, 'upstream_pool_timeout')
        finally:
            REQUEST_RECORD.reset(token)


if __name__ == '__main__':
    unittest.main()
