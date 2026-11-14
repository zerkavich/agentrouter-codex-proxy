"""Protect real HTTPX pools from cancellation of queued upstream requests."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import httpx
import httpcore
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Capacity fixture did not reach the expected state")
        await asyncio.sleep(.005)


class UpstreamCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers = []
        self.pending = set()
        self.disconnect = asyncio.Event()
        self.tasks = []
        self.responses = []
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(5, pool=5),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0), trust_env=False)
        self.previous_client = proxy._SHARED_CLIENT
        proxy._SHARED_CLIENT = self.client
        self.patch = mock.patch.multiple(proxy, UPSTREAM=self.url, REQUEST_GZIP_ENABLED=False)
        self.patch.start()
        proxy.reset_rate_limit_state()

    async def asyncTearDown(self):
        self.disconnect.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for response in self.responses:
            await response.aclose()
        await self.client.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.patch.stop()
        proxy.reset_rate_limit_state()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def serve(self, reader, writer):
        self.writers.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in headers.splitlines()
                           if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            if headers.startswith(b"GET /ok "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
                return
            self.pending.add(writer)
            if headers.startswith(b"GET /body "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.pending.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def request(self, index):
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b'{"input":"synthetic","stream":false}', "more_body": False}
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        return Request({"type": "http", "method": "POST", "path": "/v1/responses", "query_string": b"",
                        "headers": [(b"content-type", b"application/json"),
                                    (b"authorization", f"Bearer synthetic-burst-{index}".encode())]}, receive)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    async def send(self, path, **kwargs):
        request = self.client.build_request("GET", self.url + path, **kwargs)
        response = await proxy._send_with_capacity(self.client, request, stream=True)
        self.responses.append(response)
        return response

    async def fill(self):
        for _ in range(2):
            await self.send("/body")
        await until(lambda: len(self.pending) == 2)
        capacity = proxy._get_upstream_capacity(self.client)
        self.assertEqual(capacity.limit, 2)
        self.assertEqual(capacity.in_use, 2)
        return capacity

    async def assert_available(self):
        capacity = proxy._get_upstream_capacity(self.client)
        self.assertEqual(capacity.in_use, 0)
        self.assertEqual(capacity.waiters, 0)
        self.assertEqual(len(self.client._transport._pool._requests), 0)
        self.assertFalse(any(getattr(connection, "_connection", True) is None
                             for connection in self.client._transport._pool.connections))
        response = await self.send("/ok")
        self.assertEqual(await response.aread(), b"OK")
        self.assertEqual(capacity.in_use, 0)

    async def test_abandoned_bursts_wait_outside_httpcore_then_reuse_the_same_pool(self):
        for round_number in range(2):
            self.disconnect.clear()
            tasks = [self.start(proxy.handle_responses(self.request(round_number * 100 + index)))
                     for index in range(34)]
            capacity = proxy._get_upstream_capacity(self.client)
            await until(lambda: capacity.in_use == 2 and capacity.waiters == 32 and len(self.pending) == 2)
            self.assertEqual(len(self.client._transport._pool._requests), 2)
            self.assertEqual(len(self.client._transport._pool.connections), 2)
            self.disconnect.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
            self.assertTrue(all(response.status_code == 499 for response in results))
            await until(lambda: not self.pending)
            self.assertTrue(all(state["active"] == 0 for state in proxy._RATE_LIMIT_STATES.values()))
            await self.assert_available()

    async def test_response_headers_do_not_release_capacity_before_body_closes(self):
        capacity = await self.fill()
        queued = self.start(self.send("/ok"))
        await until(lambda: capacity.waiters == 1)
        self.assertFalse(queued.done())
        self.assertEqual(len(self.client._transport._pool._requests), 2)
        await self.responses[0].aclose()
        response = await asyncio.wait_for(queued, timeout=1)
        self.assertEqual(await response.aread(), b"OK")
        self.assertEqual(capacity.in_use, 1)
        await self.responses[1].aclose()
        await self.assert_available()

    async def test_cancelled_waiter_cannot_take_a_later_released_slot(self):
        capacity = await self.fill()
        queued = self.start(self.send("/ok"))
        await until(lambda: capacity.waiters == 1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(capacity.in_use, 2)
        self.assertEqual(capacity.waiters, 0)
        await self.responses[0].aclose()
        response = await asyncio.wait_for(self.send("/ok"), timeout=1)
        self.assertEqual(await response.aread(), b"OK")
        await self.responses[1].aclose()
        await self.assert_available()

    async def test_wait_timeout_uses_the_individual_requests_pool_timeout(self):
        capacity = await self.fill()
        request = self.client.build_request("GET", self.url + "/ok", timeout=httpx.Timeout(2, pool=.02))
        with self.assertRaises(httpx.PoolTimeout) as raised:
            await asyncio.wait_for(proxy._send_with_capacity(self.client, request, stream=True), timeout=1)
        self.assertIs(raised.exception.request, request)
        self.assertEqual(capacity.waiters, 0)
        self.assertEqual(capacity.in_use, 2)
        for response in self.responses:
            await response.aclose()
        await self.assert_available()

    async def test_cancelled_active_header_sends_release_admission_and_pool(self):
        tasks = [self.start(self.send("/headers")) for _ in range(2)]
        await until(lambda: len(self.pending) == 2)
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes))
        await until(lambda: not self.pending)
        await self.assert_available()

    async def test_separate_clients_keep_independent_capacity(self):
        capacity = await self.fill()
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1), trust_env=False) as other:
            request = other.build_request("GET", self.url + "/ok")
            response = await asyncio.wait_for(proxy._send_with_capacity(other, request, stream=False), timeout=1)
            self.assertEqual(response.content, b"OK")
            self.assertEqual(proxy._get_upstream_capacity(other).in_use, 0)
            self.assertEqual(proxy._get_upstream_capacity(other).limit, 1)
        self.assertEqual(capacity.in_use, 2)

    async def test_failed_sends_release_capacity_before_another_attempt(self):
        async def failure(request):
            raise httpx.ConnectError("Synthetic connection failure", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(failure), trust_env=False) as client:
            for _ in range(3):
                request = client.build_request("GET", "http://synthetic/ok")
                with self.assertRaises(httpx.ConnectError):
                    await proxy._send_with_capacity(client, request, stream=True)
                self.assertEqual(proxy._get_upstream_capacity(client).in_use, 0)

    async def test_a_failing_stream_close_does_not_leak_or_double_release_capacity(self):
        class FailingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"synthetic token"

            async def aclose(self):
                raise RuntimeError("Synthetic close failure")

        async def upstream(_request):
            return httpx.Response(200, stream=FailingStream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False) as client:
            response = await proxy._send_with_capacity(client, client.build_request("GET", "http://synthetic"), stream=True)
            self.assertEqual(proxy._get_upstream_capacity(client).in_use, 1)
            with self.assertRaisesRegex(RuntimeError, "Synthetic close failure"):
                await response.aclose()
            await response.aclose()
            self.assertEqual(proxy._get_upstream_capacity(client).in_use, 0)

    async def test_full_pool_of_abandoned_connecting_entries_recovers_without_replacing_client(self):
        pool = self.client._transport._pool
        origin = httpcore.Origin(scheme=b"http", host=b"127.0.0.1", port=self.server.sockets[0].getsockname()[1])
        # Reproduce the exact HTTPcore fault state seen in the mixed TCP soak:
        # all connection slots occupied, no pool request owns those entries.
        pool._connections.extend(pool.create_connection(origin) for _ in range(2))
        self.assertEqual(len(pool._requests), 0)
        self.assertTrue(all(connection._connection is None for connection in pool.connections))
        reclaimed_before = proxy.STATS["upstream_unstarted_connections_reclaimed"]
        response = await asyncio.wait_for(self.send("/ok"), timeout=1)
        self.assertEqual(await response.aread(), b"OK")
        self.assertIs(self.client._transport._pool, pool)
        self.assertEqual(proxy.STATS["upstream_unstarted_connections_reclaimed"] - reclaimed_before, 2)
        await self.assert_available()

    async def test_owned_connecting_entries_survive_another_queued_requests_cancellation(self):
        pool = self.client._transport._pool
        release = asyncio.Event()
        original_connect = pool._network_backend.connect_tcp

        async def delayed_connect(**kwargs):
            await release.wait()
            return await original_connect(**kwargs)

        reclaimed_before = proxy.STATS["upstream_unstarted_connections_reclaimed"]
        with mock.patch.object(pool._network_backend, "connect_tcp", side_effect=delayed_connect):
            sends = [self.start(self.send("/body")) for _ in range(2)]
            await until(lambda: len(pool._requests) == 2 and len(pool.connections) == 2)
            self.assertTrue(all(connection._connection is None for connection in pool.connections))
            # A direct HTTPX caller deliberately queues inside HTTPcore while
            # the proxy's two legitimate TCP connects are still pending.
            queued = self.start(self.client.get(self.url + "/ok"))
            await until(lambda: len(pool._requests) == 3)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertEqual(len(pool.connections), 2)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*sends), timeout=2)
        self.assertEqual(proxy.STATS["upstream_unstarted_connections_reclaimed"], reclaimed_before)
        for response in responses:
            await response.aclose()
        await self.assert_available()

    async def test_an_active_connection_without_a_pool_request_is_closed_and_reclaimed(self):
        proxy._get_upstream_capacity(self.client)
        pool = self.client._transport._pool
        response = await self.client.send(self.client.build_request("GET", self.url + "/body"), stream=True)
        await until(lambda: len(self.pending) == 1)
        connection = pool.connections[0]
        self.assertFalse(connection.is_idle())
        self.assertFalse(connection.is_closed())
        # Fault state from interrupted body cleanup: HTTPX already marks the
        # response closed and HTTPcore removes ownership before the ACTIVE
        # connection finishes closing. Leave the real TCP socket held open.
        response.is_closed = True
        pool._requests.clear()
        reclaimed_before = proxy.STATS["upstream_orphaned_connections_reclaimed"]
        followup = await asyncio.wait_for(self.send("/ok"), timeout=1)
        self.assertEqual(await followup.aread(), b"OK")
        await until(lambda: not self.pending)
        self.assertTrue(connection.is_closed())
        self.assertIs(self.client._transport._pool, pool)
        self.assertEqual(proxy.STATS["upstream_orphaned_connections_reclaimed"] - reclaimed_before, 1)
        await self.assert_available()
