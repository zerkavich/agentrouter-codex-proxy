"""Listener-release failures retain recovery instead of ending the supervisor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service
from control import atomic_json


class Worker:
    stdout = None
    def __init__(self, pid=7123):
        self.pid, self.returncode = pid, None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = 0
    kill = terminate
    def wait(self, timeout=None):
        return self.returncode


class SupervisorRestartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'panel').mkdir()
        (self.root / 'panel/index.html').write_text('<html>independent recovery</html>')
        (self.root / 'proxy.py').write_text('VERSION = "1.9.0"\n')
        self.logger = service.RotatingFileLogger(self.root / 'logs/proxy.log')
        self.stop = threading.Event()
        self.worker = Worker()
        self.worker.terminate()

    def listener(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(32)
        self.addCleanup(listener.close)
        return listener, listener.getsockname()[1]

    def until(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(.02)
        self.fail('Supervisor condition did not become ready')

    def test_delayed_listener_release_is_waited_for(self):
        listener, port = self.listener()
        panel = SimpleNamespace(root=self.root, server=None)
        timer = threading.Timer(.2, listener.close)
        timer.start()
        try:
            self.assertTrue(service._wait_for_restart_port(self.worker, port, '127.0.0.1',
                            self.stop, panel, self.logger, timeout_s=2))
        finally:
            timer.join(2)

    def test_an_open_owned_process_prevents_new_worker_startup(self):
        panel = SimpleNamespace(root=self.root, server=None)
        worker = Worker()
        with mock.patch.object(service, '_is_port_busy', return_value=False):
            self.assertFalse(service._wait_for_restart_port(worker, 18788, '127.0.0.1',
                             self.stop, panel, self.logger, timeout_s=.05))
        self.assertIsNone(worker.poll())

    def test_shutdown_cancels_a_listener_wait(self):
        listener, port = self.listener()
        panel = SimpleNamespace(root=self.root, server=None)
        timer = threading.Timer(.05, self.stop.set)
        timer.start()
        try:
            self.assertFalse(service._wait_for_restart_port(self.worker, port, '127.0.0.1',
                             self.stop, panel, self.logger, timeout_s=10))
            self.assertTrue(service._is_port_busy(port))
        finally:
            timer.join(2)

    def test_persistent_conflict_keeps_panel_and_does_not_kill_other_listener(self):
        listener, port = self.listener()
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            panel_port = reserved.getsockname()[1]
        panel = service.IndependentPanel(self.root, port, '127.0.0.1', 's' * 40,
                    lambda: (None, False), self.logger, panel_port=panel_port)
        self.assertTrue(panel.start())
        result = []
        action = {'id': 'a' * 32, 'action': 'restart', 'started_at': '2026-11-14T00:00:00+00:00'}
        thread = threading.Thread(target=lambda: result.append(service._wait_for_restart_port(
            self.worker, port, '127.0.0.1', self.stop, panel, self.logger, action, timeout_s=.05)))
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f'http://127.0.0.1:{panel_port}'
        def status():
            with opener.open(base + '/_control/api/status', timeout=2) as response:
                return json.loads(response.read())
        try:
            self.until(lambda: status()['management']['last_action'])
            self.assertEqual(status()['management']['last_action']['error_code'], 'restart_port_occupied')
            self.assertFalse(status()['supervisor']['worker_responsive'])
            request = urllib.request.Request(base + '/_control/api/recover', data=b'{"confirm":true}',
                headers={'Content-Type': 'application/json', 'X-Proxy-Control': panel.token}, method='POST')
            with opener.open(request, timeout=2) as response:
                self.assertEqual(response.status, 202)
            self.assertTrue(service._is_port_busy(port))
            self.assertTrue(thread.is_alive())
            listener.close()
            thread.join(3)
            self.assertEqual(result, [True])
            self.assertTrue(panel.server is not None)
            self.assertEqual(status()['management']['last_action']['action'], 'restart')
        finally:
            self.stop.set()
            thread.join(3)
            panel.close()

    def test_main_survives_failed_restart_then_verifies_replacement(self):
        busy, shutdown = threading.Event(), threading.Event()
        workers, result = [], []
        panel = SimpleNamespace(root=self.root, server=object(), port=18789,
                    recovery_requested=threading.Event(), history=mock.Mock(),
                    start=mock.Mock(), close=mock.Mock())
        def spawn(*args, **kwargs):
            worker = Worker(7123 + len(workers))
            workers.append(worker)
            return worker
        read_command = service._read_control_command
        def commands(*args):
            if shutdown.is_set():
                raise KeyboardInterrupt()
            return read_command(*args)
        def run():
            try:
                result.append(service.main(['--port', '18788', '--no-auto-update', '--skip-start-update']))
            except KeyboardInterrupt:
                result.append('stopped')
            except BaseException as error:
                result.append(error)
        with mock.patch.dict(os.environ, {'PROXY_CONTROL_STATE_DIR': str(self.root / 'logs'),
                                          'PROXY_CONTROL_UPDATES': '0'}), \
                mock.patch.object(service, 'ROOT', self.root), \
                mock.patch.object(service, 'IndependentPanel', return_value=panel), \
                mock.patch.object(service, '_ensure_dependencies', return_value=True), \
                mock.patch.object(service, '_resolve_worker_python', return_value='synthetic-python'), \
                mock.patch.object(service, '_is_port_busy', side_effect=lambda *args: busy.is_set()), \
                mock.patch.object(service, '_probe_our_health', return_value=True), \
                mock.patch.object(service, '_read_control_command', side_effect=commands), \
                mock.patch.object(service.subprocess, 'Popen', side_effect=spawn):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.until(lambda: workers)
                busy.set()
                atomic_json(self.root / 'logs/control-command.json', {'action': 'restart',
                            'token': os.environ['PROXY_SUPERVISOR_TOKEN'], 'worker_pid': workers[0].pid,
                            'id': 'a' * 32})
                def blocked_or_exited():
                    path = self.root / 'logs/control-action.json'
                    return not thread.is_alive() or (path.is_file() and json.loads(path.read_text())['state'] == 'failed')
                self.until(blocked_or_exited)
                self.assertTrue(thread.is_alive(), result)
                panel.close.assert_not_called()
                self.assertEqual(len(workers), 1)
                self.assertIsNotNone(workers[0].poll())
                busy.clear()
                self.until(lambda: len(workers) == 2)
                def verified():
                    action = json.loads((self.root / 'logs/control-action.json').read_text())
                    return action if action['state'] == 'verified' else None
                action = self.until(verified)
                self.assertGreaterEqual(action['duration_s'], 5)
                self.assertEqual(action['id'], 'a' * 32)
                panel.close.assert_not_called()
            finally:
                busy.clear()
                shutdown.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['stopped'])
        self.assertTrue(all(worker.poll() is not None for worker in workers))


if __name__ == '__main__':
    unittest.main()
