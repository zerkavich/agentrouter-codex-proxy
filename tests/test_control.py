"""Browser mutations, isolated settings and real request drain/cancellation."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import ControlRuntime, ProxyActivityMiddleware, attach_control, apply_saved_settings, load_settings
from telemetry import RuntimeTimings
import service


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'panel').mkdir()
        (self.root / 'panel/index.html').write_text('<html>local panel</html>')
        self.timings = RuntimeTimings(False)
        self.app = FastAPI()
        async def health():
            return {'proxy': 'agentrouter-codex-proxy', 'status': 'ok', 'version': '1.8.0'}
        async def diagnostics():
            return {'proxy': 'agentrouter-codex-proxy', 'schema_version': 1, 'status': 'ready',
                    'timings': self.timings.snapshot(), 'counts': {}}
        self.runtime = ControlRuntime(self.root, '1.8.0', 8788, health, diagnostics, self.timings)
        attach_control(self.app, self.runtime)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        @self.app.get('/v1/models')
        async def listing():
            return {'ok': True}
        @self.app.get('/v1/stream')
        async def stream():
            async def body():
                self.entered.set()
                try:
                    await self.release.wait()
                    yield b'complete'
                finally:
                    self.finished.set()
            return StreamingResponse(body())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 23000)),
                                       base_url='http://127.0.0.1:8788')
        self.headers = {'x-proxy-control': self.runtime.token}

    async def asyncTearDown(self):
        self.release.set()
        await self.runtime.close()
        await self.client.aclose()
        self.directory.cleanup()

    async def post(self, action, data):
        return await self.client.post('/_control/api/' + action, headers=self.headers, json=data)

    async def test_root_is_local_panel_and_not_provider_traffic(self):
        response = await self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('local panel', response.text)
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(self.runtime.active, 0)

    async def test_unknown_control_routes_never_reach_provider(self):
        self.assertEqual((await self.client.get('/_control/no-such-route')).status_code, 404)
        self.assertEqual((await self.client.post('/_control/no-such-route')).status_code, 404)
        self.assertEqual(self.runtime.active, 0)

    async def test_loopback_host_origin_and_session_are_all_required(self):
        for headers in ({'host': 'attacker.invalid:8788'}, {'origin': 'https://attacker.invalid'},
                        {'sec-fetch-site': 'cross-site'}, {'x-proxy-control': 'wrong'}):
            response = await self.client.post('/_control/api/pause', json={'paused': True}, headers={**self.headers, **headers})
            self.assertEqual(response.status_code, 403)
            self.assertFalse(self.runtime.paused)
        missing = await self.client.post('/_control/api/pause', json={'paused': True})
        self.assertEqual(missing.status_code, 403)
        remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('192.0.2.1', 23000)), base_url='http://127.0.0.1:8788')
        async with remote:
            self.assertEqual((await remote.get('/_control/api/session')).status_code, 403)

    async def test_profile_expires_and_rejects_invalid_or_large_input(self):
        for value in (True, -1, 301, '60'):
            self.assertEqual((await self.post('profile', {'duration_s': value})).status_code, 422)
        response = await self.client.post('/_control/api/profile', headers={**self.headers, 'content-type': 'application/json'}, content=b' ' * 20000)
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await self.post('profile', {'duration_s': .03})).status_code, 200)
        self.assertTrue(self.timings.enabled)
        await asyncio.sleep(.05)
        self.assertFalse(self.timings.enabled)

    async def test_settings_conflict_unknown_keys_and_configuration_preservation(self):
        original = b'OPENAI_API_KEY=synthetic-kept-secret\n# preserve bytes\n'
        (self.root / '.env').write_bytes(original)
        settings = (await self.client.get('/_control/api/settings')).json()
        response = await self.post('settings', {'revision': settings['revision'], 'changes': {'PROXY_REQUEST_GZIP': True}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['restart_required'])
        self.assertNotIn('synthetic-kept-secret', response.text)
        self.assertEqual((self.root / '.env').read_bytes(), original)
        self.assertEqual(load_settings(self.root)[0], {'PROXY_REQUEST_GZIP': True})
        self.assertEqual((await self.post('settings', {'revision': settings['revision'], 'changes': {'PROXY_VERBOSE': True}})).status_code, 409)
        self.assertEqual((await self.post('settings', {'revision': response.json()['revision'], 'changes': {'OPENAI_API_KEY': 'wrong'}})).status_code, 422)
        self.assertEqual((await self.post('settings', {'revision': response.json()['revision'], 'changes': {'PROXY_GZIP_WORKERS': 16, 'PROXY_GZIP_MAX_JOBS': 2}})).status_code, 422)

    async def test_settings_apply_only_at_worker_start_and_keep_prior_file(self):
        first = await self.post('settings', {'revision': 'none', 'changes': {'PROXY_VERBOSE': True}})
        previous = (self.root / 'logs/control-settings.json').read_bytes()
        await self.post('settings', {'revision': first.json()['revision'], 'changes': {'PROXY_VERBOSE': False}})
        self.assertTrue(any(path.read_bytes() == previous for path in (self.root / 'logs/control-backups').glob('*.json')))
        with mock.patch.dict(os.environ, {'PROXY_VERBOSE': '1'}):
            apply_saved_settings(self.root)
            self.assertEqual(os.environ['PROXY_VERBOSE'], '0')

    async def test_pause_rejects_new_api_calls_but_status_remains_available(self):
        self.assertEqual((await self.post('pause', {'paused': True})).status_code, 200)
        response = await self.client.get('/v1/models')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '2')
        self.assertEqual((await self.client.get('/_control/api/status')).status_code, 200)
        await self.post('pause', {'paused': False})
        self.assertEqual((await self.client.get('/v1/models')).status_code, 200)

    async def test_restart_drains_complete_stream_before_signalling_supervisor(self):
        self.runtime.supervised = True
        self.runtime.supervisor_token = 't' * 40
        request = asyncio.create_task(self.client.get('/v1/stream'))
        await self.entered.wait()
        self.assertEqual(self.runtime.active, 1)
        response = await self.post('restart', {'force': False})
        self.assertEqual(response.status_code, 202)
        await asyncio.sleep(.45)
        self.assertFalse((self.root / 'logs/control-command.json').exists())
        self.assertEqual((await self.client.get('/v1/models')).status_code, 503)
        self.release.set()
        await request
        await asyncio.wait_for(self.runtime.restart_task, 1)
        self.assertEqual(self.runtime.active, 0)
        command = json.loads((self.root / 'logs/control-command.json').read_text())
        self.assertEqual(command['worker_pid'], os.getpid())
        self.assertEqual(command['token'], self.runtime.supervisor_token)
        self.assertNotIn(self.runtime.supervisor_token, json.dumps(self.runtime.management()))

    async def test_cancel_pending_restart_resumes_new_calls(self):
        self.runtime.supervised = True
        self.runtime.enter()
        await self.post('restart', {'force': False})
        await self.post('restart/cancel', {})
        self.assertFalse(self.runtime.draining)
        self.assertFalse((self.root / 'logs/control-command.json').exists())
        self.runtime.leave()
        self.assertEqual((await self.client.get('/v1/models')).status_code, 200)

    async def test_restart_cannot_be_cancelled_after_the_command_commit_begins(self):
        import control
        import threading
        began, finish = threading.Event(), threading.Event()
        original = control.atomic_json
        def committing(path, data):
            began.set()
            if not finish.wait(3):
                raise OSError('synthetic test timeout')
            return original(path, data)
        self.runtime.supervised = True
        self.runtime.supervisor_token = 't' * 40
        with mock.patch.object(control, 'atomic_json', side_effect=committing):
            await self.post('restart', {'force': True})
            try:
                for _ in range(100):
                    if began.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(began.is_set())
                self.assertEqual((await self.post('restart/cancel', {})).status_code, 409)
            finally:
                finish.set()
                await self.runtime.restart_task
        self.assertTrue((self.root / 'logs/control-command.json').is_file())

    async def test_abandoned_stream_also_releases_drain_counter(self):
        task = asyncio.create_task(self.client.get('/v1/stream'))
        await self.entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.runtime.active, 0)
        self.assertTrue(self.finished.is_set())


class SupervisorCommandTests(unittest.TestCase):
    def test_only_matching_worker_and_secret_are_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'logs/control-command.json'
            path.parent.mkdir()
            for changes in ({'token': 'wrong'}, {'worker_pid': 99}, {'action': 'shell'}, {'id': '../bad'}):
                command = {'action': 'restart', 'token': 's' * 40, 'worker_pid': 12, 'id': 'a' * 32, **changes}
                path.write_text(json.dumps(command))
                self.assertIsNone(service._read_control_command(root, 's' * 40, 12))
                self.assertFalse(path.exists())
            path.write_text(json.dumps({'action': 'restart', 'token': 's' * 40, 'worker_pid': 12, 'id': 'a' * 32}))
            self.assertEqual(service._read_control_command(root, 's' * 40, 12)['id'], 'a' * 32)
