"""Bounded timings and request metadata; no bodies, headers or credentials."""
from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections import deque
from contextvars import ContextVar
from concurrent.futures import InvalidStateError
import inspect
from itertools import count
import json
import math
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import threading
import time

STAGES = (
    "client_upload", "payload_inspect", "payload_prepare", "rate_limit_gate", "retry_backoff",
    "upstream_admission", "upstream_connect", "upstream_tls", "upstream_upload",
    "upstream_headers", "upstream_body", "sse_prime", "response_ready", "sse_delivery",
)
BOUNDS = (
    0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
    0.5, 1, 2.5, 5, 10, 20, 45, 90, 300, 600, math.inf,
)
TRACE_STAGES = {
    "connection.connect_tcp": "upstream_connect",
    "connection.start_tls": "upstream_tls",
    "http11.send_request_body": "upstream_upload",
    "http11.receive_response_headers": "upstream_headers",
    "http2.send_request_body": "upstream_upload",
    "http2.receive_response_headers": "upstream_headers",
}

# A context contains only small numeric aggregates and fixed endpoint labels.
# It never retains an ASGI scope, request, headers or payload.
REQUEST_RECORD = ContextVar('proxy_request_record', default=None)
_PROCESS_NONCE = secrets.token_hex(8)
_RECORD_SEQUENCE = count()
REQUEST_STAGES = {'rate_limit_gate': 0, 'retry_backoff': 0,
                  'upstream_admission': 1, 'payload_inspect': 2, 'payload_prepare': 2}


class RequestRecord:
    __slots__ = ('key', 'started', 'wall_time', 'method', 'endpoint', 'status', 'headers_ms',
                 'request_bytes', 'response_bytes', 'complete', 'disconnected', 'error', 'stages', 'history_token')

    def __init__(self, method, path):
        self.started, self.wall_time = time.perf_counter(), time.time()
        self.key = _PROCESS_NONCE + '-' + format(next(_RECORD_SEQUENCE), 'x')
        self.method = method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS') else 'OTHER'
        self.endpoint = 'responses' if path in ('/responses', '/v1/responses') else 'models' if path in ('/models', '/v1/models') else 'other'
        self.status, self.headers_ms = 0, None
        self.request_bytes = self.response_bytes = 0
        self.complete = self.disconnected = False
        self.error = ''
        self.stages = [0.0, 0.0, 0.0]
        self.history_token = None

    def result(self):
        elapsed = max(0, time.perf_counter() - self.started) * 1000
        if self.disconnected and not self.complete:
            outcome = 'cancelled'
        elif self.status == 499:
            outcome = 'cancelled'
        elif self.status >= 400:
            outcome = 'http_error'
        elif self.error:
            outcome = 'interrupted'
        elif not self.complete:
            outcome = 'interrupted'
        else:
            outcome = 'success'
        return (self.key, os.getpid(), self.wall_time, self.method, self.endpoint, self.status, outcome,
                round(elapsed, 3), self.headers_ms, self.request_bytes, self.response_bytes,
                *(round(value * 1000, 3) for value in self.stages), self.error)


def note_stream_error(code):
    record = REQUEST_RECORD.get()
    if record is not None and code in ('upstream_stream_error', 'upstream_response_failed', 'stream_incomplete', 'handler_error'):
        record.error = code


_WINDOWS_MEMORY = None
if os.name == 'nt':
    try:
        import ctypes
        from ctypes import wintypes

        class _MemoryCounters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak_rss', 'rss', 'peak_paged', 'paged',
                                                   'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]

        _get_process = ctypes.windll.kernel32.GetCurrentProcess
        _get_process.restype = wintypes.HANDLE
        _get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        _get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
        _get_info.restype = wintypes.BOOL
        _WINDOWS_MEMORY = (_MemoryCounters, _get_process, _get_info)
    except (OSError, AttributeError):
        pass


def process_memory():
    """Current memory off the request path, with one cached native type binding.

    ctypes caches POINTER types permanently. Defining a new structure class per
    sample would therefore retain classes for the lifetime of the process.
    """
    if os.name == 'nt':
        if _WINDOWS_MEMORY is not None:
            try:
                counter_type, get_process, get_info = _WINDOWS_MEMORY
                counters = counter_type()
                counters.cb = ctypes.sizeof(counters)
                if get_info(get_process(), ctypes.byref(counters), counters.cb):
                    return {'rss_bytes': counters.rss, 'private_bytes': counters.private}
            except (OSError, AttributeError):
                pass
    else:
        try:
            with open('/proc/self/statm', 'r', encoding='ascii') as stream:
                resident = int(stream.read(256).split()[1]) * os.sysconf('SC_PAGE_SIZE')
            return {'rss_bytes': resident, 'private_bytes': None}
        except (OSError, ValueError, IndexError):
            pass
    return {'rss_bytes': None, 'private_bytes': None}


EVENT_CODES = frozenset(('worker_started', 'worker_exit', 'watchdog_unresponsive',
                        'watchdog_restart', 'watchdog_recovered', 'watchdog_suppressed',
                        'auto_profile_started', 'auto_profile_saved', 'manual_recovery',
                        'history_write_failed', 'history_cleared'))


class _HistoryQueue(queue.Queue):
    """Bound queued writes and reserved request completions together.

    A retained start consumes two credits: its queued write and one future
    completion. Other producers cannot consume that completion's credit.
    The queue mutex only protects small deque/counter operations, never IO.
    """
    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.completion_reservations = 0

    def put_bounded(self, item, *, reserve_completion=False, complete_reservation=False):
        with self.not_full:
            if complete_reservation:
                if self.completion_reservations <= 0:
                    raise RuntimeError('missing_history_completion_reservation')
                self.completion_reservations -= 1
            else:
                required = 1 + int(reserve_completion)
                if self._qsize() + self.completion_reservations + required > self.maxsize:
                    raise queue.Full
                self.completion_reservations += int(reserve_completion)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def put_nowait(self, item):
        return self.put_bounded(item)

    def release_completion(self):
        with self.not_full:
            self.completion_reservations -= 1


class HistoryStore:
    """Bounded disk history with a nonblocking, bounded producer queue.

    One background writer batches records. SQLite cache, database pages, WAL,
    reader pages and retained rows all have separate fixed bounds. A slow disk
    drops history records instead of delaying API responses.
    """
    REQUEST_COLUMNS = ('request_key', 'worker_pid', 'started_at', 'method', 'endpoint', 'status', 'outcome', 'duration_ms',
                       'headers_ms', 'request_bytes', 'response_bytes', 'quota_ms', 'admission_ms', 'prepare_ms', 'error_code')
    _REQUEST_SQL = ('INSERT INTO requests (' + ','.join(REQUEST_COLUMNS) + ') VALUES ('
                    + ','.join('?' for _ in REQUEST_COLUMNS) + ') ON CONFLICT(request_key)')
    _START_SQL = _REQUEST_SQL + ' DO NOTHING'
    _FINISH_SQL = _REQUEST_SQL + ' DO UPDATE SET ' + ','.join(
        name + '=excluded.' + name for name in REQUEST_COLUMNS if name != 'request_key')

    def __init__(self, path, *, max_requests=2000, hours=24, enabled=True, queue_size=256):
        self.path = Path(path).resolve()
        self.max_requests = max(100, min(10000, int(max_requests)))
        self.hours = max(1, min(168, int(hours)))
        self.enabled = bool(enabled)
        self.pending = _HistoryQueue(maxsize=max(16, min(256, queue_size)))
        self._completion_token = object()
        self.stopping = threading.Event()
        self.thread = None
        self.dropped = self.write_errors = self.written = 0
        self.available = False
        self.memory = {'rss_bytes': None, 'private_bytes': None}

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._run, name='proxy-history', daemon=True)
            self.thread.start()

    def close(self, timeout=10):
        self.stopping.set()
        if self.thread is not None:
            # Startup and the last bounded batch may outlast three seconds on
            # a slow filesystem. This wait is confined to graceful shutdown.
            self.thread.join(timeout=timeout)
        return self.thread is None or not self.thread.is_alive()

    def submit(self, kind, value, completion=None):
        if not self.enabled or self.stopping.is_set():
            return False
        try:
            self.pending.put_nowait((kind, value, completion))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def request(self, record):
        if record.history_token is self._completion_token:
            record.history_token = None
            if self.stopping.is_set():
                self.pending.release_completion()
                self.dropped += 1
                return False
            # Accepted starts retain completion capacity even if new recording
            # is disabled or unrelated metadata saturates the queue.
            self.pending.put_bounded(('request', record.result(), None), complete_reservation=True)
            return True
        return self.submit('request', record.result())

    def begin_request(self, record):
        if not self.enabled or self.stopping.is_set():
            return False
        if record.history_token is self._completion_token:
            return True
        if record.history_token is not None:
            return False
        value = (record.key, os.getpid(), record.wall_time, record.method, record.endpoint,
                 0, 'active', 0, None, 0, 0, 0, 0, 0, '')
        try:
            self.pending.put_bounded(('start', value, None), reserve_completion=True)
            record.history_token = self._completion_token
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def interrupt_worker(self, worker_pid):
        return self.submit('interrupt', (int(worker_pid), time.time()))

    @staticmethod
    def _ack(completion, success):
        if completion is not None:
            try:
                completion.set_result(success)
            except InvalidStateError:
                pass

    def event(self, code, **values):
        if code not in EVENT_CODES:
            raise ValueError('unknown_history_event')
        # Codes and numeric details only, with a small explicit allowlist.
        allowed = {'worker_pid', 'failures', 'duration_s', 'status', 'requests', 'dropped'}
        detail = {key: value for key, value in values.items() if key in allowed
                  and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
        return self.submit('event', (time.time(), code, json.dumps(detail, separators=(',', ':'))))

    def metric(self, snapshot, management):
        memory = process_memory()
        self.memory = memory
        counts = snapshot.get('counts', {})
        value = (time.time(), os.getpid(), management.get('active_requests', 0),
                 snapshot.get('connection_pool', {}).get('in_use', 0),
                 snapshot.get('rate_limits', {}).get('queued_requests', 0),
                 counts.get('responses_requests', 0), counts.get('upstream_errors', 0),
                 counts.get('rate_limits_caught', 0), memory['rss_bytes'], memory['private_bytes'])
        return self.submit('metric', value)

    def report(self, snapshot, completion=None):
        # Caller supplies the CLI's sanitized allowlist, never a raw diagnostic.
        value = json.dumps(snapshot, separators=(',', ':'), allow_nan=False)
        if len(value.encode('utf-8')) > 65536:
            self.dropped += 1
            return False
        return self.submit('report', (time.time(), value), completion)

    def snapshot(self):
        return {'enabled': self.enabled, 'available': self.available, 'queued_records': self.pending.qsize(),
                'queue_limit': self.pending.maxsize, 'dropped_records': self.dropped,
                'completion_reservations': self.pending.completion_reservations,
                'write_errors': self.write_errors, 'written_records': self.written,
                'max_requests': self.max_requests, 'retention_hours': self.hours,
                'database_limit_bytes': 33554432, 'writer_cache_bytes': 524288, 'memory': self.memory}

    def _connect(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=0.25)
        else:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0.25)
        try:
            db.execute('PRAGMA cache_size=-512' if write else 'PRAGMA cache_size=-256')
            db.execute('PRAGMA mmap_size=0')
            if write:
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=NORMAL')
                db.execute('PRAGMA max_page_count=8192')
                db.execute('PRAGMA journal_size_limit=1048576')
                db.execute('PRAGMA wal_autocheckpoint=256')
        except BaseException:
            # A failed PRAGMA must not leave a Windows file handle or SQLite
            # cache alive until garbage collection; the caller has no db yet.
            db.close()
            raise
        return db

    def _schema(self, db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS history_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO history_meta VALUES ('schema', 2);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL, method TEXT NOT NULL, endpoint TEXT NOT NULL,
                status INTEGER NOT NULL, outcome TEXT NOT NULL, duration_ms REAL NOT NULL, headers_ms REAL,
                request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
                quota_ms REAL NOT NULL, admission_ms REAL NOT NULL, prepare_ms REAL NOT NULL, error_code TEXT NOT NULL,
                request_key TEXT, worker_pid INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at);
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at REAL NOT NULL, worker_pid INTEGER NOT NULL,
                active INTEGER NOT NULL, in_use INTEGER NOT NULL, queued INTEGER NOT NULL,
                requests INTEGER NOT NULL, errors INTEGER NOT NULL, rate_limits INTEGER NOT NULL,
                rss_bytes INTEGER, private_bytes INTEGER);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at REAL NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at REAL NOT NULL, snapshot TEXT NOT NULL);
        ''')
        with db:
            db.execute('BEGIN IMMEDIATE')
            schema = db.execute("SELECT value FROM history_meta WHERE key='schema'").fetchone()
            if schema not in ((1,), (2,)):
                raise sqlite3.DatabaseError('unsupported_history_schema')
            columns = {row[1] for row in db.execute('PRAGMA table_info(requests)')}
            if 'request_key' not in columns:
                db.execute('ALTER TABLE requests ADD COLUMN request_key TEXT')
            if 'worker_pid' not in columns:
                db.execute('ALTER TABLE requests ADD COLUMN worker_pid INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS requests_key ON requests(request_key)')
            db.execute("UPDATE history_meta SET value=2 WHERE key='schema'")

    def _prune(self, db):
        cutoff = time.time() - self.hours * 3600
        for table, timestamp, limit in (('requests', 'started_at', self.max_requests),
                                       ('metrics', 'sampled_at', 1440), ('events', 'occurred_at', 256),
                                       ('reports', 'captured_at', 16)):
            db.execute(f'DELETE FROM {table} WHERE {timestamp} < ?', (cutoff,))
            db.execute(f'DELETE FROM {table} WHERE id <= (SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?)', (limit,))
        db.execute('PRAGMA incremental_vacuum(16)')

    def _run(self):
        db = None
        next_maintenance = 0.0
        try:
            db = self._connect(write=True)
            self._schema(db)
            self.available = True
            while not self.stopping.is_set() or not self.pending.empty():
                batch = []
                try:
                    batch.append(self.pending.get(timeout=0.5))
                except queue.Empty:
                    pass
                if batch:
                    # Coalesce short calls into one commit. Waiting is confined
                    # to this bounded background queue, never the API path.
                    deadline = time.monotonic() + 0.01
                    for _ in range(127):
                        try:
                            batch.append(self.pending.get_nowait())
                        except queue.Empty:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or self.stopping.is_set():
                                break
                            try:
                                batch.append(self.pending.get(timeout=remaining))
                            except queue.Empty:
                                break
                try:
                    with db:
                        for kind, value, completion in batch:
                            if kind in ('start', 'request'):
                                db.execute(self._START_SQL if kind == 'start' else self._FINISH_SQL, value)
                            elif kind == 'interrupt':
                                worker_pid, ended_at = value
                                db.execute("UPDATE requests SET outcome='interrupted', error_code='worker_restarted', duration_ms=MAX(0,(?-started_at)*1000) WHERE outcome='active' AND worker_pid=?", (ended_at, worker_pid))
                            elif kind == 'startup_recovery':
                                db.execute("UPDATE requests SET outcome='interrupted', error_code='worker_restarted', duration_ms=MAX(0,(?-started_at)*1000) WHERE outcome='active'", (value,))
                            elif kind == 'metric':
                                db.execute('INSERT INTO metrics VALUES (NULL,?,?,?,?,?,?,?,?,?,?)', value)
                            elif kind == 'event':
                                db.execute('INSERT INTO events VALUES (NULL,?,?,?)', value)
                            elif kind == 'report':
                                db.execute('INSERT INTO reports VALUES (NULL,?,?)', value)
                            elif kind == 'clear':
                                for table in ('requests', 'metrics', 'events', 'reports'):
                                    db.execute('DELETE FROM ' + table)
                        # Each commit enforces the row limit, even under a sustained flood.
                        if batch or time.monotonic() >= next_maintenance:
                            self._prune(db)
                    self.written += len(batch)
                    for _, _, completion in batch:
                        self._ack(completion, True)
                    if time.monotonic() >= next_maintenance:
                        try:
                            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        except sqlite3.Error:
                            pass
                        next_maintenance = time.monotonic() + 30
                except (sqlite3.Error, OSError):
                    self.write_errors += 1
                    self.dropped += len(batch)
                    for _, _, completion in batch:
                        self._ack(completion, False)
                finally:
                    for _ in batch:
                        self.pending.task_done()
        except (sqlite3.Error, OSError):
            self.write_errors += 1
            self.available = False
        finally:
            if db is not None:
                try:
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except sqlite3.Error:
                    pass
                db.close()

    def _read(self, sql, parameters=()):
        db = None
        try:
            db = self._connect()
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, parameters)]
        except (sqlite3.Error, OSError):
            return []
        finally:
            if db is not None:
                db.close()

    def requests(self, *, before=None, outcome=None, limit=50):
        limit = max(1, min(100, int(limit)))
        clauses, parameters = ['started_at >= ?'], [time.time() - self.hours * 3600]
        if before is not None:
            clauses.append('id < ?')
            parameters.append(int(before))
        if outcome is not None:
            if outcome not in ('active', 'success', 'http_error', 'cancelled', 'interrupted'):
                raise ValueError('invalid_history_filter')
            clauses.append('outcome = ?')
            parameters.append(outcome)
        # Fetch at most one page plus a continuation flag, never all retained requests.
        rows = self._read('SELECT * FROM requests WHERE ' + ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT ?', (*parameters, limit + 1))
        return {'items': rows[:limit], 'next_before': rows[limit - 1]['id'] if len(rows) > limit else None,
                'retention_hours': self.hours, 'max_requests': self.max_requests}

    def metrics(self):
        rows = self._read('SELECT * FROM metrics WHERE sampled_at >= ? ORDER BY id DESC LIMIT 1440',
                          (time.time() - self.hours * 3600,))
        return {'items': rows[::-1], 'retention_hours': self.hours}

    def events(self):
        rows = self._read('SELECT * FROM events WHERE occurred_at >= ? ORDER BY id DESC LIMIT 50',
                          (time.time() - self.hours * 3600,))
        for row in rows:
            row['detail'] = json.loads(row['detail'])
        return {'items': rows}

    def reports(self):
        return {'items': self._read('SELECT id,captured_at FROM reports WHERE captured_at>=? ORDER BY id DESC LIMIT 16',
                                   (time.time() - self.hours * 3600,))}

    def saved_report(self, identifier):
        rows = self._read('SELECT snapshot FROM reports WHERE id=? AND captured_at>=? LIMIT 1',
                          (int(identifier), time.time() - self.hours * 3600))
        return json.loads(rows[0]['snapshot']) if rows else None


class TimingHistogram:
    __slots__ = ("count", "total", "maximum", "active", "failed", "cancelled", "bins")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.active = 0
        self.failed = 0
        self.cancelled = 0
        self.bins = [0] * len(BOUNDS)

    def record(self, elapsed, outcome="ok"):
        elapsed = max(0.0, elapsed)
        self.count += 1
        self.total += elapsed
        self.maximum = max(self.maximum, elapsed)
        self.bins[bisect_left(BOUNDS, elapsed)] += 1
        self.failed += outcome == "error"
        self.cancelled += outcome == "cancelled"

    def snapshot(self):
        def upper_bound(fraction):
            if not self.count:
                return None
            target = math.ceil(self.count * fraction)
            total = 0
            for bound, count in zip(BOUNDS, self.bins):
                total += count
                if total >= target:
                    return round(bound * 1000, 4) if math.isfinite(bound) else None
        return {
            "count": self.count, "active": self.active, "failed": self.failed,
            "cancelled": self.cancelled, "total_ms": round(self.total * 1000, 4),
            "mean_ms": round(self.total * 1000 / self.count, 4) if self.count else None,
            "max_ms": round(self.maximum * 1000, 4),
            "p50_bucket_upper_ms": upper_bound(0.5),
            "p95_bucket_upper_ms": upper_bound(0.95),
        }


class _NoTiming:
    __slots__ = ()

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False


_NO_TIMING = _NoTiming()


class _Measurement:
    __slots__ = ("histogram", "started", "record", "stage")

    def __init__(self, histogram, record=None, stage=None):
        self.histogram = histogram
        self.record, self.stage = record, stage
        if histogram is not None:
            histogram.active += 1
        self.started = time.perf_counter()

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        outcome = "ok" if exc_type is None else ("cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
        elapsed = time.perf_counter() - self.started
        if self.histogram is not None:
            self.histogram.active -= 1
            self.histogram.record(elapsed, outcome)
        if self.record is not None:
            self.record.stages[self.stage] += elapsed
        return False


class RuntimeTimings:
    def __init__(self, enabled=True):
        self.always_enabled = enabled
        self.profile_until = 0.0
        self.stages = {name: TimingHistogram() for name in STAGES}

    @property
    def enabled(self):
        if self.always_enabled:
            return True
        if not self.profile_until:
            return False
        if time.monotonic() < self.profile_until:
            return True
        self.profile_until = 0.0
        return False

    def profile(self, seconds):
        if not math.isfinite(seconds) or not 0 < seconds <= 300:
            raise ValueError("Profile duration must be finite and between 0 and 300 seconds")
        self.profile_until = max(self.profile_until, time.monotonic() + seconds)

    def profile_status(self):
        enabled = self.enabled
        return {
            "timings_enabled": enabled,
            "timing_mode": "always" if self.always_enabled else ("temporary" if enabled else "off"),
            "profile_remaining_s": round(max(0.0, self.profile_until - time.monotonic()), 3) if not self.always_enabled else 0.0,
        }

    def begin(self, stage):
        if not self.enabled:
            return None
        self.stages[stage].active += 1
        return time.perf_counter()

    def finish(self, stage, started, outcome="ok"):
        if started is None:
            return
        histogram = self.stages[stage]
        histogram.active -= 1
        histogram.record(time.perf_counter() - started, outcome)

    def measure(self, stage):
        # The normal fast mode neither allocates a timer/context generator nor
        # installs HTTPcore trace callbacks. Captured intervals finish normally
        # if a temporary profile expires while they are active.
        record = REQUEST_RECORD.get() if stage in REQUEST_STAGES else None
        histogram = self.stages[stage] if self.enabled else None
        return (_Measurement(histogram, record, REQUEST_STAGES.get(stage))
                if histogram is not None or record is not None else _NO_TIMING)

    def snapshot(self):
        return {name: histogram.snapshot() for name, histogram in self.stages.items()}

    def upstream_trace(self, previous=None):
        return _UpstreamTrace(self, previous)



class _UpstreamTrace:
    __slots__ = ("timings", "previous", "started")

    def __init__(self, timings, previous):
        self.timings = timings
        self.previous = previous
        self.started = {}

    async def __call__(self, event, info):
        # HTTPcore's info can contain request headers; it is never read or stored.
        base, _, action = event.rpartition(".")
        stage = TRACE_STAGES.get(base)
        if stage is not None:
            if action == "started":
                self.started[stage] = self.timings.begin(stage)
            elif action in ("complete", "failed") and stage in self.started:
                self.timings.finish(stage, self.started.pop(stage),
                                    "error" if action == "failed" else "ok")
        if self.previous is not None:
            outcome = self.previous(event, info)
            if inspect.isawaitable(outcome):
                await outcome

    def close(self, outcome="cancelled"):
        for stage, started in self.started.items():
            self.timings.finish(stage, started, outcome)
        self.started.clear()
