"""Frozen-revision controls for incoming allocations and concurrent loopback TCP.

Memory runs use tracemalloc; timing runs do not. The proxy process is separate
from upload clients. All payloads are synthetic and upstream bytes are verified.
No provider requests, production listeners or configuration writes occur.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timing(values):
    ordered = sorted(values)
    return {'samples': len(values), 'median_ms': statistics.median(ordered),
            'p95_ms': ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)]}


def clean_environment():
    environment = os.environ.copy()
    for name in list(environment):
        if name.upper().startswith(('PROXY_', 'AGENTROUTER_')) or name.upper() in (
                'CODEX_GATEWAY_API_KEY', 'OPENAI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            del environment[name]
    environment.update(PROXY_AUTO_UPDATE='0', PROXY_VERBOSE='0', PYTHONUTF8='1',
                       PYTHONDONTWRITEBYTECODE='1', AGENTROUTER_UPSTREAM='http://127.0.0.1:9')
    return environment


def load(args):
    sys.path.insert(0, str(args.source))
    import proxy
    return proxy


async def assemble(args):
    from starlette.requests import Request
    proxy = load(args)
    unit = b'0123456789ABCDEF' * 4096
    results = []
    for size_mib in (1, 4, 16, 32):
        size = size_mib * 2**20
        expected = hashlib.sha256(unit * (size // len(unit))).hexdigest()
        for known in (True, False):
            for index in range(args.samples):
                left = size
                async def receive():
                    nonlocal left
                    if left:
                        chunk = bytes(bytearray(unit))
                        left -= len(chunk)
                        return {'type':'http.request', 'body':chunk, 'more_body':bool(left)}
                    return {'type':'http.request', 'body':b'', 'more_body':False}
                headers = [(b'content-length', str(size).encode())] if known else []
                request = Request({'type':'http','method':'POST','path':'/v1/responses','headers':headers}, receive)
                lease = proxy._BodyLease() if hasattr(proxy, '_BodyLease') else None
                gc.collect()
                if args.mode == 'memory':
                    tracemalloc.start(1)
                start = time.perf_counter()
                try:
                    body = await proxy._read_request_body(request, lease) if lease else await request.body()
                    elapsed = (time.perf_counter() - start) * 1000
                    current, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (None, None)
                    assert len(body) == size and hashlib.sha256(body).hexdigest() == expected
                    results.append({'size_mib':size_mib,'known_length':known,'sample':index,
                                    'assembly_ms':elapsed if args.mode == 'timing' else None,
                                    'current_python_bytes':current,'peak_python_bytes':peak})
                finally:
                    if tracemalloc.is_tracing():
                        tracemalloc.stop()
                    if lease:
                        lease.release()
                    if hasattr(request, '_body'):
                        request._body = b''
                    body = None
    write(args.result, {'status':'passed','mode':args.mode,'samples':results,'provider_calls':0})


def serve(args):
    os.environ.update(PROXY_PORT=str(args.port), PROXY_REQUEST_GZIP='0',
        PROXY_BODY_BUDGET_BYTES=str(args.budget), PROXY_HISTORY_ENABLED='1',
        AGENTROUTER_UPSTREAM=f'http://127.0.0.1:{args.port}/_synthetic_upstream')
    proxy = load(args)
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from telemetry import process_memory

    state = {'uploaded':0,'active_large':0,'max_active_large':0,'invalid':0}
    async def upstream(request):
        check = hashlib.sha256()
        async for chunk in request.stream():
            check.update(chunk)
        if check.hexdigest() != request.headers.get('x-bench-sha256'):
            state['invalid'] += 1
            return JSONResponse({'error':{'message':'synthetic byte mismatch'}}, status_code=422)
        if request.headers.get('x-bench-large') == '1':
            state['uploaded'] += 1
            state['active_large'] += 1
            state['max_active_large'] = max(state['max_active_large'], state['active_large'])
            try:
                await asyncio.sleep(float(request.headers['x-bench-hold']))
            finally:
                state['active_large'] -= 1
        return JSONResponse({'ok':True})

    async def metadata(request):
        if request.query_params.get('reset') == '1':
            state.update(uploaded=0,active_large=0,max_active_large=0,invalid=0)
            gc.collect()
            if args.mode == 'memory':
                tracemalloc.start(1)
        current, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (None,None)
        body = proxy._body_budget().snapshot() if hasattr(proxy, '_body_budget') else None
        return JSONResponse({**state,'body_memory':body,'memory':process_memory(),
                             'current_python_bytes':current,'peak_python_bytes':peak})

    app = Starlette(routes=[Route('/_benchmark_metadata',metadata),
        Mount('/_synthetic_upstream',Starlette(routes=[Route('/v1/responses',upstream,methods=['POST'])])),
        Mount('/',proxy.app)],lifespan=proxy._app_lifespan)
    uvicorn.run(app,host='127.0.0.1',port=args.port,http='httptools',ws='none',
                log_level='warning',access_log=False,timeout_keep_alive=60)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)


async def concurrent(args, source, folder, profile, mode, epoch, budget):
    import httpx
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port = reservation.getsockname()[1]
    assert port not in (8788,8789)
    label = f'{profile}-{mode}-{epoch}'
    with (folder / (label + '.log')).open('x',encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker','server',
            '--source',str(source),'--port',str(port),'--budget',str(budget),'--mode',mode],
            env=clean_environment(),cwd=source,stdout=log,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            endpoint = f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient(base_url=endpoint,trust_env=False,timeout=60,
                    limits=httpx.Limits(max_connections=64,max_keepalive_connections=64)) as client:
                until = time.monotonic() + 20
                while True:
                    try:
                        ready = await client.get('/_benchmark_metadata',timeout=.5)
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None or time.monotonic() > until:
                        raise RuntimeError('Synthetic worker did not become ready; inspect its log')
                    await asyncio.sleep(.05)
                small = b'{"model":"synthetic","input":"OK"}'
                small_hash = hashlib.sha256(small).hexdigest()
                headers = {'content-type':'application/json','authorization':'Bearer synthetic',
                           'x-bench-sha256':small_hash}
                for _ in range(6):
                    response = await client.post('/v1/responses',content=small,headers=headers)
                    assert response.status_code == 200
                large = b'{"model":"synthetic","input":"' + b'A' * (args.size_mib * 2**20) + b'"}'
                large_hash = hashlib.sha256(large).hexdigest()
                initial = (await client.get('/_benchmark_metadata?reset=1')).json()
                samples, small_times, large_times = [], [], []
                finished = asyncio.Event()
                async def large_request():
                    async def chunks():
                        for offset in range(0,len(large),65536):
                            yield large[offset:offset+65536]
                            await asyncio.sleep(0)
                    start = time.perf_counter()
                    response = await client.post('/v1/responses',content=chunks(),headers={
                        **headers,'content-length':str(len(large)),'x-bench-sha256':large_hash,
                        'x-bench-large':'1','x-bench-hold':str(args.hold_s)})
                    assert response.status_code == 200, response.status_code
                    large_times.append((time.perf_counter()-start)*1000)
                async def watch():
                    while not finished.is_set():
                        start = time.perf_counter()
                        response = await client.post('/v1/responses',content=small,headers=headers)
                        assert response.status_code == 200
                        small_times.append((time.perf_counter()-start)*1000)
                        samples.append((await client.get('/_benchmark_metadata')).json())
                        await asyncio.sleep(.01)
                tasks = [asyncio.create_task(large_request()) for _ in range(args.parallel)]
                monitor = asyncio.create_task(watch())
                start = time.perf_counter()
                try:
                    await asyncio.gather(*tasks)
                finally:
                    finished.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    await monitor
                elapsed = time.perf_counter() - start
                final = (await client.get('/_benchmark_metadata')).json()
                assert final['uploaded'] == args.parallel and final['invalid'] == 0
                assert final['active_large'] == 0
                if final['body_memory']:
                    assert final['body_memory']['reserved_bytes'] == 0 and final['body_memory']['waiters'] == 0
                all_samples = [initial,*samples,final]
                receipt = {'status':'passed','profile':profile,'epoch':epoch,'mode':mode,
                    'size_bytes':len(large),'parallel':args.parallel,'hold_s':args.hold_s,'budget_bytes':budget,
                    'max_active_large':max(value['max_active_large'] for value in all_samples),
                    'final':final,'provider_calls':0,'body_hash_checked':True,
                    'peak_python_bytes':max((value['peak_python_bytes'] or 0) for value in all_samples) if mode=='memory' else None,
                    'sampled_peak_rss_bytes':max(value['memory']['rss_bytes'] for value in all_samples),
                    'sampled_peak_private_bytes':max((value['memory']['private_bytes'] or 0) for value in all_samples),
                    'initial_memory':initial['memory'],'metadata_samples':len(samples),
                    'small_latency':timing(small_times) if mode=='timing' else None,
                    'large_latency':timing(large_times) if mode=='timing' else None,
                    'makespan_s':elapsed if mode=='timing' else None}
                write(folder / (label + '.json'),receipt)
                return receipt
        finally:
            stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref',default='bbad7506f5cda85df5fad5781c508bdb78005854')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--scenario',choices=('assembly','concurrent','all'),default='all')
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--epochs',type=int,default=2)
    parser.add_argument('--size-mib',type=int,default=32)
    parser.add_argument('--parallel',type=int,default=8)
    parser.add_argument('--hold-s',type=float,default=1)
    parser.add_argument('--worker',choices=('assembly','server'))
    parser.add_argument('--source',type=Path)
    parser.add_argument('--result',type=Path)
    parser.add_argument('--mode',choices=('memory','timing'),default='memory')
    parser.add_argument('--port',type=int)
    parser.add_argument('--budget',type=int,default=268435456)
    args = parser.parse_args()
    if args.worker:
        if args.worker == 'assembly':
            asyncio.run(assemble(args))
        else:
            assert args.port not in (8788,8789)
            serve(args)
        return
    if not args.output or args.samples < 1 or args.epochs < 1 or args.parallel < 1 or args.size_mib < 1 or args.hold_s <= 0:
        parser.error('Select a new output directory and positive sample/size/concurrency values')
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(source / 'benchmarks'))
    from compare import snapshot_runtime
    folder = args.output.resolve()
    folder.mkdir(parents=True,exist_ok=False)
    snapshots = {'baseline':snapshot_runtime(source/'proxy.py',source,args.baseline_ref),
                 'candidate':snapshot_runtime(source/'proxy.py',source)}
    runtime_hashes = {}
    for profile, files in snapshots.items():
        (folder / profile).mkdir()
        for name, value in files.items():
            (folder / profile / name).write_bytes(value)
        runtime_hashes[profile] = {name:hashlib.sha256(value).hexdigest() for name,value in files.items()}
    receipt = {'status':'running','started_at':datetime.now(timezone.utc).isoformat(),
        'baseline_commit':args.baseline_ref,'runtime_sha256':runtime_hashes,'provider_calls':0,
        'python':platform.python_version(),'platform':platform.platform(),
        'assembly':[],'concurrent':[],'harness_sha256':digest(Path(__file__))}
    write(folder/'receipt.json',receipt)
    try:
        if args.scenario in ('all','assembly'):
            for mode in ('memory','timing'):
                for profile in ('baseline','candidate'):
                    result = folder / f'assembly-{profile}-{mode}.json'
                    with (folder / f'assembly-{profile}-{mode}.log').open('x',encoding='utf-8') as log:
                        subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','assembly',
                            '--source',str(folder/profile),'--result',str(result),'--mode',mode,'--samples',str(args.samples)],
                            env=clean_environment(),cwd=folder/profile,stdout=log,stderr=subprocess.STDOUT,
                            timeout=90,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    receipt['assembly'].append({'profile':profile,**json.loads(result.read_text())})
                    print(json.dumps({'step':'assembly','profile':profile,'mode':mode}),flush=True)
        if args.scenario in ('all','concurrent'):
            for mode in ('memory','timing'):
                for epoch in range(args.epochs):
                    profiles = [('baseline','baseline',0),('candidate_unlimited','candidate',0),('candidate_budget','candidate',args.budget)]
                    if epoch % 2:
                        profiles.reverse()
                    for label, profile, budget in profiles:
                        value = asyncio.run(concurrent(args,folder/profile,folder,label,mode,epoch,budget))
                        receipt['concurrent'].append(value)
                        write(folder/'receipt.json',receipt)
                        print(json.dumps({'step':'concurrent','profile':label,'mode':mode,'epoch':epoch,
                                          'peak_python_bytes':value['peak_python_bytes'],'small_latency':value['small_latency']}),flush=True)
        after = {profile:{name:digest(folder/profile/name) for name in files} for profile,files in snapshots.items()}
        assert after == runtime_hashes
        receipt.update(status='passed',finished_at=datetime.now(timezone.utc).isoformat(),sources_unchanged=True)
        write(folder/'receipt.json',receipt)
    except BaseException:
        receipt.update(status='failed',finished_at=datetime.now(timezone.utc).isoformat())
        write(folder/'receipt.json',receipt)
        raise


if __name__ == '__main__':
    main()
