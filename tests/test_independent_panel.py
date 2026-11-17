"""Real loopback management isolation, offline history and recovery guards."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import IndependentPanel, RotatingFileLogger, _read_control_command
from telemetry import RequestRecord


class Worker:
    pid = 7123
    def poll(self):
        return None


class IndependentPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / 'panel').mkdir()
        (self.root / 'panel/index.html').write_text('<html>independent local panel</html>')
        (self.root / 'proxy.py').write_text('VERSION = "1.9.0"\n')
        self.worker = Worker()
        self.updating = False
        self.calls = []
        self.prepare_release = threading.Event()
        self.preparation_finished = threading.Event()
        owner = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.calls.append(self.path)
                if self.path == '/_control/api/status':
                    value = {'snapshot': {'proxy': 'agentrouter-codex-proxy', 'process_id': owner.worker.pid,
                         'version': '1.9.0', 'counts': {}, 'connection_pool': {}, 'rate_limits': {}, 'findings': []},
                         'management': {'active_requests': 0, 'history': {'enabled': True}}}
                elif self.path == '/_control/api/session':
                    value = {'token': 'w' * 40}
                else:
                    value = {'ok': True}
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                owner.calls.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                valid = self.headers.get('X-Proxy-Control') == 'w' * 40
                code = 200 if valid else 403
                if valid and self.path == '/_control/api/updates/prepare':
                    def finish():
                        owner.prepare_release.wait(5)
                        with owner.panel.lock:
                            owner.panel.cache['management']['update'] = {'state': 'prepared', 'commit': 'a' * 40}
                        owner.preparation_finished.set()
                    threading.Thread(target=finish, daemon=True).start()
                    body, code = b'{"state":"preparing","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","job_id":"synthetic"}', 202
                else:
                    body = b'{"paused":true}' if valid else b'{"error_code":"panel_session_expired"}'
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            panel_port = listener.getsockname()[1]
        self.panel = IndependentPanel(self.root, self.upstream.server_port, '127.0.0.1', 's' * 40,
                    lambda: (self.worker, self.updating), RotatingFileLogger(self.root / 'logs/log.txt'), panel_port=panel_port)
        self.assertTrue(self.panel.start())
        self.origin = 'http://127.0.0.1:' + str(panel_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 5
        while not self.panel.responsive and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.panel.responsive)

    def tearDown(self):
        self.prepare_release.set()
        self.panel.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(2)
        self.directory.cleanup()

    def test_retained_attempt_details_are_available_without_the_worker(self):
        from telemetry import REQUEST_RECORD, note_attempt
        record = RequestRecord('POST', '/responses')
        record.status, record.complete = 200, True
        token = REQUEST_RECORD.set(record)
        try:
            note_attempt('upstream', status=503)
            note_attempt('retry_wait', wait_s=.1)
            note_attempt('upstream', status=200)
        finally:
            REQUEST_RECORD.reset(token)
        self.panel.history.request(record)
        deadline = time.monotonic() + 5
        while self.panel.history.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        row = self.panel.history.requests()['items'][0]
        path = '/_control/api/history/attempts/' + str(row['id'])
        with self.opener.open(self.origin + path, timeout=3) as response:
            detail = json.loads(response.read())
        self.assertEqual([event[0] for event in detail['events']], ['upstream', 'retry_wait', 'upstream'])
        self.assertNotIn(path, self.calls)

    def request(self, path, data=None, headers=None):
        headers = dict(headers or {})
        if data is not None:
            headers.setdefault('Content-Type', 'application/json')
            headers.setdefault('X-Proxy-Control', self.panel.token)
        request = urllib.request.Request(self.origin + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            response = self.opener.open(request, timeout=4)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_independent_assets_and_status_do_not_transport_api_calls(self):
        self.assertIn(b'independent local panel', self.request('/')[1])
        status, body = self.request('/_control/api/status')
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertTrue(value['supervisor']['independent'])
        self.assertTrue(value['supervisor']['worker_responsive'])
        self.assertEqual(self.request('/v1/responses', {})[0], 404)
        self.assertFalse(any('/v1/' in path for path in self.calls))
        self.assertNotIn(('s' * 40).encode(), body)

    def test_token_host_origin_and_unknown_routes_are_guarded(self):
        for headers in ({'X-Proxy-Control': 'wrong'}, {'Host': 'bad.example:80'},
                        {'Origin': 'https://example.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/_control/api/pause', {'paused': True}, headers)[0], 403)
        self.assertEqual(self.request('/_control/api/unknown', {})[0], 404)
        self.assertEqual(self.request('/_control/assets/../proxy.py')[0], 404)
        self.assertEqual(self.request('/_control/api/history/requests?limit=1000')[0], 422)

    def test_management_relay_uses_worker_token_instead_of_browser_token(self):
        status, body = self.request('/_control/api/pause', {'paused': True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['paused'])
        self.assertIn('/_control/api/pause', self.calls)

    def test_update_job_relay_returns_before_preparation_finishes(self):
        started = time.monotonic()
        code, body = self.request('/_control/api/updates/prepare', {'commit': 'a' * 40})
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)['state'], 'preparing')
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.preparation_finished.is_set())
        self.prepare_release.set()
        self.assertTrue(self.preparation_finished.wait(3))
        with self.panel.lock:
            self.assertEqual(self.panel.cache['management']['update']['state'], 'prepared')

    def test_offline_panel_history_and_authenticated_recovery_remain_available(self):
        value = RequestRecord('POST', '/responses')
        value.status, value.complete = 200, True
        self.assertTrue(self.panel.history.request(value))
        deadline = time.monotonic() + 5
        while self.panel.history.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        self.upstream.shutdown()
        self.upstream.server_close()
        deadline = time.monotonic() + 5
        while self.panel.responsive and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.panel.responsive)
        self.assertEqual(self.request('/')[0], 200)
        rows = json.loads(self.request('/_control/api/history/requests')[1])['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.request('/_control/api/recover', {})[0], 422)
        self.assertEqual(self.request('/_control/api/recover', {'confirm': True})[0], 202)
        command = _read_control_command(self.root, 's' * 40, self.worker.pid)
        self.assertEqual(command['action'], 'restart')

    def test_recovery_never_overwrites_an_active_managed_update(self):
        self.updating = True
        self.assertEqual(self.request('/_control/api/recover', {'confirm': True})[0], 409)
        self.assertFalse((self.root / 'logs/control-command.json').exists())

    def test_handler_limit_rejects_excess_slow_clients_without_more_threads(self):
        sockets = []
        try:
            for _ in range(6):
                connection = socket.create_connection(('127.0.0.1', self.panel.port), timeout=2)
                connection.sendall(('POST /_control/api/pause HTTP/1.0\r\nHost: 127.0.0.1:' + str(self.panel.port)
                    + '\r\nContent-Type: application/json\r\nX-Proxy-Control: ' + self.panel.token + '\r\nContent-Length: 100\r\n\r\n').encode())
                sockets.append(connection)
            time.sleep(.1)
            self.assertFalse(self.panel.server.slots.acquire(blocking=False))
            try:
                self.assertEqual(self.request('/_control/api/status')[0], 503)
            except (ConnectionResetError, ConnectionAbortedError):
                # Closing an unread rejected socket can reset TCP on Windows.
                # It still rejects the caller without allocating another handler.
                pass
            self.assertFalse(self.panel.server.slots.acquire(blocking=False))
        finally:
            for connection in sockets:
                connection.close()

    def stop_monitor(self):
        self.panel.stop.set()
        self.panel.monitor.join(2)

    def test_watchdog_requires_three_spaced_failures_after_startup_grace(self):
        self.stop_monitor()
        now = time.monotonic()
        self.panel.worker_started = now - 60
        self.panel.next_watchdog = 0
        self.panel._watchdog_tick(False, self.worker, False, now)
        self.panel._watchdog_tick(False, self.worker, False, now + 1)
        self.assertEqual(self.panel.failures, 1)
        self.panel._watchdog_tick(False, self.worker, False, now + 10)
        self.assertFalse((self.root / 'logs/control-command.json').exists())
        self.panel._watchdog_tick(False, self.worker, False, now + 20)
        self.assertEqual(_read_control_command(self.root, 's' * 40, self.worker.pid)['action'], 'restart')

    def test_watchdog_preserves_responsive_workers_startup_and_managed_updates(self):
        self.stop_monitor()
        now = time.monotonic()
        self.panel.worker_started = now
        self.panel._watchdog_tick(False, self.worker, False, now + 5)
        self.assertEqual(self.panel.failures, 0)
        self.panel.worker_started = now - 60
        for offset in (0, 10, 20):
            self.panel._watchdog_tick(True, self.worker, False, now + offset)
            self.panel._watchdog_tick(False, self.worker, True, now + offset)
        self.assertEqual(self.panel.failures, 0)
        self.assertFalse((self.root / 'logs/control-command.json').exists())

    def test_watchdog_restart_budget_recovers_only_after_cooldown(self):
        self.stop_monitor()
        now = time.monotonic()
        self.panel.worker_started = now - 60
        self.panel.restarts.extend((now - 20, now - 10, now))
        for offset in (0, 10, 20):
            self.panel._watchdog_tick(False, self.worker, False, now + offset)
        self.assertFalse((self.root / 'logs/control-command.json').exists())
        self.panel._watchdog_tick(False, self.worker, False, now + 650)
        self.assertIsNotNone(_read_control_command(self.root, 's' * 40, self.worker.pid))

    def test_manual_recovery_can_resume_a_stopped_crash_loop(self):
        self.stop_monitor()
        self.worker = None
        self.assertEqual(self.request('/_control/api/recover', {'confirm': True})[0], 202)
        self.assertTrue(self.panel.recovery_requested.is_set())

    def test_occupied_panel_port_is_left_untouched(self):
        other = IndependentPanel(self.root, self.upstream.server_port, '127.0.0.1', 's' * 40,
                    lambda: (self.worker, False), RotatingFileLogger(self.root / 'logs/other.txt'), panel_port=self.panel.port)
        try:
            self.assertFalse(other.start())
            self.assertEqual(self.request('/_control/api/status')[0], 200)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
