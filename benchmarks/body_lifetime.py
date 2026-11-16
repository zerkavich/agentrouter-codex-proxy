"""Compare large upload lifetimes over real, credential-free loopback TCP.

Memory runs report live Python allocations, not RSS. Timing runs disable
tracemalloc. No provider calls, production ports or configuration changes occur.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import tracemalloc


CASES = ("large_image", "thirty_images", "large_text", "resource_mismatch",
         "resource_mismatch_text", "resource_mismatch_history")
VARIANTS = ("baseline", "candidate")


def fixture(case, size):
    if case in ("resource_mismatch_text", "resource_mismatch_history"):
        prefix = b'{"model":"synthetic","stream":true,"input":'
        if case == "resource_mismatch_text":
            unit = 'История текста 🌍. '.encode('utf-8')
            content = b'"' + unit * (size // len(unit)) + b'"'
        else:
            message = (b'{"type":"message","role":"user","content":[{"type":"input_text","text":"'
                       + b"A" * 4000 + b'"}]}')
            content = b'[' + b','.join([message] * (size // len(message))) + b']'
        body = prefix + content + b',"previous_response_id":"resp_synthetic"}'
        expected = hashlib.sha256()
        expected.update(prefix)
        expected.update(content)
        expected.update(b'}')
        return body, expected.hexdigest()
    if case == "thirty_images":
        image = b'{"type":"input_image","image_url":"data:image/png;base64,' + b"A" * (size // 30) + b'"}'
        body = (b'{"model":"synthetic","stream":true,"input":[{"type":"message","role":"user","content":['
                + b",".join([image] * 30) + b']}]}')
        return body, hashlib.sha256(body).hexdigest()
    if case == "large_text":
        body = b'{"model":"synthetic","stream":true,"input":"' + b"A" * size + b'"}'
        return body, hashlib.sha256(body).hexdigest()
    prefix = (b'{"model":"synthetic","stream":true,"input":[{"type":"message","role":"user",'
              b'"content":[{"type":"input_image","image_url":"data:image/png;base64,')
    suffix = b'"}]}]}'
    if case == "resource_mismatch":
        state_suffix = b'"}]}],"previous_response_id":"resp_synthetic"}'
        body = prefix + b"A" * size + state_suffix
        expected = hashlib.sha256()
        expected.update(memoryview(body)[:-len(state_suffix)])
        expected.update(suffix)
        return body, expected.hexdigest()
    body = prefix + b"A" * size + suffix
    return body, hashlib.sha256(body).hexdigest()


async def worker(args):
    sys.path.insert(0, str(args.source))
    import httpx
    from starlette.requests import Request
    import proxy

    writers = []
    uploaded = asyncio.Event()
    send_headers = asyncio.Event()
    uploads = []
    current_case = None

    async def serve(reader, writer):
        writers.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            if headers.startswith(b"GET /warmup "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
                return
            fields = {name.lower(): value.strip() for name, value in
                      (line.split(b":", 1) for line in headers.split(b"\r\n")[1:] if b":" in line)}
            remaining = int(fields[b"content-length"])
            if b"transfer-encoding" in fields:
                raise RuntimeError("Unexpected chunked request upload")
            digest = hashlib.sha256()
            while remaining:
                chunk = await reader.readexactly(min(remaining, 65536))
                digest.update(chunk)
                remaining -= len(chunk)
            uploads.append(digest.hexdigest())
            if current_case.startswith("resource_mismatch") and len(uploads) == 1:
                error = b'{"error":{"message":"The requested item was created under a different OpenAI resource."}}'
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(error)).encode() + b"\r\nConnection: close\r\n\r\n" + error)
                await writer.drain()
                return
            uploaded.set()
            await send_headers.wait()
            first = b'event: response.output_text.delta\ndata: {"delta":"synthetic"}\n\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                         + f"{len(first):x}\r\n".encode() + first + b"\r\n")
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    proxy.UPSTREAM = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    proxy.REQUEST_GZIP_ENABLED = False
    client = httpx.AsyncClient(timeout=30, trust_env=False)
    proxy._SHARED_CLIENT = client
    result = {}
    timings = []
    try:
        warm = await client.get(proxy.UPSTREAM + "/warmup")
        warm.raise_for_status()
        for iteration in range(args.timing_samples + 1):
            mode = "memory" if iteration == 0 else "timing"
            current_case = args.case
            uploads.clear()
            uploaded.clear()
            send_headers.clear()
            response = None
            handler = None
            gc.collect()
            if mode == "memory":
                tracemalloc.start(1)
            try:
                body, expected_hash = fixture(args.case, args.size_mib * 1024 * 1024)
                input_hash = hashlib.sha256(body).hexdigest()
                input_size = len(body)
                unread = [body]
                del body

                async def receive():
                    if unread:
                        return {"type": "http.request", "body": unread.pop(), "more_body": False}
                    await asyncio.Event().wait()

                request = Request({"type": "http", "method": "POST", "path": "/v1/responses",
                                   "headers": [(b"content-type", b"application/json")], "query_string": b""}, receive)
                if mode == "memory":
                    tracemalloc.reset_peak()
                started = time.perf_counter()
                handler = asyncio.create_task(proxy.handle_responses(request))
                await asyncio.wait_for(uploaded.wait(), 30)
                uploaded_ms = (time.perf_counter() - started) * 1000
                memory = {}
                if mode == "memory":
                    gc.collect()
                    waiting, peak = tracemalloc.get_traced_memory()
                    memory.update(waiting_headers_bytes=waiting, peak_to_headers_bytes=peak)
                send_headers.set()
                response = await asyncio.wait_for(handler, 30)
                ready_ms = (time.perf_counter() - started) * 1000
                if not isinstance(response, proxy.UpstreamStreamingResponse):
                    raise RuntimeError("Expected an open SSE response")
                if mode == "memory":
                    gc.collect()
                    streaming, peak = tracemalloc.get_traced_memory()
                    memory.update(committed_stream_bytes=streaming, peak_bytes=peak)
                expected_uploads = ([input_hash, expected_hash] if args.case.startswith("resource_mismatch") else [input_hash])
                if uploads != expected_uploads or response.upstream_response.is_closed:
                    raise RuntimeError("Upload hash or open-stream verification failed")
                sample = {"input_bytes": input_size, "input_sha256": input_hash,
                          "final_upload_sha256": expected_hash, "upstream_attempts": len(uploads),
                          "hashes_verified": True, **memory}
                if mode == "timing":
                    sample.update(final_upload_ms=uploaded_ms, response_ready_ms=ready_ms)
                    timings.append(sample)
                else:
                    result[mode] = sample
            finally:
                send_headers.set()
                if handler is not None and not handler.done():
                    handler.cancel()
                    await asyncio.gather(handler, return_exceptions=True)
                if isinstance(response, proxy.UpstreamStreamingResponse):
                    await proxy._close_sse_producer(response.upstream_response, response.producer_task)
                if tracemalloc.is_tracing():
                    tracemalloc.stop()
                # Release the completed measurement's request/task as a real
                # ASGI call would; the next timing run starts without its body.
                response = handler = request = None
        result["timing"] = {**timings[0], "samples": len(timings),
            **{key: statistics.median(sample[key] for sample in timings)
               for key in ("final_upload_ms", "response_ready_ms")}}
        result["timing_samples"] = timings
        return result
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="4353d3c4cf7a37fc552a0309247f688f63a35fa5")
    parser.add_argument("--candidate", type=Path, default=repository / "proxy.py")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=6, help="Even number of adjacent AB/BA pairs per case")
    parser.add_argument("--size-mib", type=int, default=32)
    parser.add_argument("--timing-samples", type=int, default=10, help="Untraced uploads per isolated worker")
    parser.add_argument("--cases", nargs='+', choices=CASES, default=CASES,
                        help="Synthetic cases to measure (default: all)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(worker(args))), flush=True)
        return 0
    if args.samples < 2 or args.samples % 2 or not 1 <= args.size_mib <= 64 or not 1 <= args.timing_samples <= 100:
        parser.error("Use an even pair count >= 2, a size of 1-64 MiB and 1-100 timing samples")
    if len(set(args.cases)) != len(args.cases):
        parser.error("Select each case once")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from compare import snapshot_runtime
    sources = {"baseline": snapshot_runtime(repository / "proxy.py", repository, args.baseline_ref),
               "candidate": snapshot_runtime(args.candidate.resolve(), repository)}
    folder = (args.output or repository / "benchmark-results" /
              datetime.now(timezone.utc).strftime("body-%Y%m%dT%H%M%S%fZ")).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "benchmark.py").write_bytes(Path(__file__).read_bytes())
    hashes = {}
    for variant, files in sources.items():
        root = folder / variant
        root.mkdir()
        for name, value in files.items():
            (root / name).write_bytes(value)
        hashes[variant] = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    environment = os.environ.copy()
    for name in list(environment):
        if name.upper().startswith(("PROXY_", "AGENTROUTER_")) or name.upper() in (
                "CODEX_GATEWAY_API_KEY", "OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            del environment[name]
    environment.update(PROXY_AUTO_UPDATE="0", PROXY_VERBOSE="0", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                       AGENTROUTER_UPSTREAM="http://127.0.0.1:9")
    receipt = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
               "baseline_ref": args.baseline_ref, "runtime_sha256": hashes,
               "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "python": sys.version, "samples": args.samples, "size_mib": args.size_mib,
               "timing_samples_per_worker": args.timing_samples,
               "cases": list(args.cases),
               "real_model_calls": 0, "memory_semantics": "Live Python allocations including fixture; not RSS",
               "method": "Isolated processes in adjacent AB/BA order; warmed TCP backend; separate untraced timing run",
               "packages": {name: importlib.metadata.version(name) for name in ("httpx", "httpcore", "starlette", "anyio")}}

    def save():
        (folder / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    save()
    pairs = {case: [] for case in args.cases}
    try:
        with (folder / "pairs.jsonl").open("x", encoding="utf-8") as raw:
            for index in range(args.samples):
                for case in args.cases:
                    order = VARIANTS if index % 2 == 0 else VARIANTS[::-1]
                    pair = {"case": case, "index": index, "order": list(order)}
                    for variant in order:
                        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                            "--source", str(folder / variant), "--case", case, "--size-mib", str(args.size_mib),
                            "--timing-samples", str(args.timing_samples)],
                            cwd=folder / variant, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        (folder / f"{case}-{index}-{variant}.log").write_bytes(completed.stderr)
                        if completed.returncode:
                            raise RuntimeError("Benchmark worker failed; inspect its isolated log")
                        pair[variant] = json.loads(completed.stdout)
                    for mode in ("memory", "timing"):
                        if any(pair[variant][mode]["input_sha256"] != pair["baseline"][mode]["input_sha256"]
                               or pair[variant][mode]["final_upload_sha256"] != pair["baseline"][mode]["final_upload_sha256"]
                               for variant in VARIANTS):
                            raise RuntimeError("Baseline and candidate fixtures or recovery outputs differ")
                    raw.write(json.dumps(pair, separators=(",", ":")) + "\n")
                    raw.flush()
                    pairs[case].append(pair)
                print(json.dumps({"completed_pairs_per_case": index + 1}), flush=True)
        summary = {}
        for case, rows in pairs.items():
            summary[case] = {variant: {mode: {key: statistics.median(row[variant][mode][key] for row in rows)
                for key in (("waiting_headers_bytes", "committed_stream_bytes", "peak_bytes") if mode == "memory" else
                            ("final_upload_ms", "response_ready_ms"))} for mode in ("memory", "timing")} for variant in VARIANTS}
            summary[case]["input_bytes"] = rows[0]["baseline"]["memory"]["input_bytes"]
            summary[case]["median_paired_ready_saved_ms"] = statistics.median(
                row["baseline"]["timing"]["response_ready_ms"] - row["candidate"]["timing"]["response_ready_ms"] for row in rows)
        (folder / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        for variant, files in hashes.items():
            if any(hashlib.sha256((folder / variant / name).read_bytes()).hexdigest() != value for name, value in files.items()):
                raise RuntimeError("Frozen runtime changed during measurement")
        receipt.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat(), hashes_verified=True,
                       frozen_sources_unchanged=True, summary=summary)
        save()
        print(json.dumps(summary), flush=True)
        return 0
    except BaseException as error:
        receipt.update(status="failed", error_type=type(error).__name__, error=str(error))
        save()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
