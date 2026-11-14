r"""
service.py -- Headless background supervisor & auto-updater runner.

Designed to be launched by `pythonw.exe` (Windows GUI subsystem) at user logon
or via `.\start-proxy.ps1 -Background` so NO console window or Windows Terminal
popup is ever created. Child `git`, `pip`, and `uvicorn` processes on Windows
are spawned with `CREATE_NO_WINDOW`, and output is written to a rotating log
file (`logs/proxy.log`).
"""

from __future__ import annotations

import argparse
from collections import deque
import ipaddress
import json
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import signal
import secrets
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0
_CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0


class RotatingFileLogger:
    """Thread-safe size-bounded file logger with single-backup rotation."""

    def __init__(self, log_path: Path, max_kb: int = 1024) -> None:
        self.log_path = log_path
        self.max_bytes = max(64, max_kb) * 1024
        self._lock = threading.Lock()
        self._line_count = 0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self) -> None:
        try:
            if self.log_path.is_file() and self.log_path.stat().st_size > self.max_bytes:
                backup = Path(f"{self.log_path}.1")
                if backup.exists():
                    backup.unlink(missing_ok=True)
                self.log_path.replace(backup)
        except OSError:
            pass

    def write(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}\n"
        with self._lock:
            if self._line_count % 50 == 0:
                self._rotate_if_needed()
            self._line_count += 1
            try:
                with self.log_path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(line)
            except OSError:
                pass

    def write_raw(self, raw_line: str) -> None:
        clean = raw_line.rstrip("\r\n")
        if not clean:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if self._line_count % 50 == 0:
                self._rotate_if_needed()
            self._line_count += 1
            try:
                with self.log_path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{stamp} {clean}\n")
            except OSError:
                pass


def _resolve_worker_python() -> str:
    """Return python.exe (non-windowed via CREATE_NO_WINDOW) to host uvicorn."""
    if sys.platform == "win32":
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
        if venv_py.is_file():
            return str(venv_py)
        sibling = Path(sys.executable).with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    else:
        venv_py = ROOT / ".venv" / "bin" / "python"
        if venv_py.is_file():
            return str(venv_py)
    return sys.executable


def _resolve_service_python() -> str:
    """Return pythonw.exe on Windows so self-restarts remain windowless."""
    if sys.platform == "win32":
        venv_pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if venv_pyw.is_file():
            return str(venv_pyw)
        sibling = Path(sys.executable).with_name("pythonw.exe")
        if sibling.is_file():
            return str(sibling)
    return sys.executable


def _subprocess_kwargs(*, pipe_output: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
    }
    if pipe_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
        kwargs["bufsize"] = 1
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if _CREATE_NO_WINDOW:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return kwargs


def _is_loopback_host(host: str) -> bool:
    cleaned = (host or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if cleaned.lower() in ("", "localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _probe_connect_host(bind_host: str) -> str:
    """Return a local connectable address for probing a bound listener."""
    cleaned = (bind_host or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if cleaned in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if cleaned in ("::", "::0"):
        return "::1"
    return cleaned


def _format_url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _probe_our_health(port: int, host: str = "127.0.0.1", timeout_s: float = 3.0) -> bool:
    connect_host = _probe_connect_host(host)
    url = f"http://{_format_url_host(connect_host)}:{port}/_health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return data.get("status") == "ok" and data.get("listen") == f"{host}:{port}"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _probe_worker_version(port, host, version, worker_pid=None):
    url = f"http://{_format_url_host(_probe_connect_host(host))}:{port}/_health"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=0.5) as response:
            data = json.loads(response.read(32769))
        matching = (data.get('proxy') == 'agentrouter-codex-proxy'
                and data.get('status') == 'ok' and data.get('version') == version
                and data.get('connection_pool_initialized') and data.get('upstream_pool_cleanup_active')
                and data.get('upstream_max_inflight') == 50 and data.get('listen') == f'{host}:{port}')
        if matching and worker_pid is not None:
            with opener.open(url.replace('/_health', '/_diagnostics'), timeout=0.5) as response:
                snapshot = json.loads(response.read(32769))
            matching = snapshot.get('process_id') == worker_pid
        return matching
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _release_verified_worker(port, host):
    """Only admit traffic after a managed update has passed the local probe."""
    base = f"http://{_format_url_host(_probe_connect_host(host))}:{port}/_control/api/"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + 'session', timeout=0.5) as response:
            session = json.loads(response.read(32769))
        request = urllib.request.Request(base + 'pause', data=b'{"paused":false}',
                    headers={'Content-Type': 'application/json', 'X-Proxy-Control': session['token']},
                    method='POST')
        with opener.open(request, timeout=0.5) as response:
            return response.status == 200 and json.loads(response.read(4097)).get('paused') is False
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return False


def _is_port_busy(port: int, host: str = "127.0.0.1") -> bool:
    connect_host = _probe_connect_host(host)
    family = socket.AF_INET6 if ":" in connect_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((connect_host, port)) == 0
    except OSError:
        return False


def _ensure_dependencies(worker_python: str, logger: RotatingFileLogger) -> bool:
    check_cmd = [worker_python, "-c", "import fastapi, uvicorn, httpx, isal.igzip, httptools"]
    dependency_check = subprocess.run(
        check_cmd,
        **_subprocess_kwargs(pipe_output=True),
        timeout=30,
    )
    if dependency_check.returncode == 0:
        return True

    req_path = ROOT / "requirements.txt"
    if not req_path.is_file():
        logger.write("fail ", "missing dependencies (fastapi/uvicorn/httpx/isal/httptools) and requirements.txt not found")
        return False

    logger.write("warn ", "missing dependencies (fastapi/uvicorn/httpx/isal/httptools); installing from requirements.txt")
    cmd = [worker_python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(req_path)]
    proc = subprocess.run(cmd, **_subprocess_kwargs(pipe_output=True), timeout=180)
    if proc.returncode != 0:
        logger.write("fail ", f"pip install failed (exit {proc.returncode}): {(proc.stdout or '')[:300]}")
        return False
    dependency_check = subprocess.run(check_cmd, **_subprocess_kwargs(pipe_output=True), timeout=30)
    if dependency_check.returncode != 0:
        logger.write("fail ", "dependencies still unavailable after installation")
        return False
    logger.write("ok   ", "dependencies installed")
    return True


def _stop_child(proc: Optional[subprocess.Popen[Any]]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass
    except Exception:
        pass


def _wait_for_restart_port(proc, port, host, stop_event, panel, logger, action=None, *, timeout_s=5.0):
    """Wait for our worker and listener to close without losing recovery UI.

    Windows can release a terminated worker's socket after its process handle
    is signalled. A persistent conflict must not shut down the independent
    supervisor or cause us to terminate an unrelated listener.
    """
    def ready():
        return (proc is None or proc.poll() is not None) and not _is_port_busy(port, host)

    deadline = time.monotonic() + timeout_s
    while not stop_event.is_set():
        if ready():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        stop_event.wait(min(0.1, remaining))
    if stop_event.is_set():
        return False

    if action is not None:
        _write_control_action(panel.root, {**action, 'state': 'failed',
                                          'error_code': 'restart_port_occupied'})
    logger.write('fail ', f'worker shutdown or port {port} release is pending; '
                 + ('independent panel remains available' if panel.server is not None
                    else 'no independent panel is available'))
    if panel.server is None:
        return False

    # The old Popen handle is our sole termination target. A browser recovery
    # retries that handle; it never kills a process discovered by port number.
    while not stop_event.is_set():
        if ready():
            logger.write('ok   ', f'port {port} released; resuming worker startup')
            return True
        if panel.recovery_requested.is_set():
            panel.recovery_requested.clear()
            _stop_child(proc)
        stop_event.wait(0.25)
    _stop_child(proc)
    return False


def _respawn_self(args: argparse.Namespace) -> None:
    exe = _resolve_service_python()
    cmd = [
        exe,
        os.environ.get('PROXY_SERVICE_ENTRY', str(ROOT / "service.py")),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--max-log-kb",
        str(args.max_log_kb),
        "--skip-start-update",
    ]
    if args.verbose:
        cmd.append("--verbose")
    if args.no_auto_update:
        cmd.append("--no-auto-update")

    if sys.platform == "win32":
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
        )
    else:
        os.execv(exe, cmd)


def _write_pid_file(pid_path: Path) -> None:
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass


def _remove_pid_file(pid_path: Path) -> None:
    try:
        if pid_path.is_file() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_control_command(root: Path, token: str, worker_pid: int):
    from control import state_dir
    path = state_dir(root) / 'control-command.json'
    try:
        with path.open('rb') as stream:
            body = stream.read(4097)
        path.unlink(missing_ok=True)
        data = json.loads(body) if len(body) <= 4096 else None
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get('action') not in ('restart', 'update')
            or not isinstance(data.get('token'), str) or not hmac.compare_digest(data['token'], token)
            or data.get('worker_pid') != worker_pid or not isinstance(data.get('id'), str)
            or len(data['id']) != 32 or any(character not in '0123456789abcdef' for character in data['id'])):
        return None
    result = {'id': data['id'], 'started_at': datetime.now(timezone.utc).isoformat(), 'action': data['action']}
    if data['action'] == 'update':
        import re
        if not isinstance(data.get('commit'), str) or not re.fullmatch('[0-9a-f]{40}', data['commit']):
            return None
        result['commit'] = data['commit']
    return result


def _write_control_action(root: Path, action: dict):
    try:
        from control import atomic_json, state_dir
        atomic_json(state_dir(root) / 'control-action.json', action)
    except OSError:
        pass


class IndependentPanel:
    """Small stdlib control listener. Never transports Codex/provider traffic.

    A bounded handler pool serves files from disk and relays only allowlisted
    control calls. Its health monitor and history reader survive worker failure.
    """
    READ_ROUTES = frozenset(('settings', 'report'))
    WRITE_ROUTES = frozenset(('pause', 'settings', 'restart', 'restart/cancel', 'profile',
                             'profile/stop', 'updates/check', 'updates/prepare', 'updates/install'))

    def __init__(self, root, worker_port, host, token, context, logger, panel_port=None):
        from control import state_dir, current_settings, load_settings, ControlError
        from telemetry import HistoryStore, RuntimeTimings
        self.root, self.worker_port, self.host = Path(root), worker_port, host
        self.port = panel_port if panel_port is not None else worker_port + 1
        self.supervisor_token, self.context, self.logger = token, context, logger
        self.token = secrets.token_urlsafe(32)
        self.lock, self.action_lock, self.session_lock = threading.Lock(), threading.Lock(), threading.Lock()
        self.worker_token = None
        self.worker_pid = None
        self.worker_started = time.monotonic()
        self.failures = 0
        self.last_seen = None
        self.last_seen_mono = 0.0
        self.responsive = False
        self.status_available = False
        self.restarts = deque(maxlen=3)
        self.next_watchdog = 0.0
        self.stop = threading.Event()
        self.recovery_requested = threading.Event()
        self.server = self.thread = self.monitor = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.base = f'http://{_format_url_host(_probe_connect_host(host))}:{worker_port}/_control/api/'
        self.values = current_settings()
        try:
            self.values.update(load_settings(root)[0])
        except ControlError:
            pass
        self.history = HistoryStore(state_dir(root) / 'request-history.sqlite3',
                                    max_requests=self.values['PROXY_HISTORY_MAX_REQUESTS'],
                                    hours=self.values['PROXY_HISTORY_HOURS'],
                                    enabled=self.values['PROXY_HISTORY_ENABLED'], queue_size=64)
        self.version = 'unknown'
        with (self.root / 'proxy.py').open('r', encoding='utf-8') as stream:
            match = re.search(r'^VERSION\s*=\s*[\"\'](\d+\.\d+\.\d+)[\"\']', stream.read(65536), re.M)
        if match:
            self.version = match.group(1)
        self.cache = {'snapshot': {'proxy': 'agentrouter-codex-proxy', 'schema_version': 1,
            'version': self.version, 'process_id': None, 'status': 'unavailable',
            'uptime_s': 0, 'listen': f'{host}:{worker_port}', 'counts': {},
            'connection_pool': {'in_use': 0, 'max_inflight': 50, 'waiters': 0},
            'rate_limits': {'queued_requests': 0, 'cooldown_s': 0},
            'compression': {'enabled': False}, 'streaming': {}, 'findings': [],
            'timing_mode': 'off', 'profile_remaining_s': 0, 'timings': RuntimeTimings(False).snapshot()},
            'management': {'active_requests': 0, 'paused': False, 'draining': False,
                'restart_supported': True, 'restart_job': None, 'pending_settings': False,
                'updates_supported': False, 'update': {'state': 'not_checked', 'update_available': False}}}

    def start(self):
        if not 1 <= self.port <= 65535 or self.port == self.worker_port:
            return False
        owner = self
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False
            request_queue_size = 8
            slots = threading.BoundedSemaphore(6)
            def process_request(self, request, address):
                if not self.slots.acquire(blocking=False):
                    try:
                        request.settimeout(0.5)
                        request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    except OSError:
                        pass
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except Exception:
                    self.slots.release()
                    raise
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.slots.release()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'
            def log_message(self, *args):
                pass
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def respond(self, status, body, media='application/json', disposition=None):
                from control import HEADERS
                self.send_response(status)
                for key, value in HEADERS.items():
                    self.send_header(key, value)
                self.send_header('Content-Type', media)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                if disposition:
                    self.send_header('Content-Disposition', disposition)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            def dispatch(self):
                from control import ControlError, local_request, SETTINGS
                from starlette.requests import Request
                try:
                    parsed = urlsplit(self.path)
                    if parsed.scheme or parsed.netloc or parsed.fragment:
                        raise ControlError('unknown_panel_route', 404)
                    path = parsed.path
                    navigation = path in ('/', '/_control', '/_control/')
                    scope = {'type': 'http', 'scheme': 'http', 'method': self.command,
                             'path': path, 'query_string': parsed.query.encode('ascii'),
                             'headers': [(key.lower().encode('latin1'), value.encode('latin1')) for key, value in self.headers.raw_items()],
                             'client': self.client_address, 'server': ('127.0.0.1', owner.port)}
                    local_request(Request(scope), owner.port, mutation=self.command == 'POST',
                                  token=owner.token, navigation=navigation)
                    if self.command in ('GET', 'HEAD') and (navigation or path.startswith('/_control/assets/')):
                        name = 'index.html' if navigation else path[len('/_control/assets/'):]
                        media = {'index.html': 'text/html', 'panel.js': 'text/javascript',
                                 'panel.css': 'text/css', 'mark.svg': 'image/svg+xml'}
                        if name not in media:
                            raise ControlError('unknown_panel_asset', 404)
                        asset = owner.root / 'panel' / name
                        from control import HEADERS
                        with asset.open('rb') as stream:
                            self.send_response(200)
                            for key, value in HEADERS.items():
                                self.send_header(key, value)
                            self.send_header('Content-Type', media[name])
                            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            if self.command != 'HEAD':
                                while True:
                                    chunk = stream.read(65536)
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                        return
                    if path == '/favicon.ico' and self.command in ('GET', 'HEAD'):
                        return self.respond(204, b'')
                    if not path.startswith('/_control/api/'):
                        raise ControlError('unknown_panel_route', 404)
                    action = path[len('/_control/api/'):]
                    disposition = None
                    if self.command == 'GET':
                        if action == 'session':
                            value = {'token': owner.token, 'version': owner.version,
                                     'settings_schema': SETTINGS, 'restart_supported': True}
                        elif action == 'status':
                            value = owner.status()
                        elif action.startswith('history/'):
                            value, disposition = owner.read_history(action, parsed.query)
                        elif action in owner.READ_ROUTES:
                            status, body, disposition = owner.relay(action)
                            return self.respond(status, body, disposition=disposition)
                        else:
                            raise ControlError('unknown_panel_route', 404)
                        return self.respond(200, json.dumps(value, allow_nan=False, separators=(',', ':')).encode(), disposition=disposition)
                    if self.command != 'POST':
                        raise ControlError('unknown_panel_route', 404)
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                        raise ControlError('invalid_json', 400)
                    try:
                        length = int(lengths[0])
                        if not 0 <= length <= 16384:
                            raise ControlError('panel_request_too_large', 413)
                        body = self.rfile.read(length)
                        if len(body) != length:
                            raise ValueError()
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise ValueError()
                    except ValueError:
                        raise ControlError('invalid_json', 400) from None
                    if action == 'recover':
                        if data.get('confirm') is not True:
                            raise ControlError('recovery_requires_confirmation')
                        result = owner.recover(manual=True)
                        return self.respond(202, json.dumps(result).encode())
                    if action == 'history/clear':
                        if data.get('confirm') is not True:
                            raise ControlError('history_clear_requires_confirmation')
                        if not owner.history.submit('clear', None):
                            raise ControlError('history_unavailable', 503)
                        return self.respond(202, b'{"accepted":true}')
                    if action not in owner.WRITE_ROUTES:
                        raise ControlError('unknown_panel_action', 404)
                    status, output, disposition = owner.relay(action, body)
                    return self.respond(status, output, disposition=disposition)
                except ControlError as exc:
                    try:
                        self.respond(exc.status, json.dumps({'error_code': exc.code, 'fields': exc.fields}).encode())
                    except OSError:
                        pass
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except (OSError, ValueError, UnicodeError):
                    try:
                        self.respond(503, b'{"error_code":"panel_unavailable"}')
                    except OSError:
                        pass
            do_GET = dispatch
            do_POST = dispatch
            do_HEAD = dispatch
            do_OPTIONS = dispatch
            do_PUT = dispatch
            do_PATCH = dispatch
            do_DELETE = dispatch

        try:
            self.server = Server(('127.0.0.1', self.port), Handler)
        except OSError:
            self.logger.write('warn ', f'independent panel port {self.port} is unavailable; worker control remains available')
            return False
        self.history.start()
        if self.context()[0] is None:
            self.history.submit('startup_recovery', time.time())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.25}, name='proxy-panel', daemon=True)
        self.thread.start()
        self.monitor = threading.Thread(target=self._monitor, name='proxy-watchdog', daemon=True)
        self.monitor.start()
        self.logger.write('ok   ', f'independent browser panel listening on http://127.0.0.1:{self.port}/')
        return True

    def close(self):
        self.stop.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.monitor is not None:
            self.monitor.join(timeout=2)
        self.history.close()

    def status(self):
        from control import safe_action
        with self.lock:
            snapshot = dict(self.cache['snapshot'])
            management = dict(self.cache['management'])
            supervisor = {'independent': True, 'worker_responsive': self.responsive,
                          'status_available': self.status_available,
                          'last_seen_at': self.last_seen, 'stale_seconds': round(max(0, time.monotonic() - self.last_seen_mono), 1) if self.last_seen else None,
                          'failures': self.failures, 'watchdog_enabled': self.values['PROXY_WATCHDOG'],
                          'watchdog_restart_limit': 3, 'panel_port': self.port}
        management['last_action'] = safe_action(self.root)
        management['restart_supported'] = True
        management['history'] = {**self.history.snapshot(), **management.get('history', {})}
        management['independent_panel_url'] = f'http://127.0.0.1:{self.port}/'
        return {'snapshot': snapshot, 'management': management, 'supervisor': supervisor}

    def read_history(self, action, query):
        from control import ControlError
        kind = action[len('history/'):]
        try:
            if kind == 'requests':
                parameters = parse_qs(query, keep_blank_values=True, max_num_fields=3)
                if any(key not in ('before', 'outcome', 'limit') or len(value) != 1 for key, value in parameters.items()):
                    raise ValueError()
                limit = int(parameters.get('limit', ['50'])[0])
                before = int(parameters['before'][0]) if 'before' in parameters else None
                if not 1 <= limit <= 100 or (before is not None and not 1 <= before < 2**63):
                    raise ValueError()
                return self.history.requests(limit=limit, before=before, outcome=parameters.get('outcome', [None])[0]), None
            if kind in ('metrics', 'events', 'reports'):
                return getattr(self.history, kind)(), None
            if kind.startswith('report/'):
                identifier = kind[len('report/'):]
                if not identifier.isdigit() or len(identifier) > 18:
                    raise ValueError()
                report = self.history.saved_report(int(identifier))
                if report is None:
                    raise ControlError('history_report_unavailable', 404)
                return report, 'attachment; filename="proxy-auto-report-' + identifier + '.json"'
            raise ControlError('unknown_history_route', 404)
        except (ValueError, OverflowError):
            raise ControlError('invalid_history_filter') from None

    def relay(self, action, body=None, retry_session=True):
        from control import ControlError
        try:
            headers = {}
            if body is not None:
                with self.session_lock:
                    if self.worker_token is None:
                        with self.opener.open(self.base + 'session', timeout=1) as response:
                            value = json.loads(response.read(65537))
                        token = value.get('token')
                        if not isinstance(token, str) or not 32 <= len(token) <= 128:
                            raise ValueError()
                        self.worker_token = token
                    headers = {'Content-Type': 'application/json', 'X-Proxy-Control': self.worker_token,
                               'Origin': self.base.split('/_control/', 1)[0], 'Sec-Fetch-Site': 'same-origin'}
            request = urllib.request.Request(self.base + action, data=body, headers=headers)
            try:
                response = self.opener.open(request, timeout=90 if action.startswith('updates/') else 3)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                value = response.read(1048577)
                if len(value) > 1048576:
                    raise ValueError()
                # Only JSON from fixed local control routes may leave the relay.
                json.loads(value)
                if response.status == 403:
                    with self.session_lock:
                        self.worker_token = None
                    if body is not None and retry_session:
                        return self.relay(action, body, retry_session=False)
                return response.status, value, response.headers.get('Content-Disposition')
        except (OSError, ValueError, urllib.error.URLError):
            raise ControlError('worker_unavailable', 503) from None

    def recover(self, manual=False):
        from control import ControlError, atomic_json, state_dir
        with self.action_lock:
            proc, updating = self.context()
            if updating:
                raise ControlError('update_busy', 409)
            if proc is None or proc.poll() is not None:
                if manual:
                    self.recovery_requested.set()
                    self.history.event('manual_recovery')
                return {'accepted': True, 'state': 'restarting'}
            path = state_dir(self.root) / 'control-command.json'
            if path.exists():
                raise ControlError('restart_already_pending', 409)
            command = {'action': 'restart', 'token': self.supervisor_token, 'worker_pid': proc.pid,
                       'id': secrets.token_hex(16), 'started_at': datetime.now(timezone.utc).isoformat()}
            atomic_json(path, command)
            self.history.event('manual_recovery' if manual else 'watchdog_restart', worker_pid=proc.pid, failures=self.failures)
            return {'accepted': True, 'state': 'restarting', 'id': command['id']}

    def _monitor(self):
        from control import current_settings, load_settings, ControlError
        while not self.stop.is_set():
            now = time.monotonic()
            proc, updating = self.context()
            pid = proc.pid if proc is not None else None
            if pid != self.worker_pid:
                if self.worker_pid is not None:
                    self.history.interrupt_worker(self.worker_pid)
                with self.lock:
                    self.worker_pid, self.worker_started = pid, now
                    self.worker_token = None
                    self.failures = 0
                    self.responsive = False
                if pid is not None:
                    self.history.event('worker_started', worker_pid=pid)
                try:
                    self.values = {**current_settings(), **load_settings(self.root)[0]}
                    self.history.hours = self.values['PROXY_HISTORY_HOURS']
                    self.history.max_requests = self.values['PROXY_HISTORY_MAX_REQUESTS']
                    self.history.enabled = self.values['PROXY_HISTORY_ENABLED']
                    self.history.start()
                except ControlError:
                    pass
            healthy = False
            if proc is not None and proc.poll() is None:
                try:
                    with self.opener.open(self.base + 'status', timeout=1) as response:
                        raw = response.read(131073)
                    data = json.loads(raw) if len(raw) <= 131072 else None
                    if (not isinstance(data, dict) or data.get('snapshot', {}).get('process_id') != pid
                            or data['snapshot'].get('proxy') != 'agentrouter-codex-proxy'):
                        raise ValueError()
                    with self.lock:
                        self.cache = data
                        self.status_available = True
                        self.responsive = healthy = True
                        self.last_seen = datetime.now(timezone.utc).isoformat()
                        self.last_seen_mono = time.monotonic()
                        previous_failures = self.failures
                        self.failures = 0
                    if previous_failures:
                        self.history.event('watchdog_recovered', failures=previous_failures, worker_pid=pid)
                except (OSError, ValueError, urllib.error.URLError):
                    with self.lock:
                        self.responsive = self.status_available = False
                    # An invalid settings file can make /status fail while
                    # the request loop remains responsive. That is not a hang.
                    try:
                        with self.opener.open(self.base.replace('/_control/api/', '/_health'), timeout=.5) as response:
                            raw = response.read(32769)
                        health = json.loads(raw) if len(raw) <= 32768 else {}
                        healthy = (health.get('proxy') == 'agentrouter-codex-proxy'
                                   and health.get('status') == 'ok' and health.get('listen') == f'{self.host}:{self.worker_port}')
                        if healthy:
                            with self.lock:
                                self.responsive = True
                                self.failures = 0
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
            self._watchdog_tick(healthy, proc, updating, now)
            self.stop.wait(2)

    def _watchdog_tick(self, healthy, proc, updating, now):
        from control import ControlError
        if (healthy or proc is None or proc.poll() is not None or updating
                or now - self.worker_started < 30 or now < self.next_watchdog):
            return
        self.next_watchdog = now + self.values['PROXY_WATCHDOG_INTERVAL_S']
        with self.lock:
            self.failures += 1
        if self.failures == 1:
            self.history.event('watchdog_unresponsive', worker_pid=proc.pid, failures=1)
        if self.values['PROXY_WATCHDOG'] and self.failures >= self.values['PROXY_WATCHDOG_FAILURES']:
            while self.restarts and self.restarts[0] < now - 600:
                self.restarts.popleft()
            if len(self.restarts) < 3:
                try:
                    self.recover()
                    self.restarts.append(now)
                    self.failures = 0
                except (ControlError, OSError):
                    pass
            elif self.failures == self.values['PROXY_WATCHDOG_FAILURES']:
                self.history.event('watchdog_suppressed', worker_pid=proc.pid, failures=self.failures)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless background supervisor for AgentRouter Codex proxy.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 loopback only; pass 0.0.0.0 explicitly for Docker/LAN).",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", "8788")))
    parser.add_argument("--max-log-kb", type=int, default=1024)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-auto-update", action="store_true")
    parser.add_argument("--skip-start-update", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.host = (args.host or "").strip() or "127.0.0.1"
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    # Recovery precedes every import of mutable application code. The machine
    # entrypoint also runs the immutable guard before importing this module.
    import update_guard
    try:
        startup_transaction = (update_guard.recover_interrupted_update(
            ROOT, os.environ.pop('PROXY_UPDATE_HANDOFF', ''))
            if os.environ.get('PROXY_CONTROL_UPDATES') == '1' else None)
    except (OSError, ValueError):
        RotatingFileLogger(ROOT / 'logs/proxy.log').write('fail ', 'managed update recovery failed; refusing worker start')
        return 1
    global updater
    import updater
    updater.load_env_file(ROOT)
    args = _parse_args(argv)
    os.environ["PROXY_PORT"] = str(args.port)
    if args.verbose:
        os.environ["PROXY_VERBOSE"] = "1"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")

    logger = RotatingFileLogger(ROOT / "logs" / "proxy.log", max_kb=args.max_log_kb)
    pid_path = ROOT / "logs" / "service.pid"

    if _is_port_busy(args.port, args.host):
        if _probe_our_health(args.port, args.host):
            logger.write("ok   ", f"proxy already healthy on {args.host}:{args.port} -- exiting cleanly")
            return 0
        logger.write(
            "fail ",
            f"port {args.port} is already occupied (not matching {args.host}:{args.port}) -- leaving it untouched",
        )
        return 2

    if not _is_loopback_host(args.host):
        logger.write(
            "warn ",
            f"non-loopback bind enabled ({args.host}:{args.port}) -- "
            "restrict access with a firewall and avoid exposing a shared fallback API key",
        )

    worker_python = _resolve_worker_python()
    if not _ensure_dependencies(worker_python, logger):
        return 1
    auto_update = (not args.no_auto_update) and updater.is_auto_update_enabled()
    if auto_update and updater.auto_update_on_start() and not args.skip_start_update:
        up_res = updater.apply_update(ROOT, python_exe=worker_python)
        status = up_res.get("status")
        if up_res.get("applied"):
            changed = ", ".join(up_res.get("changed_files", [])) or "files updated"
            logger.write(
                "ok   ",
                f"auto-update applied {(up_res.get('old_commit') or '')[:7]} -> "
                f"{(up_res.get('local_commit') or '')[:7]} ({changed})",
            )
            _respawn_self(args)
            return 0
        if status not in ("up_to_date", "not_git_repo", "no_remote"):
            logger.write("warn ", f"startup auto-update check: {status} ({up_res.get('error') or ''})")

    _write_pid_file(pid_path)

    stop_event = threading.Event()
    restart_for_update = threading.Event()
    child_lock = threading.Lock()
    active_child: dict[str, Optional[subprocess.Popen[Any]]] = {"proc": None}
    supervisor_token = secrets.token_urlsafe(32)
    os.environ['PROXY_SUPERVISOR_TOKEN'] = supervisor_token
    controlled_restart = threading.Event()
    pending_control: dict[str, Any] = {'action': None, 'started': None, 'duration_offset': 0,
                                      'transaction': startup_transaction if startup_transaction and startup_transaction['state'] == 'pending_health' else None}
    if startup_transaction is not None:
        pending_control.update(started=time.monotonic(), action={
            'id': startup_transaction['id'], 'commit': startup_transaction['commit'],
            'action': 'update', 'started_at': startup_transaction.get('action_started_at', startup_transaction['started_at'])},
            duration_offset=startup_transaction.get('elapsed_before_handoff_s', 0))
        try:
            since = datetime.fromisoformat(pending_control['action']['started_at'])
            pending_control['duration_offset'] = max(0, (datetime.now(timezone.utc) - since).total_seconds())
        except (ValueError, TypeError):
            pass
        if startup_transaction['state'] == 'rolled_back':
            pending_control['action'].update(error_code='update_rolled_back',
                                             expected_version=startup_transaction.get('before_version'))

    def panel_context():
        with child_lock:
            return active_child['proc'], pending_control['transaction'] is not None or (
                pending_control['action'] is not None and pending_control['action'].get('action') == 'update')

    try:
        panel_port = int(os.environ.get('PROXY_PANEL_PORT', str(args.port + 1)))
    except ValueError:
        panel_port = args.port + 1
    independent_panel = IndependentPanel(ROOT, args.port, args.host, supervisor_token,
                                         panel_context, logger, panel_port=panel_port)
    independent_panel.start()
    if independent_panel.server is not None:
        os.environ['PROXY_PANEL_PORT'] = str(panel_port)

    def handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()
        with child_lock:
            _stop_child(active_child["proc"])

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                pass

    def update_loop() -> None:
        interval = updater.get_update_interval_s()
        logger.write("info ", f"background auto-updater enabled (interval={interval}s)")
        while not stop_event.wait(interval):
            try:
                res = updater.apply_update(ROOT, python_exe=worker_python)
            except Exception as exc:
                logger.write("warn ", f"auto-updater error: {type(exc).__name__}: {exc}")
                continue
            if res.get("applied"):
                changed = ", ".join(res.get("changed_files", [])) or "files updated"
                logger.write(
                    "ok   ",
                    f"auto-update applied {(res.get('old_commit') or '')[:7]} -> "
                    f"{(res.get('local_commit') or '')[:7]} ({changed}); restarting service",
                )
                restart_for_update.set()
                with child_lock:
                    _stop_child(active_child["proc"])
                return
            st = res.get("status")
            if st not in ("up_to_date", "not_git_repo", "no_remote"):
                logger.write("warn ", f"auto-updater status={st} ({res.get('error') or ''})")

    if auto_update:
        t = threading.Thread(target=update_loop, name="proxy-auto-updater", daemon=True)
        t.start()

    backoff = 5
    max_backoff = 60
    rapid_failures = 0

    try:
        while not stop_event.is_set() and not restart_for_update.is_set():
            started_at = time.monotonic()
            cmd = [
                worker_python,
                "-m",
                "uvicorn",
                "proxy:app",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--timeout-keep-alive",
                "600",
                "--log-level",
                "warning",
                "--no-access-log",
            ]
            logger.write("start", f"starting headless uvicorn on {args.host}:{args.port}")
            worker_env = os.environ.copy()
            worker_env.pop('PROXY_CONTROL_BOOT_PAUSED', None)
            if pending_control['transaction'] is not None:
                worker_env['PROXY_CONTROL_BOOT_PAUSED'] = '1'
            proc = subprocess.Popen(cmd, env=worker_env, **_subprocess_kwargs(pipe_output=True))
            with child_lock:
                active_child["proc"] = proc

            def collect_output(worker):
                if worker.stdout is not None:
                    for line in worker.stdout:
                        logger.write_raw(line)

            output_thread = threading.Thread(target=collect_output, args=(proc,), name='proxy-output', daemon=True)
            output_thread.start()
            try:
                while proc.poll() is None:
                    if stop_event.is_set() or restart_for_update.is_set():
                        break
                    if pending_control['action'] is not None:
                        elapsed = time.monotonic() - pending_control['started']
                        transaction = pending_control['transaction']
                        expected_version = transaction['version'] if transaction else pending_control['action'].get('expected_version')
                        healthy = (_probe_worker_version(args.port, args.host, expected_version, proc.pid)
                                   if expected_version else _probe_our_health(args.port, args.host, timeout_s=0.5))
                        if healthy and transaction is not None:
                            try:
                                update_guard.verify_after(ROOT, transaction)
                                healthy = _release_verified_worker(args.port, args.host)
                            except (OSError, ValueError):
                                healthy = False
                                elapsed = 31
                        if healthy:
                            if transaction is not None:
                                from control import atomic_json, state_dir
                                transaction['state'] = 'verified'
                                transaction.pop('handoff_sha256', None)
                                atomic_json(state_dir(ROOT) / 'update-transaction.json', transaction)
                            action = {**pending_control['action'], 'state': 'rolled_back' if pending_control['action'].get('error_code') == 'update_rolled_back' else 'verified',
                                      'finished_at': datetime.now(timezone.utc).isoformat(),
                                      'duration_s': round(elapsed + pending_control['duration_offset'], 3)}
                            _write_control_action(ROOT, action)
                            pending_control.update(action=None, transaction=None)
                        elif elapsed > 30:
                            if transaction is not None:
                                _stop_child(proc)
                                try:
                                    update_guard.rollback_update(ROOT, transaction)
                                except update_guard.RollbackError:
                                    _write_control_action(ROOT, {**pending_control['action'], 'state': 'failed',
                                                                 'error_code': 'update_rollback_failed'})
                                    return 1
                                pending_control['duration_offset'] += time.monotonic() - pending_control['started']
                                pending_control.update(transaction=None, started=time.monotonic(),
                                                       action={**pending_control['action'], 'error_code': 'update_rolled_back',
                                                               'expected_version': transaction.get('before_version')})
                                controlled_restart.set()
                                break
                            _write_control_action(ROOT, {**pending_control['action'], 'state': 'failed',
                                                         'error_code': 'restart_health_timeout'})
                            pending_control['action'] = None
                    command = _read_control_command(ROOT, supervisor_token, proc.pid)
                    if command is not None:
                        logger.write('info ', 'local panel requested worker restart')
                        _write_control_action(ROOT, {**command, 'state': 'restarting'})
                        pending_control.update(action=command, started=time.monotonic(), transaction=None, duration_offset=0)
                        controlled_restart.set()
                        break
                    stop_event.wait(0.25)
            finally:
                with child_lock:
                    _stop_child(proc)
                    if proc.poll() is None:
                        logger.write('fail ', f'owned worker PID {proc.pid} did not exit after terminate/kill')
                    active_child["proc"] = None
                independent_panel.history.interrupt_worker(proc.pid)
                output_thread.join(timeout=1)

            if stop_event.is_set() or restart_for_update.is_set():
                break

            # A worker can fail before ever responding to health. Roll back
            # immediately instead of waiting through the ordinary crash backoff.
            if pending_control['transaction'] is not None and not controlled_restart.is_set():
                transaction = pending_control['transaction']
                try:
                    update_guard.rollback_update(ROOT, transaction)
                except update_guard.RollbackError:
                    _write_control_action(ROOT, {**pending_control['action'], 'state': 'failed',
                                                 'error_code': 'update_rollback_failed'})
                    return 1
                pending_control['duration_offset'] += time.monotonic() - pending_control['started']
                pending_control.update(transaction=None, started=time.monotonic(),
                                       action={**pending_control['action'], 'error_code': 'update_rolled_back',
                                               'expected_version': transaction.get('before_version')})
                controlled_restart.set()

            if controlled_restart.is_set():
                controlled_restart.clear()
                if not _wait_for_restart_port(proc, args.port, args.host, stop_event,
                                              independent_panel, logger, pending_control['action']):
                    if stop_event.is_set():
                        break
                    return 2
                command = pending_control['action']
                if command.get('action') == 'update' and not command.get('error_code'):
                    transaction = None
                    try:
                        from panel_updates import install_prepared
                        transaction = install_prepared(ROOT, command['commit'], command['id'])
                        transaction['action_started_at'] = command['started_at']
                        transaction['elapsed_before_handoff_s'] = time.monotonic() - pending_control['started']
                        handoff = secrets.token_urlsafe(32)
                        transaction['handoff_sha256'] = hashlib.sha256(handoff.encode()).hexdigest()
                        update_guard.atomic_json(update_guard.state_dir(ROOT) / 'update-transaction.json', transaction)
                        os.environ['PROXY_UPDATE_HANDOFF'] = handoff
                        _remove_pid_file(pid_path)
                        # Start the new supervisor once; it owns the health
                        # check and starts a single, initially paused worker.
                        try:
                            independent_panel.close()
                            _respawn_self(args)
                        except OSError:
                            os.environ.pop('PROXY_UPDATE_HANDOFF', None)
                            update_guard.rollback_update(ROOT, transaction)
                            independent_panel = IndependentPanel(ROOT, args.port, args.host, supervisor_token,
                                                                 panel_context, logger, panel_port=panel_port)
                            independent_panel.start()
                            raise
                        return 0
                    except update_guard.RollbackError:
                        _write_control_action(ROOT, {**command, 'state': 'failed', 'error_code': 'update_rollback_failed'})
                        return 1
                    except Exception as exc:
                        logger.write('fail ', 'local update failed: ' + type(exc).__name__)
                        if transaction is not None and transaction['state'] != 'rolled_back':
                            try:
                                update_guard.rollback_update(ROOT, transaction)
                            except update_guard.RollbackError:
                                _write_control_action(ROOT, {**command, 'state': 'failed', 'error_code': 'update_rollback_failed'})
                                return 1
                        if transaction is not None:
                            pending_control['duration_offset'] += time.monotonic() - pending_control['started']
                            pending_control.update(transaction=None, started=time.monotonic(),
                                                   action={**command, 'error_code': 'update_rolled_back',
                                                           'expected_version': transaction.get('before_version')})
                        else:
                            _write_control_action(ROOT, {**command, 'state': 'failed', 'error_code': 'update_install_failed'})
                            pending_control['action'] = None
                backoff = 5
                rapid_failures = 0
                # Health gets its full startup budget even if listener release
                # took longer; action duration still includes that wait.
                if pending_control['action'] is not None:
                    pending_control['duration_offset'] += time.monotonic() - pending_control['started']
                    pending_control['started'] = time.monotonic()
                continue

            code = proc.returncode if proc.returncode is not None else "unknown"
            ran_for = time.monotonic() - started_at
            logger.write("warn ", f"uvicorn exited with code {code} after {ran_for:.0f}s")
            independent_panel.history.event('worker_exit', worker_pid=proc.pid,
                                             status=proc.returncode or 0, duration_s=ran_for)

            if not _wait_for_restart_port(proc, args.port, args.host, stop_event,
                                          independent_panel, logger):
                if stop_event.is_set():
                    break
                return 2

            if ran_for >= 60.0:
                backoff = 5
                rapid_failures = 0
            else:
                rapid_failures += 1

            if rapid_failures >= 10:
                logger.write("fail ", f"uvicorn failed {rapid_failures} times rapidly -- waiting for browser recovery")
                if independent_panel.server is None:
                    return 1
                while not stop_event.is_set() and not restart_for_update.is_set():
                    if independent_panel.recovery_requested.wait(0.25):
                        independent_panel.recovery_requested.clear()
                        rapid_failures, backoff = 0, 5
                        break
                continue

            logger.write("warn ", f"restarting in {backoff}s (rapid failures: {rapid_failures})")
            if stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, max_backoff)

        if restart_for_update.is_set() and not stop_event.is_set():
            _remove_pid_file(pid_path)
            independent_panel.close()
            _respawn_self(args)
            return 0
        return 0
    finally:
        stop_event.set()
        independent_panel.close()
        with child_lock:
            _stop_child(active_child["proc"])
        _remove_pid_file(pid_path)


if __name__ == "__main__":
    raise SystemExit(main())
