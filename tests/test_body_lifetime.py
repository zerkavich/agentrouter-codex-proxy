"""Real TCP uploads release parsed and transmitted payloads during long SSE."""
from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import gzip
import json
from pathlib import Path
import sys
import tracemalloc
import unittest
import weakref
from unittest import mock

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class BodyLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers = []
        self.uploaded = asyncio.Event()
        self.send_headers = asyncio.Event()
        self.upload_hash = None
        self.uploads = []
        self.actions = []
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.client = httpx.AsyncClient(trust_env=False, timeout=5)
        self.previous_client = proxy._SHARED_CLIENT
        self.previous_stats = proxy.STATS.copy()
        proxy._SHARED_CLIENT = self.client
        self.settings = (
            mock.patch.object(proxy, "UPSTREAM", self.url),
            mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", False),
        )
        for setting in self.settings:
            setting.start()
        self.handlers = []
        self.responses = []
        # Load HTTPcore's async backend before allocation measurements begin.
        warm = await self.client.get(self.url + "/warmup")
        self.assertEqual(warm.status_code, 200)

    async def asyncTearDown(self):
        self.send_headers.set()
        for handler in self.handlers:
            if not handler.done():
                try:
                    response = await asyncio.wait_for(handler, timeout=5)
                    if isinstance(response, proxy.UpstreamStreamingResponse) and response not in self.responses:
                        self.responses.append(response)
                except asyncio.CancelledError:
                    pass
        for response in self.responses:
            await proxy._close_sse_producer(response.upstream_response, response.producer_task)
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        for setting in self.settings:
            setting.stop()
        proxy._SHARED_CLIENT = self.previous_client
        proxy.STATS.update(self.previous_stats)
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    async def serve(self, reader, writer):
        self.writers.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            if headers.startswith(b"GET /warmup "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
                return
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                chunk = await reader.readexactly(min(remaining, 65536))
                digest.update(chunk)
                remaining -= len(chunk)
            self.upload_hash = digest.hexdigest()
            sent_headers = dict(line.split(b":", 1) for line in headers.split(b"\r\n")[1:] if b":" in line)
            self.uploads.append({"sha256": self.upload_hash, "length": length,
                                 "headers": {name.lower(): value.strip() for name, value in sent_headers.items()}})
            action = self.actions.pop(0) if self.actions else "stream"
            if action == "reset":
                writer.transport.abort()
                return
            if action in ("mismatch", "sse_mismatch", "gzip_rejected"):
                message = ("Unsupported content encoding" if action == "gzip_rejected" else
                           "The requested item was created under a different OpenAI resource.")
                payload = json.dumps({"error": {"message": message}}).encode()
                if action == "sse_mismatch":
                    payload = b"event: error\ndata: " + payload + b"\n\n"
                    status, content_type = b"200 OK", b"text/event-stream"
                else:
                    status = b"415 Unsupported Media Type" if action == "gzip_rejected" else b"400 Bad Request"
                    content_type = b"application/json"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type
                             + b"\r\nContent-Length: " + str(len(payload)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + payload)
                await writer.drain()
                return
            self.uploaded.set()
            await self.send_headers.wait()
            first = b'event: response.output_text.delta\ndata: {"delta":"synthetic"}\n\n'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                + f"{len(first):x}\r\n".encode() + first + b"\r\n"
            )
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def request(self, body):
        unread = [body]
        async def receive():
            if unread:
                return {"type": "http.request", "body": unread.pop(), "more_body": False}
            await asyncio.Event().wait()
        return Request({"type": "http", "method": "POST", "path": "/v1/responses",
                        "headers": [(b"content-type", b"application/json"),
                                    (b"authorization", b"Bearer synthetic")], "query_string": b""}, receive)

    @staticmethod
    def image_body():
        return (b'{"model":"synthetic","stream":true,"input":[{"type":"message","role":"user",'
                b'"content":[{"type":"input_image","image_url":"data:image/png;base64,'
                + b"A" * (4 * 1024 * 1024) + b'"}]}]}')

    async def start(self, request):
        handler = asyncio.create_task(proxy.handle_responses(request))
        self.handlers.append(handler)
        await asyncio.wait_for(self.uploaded.wait(), timeout=5)
        return handler

    async def response(self, handler):
        self.send_headers.set()
        response = await asyncio.wait_for(handler, timeout=5)
        self.responses.append(response)
        self.assertIsInstance(response, proxy.UpstreamStreamingResponse)
        return response

    async def test_image_below_cap_does_not_decode_during_header_wait(self):
        body = self.image_body()
        expected_hash = hashlib.sha256(body).hexdigest()
        request = self.request(body)
        with mock.patch.object(proxy, "_decode_responses_payload", wraps=proxy._decode_responses_payload) as decode:
            handler = await self.start(request)
            self.assertEqual(decode.call_count, 0, "Below-cap images must not allocate a decoded payload")
            await self.response(handler)
        self.assertEqual(self.upload_hash, expected_hash)

    async def test_committed_stream_does_not_retain_large_uploaded_body(self):
        gc.collect()
        tracemalloc.start(1)
        body = self.image_body()
        expected_hash = hashlib.sha256(body).hexdigest()
        request = self.request(body)
        del body
        handler = await self.start(request)
        response = await self.response(handler)
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
        self.assertEqual(self.upload_hash, expected_hash)
        # A 4-MiB upload must not stay cached through request/response objects.
        # The generous threshold includes sockets, SSE buffers and test tasks.
        self.assertLess(retained, 2 * 1024 * 1024)
        self.assertFalse(response.upstream_response.is_closed)

    async def test_large_upload_is_replayed_after_transport_reset(self):
        body = self.image_body()
        expected_hash = hashlib.sha256(body).hexdigest()
        self.actions = ["reset"]
        handler = await self.start(self.request(body))
        await self.response(handler)
        self.assertEqual([upload["sha256"] for upload in self.uploads], [expected_hash, expected_hash])
        for upload in self.uploads:
            self.assertEqual(upload["length"], len(body))
            self.assertNotIn(b"transfer-encoding", upload["headers"])
            self.assertEqual(upload["headers"][b"authorization"], b"Bearer synthetic")
            self.assertEqual(upload["headers"][b"content-type"], b"application/json")

    async def test_stateless_fallback_releases_decoded_payload_before_headers(self):
        class Parsed(dict):
            pass

        body = self.image_body().replace(b'{"model"', b'{"previous_response_id":"resp_synthetic","model"')
        expected = proxy._encode_responses_payload(proxy.sanitize_responses_payload(json.loads(body)))
        expected_hash = hashlib.sha256(expected).hexdigest()
        del expected
        self.actions = ["mismatch"]
        references = []
        original_decode = proxy._decode_responses_payload

        def decode(content, **options):
            payload = Parsed(original_decode(content, **options))
            references.append(weakref.ref(payload))
            return payload

        with mock.patch.object(proxy, "_decode_responses_payload", side_effect=decode):
            handler = await self.start(self.request(body))
            gc.collect()
            self.assertEqual(len(references), 1, "Fallback must decode only once")
            self.assertTrue(all(reference() is None for reference in references),
                            "Sanitized upload must not retain the decoded source")
            await self.response(handler)
        self.assertEqual([upload["sha256"] for upload in self.uploads],
                         [hashlib.sha256(body).hexdigest(), expected_hash])

    async def test_sse_mismatch_replays_large_sanitized_body(self):
        body = self.image_body().replace(b'{"model"', b'{"previous_response_id":"resp_synthetic","model"')
        expected = proxy._encode_responses_payload(proxy.sanitize_responses_payload(json.loads(body)))
        self.actions = ["sse_mismatch"]
        handler = await self.start(self.request(body))
        await self.response(handler)
        self.assertEqual([upload["sha256"] for upload in self.uploads],
                         [hashlib.sha256(body).hexdigest(), hashlib.sha256(expected).hexdigest()])

    async def test_sanitized_body_replaces_original_before_headers(self):
        body = self.image_body().replace(b'{"model"', b'{"previous_response_id":"resp_synthetic","model"')
        expected = proxy._encode_responses_payload(proxy.sanitize_responses_payload(json.loads(body)))
        expected_hash, expected_size = hashlib.sha256(expected).hexdigest(), len(expected)
        del expected
        self.actions = ['mismatch']
        gc.collect()
        tracemalloc.start(1)
        request = self.request(body)
        del body
        handler = await self.start(request)
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
        self.assertEqual(self.upload_hash, expected_hash)
        self.assertEqual(getattr(request, '_body', None), b'')
        # Only the replayable sanitized body may remain before response headers.
        self.assertLess(retained, expected_size + 2 * 1024 * 1024)
        await self.response(handler)

    async def test_pruned_body_replaces_removed_images_before_headers(self):
        prefix = b'{"model":"synthetic","stream":true,"input":[{"type":"message","content":['
        image = b'{"type":"input_image","image_url":"data:image/png;base64,' + b'A' * (2 * 1024 * 1024) + b'"}'
        body = prefix + image + b',' + image + b']}]}'
        del image
        expected, count = proxy.prune_excess_images(json.loads(body), max_images=1)
        self.assertEqual(count, 1)
        encoded = proxy._encode_responses_payload(expected)
        expected_size, expected_hash = len(encoded), hashlib.sha256(encoded).hexdigest()
        del expected, encoded
        gc.collect()
        tracemalloc.start(1)
        request = self.request(body)
        del body
        with mock.patch.object(proxy, 'MAX_IMAGES_PER_REQUEST', 1):
            handler = await self.start(request)
            gc.collect()
            retained, _ = tracemalloc.get_traced_memory()
            self.assertEqual(self.upload_hash, expected_hash)
            self.assertEqual(getattr(request, '_body', None), b'')
            # Only current replay bytes remain; both parsed source trees and
            # removed images are gone before response headers arrive.
            self.assertLess(retained, expected_size + 2 * 1024 * 1024)
            await self.response(handler)

    async def test_gzip_rejection_replays_large_plain_body(self):
        body = self.image_body()
        expected_hash = hashlib.sha256(body).hexdigest()
        compressed_hash = hashlib.sha256(gzip.compress(body, compresslevel=1, mtime=0)).hexdigest()
        self.actions = ["gzip_rejected"]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(proxy, "_native_gzip", None):
            handler = await self.start(self.request(body))
            await self.response(handler)
        self.assertEqual([upload["sha256"] for upload in self.uploads], [compressed_hash, expected_hash])
        self.assertEqual([upload["headers"].get(b"content-encoding") for upload in self.uploads], [b"gzip", None])

    async def test_gzip_releases_original_before_headers_and_replays_after_reset(self):
        body = self.image_body()
        compressed_hash = hashlib.sha256(gzip.compress(body, compresslevel=1, mtime=0)).hexdigest()
        del body
        self.actions = ['reset']
        gc.collect()
        tracemalloc.start(1)
        request = self.request(self.image_body())
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, _native_gzip=None):
            handler = await self.start(request)
            gc.collect()
            retained, _ = tracemalloc.get_traced_memory()
            self.assertEqual(request._body, b'')
            self.assertLess(retained, 2 * 1024 * 1024)
            await self.response(handler)
        self.assertEqual([upload['sha256'] for upload in self.uploads], [compressed_hash, compressed_hash])
        self.assertTrue(all(upload['headers'][b'content-encoding'] == b'gzip' for upload in self.uploads))

    async def test_gzip_releases_original_while_waiting_for_admission(self):
        entered, released = asyncio.Event(), asyncio.Event()
        async def gate(*args, **kwargs):
            entered.set()
            await released.wait()
            return True
        gc.collect()
        tracemalloc.start(1)
        request = self.request(self.image_body())
        with mock.patch.object(proxy, 'REQUEST_GZIP_ENABLED', True), mock.patch.object(proxy, '_wait_for_rate_limit_gate', side_effect=gate):
            handler = asyncio.create_task(proxy.handle_responses(request))
            self.handlers.append(handler)
            try:
                await asyncio.wait_for(entered.wait(), 5)
                gc.collect()
                retained, _ = tracemalloc.get_traced_memory()
                self.assertEqual(request._body, b'')
                self.assertFalse(self.uploads)
                self.assertLess(retained, 2 * 1024 * 1024)
            finally:
                released.set()
            await asyncio.wait_for(self.uploaded.wait(), 5)
            await self.response(handler)

    async def test_actual_upload_trace_finishes_before_response_headers(self):
        body = self.image_body()
        traces, observations = [], []
        original_trace = proxy._UploadTrace
        original_record = proxy._record_upload_observation
        def make_trace(previous=None):
            trace = original_trace(previous)
            traces.append(trace)
            return trace
        def record(content, headers, elapsed, response, **timing):
            observations.append((len(content), elapsed, response.status_code))
            original_record(content, headers, elapsed, response, **timing)
        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0), mock.patch.object(
                proxy, '_gzip_if_worthwhile', side_effect=lambda content, **options: content), mock.patch.object(
                proxy, '_UploadTrace', side_effect=make_trace), mock.patch.object(
                proxy, '_record_upload_observation', side_effect=record):
            handler = await self.start(self.request(body))
            self.assertEqual(len(traces), 1)
            elapsed = traces[0].elapsed
            self.assertIsNotNone(elapsed)
            self.assertGreater(elapsed, 0)
            self.assertFalse(self.send_headers.is_set())
            self.assertFalse(observations)
            await asyncio.sleep(0.03)
            await self.response(handler)
        self.assertEqual(observations, [(len(body), elapsed, 200)])
        self.assertEqual(traces[0].elapsed, elapsed)
        self.assertIsNone(traces[0].previous)
        self.assertIsNone(traces[0].started)


if __name__ == "__main__":
    unittest.main()
