"""Compare gzip policy using credential-free paced upstream reading.

The upstream paces reading each request and adds a fixed pre-header delay.
This is a controlled software-shaped path, not an Internet speed measurement.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack, ExitStack
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time

bench = None

ROOT = None
PROFILES = ("baseline", "candidate")
PHASES = (("slow", 20, 80), ("medium", 100, 25), ("fast", 1000, 0))
FIXTURES = ("small", "english_history", "tool_history", "tool_history_415")


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_hashes():
    expected = json.loads((ROOT / 'inputs.json').read_text(encoding='utf-8'))['runtime_sha256']
    return {profile: {name: digest(ROOT / profile / name) for name in files}
            for profile, files in expected.items()}


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def serve(args):
    sys.path.insert(0, str(ROOT / args.profile))
    proxy = bench.load_proxy(args.profile, args.port)
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Mount, Route
    import uvicorn

    counters = Counter()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in bench.fixtures().items()}

    def frame(event, data):
        return ("event: " + event + "\ndata: " + json.dumps(data, separators=(",", ":")) + "\n\n").encode()

    async def upstream(request):
        counters["requests"] += 1
        content = bytearray()
        started = time.perf_counter()
        async for chunk in request.stream():
            content.extend(chunk)
            scheduled = started + len(content) * 8 / (args.rate * 1e6)
            wait = scheduled - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
        if args.delay:
            await asyncio.sleep(args.delay / 1000)
        received_ns = time.perf_counter_ns()
        name = request.headers["x-bench-fixture"]
        encoded = request.headers.get("content-encoding") == "gzip"
        counters["wire_bytes"] += len(content)
        if encoded and name.endswith("_415"):
            counters["gzip_rejections"] += 1
            return JSONResponse({"error": {"message": "synthetic gzip rejection"}}, status_code=415)
        decoded = gzip.decompress(content) if encoded else content
        counters["gzip_successes" if encoded else "plain_successes"] += 1
        digest_value = hashlib.sha256(decoded).hexdigest()
        if digest_value != hashes[name]:
            return JSONResponse({"error": {"message": "unexpected decoded bytes"}}, status_code=422)
        validation_ms = (time.perf_counter_ns() - received_ns) / 1e6

        async def events():
            yield frame("response.created", {"type": "response.created", "response": {"id": "synthetic", "status": "in_progress"}})
            emitted_ns = time.perf_counter_ns()
            yield frame("response.output_text.delta", {"type": "response.output_text.delta", "delta": "OK", "benchmark": {
                "received_ns": received_ns, "emitted_ns": emitted_ns, "wire_bytes": len(content),
                "validation_ms": validation_ms, "body_preserved": digest_value == request.headers["x-bench-body-sha256"],
            }})
            yield frame("response.completed", {"type": "response.completed", "response": {"id": "synthetic", "status": "completed", "output": []}})

        return StreamingResponse(events(), media_type="text/event-stream")

    async def metadata(request):
        client = proxy._SHARED_CLIENT
        pool = getattr(getattr(client, "_transport", None), "_pool", None)
        return JSONResponse({"health": await proxy.health(), "upstream_requests": len(getattr(pool, "_requests", [])),
                             "upstream_counts": dict(counters), "shaped_upload_mbit_s": args.rate,
                             "added_header_delay_ms": args.delay})

    gateway = Starlette(routes=[Route("/_benchmark_metadata", metadata),
        Mount("/_synthetic_upstream", Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])),
        Mount("/", proxy.app)], lifespan=proxy._app_lifespan)
    uvicorn.Server(uvicorn.Config(gateway, host="127.0.0.1", port=args.port,
        http="httptools", log_level="warning", access_log=False, timeout_keep_alive=600)).run()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def ready(client, endpoint, process):
    deadline = time.monotonic() + 30
    while True:
        try:
            response = await client.get(endpoint + "/_benchmark_metadata")
            if response.status_code == 200:
                return response.json()
        except bench.httpx.TransportError:
            pass
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Shaped benchmark server did not become ready")
        await asyncio.sleep(.05)


async def phase(folder, name, rate, delay, count):
    processes, endpoints = {}, {}
    bodies = bench.fixtures()
    pairs = {fixture: [] for fixture in FIXTURES}
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with ExitStack() as logs:
            ports = set()
            for profile in PROFILES:
                port = bench.free_port()
                while port in ports:
                    port = bench.free_port()
                ports.add(port)
                endpoints[profile] = f"http://127.0.0.1:{port}"
                log = logs.enter_context((folder / (name + "-" + profile + ".log")).open("wb"))
                processes[profile] = subprocess.Popen([sys.executable, str(Path(__file__)), "--serve",
                    "--profile", profile, "--port", str(port), "--rate", str(rate), "--delay", str(delay)],
                    stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            async with AsyncExitStack() as stack:
                clients = {profile: await stack.enter_async_context(bench.httpx.AsyncClient(timeout=30, trust_env=False)) for profile in PROFILES}
                await asyncio.gather(*(ready(clients[p], endpoints[p], processes[p]) for p in PROFILES))
                # Three real unencoded large uploads supply the candidate's lower-bound hint.
                for index in range(3):
                    for profile in PROFILES if index % 2 == 0 else tuple(reversed(PROFILES)):
                        await bench.one_request(clients[profile], endpoints[profile], "thirty_images", bodies["thirty_images"])
                for fixture in FIXTURES:
                    for profile in PROFILES:
                        for _ in range(2):
                            await bench.one_request(clients[profile], endpoints[profile], fixture, bodies[fixture])
                metadata_before = {p: (await clients[p].get(endpoints[p] + "/_benchmark_metadata")).json() for p in PROFILES}
                rng = random.Random(20261114 + rate)
                for index in range(count):
                    names = list(FIXTURES)
                    rng.shuffle(names)
                    for fixture in names:
                        order = PROFILES if index % 2 == 0 else tuple(reversed(PROFILES))
                        pair = {"index": index + 1, "fixture": fixture, "order": list(order)}
                        for profile in order:
                            pair[profile] = await bench.one_request(clients[profile], endpoints[profile], fixture, bodies[fixture])
                            if not pair[profile]["body_preserved"]:
                                raise RuntimeError("Shaped upstream hash verification failed")
                        pairs[fixture].append(pair)
                metadata_after = {p: (await clients[p].get(endpoints[p] + "/_benchmark_metadata")).json() for p in PROFILES}
                if any(value["upstream_requests"] for value in metadata_after.values()):
                    raise RuntimeError("Shaped benchmark left requests in the pool")
        variants = {p: {"fixtures": {fixture: {
            "first_output_ms": bench.timing([pair[p]["first_output_ms"] for pair in values]),
            "wire_bytes": {str(size): sum(pair[p]["wire_bytes"] == size for pair in values)
                           for size in sorted({pair[p]["wire_bytes"] for pair in values})},
            "body_preserved": all(pair[p]["body_preserved"] for pair in values),
        } for fixture, values in pairs.items()}} for p in PROFILES}
        result = {"phase": name, "shaped_upload_mbit_s": rate, "added_header_delay_ms": delay,
                  "variants": variants, "pairs": pairs, "metadata_before": metadata_before,
                  "metadata_after": metadata_after}
        save(folder / (name + ".json"), result)
        print(json.dumps({"phase": name, "rate": rate, "delay": delay,
            "fixtures": {fixture: {p: variants[p]["fixtures"][fixture] for p in PROFILES} for fixture in FIXTURES}}), flush=True)
        return result
    finally:
        for process in processes.values():
            stop(process)


async def main(args):
    folder = ROOT
    sources = runtime_hashes()
    receipt = {"status": "running", "started_utc": utc(), "runtime_sha256": sources,
        "benchmark_sha256": digest(Path(__file__)), "server_helper_sha256": digest(Path(__file__).with_name("_http_fixture.py")),
        "real_model_calls": 0, "pairs_per_fixture_per_phase": args.samples,
        "method": "Separate continuously warm proxy processes; adjacent AB/BA pairs; upstream paces request reading and delays response headers; real HTTPX sockets"}
    save(folder / "receipt.json", receipt)
    results = {}
    try:
        for name, rate, delay in PHASES:
            if name not in args.phases:
                continue
            results[name] = await phase(folder, name, rate, delay, args.samples)
            receipt.update(completed_phases=list(results), checked_utc=utc())
            save(folder / "receipt.json", receipt)
        if sources != runtime_hashes():
            raise RuntimeError("A frozen source changed during the shaped benchmark")
        receipt.update(status="passed", finished_utc=utc(), source_unchanged=True)
        save(folder / "comparison.json", {"runtime_sha256": sources, "phases": {
            name: {key: value for key, value in result.items() if key != "pairs"} for name, result in results.items()}})
        save(folder / "receipt.json", receipt)
        print(json.dumps({"status": "passed", "result": str(folder / "comparison.json")}), flush=True)
    except BaseException as error:
        receipt.update(status="failed", finished_utc=utc(), error=type(error).__name__ + ": " + str(error))
        save(folder / "receipt.json", receipt)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--profile", choices=PROFILES, default="candidate")
    parser.add_argument("--port", type=int, default=12345)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--delay", type=float, default=80)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--output", type=Path, help="New result directory; existing data is preserved")
    parser.add_argument("--baseline-ref", default="f112857586c0ec01043ddd608b87d597c5e28da2")
    parser.add_argument("--phases", nargs='+', choices=[value[0] for value in PHASES],
                        default=[value[0] for value in PHASES])
    args = parser.parse_args()
    if (args.samples < 2 or args.samples % 2 or args.rate <= 0 or args.delay < 0
            or not math.isfinite(args.rate) or not math.isfinite(args.delay)):
        parser.error("Use a positive rate, nonnegative delay, and positive even sample count")
    for name in list(os.environ):
        if name.upper().startswith(("PROXY_", "AGENTROUTER_")) or name.upper() in (
                "CODEX_GATEWAY_API_KEY", "OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            del os.environ[name]
    os.environ.update(PROXY_AUTO_UPDATE="0", PROXY_VERBOSE="0", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    if args.serve:
        ROOT = Path(os.environ["CODEX_PROXY_BENCH_ROOT"]).resolve()
    else:
        from compare import snapshot_runtime
        repository = Path(__file__).resolve().parents[1]
        ROOT = (args.output or repository / 'benchmark-results' /
                datetime.now(timezone.utc).strftime('paced-%Y%m%dT%H%M%S%fZ')).resolve()
        if ROOT.exists():
            parser.error("Output already exists; choose a new directory")
        try:
            runtimes = {profile: snapshot_runtime(repository / 'proxy.py', repository, revision)
                        for profile, revision in (("baseline", args.baseline_ref), ("candidate", None))}
        except (OSError, SyntaxError, subprocess.TimeoutExpired):
            parser.error("Cannot freeze complete runtimes; use a full Git clone with the baseline commit")
        ROOT.mkdir(parents=True)
        hashes = {}
        for profile, files in runtimes.items():
            (ROOT / profile).mkdir()
            for name, content in files.items():
                (ROOT / profile / name).write_bytes(content)
            hashes[profile] = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
        save(ROOT / 'inputs.json', {'baseline_ref': args.baseline_ref, 'runtime_sha256': hashes,
                                   'real_model_calls': 0})
        os.environ['CODEX_PROXY_BENCH_ROOT'] = str(ROOT)
        os.environ['CODEX_PROXY_BENCH_GZIP'] = '1'
    import _http_fixture as bench
    if args.serve:
        serve(args)
    else:
        asyncio.run(main(args))
