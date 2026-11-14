"""Disk/memory bounds, privacy, cancellation and automatic incident capture."""
from __future__ import annotations

import asyncio
from contextlib import closing
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import ControlRuntime, attach_control
from telemetry import HistoryStore, RequestRecord, RuntimeTimings, note_stream_error, process_memory


def record(index=0):
    value = RequestRecord('POST', '/v1/responses')
    value.status, value.complete = 200, True
    value.request_bytes, value.response_bytes = index, index + 1
    return value


class MemorySampleTests(unittest.TestCase):
    def test_supervisor_helpers_do_not_load_the_api_framework(self):
        code = ('import sys; sys.path[:0] = ' + repr(sys.path)
                + '; import service, control; assert "fastapi" not in sys.modules')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repeated_memory_samples_do_not_retain_new_ctypes_pointer_types(self):
        process_memory()
        cached_types = set(ctypes._pointer_type_cache)
        for _ in range(100):
            value = process_memory()
            self.assertEqual(set(value), {'rss_bytes', 'private_bytes'})
            if os.name == 'nt' or Path('/proc/self/statm').is_file():
                self.assertGreater(value['rss_bytes'], 0)
        self.assertEqual(set(ctypes._pointer_type_cache), cached_types)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name).resolve() / 'history.sqlite3'
        self.store = HistoryStore(self.path, max_requests=100, hours=1)
        self.store.start()
        deadline = time.monotonic() + 5
        while not self.store.available and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.store.available)

    def tearDown(self):
        self.assertTrue(self.store.close())
        self.directory.cleanup()

    def flush(self, store=None):
        store = store or self.store
        deadline = time.monotonic() + 5
        while store.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(store.pending.unfinished_tasks, 0)
        self.assertEqual(store.write_errors, 0)

    def test_row_bounds_and_strictly_older_cursor_pages(self):
        for index in range(150):
            self.assertTrue(self.store.request(record(index)))
        self.flush()
        first = self.store.requests(limit=50)
        second = self.store.requests(limit=50, before=first['next_before'])
        self.assertEqual(len(first['items']), 50)
        self.assertEqual(len(second['items']), 50)
        self.assertIsNone(second['next_before'])
        self.assertFalse(set(row['id'] for row in first['items']) & set(row['id'] for row in second['items']))
        self.assertEqual(first['items'][0]['request_bytes'], 149)
        self.assertEqual(second['items'][-1]['request_bytes'], 50)

    def test_expired_requests_and_reports_are_not_returned(self):
        expired = record()
        expired.wall_time = time.time() - 7200
        self.store.request(expired)
        self.store.submit('report', (time.time() - 7200, '{}'))
        self.store.request(record(7))
        self.flush()
        self.assertEqual(len(self.store.requests()['items']), 1)
        self.assertEqual(self.store.reports()['items'], [])

    def test_history_survives_writer_restart(self):
        self.store.request(record(4))
        self.flush()
        self.store.close()
        self.store = HistoryStore(self.path, max_requests=100, hours=1)
        self.store.start()
        self.assertEqual(self.store.requests()['items'][0]['request_bytes'], 4)

    def test_active_calls_survive_a_crash_and_are_marked_interrupted(self):
        active = RequestRecord('POST', '/responses')
        self.assertTrue(self.store.begin_request(active))
        self.flush()
        self.assertEqual(self.store.requests()['items'][0]['outcome'], 'active')
        self.store.close()
        self.store = HistoryStore(self.path, max_requests=100, hours=1)
        self.store.start()
        self.store.interrupt_worker(os.getpid())
        self.flush()
        row = self.store.requests()['items'][0]
        self.assertEqual(row['outcome'], 'interrupted')
        self.assertEqual(row['error_code'], 'worker_restarted')

    def test_completion_updates_the_started_row_without_duplicates(self):
        value = record(9)
        self.store.begin_request(value)
        self.store.request(value)
        self.flush()
        rows = self.store.requests()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'success')
        self.assertEqual(rows[0]['request_bytes'], 9)

    def test_queue_saturation_drops_without_blocking_or_growing(self):
        store = HistoryStore(self.path.with_name('saturated.sqlite3'), queue_size=16)
        began = time.perf_counter()
        for index in range(1000):
            store.request(record(index))
        self.assertEqual(store.pending.qsize(), 16)
        self.assertEqual(store.dropped, 984)
        self.assertLess(time.perf_counter() - began, 1)

    def test_recorded_starts_keep_their_completions_when_the_queue_is_saturated(self):
        store = HistoryStore(self.path.with_name('reserved.sqlite3'), queue_size=16)
        values = [record(index) for index in range(8)]
        for value in values:
            self.assertTrue(store.begin_request(value))
        # Withhold the disk writer and overload it with unrelated metadata.
        for _ in range(32):
            store.event('worker_started', worker_pid=7)
        for value in values:
            self.assertTrue(store.request(value))
        try:
            store.start()
            self.flush(store)
            rows = store.requests()['items']
            self.assertEqual(len(rows), len(values))
            self.assertTrue(all(row['outcome'] == 'success' for row in rows))
        finally:
            store.close()

    def test_disabling_new_recording_still_finishes_already_recorded_requests(self):
        value = record(12)
        self.assertTrue(self.store.begin_request(value))
        self.flush()
        self.store.enabled = False
        self.assertTrue(self.store.request(value))
        self.flush()
        self.assertEqual(self.store.requests()['items'][0]['outcome'], 'success')

    def test_metrics_events_and_reports_have_independent_row_limits(self):
        for _ in range(18):
            self.store.report({'version': '1.9.0', 'timings': {}})
        for _ in range(260):
            if not self.store.event('worker_started', worker_pid=7):
                self.flush()
                self.assertTrue(self.store.event('worker_started', worker_pid=7))
        self.store.metric({'counts': {}, 'connection_pool': {}, 'rate_limits': {}}, {'active_requests': 3})
        self.flush()
        self.assertEqual(len(self.store.reports()['items']), 16)
        self.assertEqual(len(self.store.events()['items']), 50)
        metrics = self.store.metrics()['items']
        self.assertEqual(metrics[0]['active'], 3)
        if os.name == 'nt' or sys.platform.startswith('linux'):
            self.assertGreater(metrics[0]['rss_bytes'], 0)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM events').fetchone()[0], 256)

    def test_event_details_reject_arbitrary_strings_and_unknown_codes(self):
        self.store.event('worker_started', worker_pid=7, prompt='synthetic-secret', arbitrary='synthetic-secret')
        self.flush()
        self.assertEqual(self.store.events()['items'][0]['detail'], {'worker_pid': 7})
        with self.assertRaises(ValueError):
            self.store.event('synthetic-secret')

    def test_failed_storage_does_not_block_request_history_producers(self):
        failed = HistoryStore(self.path / 'invalid' / 'history.sqlite3', queue_size=16)
        failed.start()
        failed.thread.join(3)
        self.assertFalse(failed.available)
        self.assertGreater(failed.write_errors, 0)
        for _ in range(40):
            failed.request(record())
        self.assertEqual(failed.pending.qsize(), 16)
        failed.close()

    def test_connection_initialization_failure_closes_the_opened_database(self):
        connect = sqlite3.connect
        opened = []

        class FailedPragma(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith('PRAGMA'):
                    raise sqlite3.OperationalError('synthetic_initialization_failure')
                return super().execute(sql, parameters)

        def failing_connection(*args, **kwargs):
            database = connect(*args, factory=FailedPragma, **kwargs)
            opened.append(database)
            return database

        for write in (False, True):
            with self.subTest(write=write):
                with mock.patch('telemetry.sqlite3.connect', side_effect=failing_connection):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.store._connect(write=write)
                try:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        opened[-1].execute('SELECT 1')
                finally:
                    opened[-1].close()

    def test_close_reports_an_unfinished_writer_and_can_be_retried(self):
        store = HistoryStore(self.path.with_name('slow-shutdown.sqlite3'))
        entered, release = threading.Event(), threading.Event()
        initialize = store._schema

        def slow_initialize(database):
            entered.set()
            release.wait(5)
            initialize(database)

        try:
            with mock.patch.object(store, '_schema', side_effect=slow_initialize):
                store.start()
                self.assertTrue(entered.wait(5))
                self.assertFalse(store.close(timeout=0))
                self.assertTrue(store.thread.is_alive())
                release.set()
                self.assertTrue(store.close())
                self.assertFalse(store.thread.is_alive())
                self.assertTrue(store.close(timeout=0))
            # Windows refuses the rename if the writer still owns the file.
            store.path.rename(store.path.with_suffix('.closed'))
        finally:
            release.set()
            store.close()
            if store.thread is not None:
                store.thread.join(5)

    def test_two_process_style_writers_and_readers_share_wal_history(self):
        other = HistoryStore(self.path, max_requests=100, hours=1)
        other.start()
        try:
            for index in range(30):
                self.store.request(record(index))
                other.event('worker_started', worker_pid=index)
                self.store.requests(limit=10)
            self.flush()
            self.flush(other)
            self.assertEqual(len(other.requests()['items']), 30)
            self.assertEqual(len(self.store.events()['items']), 30)
        finally:
            other.close()


class RequestHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.timings = RuntimeTimings(False)
        self.app = FastAPI()
        async def health():
            return {'proxy': 'agentrouter-codex-proxy', 'status': 'ok', 'version': '1.9.0'}
        async def diagnostics():
            return {'proxy': 'agentrouter-codex-proxy', 'schema_version': 1, 'status': 'ready',
                    'version': '1.9.0', 'process_id': os.getpid(), 'counts': {}, 'findings': [],
                    'timings': self.timings.snapshot(), **self.timings.profile_status()}
        # Partial old settings must gain defaults for all new options.
        self.runtime = ControlRuntime(self.root, '1.9.0', 8788, health, diagnostics, self.timings,
                                      active_settings={'PROXY_TIMINGS': False})
        attach_control(self.app, self.runtime)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        @self.app.post('/v1/responses')
        async def response(request: Request):
            await request.body()
            with self.timings.measure('rate_limit_gate'):
                await asyncio.sleep(.01)
            return JSONResponse({'output': 'SYNTHETIC_RESPONSE_CANARY'})
        @self.app.get('/v1/failure')
        async def failure():
            return JSONResponse({'error': 'SYNTHETIC_ERROR_CANARY'}, status_code=502)
        @self.app.get('/v1/stream')
        async def stream():
            async def body():
                self.entered.set()
                await self.release.wait()
                yield b'done'
            return StreamingResponse(body())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 23000)), base_url='http://127.0.0.1:8788')
        await self.runtime.start()
        self.headers = {'x-proxy-control': self.runtime.token}

    async def asyncTearDown(self):
        self.release.set()
        await self.runtime.close()
        self.assertFalse(self.runtime.history.thread.is_alive())
        await self.client.aclose()
        self.directory.cleanup()

    async def flush(self):
        deadline = time.monotonic() + 5
        while self.runtime.history.pending.unfinished_tasks and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertEqual(self.runtime.history.pending.unfinished_tasks, 0)
        self.assertEqual(self.runtime.history.write_errors, 0)

    async def test_history_has_numeric_metadata_without_payload_query_or_credentials(self):
        await self.client.post('/v1/responses?private=SYNTHETIC_QUERY_CANARY', content=b'SYNTHETIC_REQUEST_CANARY', headers={'authorization': 'Bearer SYNTHETIC_KEY_CANARY'})
        await self.flush()
        response = await self.client.get('/_control/api/history/requests')
        row = response.json()['items'][0]
        self.assertEqual(row['endpoint'], 'responses')
        self.assertEqual(row['status'], 200)
        self.assertEqual(row['outcome'], 'success')
        self.assertEqual(row['request_bytes'], len(b'SYNTHETIC_REQUEST_CANARY'))
        self.assertGreater(row['quota_ms'], 0)
        self.assertEqual(self.timings.stages['rate_limit_gate'].count, 0)
        self.assertNotIn('SYNTHETIC', response.text)
        await self.runtime.close()
        for path in self.root.glob('logs/request-history.sqlite3*'):
            self.assertNotIn(b'SYNTHETIC', path.read_bytes())

    async def test_cancelled_stream_is_recorded_after_capacity_is_released(self):
        task = asyncio.create_task(self.client.get('/v1/stream'))
        await self.entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.flush()
        self.assertEqual(self.runtime.active, 0)
        rows = self.runtime.history.requests(outcome='cancelled')['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 200)

    async def test_three_real_failures_trigger_one_temporary_profile(self):
        for _ in range(4):
            await self.client.get('/v1/failure')
        self.assertTrue(self.timings.enabled)
        self.assertEqual(self.timings.profile_status()['timing_mode'], 'temporary')
        await self.flush()
        events = self.runtime.history.events()['items']
        self.assertEqual(sum(row['code'] == 'auto_profile_started' for row in events), 1)
        self.assertEqual(len(self.runtime.history.requests(outcome='http_error')['items']), 4)

    async def test_completed_sse_failures_trigger_capture_after_final_disconnect(self):
        @self.app.get('/v1/late-error')
        async def late_error():
            async def body():
                yield b'event: response.output_text.delta\ndata: {}\n\n'
                note_stream_error('upstream_response_failed')
                yield b'event: response.failed\ndata: {}\n\n'
            return StreamingResponse(body(), media_type='text/event-stream')
        for _ in range(3):
            self.assertEqual((await self.client.get('/v1/late-error')).status_code, 200)
        await self.flush()
        self.assertEqual(len(self.runtime.history.requests(outcome='interrupted')['items']), 3)
        self.assertTrue(self.timings.enabled)

    async def test_paused_requests_do_not_start_automatic_diagnostics(self):
        self.runtime.paused = True
        for _ in range(4):
            self.assertEqual((await self.client.post('/v1/responses')).status_code, 503)
        await self.flush()
        self.assertFalse(self.timings.enabled)
        self.assertEqual(len(self.runtime.history.requests()['items']), 4)

    async def test_finished_automatic_capture_saves_a_sanitized_report(self):
        for _ in range(3):
            await self.client.get('/v1/failure')
        self.runtime.auto_profile_until = time.monotonic() - 1
        self.timings.profile_until = time.monotonic() - 1
        deadline = time.monotonic() + 7
        while not self.runtime.auto_profile_saved and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertTrue(self.runtime.auto_profile_saved)
        await self.flush()
        reports = self.runtime.history.reports()['items']
        self.assertEqual(len(reports), 1)
        response = await self.client.get('/_control/api/history/report/' + str(reports[0]['id']))
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['content-disposition'])
        self.assertNotIn('SYNTHETIC', response.text)
        self.assertFalse(self.timings.enabled)

    async def test_history_filters_limits_and_local_origin_are_guarded(self):
        for query in ('?limit=101', '?limit=0', '?before=-1', '?before=9223372036854775808', '?outcome=private', '?unknown=x'):
            self.assertEqual((await self.client.get('/_control/api/history/requests' + query)).status_code, 422)
        self.assertEqual((await self.client.get('/_control/api/history/requests', headers={'origin': 'https://example.invalid'})).status_code, 403)
        self.assertEqual((await self.client.get('/_control/api/history/no-such-route')).status_code, 404)

    async def test_clear_history_requires_explicit_confirmation(self):
        await self.client.post('/v1/responses')
        await self.flush()
        response = await self.client.post('/_control/api/history/clear', headers=self.headers, json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.runtime.history.requests()['items']), 1)
        response = await self.client.post('/_control/api/history/clear', headers=self.headers, json={'confirm': True})
        self.assertEqual(response.status_code, 202)
        await self.flush()
        self.assertEqual(self.runtime.history.requests()['items'], [])

    async def test_disabled_recording_leaves_request_path_without_history_records(self):
        self.runtime.history.enabled = False
        await self.client.post('/v1/responses')
        await self.flush()
        self.assertEqual(self.runtime.history.requests()['items'], [])
        self.assertEqual(self.timings.stages['rate_limit_gate'].count, 0)


if __name__ == '__main__':
    unittest.main()
