"""Compression hints use successful send-body events, isolated per attempt."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy
from telemetry import RuntimeTimings


class UploadTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http11_and_http2_ignore_connect_and_header_waits(self):
        for protocol in ('http11', 'http2'):
            events = []
            trace = proxy._UploadTrace(lambda event, info: events.append(event))
            await trace('connection.connect_tcp.started', {'headers': {'authorization': 'synthetic-secret'}})
            with mock.patch.object(proxy.time, 'perf_counter', side_effect=(10.0, 10.125, 310.125)):
                await trace(protocol + '.send_request_body.started', {})
                await trace(protocol + '.send_request_body.complete', {})
                await trace(protocol + '.receive_response_headers.started', {})
                await trace(protocol + '.receive_response_headers.complete', {})
            self.assertEqual(trace.elapsed, 0.125)
            self.assertEqual(trace.header_wait, 300)
            self.assertEqual(len(events), 5)
            trace.close()
            self.assertIsNone(trace.previous)
            self.assertIsNone(trace.started)
            self.assertIsNone(trace.protocol)
            self.assertFalse(hasattr(trace, '__dict__'))

    async def test_failed_body_and_protocol_mismatch_do_not_produce_samples(self):
        trace = proxy._UploadTrace()
        await trace('http11.send_request_body.started', {})
        await trace('http2.send_request_body.complete', {})
        self.assertIsNone(trace.elapsed)
        await trace('http11.send_request_body.failed', {})
        self.assertIsNone(trace.elapsed)
        self.assertIsNone(trace.started)
        await trace('http11.receive_response_headers.complete', {})
        self.assertIsNone(trace.elapsed)

    async def test_async_previous_hook_is_awaited(self):
        events = []
        async def previous(event, info):
            await asyncio.sleep(0)
            events.append(event)
        trace = proxy._UploadTrace(previous)
        await trace('http2.send_request_body.started', {})
        await trace('http2.send_request_body.complete', {})
        self.assertEqual(len(events), 2)
        trace.close()
        self.assertIsNone(trace.previous)

    async def test_http2_body_and_header_events_reach_opt_in_diagnostics(self):
        timings = RuntimeTimings()
        diagnostic_trace = timings.upstream_trace()
        trace = proxy._UploadTrace(diagnostic_trace)
        for stage in ('send_request_body', 'receive_response_headers'):
            await trace('http2.' + stage + '.started', {})
            await trace('http2.' + stage + '.complete', {})
        trace.close()
        diagnostic_trace.close()
        snapshot = timings.snapshot()
        self.assertEqual(snapshot['upstream_upload']['count'], 1)
        self.assertEqual(snapshot['upstream_headers']['count'], 1)

    async def test_cancellation_closes_accepted_trace_and_frees_capacity(self):
        accepted = asyncio.Event()
        traces = []
        async def upstream(request):
            trace = request.extensions['trace']
            traces.append(trace)
            await trace('http11.send_request_body.started', {})
            accepted.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
                task = asyncio.create_task(proxy._send_with_transport_retry(
                    client, method='POST', url='http://synthetic/v1/responses',
                    headers={}, content=b'A' * (2**20), stream=True))
                try:
                    await asyncio.wait_for(accepted.wait(), 2)
                finally:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(proxy._get_upstream_capacity(client).in_use, 0)
        self.assertEqual(len(traces), 1)
        self.assertIsNone(traces[0].previous)
        self.assertIsNone(traces[0].started)
        self.assertIsNone(traces[0].elapsed)

    async def test_mock_response_metadata_without_body_events_cannot_train(self):
        proxy.reset_compression_state()
        headers = {'authorization': 'Bearer synthetic'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={'output': 'synthetic'}, extensions={'network_stream': object()}))) as client:
            with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0):
                await proxy._send_with_transport_retry(client, 'POST', 'http://synthetic/v1/responses',
                                                       headers, b'A' * (2**20))
                self.assertEqual(proxy._upload_estimate(headers), 0)
                self.assertFalse(proxy._UPLOAD_OBSERVATIONS)

    async def test_small_buffered_uploads_do_not_install_adaptive_traces(self):
        async def upstream(request):
            self.assertNotIn('trace', request.extensions)
            return httpx.Response(200, extensions={'network_stream': object()})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=True, REQUEST_GZIP_UPLOAD_MBIT_S=0), mock.patch.object(
                    proxy, 'TIMINGS', RuntimeTimings(enabled=False)), mock.patch.object(proxy, '_UploadTrace') as trace:
                await proxy._send_with_transport_retry(client, 'POST', 'http://synthetic/v1/responses',
                                                       {}, b'A' * (256 * 1024))
                trace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
