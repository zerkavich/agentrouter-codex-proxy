"""
proxy.py -- Local OpenAI Responses API compatibility proxy for Codex & AgentRouter.

Purpose
-------
When using Codex with AgentRouter, multi-turn sessions often fail with:
"OpenAI Responses bad request: The requested item was created under a different
**** OpenAI resource. Use the same resource that created the item to access it."

This proxy eliminates this issue by:
  1. Intercepting requests to `/v1/responses` (and `/responses`).
  2. Providing resilient DNS resolution with static fallback IPs (8.214.160.125 / 8.214.161.192)
     so local network DNS drops or rate-limits never cause [Errno 11001] getaddrinfo failed.
  3. Reusing persistent HTTP keep-alive connection pools to minimize connection overhead and
     eliminate redundant DNS queries.
  4. Automatic transport-level retries on transient connection or server disconnect errors.
  5. Attempting the original request transparently first to leverage upstream KV cache.
  6. When an upstream resource mismatch or state error occurs (HTTP 400), activating a
     guaranteed recovery phase with a sanitized stateless payload (stripping previous_response_id,
     server-minted item IDs, and un-decryptable reasoning tokens).
  7. Streaming SSE events (`text/event-stream`) directly to Codex once upstream commits.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import gzip
from functools import partial
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import socket
import sys
import threading
import time
import weakref
import zlib
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

# Embedded Python may omit the script directory from sys.path.
_APP_ROOT = str(Path(__file__).resolve().parent)
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
from telemetry import RuntimeTimings, REQUEST_RECORD, note_stream_error, note_attempt
from control import ControlRuntime, attach_control, apply_saved_settings

try:
    from isal import igzip as _native_gzip
except ImportError:
    _native_gzip = None

# ----------------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentrouter-codex-proxy")

# ----------------------------------------------------------------------------
# Resilient DNS Resolver (Fixes [Errno 11001] getaddrinfo failed permanently)
# ----------------------------------------------------------------------------
_ORIG_GETADDRINFO = socket.getaddrinfo

# Pre-seeded fallback IP addresses for upstream hosts
STATIC_DNS_FALLBACKS: Dict[str, List[str]] = {
    "agentrouter.org": ["8.214.160.125", "8.214.161.192"],
    "api.agentrouter.org": ["8.214.160.125", "8.214.161.192"],
}

# Dynamic in-memory DNS cache: updated whenever system DNS succeeds
DYNAMIC_DNS_CACHE: Dict[str, List[tuple]] = {}

def resilient_getaddrinfo(host, port, *args, **kwargs):
    """
    Bulletproof getaddrinfo hook:
    1. Attempts standard OS getaddrinfo first.
    2. On success, updates DYNAMIC_DNS_CACHE.
    3. On failure (socket.gaierror), falls back to DYNAMIC_DNS_CACHE or STATIC_DNS_FALLBACKS.
    """
    if isinstance(host, (bytes, bytearray)):
        host_str = host.decode("utf-8", errors="ignore")
    else:
        host_str = str(host)

    try:
        results = _ORIG_GETADDRINFO(host, port, *args, **kwargs)
        if results and host_str in STATIC_DNS_FALLBACKS:
            DYNAMIC_DNS_CACHE[host_str] = results
        return results
    except socket.gaierror as exc:
        # Check if we have cached results from earlier successful queries
        if host_str in DYNAMIC_DNS_CACHE:
            cached = []
            for item in DYNAMIC_DNS_CACHE[host_str]:
                cached.append((item[0], item[1], item[2], item[3], (item[4][0], port)))
            logger.warning("[DNS Fallback] getaddrinfo failed for %s. Using cached IPs: %s", host_str, [x[4][0] for x in cached])
            return cached

        # Check if we have static fallback IPs
        if host_str in STATIC_DNS_FALLBACKS:
            fallback_ips = STATIC_DNS_FALLBACKS[host_str]
            logger.warning("[DNS Fallback] getaddrinfo failed for %s. Using static fallback IPs: %s", host_str, fallback_ips)
            results = []
            for ip in fallback_ips:
                results.append((socket.AF_INET, socket.SocketKind.SOCK_STREAM, 6, "", (ip, port)))
            return results

        raise

# Install resilient DNS resolver globally
socket.getaddrinfo = resilient_getaddrinfo

# ----------------------------------------------------------------------------
# Resilient TCP Socket Keep-Alive Hook
# Proactively prevents intermediate load balancers (Alibaba ALB) and local NAT
# from closing idle TCP sockets during long reasoning / thinking pauses.
# ----------------------------------------------------------------------------
_ORIG_SOCKET_INIT = socket.socket.__init__

def resilient_socket_init(self, *args, **kwargs):
    _ORIG_SOCKET_INIT(self, *args, **kwargs)
    try:
        if self.family in (socket.AF_INET, socket.AF_INET6) and self.type == socket.SOCK_STREAM:
            self.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):
                # Windows: probe after 15s idle, then every 2s
                self.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 15000, 2000))
            else:
                if hasattr(socket, "TCP_KEEPIDLE"):
                    self.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                if hasattr(socket, "TCP_KEEPINTVL"):
                    self.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                if hasattr(socket, "TCP_KEEPCNT"):
                    self.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    except Exception:
        pass

socket.socket.__init__ = resilient_socket_init

# ----------------------------------------------------------------------------
# Windows Proactor Socket Teardown Guard
# CPython 3.12's _ProactorBasePipeTransport._call_connection_lost calls
# self._sock.shutdown(SHUT_RDWR) without guarding OSError, causing benign
# WinError 10054 tracebacks and skipping close/detach when peers reset early.
# ----------------------------------------------------------------------------
def _install_proactor_teardown_guard() -> None:
    """Guard Windows Proactor socket teardown against OSError during shutdown()."""
    if sys.platform != "win32":
        return
    try:
        import asyncio.proactor_events as _proactor_events
    except ImportError:
        return

    transport_cls = getattr(_proactor_events, "_ProactorBasePipeTransport", None)
    if transport_cls is None or getattr(transport_cls, "_agentrouter_teardown_guarded", False):
        return

    orig_call_connection_lost = transport_cls._call_connection_lost

    def _safe_call_connection_lost(self, exc):
        if getattr(self, "_called_connection_lost", False):
            return
        try:
            orig_call_connection_lost(self, exc)
        except OSError:
            if getattr(self, "_called_connection_lost", False):
                raise
            sock = getattr(self, "_sock", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._sock = None
            server = getattr(self, "_server", None)
            if server is not None:
                server._detach()
                self._server = None
            self._called_connection_lost = True

    transport_cls._call_connection_lost = _safe_call_connection_lost
    transport_cls._agentrouter_teardown_guarded = True

_install_proactor_teardown_guard()

# ----------------------------------------------------------------------------
# Environment & Configuration
# ----------------------------------------------------------------------------
def _load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

_load_env()
apply_saved_settings(Path(__file__).resolve().parent)

UPSTREAM = os.environ.get("AGENTROUTER_UPSTREAM", "https://agentrouter.org").rstrip("/")
DEFAULT_LISTEN_HOST = "127.0.0.1"


def is_loopback_host(host: str) -> bool:
    """Return True when *host* resolves to a loopback interface."""
    cleaned = (host or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if cleaned.lower() in ("", "localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _extract_uvicorn_bind_overrides(
    argv: list[str], default_host: str, default_port: int
) -> tuple[str, int]:
    """Sync LISTEN_HOST/PORT with uvicorn CLI flags so /_health stays accurate."""
    if not argv:
        return default_host, default_port
    exe_name = Path(argv[0]).name.lower()
    if "uvicorn" not in exe_name and "proxy:app" not in argv[1:]:
        return default_host, default_port
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    try:
        known, _ = parser.parse_known_args(argv[1:])
    except SystemExit:
        return default_host, default_port
    host = (known.host or "").strip() or default_host
    port = known.port if 1 <= known.port <= 65535 else default_port
    return host, port


def _parse_proxy_cli_args(
    argv: Optional[list[str]] = None,
    default_host: str = DEFAULT_LISTEN_HOST,
    default_port: Optional[int] = None,
) -> argparse.Namespace:
    """Parse standalone CLI arguments for running `python proxy.py`."""
    resolved_default_port = default_port if default_port is not None else PORT
    parser = argparse.ArgumentParser(
        description="Local OpenAI Responses API compatibility proxy for Codex & AgentRouter."
    )
    parser.add_argument(
        "--host",
        default=default_host,
        help=(
            f"Bind address (default: {default_host} loopback only; pass 0.0.0.0 "
            "explicitly when running inside Docker/Proxmox behind a firewall)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=resolved_default_port,
        help=f"Listen port (default: {resolved_default_port}).",
    )
    args = parser.parse_args(argv)
    args.host = (args.host or "").strip() or default_host
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    return args


LISTEN_HOST = DEFAULT_LISTEN_HOST
PORT = int(os.environ.get("PROXY_PORT", "8788"))
LISTEN_HOST, PORT = _extract_uvicorn_bind_overrides(sys.argv, LISTEN_HOST, PORT)
RETRY_DELAY_MS = int(os.environ.get("PROXY_RETRY_DELAY_MS", "300"))
VERBOSE = os.environ.get("PROXY_VERBOSE", "1") not in ("0", "", "false", "False")
logging.getLogger("httpx").setLevel(logging.INFO if VERBOSE else logging.WARNING)

# Rate-limit / SSE priming configuration
RATE_LIMIT_BASE_DELAY_S = float(os.environ.get("PROXY_RATE_LIMIT_BASE_S", "2.0"))
RATE_LIMIT_MAX_DELAY_S = float(os.environ.get("PROXY_RATE_LIMIT_MAX_DELAY_S", "15.0"))
MAX_RATE_LIMIT_WAIT_S = float(os.environ.get("PROXY_MAX_RATE_LIMIT_WAIT_S", "45.0"))
MAX_TRANSIENT_RETRIES = int(os.environ.get("PROXY_MAX_TRANSIENT_RETRIES", "6"))
RECOVERY_STAGGER_S = float(os.environ.get("PROXY_RECOVERY_STAGGER_S", "0.6"))
SSE_PRIME_FIRST_TIMEOUT_S = float(os.environ.get("PROXY_SSE_PRIME_FIRST_TIMEOUT_S", "45.0"))
SSE_PRIME_CREATED_GRACE_S = float(os.environ.get("PROXY_SSE_PRIME_CREATED_GRACE_S", "3.0"))

CONNECT_TIMEOUT_S = float(os.environ.get("PROXY_CONNECT_TIMEOUT_S", "20.0"))
READ_TIMEOUT_S = float(os.environ.get("PROXY_READ_TIMEOUT_S", "600.0"))
REQUEST_GZIP_ENABLED = os.environ.get("PROXY_REQUEST_GZIP", "0").lower() in ("1", "true", "yes")
REQUEST_GZIP_MIN_BYTES = max(65536, int(os.environ.get("PROXY_REQUEST_GZIP_MIN_BYTES", "262144")))
REQUEST_GZIP_WORKERS = max(1, min(16, int(os.environ.get("PROXY_GZIP_WORKERS", "4"))))
REQUEST_GZIP_MAX_JOBS = max(REQUEST_GZIP_WORKERS, int(os.environ.get("PROXY_GZIP_MAX_JOBS", "16")))
REQUEST_GZIP_REJECT_TTL_S = max(0.0, float(os.environ.get("PROXY_GZIP_REJECT_TTL_S", "60")))
REQUEST_GZIP_CACHE_SIZE = 256
REQUEST_GZIP_UPLOAD_MBIT_S = max(0.0, float(os.environ.get("PROXY_GZIP_UPLOAD_MBIT_S", "0")))
REQUEST_GZIP_COST_MARGIN = max(1.0, float(os.environ.get("PROXY_GZIP_COST_MARGIN", "2")))
GZIP_PROFILE_TTL_S = 120.0
UPLOAD_OBSERVATION_MIN_BYTES = 512 * 1024
UPLOAD_ESTIMATE_MAX_MBIT_S = 1000.0
SSE_CHUNK_MAX_BYTES = 65536
SSE_BUFFER_MAX_BYTES = max(SSE_CHUNK_MAX_BYTES, int(os.environ.get("PROXY_SSE_BUFFER_BYTES", "262144")))
SSE_PRIME_MAX_BYTES = max(SSE_CHUNK_MAX_BYTES, int(os.environ.get("PROXY_SSE_PRIME_BYTES", "262144")))
SSE_PRIME_MAX_FRAMES = max(1, int(os.environ.get("PROXY_SSE_PRIME_FRAMES", "1024")))
LARGE_REQUEST_BODY_BYTES = 1024 * 1024
BODY_BUDGET_BYTES = max(0, int(os.environ.get("PROXY_BODY_BUDGET_BYTES", "268435456")))
BODY_BUDGET_WAIT_S = max(1.0, float(os.environ.get("PROXY_BODY_BUDGET_WAIT_S", "60")))
BODY_PREALLOC_MAX_BYTES = 64 * 1024 * 1024
BODY_GROWTH_STEP_BYTES = 8 * 1024 * 1024
BODY_RESERVATION_FACTOR = 3
RATE_LIMIT_SCOPE = os.environ.get("PROXY_RATE_LIMIT_SCOPE", "credential").lower()
if RATE_LIMIT_SCOPE not in ("credential", "shared"):
    raise ValueError("PROXY_RATE_LIMIT_SCOPE must be credential or shared")
RATE_LIMIT_MAX_PARTITIONS = max(1, int(os.environ.get("PROXY_RATE_LIMIT_PARTITIONS", "256")))
TIMINGS = RuntimeTimings(os.environ.get("PROXY_TIMINGS", "0").lower() not in ("0", "", "false"))
_STARTED_MONOTONIC = time.monotonic()
VERSION = "1.10.0"
_PROFILE_HASH_KEY = os.urandom(32)

CLIENT_TIMEOUT = httpx.Timeout(
    timeout=READ_TIMEOUT_S,
    connect=CONNECT_TIMEOUT_S,
    read=READ_TIMEOUT_S,
    write=60.0,
    pool=30.0,
)

# Match patterns for cross-resource Azure OpenAI error & invalid state references
RESOURCE_MISMATCH_PATTERNS = [
    re.compile(r"created under a different.*OpenAI resource", re.IGNORECASE),
    re.compile(r"Use the same resource that created the item", re.IGNORECASE),
    re.compile(r"requested item was created under a different", re.IGNORECASE),
    re.compile(r"previous_response_id", re.IGNORECASE),
    re.compile(r"Response.*not found", re.IGNORECASE),
    re.compile(r"No response found", re.IGNORECASE),
    re.compile(r"No item found", re.IGNORECASE),
    re.compile(r"item.*not found", re.IGNORECASE),
    re.compile(r"Invalid.*\.id", re.IGNORECASE),
    re.compile(r"Exceeded maximum number of images", re.IGNORECASE),
    re.compile(r"maximum number of images", re.IGNORECASE),
    re.compile(r"too many images", re.IGNORECASE),
]

# Hop-by-hop headers to strip
HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
})

STRIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding", "date"}

RATE_LIMIT_CODES = frozenset({
    "rate_limit_exceeded",
    "tokens_limit_exceeded",
    "requests_limit_exceeded",
    "too_many_requests",
    "token_rate_limit_exceeded",
})

TRANSIENT_ERROR_CODES = RATE_LIMIT_CODES | frozenset({
    "server_error",
    "overloaded",
    "service_unavailable",
    "upstream_error",
    "timeout",
    "internal_error",
})

RATE_LIMIT_TEXT_RE = re.compile(
    r"rate[_\s-]*limit|exceeded.*token|tokens?\s+per\s+min|too many requests|quota.*exceeded",
    re.IGNORECASE,
)

TRANSIENT_CAPACITY_TEXT_RE = re.compile(
    r"overloaded|temporarily unavailable|capacity|high demand|try again in|server had an error|bad gateway|gateway timeout",
    re.IGNORECASE,
)

RETRY_AFTER_TEXT_RE = re.compile(
    r"(?:retry\s+after|try\s+again\s+in|wait)\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|seconds?)",
    re.IGNORECASE,
)

RESET_DURATION_RE = re.compile(r"(?:(\d+)m)?(\d+(?:\.\d+)?)s|(\d+(?:\.\d+)?)ms", re.IGNORECASE)
SSE_DELIM_RE = re.compile(rb"(?:\r\n\r\n|\n\n|\r\r)")
SSE_SETUP_EVENTS = frozenset({"response.created", "response.queued", "response.in_progress", "ping", "keepalive"})

# ----------------------------------------------------------------------------
# Image Capping Configuration & Utilities
# OpenAI Responses API enforces a hard limit of 50 images per request.
# In long sessions with browser/computer use, screenshots accumulate and break sessions.
# ----------------------------------------------------------------------------
MAX_IMAGES_PER_REQUEST = int(os.environ.get("PROXY_MAX_IMAGES", "35"))

def is_image_part(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    t = str(part.get("type", "")).lower()
    return t in ("input_image", "computer_screenshot", "image_url") or "image_url" in part

def prune_excess_images(payload: dict, max_images: int = MAX_IMAGES_PER_REQUEST) -> tuple[dict, int]:
    """
    Finds all image parts across messages, tool calls, and tool outputs.
    If total images > max_images, replaces the oldest images with compact text placeholders,
    preserving the latest max_images and valid schema. Returns the original payload
    when no pruning is needed; never mutates it when images must be replaced.
    """
    raw_input = payload.get("input")
    if not isinstance(raw_input, list):
        return payload, 0

    # Count first without retaining a second list of all image references.
    total_images = 0
    for item in raw_input:
        if not isinstance(item, dict):
            continue

        # Check top-level item
        t = str(item.get("type", "")).lower()
        if t in ("input_image", "computer_screenshot"):
            total_images += 1
            continue

        # Check content and output fields
        for field in ("content", "output"):
            parts = item.get(field)
            if isinstance(parts, list):
                for part in parts:
                    if is_image_part(part):
                        total_images += 1

    if total_images <= max_images:
        return payload, 0

    # Copy only containers that actually change. Images and large tool outputs
    # that survive pruning remain shared, immutable input to the serializer.
    clean_data = dict(payload)
    clean_input = list(raw_input)
    clean_data['input'] = clean_input
    to_prune = total_images - max_images
    remaining = to_prune
    for item_idx, item in enumerate(raw_input):
        if not remaining:
            break
        if not isinstance(item, dict):
            continue
        if str(item.get('type', '')).lower() in ('input_image', 'computer_screenshot'):
            clean_input[item_idx] = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "[Historical screenshot omitted: turn exceeded 50-image limit]"}]
            }
            remaining -= 1
        else:
            copied_item = None
            for field in ('content', 'output'):
                parts = item.get(field)
                if not isinstance(parts, list):
                    continue
                copied_parts = None
                for part_idx, part in enumerate(parts):
                    if not remaining:
                        break
                    if is_image_part(part):
                        if copied_item is None:
                            copied_item = dict(item)
                            clean_input[item_idx] = copied_item
                        if copied_parts is None:
                            copied_parts = list(parts)
                            copied_item[field] = copied_parts
                        copied_parts[part_idx] = {
                            'type': 'input_text',
                            'text': '[Historical screenshot omitted: turn exceeded 50-image limit]'
                        }
                        remaining -= 1

    return clean_data, to_prune

# ----------------------------------------------------------------------------
# Runtime Stats
# ----------------------------------------------------------------------------
STATS = {
    "requests_total": 0,
    "responses_requests": 0,
    "resource_mismatches_caught": 0,
    "stateless_fallbacks_applied": 0,
    "successful_recoveries": 0,
    "transport_retries": 0,
    "pool_timeouts": 0,
    "upstream_capacity_waits": 0,
    "upstream_unstarted_connections_reclaimed": 0,
    "upstream_orphaned_connections_reclaimed": 0,
    "client_disconnects_during_upload": 0,
    "upstream_disconnect_cancellations": 0,
    "gzip_payloads": 0,
    "gzip_input_bytes": 0,
    "gzip_output_bytes": 0,
    "gzip_fallbacks": 0,
    "gzip_cached_rejection_skips": 0,
    "gzip_busy_skips": 0,
    "gzip_cost_skips": 0,
    "gzip_evaluations": 0,
    "gzip_preparation_seconds_total": 0.0,
    "sse_backpressure_events": 0,
    "sse_prime_buffer_commits": 0,
    "rate_limit_overflow_requests": 0,
    "rate_limits_caught": 0,
    "stream_precommit_retries": 0,
    "rate_limit_wait_seconds_total": 0.0,
    "rate_limit_gate_waits": 0,
    "rate_limit_gate_timeouts": 0,
    "rate_limit_cancelled_waiters": 0,
    "rate_limit_stale_successes_ignored": 0,
    "rate_limit_budget_exhaustions": 0,
    "rate_limit_hint_responses": 0,
    "images_pruned_total": 0,
    "start_time": time.time(),
}

# ----------------------------------------------------------------------------
# Shared Rate-Limit Cooldown & Recovery Stagger (Anti-Stampede)
# When concurrent subagents share AgentRouter's upstream TPM pool, a 429 or SSE
# rate_limit_exceeded pauses and staggers launches across all active callers.
# ----------------------------------------------------------------------------
def _new_rate_limit_state() -> Dict[str, Any]:
    return {"cooldown_until": 0.0, "streak": 0, "last_limit_at": 0.0,
            "next_launch_after": 0.0, "active": 0, "generation": 0, "waiters": 0,
            "launch_lock": None, "launch_loop": None}


_RATE_LIMIT_STATE: Dict[str, Any] = _new_rate_limit_state()
_OVERFLOW_RATE_LIMIT_STATE: Dict[str, Any] = _new_rate_limit_state()
_RATE_LIMIT_STATES: OrderedDict[bytes, Dict[str, Any]] = OrderedDict()


def _credential_fingerprint(headers: Mapping[str, str]) -> Optional[bytes]:
    authorization = headers.get("authorization", "").strip()
    api_key = headers.get("api-key", "").strip()
    if not authorization and not api_key:
        return None
    # Retain no credentials in caches; fingerprints are keyed per process.
    return hashlib.blake2b((authorization + "\0" + api_key).encode("utf-8"),
                          key=_PROFILE_HASH_KEY, digest_size=16).digest()


def _acquire_rate_limit_state(headers: Mapping[str, str]) -> Dict[str, Any]:
    key = _credential_fingerprint(headers) if RATE_LIMIT_SCOPE == "credential" else None
    if key is None:
        state = _RATE_LIMIT_STATE
    else:
        state = _RATE_LIMIT_STATES.get(key)
        if state is None:
            if len(_RATE_LIMIT_STATES) >= RATE_LIMIT_MAX_PARTITIONS:
                now = time.monotonic()
                for cached_key, cached in _RATE_LIMIT_STATES.items():
                    if (cached["active"] == 0 and cached["cooldown_until"] <= now
                            and cached["last_limit_at"] + 10.0 <= now
                            and cached["next_launch_after"] <= now):
                        del _RATE_LIMIT_STATES[cached_key]
                        break
            if len(_RATE_LIMIT_STATES) < RATE_LIMIT_MAX_PARTITIONS:
                state = _new_rate_limit_state()
                _RATE_LIMIT_STATES[key] = state
            else:
                # Do not evict gates with active requests or pending cooldowns.
                state = _OVERFLOW_RATE_LIMIT_STATE
                STATS["rate_limit_overflow_requests"] += 1
        elif key in _RATE_LIMIT_STATES:
            _RATE_LIMIT_STATES.move_to_end(key)
    state["active"] += 1
    return state


def _release_rate_limit_state(state: Dict[str, Any]) -> None:
    state["active"] -= 1


def _get_launch_lock(state: Optional[Dict[str, Any]] = None) -> asyncio.Lock:
    state = _RATE_LIMIT_STATE if state is None else state
    loop = asyncio.get_running_loop()
    if state["launch_loop"] is not loop:
        state["launch_loop"] = loop
        state["launch_lock"] = asyncio.Lock()
    return state["launch_lock"]

def reset_rate_limit_state() -> None:
    """Resets in-memory rate-limit cooldown state (primarily for tests)."""
    _RATE_LIMIT_STATE.clear()
    _RATE_LIMIT_STATE.update(_new_rate_limit_state())
    _OVERFLOW_RATE_LIMIT_STATE.clear()
    _OVERFLOW_RATE_LIMIT_STATE.update(_new_rate_limit_state())
    _RATE_LIMIT_STATES.clear()

def _valid_delay_hint(raw: str, multiplier: float = 1.0) -> Optional[float]:
    try:
        value = float(raw.strip()) * multiplier
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, OverflowError):
        return None


def _parse_reset_duration(raw: str) -> Optional[float]:
    raw = raw.strip().lower()
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", raw))
    if not matches or re.sub(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", "", raw).strip():
        return None
    scales = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    value = sum(float(match[1]) * scales[match[2]] for match in matches)
    return value if math.isfinite(value) else None


def _extract_retry_delay_s(
    headers: Optional[Mapping[str, str]],
    text: str,
    streak: int,
) -> Tuple[float, Optional[int]]:
    """Return jittered delay and ceil(hint); the backoff cap never shortens a hint."""
    hint_s: Optional[float] = None
    if headers:
        h = {k.lower(): v for k, v in headers.items()}
        if "retry-after-ms" in h:
            hint_s = _valid_delay_hint(h["retry-after-ms"], 0.001)
        if hint_s is None and "retry-after" in h:
            hint_s = _valid_delay_hint(h["retry-after"])
            if hint_s is None:
                try:
                    dt = parsedate_to_datetime(h["retry-after"].strip())
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    hint_s = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        if hint_s is None:
            resets = []
            for dimension in ("tokens", "requests"):
                parsed = _parse_reset_duration(h.get("x-ratelimit-reset-" + dimension, ""))
                remaining = _valid_delay_hint(h.get("x-ratelimit-remaining-" + dimension, ""))
                if parsed is not None and parsed > 0 and (remaining is None or remaining == 0):
                    resets.append(parsed)
            if resets:
                hint_s = max(resets)

    if hint_s is None and text:
        match = RETRY_AFTER_TEXT_RE.search(text)
        if match:
            hint_s = _valid_delay_hint(match[1], 0.001 if match[2].lower().startswith(("ms", "millisecond")) else 1.0)

    if hint_s is not None:
        delay = max(min(0.05, RATE_LIMIT_MAX_DELAY_S), hint_s)
        # Small explicit hints need small jitter, while the FIFO launch gate
        # still prevents callers sharing a quota from retrying in a burst.
        jitter = min(0.5, max(0.0, RATE_LIMIT_MAX_DELAY_S - delay), delay * 0.1)
        delay += random.uniform(0.0, jitter)
        return delay, max(1, math.ceil(hint_s))

    exp = RATE_LIMIT_BASE_DELAY_S * (2 ** min(max(streak - 1, 0), 3))
    return min(exp + random.uniform(0.2, 0.8), RATE_LIMIT_MAX_DELAY_S), None


def _record_rate_limit_cooldown(delay_s: float, state: Optional[Dict[str, Any]] = None) -> int:
    state = _RATE_LIMIT_STATE if state is None else state
    now = time.monotonic()
    state["generation"] += 1
    state["streak"] = min(int(state["streak"]) + 1, 4)
    state["last_limit_at"] = now
    state["cooldown_until"] = max(float(state["cooldown_until"]), now + delay_s)
    return state["streak"]


def _clear_rate_limit_cooldown(state: Optional[Dict[str, Any]] = None, generation: Optional[int] = None) -> None:
    state = _RATE_LIMIT_STATE if state is None else state
    if generation is not None and generation != state["generation"]:
        STATS["rate_limit_stale_successes_ignored"] += 1
        return
    state["streak"] = 0
    state["cooldown_until"] = 0.0


async def _client_disconnected(request: Request) -> bool:
    listener = getattr(request, '_proxy_disconnect_listener', None)
    if listener is not None:
        if listener.done():
            listener.result()
            return True
        return False
    try:
        return await request.is_disconnected()
    except Exception:
        return False


async def _receive_until_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message['type'] == 'http.disconnect':
            return


@asynccontextmanager
async def _watch_client_disconnect(request: Request):
    # One receive owner covers quota waits, sends and partially read bodies.
    # It avoids Request.is_disconnected's self-cancelling AnyIO scope, which
    # can swallow a simultaneous external Task.cancel on Python 3.10.
    listener = asyncio.create_task(_receive_until_disconnect(request))
    request._proxy_disconnect_listener = listener
    try:
        yield
    finally:
        with anyio.CancelScope(shield=True):
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        del request._proxy_disconnect_listener


async def _sleep_unless_disconnected(delay_s: float, request: Request, *, is_rate_limit: bool = True) -> bool:
    """Count elapsed backoff, including cancellation, rather than scheduled sleep."""
    if delay_s <= 0:
        return True
    started = time.perf_counter()
    end = time.monotonic() + delay_s
    try:
        with TIMINGS.measure("retry_backoff"):
            while True:
                if await _client_disconnected(request):
                    return False
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return True
                await asyncio.sleep(min(remaining, 0.25))
    finally:
        if is_rate_limit:
            STATS["rate_limit_wait_seconds_total"] += time.perf_counter() - started


class _RateLimitGateExpired(Exception):
    """The request cannot spend any more time queued behind upstream limits."""


async def _acquire_launch_lock(lock: asyncio.Lock, request: Request, deadline: Optional[float]) -> bool:
    task = asyncio.create_task(lock.acquire())
    claimed = False
    try:
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else 0.25
            if remaining <= 0:
                raise _RateLimitGateExpired()
            done, _ = await asyncio.wait((task,), timeout=min(0.25, remaining))
            if await _client_disconnected(request):
                return False
            if done:
                task.result()
                claimed = True
                return True
    finally:
        if not claimed:
            # Acquire may finish during disconnect detection or external cancellation.
            if task.done():
                # Reclaim an acquired lock synchronously, so another cancellation
                # cannot interrupt cleanup between observing it and releasing it.
                if not task.cancelled() and task.exception() is None and task.result() is True:
                    lock.release()
            else:
                task.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(task, return_exceptions=True)


async def _wait_for_rate_limit_gate(request: Request, state: Optional[Dict[str, Any]] = None,
                                    *, deadline: Optional[float] = None) -> bool:
    """Fair, cancellable launches. Only a request actually leaving spends a slot."""
    state = _RATE_LIMIT_STATE if state is None else state
    if await _client_disconnected(request):
        return False
    now = time.monotonic()
    recently_limited = state["streak"] > 0 or now - float(state["last_limit_at"]) < 10.0
    if now < state["cooldown_until"] and deadline is not None and state["cooldown_until"] >= deadline:
        STATS["rate_limit_gate_timeouts"] += 1
        raise _RateLimitGateExpired()
    if now >= state["cooldown_until"] and (not recently_limited or RECOVERY_STAGGER_S <= 0):
        return True

    started = time.perf_counter()
    state["waiters"] += 1
    STATS["rate_limit_gate_waits"] += 1
    lock = _get_launch_lock(state)
    acquired = False
    try:
        with TIMINGS.measure("rate_limit_gate"):
            acquired = await _acquire_launch_lock(lock, request, deadline)
            if not acquired:
                STATS["rate_limit_cancelled_waiters"] += 1
                return False
            while True:
                if await _client_disconnected(request):
                    STATS["rate_limit_cancelled_waiters"] += 1
                    return False
                now = time.monotonic()
                recently_limited = state["streak"] > 0 or now - float(state["last_limit_at"]) < 10.0
                launch_at = float(state["cooldown_until"])
                if recently_limited and RECOVERY_STAGGER_S > 0:
                    launch_at = max(launch_at, float(state["next_launch_after"]))
                if deadline is not None and (now >= deadline or launch_at >= deadline):
                    raise _RateLimitGateExpired()
                if now >= launch_at:
                    if recently_limited and RECOVERY_STAGGER_S > 0:
                        state["next_launch_after"] = now + RECOVERY_STAGGER_S
                    return True
                remaining = min(0.25, launch_at - now)
                if deadline is not None:
                    remaining = min(remaining, deadline - now)
                await asyncio.sleep(remaining)
                # Recheck a newer 429 before issuing the launch.
    except _RateLimitGateExpired:
        STATS["rate_limit_gate_timeouts"] += 1
        raise
    except asyncio.CancelledError:
        STATS["rate_limit_cancelled_waiters"] += 1
        raise
    finally:
        if acquired:
            lock.release()
        state["waiters"] -= 1
        STATS["rate_limit_wait_seconds_total"] += time.perf_counter() - started


# ----------------------------------------------------------------------------
# Shared Connection Pool
# ----------------------------------------------------------------------------
_SHARED_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_CAPACITY = weakref.WeakKeyDictionary()


def _assign_without_abandoned_connections(pool, original_assign):
    # Interrupted HTTPcore cleanup can leave an ACTIVE or never-started
    # connection with no owning pool request; neither is idle or closed.
    # Run under HTTPcore's own pool lock, before it assigns the remaining queue.
    busy = [connection for connection in pool._connections
            if not connection.is_idle() and not connection.is_closed()]
    abandoned = []
    if busy:
        assigned = {request.connection for request in pool._requests}
        abandoned = [connection for connection in busy if connection not in assigned]
        for connection in abandoned:
            pool._connections.remove(connection)
        STATS["upstream_unstarted_connections_reclaimed"] += sum(
            getattr(connection, "_connection", False) is None for connection in abandoned)
        STATS["upstream_orphaned_connections_reclaimed"] += len(abandoned)
    # The caller closes these using HTTPcore's normal cancellation shield.
    # Established keep-alive connections and owned TCP/TLS connects are retained.
    return abandoned + original_assign()


def _install_upstream_pool_cleanup(pool) -> None:
    if pool is None or getattr(pool, "_proxy_unstarted_cleanup", False):
        return
    if not all(hasattr(pool, name) for name in ("_connections", "_requests", "_assign_requests_to_connections")):
        return
    original_assign = pool._assign_requests_to_connections
    pool._assign_requests_to_connections = partial(_assign_without_abandoned_connections, pool, original_assign)
    pool._proxy_unstarted_cleanup = True


class _UpstreamCapacity:
    def __init__(self, limit: int):
        self.limit = limit
        self.loop = asyncio.get_running_loop()
        self.semaphore = asyncio.Semaphore(limit)
        self.in_use = 0
        self.waiters = 0


class _UpstreamLease:
    def __init__(self, capacity: _UpstreamCapacity):
        self.capacity = capacity
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.capacity.in_use -= 1
            self.capacity.semaphore.release()


class _CapacityStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, lease: _UpstreamLease):
        self.stream = stream
        self.lease = lease

    def __aiter__(self) -> AsyncIterator[bytes]:
        # Delegate the iterator directly; token chunks gain no additional yield.
        return self.stream.__aiter__()

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            self.lease.release()


def _get_upstream_capacity(client: httpx.AsyncClient) -> _UpstreamCapacity:
    capacity = _CLIENT_CAPACITY.get(client)
    if capacity is None or capacity.loop is not asyncio.get_running_loop():
        pool = getattr(getattr(client, "_transport", None), "_pool", None)
        _install_upstream_pool_cleanup(pool)
        limit = max(1, int(getattr(pool, "_max_connections", 50) or 50))
        capacity = _UpstreamCapacity(limit)
        _CLIENT_CAPACITY[client] = capacity
    return capacity


async def _send_with_capacity(client: httpx.AsyncClient, request: httpx.Request, *, stream: bool) -> httpx.Response:
    # Keep cancellable waiters outside HTTPcore. Cancelling a queued HTTPcore
    # request can otherwise leave an unused CONNECTING entry occupying its pool.
    capacity = _get_upstream_capacity(client)
    waiting = capacity.semaphore.locked()
    if waiting:
        capacity.waiters += 1
        STATS["upstream_capacity_waits"] += 1
    try:
        with TIMINGS.measure("upstream_admission"):
            timeouts = getattr(request, "extensions", {}).get("timeout", {})
            pool_timeout = timeouts.get("pool", CLIENT_TIMEOUT.pool)
            if waiting and pool_timeout is not None:
                try:
                    with anyio.fail_after(pool_timeout):
                        await capacity.semaphore.acquire()
                except TimeoutError:
                    raise httpx.PoolTimeout("Timed out waiting for an available upstream connection", request=request) from None
            else:
                await capacity.semaphore.acquire()
    finally:
        if waiting:
            capacity.waiters -= 1
    capacity.in_use += 1
    lease = _UpstreamLease(capacity)
    try:
        response = await client.send(request, stream=stream)
        if response.is_closed:
            lease.release()
        else:
            # Headers alone do not free capacity: SSE and incomplete JSON bodies
            # retain the lease until their underlying response really closes.
            response.stream = _CapacityStream(response.stream, lease)
        return response
    except BaseException:
        lease.release()
        raise

def get_shared_client() -> httpx.AsyncClient:
    """Returns a shared persistent AsyncClient with connection pooling."""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=600.0)
        _SHARED_CLIENT = httpx.AsyncClient(timeout=CLIENT_TIMEOUT, limits=limits, follow_redirects=False)
        _install_upstream_pool_cleanup(getattr(_SHARED_CLIENT._transport, "_pool", None))
    return _SHARED_CLIENT

# ----------------------------------------------------------------------------
# Payload Sanitization (The Bulletproof Fix)
# ----------------------------------------------------------------------------
def is_resource_mismatch_text(text: str) -> bool:
    """Checks if error text matches cross-resource item mismatch or image limit patterns."""
    return any(pat.search(text) for pat in RESOURCE_MISMATCH_PATTERNS)

def is_resource_mismatch_error(status_code: int, body_text: str) -> bool:
    """Checks if an HTTP error response is due to cross-resource item mismatch or image limit."""
    return status_code == 400 and is_resource_mismatch_text(body_text)

def _is_rate_limit_error(code: str, message: str) -> bool:
    c = (code or "").strip().lower()
    return c in RATE_LIMIT_CODES or bool(message and RATE_LIMIT_TEXT_RE.search(message))

def _is_transient_error(code: str, message: str) -> bool:
    c = (code or "").strip().lower()
    return c in TRANSIENT_ERROR_CODES or bool(
        message and (RATE_LIMIT_TEXT_RE.search(message) or TRANSIENT_CAPACITY_TEXT_RE.search(message))
    )

def sanitize_responses_payload(payload: dict) -> dict:
    """
    Transforms a stateful Responses API payload into a self-contained, stateless
    payload that ANY upstream Azure OpenAI / OpenAI resource can process.
    Also prunes accumulated images to safely fit under the 50-image ceiling.
    """
    clean_data = dict(payload)

    # 1. Remove all server-side session pointers
    for state_key in ("previous_response_id", "response_id", "parent_id", "parent_response_id"):
        if state_key in clean_data:
            logger.info("[Sanitizer] Stripped '%s' (%s)", state_key, clean_data[state_key])
            del clean_data[state_key]

    # 2. Sanitize input items
    raw_input = clean_data.get("input")
    if isinstance(raw_input, list):
        sanitized_input = []
        for item in raw_input:
            if not isinstance(item, dict):
                sanitized_input.append(item)
                continue

            # Drop reasoning blocks and item references completely.
            # Past turn reasoning (CoT) is server-minted, contains encrypted tokens,
            # and is rejected as stateless input by foreign Azure resources.
            t = item.get("type")
            if t in ("reasoning", "item_reference"):
                logger.info("[Sanitizer] Dropped server-state item of type '%s'", t)
                continue

            cleaned_item = dict(item)

            # Strip server-minted item ID so the receiving resource mints its own fresh ID
            # Note: call_id is preserved for tool call / tool output correlation
            cleaned_item.pop("id", None)
            cleaned_item.pop("status", None)
            cleaned_item.pop("internal_chat_message_metadata_passthrough", None)

            # Clean any encrypted_content inside content and output parts
            for field in ("content", "output"):
                parts = cleaned_item.get(field)
                if isinstance(parts, list):
                    new_parts = []
                    for part in parts:
                        if isinstance(part, dict):
                            if part.get("type") == "encrypted_content" or "encrypted_content" in part:
                                continue
                            if part.get("type") == "summary_text":
                                continue
                            new_parts.append(part)
                        else:
                            new_parts.append(part)
                    cleaned_item[field] = new_parts

            sanitized_input.append(cleaned_item)

        clean_data["input"] = sanitized_input

    # 3. Prune historical images to well below the 50-image hard limit
    clean_data, pruned_count = prune_excess_images(clean_data, max_images=MAX_IMAGES_PER_REQUEST)
    if pruned_count > 0:
        STATS["images_pruned_total"] += pruned_count
        logger.warning(
            "[Sanitizer] Pruned %d historical images (capped at %d) to guarantee <= 50 images",
            pruned_count, MAX_IMAGES_PER_REQUEST
        )

    return clean_data

# ----------------------------------------------------------------------------
# Transport Helper with Automatic Network Reconnects
# ----------------------------------------------------------------------------
def describe_upstream_error(exc: Exception) -> str:
    """Include the error class even when HTTPX supplies an empty message."""
    detail = str(exc).strip()
    if isinstance(exc, httpx.PoolTimeout) and not detail:
        detail = "Timed out waiting for an available upstream connection"
    detail = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", detail)
    detail = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", detail)
    return type(exc).__name__ + (": " + detail[:500] if detail else "")


class _ReleasingUploadStream(httpx.AsyncByteStream):
    """Let HTTPcore retain upload metadata without retaining a large payload."""

    def __init__(self, content: bytes):
        self._content: Optional[bytes] = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._content is None:
            raise httpx.StreamConsumed()
        try:
            yield self._content
        finally:
            self.release()

    def release(self) -> None:
        self._content = None

    async def aclose(self) -> None:
        self.release()


class _UploadTrace:
    """Measure successful socket-body writes, without retaining trace metadata."""
    __slots__ = ("previous", "started", "protocol", "elapsed", "header_wait")

    def __init__(self, previous=None):
        self.previous = previous
        self.started = self.protocol = self.elapsed = self.header_wait = None

    async def __call__(self, event: str, info: dict) -> None:
        if event in ("http11.send_request_body.started", "http2.send_request_body.started"):
            self.protocol = event.split(".", 1)[0]
            self.started = time.perf_counter()
            self.elapsed = self.header_wait = None
        elif self.protocol is not None and event == self.protocol + ".send_request_body.complete":
            completed = time.perf_counter()
            self.elapsed = completed - self.started
            self.started = completed
        elif self.elapsed is not None and self.protocol is not None and event == self.protocol + ".receive_response_headers.complete":
            self.header_wait = time.perf_counter() - self.started
            self.started = self.protocol = None
        elif event in ("http11.send_request_body.failed", "http2.send_request_body.failed"):
            self.started = self.protocol = self.elapsed = self.header_wait = None
        if self.previous is not None:
            result = self.previous(event, info)
            if hasattr(result, "__await__"):
                await result

    def close(self) -> None:
        self.previous = self.started = self.protocol = None


async def _send_with_transport_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    content: Optional[bytes] = None,
    params: Optional[dict] = None,
    stream: bool = False,
    max_retries: int = 5,
) -> httpx.Response:
    """
    Sends an HTTP request with dedicated retries on connection, DNS, and transport errors.
    This guarantees that momentary WiFi drops, DNS timeouts, or server disconnects
    do not kill the session or exhaust application recovery attempts.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        upload_stream = None
        attempt_started = time.perf_counter()
        try:
            request_headers = headers
            if content is not None and len(content) >= LARGE_REQUEST_BODY_BYTES:
                # Request(content=bytes) caches the body, and HTTPcore keeps its
                # upload stream until the response closes. Keep replay bytes in
                # the caller instead and release this attempt's stream on upload.
                upload_stream = _ReleasingUploadStream(content)
                request_headers = httpx.Headers(headers)
                request_headers["content-length"] = str(len(content))
            req = client.build_request(
                method=method,
                url=url,
                headers=request_headers,
                params=params,
                content=upload_stream if upload_stream is not None else content,
            )
            trace = TIMINGS.upstream_trace(req.extensions.get("trace")) if TIMINGS.enabled else None
            upload_trace = (_UploadTrace(trace or req.extensions.get("trace"))
                            if REQUEST_GZIP_ENABLED and REQUEST_GZIP_UPLOAD_MBIT_S <= 0
                            and content is not None and len(content) >= UPLOAD_OBSERVATION_MIN_BYTES
                            and not headers.get("content-encoding") else None)
            if upload_trace is not None or trace is not None:
                req.extensions["trace"] = upload_trace if upload_trace is not None else trace
            try:
                response = await _send_with_capacity(client, req, stream=stream)
                note_attempt('upstream', status=response.status_code,
                             duration_s=time.perf_counter() - attempt_started)
                if upload_trace is not None and upload_trace.elapsed is not None:
                    _record_upload_observation(content, headers, upload_trace.elapsed, response,
                                               header_wait_s=upload_trace.header_wait)
                return response
            finally:
                if upload_trace is not None:
                    upload_trace.close()
                if trace is not None:
                    trace.close()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
            last_exc = exc
            STATS["transport_retries"] += 1
            backoff_s = min(attempt * 0.75, 4.0)
            note_attempt('transport_connect' if isinstance(exc, httpx.ConnectError) else
                         'transport_protocol' if isinstance(exc, httpx.RemoteProtocolError) else 'transport_timeout',
                         duration_s=time.perf_counter() - attempt_started,
                         wait_s=backoff_s if attempt < max_retries else 0)
            logger.warning(
                "[Transport Retry %d/%d] %s: %s. Reconnecting in %.1fs...",
                attempt, max_retries, type(exc).__name__, str(exc), backoff_s
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff_s)
            else:
                raise
        except Exception as exc:
            last_exc = exc
            STATS["transport_retries"] += 1
            if isinstance(exc, httpx.PoolTimeout):
                STATS["pool_timeouts"] += 1
            backoff_s = min(attempt * 0.75, 4.0)
            note_attempt('transport_pool' if isinstance(exc, httpx.PoolTimeout) else 'transport_other',
                         duration_s=time.perf_counter() - attempt_started,
                         wait_s=backoff_s if attempt < max_retries else 0)
            logger.error("[Transport Error %d/%d] %s. Retrying in %.1fs...", attempt, max_retries, describe_upstream_error(exc), backoff_s)
            if attempt < max_retries:
                await asyncio.sleep(backoff_s)
            else:
                raise
        finally:
            if upload_stream is not None:
                # Also covers admission cancellation and incomplete uploads.
                upload_stream.release()
    raise last_exc

# ----------------------------------------------------------------------------
# FastAPI Application
# ----------------------------------------------------------------------------
@asynccontextmanager
async def _app_lifespan(_: FastAPI):
    if not is_loopback_host(LISTEN_HOST):
        logger.warning(
            "Non-loopback bind enabled (%s:%s) -- restrict access with a firewall "
            "and avoid exposing a shared fallback API key",
            LISTEN_HOST,
            PORT,
        )
    try:
        # Initialize the async backend, TLS contexts, and pool before user traffic.
        # Authentication remains per request; this does not contact upstream.
        with anyio.CancelScope():
            get_shared_client()
            if REQUEST_GZIP_ENABLED:
                _get_gzip_pool()
        await CONTROL.start()
        yield
    finally:
        global _SHARED_CLIENT, _GZIP_POOL
        await CONTROL.close()
        if _GZIP_POOL is not None:
            pool = _GZIP_POOL
            _GZIP_POOL = None
            pool.shutdown()
        if _SHARED_CLIENT and not _SHARED_CLIENT.is_closed:
            with anyio.CancelScope(shield=True):
                await _SHARED_CLIENT.aclose()


app = FastAPI(
    title="agentrouter-codex-proxy",
    description="Local compatibility proxy fixing OpenAI Responses cross-resource errors for Codex",
    version=VERSION,
    lifespan=_app_lifespan,
)

def _filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Filters client headers for proxy forwarding and injects fallback auth if needed."""
    out = {}
    has_auth = False
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "authorization":
            has_auth = True
        if k_lower not in HOP_BY_HOP:
            out[k] = v

    if not has_auth:
        fallback_key = os.environ.get("CODEX_GATEWAY_API_KEY") or os.environ.get("AGENTROUTER_API_KEY")
        if fallback_key:
            out["authorization"] = f"Bearer {fallback_key}"

    # Ensure a compatible User-Agent for AgentRouter if missing or generic
    ua = out.get("user-agent") or out.get("User-Agent")
    if not ua or "python" in ua.lower() or "curl" in ua.lower():
        out["user-agent"] = "Codex Desktop"

    return out

@app.get("/_health")
async def health():
    uptime_s = round(time.monotonic() - _STARTED_MONOTONIC, 1)
    capacity = _CLIENT_CAPACITY.get(_SHARED_CLIENT) if _SHARED_CLIENT is not None else None
    return {
        "status": "ok",
        "proxy": "agentrouter-codex-proxy",
        "version": VERSION,
        "timings_enabled": TIMINGS.enabled,
        "diagnostics_schema_version": 1,
        "on_demand_profiling_supported": True,
        "dns_fallback_active": True,
        "max_images_per_request": MAX_IMAGES_PER_REQUEST,
        "request_gzip_enabled": REQUEST_GZIP_ENABLED,
        "request_gzip_min_bytes": REQUEST_GZIP_MIN_BYTES,
        "body_memory": _body_budget().snapshot(),
        "request_gzip_backend": "isal" if _native_gzip is not None else "stdlib",
        "gzip_workers": REQUEST_GZIP_WORKERS,
        "gzip_max_jobs": REQUEST_GZIP_MAX_JOBS,
        "gzip_jobs_inflight": _GZIP_POOL.inflight if _GZIP_POOL is not None else 0,
        "gzip_rejection_cache_entries": len(_GZIP_REJECTIONS),
        "connection_pool_initialized": _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed,
        "upstream_max_inflight": capacity.limit if capacity is not None else 50,
        "upstream_slots_in_use": capacity.in_use if capacity is not None else 0,
        "upstream_slot_waiters": capacity.waiters if capacity is not None else 0,
        "upstream_pool_cleanup_active": bool(getattr(getattr(getattr(_SHARED_CLIENT, "_transport", None), "_pool", None),
                                                     "_proxy_unstarted_cleanup", False)),
        "sse_buffer_max_bytes": SSE_BUFFER_MAX_BYTES,
        "sse_prime_max_bytes": SSE_PRIME_MAX_BYTES,
        "sse_prime_max_frames": SSE_PRIME_MAX_FRAMES,
        "rate_limit_scope": RATE_LIMIT_SCOPE,
        "rate_limit_partitions": len(_RATE_LIMIT_STATES),
        "listen": f"{LISTEN_HOST}:{PORT}",
        "uptime_seconds": uptime_s,
        "upstream": UPSTREAM,
        "stats": STATS,
    }

@app.get("/_stats")
async def stats():
    return STATS


@app.get("/_diagnostics")
async def diagnostics():
    """Read-only, bounded runtime snapshot without upstream URLs or key hashes."""
    now = time.monotonic()
    states = (_RATE_LIMIT_STATE, _OVERFLOW_RATE_LIMIT_STATE, *_RATE_LIMIT_STATES.values())
    cooldowns = [max(0.0, float(state["cooldown_until"]) - now) for state in states]
    cooldown_count = sum(value > 0 for value in cooldowns)
    recent_gzip_rejections = sum(deadline > now for deadline in _GZIP_REJECTIONS.values())
    queued = sum(state["waiters"] for state in states)
    capacity = _CLIENT_CAPACITY.get(_SHARED_CLIENT) if _SHARED_CLIENT is not None else None
    pool = getattr(getattr(_SHARED_CLIENT, "_transport", None), "_pool", None)
    connections = getattr(pool, "_connections", ())
    counts = {name: value for name, value in STATS.items() if name != "start_time"}
    timing_snapshot = TIMINGS.snapshot()
    findings = []
    if cooldown_count:
        findings.append({
            "code": "upstream_rate_limit", "kind": "current",
            "message": "Upstream cooldown is active. Callers sharing that quota are waiting.",
        })
    if capacity is not None and capacity.waiters:
        findings.append({
            "code": "upstream_capacity_wait", "kind": "current",
            "message": "Requests are waiting for the existing upstream connection limit.",
        })
    if timing_snapshot["upstream_headers"]["active"]:
        findings.append({
            "code": "upstream_headers_wait", "kind": "current",
            "message": "Some upstream attempts have not sent response headers yet.",
        })
    if counts["pool_timeouts"]:
        findings.append({
            "code": "pool_timeouts_observed", "kind": "since_start",
            "message": "Pool timeouts occurred since startup; this does not establish a current leak.",
        })
    if counts["rate_limit_overflow_requests"]:
        findings.append({
            "code": "credential_partition_overflow", "kind": "since_start",
            "message": "Credential partition capacity was exceeded; overflow callers shared a gate.",
        })
    if recent_gzip_rejections:
        findings.append({
            "code": "gzip_rejection_cached", "kind": "current",
            "message": "Recent HTTP 415 responses temporarily disabled proxy-added gzip for those callers.",
        })
    return {
        "schema_version": 1, "proxy": "agentrouter-codex-proxy", "version": VERSION,
        "process_id": os.getpid(), "uptime_seconds": round(now - _STARTED_MONOTONIC, 3),
        "status": "rate_limited" if cooldown_count else ("busy" if queued or (capacity and capacity.waiters) else "ready"),
        **TIMINGS.profile_status(),
        "connection_pool": {
            "max_inflight": capacity.limit if capacity is not None else 50,
            "in_use": capacity.in_use if capacity is not None else 0,
            "waiters": capacity.waiters if capacity is not None else 0,
            "connections": len(connections),
            "idle_connections": sum(connection.is_idle() for connection in connections),
            "cleanup_active": bool(getattr(pool, "_proxy_unstarted_cleanup", False)),
        },
        "rate_limits": {
            "scope": RATE_LIMIT_SCOPE, "partitions": len(_RATE_LIMIT_STATES),
            "max_partitions": RATE_LIMIT_MAX_PARTITIONS, "cooling_partitions": cooldown_count,
            "queued_requests": queued, "longest_cooldown_remaining_s": round(max(cooldowns), 4),
            "request_wait_budget_s": MAX_RATE_LIMIT_WAIT_S,
            "wait_seconds_total": round(STATS["rate_limit_wait_seconds_total"], 6),
            "wait_seconds_semantics": "elapsed_gate_and_rate_limit_backoff",
        },
        "compression": {
            "enabled": REQUEST_GZIP_ENABLED, "backend": "isal" if _native_gzip is not None else "stdlib",
            "workers": REQUEST_GZIP_WORKERS, "max_jobs": REQUEST_GZIP_MAX_JOBS,
            "jobs_inflight": _GZIP_POOL.inflight if _GZIP_POOL is not None else 0,
            "rejection_cache_entries": recent_gzip_rejections,
        },
        "counts": counts, "timings": timing_snapshot, "findings": findings,
        "body_memory": _body_budget().snapshot(),
    }

@app.post("/_diagnostics/profile")
async def start_diagnostic_profile(request: Request):
    """Enable bounded temporary profiling from a local non-browser tool."""
    if request.client is None or not is_loopback_host(request.client.host) or request.headers.get("origin"):
        return JSONResponse(status_code=403, content={"error": "Local diagnostic tool required"})
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024:
                return JSONResponse(status_code=413, content={"error": "Profile request is too large"})
            body.extend(chunk)
    except ClientDisconnect:
        return Response(status_code=499)
    try:
        options = json.loads(body)
        seconds = options.get("duration_s", 30) if isinstance(options, dict) else None
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= 300):
            raise ValueError()
    except (ValueError, OverflowError):
        return JSONResponse(status_code=422, content={"error": "duration_s must be finite and between 0 and 300 seconds"})
    TIMINGS.profile(seconds)
    return {"schema_version": 1, "proxy": "agentrouter-codex-proxy", **TIMINGS.profile_status()}

@dataclass
class SSEPrimeResult:
    """Result of pre-commit inspection on an HTTP 200 text/event-stream response."""

    status: str  # "commit", "retry_rate_limit", "retry_transient", "resource_mismatch", "client_disconnected"
    primed_chunks: List[bytes] = field(default_factory=list)
    remainder: bytes = b""
    upstream_response: Optional[httpx.Response] = None
    queue: Optional[asyncio.Queue] = None
    producer_task: Optional[asyncio.Task] = None
    producer_exc: List[Exception] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    error_payload: Optional[Dict[str, Any]] = None


def _new_sse_queue() -> asyncio.Queue:
    # Each queued piece is at most 64 KiB; the queue itself stays within budget.
    return asyncio.Queue(maxsize=max(1, SSE_BUFFER_MAX_BYTES // SSE_CHUNK_MAX_BYTES))


async def _put_sse_chunk(queue: asyncio.Queue, chunk: Optional[bytes]) -> None:
    try:
        queue.put_nowait(chunk)
    except asyncio.QueueFull:
        STATS["sse_backpressure_events"] += 1
        await queue.put(chunk)


async def _sse_producer(
    upstream_response: httpx.Response,
    queue: asyncio.Queue,
    producer_exc: List[Exception],
) -> None:
    try:
        async for chunk in upstream_response.aiter_bytes():
            if len(chunk) <= SSE_CHUNK_MAX_BYTES:
                # Preserve small chunks immediately; never coalesce token events.
                await _put_sse_chunk(queue, chunk)
            else:
                for start in range(0, len(chunk), SSE_CHUNK_MAX_BYTES):
                    await _put_sse_chunk(queue, chunk[start:start + SSE_CHUNK_MAX_BYTES])
        await _put_sse_chunk(queue, None)
    except Exception as exc:
        producer_exc.append(exc)
        await _put_sse_chunk(queue, None)


async def _next_sse_chunk(queue: asyncio.Queue, timeout_s: float) -> Optional[bytes]:
    """Drain already buffered chunks without allocating a task and timer."""
    try:
        return queue.get_nowait()
    except asyncio.QueueEmpty:
        return await asyncio.wait_for(queue.get(), timeout=timeout_s)


async def _close_sse_producer(
    upstream_response: Optional[httpx.Response],
    producer_task: Optional[asyncio.Task],
) -> None:
    # Starlette cancels the entire AnyIO task group on client disconnect. Without
    # shielding, cancellation can interrupt gather/aclose and leak pool slots.
    with anyio.CancelScope(shield=True):
        try:
            if producer_task is not None:
                if not producer_task.done():
                    producer_task.cancel()
                await asyncio.gather(producer_task, return_exceptions=True)
        finally:
            # Direct Task.cancel() bypasses AnyIO shielding. Always close the
            # response even if cancellation interrupts joining the producer.
            if upstream_response is not None:
                try:
                    await upstream_response.aclose()
                except Exception as exc:
                    logger.warning("[Upstream Cleanup] %s", describe_upstream_error(exc))


class UpstreamStreamingResponse(StreamingResponse):
    """Release upstream even if downstream fails before iteration starts."""

    def __init__(self, *args, upstream_response, producer_task=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.upstream_response = upstream_response
        self.producer_task = producer_task

    async def __call__(self, scope, receive, send):
        started = TIMINGS.begin("sse_delivery")
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await _close_sse_producer(self.upstream_response, self.producer_task)
            finally:
                TIMINGS.finish("sse_delivery", started)


_CLIENT_DISCONNECTED = object()


async def _abort_upstream_work(task: asyncio.Task) -> None:
    # A send can finish while the disconnect check is awaiting receive(). Close
    # any returned but unclaimed Response as well as cancelling pending work.
    with anyio.CancelScope(shield=True):
        if not task.done():
            task.cancel()
        outcomes = await asyncio.gather(task, return_exceptions=True)
        if isinstance(outcomes[0], httpx.Response):
            await _close_sse_producer(outcomes[0], None)


async def _await_upstream_work(request: Request, operation):
    """Release pool capacity if a client leaves before headers/JSON are ready."""
    task = asyncio.create_task(operation)
    disconnect = getattr(request, '_proxy_disconnect_listener', None)
    owns_disconnect = False
    try:
        # After body consumption we own the receive channel until handing a
        # response to Starlette. Waiting on http.disconnect removes per-request
        # polling timers and frees abandoned sends as soon as the peer leaves.
        if disconnect is not None or hasattr(request, '_body'):
            if disconnect is None:
                disconnect = asyncio.create_task(_receive_until_disconnect(request))
                owns_disconnect = True
            done, _ = await asyncio.wait((task, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                disconnect.result()
                await _abort_upstream_work(task)
                STATS['upstream_disconnect_cancellations'] += 1
                return _CLIENT_DISCONNECTED
            return task.result()
        while True:
            done, _ = await asyncio.wait((task,), timeout=0.35)
            if done:
                return task.result()
            if await _client_disconnected(request):
                await _abort_upstream_work(task)
                STATS["upstream_disconnect_cancellations"] += 1
                return _CLIENT_DISCONNECTED
    except BaseException:
        await _abort_upstream_work(task)
        raise
    finally:
        if owns_disconnect:
            with anyio.CancelScope(shield=True):
                disconnect.cancel()
                await asyncio.gather(disconnect, return_exceptions=True)


def _parse_sse_frame(raw_frame: bytes) -> Tuple[str, Optional[Dict[str, Any]], str]:
    text = raw_frame.decode("utf-8", errors="replace")
    event_name = ""
    data_lines: List[str] = []
    has_non_comment = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        has_non_comment = True
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if not has_non_comment:
        return "", None, ""

    data_str = "\n".join(data_lines).strip()
    if not data_str or data_str == "[DONE]":
        return event_name, None, data_str

    parsed: Optional[Dict[str, Any]] = None
    try:
        obj = json.loads(data_str)
        if isinstance(obj, dict):
            parsed = obj
    except Exception:
        pass

    if not event_name and parsed is not None:
        t = parsed.get("type")
        if isinstance(t, str):
            event_name = t

    return event_name, parsed, data_str


def _extract_error_fields(
    obj: Optional[Dict[str, Any]],
    fallback_text: str = "",
) -> Tuple[str, str, Dict[str, Any]]:
    err_dict: Optional[Dict[str, Any]] = None
    if isinstance(obj, dict):
        if isinstance(obj.get("error"), dict):
            err_dict = obj["error"]
        elif isinstance(obj.get("error"), str):
            err_dict = {"message": obj["error"]}
        elif isinstance(obj.get("response"), dict) and isinstance(obj["response"].get("error"), dict):
            err_dict = obj["response"]["error"]
        elif obj.get("type") == "error" or obj.get("code") or obj.get("message"):
            err_dict = obj

    if err_dict is not None:
        code = str(err_dict.get("code") or err_dict.get("type") or "")
        msg = str(err_dict.get("message") or err_dict.get("detail") or fallback_text or "")
        return code, msg, err_dict

    return "", fallback_text, {"message": fallback_text}


def _classify_sse_frame(raw_frame: bytes) -> Tuple[str, str, str, Optional[Dict[str, Any]]]:
    """Classifies one complete SSE frame into ignore, setup, error, or commit."""
    event_name, parsed, data_str = _parse_sse_frame(raw_frame)
    if not event_name and parsed is None and not data_str:
        return "ignore", "", "", None

    ev_lower = event_name.lower()
    embedded_failed = isinstance(parsed, dict) and (
        bool(parsed.get("error"))
        or (
            isinstance(parsed.get("response"), dict)
            and (parsed["response"].get("status") == "failed" or bool(parsed["response"].get("error")))
        )
    )

    if ev_lower in ("error", "response.failed") or embedded_failed:
        code, msg, err_obj = _extract_error_fields(parsed, data_str)
        return "error", code, msg, err_obj

    if ev_lower in SSE_SETUP_EVENTS:
        return "setup", "", "", None

    if data_str == "[DONE]" and not ev_lower:
        return "ignore", "", "", None

    return "commit", "", "", None


async def _prime_responses_sse(
    upstream_response: httpx.Response,
    request: Request,
    *,
    first_frame_timeout_s: float = SSE_PRIME_FIRST_TIMEOUT_S,
    created_grace_s: float = SSE_PRIME_CREATED_GRACE_S,
) -> SSEPrimeResult:
    """
    Buffers initial SSE frames before committing HTTP 200 headers to Codex.
    Catches early HTTP-200 rate_limit_exceeded / response.failed / empty streams
    so the proxy can retry silently without tripping Codex reconnect banners.
    """
    queue: asyncio.Queue[Optional[bytes]] = _new_sse_queue()
    producer_exc: List[Exception] = []
    producer_task = asyncio.create_task(_sse_producer(upstream_response, queue, producer_exc))

    buf = bytearray()
    primed_chunks: List[bytes] = []
    primed_bytes = 0
    saw_setup = False
    overall_deadline = time.monotonic() + first_frame_timeout_s
    grace_deadline: Optional[float] = None

    def commit_result(remainder: bytes) -> SSEPrimeResult:
        return SSEPrimeResult(
            status="commit",
            primed_chunks=primed_chunks,
            remainder=remainder,
            upstream_response=upstream_response,
            queue=queue,
            producer_task=producer_task,
            producer_exc=producer_exc,
        )

    def buffer_limit_result(remainder: bytes) -> SSEPrimeResult:
        STATS["sse_prime_buffer_commits"] += 1
        return commit_result(remainder)

    async def abort_result(status: str, code: str = "", msg: str = "", payload: Optional[Dict[str, Any]] = None) -> SSEPrimeResult:
        await _close_sse_producer(upstream_response, producer_task)
        return SSEPrimeResult(status=status, error_code=code, error_message=msg, error_payload=payload)

    async def handle_frame(raw_frame: bytes, trailing_offset: int) -> Optional[SSEPrimeResult]:
        nonlocal saw_setup, grace_deadline, primed_bytes
        kind, err_code, err_msg, err_obj = _classify_sse_frame(raw_frame)
        if kind == "ignore":
            primed_chunks.append(raw_frame)
            primed_bytes += len(raw_frame)
            if primed_bytes >= SSE_PRIME_MAX_BYTES or len(primed_chunks) >= SSE_PRIME_MAX_FRAMES:
                return buffer_limit_result(bytes(buf[trailing_offset:]))
            return None
        if kind == "setup":
            primed_chunks.append(raw_frame)
            primed_bytes += len(raw_frame)
            if not saw_setup:
                saw_setup = True
                grace_deadline = time.monotonic() + created_grace_s
            if primed_bytes >= SSE_PRIME_MAX_BYTES or len(primed_chunks) >= SSE_PRIME_MAX_FRAMES:
                return buffer_limit_result(bytes(buf[trailing_offset:]))
            return None
        if kind == "error":
            primed_chunks.append(raw_frame)
            note_attempt('sse_error', status=200)
            if 'session binding invalidated' in err_msg.lower():
                note_attempt('binding_invalidated', status=200)
            if is_resource_mismatch_text(err_msg):
                return await abort_result("resource_mismatch", err_code, err_msg, err_obj)
            if _is_rate_limit_error(err_code, err_msg):
                return await abort_result("retry_rate_limit", err_code, err_msg, err_obj)
            if _is_transient_error(err_code, err_msg):
                return await abort_result("retry_transient", err_code, err_msg, err_obj)
            # Non-retryable upstream SSE rejection: commit & forward verbatim.
            return commit_result(bytes(buf[trailing_offset:]))

        primed_chunks.append(raw_frame)
        return commit_result(bytes(buf[trailing_offset:]))

    try:
        while True:
            if await _client_disconnected(request):
                return await abort_result("client_disconnected")

            now = time.monotonic()
            active_deadline = grace_deadline if grace_deadline is not None else overall_deadline
            remaining = active_deadline - now
            if remaining <= 0:
                if grace_deadline is not None and saw_setup:
                    return commit_result(bytes(buf))
                return await abort_result("retry_transient", "timeout", "SSE prime timeout waiting for upstream event")

            try:
                chunk = await _next_sse_chunk(queue, min(remaining, 0.35))
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                if buf:
                    res = await handle_frame(bytes(buf), len(buf))
                    if res is not None:
                        if res.status == "commit":
                            await queue.put(None)
                        return res
                err_note = describe_upstream_error(producer_exc[0]) if producer_exc else "Upstream SSE stream closed before emitting content"
                return await abort_result("retry_transient", "empty_stream", err_note)

            buf.extend(chunk)
            pos = 0
            for m in SSE_DELIM_RE.finditer(buf):
                raw_frame = bytes(buf[pos:m.end()])
                pos = m.end()
                res = await handle_frame(raw_frame, pos)
                if res is not None:
                    return res
            if pos > 0:
                del buf[:pos]
            if primed_bytes + len(buf) >= SSE_PRIME_MAX_BYTES:
                # Forward partial frames verbatim when inspection reaches its cap.
                # The client can finish parsing them as subsequent pieces arrive.
                return buffer_limit_result(bytes(buf))
    except asyncio.CancelledError:
        await _close_sse_producer(upstream_response, producer_task)
        raise
    except Exception as exc:
        return await abort_result("retry_transient", type(exc).__name__, describe_upstream_error(exc))


_SSE_LINE_BREAK_RE = re.compile(rb"\r\n|\r|\n")


class _SSEOutcomeTracker:
    """Inspect only bounded SSE line prefixes; never retain response bodies.

    Event headers take effect at a complete frame containing a data field.
    A clean HTTP EOF without a terminal event is not proof of an upstream error.
    """
    __slots__ = ('line', 'overlong', 'skip_lf', 'first_line', 'event', 'has_data',
                 'completed', 'error_code')
    PREFIX_LIMIT = 192

    def __init__(self):
        self.line = bytearray()
        self.overlong = self.skip_lf = False
        self.first_line = True
        self.event = b''
        self.has_data = self.completed = False
        self.error_code = ''

    def _append(self, chunk, start, end):
        available = self.PREFIX_LIMIT - len(self.line)
        length = end - start
        if available > 0:
            self.line.extend(chunk[start:start + min(length, available)])
        if length > available:
            self.overlong = True

    def _finish_line(self):
        line = self.line
        if self.first_line:
            if line.startswith(b'\xef\xbb\xbf'):
                del line[:3]
            self.first_line = False
        if not line and not self.overlong:
            if self.has_data:
                if self.event == b'response.completed':
                    self.completed = True
                elif self.event in (b'error', b'response.failed'):
                    self.error_code = 'upstream_response_failed'
                elif self.event == b'response.incomplete' and not self.error_code:
                    self.error_code = 'stream_incomplete'
            self.event, self.has_data = b'', False
        elif line == b'data' or line.startswith(b'data:'):
            self.has_data = True
        elif line == b'event' or line.startswith(b'event:'):
            value = bytes(line[6:]) if line.startswith(b'event:') else b''
            if value.startswith(b' '):
                value = value[1:]
            self.event = value if not self.overlong else b''
        line.clear()
        self.overlong = False

    def feed(self, chunk):
        start = 0
        if self.skip_lf:
            self.skip_lf = False
            if chunk.startswith(b'\n'):
                start = 1
            elif not chunk:
                self.skip_lf = True
                return
        for match in _SSE_LINE_BREAK_RE.finditer(chunk, start):
            self._append(chunk, start, match.start())
            self._finish_line()
            start = match.end()
            self.skip_lf = chunk[match.start()] == 13 and start == len(chunk)
        if start < len(chunk):
            self._append(chunk, start, len(chunk))


async def _stream_generator(
    upstream_response: httpx.Response,
    *,
    primed_chunks: Optional[List[bytes]] = None,
    remainder: bytes = b"",
    queue: Optional[asyncio.Queue] = None,
    producer_task: Optional[asyncio.Task] = None,
    producer_exc: Optional[List[Exception]] = None,
    ping_interval: float = 15.0,
) -> AsyncIterator[bytes]:
    """
    Streams SSE chunks from upstream back to Codex with periodic keep-alive heartbeat comments.
    Replays any pre-commit primed frames verbatim before continuing live chunks.
    """
    if queue is None or producer_task is None:
        queue = _new_sse_queue()
        producer_exc = []
        producer_task = asyncio.create_task(_sse_producer(upstream_response, queue, producer_exc))

    record = REQUEST_RECORD.get()
    tracker = _SSEOutcomeTracker() if record is not None and record.endpoint == 'responses' else None

    try:
        for pre in primed_chunks or ():
            if pre:
                if tracker is not None:
                    tracker.feed(pre)
                yield pre
        if remainder:
            if tracker is not None:
                tracker.feed(remainder)
            yield remainder

        while True:
            try:
                chunk = await _next_sse_chunk(queue, ping_interval)
                if chunk is None:
                    break
                if tracker is not None:
                    tracker.feed(chunk)
                yield chunk
            except asyncio.TimeoutError:
                yield b": ping\n\n"
    except Exception as exc:
        note_stream_error('upstream_stream_error')
        logger.warning("[Stream Exception] Upstream SSE connection interrupted: %s", describe_upstream_error(exc))
    finally:
        await _close_sse_producer(upstream_response, producer_task)
        if producer_exc:
            note_stream_error('upstream_stream_error')
            logger.warning("[Stream Exception] Upstream SSE connection interrupted: %s", describe_upstream_error(producer_exc[0]))
        elif tracker is not None and tracker.error_code:
            note_stream_error(tracker.error_code)
        if tracker is not None and not tracker.completed:
            logger.info("[Stream Finished] Upstream stream ended.")


def _clean_response_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in STRIP_RESPONSE_HEADERS}


def _rate_limit_error_response(
    err_payload: Optional[Dict[str, Any]],
    message: str,
    code: str,
    retry_after_s: int,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    out_headers = {key: value for key, value in (headers or {}).items()
                   if key.lower() not in ("content-type", "retry-after")}
    out_headers["retry-after"] = str(max(1, retry_after_s))
    body_err = dict(err_payload) if isinstance(err_payload, dict) else {"message": message or "Upstream rate limit exceeded"}
    if code and "code" not in body_err:
        body_err["code"] = code
    if "type" not in body_err:
        body_err["type"] = "rate_limit_error"
    return JSONResponse(status_code=429, content={"error": body_err}, headers=out_headers)


_JSON_DECODE_GROUP_BYTES = 262144
_JSON_DECODE_CHUNK_BYTES = 65536
_JSON_WS_BYTES_RE = re.compile(rb'[ \t\r\n]*')
_JSON_STRUCTURAL_BYTES_RE = re.compile(rb'["{}\[\]]')
_JSON_SCALAR_END_BYTES_RE = re.compile(rb'[ \t\r\n,}\]]')


class _ChunkedJSONText:
    """Canonical JSON string interiors; no source views or joined Unicode."""
    __slots__ = ('chunks', 'requires_ascii')

    def __init__(self, chunks, requires_ascii=False):
        self.chunks = tuple(chunks)
        self.requires_ascii = requires_ascii


def _json_string_end(body, start):
    cursor = start + 1
    while True:
        end = body.find(b'"', cursor)
        if end < 0:
            raise ValueError('unterminated string')
        if body[end - 1] != 92:
            return end + 1
        slash = end - 1
        while slash >= start and body[slash] == 92:
            slash -= 1
        if (end - slash - 1) % 2 == 0:
            return end + 1
        cursor = end + 1


def _has_large_json_string(body):
    cursor = 0
    for _ in range(128):
        start = body.find(b'"', cursor)
        if start < 0:
            return False
        cursor = _json_string_end(body, start)
        if cursor - start > _JSON_DECODE_GROUP_BYTES:
            return True
    return False


class _ChunkedJSONDecoder:
    """Use the stdlib C decoder for groups and bounded long-string pieces.

    Only large strings take this path. Keys remain ordinary strings; groups
    preserve numeric precision, duplicate-key order and stdlib escape handling.
    The span cache holds at most 128 integer pairs, avoiding repeated scans of
    the same multi-MiB string through its enclosing containers.
    """
    __slots__ = ('body', 'view', 'ascii', 'spans')

    def __init__(self, body):
        self.body = body
        self.view = memoryview(body)
        self.ascii = body.isascii()
        self.spans = {}

    def remember(self, start, end):
        if end - start > _JSON_DECODE_GROUP_BYTES:
            if len(self.spans) >= 128:
                self.spans.pop(next(iter(self.spans)))
            self.spans[start] = end

    def ws(self, pos):
        return _JSON_WS_BYTES_RE.match(self.body, pos).end()

    def end(self, start):
        cached = self.spans.get(start)
        if cached is not None:
            return cached
        char = self.body[start]
        if char == 34:
            end = _json_string_end(self.body, start)
            self.remember(start, end)
            return end
        if char in (123, 91):
            stack = [(start, char)]
            cursor = start + 1
            while stack:
                token = _JSON_STRUCTURAL_BYTES_RE.search(self.body, cursor)
                if token is None:
                    raise ValueError('unterminated container')
                pos = token.start()
                char = self.body[pos]
                if char == 34:
                    cursor = _json_string_end(self.body, pos)
                    self.remember(pos, cursor)
                    continue
                if char in (123, 91):
                    stack.append((pos, char))
                    if len(stack) > 1024:
                        raise RecursionError('JSON nesting')
                else:
                    opened, kind = stack.pop()
                    if char != (125 if kind == 123 else 93):
                        raise ValueError('mismatched container')
                    self.remember(opened, pos + 1)
                cursor = pos + 1
            return cursor
        token = _JSON_SCALAR_END_BYTES_RE.search(self.body, start)
        return token.start() if token is not None else len(self.body)

    def load(self, start, end, opening='', closing=''):
        return json.loads(opening + str(self.view[start:end], 'utf-8') + closing)

    def string(self, start, end, *, chunked=True):
        if self.body.find(b'\\', start + 1, end - 1) < 0:
            if end - start <= _JSON_DECODE_GROUP_BYTES:
                return self.load(start, end)
            pieces = []
            cursor = start + 1
            limit = end - 1
            while cursor < limit:
                boundary = min(cursor + _JSON_DECODE_CHUNK_BYTES, limit)
                if boundary < limit and not self.ascii:
                    while boundary > cursor and self.body[boundary] & 192 == 128:
                        boundary -= 1
                if boundary <= cursor:
                    raise ValueError('invalid string boundary')
                text = str(self.view[cursor:boundary], 'ascii' if self.ascii else 'utf-8')
                # C scanstring validates ASCII controls in bounded pieces. For
                # Unicode, decoding bounded pieces also avoids UTF-8 decoder's
                # worst-case full-document UCS4 capacity and resize copies.
                piece, _ = json.decoder.scanstring(text + '"', 0)
                # Valid unescaped UTF-8 is already the exact canonical string
                # interior. Copy bounded bytes, discarding the Unicode scratch
                # piece, so sending needs neither joining nor re-encoding it.
                pieces.append(bytes(self.view[cursor:boundary]) if chunked else piece)
                cursor = boundary
            return _ChunkedJSONText(pieces) if chunked else ''.join(pieces)
        if end - start <= _JSON_DECODE_GROUP_BYTES:
            return self.load(start, end)
        pieces = []
        requires_ascii = False
        cursor = start + 1
        limit = end - 1
        while cursor < limit:
            boundary = min(cursor + _JSON_DECODE_CHUNK_BYTES, limit)
            if boundary < limit:
                while boundary > cursor and self.body[boundary] & 192 == 128:
                    boundary -= 1
                slash = self.body.rfind(b'\\', max(cursor, boundary - 6), boundary)
                if slash >= 0:
                    before = slash - 1
                    while before >= cursor and self.body[before] == 92:
                        before -= 1
                    if (slash - before) % 2:
                        escape_end = slash + (6 if self.body[slash + 1] == 117 else 2)
                        if escape_end > boundary:
                            boundary = slash
                # JSON combines an escaped high/low surrogate pair. Never
                # split that pair into two independent stdlib string decodes.
                if boundary >= cursor + 6 and self.body[boundary - 6:boundary - 4] == b'\\u':
                    high = self.body[boundary - 4:boundary]
                    low = self.body[boundary + 2:boundary + 6]
                    if self.body[boundary:boundary + 2] == b'\\u':
                        try:
                            pair = 0xD800 <= int(high, 16) <= 0xDBFF and 0xDC00 <= int(low, 16) <= 0xDFFF
                        except ValueError:
                            pair = False
                        if pair:
                            slash = boundary - 6
                            before = slash - 1
                            while before >= cursor and self.body[before] == 92:
                                before -= 1
                            if (slash - before) % 2:
                                boundary -= 6
            if boundary <= cursor:
                raise ValueError('invalid string boundary')
            piece = self.load(cursor, boundary, '"', '"')
            if chunked:
                try:
                    canonical = json.encoder.encode_basestring(piece).encode('utf-8')
                except UnicodeEncodeError:
                    requires_ascii = True
                    canonical = json.encoder.encode_basestring_ascii(piece).encode('ascii')
                pieces.append(canonical[1:-1])
            else:
                pieces.append(piece)
            cursor = boundary
        return _ChunkedJSONText(pieces, requires_ascii) if chunked else ''.join(pieces)

    def value(self, start, end):
        if end - start <= _JSON_DECODE_GROUP_BYTES:
            return self.load(start, end)
        char = self.body[start]
        if char == 34:
            return self.string(start, end)
        if char == 91:
            return self.array(start, end)
        if char == 123:
            return self.object(start, end)
        return self.load(start, end)

    def array(self, start, end):
        result = []
        cursor = self.ws(start + 1)
        group_start = group_end = None
        def flush():
            nonlocal group_start
            if group_start is not None:
                result.extend(self.load(group_start, group_end, '[', ']'))
                group_start = None
        if cursor == end - 1 and self.body[cursor] == 93:
            return result
        while cursor < end - 1:
            value_end = self.end(cursor)
            if value_end > end - 1:
                raise ValueError('array boundary')
            if value_end - cursor > _JSON_DECODE_GROUP_BYTES:
                flush()
                result.append(self.value(cursor, value_end))
            else:
                if group_start is not None and value_end - group_start > _JSON_DECODE_GROUP_BYTES:
                    flush()
                if group_start is None:
                    group_start = cursor
                group_end = value_end
            cursor = self.ws(value_end)
            if cursor == end - 1 and self.body[cursor] == 93:
                flush()
                return result
            if self.body[cursor] != 44:
                raise ValueError('array comma')
            cursor = self.ws(cursor + 1)
        raise ValueError('array trailing comma')

    def object(self, start, end):
        result = {}
        cursor = self.ws(start + 1)
        group_start = group_end = None
        def flush():
            nonlocal group_start
            if group_start is not None:
                result.update(self.load(group_start, group_end, '{', '}'))
                group_start = None
        if cursor == end - 1 and self.body[cursor] == 125:
            return result
        while cursor < end - 1:
            if self.body[cursor] != 34:
                raise ValueError('object key')
            key_end = _json_string_end(self.body, cursor)
            colon = self.ws(key_end)
            if self.body[colon] != 58:
                raise ValueError('object colon')
            value_start = self.ws(colon + 1)
            value_end = self.end(value_start)
            if value_end > end - 1:
                raise ValueError('object boundary')
            if value_end - cursor > _JSON_DECODE_GROUP_BYTES:
                flush()
                key = self.string(cursor, key_end, chunked=False)
                result[key] = self.value(value_start, value_end)
            else:
                if group_start is not None and value_end - group_start > _JSON_DECODE_GROUP_BYTES:
                    flush()
                if group_start is None:
                    group_start = cursor
                group_end = value_end
            cursor = self.ws(value_end)
            if cursor == end - 1 and self.body[cursor] == 125:
                flush()
                return result
            if self.body[cursor] != 44:
                raise ValueError('object comma')
            cursor = self.ws(cursor + 1)
        raise ValueError('object trailing comma')

    def decode(self):
        start = self.ws(0)
        end = self.end(start)
        if self.ws(end) != len(self.body):
            raise ValueError('trailing bytes')
        return self.value(start, end)


_JSON_GROUP_CHARACTERS = 262144
_JSON_STRING_CHARACTERS = 65536


def _json_weight(value: Any, limit: int = _JSON_GROUP_CHARACTERS) -> int:
    """Cheap, capped sizing of parsed JSON; never walks large string contents."""
    if isinstance(value, str):
        return len(value) * (1 if value.isascii() else 4) + 2
    if isinstance(value, dict):
        size = 2
        for key, item in value.items():
            # Message histories have many short string leaves. Size those here
            # rather than making a Python recursive call for every field.
            weight = (len(item) * (1 if item.isascii() else 4) + 2
                      if isinstance(item, str) else _json_weight(item, limit - size))
            size += len(key) + 4 + weight
            if size > limit:
                break
        return size
    if isinstance(value, list):
        size = 2
        for item in value:
            weight = (len(item) * (1 if item.isascii() else 4) + 2
                      if isinstance(item, str) else _json_weight(item, limit - size))
            size += 1 + weight
            if size > limit:
                break
        return size
    if type(value) is int:
        # Upper-bound decimal digits without constructing a potentially huge
        # decimal string. Booleans retain the scalar bound below.
        return value.bit_length() * 30103 // 100000 + 2 + (value < 0)
    if isinstance(value, _ChunkedJSONText):
        # Force parent containers through the bounded writer as well: the C
        # encoder cannot consume our internal, already-decoded string pieces.
        return limit + 1
    return 24


def _encode_large_responses_payload(payload: dict, *, ensure_ascii: bool = False, source_size: int = 0) -> bytes:
    """Keep the C encoder for groups, avoiding a full-size intermediate string."""
    output = io.BytesIO()
    string_encoder = json.encoder.encode_basestring_ascii if ensure_ascii else json.encoder.encode_basestring
    # These trees originate in JSON and the sanitizer only copies/removes
    # containers. They are acyclic, so per-node circular markers add no value.
    group_encoder = json.JSONEncoder(ensure_ascii=ensure_ascii, check_circular=False, separators=(",", ":"))

    def packed(value: Any) -> str:
        return group_encoder.encode(value)

    def write_string(value: str) -> None:
        if len(value) <= _JSON_STRING_CHARACTERS:
            output.write(string_encoder(value).encode("utf-8"))
            return
        output.write(b'"')
        for start in range(0, len(value), _JSON_STRING_CHARACTERS):
            text = string_encoder(value[start:start + _JSON_STRING_CHARACTERS]).encode("utf-8")
            output.write(memoryview(text)[1:-1])
        output.write(b'"')

    def write(value: Any, weight: Optional[int] = None) -> None:
        if weight is None:
            weight = _json_weight(value)
        if weight <= _JSON_GROUP_CHARACTERS:
            output.write(packed(value).encode("utf-8"))
        elif isinstance(value, _ChunkedJSONText):
            if value.requires_ascii and not ensure_ascii:
                # Keep the existing whole-document ASCII fallback for a lone
                # surrogate, including other keys and ordinary string values.
                raise UnicodeEncodeError('utf-8', '\ud800', 0, 1, 'surrogates not allowed')
            output.write(b'"')
            for chunk in value.chunks:
                if not ensure_ascii or (chunk.isascii() and b'\x7f' not in chunk):
                    output.write(chunk)
                else:
                    decoded = json.loads('"' + chunk.decode('utf-8') + '"')
                    text = string_encoder(decoded).encode('ascii')
                    output.write(memoryview(text)[1:-1])
            output.write(b'"')
        elif isinstance(value, str):
            write_string(value)
        elif isinstance(value, (dict, list)):
            mapping = isinstance(value, dict)
            output.write(b"{" if mapping else b"[")
            group = {} if mapping else []
            group_size = 0
            first = True

            def flush() -> None:
                nonlocal group_size, first
                if group:
                    if not first:
                        output.write(b",")
                    output.write(memoryview(packed(group).encode("utf-8"))[1:-1])
                    group.clear()
                    group_size = 0
                    first = False

            items = value.items() if mapping else enumerate(value)
            for key, item in items:
                item_weight = _json_weight(item)
                entry_weight = item_weight + (len(key) + 4 if mapping else 1)
                if entry_weight > _JSON_GROUP_CHARACTERS:
                    flush()
                    if not first:
                        output.write(b",")
                    if mapping:
                        write_string(key)
                        output.write(b":")
                    write(item, item_weight)
                    first = False
                else:
                    if group_size + entry_weight > _JSON_GROUP_CHARACTERS:
                        flush()
                    if mapping:
                        group[key] = item
                    else:
                        group.append(item)
                    group_size += entry_weight
            flush()
            output.write(b"}" if mapping else b"]")
        else:
            output.write(packed(value).encode("utf-8"))

    try:
        weight = _json_weight(payload)
        if source_size:
            # Growing a multi-MiB BytesIO repeatedly copies its whole buffer.
            # The capped probe covers small replacements. Larger trees reserve
            # from the source, avoiding a second traversal of every message.
            hint = source_size if weight > _JSON_GROUP_CHARACTERS else min(source_size, weight)
            hint = (hint + _JSON_STRING_CHARACTERS - 1) // _JSON_STRING_CHARACTERS * _JSON_STRING_CHARACTERS
            if hint:
                output.seek(hint - 1)
                output.write(b"\0")
                output.seek(0)
        write(payload, weight)
        output.truncate()
        return output.getvalue()
    finally:
        output.close()


def _encode_responses_payload(payload: dict, *, source_size: int = 0) -> bytes:
    """Encode only modified payloads, without expanding Unicode to ASCII escapes."""
    if source_size >= LARGE_REQUEST_BODY_BYTES:
        try:
            return _encode_large_responses_payload(payload, source_size=source_size)
        except UnicodeEncodeError:
            return _encode_large_responses_payload(payload, ensure_ascii=True, source_size=source_size)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        # Preserve the previous serializer's handling of escaped lone surrogates.
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


_IMAGE_PRUNING_MARKER_RE = re.compile(
    rb'"(?:input_image|computer_screenshot|image_url)"'
    rb'(?:\s*,\s*"image_url")?(?P<value>\s*:\s*")?', re.IGNORECASE,
)


def _payload_needs_image_pruning(body: bytes, max_images: int) -> bool:
    # Every recognized image has a quoted marker. Adjacent type/value and
    # image_url key markers belong to the same object and can count as one.
    # Other matches can overestimate, never undercount. A string field's value
    # cannot contain more image objects, so skip it with bytes.find (no copy).
    cursor = count = 0
    while True:
        marker = _IMAGE_PRUNING_MARKER_RE.search(body, cursor)
        boundary = marker.start() if marker is not None else len(body)
        # Escapes outside skipped string values can hide a type/key.
        if body.find(b"\\u", cursor, boundary) != -1:
            return True
        if marker is None:
            return False
        count += 1
        if count > max_images:
            return True
        cursor = marker.end()
        if marker.group("value") is not None:
            quote = body.find(b'"', cursor)
            if quote == -1:
                return True
            # An escaped quote ends the shortcut early; subsequent bytes are
            # inspected conservatively instead of needing a JSON string parser.
            cursor = quote + 1


def _decode_responses_payload(body: bytes, *, chunked: bool = False) -> Optional[dict]:
    try:
        # Small/dense documents keep the C decoder. A capped quote probe avoids
        # walking every historical message just to choose an encoding strategy.
        payload = (_ChunkedJSONDecoder(body).decode()
                   if chunked and len(body) >= LARGE_REQUEST_BODY_BYTES and _has_large_json_string(body)
                   else json.loads(body.decode("utf-8")))
        return payload if isinstance(payload, dict) else None
    except RecursionError:
        # Our Python container frames reach their recursion limit before the C
        # decoder. Preserve the existing acceptance boundary for deep inputs.
        try:
            payload = json.loads(body.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None
    except Exception:
        return None


def _compress_request_gzip(content: bytes) -> bytes:
    if _native_gzip is not None:
        return _native_gzip.compress(content, compresslevel=1, mtime=0)
    return gzip.compress(content, compresslevel=1, mtime=0)


def _decompress_proxy_gzip(content: bytes, source_size: int) -> bytes:
    """Reconstruct our single gzip member, reserving its known original size."""
    if _native_gzip is not None:
        codec = _native_gzip.igzip_lib
        return codec.decompress(content, flag=codec.DECOMP_GZIP, bufsize=source_size)
    return zlib.decompress(content, wbits=31, bufsize=source_size)


class _GzipPool:
    """Bound compressed work without competing with asyncio's default executor."""

    def __init__(self, workers: int, max_jobs: int):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-gzip")
        self.slots = threading.BoundedSemaphore(max_jobs)
        self.lock = threading.Lock()
        self._inflight = 0

    @property
    def inflight(self) -> int:
        with self.lock:
            return self._inflight

    def _completed(self, _future) -> None:
        with self.lock:
            self._inflight -= 1
        self.slots.release()

    def submit(self, function, content: bytes):
        if not self.slots.acquire(blocking=False):
            return None
        with self.lock:
            self._inflight += 1
        try:
            future = self.executor.submit(function, content)
        except BaseException:
            self._completed(None)
            raise
        # Runs even if an awaiting request is cancelled. A running compressor
        # retains its slot until it really finishes; a cancelled queued job frees it.
        future.add_done_callback(self._completed)
        return future

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


_GZIP_POOL: Optional[_GzipPool] = None
_GZIP_REJECTIONS: OrderedDict[Tuple[str, Optional[bytes]], float] = OrderedDict()
_GZIP_COST_PROFILES: OrderedDict[Tuple[int, int, bool], Tuple[float, float]] = OrderedDict()
_GZIP_PROFILE_LOCK = threading.Lock()
_UPLOAD_OBSERVATIONS: OrderedDict[Tuple[str, Optional[bytes]], List[Tuple[float, float]]] = OrderedDict()


def _get_gzip_pool() -> _GzipPool:
    global _GZIP_POOL
    if _GZIP_POOL is None:
        _GZIP_POOL = _GzipPool(REQUEST_GZIP_WORKERS, REQUEST_GZIP_MAX_JOBS)
    return _GZIP_POOL


def reset_compression_state() -> None:
    """Reset capability hints, primarily to isolate independent tests."""
    _GZIP_REJECTIONS.clear()
    _UPLOAD_OBSERVATIONS.clear()
    with _GZIP_PROFILE_LOCK:
        _GZIP_COST_PROFILES.clear()


def _gzip_capability_key(headers: Mapping[str, str]) -> Tuple[str, Optional[bytes]]:
    fingerprint = _credential_fingerprint(headers)
    organization = headers.get("openai-organization", "")
    project = headers.get("openai-project", "")
    if organization or project:
        fingerprint = hashlib.blake2b((fingerprint or b"") + b"\0" + organization.encode()
                                      + b"\0" + project.encode(), key=_PROFILE_HASH_KEY, digest_size=16).digest()
    return UPSTREAM, fingerprint


def _gzip_rejected_recently(headers: Mapping[str, str]) -> bool:
    if REQUEST_GZIP_REJECT_TTL_S <= 0:
        return False
    key = _gzip_capability_key(headers)
    deadline = _GZIP_REJECTIONS.get(key)
    if deadline is None:
        return False
    if deadline <= time.monotonic():
        del _GZIP_REJECTIONS[key]
        return False
    _GZIP_REJECTIONS.move_to_end(key)
    return True


def _record_gzip_rejection(headers: Mapping[str, str]) -> None:
    if REQUEST_GZIP_REJECT_TTL_S <= 0:
        return
    key = _gzip_capability_key(headers)
    _GZIP_REJECTIONS[key] = time.monotonic() + REQUEST_GZIP_REJECT_TTL_S
    _GZIP_REJECTIONS.move_to_end(key)
    while len(_GZIP_REJECTIONS) > REQUEST_GZIP_CACHE_SIZE:
        _GZIP_REJECTIONS.popitem(last=False)


def _record_upload_observation(content: bytes, headers: Mapping[str, str], elapsed: float,
                               response: httpx.Response, *, header_wait_s: Optional[float] = None) -> None:
    # Real, large, unencoded requests only, measured between HTTPcore's send-body
    # events. Ignore small uploads that socket buffers can absorb immediately;
    # even larger writes remain an estimate, not a bandwidth test. Cap automatic
    # hints conservatively; an explicit rate override bypasses this ceiling.
    if (not REQUEST_GZIP_ENABLED or REQUEST_GZIP_UPLOAD_MBIT_S > 0
            or len(content) < UPLOAD_OBSERVATION_MIN_BYTES or headers.get("content-encoding") or elapsed <= 0
            or header_wait_s is None or not math.isfinite(elapsed) or not math.isfinite(header_wait_s)
            or response.status_code != 200 or "network_stream" not in response.extensions):
        return
    key = _gzip_capability_key(headers)
    if header_wait_s > max(0.05, elapsed * 4):
        # A quick write followed by long header waiting cannot distinguish
        # queued delivery from upstream work. Do not train from it or retain a
        # stale fast hint. Header waiting is a confidence check, never bandwidth.
        _UPLOAD_OBSERVATIONS.pop(key, None)
        return
    now = time.monotonic()
    samples = _UPLOAD_OBSERVATIONS.get(key, [])
    samples = [(at, rate) for at, rate in samples if now - at < GZIP_PROFILE_TTL_S]
    samples.append((now, min(UPLOAD_ESTIMATE_MAX_MBIT_S, len(content) * 8 / elapsed / 1e6)))
    _UPLOAD_OBSERVATIONS[key] = samples[-6:]
    _UPLOAD_OBSERVATIONS.move_to_end(key)
    while len(_UPLOAD_OBSERVATIONS) > REQUEST_GZIP_CACHE_SIZE:
        _UPLOAD_OBSERVATIONS.popitem(last=False)


def _upload_estimate(headers: Mapping[str, str]) -> float:
    if REQUEST_GZIP_UPLOAD_MBIT_S > 0:
        return REQUEST_GZIP_UPLOAD_MBIT_S
    now = time.monotonic()
    key = _gzip_capability_key(headers)
    samples = _UPLOAD_OBSERVATIONS.get(key)
    if not samples:
        return 0.0
    recent = [rate for at, rate in samples if now - at < GZIP_PROFILE_TTL_S]
    if not recent:
        del _UPLOAD_OBSERVATIONS[key]
    # Require multiple observations and keep the slowest recent lower bound.
    return min(recent) if len(recent) >= 2 else 0.0


def _gzip_if_worthwhile(content: bytes, *, upload_mbit_s: float = 0.0) -> bytes:
    # Sample three regions before spending CPU on a whole payload. Already
    # compressed images and encrypted state usually fail this compressibility check.
    sample_size = min(8192, len(content) // 3)
    if not sample_size:
        return content
    started = time.perf_counter()
    sample_output_bytes = 0
    for start in (0, (len(content) - sample_size) // 2, len(content) - sample_size):
        sample = content[start:start + sample_size]
        sample_bytes = len(_compress_request_gzip(sample))
        if sample_bytes > len(sample) * 0.6:
            return content
        sample_output_bytes += sample_bytes
    sample_ratio = sample_output_bytes / (3 * sample_size)
    profile_key = (int(sample_ratio * 20), len(content).bit_length(), _native_gzip is None)
    with _GZIP_PROFILE_LOCK:
        profile = _GZIP_COST_PROFILES.get(profile_key)
    if profile is not None and time.monotonic() - profile[1] < GZIP_PROFILE_TTL_S and upload_mbit_s > 0:
        expected_cost = profile[0] * len(content) * REQUEST_GZIP_COST_MARGIN
        expected_upload_saving = len(content) * (1 - sample_ratio) * 8 / (upload_mbit_s * 1e6)
        if expected_cost >= expected_upload_saving:
            with _GZIP_PROFILE_LOCK:
                STATS["gzip_cost_skips"] += 1
            return content
    compressed = _compress_request_gzip(content)
    elapsed_per_byte = (time.perf_counter() - started) / len(content)
    with _GZIP_PROFILE_LOCK:
        previous = _GZIP_COST_PROFILES.get(profile_key)
        if previous is not None and time.monotonic() - previous[1] < GZIP_PROFILE_TTL_S:
            # React immediately to slower work, decay conservatively when faster.
            elapsed_per_byte = max(elapsed_per_byte, previous[0] * 0.75 + elapsed_per_byte * 0.25)
        _GZIP_COST_PROFILES[profile_key] = (elapsed_per_byte, time.monotonic())
        _GZIP_COST_PROFILES.move_to_end(profile_key)
        while len(_GZIP_COST_PROFILES) > 128:
            _GZIP_COST_PROFILES.popitem(last=False)
    return compressed if len(compressed) <= len(content) * 0.8 else content


async def _prepare_upstream_content(content: bytes, source_headers: Dict[str, str]) -> Tuple[bytes, Dict[str, str]]:
    if (
        not REQUEST_GZIP_ENABLED or len(content) < REQUEST_GZIP_MIN_BYTES
        or any(name.lower() == "content-encoding" for name in source_headers)
    ):
        return content, source_headers
    if _gzip_rejected_recently(source_headers):
        STATS["gzip_cached_rejection_skips"] += 1
        return content, source_headers
    try:
        started = time.perf_counter()
        upload_mbit_s = _upload_estimate(source_headers)
        compress = partial(_gzip_if_worthwhile, upload_mbit_s=upload_mbit_s) if upload_mbit_s else _gzip_if_worthwhile
        job = _get_gzip_pool().submit(compress, content)
        if job is None:
            STATS["gzip_busy_skips"] += 1
            return content, source_headers
        compressed = await asyncio.wrap_future(job)
        STATS["gzip_evaluations"] += 1
        STATS["gzip_preparation_seconds_total"] += time.perf_counter() - started
    except Exception as exc:
        logger.warning("[Request Compression] %s", describe_upstream_error(exc))
        return content, source_headers
    if compressed is content:
        return content, source_headers
    headers = dict(source_headers)
    headers["content-encoding"] = "gzip"
    STATS["gzip_payloads"] += 1
    STATS["gzip_input_bytes"] += len(content)
    STATS["gzip_output_bytes"] += len(compressed)
    return compressed, headers


class _BodyBudgetExpired(Exception):
    pass


class _BodyBudget:
    """FIFO admission estimates, not a process RSS limit. One asyncio loop."""
    __slots__ = ('limit', 'used', 'peak', 'queue', 'waits', 'timeouts')

    def __init__(self, limit):
        self.limit = limit
        self.used = self.peak = self.waits = self.timeouts = 0
        self.queue = deque()

    def _drain(self):
        while self.queue:
            future, weight = self.queue[0]
            if future.cancelled():
                self.queue.popleft()
                continue
            # An oversized request runs alone rather than waiting forever.
            if self.used and self.used + weight > self.limit:
                break
            self.queue.popleft()
            self.used += weight
            self.peak = max(self.peak, self.used)
            future.set_result(weight)

    async def acquire(self, weight):
        if not self.limit or not weight:
            return 0
        if not self.queue and (not self.used or self.used + weight <= self.limit):
            self.used += weight
            self.peak = max(self.peak, self.used)
            return weight
        self.waits += 1
        started = time.perf_counter()
        future = asyncio.get_running_loop().create_future()
        entry = (future, weight)
        self.queue.append(entry)
        try:
            return await asyncio.wait_for(asyncio.shield(future), BODY_BUDGET_WAIT_S)
        except BaseException as error:
            # Cancellation after admission also returns its reserved credits.
            if future.done() and not future.cancelled():
                self.used -= future.result()
            else:
                future.cancel()
                try:
                    self.queue.remove(entry)
                except ValueError:
                    pass
            self._drain()
            if isinstance(error, asyncio.TimeoutError):
                self.timeouts += 1
                raise _BodyBudgetExpired() from None
            raise
        finally:
            note_attempt('memory_wait', duration_s=time.perf_counter() - started)

    def release(self, weight):
        self.used -= weight
        self._drain()

    def snapshot(self):
        return {'limit_bytes': self.limit, 'reserved_bytes': self.used,
                'peak_reserved_bytes': self.peak, 'waiters': len(self.queue),
                'waits': self.waits, 'timeouts': self.timeouts,
                'reservation_factor': BODY_RESERVATION_FACTOR}


_BODY_BUDGETS = weakref.WeakKeyDictionary()


def _body_budget():
    loop = asyncio.get_running_loop()
    budget = _BODY_BUDGETS.get(loop)
    if budget is None:
        budget = _BodyBudget(BODY_BUDGET_BYTES)
        _BODY_BUDGETS[loop] = budget
    return budget


class _BodyLease:
    __slots__ = ('budget', 'weight')

    def __init__(self):
        self.budget = None
        self.weight = 0

    async def reserve(self, source_size, *, unknown=False):
        if not BODY_BUDGET_BYTES or (source_size < LARGE_REQUEST_BODY_BYTES and not self.weight):
            return
        if self.budget is None:
            self.budget = _body_budget()
        weight = max(self.budget.limit, source_size * BODY_RESERVATION_FACTOR) if unknown else source_size * BODY_RESERVATION_FACTOR
        if weight <= self.weight:
            self.budget.release(self.weight - weight)
            self.weight = weight
            return
        # Release before an upgrade; partial reservations can deadlock each other.
        # Only compressed replay or a bounded prefix remains during this wait.
        self.release()
        self.weight = await self.budget.acquire(weight)

    async def reserve_incoming(self, request, source_size, *, unknown=False):
        if not BODY_BUDGET_BYTES or source_size < LARGE_REQUEST_BODY_BYTES:
            return
        budget = self.budget or _body_budget()
        weight = max(budget.limit, source_size * BODY_RESERVATION_FACTOR) if unknown else source_size * BODY_RESERVATION_FACTOR
        if not budget.queue and (not budget.used or budget.used + weight <= budget.limit):
            return await self.reserve(source_size, unknown=unknown)
        # Before the body is consumed, watch just one ASGI receive event while
        # awaiting admission. Replay a body event exactly once; never drain and
        # accumulate the entire upload to discover a disconnect.
        admission = asyncio.create_task(self.reserve(source_size, unknown=unknown))
        original_receive = request.receive
        receive = asyncio.create_task(original_receive())
        message = None
        try:
            done, _ = await asyncio.wait((admission, receive), return_when=asyncio.FIRST_COMPLETED)
            if receive in done:
                message = receive.result()
                if message['type'] == 'http.disconnect':
                    raise ClientDisconnect()
                if message['type'] == 'http.request' and not message.get('more_body', False) and not admission.done():
                    # A complete prefetched body needs no further body buffers.
                    # Its next event can report a disconnected queued caller.
                    receive = asyncio.create_task(original_receive())
                    done, _ = await asyncio.wait((admission, receive), return_when=asyncio.FIRST_COMPLETED)
                    if receive in done and receive.result()['type'] == 'http.disconnect':
                        raise ClientDisconnect()
            await admission
        finally:
            with anyio.CancelScope(shield=True):
                if not receive.done():
                    receive.cancel()
                if not admission.done():
                    admission.cancel()
                await asyncio.gather(receive, admission, return_exceptions=True)
            if message is None and receive.done() and not receive.cancelled() and receive.exception() is None:
                message = receive.result()
            if message is not None and message['type'] == 'http.request':
                unread = [message]
                async def replay():
                    if unread:
                        return unread.pop()
                    request._receive = original_receive
                    return await original_receive()
                request._receive = replay

    def shrink(self, source_size):
        weight = min(self.weight, source_size * BODY_RESERVATION_FACTOR)
        if self.weight != weight:
            self.budget.release(self.weight - weight)
            self.weight = weight

    def release(self):
        if self.weight:
            weight, self.weight = self.weight, 0
            self.budget.release(weight)


def _content_length(request):
    raw = request.headers.get('content-length', '')
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 12 else None


async def _read_request_body(request, lease):
    """Single chunks are reused; incremental BytesIO avoids a join-size copy."""
    if hasattr(request, '_body'):
        body = await request.body()
        await lease.reserve(len(body))
        return body
    hint = _content_length(request)
    if hint is not None and hint < LARGE_REQUEST_BODY_BYTES:
        body = await request.body()
        # Uvicorn enforces Content-Length. Also account for direct ASGI callers
        # supplying an inexact small hint before any large-body transforms.
        if len(body) >= LARGE_REQUEST_BODY_BYTES:
            await lease.reserve(len(body))
        return body
    if hint is not None:
        await lease.reserve_incoming(request, hint)
    first = output = None
    capacity = 0
    total = 0
    unknown_reserved = False
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if (hint is None or total > hint) and not unknown_reserved and total >= LARGE_REQUEST_BODY_BYTES:
                # Unknown-length large uploads reserve exclusively, avoiding
                # incremental capacity upgrades between multiple partial bodies.
                await lease.reserve_incoming(request, total, unknown=True)
                unknown_reserved = True
            if first is None and output is None:
                first = chunk
                continue
            if output is None:
                output = io.BytesIO()
                if hint is not None and LARGE_REQUEST_BODY_BYTES <= hint <= BODY_PREALLOC_MAX_BYTES:
                    capacity = hint
                    output.seek(hint - 1)
                    output.write(b'\0')
                    output.seek(0)
                output.write(first)
                first = None
            if total > capacity and (hint is None or total > hint or hint > BODY_PREALLOC_MAX_BYTES):
                # Grow unknown/very large bodies geometrically at first, then
                # in bounded 8-MiB steps. Avoid repeated small reallocations.
                growth = min(max(capacity * 2, total), BODY_GROWTH_STEP_BYTES) if capacity < BODY_GROWTH_STEP_BYTES else capacity + BODY_GROWTH_STEP_BYTES
                capacity = max(total, growth)
                position = output.tell()
                output.seek(capacity - 1)
                output.write(b'\0')
                output.seek(position)
            output.write(chunk)
        if output is None:
            body = first if first is not None else b''
        else:
            output.truncate(total)
            body = output.getvalue()
        request._body = body
        if unknown_reserved and total * BODY_RESERVATION_FACTOR > lease.weight and lease.weight:
            # Unknown-length uploads entered exclusively. Grow their estimate
            # without another wait or releasing the fully assembled body.
            weight = total * BODY_RESERVATION_FACTOR
            lease.budget.used += weight - lease.weight
            lease.budget.peak = max(lease.budget.peak, lease.budget.used)
            lease.weight = weight
        lease.shrink(total)
        return body
    finally:
        if output is not None:
            output.close()


@app.post("/v1/responses")
@app.post("/responses")
async def handle_responses(request: Request):
    with TIMINGS.measure("response_ready"):
        return await _prepare_responses_request(request)


async def _prepare_responses_request(request: Request):
    """
    Main handler for Codex /v1/responses.
    Performs pre-commit SSE priming, staggered rate-limit recovery, and stateless
    sanitization fallback on cross-resource mismatches.
    """
    STATS["requests_total"] += 1
    STATS["responses_requests"] += 1

    lease = _BodyLease()
    try:
        return await _prepare_budgeted_responses(request, lease)
    except _BodyBudgetExpired:
        note_attempt('memory_budget_exhausted')
        return JSONResponse(status_code=503, headers={'Retry-After': '2'},
                            content={'error': {'type': 'proxy_memory_busy',
                                               'message': 'Large request memory admission timed out; retry shortly'}})
    except ClientDisconnect:
        note_attempt('cancelled')
        return Response(status_code=499)
    finally:
        lease.release()


async def _prepare_budgeted_responses(request: Request, lease):
    try:
        with TIMINGS.measure("client_upload"):
            body_bytes = await _read_request_body(request, lease)
    except ClientDisconnect:
        STATS["client_disconnects_during_upload"] += 1
        note_attempt('cancelled')
        return Response(status_code=499)
    body_size = len(body_bytes)
    source_headers = _filter_headers(dict(request.headers))
    rate_state = _acquire_rate_limit_state(source_headers)
    async with _watch_client_disconnect(request):
        try:
            operation = _handle_responses(request, body_bytes, source_headers, rate_state, lease)
            # Only the handler and receive cache own replay bytes while awaiting.
            del body_bytes
            response = await operation
            return response
        finally:
            if body_size >= LARGE_REQUEST_BODY_BYTES:
                # No further recovery or upload needs the body, including after
                # cancellation. Keep the cache to mark the receive side consumed.
                request._body = b""
            _release_rate_limit_state(rate_state)


async def _handle_responses(request: Request, body_bytes: bytes, source_headers: Dict[str, str],
                            rate_state: Dict[str, Any], lease):
    with TIMINGS.measure("payload_inspect"):
        body_json = (_decode_responses_payload(body_bytes, chunked=True)
                     if _payload_needs_image_pruning(body_bytes, MAX_IMAGES_PER_REQUEST) else None)
        headers = source_headers
        upstream_url = f"{UPSTREAM}/v1/responses"

        active_payload = body_json
        req_content = body_bytes
        wire_content = None
        proxy_added_gzip = False
        compressed_plain_size = 0
        fallback_active = False
        gzip_fallback_active = False

        # Preemptive check: prune excess historical images before sending upstream.
        if active_payload and isinstance(active_payload.get("input"), list):
            active_payload, pre_pruned = prune_excess_images(active_payload, max_images=MAX_IMAGES_PER_REQUEST)
            if pre_pruned > 0:
                # Recovery uses the pruned tree; release removed image strings.
                body_json = active_payload
                source_size = len(body_bytes)
                body_bytes = req_content = b""
                if source_size >= LARGE_REQUEST_BODY_BYTES:
                    request._body = b""
                req_content = _encode_responses_payload(active_payload, source_size=source_size)
                STATS["images_pruned_total"] += pre_pruned
                logger.warning(
                    "[Preemptive Image Pruning] Pruned %d historical images (kept %d latest) to prevent 50-image ceiling rejection",
                    pre_pruned, MAX_IMAGES_PER_REQUEST
                )

        # Any fallback decodes the current request; removed images are not needed.
        body_bytes = b""
        body_json = active_payload = None
        lease.shrink(len(req_content))

    retry_start = time.monotonic()
    transient_retries = 0
    mismatch_attempts = 0
    max_mismatch_retries = 3
    last_failure_response: Optional[Response] = None

    async def plain_content() -> bytes:
        nonlocal req_content
        if req_content is None:
            admitted = await _await_upstream_work(request, lease.reserve(compressed_plain_size))
            if admitted is _CLIENT_DISCONNECTED:
                raise ClientDisconnect()
            # Only our own gzip is reconstructed, and only when recovery or a
            # 415 needs the exact current JSON. Normal retries replay gzip as is.
            req_content = _decompress_proxy_gzip(wire_content, compressed_plain_size)
        return req_content

    async def activate_stateless_fallback(source_label: str, detail: str) -> bool:
        nonlocal fallback_active, active_payload, body_json, mismatch_attempts, req_content, wire_content, proxy_added_gzip
        STATS["resource_mismatches_caught"] += 1
        logger.warning("[Resource Mismatch] %s: %s", source_label, detail[:160])
        if not fallback_active and body_json is None:
            body_json = _decode_responses_payload(await plain_content(), chunked=True)
        if not fallback_active and body_json is not None:
            fallback_active = True
            STATS["stateless_fallbacks_applied"] += 1
            active_payload = sanitize_responses_payload(
                active_payload if active_payload is not None else body_json
            )
            source_size = len(req_content)
            # Once recovery has a sanitized tree, original replay bytes and
            # removed state items are no longer needed during serialization.
            req_content = b""
            wire_content = body_json = None
            proxy_added_gzip = False
            if source_size >= LARGE_REQUEST_BODY_BYTES:
                request._body = b""
            req_content = _encode_responses_payload(active_payload, source_size=source_size)
            wire_content = req_content if gzip_fallback_active else None
            # Subsequent attempts reuse these bytes, not the decoded trees.
            body_json = active_payload = None
            lease.shrink(len(req_content))
            note_attempt('resource_recovery')
            logger.warning("[Recovery] Activating sanitized stateless payload to bypass cross-resource pinning!")
            return True
        mismatch_attempts += 1
        return mismatch_attempts <= max_mismatch_retries

    async def schedule_transient_wait(
        is_rate_limit: bool,
        resp_hdrs: Mapping[str, str],
        err_msg: str,
        label: str,
    ) -> Tuple[bool, int]:
        nonlocal transient_retries
        if is_rate_limit:
            STATS["rate_limits_caught"] += 1
            streak = min(int(rate_state["streak"]) + 1, 4)
            delay_s, hint_s = _extract_retry_delay_s(resp_hdrs, err_msg, streak)
            if hint_s is not None:
                STATS["rate_limit_hint_responses"] += 1
            # Always protect the other callers, even when this request has no
            # retries left or cannot fit the authoritative delay in its budget.
            _record_rate_limit_cooldown(delay_s, rate_state)
            suggested_ra = max(1, math.ceil(rate_state["cooldown_until"] - time.monotonic()))
        else:
            delay_s = min(1.0 * (2 ** min(transient_retries, 3)) + random.uniform(0.1, 0.4), RATE_LIMIT_MAX_DELAY_S)
            suggested_ra = max(1, int(round(delay_s)))

        remaining_budget = MAX_RATE_LIMIT_WAIT_S - (time.monotonic() - retry_start)
        if transient_retries >= MAX_TRANSIENT_RETRIES or delay_s >= remaining_budget:
            if is_rate_limit:
                STATS["rate_limit_budget_exhaustions"] += 1
            return False, suggested_ra

        wait_s = delay_s
        note_attempt('quota_wait' if is_rate_limit else 'retry_wait', wait_s=wait_s)
        transient_retries += 1
        logger.warning(
            "[%s] Retrying (%d/%d) after %.1fs: %s",
            label, transient_retries, MAX_TRANSIENT_RETRIES, wait_s, (err_msg or "")[:140]
        )
        ok = await _sleep_unless_disconnected(wait_s, request, is_rate_limit=is_rate_limit)
        return ok, suggested_ra

    while True:
        if wire_content is None:
            payload_size = len(req_content)
            with TIMINGS.measure("payload_prepare"):
                wire_content, headers = await _prepare_upstream_content(req_content, source_headers)
            proxy_added_gzip = wire_content is not req_content
            if proxy_added_gzip:
                # Compressed bytes are sufficient for every normal retry. Drop
                # both owners of the original before admission/headers waits.
                compressed_plain_size = payload_size
                req_content = None
                request._body = b""
                lease.shrink(len(wire_content))
            if REQUEST_GZIP_ENABLED and payload_size >= REQUEST_GZIP_MIN_BYTES and await _client_disconnected(request):
                return Response(status_code=499)

        try:
            if not await _wait_for_rate_limit_gate(
                request, rate_state, deadline=retry_start + MAX_RATE_LIMIT_WAIT_S
            ):
                return Response(status_code=499)
        except _RateLimitGateExpired:
            retry_after = max(1, math.ceil(max(
                rate_state["cooldown_until"], rate_state["next_launch_after"]
            ) - time.monotonic()))
            return _rate_limit_error_response(
                None, "Upstream rate-limit queue exceeded this request's retry budget",
                "rate_limit_exceeded", retry_after, {},
            )
        attempt_generation = rate_state["generation"]

        resp = None
        stream_handed_off = False
        try:
            client = get_shared_client()
            pending_response = await _await_upstream_work(request, _send_with_transport_retry(
                client,
                method="POST",
                url=upstream_url,
                headers=headers,
                content=wire_content,
                stream=True,
                max_retries=5,
            ))
            if pending_response is _CLIENT_DISCONNECTED:
                return Response(status_code=499)
            resp = pending_response

            resp_headers = _clean_response_headers(resp.headers)
            content_type = resp.headers.get("content-type", "")

            if resp.status_code == 200 and "text/event-stream" in content_type.lower():
                with TIMINGS.measure("sse_prime"):
                    prime = await _prime_responses_sse(resp, request)
                if prime.status == "commit":
                    _clear_rate_limit_cooldown(rate_state, attempt_generation)
                    if fallback_active or gzip_fallback_active or transient_retries > 0 or mismatch_attempts > 0:
                        STATS["successful_recoveries"] += 1
                        logger.info(
                            "[Success] Request recovered (fallback=%s, gzip_fallback=%s, retries=%d)!",
                            fallback_active, gzip_fallback_active, transient_retries + mismatch_attempts
                        )
                    downstream = UpstreamStreamingResponse(
                        _stream_generator(
                            resp,
                            primed_chunks=prime.primed_chunks,
                            remainder=prime.remainder,
                            queue=prime.queue,
                            producer_task=prime.producer_task,
                            producer_exc=prime.producer_exc,
                        ),
                        status_code=200,
                        headers=resp_headers,
                        media_type="text/event-stream",
                        upstream_response=resp,
                        producer_task=prime.producer_task,
                    )
                    stream_handed_off = True
                    return downstream

                if prime.status == "client_disconnected":
                    return Response(status_code=499)

                if prime.status == "resource_mismatch":
                    if await activate_stateless_fallback("SSE 200 error", prime.error_message):
                        await asyncio.sleep((RETRY_DELAY_MS / 1000.0) * max(1, mismatch_attempts))
                        continue
                    return JSONResponse(
                        status_code=400,
                        content={"error": prime.error_payload or {"message": prime.error_message, "code": prime.error_code}},
                        headers=resp_headers,
                    )

                is_rl = prime.status == "retry_rate_limit"
                STATS["stream_precommit_retries"] += 1
                should_retry, suggested_ra = await schedule_transient_wait(
                    is_rl,
                    resp.headers,
                    prime.error_message,
                    "Rate Limit / SSE" if is_rl else "Stream Pre-Commit",
                )
                if should_retry:
                    continue
                if await _client_disconnected(request):
                    return Response(status_code=499)
                if is_rl:
                    logger.error("[Forwarding Rate Limit 429] Exhausted retries: %s", prime.error_message[:200])
                    return _rate_limit_error_response(
                        prime.error_payload, prime.error_message, prime.error_code, suggested_ra, resp_headers
                    )
                logger.error("[Forwarding Stream Error 502] %s", prime.error_message[:200])
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": prime.error_message or "Upstream stream failed before emitting output", "type": "proxy_error"}},
                )

            if resp.status_code == 200:
                with TIMINGS.measure("upstream_body"):
                    body = resp.content if resp.is_stream_consumed else await _await_upstream_work(request, resp.aread())
                if body is _CLIENT_DISCONNECTED:
                    return Response(status_code=499)
                await resp.aclose()
                parsed_body: Optional[Dict[str, Any]] = None
                try:
                    obj = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict):
                        parsed_body = obj
                except Exception:
                    pass

                if parsed_body is not None and (
                    bool(parsed_body.get("error")) or parsed_body.get("status") == "failed"
                ):
                    code, msg, err_obj = _extract_error_fields(parsed_body, "")
                    if 'session binding invalidated' in msg.lower():
                        note_attempt('binding_invalidated', status=200)
                    if is_resource_mismatch_text(msg):
                        if await activate_stateless_fallback("JSON 200 error", msg):
                            continue
                    elif _is_rate_limit_error(code, msg) or _is_transient_error(code, msg):
                        is_rl = _is_rate_limit_error(code, msg)
                        should_retry, suggested_ra = await schedule_transient_wait(
                            is_rl, resp.headers, msg, "Rate Limit / JSON" if is_rl else "JSON Error"
                        )
                        if should_retry:
                            continue
                        if is_rl:
                            return _rate_limit_error_response(err_obj, msg, code, suggested_ra, resp_headers)

                _clear_rate_limit_cooldown(rate_state, attempt_generation)
                if fallback_active or gzip_fallback_active or transient_retries > 0 or mismatch_attempts > 0:
                    STATS["successful_recoveries"] += 1
                    logger.info("[Success] Request recovered!")
                return Response(content=body, status_code=200, headers=resp_headers)

            # Non-200 HTTP status
            with TIMINGS.measure("upstream_body"):
                err_body = resp.content if resp.is_stream_consumed else await _await_upstream_work(request, resp.aread())
            if err_body is _CLIENT_DISCONNECTED:
                return Response(status_code=499)
            await resp.aclose()
            err_text = err_body.decode("utf-8", errors="replace")

            if (
                resp.status_code == 415
                and proxy_added_gzip
                and headers.get("content-encoding") == "gzip"
            ):
                # Retry only proxy-added encoding, using the current payload.
                # Keep subsequent retries and stateless recovery plain as well.
                gzip_fallback_active = True
                _record_gzip_rejection(source_headers)
                wire_content = await plain_content()
                proxy_added_gzip = False
                headers = source_headers
                STATS["gzip_fallbacks"] += 1
                note_attempt('gzip_recovery', status=415)
                logger.warning("[Request Compression] Upstream HTTP 415; retrying without proxy-added gzip")
                continue

            if 'session binding invalidated' in err_text.lower():
                note_attempt('binding_invalidated', status=resp.status_code)
            if is_resource_mismatch_error(resp.status_code, err_text):
                if await activate_stateless_fallback("Upstream 400", err_text):
                    if mismatch_attempts > 0:
                        await asyncio.sleep((RETRY_DELAY_MS / 1000.0) * mismatch_attempts)
                    continue
                logger.error("[Forwarding Error %d] %s", resp.status_code, err_text[:200])
                return Response(content=err_body, status_code=resp.status_code, headers=resp_headers)

            is_http_rl = resp.status_code == 429 or _is_rate_limit_error("", err_text)
            if resp.status_code in (429, 502, 503, 504):
                should_retry, suggested_ra = await schedule_transient_wait(
                    is_http_rl,
                    resp.headers,
                    err_text,
                    f"Gateway {resp.status_code}",
                )
                if should_retry:
                    continue
                if await _client_disconnected(request):
                    return Response(status_code=499)
                if resp.status_code == 429:
                    resp_headers["retry-after"] = str(suggested_ra)
                logger.error("[Forwarding Error %d] %s", resp.status_code, err_text[:200])
                return Response(content=err_body, status_code=resp.status_code, headers=resp_headers)

            logger.error("[Forwarding Error %d] %s", resp.status_code, err_text[:200])
            return Response(content=err_body, status_code=resp.status_code, headers=resp_headers)

        except (_BodyBudgetExpired, ClientDisconnect):
            raise
        except Exception as exc:
            description = describe_upstream_error(exc)
            logger.error("[Attempt Exception] %s", description)
            last_failure_response = JSONResponse(
                status_code=502,
                content={"error": {"message": f"Proxy upstream communication error: {description}", "type": "proxy_error"}},
            )
            should_retry, _ = await schedule_transient_wait(False, {}, description, "Attempt Exception")
            if should_retry:
                continue
            return last_failure_response
        finally:
            if resp is not None and not stream_handed_off:
                await _close_sse_producer(resp, None)

CONTROL = ControlRuntime(Path(__file__).resolve().parent, VERSION, PORT, health, diagnostics, TIMINGS,
    active_settings={
        'PROXY_REQUEST_GZIP': REQUEST_GZIP_ENABLED,
        'PROXY_REQUEST_GZIP_MIN_BYTES': REQUEST_GZIP_MIN_BYTES,
        'PROXY_GZIP_WORKERS': REQUEST_GZIP_WORKERS,
        'PROXY_GZIP_MAX_JOBS': REQUEST_GZIP_MAX_JOBS,
        'PROXY_GZIP_REJECT_TTL_S': REQUEST_GZIP_REJECT_TTL_S,
        'PROXY_GZIP_UPLOAD_MBIT_S': REQUEST_GZIP_UPLOAD_MBIT_S,
        'PROXY_GZIP_COST_MARGIN': REQUEST_GZIP_COST_MARGIN,
        'PROXY_CONNECT_TIMEOUT_S': CONNECT_TIMEOUT_S,
        'PROXY_READ_TIMEOUT_S': READ_TIMEOUT_S,
        'PROXY_VERBOSE': VERBOSE,
        'PROXY_BODY_BUDGET_BYTES': BODY_BUDGET_BYTES,
        'PROXY_BODY_BUDGET_WAIT_S': BODY_BUDGET_WAIT_S,
        'PROXY_RATE_LIMIT_BASE_S': RATE_LIMIT_BASE_DELAY_S,
        'PROXY_RATE_LIMIT_MAX_DELAY_S': RATE_LIMIT_MAX_DELAY_S,
        'PROXY_MAX_RATE_LIMIT_WAIT_S': MAX_RATE_LIMIT_WAIT_S,
        'PROXY_MAX_TRANSIENT_RETRIES': MAX_TRANSIENT_RETRIES,
        'PROXY_RECOVERY_STAGGER_S': RECOVERY_STAGGER_S,
        'PROXY_RATE_LIMIT_SCOPE': RATE_LIMIT_SCOPE,
        'PROXY_RATE_LIMIT_PARTITIONS': RATE_LIMIT_MAX_PARTITIONS,
        'PROXY_SSE_BUFFER_BYTES': SSE_BUFFER_MAX_BYTES,
        'PROXY_SSE_PRIME_BYTES': SSE_PRIME_MAX_BYTES,
        'PROXY_SSE_PRIME_FRAMES': SSE_PRIME_MAX_FRAMES,
        'PROXY_SSE_PRIME_FIRST_TIMEOUT_S': SSE_PRIME_FIRST_TIMEOUT_S,
        'PROXY_SSE_PRIME_CREATED_GRACE_S': SSE_PRIME_CREATED_GRACE_S,
        'PROXY_TIMINGS': TIMINGS.always_enabled,
    })
attach_control(app, CONTROL)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def passthrough_handler(request: Request, path: str):
    """Passthrough for models listing or any other API routes."""
    STATS["requests_total"] += 1

    headers = _filter_headers(dict(request.headers))
    try:
        body = await request.body()
    except ClientDisconnect:
        STATS["client_disconnects_during_upload"] += 1
        return Response(status_code=499)

    clean_path = path.lstrip("/")
    if not clean_path.startswith("v1/"):
        target_url = f"{UPSTREAM}/v1/{clean_path}"
    else:
        target_url = f"{UPSTREAM}/{clean_path}"

    try:
        client = get_shared_client()
        pending_response = await _await_upstream_work(request, _send_with_transport_retry(
            client,
            method=request.method,
            url=target_url,
            headers=headers,
            params=dict(request.query_params),
            content=body,
            stream=False,
            max_retries=5,
        ))
        if pending_response is _CLIENT_DISCONNECTED:
            return Response(status_code=499)
        resp = pending_response
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in STRIP_RESPONSE_HEADERS
        }
        return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
    except Exception as exc:
        description = describe_upstream_error(exc)
        logger.error("[Passthrough Error] %s -> %s: %s", request.method, target_url, description)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Proxy passthrough error: {description}", "type": "proxy_error"}}
        )

# ----------------------------------------------------------------------------
# Main entrypoint
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    cli_args = _parse_proxy_cli_args(default_host=LISTEN_HOST, default_port=PORT)
    LISTEN_HOST = cli_args.host
    PORT = cli_args.port
    os.environ["PROXY_PORT"] = str(PORT)

    bind_scope = "loopback only" if is_loopback_host(LISTEN_HOST) else "non-loopback -- firewall required"
    url_host = f"[{LISTEN_HOST}]" if ":" in LISTEN_HOST and not LISTEN_HOST.startswith("[") else LISTEN_HOST
    print(f"[*] Starting agentrouter-codex-proxy on http://{url_host}:{PORT} ({bind_scope})")
    print(f"[*] Forwarding to upstream: {UPSTREAM}")
    uvicorn.run(app, host=LISTEN_HOST, port=PORT, timeout_keep_alive=600, log_level="info")
