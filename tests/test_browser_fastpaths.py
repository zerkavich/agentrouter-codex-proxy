"""Cancellation ownership while handing an upstream response to Starlette."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class DisconnectOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_owner_is_reused_until_response_handoff(self):
        entered, exited = asyncio.Event(), asyncio.Event()
        owners = []
        async def receive():
            owners.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        request = Request({'type': 'http', 'method': 'POST', 'headers': []}, receive)
        request._body = b'{}'
        async def complete():
            await entered.wait()
            return httpx.Response(200)
        with mock.patch.object(request, 'is_disconnected', side_effect=AssertionError('polling receive')):
            async with proxy._watch_client_disconnect(request):
                for _ in range(2):
                    self.assertEqual((await proxy._await_upstream_work(request, complete())).status_code, 200)
                    self.assertFalse(await proxy._client_disconnected(request))
                    self.assertFalse(exited.is_set())
        self.assertEqual(len(owners), 1)
        self.assertTrue(exited.is_set())
        self.assertFalse(hasattr(request, '_proxy_disconnect_listener'))

    async def test_cancelled_retry_wait_joins_its_receive_owner(self):
        entered, exited = asyncio.Event(), asyncio.Event()
        async def receive():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        request = Request({'type': 'http', 'method': 'POST', 'headers': []}, receive)
        request._body = b'{}'
        async def wait():
            async with proxy._watch_client_disconnect(request):
                await proxy._sleep_unless_disconnected(30, request)
        task = asyncio.create_task(wait())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())
        self.assertFalse(hasattr(request, '_proxy_disconnect_listener'))

    async def test_cached_body_receive_waiter_is_joined_before_response_handoff(self):
        first = True
        entered, exited = asyncio.Event(), asyncio.Event()
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': b'{}', 'more_body': False}
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        request = Request({'type': 'http', 'method': 'POST', 'headers': []}, receive)
        await request.body()
        response = httpx.Response(200)
        async def complete():
            await entered.wait()
            return response
        self.assertIs(await proxy._await_upstream_work(request, complete()), response)
        self.assertTrue(exited.is_set())

    async def test_cancellation_joins_the_receive_owner_and_upstream_operation(self):
        finished, received = asyncio.Event(), asyncio.Event()
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': b'{}', 'more_body': False}
            try:
                await asyncio.Event().wait()
            finally:
                received.set()
        request = Request({'type': 'http', 'method': 'POST', 'headers': []}, receive)
        await request.body()
        async def upstream():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        task = asyncio.create_task(proxy._await_upstream_work(request, upstream()))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertTrue(received.is_set())
