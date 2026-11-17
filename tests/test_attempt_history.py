"""Retry metadata remains bounded, private, restart-safe and rollback-readable."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

import httpx
from starlette.requests import Request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy
from telemetry import HistoryStore, RequestRecord, REQUEST_RECORD, note_attempt


class AttemptHistoryTests(unittest.TestCase):
    def test_allowlist_and_bound_keep_no_raw_errors_or_credentials(self):
        record = RequestRecord('POST', '/v1/responses')
        token = REQUEST_RECORD.set(record)
        try:
            note_attempt('SYNTHETIC_SECRET')
            for index in range(100):
                note_attempt('upstream', status=200, duration_s=index / 10)
            note_attempt('retry_wait', wait_s=float('inf'))
            detail = json.loads(record.attempt_detail())
            self.assertEqual(len(detail['events']), 16)
            self.assertEqual(detail['total'], 101)
            self.assertNotIn('SYNTHETIC', record.attempt_detail())
            self.assertEqual(detail['events'][-1], ['retry_wait', 0, 0, 0])
            self.assertLess(len(record.attempt_detail()), 4096)
        finally:
            REQUEST_RECORD.reset(token)

    def test_disabled_history_does_not_allocate_an_event(self):
        token = REQUEST_RECORD.set(None)
        try:
            note_attempt('upstream', status=200)
            self.assertIsNone(REQUEST_RECORD.get())
        finally:
            REQUEST_RECORD.reset(token)

    def test_attempts_survive_writer_restart_and_prune_with_their_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            store = HistoryStore(path, hours=1)
            store.start()
            value = RequestRecord('POST', '/responses')
            value.status, value.complete = 200, True
            token = REQUEST_RECORD.set(value)
            try:
                note_attempt('upstream', status=503, duration_s=.2)
                note_attempt('retry_wait', wait_s=.5)
                note_attempt('upstream', status=200, duration_s=.1)
            finally:
                REQUEST_RECORD.reset(token)
            store.request(value)
            self.assertTrue(store.close())
            row = store.requests()['items'][0]
            self.assertNotIn('attempts', row)  # Ordinary pages stay small.
            detail = store.attempts(row['id'])
            self.assertEqual(detail['total'], 3)
            store = HistoryStore(path, hours=1)
            store.start()
            self.assertEqual(store.attempts(row['id']), detail)
            expired = RequestRecord('POST', '/responses')
            expired.wall_time = time.time() - 7200
            store.request(expired)
            self.assertTrue(store.close())
            with closing(sqlite3.connect(path)) as database:
                self.assertEqual(database.execute('SELECT COUNT(*) FROM request_attempts').fetchone()[0], 1)
                self.assertEqual(database.execute("SELECT value FROM history_meta WHERE key='schema'").fetchone()[0], 2)

    def test_corrupt_stored_events_cannot_expose_arbitrary_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / 'history.sqlite3')
            store.start()
            value = RequestRecord('POST', '/responses')
            store.request(value)
            self.assertTrue(store.close())
            row = store.requests()['items'][0]
            with closing(sqlite3.connect(store.path)) as database:
                with database:
                    database.execute('UPDATE request_attempts SET detail=?', (
                        json.dumps({'events': [[{'secret': 'SYNTHETIC'}, 200, 0, 0], ['upstream', True, 0, 0], ['upstream', 200, 1, 0]], 'total': 3}),))
            result = store.attempts(row['id'])
            self.assertEqual(result['events'], [['upstream', 200, 1, 0]])
            self.assertNotIn('SYNTHETIC', json.dumps(result))


class RealAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_error_is_recorded_for_http_json_and_early_sse(self):
        error = {'message': 'session binding invalidated: SYNTHETIC_PRIVATE_ERROR; restart with a new session id'}
        cases = [httpx.Response(400, json={'error': error}),
                 httpx.Response(200, json={'status': 'failed', 'error': error}),
                 httpx.Response(200, headers={'content-type': 'text/event-stream'},
                    content=('event: response.failed\ndata: ' + json.dumps({'type': 'response.failed', 'response': {'status': 'failed', 'error': error}}) + '\n\n').encode())]
        for fixture in cases:
            with self.subTest(status=fixture.status_code, kind=fixture.headers.get('content-type')):
                used = False
                async def receive():
                    nonlocal used
                    if not used:
                        used = True
                        return {'type': 'http.request', 'body': b'{"input":"synthetic","stream":true}', 'more_body': False}
                    await asyncio.Event().wait()
                request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
                                   'headers': [(b'content-type', b'application/json')]}, receive)
                value = RequestRecord('POST', '/v1/responses')
                token = REQUEST_RECORD.set(value)
                previous = proxy._SHARED_CLIENT
                try:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: fixture)) as client:
                        proxy._SHARED_CLIENT = client
                        with mock.patch.multiple(proxy, REQUEST_GZIP_ENABLED=False, MAX_TRANSIENT_RETRIES=0):
                            response = await proxy.handle_responses(request)
                            if isinstance(response, proxy.UpstreamStreamingResponse):
                                await proxy._close_sse_producer(response.upstream_response, response.producer_task)
                    codes = [event[0] for event in json.loads(value.attempt_detail())['events']]
                    self.assertIn('binding_invalidated', codes)
                    self.assertNotIn('SYNTHETIC', value.attempt_detail())
                finally:
                    proxy._SHARED_CLIENT = previous
                    REQUEST_RECORD.reset(token)
                    proxy.reset_rate_limit_state()

    async def test_transport_failure_and_next_attempt_are_recorded(self):
        calls = 0
        async def upstream(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError('SYNTHETIC_PRIVATE_ERROR')
            return httpx.Response(200, json={'ok': True})
        record = RequestRecord('POST', '/v1/responses')
        token = REQUEST_RECORD.set(record)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                response = await proxy._send_with_transport_retry(client, 'POST', 'http://synthetic/v1/responses', {}, b'{}')
            self.assertEqual(response.status_code, 200)
            detail = json.loads(record.attempt_detail())
            self.assertEqual([row[0] for row in detail['events']], ['transport_connect', 'upstream'])
            self.assertGreater(detail['events'][0][3], 0)
            self.assertNotIn('SYNTHETIC', record.attempt_detail())
        finally:
            REQUEST_RECORD.reset(token)


if __name__ == '__main__':
    unittest.main()
