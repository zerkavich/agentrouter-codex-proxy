"""Local browser control plane, isolated from provider traffic and credentials."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect


SETTINGS = {
    'PROXY_REQUEST_GZIP': {'kind': 'bool', 'default': False, 'group': 'compression'},
    'PROXY_REQUEST_GZIP_MIN_BYTES': {'kind': 'int', 'default': 262144, 'min': 65536, 'max': 16777216, 'group': 'compression'},
    'PROXY_GZIP_WORKERS': {'kind': 'int', 'default': 4, 'min': 1, 'max': 16, 'group': 'compression'},
    'PROXY_GZIP_MAX_JOBS': {'kind': 'int', 'default': 16, 'min': 1, 'max': 128, 'group': 'compression'},
    'PROXY_GZIP_REJECT_TTL_S': {'kind': 'float', 'default': 60, 'min': 0, 'max': 3600, 'group': 'compression'},
    'PROXY_GZIP_UPLOAD_MBIT_S': {'kind': 'float', 'default': 0, 'min': 0, 'max': 100000, 'group': 'compression'},
    'PROXY_GZIP_COST_MARGIN': {'kind': 'float', 'default': 2, 'min': 1, 'max': 10, 'group': 'compression'},
    'PROXY_CONNECT_TIMEOUT_S': {'kind': 'float', 'default': 20, 'min': 1, 'max': 120, 'group': 'network'},
    'PROXY_READ_TIMEOUT_S': {'kind': 'float', 'default': 600, 'min': 30, 'max': 3600, 'group': 'network'},
    'PROXY_VERBOSE': {'kind': 'bool', 'default': False, 'group': 'network'},
    'PROXY_RATE_LIMIT_BASE_S': {'kind': 'float', 'default': 2, 'min': 0.05, 'max': 30, 'group': 'recovery'},
    'PROXY_RATE_LIMIT_MAX_DELAY_S': {'kind': 'float', 'default': 15, 'min': 0.05, 'max': 120, 'group': 'recovery'},
    'PROXY_MAX_RATE_LIMIT_WAIT_S': {'kind': 'float', 'default': 45, 'min': 1, 'max': 300, 'group': 'recovery'},
    'PROXY_MAX_TRANSIENT_RETRIES': {'kind': 'int', 'default': 6, 'min': 0, 'max': 12, 'group': 'recovery'},
    'PROXY_RECOVERY_STAGGER_S': {'kind': 'float', 'default': 0.6, 'min': 0, 'max': 5, 'group': 'recovery'},
    'PROXY_RATE_LIMIT_SCOPE': {'kind': 'enum', 'default': 'credential', 'values': ['credential', 'shared'], 'group': 'recovery'},
    'PROXY_RATE_LIMIT_PARTITIONS': {'kind': 'int', 'default': 256, 'min': 1, 'max': 4096, 'group': 'recovery'},
    'PROXY_SSE_BUFFER_BYTES': {'kind': 'int', 'default': 1048576, 'min': 65536, 'max': 16777216, 'group': 'streaming'},
    'PROXY_SSE_PRIME_BYTES': {'kind': 'int', 'default': 262144, 'min': 65536, 'max': 4194304, 'group': 'streaming'},
    'PROXY_SSE_PRIME_FRAMES': {'kind': 'int', 'default': 1024, 'min': 1, 'max': 8192, 'group': 'streaming'},
    'PROXY_SSE_PRIME_FIRST_TIMEOUT_S': {'kind': 'float', 'default': 45, 'min': 1, 'max': 180, 'group': 'streaming'},
    'PROXY_SSE_PRIME_CREATED_GRACE_S': {'kind': 'float', 'default': 3, 'min': 0, 'max': 30, 'group': 'streaming'},
    'PROXY_TIMINGS': {'kind': 'bool', 'default': False, 'group': 'diagnostics'},
}


class ControlError(Exception):
    def __init__(self, code, status=422, fields=None):
        self.code, self.status, self.fields = code, status, fields or []
        super().__init__(code)


def utc():
    return datetime.now(timezone.utc).isoformat()


def settings_path(root):
    return state_dir(root) / 'control-settings.json'


def state_dir(root):
    return Path(os.environ.get('PROXY_CONTROL_STATE_DIR', str(Path(root) / 'logs'))).resolve()


def validate_settings(values):
    if not isinstance(values, dict) or any(name not in SETTINGS for name in values):
        raise ControlError('unknown_setting')
    clean = {}
    errors = []
    for name, value in values.items():
        spec = SETTINGS[name]
        kind = spec['kind']
        valid = False
        if kind == 'bool':
            valid = isinstance(value, bool)
        elif kind == 'enum':
            valid = value in spec['values'] if isinstance(value, str) else False
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                valid = math.isfinite(value) and spec['min'] <= value <= spec['max']
                if kind == 'int':
                    valid = valid and isinstance(value, int)
            except OverflowError:
                pass
        if not valid:
            errors.append(name)
        else:
            clean[name] = value
    if errors:
        raise ControlError('invalid_setting', fields=errors)
    return clean


def load_settings(root):
    path = settings_path(root)
    try:
        with path.open('rb') as stream:
            body = stream.read(16385)
    except FileNotFoundError:
        return {}, 'none'
    except OSError:
        raise ControlError('settings_unreadable', 503) from None
    if len(body) > 16384:
        raise ControlError('settings_too_large', 503)
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or data.get('schema_version') != 1:
            raise ValueError()
        values = validate_settings(data['values'])
    except (ValueError, KeyError, TypeError, ControlError):
        raise ControlError('settings_invalid', 503) from None
    return values, hashlib.sha256(body).hexdigest()


def settings_environment(values):
    return {name: ('1' if value else '0') if isinstance(value, bool) else str(value)
            for name, value in values.items()}


def apply_saved_settings(root):
    """Called once at worker startup; never writes .env or provider settings."""
    values, _ = load_settings(root)
    os.environ.update(settings_environment(values))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def current_settings(environment=None):
    environment = os.environ if environment is None else environment
    values = {}
    for name, spec in SETTINGS.items():
        raw = environment.get(name)
        if raw is None:
            values[name] = spec['default']
        elif spec['kind'] == 'bool':
            values[name] = raw.lower() not in ('0', '', 'false', 'no', 'off')
        elif spec['kind'] == 'enum':
            values[name] = raw
        else:
            try:
                values[name] = (int if spec['kind'] == 'int' else float)(raw)
            except (ValueError, OverflowError):
                values[name] = spec['default']
    return values


def safe_action(root):
    try:
        with (state_dir(root) / 'control-action.json').open('rb') as stream:
            body = stream.read(4097)
        data = json.loads(body) if len(body) <= 4096 else {}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('state') not in ('restarting', 'verified', 'failed', 'rolled_back'):
        return None
    return {name: data[name] for name in ('id', 'state', 'action', 'started_at', 'finished_at', 'duration_s', 'error_code')
            if name in data and isinstance(data[name], (str, int, float)) and not isinstance(data[name], bool)}


class ControlRuntime:
    def __init__(self, root, version, port, health, diagnostics, timings, active_settings=None):
        self.root, self.version = Path(root), version
        self.port, self.health, self.diagnostics, self.timings = port, health, diagnostics, timings
        self.active_settings = active_settings or current_settings()
        self.token = secrets.token_urlsafe(32)
        self.active = 0
        self.paused = os.environ.get('PROXY_CONTROL_BOOT_PAUSED') == '1'
        self.draining = False
        self.restart_job = None
        self.restart_task = None
        self.idle = None
        self.settings_lock = asyncio.Lock()
        self.updates_lock = asyncio.Lock()
        self.update_info = {'state': 'not_checked', 'update_available': False}
        self.supervisor_token = os.environ.get('PROXY_SUPERVISOR_TOKEN', '')
        self.supervised = len(self.supervisor_token) >= 32

    def idle_event(self):
        if self.idle is None:
            self.idle = asyncio.Event()
            if not self.active:
                self.idle.set()
        return self.idle

    def enter(self):
        self.active += 1
        if self.idle is not None:
            self.idle.clear()

    def leave(self):
        self.active -= 1
        if not self.active and self.idle is not None:
            self.idle.set()

    async def close(self):
        if self.restart_task is not None and not self.restart_task.done():
            self.restart_task.cancel()
            await asyncio.gather(self.restart_task, return_exceptions=True)

    def management(self):
        values, revision = load_settings(self.root)
        effective = {**self.active_settings, **values}
        return {'active_requests': self.active, 'paused': self.paused, 'draining': self.draining,
                'restart_supported': self.supervised, 'restart_job': self.restart_job,
                'last_action': safe_action(self.root), 'settings_revision': revision,
                'pending_settings': effective != self.active_settings,
                'updates_supported': os.environ.get('PROXY_CONTROL_UPDATES') == '1',
                'update': self.update_info}

    async def save_settings(self, changes, revision):
        clean = validate_settings(changes)
        async with self.settings_lock:
            stored, previous_revision = load_settings(self.root)
            if revision != previous_revision:
                raise ControlError('settings_conflict', 409)
            merged = {**stored, **clean}
            effective = {**self.active_settings, **merged}
            if effective['PROXY_GZIP_MAX_JOBS'] < effective['PROXY_GZIP_WORKERS']:
                raise ControlError('gzip_jobs_below_workers', fields=['PROXY_GZIP_MAX_JOBS'])
            if effective['PROXY_RATE_LIMIT_MAX_DELAY_S'] < effective['PROXY_RATE_LIMIT_BASE_S']:
                raise ControlError('backoff_max_below_base', fields=['PROXY_RATE_LIMIT_MAX_DELAY_S'])
            path = settings_path(self.root)
            identifier = uuid.uuid4().hex
            if path.exists():
                backup = state_dir(self.root) / 'control-backups' / (identifier + '.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                with backup.open('xb') as stream:
                    stream.write(path.read_bytes())
            try:
                await asyncio.to_thread(atomic_json, path, {'schema_version': 1, 'saved_at': utc(), 'values': merged})
            except OSError:
                raise ControlError('settings_save_failed', 503) from None
            values, new_revision = load_settings(self.root)
            return {'saved': True, 'revision': new_revision, 'values': {**self.active_settings, **values},
                    'restart_required': effective != self.active_settings}

    async def request_restart(self, force=False, commit=None):
        if not self.supervised:
            raise ControlError('restart_requires_supervisor', 409)
        if self.updates_lock.locked():
            raise ControlError('update_busy', 409)
        if self.restart_task is not None and not self.restart_task.done():
            raise ControlError('restart_already_pending', 409)
        self.draining = True
        self.restart_job = {'id': uuid.uuid4().hex, 'state': 'draining', 'started_at': utc(), 'force': force,
                            'action': 'update' if commit else 'restart'}
        self.restart_task = asyncio.create_task(self._restart(force, commit))
        return self.restart_job

    async def _restart(self, force, commit=None):
        try:
            # Deliver the accepted action before the supervisor replaces this worker.
            await asyncio.sleep(0.4)
            if not force:
                await self.idle_event().wait()
            command = {'action': 'update' if commit else 'restart', 'token': self.supervisor_token,
                       'worker_pid': os.getpid(), 'id': self.restart_job['id'], 'started_at': self.restart_job['started_at']}
            if commit:
                command['commit'] = commit
            # Cancellation is possible while draining, but not once the
            # supervisor command is being committed by the filesystem thread.
            self.restart_job['state'] = 'restarting'
            await asyncio.to_thread(atomic_json, state_dir(self.root) / 'control-command.json', command)
        except asyncio.CancelledError:
            self.draining = False
            if self.restart_job is not None:
                self.restart_job['state'] = 'cancelled'
            raise
        except OSError:
            self.draining = False
            self.restart_job.update(state='failed', error_code='restart_signal_failed')

    async def cancel_restart(self):
        if not self.restart_task or self.restart_task.done() or self.restart_job['state'] != 'draining':
            raise ControlError('restart_not_cancellable', 409)
        self.restart_task.cancel()
        await asyncio.gather(self.restart_task, return_exceptions=True)
        self.draining = False
        self.restart_job['state'] = 'cancelled'
        return {'cancelled': True}


class ProxyActivityMiddleware:
    """Two counters around API traffic; no extra task, timer or response buffering."""
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if (scope['type'] != 'http' or path == '/' or path.startswith('/_control')
                or path in ('/_health', '/_stats', '/_diagnostics', '/_diagnostics/profile', '/favicon.ico')):
            return await self.app(scope, receive, send)
        runtime = self.runtime
        if runtime.paused or runtime.draining:
            reason = 'Proxy is waiting for a controlled restart' if runtime.draining else 'New proxy requests are paused in the local panel'
            response = JSONResponse(status_code=503, headers={'Retry-After': '2'},
                                    content={'error': {'type': 'proxy_paused', 'message': reason}})
            return await response(scope, receive, send)
        runtime.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            runtime.leave()


def local_request(request, port, *, mutation=False, token=None, navigation=False):
    if request.client is None:
        raise ControlError('local_panel_only', 403)
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            raise ValueError()
    except ValueError:
        raise ControlError('local_panel_only', 403) from None
    hosts = request.headers.getlist('host')
    if len(hosts) != 1:
        raise ControlError('invalid_panel_host', 403)
    raw_host = hosts[0]
    try:
        parsed = urlsplit('http://' + raw_host)
        host = parsed.hostname
        actual_port = parsed.port or 80
        allowed = host == 'localhost' or (host is not None and ipaddress.ip_address(host).is_loopback)
        if (not allowed or actual_port != port or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or raw_host != raw_host.strip()):
            raise ValueError()
    except ValueError:
        raise ControlError('invalid_panel_host', 403) from None
    origin = request.headers.get('origin')
    if origin and origin != request.url.scheme + '://' + raw_host:
        raise ControlError('cross_origin_panel_request', 403)
    site = request.headers.get('sec-fetch-site')
    if site and site not in ('same-origin', 'none') and not navigation:
        raise ControlError('cross_origin_panel_request', 403)
    if mutation:
        provided = request.headers.get('x-proxy-control', '')
        if not token or not hmac.compare_digest(provided, token):
            raise ControlError('panel_session_expired', 403)
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ControlError('json_required', 415)


async def read_body(request, limit=16384):
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise ControlError('panel_request_too_large', 413)
            body.extend(chunk)
    except ClientDisconnect:
        raise ControlError('panel_client_disconnected', 499) from None
    try:
        value = json.loads(body)
    except ValueError:
        raise ControlError('invalid_json') from None
    if not isinstance(value, dict):
        raise ControlError('invalid_json')
    return value


HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'no-referrer', 'Cross-Origin-Resource-Policy': 'same-origin',
           'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}


def attach_control(app, runtime):
    router = APIRouter()
    assets = runtime.root / 'panel'

    def checked(request, **options):
        local_request(request, runtime.port, token=runtime.token, **options)

    def error(exc):
        return JSONResponse(status_code=exc.status, content={'error_code': exc.code, 'fields': exc.fields}, headers=HEADERS)

    @router.get('/')
    @router.get('/_control')
    @router.get('/_control/')
    async def panel(request: Request):
        try:
            checked(request, navigation=True)
            return FileResponse(assets / 'index.html', media_type='text/html', headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.get('/_control/assets/{name}')
    async def asset(request: Request, name: str):
        try:
            checked(request)
            if name not in ('panel.js', 'panel.css', 'mark.svg'):
                raise ControlError('unknown_panel_asset', 404)
            media = {'panel.js': 'text/javascript', 'panel.css': 'text/css', 'mark.svg': 'image/svg+xml'}[name]
            return FileResponse(assets / name, media_type=media, headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.get('/favicon.ico')
    async def favicon():
        return Response(status_code=204, headers=HEADERS)

    @router.get('/_control/api/session')
    async def session(request: Request):
        try:
            checked(request)
            return JSONResponse({'token': runtime.token, 'version': runtime.version,
                                 'settings_schema': SETTINGS, 'restart_supported': runtime.supervised}, headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.get('/_control/api/status')
    async def status(request: Request):
        try:
            checked(request)
            data = await runtime.diagnostics()
            return JSONResponse({'snapshot': data, 'management': await asyncio.to_thread(runtime.management)}, headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.get('/_control/api/settings')
    async def settings(request: Request):
        try:
            checked(request)
            values, revision = load_settings(runtime.root)
            return JSONResponse({'active': runtime.active_settings, 'saved': {**runtime.active_settings, **values},
                                 'revision': revision, 'schema': SETTINGS,
                                 'restart_required': {**runtime.active_settings, **values} != runtime.active_settings}, headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.get('/_control/api/report')
    async def report(request: Request):
        try:
            checked(request)
            # Reuse the CLI allowlists, keeping bootstrap/settings/URLs out of exports.
            from doctor import sanitize_snapshot
            data = sanitize_snapshot(await runtime.health(), await runtime.diagnostics())
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            return Response(json.dumps(data, indent=2, allow_nan=False), media_type='application/json',
                            headers={**HEADERS, 'Content-Disposition': 'attachment; filename="proxy-report-' + stamp + '.json"'})
        except ControlError as exc:
            return error(exc)

    @router.post('/_control/api/{action:path}')
    async def command(request: Request, action: str):
        try:
            checked(request, mutation=True)
            data = await read_body(request)
            code = 200
            if runtime.draining and action.startswith('updates/'):
                raise ControlError('restart_already_pending', 409)
            if action == 'profile':
                seconds = data.get('duration_s', 60)
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    raise ControlError('invalid_profile_duration')
                try:
                    runtime.timings.profile(seconds)
                except (ValueError, OverflowError):
                    raise ControlError('invalid_profile_duration') from None
                result = runtime.timings.profile_status()
            elif action == 'profile/stop':
                if runtime.timings.always_enabled:
                    raise ControlError('continuous_profile_requires_settings', 409)
                runtime.timings.profile_until = 0.0
                result = runtime.timings.profile_status()
            elif action == 'pause':
                paused = data.get('paused')
                if not isinstance(paused, bool):
                    raise ControlError('invalid_pause')
                runtime.paused = paused
                result = {'paused': paused, 'active_requests': runtime.active}
            elif action == 'settings':
                result = await runtime.save_settings(data.get('changes'), data.get('revision'))
            elif action == 'restart':
                force = data.get('force', False)
                if not isinstance(force, bool):
                    raise ControlError('invalid_restart_mode')
                result = await runtime.request_restart(force)
                code = 202
            elif action == 'restart/cancel':
                result = await runtime.cancel_restart()
            elif action == 'updates/check':
                from panel_updates import check_latest
                async with runtime.updates_lock:
                    result = await asyncio.to_thread(check_latest, runtime.root, runtime.version)
                    runtime.update_info = result
            elif action == 'updates/prepare':
                from panel_updates import prepare_update
                async with runtime.updates_lock:
                    result = await asyncio.to_thread(prepare_update, runtime.root, data.get('commit'))
                    runtime.update_info = {**runtime.update_info, **result}
            elif action == 'updates/install':
                from panel_updates import install_supported
                commit = data.get('commit')
                if (not install_supported(runtime.root) or runtime.update_info.get('state') != 'prepared'
                        or runtime.update_info.get('commit') != commit):
                    raise ControlError('update_not_prepared', 409)
                result = await runtime.request_restart(False, commit=commit)
                code = 202
            else:
                raise ControlError('unknown_panel_action', 404)
            return JSONResponse(result, status_code=code, headers=HEADERS)
        except ControlError as exc:
            return error(exc)

    @router.api_route('/_control/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
    async def unknown(request: Request, path: str):
        return JSONResponse({'error_code': 'unknown_panel_route'}, status_code=404, headers=HEADERS)

    app.include_router(router)
    app.add_middleware(ProxyActivityMiddleware, runtime=runtime)
