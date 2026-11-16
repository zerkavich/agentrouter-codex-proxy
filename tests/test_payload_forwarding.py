"""Wire-level regressions for the unchanged-body fast path and modified payloads."""

import asyncio
import base64
import copy
import gzip
import json
from pathlib import Path
import random
import sys
import threading
import unittest
from unittest import mock

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class PayloadForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_client = proxy._SHARED_CLIENT
        self.previous_stats = proxy.STATS.copy()
        self.gzip_setting = mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", False)
        self.gzip_setting.start()
        self.gzip_threshold = mock.patch.object(proxy, "REQUEST_GZIP_MIN_BYTES", 262144)
        self.gzip_threshold.start()
        self.requests = []
        self.encodings = []
        self.sent_headers = []
        self.upstream_responses = []
        self.closed_before_send = []
        self.responses = []
        proxy.reset_rate_limit_state()
        proxy.reset_compression_state()

        async def upstream(request):
            self.requests.append((request.content, int(request.headers["content-length"])))
            self.encodings.append(request.headers.get("content-encoding"))
            self.sent_headers.append(dict(request.headers))
            self.closed_before_send.append(self.upstream_responses[-1].is_closed if self.upstream_responses else None)
            item = self.responses.pop(0)
            if isinstance(item, httpx.Response):
                response = item
            else:
                status, body = item
                response = httpx.Response(status, json=body)
            self.upstream_responses.append(response)
            return response

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        proxy._SHARED_CLIENT = self.client

    async def asyncTearDown(self):
        await self.client.aclose()
        proxy._SHARED_CLIENT = self.previous_client
        self.gzip_threshold.stop()
        self.gzip_setting.stop()
        proxy.STATS.update(self.previous_stats)
        proxy.reset_rate_limit_state()
        proxy.reset_compression_state()

    async def forward(self, body, content_encoding=None, credential="synthetic"):
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        headers = [(b"content-type", b"application/json"),
                   (b"authorization", ("Bearer " + credential).encode()),
                   (b"content-length", str(len(body)).encode())]
        if content_encoding is not None:
            headers.append((b"content-encoding", content_encoding.encode()))
        request = Request({
            "type": "http", "method": "POST", "path": "/v1/responses",
            "headers": headers,
            "query_string": b"",
        }, receive)
        with mock.patch.object(proxy, "_sleep_unless_disconnected", return_value=True):
            response = await proxy.handle_responses(request)
        for sent, length in self.requests:
            self.assertEqual(length, len(sent))
        return response

    @staticmethod
    def completed():
        return 200, {"id": "synthetic", "status": "completed", "output": []}

    async def test_healthy_request_preserves_bytes_unicode_and_number_precision(self):
        body = ('{ "model": "synthetic", "stream": true, "input": "Привет 🌍", '
                '"metadata": {"decimal": 0.12345678901234567890123} }\n').encode()
        self.responses = [self.completed()]
        response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0][0], body)

    async def test_transient_retry_preserves_original_body(self):
        body = b'{ "model":"synthetic", "stream":true, "input":"retry" }'
        self.responses = [(503, {"error": {"message": "Temporary upstream failure"}}), self.completed()]
        response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([sent for sent, _ in self.requests], [body, body])

    async def test_escaped_and_uppercase_image_markers_still_enforce_image_cap(self):
        payload = {
            "model": "synthetic", "stream": True, "instructions": "Проверка",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "INPUT_IMAGE", "image_url": "image-0"},
                {"type": "computer_screenshot", "image_url": "image-1"},
                {"type": "input_image", "image_url": "image-2"},
                {"type": "image_url", "image_url": "image-3"},
            ]}],
        }
        original = copy.deepcopy(payload)
        with mock.patch.object(proxy, "MAX_IMAGES_PER_REQUEST", 2):
            pruned, count = proxy.prune_excess_images(payload, max_images=2)
            self.assertEqual(count, 2)
            self.assertEqual(payload, original)
            body = json.dumps(payload, ensure_ascii=False).encode()
            body = body.replace(b"INPUT_IMAGE", b"INPUT_\\u0049MAGE").replace(b'"image_url"', b'"\\u0069mage_url"')
            self.responses = [self.completed()]
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        sent = self.requests[0][0]
        self.assertIn("Проверка".encode(), sent)
        self.assertEqual(json.loads(sent), pruned)
        remaining = [part["image_url"] for part in json.loads(sent)["input"][0]["content"] if "image_url" in part]
        self.assertEqual(remaining, ["image-2", "image-3"])

    async def test_lazy_fallback_sanitizes_state_and_reuses_modified_body(self):
        payload = {
            "model": "synthetic", "stream": True, "previous_response_id": "resp_synthetic",
            "input": [
                {"type": "reasoning", "encrypted_content": "synthetic"},
                {"type": "function_call", "id": "server-item", "call_id": "call-synthetic", "name": "test", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call-synthetic", "output": "Готово"},
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.responses = [
            (400, {"error": {"message": "The requested item was created under a different OpenAI resource."}}),
            (503, {"error": {"message": "Temporary upstream failure"}}),
            self.completed(),
        ]
        response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        first, second, third = [sent for sent, _ in self.requests]
        self.assertEqual(first, body)
        self.assertEqual(second, third)
        clean = json.loads(second)
        self.assertNotIn("previous_response_id", clean)
        self.assertEqual([item["type"] for item in clean["input"]], ["function_call", "function_call_output"])
        self.assertEqual([item["call_id"] for item in clean["input"]], ["call-synthetic", "call-synthetic"])
        self.assertNotIn("id", clean["input"][0])
        self.assertIn("Готово".encode(), second)

    async def test_marker_scan_enforces_cap_for_top_level_and_output_images(self):
        payload = {"model": "synthetic", "input": [
            {"type": "INPUT_IMAGE", "file_id": "oldest"},
            {"type": "computer_screenshot", "data": "second"},
            {"type": "function_call_output", "output": [
                {"type": "image_url", "url": "third"},
                {"image_url": "newest"},
            ]},
        ]}
        body = json.dumps(payload, ensure_ascii=False).encode()
        for cap in (0, 1, 3):
            with self.subTest(cap=cap), mock.patch.object(proxy, "MAX_IMAGES_PER_REQUEST", cap):
                expected, count = proxy.prune_excess_images(payload, max_images=cap)
                self.assertEqual(count, 4 - cap)
                self.responses = [self.completed()]
                response = await self.forward(body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(self.requests[-1][0]), expected)

    async def test_marker_scan_skips_below_cap_urls_and_preserves_original_bytes(self):
        payload = {"input": [{"type": "message", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,A"},
            {"type": "image_url", "image_url": "data:image/png;base64,B"},
            {"type": "computer_screenshot", "image_url": "data:image/png;base64,C"},
        ]}]}
        body = json.dumps(payload).encode().replace(b"base64,A", b"base64,\\u0041")
        self.responses = [self.completed()]
        with mock.patch.object(proxy, "MAX_IMAGES_PER_REQUEST", 3), mock.patch.object(
                proxy, "_decode_responses_payload", wraps=proxy._decode_responses_payload) as decode:
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode.call_count, 0)
        self.assertEqual(self.requests[0][0], body)

    async def test_escaped_type_after_skipped_url_still_enforces_cap(self):
        body = (b'{"input":[{"type":"message","content":['
                b'{"type":"input_image","image_url":"data:image/png;base64,\\u0041"},'
                b'{"type":"input_\\u0069mage","file_id":"newest"}]}]}')
        expected, count = proxy.prune_excess_images(json.loads(body), max_images=1)
        self.assertEqual(count, 1)
        self.responses = [self.completed()]
        with mock.patch.object(proxy, "MAX_IMAGES_PER_REQUEST", 1):
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.requests[0][0]), expected)

    async def test_gzip_preserves_bytes_and_is_prepared_once_across_retries(self):
        body = ('{ "model":"synthetic", "input":' + json.dumps("Привет 🌍 " * 40000, ensure_ascii=False)
                + ', "metadata":{"decimal":0.12345678901234567890123} }').encode()
        self.responses = [(503, {"error": {"message": "Temporary upstream failure"}}), self.completed()]
        before = proxy.STATS["gzip_payloads"]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", wraps=proxy._gzip_if_worthwhile,
        ) as compress, mock.patch.object(proxy, '_decompress_proxy_gzip', wraps=proxy._decompress_proxy_gzip) as inflate:
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(inflate.call_count, 0, 'Normal retries must not inflate proxy gzip')
        self.assertEqual(self.encodings, ["gzip", "gzip"])
        self.assertEqual(self.requests[0][0], self.requests[1][0])
        self.assertEqual(gzip.decompress(self.requests[0][0]), body)
        self.assertLess(len(self.requests[0][0]), len(body) * 0.8)
        self.assertEqual(proxy.STATS["gzip_payloads"] - before, 1)

    @staticmethod
    def large_body():
        return ('{ "model":"synthetic", "input":' + json.dumps("Привет 🌍 " * 40000, ensure_ascii=False)
                + ', "metadata":{"decimal":0.12345678901234567890123} }\n').encode()

    @staticmethod
    def unsupported_encoding():
        return httpx.Response(
            415, headers={"content-type": "application/json"},
            stream=httpx.ByteStream(b'{"error":{"message":"Unsupported content encoding"}}'),
        )

    async def test_gzip_415_retries_original_bytes_and_headers_once(self):
        body = self.large_body()
        rejected = self.unsupported_encoding()
        self.assertFalse(rejected.is_closed)
        self.responses = [rejected, self.completed()]
        before = proxy.STATS.copy()
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", wraps=proxy._gzip_if_worthwhile,
        ) as compress:
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.encodings, ["gzip", None])
        self.assertEqual(gzip.decompress(self.requests[0][0]), body)
        self.assertEqual(self.requests[1][0], body)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(self.closed_before_send, [None, True])
        for headers in self.sent_headers:
            self.assertEqual(headers["authorization"], "Bearer synthetic")
            self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(proxy.STATS["gzip_fallbacks"] - before["gzip_fallbacks"], 1)
        self.assertEqual(proxy.STATS["successful_recoveries"] - before["successful_recoveries"], 1)
        self.assertEqual(proxy.STATS["transport_retries"], before["transport_retries"])

    async def test_gzip_415_preserves_sse_bytes_and_closes_upstream(self):
        body = self.large_body()
        sse = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        )
        self.responses = [self.unsupported_encoding(), httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(sse),
        )]
        before = proxy.STATS["successful_recoveries"]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
            response = await self.forward(body)
            delivered = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(delivered, sse)
        self.assertEqual(self.encodings, ["gzip", None])
        self.assertEqual(self.requests[1][0], body)
        self.assertTrue(all(upstream.is_closed for upstream in self.upstream_responses))
        self.assertEqual(proxy.STATS["successful_recoveries"] - before, 1)

    async def test_plain_and_client_encoded_415_are_forwarded_without_retry(self):
        for encoding, body in ((None, b'{"input":"small"}'), ("gzip", gzip.compress(self.large_body(), mtime=0))):
            with self.subTest(encoding=encoding), mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
                offset = len(self.requests)
                before = proxy.STATS["gzip_fallbacks"]
                self.responses = [self.unsupported_encoding()]
                response = await self.forward(body, content_encoding=encoding)
                self.assertEqual(response.status_code, 415)
                self.assertEqual(len(self.requests) - offset, 1)
                self.assertEqual(self.requests[-1][0], body)
                self.assertEqual(self.encodings[-1], encoding)
                self.assertEqual(proxy.STATS["gzip_fallbacks"], before)
                self.assertTrue(self.upstream_responses[-1].is_closed)

    async def test_second_415_is_forwarded_without_another_retry(self):
        body = self.large_body()
        self.responses = [self.unsupported_encoding(), self.unsupported_encoding()]
        before = proxy.STATS.copy()
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
            response = await self.forward(body)
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.encodings, ["gzip", None])
        self.assertEqual(self.requests[1][0], body)
        self.assertEqual(proxy.STATS["gzip_fallbacks"] - before["gzip_fallbacks"], 1)
        self.assertEqual(proxy.STATS["successful_recoveries"], before["successful_recoveries"])

    async def test_gzip_415_cache_does_not_disable_other_credentials(self):
        body = self.large_body()
        self.responses = [self.unsupported_encoding(), self.completed(), self.completed()]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
            first = await self.forward(body)
            second = await self.forward(body, credential="another-synthetic-key")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(self.encodings, ["gzip", None, "gzip"])
        self.assertEqual(gzip.decompress(self.requests[2][0]), body)

    async def test_repeated_415_uses_plain_next_request_without_compressing_or_rejecting_again(self):
        body = self.large_body()
        self.responses = [self.unsupported_encoding(), self.completed(), self.completed()]
        before = proxy.STATS["gzip_cached_rejection_skips"]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", wraps=proxy._gzip_if_worthwhile,
        ) as compress:
            self.assertEqual((await self.forward(body)).status_code, 200)
            self.assertEqual((await self.forward(body)).status_code, 200)
        self.assertEqual(self.encodings, ["gzip", None, None])
        self.assertEqual(self.requests[1][0], body)
        self.assertEqual(self.requests[2][0], body)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(proxy.STATS["gzip_cached_rejection_skips"], before + 1)

    async def test_gzip_415_preserves_plain_stateless_recovery_in_both_orders(self):
        payload = {
            "model": "synthetic", "previous_response_id": "resp_synthetic",
            "input": [
                {"type": "reasoning", "encrypted_content": "encrypted synthetic " * 1000},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Привет " * 60000}]},
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        mismatch = (400, {"error": {"message": "The requested item was created under a different OpenAI resource."}})
        for rejection_first in (True, False):
            with self.subTest(rejection_first=rejection_first), mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
                proxy.reset_compression_state()
                offset = len(self.requests)
                self.responses = (
                    [self.unsupported_encoding(), mismatch, (503, {"error": {"message": "Temporary upstream failure"}}), self.completed()]
                    if rejection_first else [mismatch, self.unsupported_encoding(), self.completed()]
                )
                response = await self.forward(body)
                sent = [content for content, _ in self.requests[offset:]]
                encodings = self.encodings[offset:]
                self.assertEqual(response.status_code, 200)
                if rejection_first:
                    self.assertEqual(encodings, ["gzip", None, None, None])
                    self.assertEqual(sent[1], body)
                    self.assertEqual(sent[2], sent[3])
                else:
                    self.assertEqual(encodings, ["gzip", "gzip", None])
                    self.assertEqual(gzip.decompress(sent[1]), sent[2])
                clean = json.loads(sent[-1])
                self.assertNotIn("previous_response_id", clean)
                self.assertEqual([item["type"] for item in clean["input"]], ["message"])
                self.assertEqual(clean["input"][0]["content"][0]["text"], payload["input"][1]["content"][0]["text"])

    async def test_disconnect_after_415_starts_no_retry(self):
        body = self.large_body()
        self.responses = [self.unsupported_encoding(), self.completed()]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_client_disconnected", side_effect=[False, False, True],
        ):
            response = await self.forward(body)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(self.encodings, ["gzip"])
        self.assertEqual(len(self.responses), 1)
        self.assertTrue(self.upstream_responses[0].is_closed)

    async def test_fallback_rebuilds_gzip_and_removes_header_for_small_body(self):
        for text in ("small", "keep this history " * 25000):
            with self.subTest(large_retained_body=len(text) > 262144):
                payload = {
                    "model": "synthetic", "previous_response_id": "resp_synthetic",
                    "input": [
                        {"type": "reasoning", "encrypted_content": "encrypted " * 40000},
                        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
                    ],
                }
                body = json.dumps(payload).encode()
                offset = len(self.requests)
                self.responses = [
                    (400, {"error": {"message": "The requested item was created under a different OpenAI resource."}}),
                    (503, {"error": {"message": "Temporary upstream failure"}}), self.completed(),
                ]
                with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True):
                    response = await self.forward(body)
                self.assertEqual(response.status_code, 200)
                first, second, third = [sent for sent, _ in self.requests[offset:]]
                self.assertEqual(gzip.decompress(first), body)
                self.assertEqual(second, third)
                encodings = self.encodings[offset:]
                self.assertEqual(encodings, ["gzip"] + (["gzip"] if len(text) > 262144 else [None]) * 2)
                clean = json.loads(gzip.decompress(second) if encodings[1] else second)
                self.assertNotIn("previous_response_id", clean)
                self.assertEqual(len(clean["input"]), 1)
                self.assertEqual(clean["input"][0]["content"][0]["text"], text)

    async def test_high_entropy_and_already_encoded_bodies_bypass_compression(self):
        image = base64.b64encode(random.Random(17).randbytes(450000)).decode()
        body = json.dumps({"model": "synthetic", "input": [{
            "type": "message", "role": "user", "content": [{"type": "input_image", "image_url": image}],
        }]}).encode()
        encoded = gzip.compress(body, compresslevel=1, mtime=0)
        self.assertGreater(len(encoded), proxy.REQUEST_GZIP_MIN_BYTES)
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", wraps=proxy._gzip_if_worthwhile,
        ) as compress:
            self.responses = [self.completed()]
            self.assertEqual((await self.forward(body)).status_code, 200)
            self.responses = [self.completed()]
            self.assertEqual((await self.forward(encoded, content_encoding="gzip")).status_code, 200)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual([sent for sent, _ in self.requests], [body, encoded])
        self.assertEqual(self.encodings, [None, "gzip"])

    async def test_compression_failure_keeps_original_body_and_headers(self):
        body = json.dumps({"input": "synthetic " * 40000}).encode()
        self.responses = [self.completed()]
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", side_effect=RuntimeError("synthetic compression failure"),
        ):
            response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0][0], body)
        self.assertEqual(self.encodings, [None])

    async def test_cancellation_during_compression_starts_no_upstream_request(self):
        body = json.dumps({"input": "synthetic " * 40000}).encode()
        started = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def delayed_compression(content):
            loop.call_soon_threadsafe(started.set)
            try:
                release.wait(timeout=5)
                return gzip.compress(content, compresslevel=1, mtime=0)
            finally:
                loop.call_soon_threadsafe(finished.set)

        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_gzip_if_worthwhile", side_effect=delayed_compression,
        ):
            task = asyncio.create_task(self.forward(body))
            try:
                await asyncio.wait_for(started.wait(), timeout=3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.requests, [])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if started.is_set():
                    await asyncio.wait_for(finished.wait(), timeout=3)

    async def test_disconnect_during_preparation_starts_no_upstream_request(self):
        body = json.dumps({"input": "synthetic " * 40000}).encode()
        with mock.patch.object(proxy, "REQUEST_GZIP_ENABLED", True), mock.patch.object(
            proxy, "_client_disconnected", side_effect=[False, True],
        ):
            response = await self.forward(body)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(self.requests, [])

    async def test_modified_body_preserves_escaped_lone_surrogate(self):
        payload = {"model": "synthetic", "input": "\ud800", "previous_response_id": "resp_synthetic"}
        body = json.dumps(payload).encode()
        self.responses = [
            (400, {"error": {"message": "The requested item was created under a different OpenAI resource."}}),
            self.completed(),
        ]
        response = await self.forward(body)
        self.assertEqual(response.status_code, 200)
        clean = json.loads(self.requests[-1][0])
        self.assertEqual(clean["input"], "\ud800")
        self.assertNotIn("previous_response_id", clean)


class ImageMarkerScanTests(unittest.TestCase):
    def test_shortcut_never_skips_required_pruning_for_mixed_json(self):
        rng = random.Random(16112026)
        parts = [
            {"type": "input_image", "image_url": "data:image/png;base64,A"},
            {"type": "computer_screenshot", "file_id": "screenshot"},
            {"type": "IMAGE_URL", "url": "image"},
            {"image_url": 'data:image/png;base64,A\\u0041\"input_image\",\"image_url\":\"B'},
            {"type": "input_text", "text": '\"input_image\",\"image_url\":\"日本語\\u0069mage_url'},
            {"type": "message", "image_url": {"url": "image"}},
            {"type": "input_text", "text": "plain text"},
        ]
        for index in range(600):
            cap = rng.randrange(0, 8)
            items = [{"type": rng.choice(("message", "function_call_output")),
                      rng.choice(("content", "output")): [rng.choice(parts) for _ in range(rng.randrange(0, 12))]}
                     for _ in range(rng.randrange(0, 5))]
            if rng.randrange(2):
                items.insert(0, {"type": "INPUT_IMAGE", "image_url": "image"})
            payload = {"input": items, "metadata": {"image_url": "unrelated"}}
            body = json.dumps(payload, ensure_ascii=bool(rng.randrange(2))).encode()
            if rng.randrange(2):
                body = body.replace(b'"image_url"', b'"\\u0069mage_url"')
            if rng.randrange(2):
                body = body.replace(b"input_image", b"input_\\u0069mage")
            parsed = json.loads(body)
            _, pruned = proxy.prune_excess_images(parsed, max_images=cap)
            if pruned:
                self.assertTrue(proxy._payload_needs_image_pruning(body, cap),
                                f"Required pruning was skipped in fixture {index}, cap {cap}")


if __name__ == "__main__":
    unittest.main()
