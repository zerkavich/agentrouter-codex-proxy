"""Explicit updates of a managed installation from the user's tested fork."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from control import atomic_json, state_dir
from update_guard import (TARGET_FILES, RollbackError, child_path, digest, durable_copy, read_json,
                          recover_interrupted_update, rewrite_json, rollback_update, sha)


FORK = 'zerkavich/agentrouter-codex-proxy'
API = 'https://api.github.com/repos/' + FORK
MAX_BYTES = 2 * 1024 * 1024
RUNTIME_FILES = TARGET_FILES
REQUIRED_FILES = (*RUNTIME_FILES, 'update_guard.py')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def read(url, limit=MAX_BYTES):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'agentrouter-proxy-local-control',
                                                  'Accept': 'application/vnd.github+json'})
    with opener.open(request, timeout=8) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('oversize')
    return data


def installed_commit(root):
    try:
        commit = read_json(Path(root).parent / 'manifest.json', 1024 * 1024).get('local_patch_commit', '')
        return commit if isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}', commit) else None
    except (OSError, ValueError, TypeError):
        return None


def install_supported(root):
    root = Path(root)
    if os.environ.get('PROXY_CONTROL_UPDATES') != '1':
        return False
    manifest = root.parent / 'manifest.json'
    if not manifest.is_file() or not (root / 'update_guard.py').is_file():
        return False
    try:
        # os.access only inspects the read-only attribute on Windows. Opening an
        # existing file checks the real ACL without changing its bytes or time.
        for path in (manifest, *(root / name for name in RUNTIME_FILES)):
            descriptor = os.open(path, os.O_RDWR)
            os.close(descriptor)
        return True
    except OSError:
        return False


def check_latest(root, version):
    try:
        metadata = json.loads(read(API + '/commits/main'))
        commit = metadata.get('sha', '')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('invalid_commit')
        source = read('https://raw.githubusercontent.com/' + FORK + '/' + commit + '/proxy.py').decode('utf-8')
        match = re.search(r'^VERSION\s*=\s*["\'](\d+\.\d+\.\d+)["\']\s*$', source, re.M)
        latest = match[1] if match else None
        current = installed_commit(root)
        title = str(metadata.get('commit', {}).get('message', '')).split('\n', 1)[0][:160]
        return {'state': 'checked', 'fork': FORK, 'current_version': version, 'latest_version': latest,
                'commit': commit, 'installed_commit': current,
                'update_available': current != commit if current else latest != version,
                'title': title, 'url': 'https://github.com/' + FORK + '/commit/' + commit,
                'install_supported': install_supported(root)}
    except (OSError, urllib.error.URLError, ValueError, TypeError, AttributeError):
        return {'state': 'failed', 'error_code': 'update_check_unavailable', 'update_available': False}


def manifest_files(root):
    root = Path(root).resolve()
    data = read_json(root.parent / 'manifest.json', 1024 * 1024)
    entries = {}
    files = data.get('files')
    if not isinstance(files, list) or len(files) > 5000:
        raise ValueError('unsafe_manifest')
    for entry in files:
        if not isinstance(entry, dict) or not digest(entry.get('sha256')):
            raise ValueError('unsafe_manifest')
        relative = entry.get('path')
        try:
            child_path(root.parent, relative)
        except ValueError:
            raise ValueError('unsafe_manifest') from None
        if relative.lower() in entries:
            raise ValueError('unsafe_manifest')
        entries[relative.lower()] = entry
    for name in REQUIRED_FILES:
        entry = entries.get('app/' + name)
        if entry is None or sha(child_path(root, name)) != entry['sha256']:
            raise ValueError('runtime_drift')
    return data, entries


def verify_candidate(folder, root):
    folder, root = Path(folder).resolve(), Path(root).resolve()
    for name in REQUIRED_FILES:
        if not child_path(folder, name).is_file():
            raise ValueError('candidate_incomplete')
    for name in ('requirements.txt', 'update_guard.py'):
        if (folder / name).read_bytes().replace(b'\r\n', b'\n') != (root / name).read_bytes().replace(b'\r\n', b'\n'):
            raise ValueError('dependency_update_requires_admin' if name == 'requirements.txt' else 'bootstrap_update_requires_admin')
    for name in REQUIRED_FILES:
        if name.endswith('.py'):
            compile((folder / name).read_bytes(), name, 'exec')


PROBE = r'''
import asyncio, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import control, doctor, httpcore, panel_updates, proxy, service, telemetry, updater, update_guard
import httpx
assert httpcore.__version__ == "1.0.9"
for module in (control, doctor, panel_updates, proxy, service, telemetry, updater, update_guard):
    assert pathlib.Path(module.__file__).resolve().parent == root
async def main():
    async with proxy.app.router.lifespan_context(proxy.app):
        health = await proxy.health()
        assert health["connection_pool_initialized"] and health["upstream_pool_cleanup_active"]
        assert health["upstream_max_inflight"] == 50
        original = proxy._SHARED_CLIENT
        await original.aclose()
        payload = b'{"input":"synthetic update probe","stream":false}'
        async def upstream(request):
            assert request.content == payload
            assert request.headers["authorization"] == "Bearer synthetic-update-probe"
            return httpx.Response(200, json={"status":"completed","output":[]})
        proxy._SHARED_CLIENT = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18788") as client:
            response = await client.post("/v1/responses", content=payload,
                headers={"content-type":"application/json","authorization":"Bearer synthetic-update-probe"})
            assert response.status_code == 200 and response.json()["status"] == "completed"
            assert proxy.CONTROL.active == 0
            session = await client.get("/_control/api/session")
            assert session.status_code == 200 and session.json()["version"] == proxy.VERSION
            for name in ("panel.js","panel.css","mark.svg"):
                assert (await client.get("/_control/assets/" + name)).status_code == 200
            report = await client.get("/_control/api/report")
            assert report.status_code == 200
            assert not any(value in report.text for value in ("synthetic-update-probe","synthetic update probe",str(root)))
            assert not (await client.get("/_control/api/settings")).json()["restart_required"]
    print(json.dumps({"probe_passed":True,"version":proxy.VERSION}, separators=(",",":")))
asyncio.run(main())
'''


def probe_candidate(folder):
    """Import and exercise the exact staged runtime without credentials/network."""
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith('PROXY_') and name.upper() not in
                   {'CODEX_GATEWAY_API_KEY', 'AGENTROUTER_API_KEY', 'OPENAI_API_KEY',
                    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'PYTHONPATH', 'PYTHONHOME'}}
    with tempfile.TemporaryDirectory(prefix='probe-', dir=Path(folder).parent) as isolated:
        environment.update(PROXY_CONTROL_STATE_DIR=isolated, PROXY_AUTO_UPDATE='0', PROXY_VERBOSE='0',
                           PROXY_PORT='18788', PROXY_REQUEST_GZIP='0', PYTHONDONTWRITEBYTECODE='1',
                           AGENTROUTER_UPSTREAM='http://127.0.0.1:9', PYTHONUTF8='1')
        options = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        result = subprocess.run([str(python), '-I', '-B', '-c', PROBE, str(Path(folder).resolve())],
                                cwd=folder, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25, **options)
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError('update_probe_failed')
        try:
            receipt = json.loads(result.stdout)
        except ValueError:
            raise ValueError('update_probe_failed') from None
        if not isinstance(receipt, dict) or receipt.get('probe_passed') is not True:
            raise ValueError('update_probe_failed')
        return receipt


def extract_candidate(archive_data, folder):
    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if (not names or len(names) > 2048 or len(names) != len(set(names))
                or sum(info.file_size for info in infos) > 64 * 1024 * 1024):
            raise ValueError('update_archive_invalid')
        prefix = names[0].split('/', 1)[0] + '/'
        if not all(name.startswith(prefix) for name in names):
            raise ValueError('update_archive_invalid')
        for name in REQUIRED_FILES:
            info = archive.getinfo(prefix + name)
            if (info.file_size > 4 * 1024 * 1024 or info.is_dir()
                    or info.flag_bits & 1 or ((info.external_attr >> 16) & 0o170000) == 0o120000):
                raise ValueError('update_archive_invalid')
            target = child_path(folder, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            # ZipFile.read checks this file's CRC without decompressing others.
            target.write_bytes(archive.read(info))


def prepared_data(folder, root, commit):
    prepared = read_json(folder / 'prepared.json')
    hashes = prepared.get('hashes')
    if (prepared.get('state') != 'prepared' or prepared.get('commit') != commit
            or prepared.get('probe_passed') is not True or not isinstance(hashes, dict)
            or set(hashes) != set(REQUIRED_FILES) or not all(map(digest, hashes.values()))
            or not isinstance(prepared.get('version'), str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', prepared['version'])):
        raise ValueError('update_prepared_invalid')
    verify_candidate(folder / 'candidate', root)
    for name, expected in hashes.items():
        if sha(child_path(folder / 'candidate', name)) != expected:
            raise ValueError('update_prepared_changed')
    return prepared


def prepare_update(root, commit):
    root = Path(root).resolve()
    if not install_supported(root):
        return {'state': 'failed', 'error_code': 'updates_require_prepared_install'}
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
        return {'state': 'failed', 'error_code': 'invalid_update_commit'}
    folder = state_dir(root).resolve() / 'updates' / commit
    try:
        latest = json.loads(read(API + '/commits/main'))
        if latest.get('sha') != commit:
            raise ValueError('update_changed_check_again')
        status = json.loads(read(API + '/commits/' + commit + '/check-runs'))
        required = {f'{platform}-latest / Python {python}' for platform in ('ubuntu', 'windows') for python in ('3.10', '3.12')}
        checks = status.get('check_runs', [])
        accepted = {entry.get('name') for entry in checks if isinstance(entry, dict) and entry.get('head_sha') == commit
                    and entry.get('status') == 'completed' and entry.get('conclusion') == 'success'
                    and entry.get('app', {}).get('slug') == 'github-actions'}
        if not required.issubset(accepted):
            raise ValueError('update_ci_not_passed')
        manifest_files(root)
        if folder.exists():
            prepared = prepared_data(folder, root, commit)
        else:
            folder.mkdir(parents=True, exist_ok=False)
            archive_data = read('https://codeload.github.com/' + FORK + '/zip/' + commit, 16 * 1024 * 1024)
            extract_candidate(archive_data, folder / 'candidate')
            verify_candidate(folder / 'candidate', root)
            probe = probe_candidate(folder / 'candidate')
            prepared = {'state': 'prepared', 'commit': commit, 'version': probe['version'], 'probe_passed': True,
                        'hashes': {name: sha(folder / 'candidate' / name) for name in REQUIRED_FILES},
                        'prepared_at': datetime.now(timezone.utc).isoformat()}
            atomic_json(folder / 'prepared.json', prepared)
        return {'state': 'prepared', 'commit': commit, 'latest_version': prepared['version'], 'install_supported': True}
    except ValueError as error:
        safe = {'update_changed_check_again', 'update_ci_not_passed', 'runtime_drift', 'unsafe_manifest',
                'dependency_update_requires_admin', 'bootstrap_update_requires_admin', 'candidate_incomplete',
                'update_probe_failed', 'update_prepared_changed', 'update_prepared_invalid'}
        return {'state': 'failed', 'error_code': str(error) if str(error) in safe else 'update_package_invalid'}
    except (OSError, urllib.error.URLError, KeyError, zipfile.BadZipFile, SyntaxError, TypeError,
            AttributeError, subprocess.SubprocessError):
        return {'state': 'failed', 'error_code': 'update_prepare_failed'}


def install_prepared(root, commit, action_id):
    """Called after the matching worker drains/stops; write-ahead backup."""
    root = Path(root).resolve()
    if (not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit)
            or not isinstance(action_id, str) or not re.fullmatch('[0-9a-f]{32}', action_id)):
        raise ValueError('invalid_update_identity')
    folder = state_dir(root).resolve() / 'updates' / commit
    prepared = prepared_data(folder, root, commit)
    manifest, entries = manifest_files(root)
    manifest_path = root.parent / 'manifest.json'
    backup = state_dir(root).resolve() / 'updates' / ('backup-' + action_id)
    backup.mkdir(parents=True, exist_ok=False)
    prior = {name: sha(child_path(root, name)) for name in RUNTIME_FILES}
    for name in RUNTIME_FILES:
        destination = backup / 'app' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        durable_copy(child_path(root, name), destination)
        if sha(destination) != prior[name]:
            raise ValueError('update_backup_failed')
    manifest_digest = sha(manifest_path)
    durable_copy(manifest_path, backup / 'manifest.json')
    if sha(backup / 'manifest.json') != manifest_digest:
        raise ValueError('update_backup_failed')
    transaction = {'state': 'installing', 'id': action_id, 'commit': commit, 'version': prepared['version'],
                   'before_version': manifest.get('proxy_version'), 'before_sha256': prior,
                   'after_sha256': {name: prepared['hashes'][name] for name in RUNTIME_FILES},
                   'before_manifest_sha256': manifest_digest, 'backup_name': backup.name,
                   'started_at': datetime.now(timezone.utc).isoformat()}
    atomic_json(state_dir(root) / 'update-transaction.json', transaction)
    try:
        for name in RUNTIME_FILES:
            target = child_path(root, name)
            durable_copy(child_path(folder / 'candidate', name), target)
            if sha(target) != prepared['hashes'][name]:
                raise ValueError('update_copy_failed')
            entries['app/' + name].update(sha256=prepared['hashes'][name], size=target.stat().st_size)
        manifest.update(proxy_version=prepared['version'], local_patch_commit=commit,
                        local_patch_source_sha256=prepared['hashes']['proxy.py'],
                        browser_update_staging='installation_manifest_is_authoritative')
        rewrite_json(manifest_path, manifest)
        transaction['after_manifest_sha256'] = sha(manifest_path)
        transaction['state'] = 'pending_health'
        atomic_json(state_dir(root) / 'update-transaction.json', transaction)
        return transaction
    except (OSError, ValueError):
        rollback_update(root, transaction)
        raise
