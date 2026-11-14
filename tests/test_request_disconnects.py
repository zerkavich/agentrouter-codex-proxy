"""Disconnects before response headers and while consuming JSON bodies."""
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


class RequestDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers = []
        self.arrived = asyncio.Event()
        self.released = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(3, pool=.08),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0), trust_env=False)
        self.previous_client = proxy._SHARED_CLIENT
        proxy._SHARED_CLIENT = self.client
        proxy.reset_rate_limit_state()
        self.patch = mock.patch.multiple(proxy, UPSTREAM=self.url, REQUEST_GZIP_ENABLED=False)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.client.aclose()
        proxy._SHARED_CLIENT = self.previous_client
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
            if headers.startswith(b"GET /ok "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
                return
            length = next((int(line.split(b":", 1)[1]) for line in headers.splitlines()
                           if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            if b"x-disconnect-test-mode: json" in headers.lower():
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                await writer.drain()
            elif b"x-disconnect-test-mode: error" in headers.lower():
                writer.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                await writer.drain()
            self.arrived.set()
            await reader.read()
            self.released.set()
        except (ConnectionError, asyncio.IncompleteReadError):
            self.released.set()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def request(self, mode="headers", method="POST"):
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b'{"input":"synthetic","stream":true}', "more_body": False}
            await self.disconnected.wait()
            return {"type": "http.disconnect"}

        return Request({"type":"http", "method":method, "path":"/v1/responses", "query_string":b"",
            "headers":[(b"content-type",b"application/json"), (b"authorization",b"Bearer synthetic-disconnect"),
                       (b"x-disconnect-test-mode",mode.encode())]}, receive)

    async def pool_available(self):
        self.assertEqual(len(self.client._transport._pool._requests), 0)
        response = await self.client.get(self.url + "/ok")
        self.assertEqual(response.text, "OK")
        self.assertTrue(all(state["active"] == 0 for state in proxy._RATE_LIMIT_STATES.values()))

    async def disconnected_request(self, mode, *, passthrough=False):
        self.arrived.clear()
        self.released.clear()
        self.disconnected.clear()
        request = self.request(mode, "GET" if passthrough else "POST")
        operation = proxy.passthrough_handler(request, "v1/models") if passthrough else proxy.handle_responses(request)
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(self.arrived.wait(), timeout=2)
            self.disconnected.set()
            response = await asyncio.wait_for(task, timeout=1.5)
            self.assertEqual(response.status_code, 499)
            await asyncio.wait_for(self.released.wait(), timeout=1)
            await self.pool_available()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_disconnect_before_headers_releases_pool_without_cancelling_handler(self):
        await self.disconnected_request("headers")

    async def test_ten_cancelled_header_waits_reuse_one_slot(self):
        for _ in range(10):
            await self.disconnected_request("headers")

    async def test_disconnect_during_json_body_releases_pool(self):
        await self.disconnected_request("json")

    async def test_disconnect_during_error_body_releases_pool(self):
        await self.disconnected_request("error")

    async def test_models_passthrough_disconnect_releases_pool(self):
        await self.disconnected_request("headers", passthrough=True)

    async def test_handler_cancellation_before_headers_releases_pending_send(self):
        task = asyncio.create_task(proxy.handle_responses(self.request()))
        await asyncio.wait_for(self.arrived.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.pool_available()

    async def test_upload_disconnect_does_not_start_upstream_work(self):
        messages = iter([{"type":"http.request", "body":b'{"input":', "more_body":True},
                         {"type":"http.disconnect"}])
        async def receive():
            return next(messages)
        request = Request({"type":"http", "method":"POST", "path":"/v1/responses", "query_string":b"", "headers":[]}, receive)
        with mock.patch.object(proxy, "get_shared_client", side_effect=AssertionError("Disconnected upload contacted upstream")):
            result = await proxy.handle_responses(request)
        self.assertEqual(result.status_code, 499)
        self.assertFalse(self.arrived.is_set())
        await self.pool_available()

    async def test_response_completed_during_disconnect_check_is_closed(self):
        release = asyncio.Event()
        response = httpx.Response(200, stream=httpx.ByteStream(b"synthetic"))
        async def operation():
            await release.wait()
            return response
        async def finish_during_check(_request):
            release.set()
            await asyncio.sleep(0)
            return True
        with mock.patch.object(proxy, "_client_disconnected", side_effect=finish_during_check):
            result = await proxy._await_upstream_work(self.request(), operation())
        self.assertIs(result, proxy._CLIENT_DISCONNECTED)
        self.assertTrue(response.is_closed)
