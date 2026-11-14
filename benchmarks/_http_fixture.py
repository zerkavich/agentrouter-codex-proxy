"""Synthetic HTTP/SSE fixture server used by compare.py."""
from __future__ import annotations
import argparse
import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import socket
import statistics
import sys
import time

import httpx

ROOT = Path(os.environ["CODEX_PROXY_BENCH_ROOT"]).resolve()
GZIP = os.environ.get("CODEX_PROXY_BENCH_GZIP", "1") == "1"
PROFILES = {profile: {"source": profile + ".py", "gzip": GZIP,
                      "http": "auto", "verbose": False}
            for profile in ("baseline", "candidate")}

def fixtures():
    messages = lambda value: [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": value * 100},
    ]} for _ in range(120)]
    rng = random.Random(41)
    tool_history = [{
        "type": "function_call_output", "call_id": f"call-{index:04}",
        "output": "\n".join(
            f"src/module_{index % 17}/file_{line % 23}.py:{line + 1} check passed sha256={rng.randbytes(32).hex()}"
            for line in range(80)
        ),
    } for index in range(120)]
    image_data = base64.b64encode(random.Random(17).randbytes(75000)).decode()
    inputs = {
        "small": "Reply OK.",
        "english_history": messages('Synthetic text with escaped quotes " and newline\n'),
        "russian_history": messages("Синтетический текст для замера, без реальной переписки.\n"),
        "tool_history": tool_history,
        "tool_history_415": tool_history,
        "thirty_images": [{"type": "message", "role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64," + image_data},
        ]} for _ in range(30)],
    }
    return {
        name: json.dumps({"model": "synthetic", "stream": True, "input": value},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for name, value in inputs.items()
    }


def load_proxy(profile, port=12345):
    settings = PROFILES[profile]
    os.environ["AGENTROUTER_UPSTREAM"] = f"http://127.0.0.1:{port}/_synthetic_upstream"
    os.environ["PROXY_PORT"] = str(port)
    os.environ["PROXY_REQUEST_GZIP"] = "1" if settings["gzip"] else "0"
    os.environ["PROXY_REQUEST_GZIP_MIN_BYTES"] = "262144"
    os.environ["PROXY_GZIP_UPLOAD_MBIT_S"] = "0"
    os.environ["PROXY_VERBOSE"] = "1" if settings["verbose"] else "0"
    for name in ("CODEX_GATEWAY_API_KEY", "AGENTROUTER_API_KEY", "OPENAI_API_KEY",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["PROXY_AUTO_UPDATE"] = "0"
    spec = importlib.util.spec_from_file_location("bench_proxy", ROOT / settings["source"])
    proxy = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = proxy
    spec.loader.exec_module(proxy)
    return proxy


def percentile(values, fraction=0.95):
    return sorted(values)[max(0, int(len(values) * fraction + 0.999999) - 1)]


def timing(values):
    return {"median": round(statistics.median(values), 4), "p95": round(percentile(values), 4),
            "samples": len(values)}


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if port == 8788:
        return free_port()
    return port


def serve(args):
    proxy = load_proxy(args.profile, args.port)
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Mount, Route
    import uvicorn

    bodies = fixtures()
    upstream_counts = {"total": 0, "gzip_rejections": 0}
    expected_hashes = {}
    for name, body in bodies.items():
        expected = body
        assert json.loads(expected) == json.loads(body)
        accepted = {hashlib.sha256(expected).hexdigest()}
        if args.profile == "baseline":
            # Older upstream releases re-serialize every normal JSON body.
            accepted.add(hashlib.sha256(json.dumps(json.loads(body)).encode("utf-8")).hexdigest())
        expected_hashes[name] = accepted

    def frame(event, data):
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    async def upstream(request):
        upstream_counts["total"] += 1
        content = await request.body()
        received_ns = time.perf_counter_ns()
        encoding = request.headers.get("content-encoding")
        if encoding == "gzip" and request.headers["x-bench-fixture"].endswith("_415"):
            upstream_counts["gzip_rejections"] += 1
            return JSONResponse({"error":{"message":"synthetic gzip rejection"}}, status_code=415)
        decoded = gzip.decompress(content) if encoding == "gzip" else content
        digest = hashlib.sha256(decoded).hexdigest()
        if digest not in expected_hashes[request.headers["x-bench-fixture"]]:
            return JSONResponse({"error": {"message": "Unexpected synthetic request bytes"}}, status_code=422)
        validation_ms = (time.perf_counter_ns() - received_ns) / 1e6
        delay_ms = float(request.headers.get("x-bench-output-delay-ms", "0"))

        async def events():
            yield frame("response.created", {"type": "response.created", "response": {"id": "synthetic", "status": "in_progress"}})
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            emitted_ns = time.perf_counter_ns()
            yield frame("response.output_text.delta", {
                "type": "response.output_text.delta", "delta": "OK", "benchmark": {
                    "received_ns": received_ns, "emitted_ns": emitted_ns, "wire_bytes": len(content),
                    "validation_ms": validation_ms,
                    "body_preserved": digest == request.headers["x-bench-body-sha256"],
                },
            })
            yield frame("response.completed", {"type": "response.completed", "response": {"id": "synthetic", "status": "completed", "output": []}})

        return StreamingResponse(events(), media_type="text/event-stream")

    async def metadata(request):
        client = proxy._SHARED_CLIENT
        pool = getattr(getattr(client, "_transport", None), "_pool", None)
        return JSONResponse({"http_protocol": config.http_protocol_class.__name__,
                             "gzip_backend": "isal" if getattr(proxy, "_native_gzip", None) is not None else None,
                             "upstream_requests": len(getattr(pool, "_requests", [])), "upstream_counts": dict(upstream_counts), "health": await proxy.health()})

    gateway = Starlette(routes=[
        Route("/_benchmark_metadata", metadata),
        Mount("/_synthetic_upstream", Starlette(routes=[Route("/v1/responses", upstream, methods=["POST"])])),
        Mount("/", proxy.app),
    ], lifespan=proxy._app_lifespan)
    config = uvicorn.Config(gateway, host="127.0.0.1", port=args.port, http=PROFILES[args.profile]["http"],
                            log_level="warning", access_log=False, timeout_keep_alive=600)
    config.load()
    uvicorn.Server(config).run()


async def one_request(client, endpoint, name, body, delay_ms=0):
    started = time.perf_counter_ns()
    result = None
    async with client.stream("POST", endpoint + "/v1/responses", content=body, headers={
        "content-type": "application/json", "authorization": "Bearer synthetic-no-gzip" if name.endswith("_415") else "Bearer synthetic",
        "x-bench-fixture": name, "x-bench-body-sha256": hashlib.sha256(body).hexdigest(),
        "x-bench-output-delay-ms": str(delay_ms),
    }) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Synthetic HTTP response: {response.status_code}")
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            observed = time.perf_counter_ns()
            buffer.extend(chunk)
            while b"\n\n" in buffer:
                event, _, tail = buffer.partition(b"\n\n")
                buffer = bytearray(tail)
                for line in event.splitlines():
                    if line.startswith(b"data:"):
                        data = json.loads(line[5:].strip())
                        if data.get("type") == "response.output_text.delta":
                            meta = data["benchmark"]
                            result = {
                                "first_output_ms": (observed - started) / 1e6,
                                "body_arrival_ms": (meta["received_ns"] - started) / 1e6,
                                "output_forward_ms": (observed - meta["emitted_ns"]) / 1e6,
                                "validation_ms": meta["validation_ms"], "wire_bytes": meta["wire_bytes"],
                                "body_preserved": meta["body_preserved"],
                            }
    if result is None:
        raise RuntimeError("No synthetic output event")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if args.port == 8788:
        parser.error("The production listener cannot be used by this benchmark")
    serve(args)
