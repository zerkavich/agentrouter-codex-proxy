"""Compare frozen proxy revisions using adjacent synthetic loopback requests.

Install the repository's requirements first. Example:
  python benchmarks/compare.py --baseline-ref a5621ce --samples 90 --epochs 4
No provider requests, production-port connections or configuration writes occur.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def snapshot_runtime(source_path, repository, revision=None):
    """Freeze the proxy and its local module from the same path or revision."""
    def read(name):
        if revision is None:
            return (source_path if name == "proxy.py" else source_path.with_name(name)).read_bytes()
        result = subprocess.run(["git", "-c", "safe.directory=" + repository.as_posix(), "show",
            "--no-ext-diff", "--end-of-options", revision + ":" + name], cwd=repository,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise OSError("Required source is not available at the baseline revision")
        return result.stdout

    files = {"proxy.py": read("proxy.py")}
    tree = ast.parse(files["proxy.py"])
    if any((isinstance(node, ast.ImportFrom) and node.module == "telemetry" and not node.level)
           or (isinstance(node, ast.Import) and any(name.name == "telemetry" for name in node.names))
           for node in ast.walk(tree)):
        files["telemetry.py"] = read("telemetry.py")
    for name, source in files.items():
        compile(source, name, "exec")
    return files


def main():
    scripts = Path(__file__).resolve().parent
    repository = scripts.parent
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline", type=Path, help="Path to a previous proxy.py")
    baseline.add_argument("--baseline-ref", help="Locally available Git revision (default: a5621ce)")
    parser.add_argument("--candidate", type=Path, default=repository / "proxy.py")
    parser.add_argument("--output", type=Path, help="New output directory; existing results are retained")
    parser.add_argument("--samples", type=int, default=90, help="Even number of adjacent pairs per scenario/epoch")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--gzip", choices=("on", "off"), default="on")
    args = parser.parse_args()
    if args.samples < 2 or args.samples % 2 or args.epochs < 1 or args.warmup < 1:
        parser.error("Use positive epochs/warmup and an even sample count of at least two")
    try:
        candidate_files = snapshot_runtime(args.candidate.resolve(), repository)
        if args.baseline:
            baseline_files = snapshot_runtime(args.baseline.resolve(), repository)
            baseline_origin = str(args.baseline.resolve())
        else:
            revision = args.baseline_ref or "a5621ce"
            baseline_files = snapshot_runtime(repository / "proxy.py", repository, revision)
            baseline_origin = revision
    except (OSError, SyntaxError, subprocess.TimeoutExpired) as error:
        parser.error("Cannot prepare complete runtime snapshots; use a full clone or colocated modules: " + type(error).__name__)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = (args.output or repository / "benchmark-results" / identifier).resolve()
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("Output directory already exists; select a new directory to preserve previous results")
    runtime_hashes = {}
    for profile, files in (("baseline", baseline_files), ("candidate", candidate_files)):
        (folder / profile).mkdir()
        for name, source in files.items():
            (folder / profile / name).write_bytes(source)
        runtime_hashes[profile] = {name: hashlib.sha256(source).hexdigest() for name, source in files.items()}
    prepared = {"baseline": baseline_origin, "candidate": str(args.candidate.resolve()),
        "source_sha256": {profile: hashes["proxy.py"] for profile, hashes in runtime_hashes.items()},
        "runtime_sha256": runtime_hashes,
        "real_model_calls": 0, "request_gzip": args.gzip,
        "method": "Adjacent AB/BA pairs, continuously warm synthetic loopback servers; includes upstream validation"}
    (folder / "inputs.json").write_text(json.dumps(prepared, indent=2) + "\n", encoding="utf-8")
    # All snapshots and child working directories are isolated from the proxy's
    # .env and inherited proxy tuning. Only this process's environment changes.
    for name in list(os.environ):
        if name.upper().startswith(("PROXY_", "AGENTROUTER_")) or name.upper() in {
            "CODEX_GATEWAY_API_KEY", "OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        }:
            del os.environ[name]
    os.environ.update(CODEX_PROXY_BENCH_ROOT=str(folder), CODEX_PROXY_BENCH_GZIP="1" if args.gzip == "on" else "0",
                      PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PROXY_AUTO_UPDATE="0")
    sys.path.insert(0, str(scripts))
    from _comparison import run
    args.label = "measurement"
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
