"""Adjacent, order-balanced request pairs with two continuously warm servers.

Only synthetic loopback traffic is generated. The running machine proxy,
credentials, source snapshots and long-soak processes are never changed.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time

import _http_fixture as bench

ROOT = Path(os.environ["CODEX_PROXY_BENCH_ROOT"]).resolve()
SERVER_HELPER = Path(__file__).with_name("_http_fixture.py")
PROFILES = ("baseline", "candidate")


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def distribution(values):
    return {str(size): values.count(size) for size in sorted(set(values))}


def bootstrap_median(values, seed):
    """Pair-level interval, not a claim about other machines or Internet links."""
    rng = random.Random(seed)
    resampled = [statistics.median(rng.choices(values, k=len(values))) for _ in range(2000)]
    ordered = sorted(resampled)
    return {"lower": ordered[49], "upper": ordered[1949], "resamples": 2000,
            "method": "percentile bootstrap over adjacent pairs; samples may have serial correlation"}


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
            raise RuntimeError("Benchmark server failed to become ready; inspect its log")
        await asyncio.sleep(.05)


def terminate(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def run(args):
    folder = ROOT / args.label
    folder.mkdir(exist_ok=False)
    sources = {profile: digest(ROOT / (profile + ".py")) for profile in PROFILES}
    bodies = bench.fixtures()
    samples = {name: [] for name in bodies}
    status = {"status": "running", "started_utc": utc(), "source_sha256": sources,
              "benchmark_sha256": digest(Path(__file__)),
              "server_harness_sha256": digest(SERVER_HELPER),
              "real_model_calls": 0, "completed_pairs": 0, "completed_epochs": 0,
              "epochs": args.epochs, "pairs_per_fixture_per_epoch": args.samples,
              "warmup_per_fixture_per_variant": args.warmup,
              "method": "Both servers remain running; adjacent request pairs alternate AB/BA for each fixture; fixture order is shuffled in each cycle",
              "python": platform.python_version(), "platform": platform.platform(),
              "packages": {name: importlib.metadata.version(name) for name in
                           ("httpx", "httpcore", "anyio", "starlette", "uvicorn", "httptools", "isal")}}
    write_json(folder / "receipt.json", status)
    processes = {}
    endpoints = {}
    ports = set()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with ExitStack() as stack:
            raw = stack.enter_context((folder / "pairs.jsonl").open("w", encoding="utf-8"))
            for profile in PROFILES:
                port = bench.free_port()
                while port in ports:
                    port = bench.free_port()
                ports.add(port)
                endpoints[profile] = f"http://127.0.0.1:{port}"
                log = stack.enter_context((folder / (profile + ".log")).open("wb"))
                processes[profile] = subprocess.Popen(
                    [sys.executable, str(SERVER_HELPER),
                     "--profile", profile, "--port", str(port)], env=environment, cwd=ROOT,
                    stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            async with AsyncExitStack() as clients_stack:
                clients = {profile: await clients_stack.enter_async_context(
                    bench.httpx.AsyncClient(timeout=30, trust_env=False)) for profile in PROFILES}
                metadata = await asyncio.gather(*(ready(clients[p], endpoints[p], processes[p]) for p in PROFILES))
                status["metadata_before"] = dict(zip(PROFILES, metadata))
                for name, body in bodies.items():
                    for index in range(args.warmup):
                        order = PROFILES if index % 2 == 0 else tuple(reversed(PROFILES))
                        for profile in order:
                            sample = await bench.one_request(clients[profile], endpoints[profile], name, body)
                            if profile == "candidate" and not sample["body_preserved"]:
                                raise RuntimeError("Warmup changed the decoded request body")
                status["warmup_finished_utc"] = utc()
                write_json(folder / "receipt.json", status)
                rng = random.Random(20261113)
                order_offsets = {name: rng.randrange(2) for name in bodies}
                for epoch in range(args.epochs):
                    for index in range(args.samples):
                        names = list(bodies)
                        rng.shuffle(names)
                        for name in names:
                            order = PROFILES if (index + epoch + order_offsets[name]) % 2 == 0 else tuple(reversed(PROFILES))
                            pair = {"epoch": epoch + 1, "index": index + 1, "fixture": name,
                                    "order": list(order), "started_utc": utc()}
                            for profile in order:
                                pair[profile] = await bench.one_request(clients[profile], endpoints[profile], name, bodies[name])
                                if profile == "candidate" and not pair[profile]["body_preserved"]:
                                    raise RuntimeError("Synthetic upstream hash verification failed")
                            samples[name].append(pair)
                            raw.write(json.dumps(pair, separators=(",", ":")) + "\n")
                            status["completed_pairs"] += 1
                    raw.flush()
                    status.update(completed_epochs=epoch + 1, checked_utc=utc())
                    write_json(folder / "receipt.json", status)
                    print(json.dumps({"epoch": epoch + 1, "pairs": status["completed_pairs"],
                        "medians_ms": {name: {p: round(statistics.median(pair[p]["first_output_ms"] for pair in pairs), 3)
                                              for p in PROFILES} for name, pairs in samples.items()}}), flush=True)
                delayed = {p: [] for p in PROFILES}
                for index in range(6):
                    order = PROFILES if index % 2 == 0 else tuple(reversed(PROFILES))
                    for profile in order:
                        delayed[profile].append(await bench.one_request(clients[profile], endpoints[profile], "small", bodies["small"], delay_ms=200))
                status["metadata_after"] = {p: (await clients[p].get(endpoints[p] + "/_benchmark_metadata")).json() for p in PROFILES}
                if any(value["upstream_requests"] for value in status["metadata_after"].values()):
                    raise RuntimeError("Healthy benchmark left upstream requests pending")
            variants = {}
            paired = {}
            for profile in PROFILES:
                variants[profile] = {"fixtures": {}, "delayed_200ms_first_output_ms":
                    bench.timing([s["first_output_ms"] for s in delayed[profile]])}
                for name, pairs in samples.items():
                    values = [pair[profile] for pair in pairs]
                    variants[profile]["fixtures"][name] = {
                        key: bench.timing([s[key] for s in values]) for key in
                        ("first_output_ms", "body_arrival_ms", "output_forward_ms", "validation_ms")
                    } | {"input_bytes": len(bodies[name]),
                         "wire_bytes": distribution([s["wire_bytes"] for s in values]),
                         "body_preserved": all(s["body_preserved"] for s in values)}
            for offset, (name, pairs) in enumerate(samples.items()):
                deltas = [p["baseline"]["first_output_ms"] - p["candidate"]["first_output_ms"] for p in pairs]
                percentages = [100 * (1 - p["candidate"]["first_output_ms"] / p["baseline"]["first_output_ms"]) for p in pairs]
                paired[name] = {
                    "pairs": len(pairs), "median_saved_ms": statistics.median(deltas),
                    "median_saved_percent": statistics.median(percentages),
                    "candidate_faster_pairs": sum(value > 0 for value in deltas),
                    "median_saved_ms_bootstrap_95": bootstrap_median(deltas, 619 + offset),
                    "median_saved_percent_bootstrap_95": bootstrap_median(percentages, 1919 + offset),
                    "order_strata": {first: {profile: bench.timing([
                        p[profile]["first_output_ms"] for p in pairs if p["order"][0] == first])
                        for profile in PROFILES} for first in PROFILES},
                    "epochs": {str(epoch): {profile: bench.timing([
                        p[profile]["first_output_ms"] for p in pairs if p["epoch"] == epoch])
                        for profile in PROFILES} for epoch in range(1, args.epochs + 1)},
                }
            source_after = {p: digest(ROOT / (p + ".py")) for p in PROFILES}
            if source_after != sources:
                raise RuntimeError("Benchmark source changed during the run")
            status.update(status="passed", finished_utc=utc(), source_unchanged=True,
                          raw_pairs_sha256=digest(folder / "pairs.jsonl"))
            write_json(folder / "comparison.json", {
                "checked_at": utc(), "scope": "Complete loopback HTTP/SSE, including synthetic decompression/hash validation; interleaved adjacent pairs",
                "real_upstream_requests": 0, "model_generation_ms": 0,
                "source_sha256": sources, "method": status["method"], "epochs": args.epochs,
                "pairs_per_fixture_per_epoch": args.samples, "variants": variants, "paired": paired,
                "receipt": "receipt.json", "raw_pairs": "pairs.jsonl",
            })
            write_json(folder / "receipt.json", status)
            print(json.dumps({"status": "passed", "pairs": status["completed_pairs"],
                "source_sha256": sources, "result": str(folder / "comparison.json")}), flush=True)
    except BaseException as error:
        status.update(status="failed", finished_utc=utc(), error=type(error).__name__ + ": " + str(error))
        write_json(folder / "receipt.json", status)
        raise
    finally:
        for process in processes.values():
            terminate(process)

