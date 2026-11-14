"""SSE history must preserve bytes and distinguish events from output text."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy
from telemetry import REQUEST_RECORD, RequestRecord


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        self.closed = True


class StreamHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, chunks, *, primed=(), remainder=b'', failure=None, tracked=True):
        stream = Chunks(chunks, failure)
        response = httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        record = RequestRecord('POST', '/v1/responses') if tracked else None
        context = REQUEST_RECORD.set(record)
        try:
            received = b''.join([chunk async for chunk in proxy._stream_generator(
                response, primed_chunks=primed, remainder=remainder)])
            if record is not None:
                record.status, record.complete = 200, True
            self.assertTrue(response.is_closed)
            self.assertTrue(stream.closed)
            return received, record
        finally:
            REQUEST_RECORD.reset(context)

    async def test_fragmented_completion_across_primed_remainder_and_live_chunks(self):
        for newline in (b'\n', b'\r\n', b'\r'):
            body = b'event: response.completed' + newline + b'data: {"type":"response.completed"}' + newline * 2
            with self.subTest(newline=newline):
                fragments = [body[index:index + 1] for index in range(len(body))]
                received, record = await self.deliver(fragments[11:], primed=fragments[:5], remainder=b''.join(fragments[5:11]))
                self.assertEqual(received, body)
                self.assertEqual(record.error, '')
                self.assertEqual(record.result()[6], 'success')

    async def test_late_fragmented_failure_is_recorded_even_after_completion(self):
        for event in (b'response.failed', b'error'):
            body = (b'event: response.completed\r\ndata: {}\r\n\r\n'
                    + b'event: ' + event + b'\r\ndata: {"error":{}}\r\n\r\n')
            with self.subTest(event=event):
                received, record = await self.deliver([body[index:index + 1] for index in range(len(body))])
                self.assertEqual(received, body)
                self.assertEqual(record.error, 'upstream_response_failed')
                self.assertEqual(record.result()[6], 'interrupted')

    async def test_clean_eof_without_terminal_event_is_not_inferred_to_be_an_error(self):
        for body in (b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n',
                     b'data: [DONE]\n\n', b'event: response.failed\ndata: {}'):
            with self.subTest(body=body):
                received, record = await self.deliver([body])
                self.assertEqual(received, body)
                self.assertEqual(record.error, '')

    async def test_failure_words_in_output_comments_or_overlong_event_are_not_events(self):
        body = (b': event: response.failed\n\n'
                b'event: response.output_text.delta\ndata: {"delta":"event: response.failed"}\n\n'
                b'event: response.failed\nevent: response.failed' + b'x' * 2000 + b'\ndata: {}\n\n'
                b'event: response.failed\n\n')
        received, record = await self.deliver([body[:37], body[37:91], body[91:]])
        self.assertEqual(received, body)
        self.assertEqual(record.error, '')

    async def test_explicit_incomplete_event_has_a_distinct_history_code(self):
        body = b'event: response.incomplete\ndata: {}\n\n'
        received, record = await self.deliver([body[:18], body[18:]])
        self.assertEqual(received, body)
        self.assertEqual(record.error, 'stream_incomplete')

    async def test_transport_error_records_failure_and_releases_the_stream(self):
        first = b'event: response.output_text.delta\ndata: {}\n\n'
        received, record = await self.deliver([first], failure=httpx.RemoteProtocolError('synthetic disconnect'))
        self.assertEqual(received, first)
        self.assertEqual(record.error, 'upstream_stream_error')

    async def test_disabled_history_does_not_construct_or_run_the_tracker(self):
        body = b'event: response.failed\ndata: {}\n\n'
        with mock.patch.object(proxy, '_SSEOutcomeTracker', side_effect=AssertionError('history fast path')):
            received, record = await self.deliver([body], tracked=False)
        self.assertEqual(received, body)
        self.assertIsNone(record)

    async def test_large_data_line_is_bounded_and_forwarded_verbatim(self):
        prefix = b'event: response.output_text.delta\ndata: '
        payload = b'x' * 2_000_000
        terminal = b'\n\nevent: response.failed\ndata: {}\n\n'
        tracker = proxy._SSEOutcomeTracker()
        tracker.feed(prefix)
        tracker.feed(payload)
        self.assertLessEqual(len(tracker.line), tracker.PREFIX_LIMIT)
        tracker.feed(terminal)
        self.assertEqual(tracker.error_code, 'upstream_response_failed')
        received, record = await self.deliver([payload, terminal], primed=[prefix])
        self.assertEqual(received, prefix + payload + terminal)
        self.assertEqual(record.error, 'upstream_response_failed')

    async def test_bom_and_split_crlf_follow_sse_field_rules(self):
        body = b'\xef\xbb\xbfevent: error\r\ndata\r\n\r\n'
        received, record = await self.deliver([body[:1], body[1:2], body[2:-1], body[-1:]])
        self.assertEqual(received, body)
        self.assertEqual(record.error, 'upstream_response_failed')


if __name__ == '__main__':
    unittest.main()
