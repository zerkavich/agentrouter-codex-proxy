"""Compare payload transformation allocations in isolated credential-free workers.

Reports Python allocations during transformation, not total process RSS, API
latency or model generation. Baseline and candidate receive identical fixtures;
decoded output and unchanged input hashes must match.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tracemalloc


def scrub_environment(source):
    environment = os.environ.copy()
    for name in list(environment):
        if name.upper().startswith(('PROXY_', 'AGENTROUTER_')) or name.upper() in (
                'OPENAI_API_KEY', 'CODEX_GATEWAY_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            environment.pop(name)
    environment.update(PROXY_AUTO_UPDATE='0', PROXY_VERBOSE='0', PYTHONDONTWRITEBYTECODE='1',
                       AGENTROUTER_UPSTREAM='http://127.0.0.1:9', PYTHONPATH=os.pathsep.join((str(source.parent), environment.get('PYTHONPATH', ''))))
    return environment


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def fixtures():
    messages = [{'type': 'message', 'role': 'user', 'id': 'synthetic-' + str(index),
                 'content': [{'type': 'input_text', 'text': 'Synthetic tool history ' * 12}],
                 'metadata': {'values': list(range(80)), 'nested': [{'index': index, 'values': list(range(12))} for _ in range(6)]}}
                for index in range(2000)]
    images = [{'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Synthetic screenshot'},
        {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 2048}],
        'metadata': {'values': list(range(80))}} for _ in range(150)]
    return {'image_pruning': {'input': messages + images},
            'stateless_recovery': {'previous_response_id': 'synthetic-server-state', 'input': messages + images}}


def worker(source, samples):
    sys.path.insert(0, str(source.parent))
    specification = importlib.util.spec_from_file_location('proxy', source)
    proxy = importlib.util.module_from_spec(specification)
    sys.modules['proxy'] = proxy
    specification.loader.exec_module(proxy)
    results = {}
    for name, payload in fixtures().items():
        original = digest(payload)
        transform = (lambda: proxy.prune_excess_images(payload)[0]) if name == 'image_pruning' else lambda: proxy.sanitize_responses_payload(payload)
        peaks = []
        output_hash = None
        for index in range(samples + 2):
            gc.collect()
            tracemalloc.start(1)
            result = transform()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            current_hash = digest(result)
            if output_hash is not None and current_hash != output_hash:
                raise AssertionError('unstable_output')
            output_hash = current_hash
            del result
            if digest(payload) != original:
                raise AssertionError('input_was_mutated')
            if index >= 2:
                peaks.append(peak)
        results[name] = {'median_peak_python_bytes': statistics.median(peaks),
                         'maximum_peak_python_bytes': max(peaks), 'samples': peaks,
                         'input_sha256': original, 'output_sha256': output_hash}
    return {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'cases': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path, default=Path(__file__).resolve().parents[1] / 'proxy.py')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=15)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 3 <= args.samples <= 100:
        parser.error('Use between 3 and 100 samples')
    if args.worker:
        print(json.dumps(worker(args.worker.resolve(), args.samples)))
        return
    if args.baseline is None or args.output is None:
        parser.error('--baseline and a new --output file are required')
    if args.output.exists():
        parser.error('Output already exists; preserve it and select another file')
    results = {}
    for label, source in (('baseline', args.baseline.resolve()), ('candidate', args.candidate.resolve())):
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(source), '--samples', str(args.samples)]
        completed = subprocess.run(command, env=scrub_environment(source), text=True, encoding='utf-8',
                    capture_output=True, timeout=180, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        results[label] = json.loads(completed.stdout.strip().splitlines()[-1])
    comparisons = {}
    for name in results['baseline']['cases']:
        previous, current = results['baseline']['cases'][name], results['candidate']['cases'][name]
        if (previous['output_sha256'] != current['output_sha256'] or previous['input_sha256'] != current['input_sha256']):
            raise AssertionError('baseline_candidate_semantics_differ')
        comparisons[name] = {'baseline_peak_bytes': previous['median_peak_python_bytes'],
                             'candidate_peak_bytes': current['median_peak_python_bytes'],
                             'peak_reduction_percent': round((1 - current['median_peak_python_bytes'] / previous['median_peak_python_bytes']) * 100, 2)}
    receipt = {'status': 'passed', 'captured_at': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
               'samples_per_case': args.samples, 'measurement': 'Python transformation allocation peaks; not process RSS or API latency',
               'provider_calls': 0, 'comparison': comparisons, 'raw_results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, indent=2)
        stream.write('\n')
    print(json.dumps(comparisons), flush=True)


if __name__ == '__main__':
    main()
