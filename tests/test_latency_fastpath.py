"""Native compression, startup, and queued SSE lifecycle regressions."""

import asyncio
import gzip
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk, delay in self.chunks:
            if delay:
                await asyncio.sleep(delay)
            yield chunk


class LatencyFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_prepares_pool_without_sending_and_shutdown_closes_it(self):
        previous = proxy._SHARED_CLIENT
        sent = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request)))
        proxy._SHARED_CLIENT = None
        try:
            with mock.patch.object(proxy.httpx, "AsyncClient", return_value=client):
                async with proxy._app_lifespan(proxy.app):
                    self.assertIs(proxy._SHARED_CLIENT, client)
                    self.assertTrue((await proxy.health())["connection_pool_initialized"])
                    self.assertEqual(sent, [])
            self.assertTrue(client.is_closed)
        finally:
            await client.aclose()
            proxy._SHARED_CLIENT = previous

    async def test_startup_pool_is_closed_when_lifespan_task_is_cancelled(self):
        previous = proxy._SHARED_CLIENT
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
        proxy._SHARED_CLIENT = None
        entered = asyncio.Event()

        async def serve():
            async with proxy._app_lifespan(proxy.app):
                entered.set()
                await asyncio.Event().wait()

        try:
            with mock.patch.object(proxy.httpx, "AsyncClient", return_value=client):
                task = asyncio.create_task(serve())
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(client.is_closed)
        finally:
            await client.aclose()
            proxy._SHARED_CLIENT = previous

    async def test_many_coalesced_setup_frames_preserve_large_verbatim_tail(self):
        setup = b'event: response.in_progress\ndata: {"type":"response.in_progress"}\n\n' * 200
        first = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        tail = b'event: response.completed\ndata: {"type":"response.completed","text":"' + b"x" * 500000 + b'"}\n\n'
        body = setup + first + tail
        upstream = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream([(body, 0)]))
        with mock.patch.object(proxy, "_client_disconnected", return_value=False):
            prime = await proxy._prime_responses_sse(upstream, object(), first_frame_timeout_s=2, created_grace_s=1)
        self.assertEqual(prime.status, "commit")
        chunks = [chunk async for chunk in proxy._stream_generator(
            upstream, primed_chunks=prime.primed_chunks, remainder=prime.remainder,
            queue=prime.queue, producer_task=prime.producer_task, producer_exc=prime.producer_exc,
        )]
        self.assertEqual(b"".join(chunks), body)
        self.assertTrue(upstream.is_closed)
        self.assertTrue(prime.producer_task.done())

    async def test_queued_setup_and_delayed_failure_still_allow_recovery(self):
        setup = b'event: response.created\ndata: {"type":"response.created"}\n\n' * 20
        error = b'event: error\ndata: {"type":"error","error":{"code":"rate_limit_exceeded","message":"rate limit"}}\n\n'
        upstream = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream([(setup, 0), (error, 0.01)]))
        with mock.patch.object(proxy, "_client_disconnected", return_value=False):
            prime = await proxy._prime_responses_sse(upstream, object(), first_frame_timeout_s=2, created_grace_s=1)
        self.assertEqual(prime.status, "retry_rate_limit")
        self.assertTrue(upstream.is_closed)

    async def test_standard_gzip_fallback_is_reproducible_and_preserves_unicode(self):
        body = "Синтетическая проверка 🌍\n".encode() * 20000
        with mock.patch.object(proxy, "_native_gzip", None):
            first = proxy._compress_request_gzip(body)
            second = proxy._compress_request_gzip(body)
        self.assertEqual(first, second)
        self.assertEqual(gzip.decompress(first), body)


if __name__ == "__main__":
    unittest.main()
