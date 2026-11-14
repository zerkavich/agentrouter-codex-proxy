"""Measure cached-body TCP disconnect cleanup; requires matching proxy modules."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--samples', type=int, default=42)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if args.samples < 1: parser.error('samples must be positive')
if args.output.exists(): parser.error('output exists; choose a new file')

async def measure():
    for name in list(os.environ):
        if name.upper().startswith(('PROXY_', 'AGENTROUTER_')) or name.upper() in (
                'OPENAI_API_KEY', 'CODEX_GATEWAY_API_KEY','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
            del os.environ[name]
    os.environ.update(PROXY_VERBOSE='0', PROXY_REQUEST_GZIP='0', PROXY_TIMINGS='0',
                      PROXY_AUTO_UPDATE='0', AGENTROUTER_UPSTREAM='http://127.0.0.1:9')
    source = args.source.resolve()
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location('proxy', source)
    proxy = importlib.util.module_from_spec(spec)
    sys.modules['proxy'] = proxy
    spec.loader.exec_module(proxy)
    import httpx
    from starlette.requests import Request
    arrived, released = asyncio.Event(), asyncio.Event()
    writers = []
    async def serve(reader, writer):
        writers.append(writer)
        try:
            await reader.readuntil(b'\r\n\r\n')
            arrived.set()
            await reader.read()
        finally:
            released.set()
            writer.close()
    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    rows = []
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
            for index in range(args.samples):
                first = True
                abandoned = asyncio.Event()
                arrived.clear(); released.clear()
                async def receive():
                    nonlocal first
                    if first:
                        first = False
                        return {'type':'http.request','body':b'{}','more_body':False}
                    await abandoned.wait()
                    return {'type':'http.disconnect'}
                request = Request({'type':'http','method':'POST','headers':[]}, receive)
                await request.body()
                task = asyncio.create_task(proxy._await_upstream_work(request,
                    proxy._send_with_transport_retry(client, 'GET', f'http://127.0.0.1:{port}/pending',
                                                      {}, max_retries=1)))
                await asyncio.wait_for(arrived.wait(), 3)
                delay = (index % 7) * .05
                await asyncio.sleep(delay)
                began = time.perf_counter()
                abandoned.set()
                result = await asyncio.wait_for(task, 2)
                assert result is proxy._CLIENT_DISCONNECTED
                await asyncio.wait_for(released.wait(), 1)
                duration = (time.perf_counter() - began) * 1000
                assert not client._transport._pool._requests
                assert proxy._get_upstream_capacity(client).in_use == 0
                rows.append({'disconnect_offset_ms': delay * 1000, 'release_ms': duration})
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    values = sorted(row['release_ms'] for row in rows)
    receipt = {'status':'passed', 'real_provider_calls':0, 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
               'samples':rows, 'median_ms':statistics.median(values), 'p95_ms':values[(len(values)*95+99)//100-1],
               'pool_released_each_sample':True, 'method':'Cached client body, real pending TCP headers, staggered disconnects'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({key:value for key,value in receipt.items() if key != 'samples'}), flush=True)

asyncio.run(measure())
