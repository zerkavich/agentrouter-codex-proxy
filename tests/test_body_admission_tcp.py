"""Real inbound/outbound TCP controls for large-body admission and cancellation."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

import httpx
import uvicorn
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class IncomingAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = asyncio.Event()
        self.arrived = asyncio.Event()
        self.uploads = {}
        self.handlers = set()
        self.budget = proxy._BodyBudget(3 * 2**20)

        async def upstream(reader, writer):
            self.handlers.add(asyncio.current_task())
            try:
                raw = await reader.readuntil(b'\r\n\r\n')
                fields = {name.lower(): value.strip() for name, value in
                          (line.split(b':', 1) for line in raw.split(b'\r\n')[1:] if b':' in line)}
                remaining = int(fields[b'content-length'])
                digest = hashlib.sha256()
                while remaining:
                    chunk = await reader.readexactly(min(65536, remaining))
                    digest.update(chunk)
                    remaining -= len(chunk)
                name = fields[b'x-body-test'].decode('ascii')
                self.uploads[name] = digest.hexdigest()
                if name == 'first':
                    self.arrived.set()
                    await self.release.wait()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                self.handlers.discard(asyncio.current_task())

        self.upstream = await asyncio.start_server(upstream, '127.0.0.1', 0)
        url = 'http://127.0.0.1:' + str(self.upstream.sockets[0].getsockname()[1])
        self.previous_client = proxy._SHARED_CLIENT
        proxy._SHARED_CLIENT = httpx.AsyncClient(trust_env=False, timeout=5)
        self.settings = mock.patch.multiple(proxy, UPSTREAM=url, REQUEST_GZIP_ENABLED=False,
                                           BODY_BUDGET_BYTES=self.budget.limit)
        self.settings.start()
        self.budget_patch = mock.patch.object(proxy, '_body_budget', return_value=self.budget)
        self.budget_patch.start()
        proxy.reset_rate_limit_state()
        app = FastAPI()
        app.post('/v1/responses')(proxy.handle_responses)
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.setblocking(False)
        self.listener.listen(128)
        port = self.listener.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            http='httptools', ws='none', lifespan='off', log_level='critical', access_log=False,
            timeout_graceful_shutdown=1))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        while not self.server.started:
            if self.server_task.done():
                await self.server_task
            await asyncio.sleep(.001)
        self.client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5)
        self.tasks = []

    async def asyncTearDown(self):
        self.release.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 5)
        await proxy._SHARED_CLIENT.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.upstream.close()
        await self.upstream.wait_closed()
        for handler in list(self.handlers):
            handler.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)
        self.budget_patch.stop()
        self.settings.stop()
        proxy.reset_rate_limit_state()
        self.assertEqual(self.budget.used, 0)
        self.assertFalse(self.budget.queue)

    async def send(self, name, large=True):
        body = b'{"input":"' + b'A' * (2**20 if large else 10) + b'"}'
        async def chunks():
            for offset in range(0, len(body), 65536):
                yield body[offset:offset + 65536]
                await asyncio.sleep(0)
        response = await self.client.post('/v1/responses', content=chunks(), headers={
            'content-length': str(len(body)), 'content-type': 'application/json',
            'x-body-test': name, 'authorization': 'Bearer synthetic'})
        if response.status_code == 200:
            self.assertEqual(self.uploads[name], hashlib.sha256(body).hexdigest())
        return response

    async def queued(self):
        async def wait():
            while not self.budget.queue:
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 2)

    async def test_large_waiter_does_not_block_a_small_request(self):
        first = asyncio.create_task(self.send('first'))
        self.tasks.append(first)
        await asyncio.wait_for(self.arrived.wait(), 2)
        second = asyncio.create_task(self.send('second'))
        self.tasks.append(second)
        await self.queued()
        self.assertNotIn('second', self.uploads)
        self.assertEqual((await asyncio.wait_for(self.send('small', False), 2)).status_code, 200)
        self.assertFalse(first.done())
        self.release.set()
        self.assertEqual([result.status_code for result in await asyncio.wait_for(asyncio.gather(first, second), 3)], [200, 200])
        self.assertEqual(self.budget.used, 0)
        self.assertFalse(self.budget.queue)

    async def test_cancelled_upstream_wait_releases_admission_to_next_upload(self):
        first = asyncio.create_task(self.send('first'))
        self.tasks.append(first)
        await asyncio.wait_for(self.arrived.wait(), 2)
        second = asyncio.create_task(self.send('second'))
        self.tasks.append(second)
        await self.queued()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await asyncio.wait_for(second, 3)).status_code, 200)
        self.assertEqual(self.budget.used, 0)
        self.assertFalse(self.budget.queue)

    async def test_wait_timeout_is_retryable_and_does_not_forward_queued_body(self):
        first = asyncio.create_task(self.send('first'))
        self.tasks.append(first)
        await asyncio.wait_for(self.arrived.wait(), 2)
        with mock.patch.object(proxy, 'BODY_BUDGET_WAIT_S', .05):
            busy = await asyncio.wait_for(self.send('busy'), 2)
        self.assertEqual(busy.status_code, 503)
        self.assertEqual(busy.headers['retry-after'], '2')
        self.assertEqual(busy.json()['error']['type'], 'proxy_memory_busy')
        self.assertNotIn('busy', self.uploads)
        self.assertFalse(self.budget.queue)
        self.release.set()
        self.assertEqual((await first).status_code, 200)


if __name__ == '__main__':
    unittest.main()
