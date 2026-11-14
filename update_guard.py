"""Immutable, stdlib-only recovery bootstrap for managed browser updates.

Install this file with administrator permissions. Browser updates cannot change
it, the machine entrypoint, dependencies, credentials or the service account.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid


TARGET_FILES = ('proxy.py', 'telemetry.py', 'control.py', 'panel_updates.py', 'service.py',
                'updater.py', 'doctor.py', 'diagnose.ps1', 'requirements.txt',
                'panel/index.html', 'panel/panel.js', 'panel/panel.css', 'panel/mark.svg')


class RollbackError(ValueError):
    pass


def state_dir(root):
    return Path(os.environ.get('PROXY_CONTROL_STATE_DIR', str(Path(root) / 'logs'))).resolve()


def read_json(path, limit=65536):
    with Path(path).open('rb') as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError('oversize_json')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('invalid_json_object')
    return value


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def durable_copy(source, destination):
    shutil.copyfile(source, destination)
    with Path(destination).open('r+b') as stream:
        stream.flush()
        os.fsync(stream.fileno())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rewrite_json(path, value):
    """Existing allowlisted file only; durable transaction covers interruption.

    Unlike a rename, this needs no write permission on the protected app parent.
    The immutable bootstrap restores the manifest if this write is interrupted.
    """
    data = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8')
    with Path(path).open('r+b') as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def child_path(base, relative):
    base = Path(base).resolve()
    if (not isinstance(relative, str) or '\\' in relative or ':' in relative
            or not relative or any(part in ('', '.', '..') for part in relative.split('/'))
            or PurePosixPath(relative).is_absolute()):
        raise ValueError('unsafe_path')
    path = base / relative
    if not path.resolve().is_relative_to(base):
        raise ValueError('unsafe_path')
    for parent in (path, *path.parents):
        if parent == base:
            break
        if parent.is_symlink():
            raise ValueError('unsafe_path')
    return path


def digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_transaction(root, transaction):
    if (not isinstance(transaction, dict)
            or transaction.get('state') not in ('installing', 'pending_health', 'verified', 'rolled_back')
            or not isinstance(transaction.get('id'), str)
            or re.fullmatch('[0-9a-f]{32}', transaction['id']) is None
            or not isinstance(transaction.get('commit'), str)
            or re.fullmatch('[0-9a-f]{40}', transaction['commit']) is None
            or not isinstance(transaction.get('version'), str)
            or re.fullmatch(r'\d+\.\d+\.\d+', transaction['version']) is None
            or transaction.get('backup_name') != 'backup-' + transaction['id']
            or not digest(transaction.get('before_manifest_sha256'))):
        raise ValueError('unsafe_transaction')
    for name in ('before_sha256', 'after_sha256'):
        values = transaction.get(name)
        if not isinstance(values, dict) or set(values) != set(TARGET_FILES) or not all(map(digest, values.values())):
            raise ValueError('unsafe_transaction')
    if transaction['state'] in ('pending_health', 'verified') and not digest(transaction.get('after_manifest_sha256')):
        raise ValueError('unsafe_transaction')
    return child_path(state_dir(root) / 'updates', transaction['backup_name'])


def verify_after(root, transaction):
    validate_transaction(root, transaction)
    for name, expected in transaction['after_sha256'].items():
        if sha(child_path(root, name)) != expected:
            raise ValueError('update_runtime_changed')
    if sha(Path(root).resolve().parent / 'manifest.json') != transaction['after_manifest_sha256']:
        raise ValueError('update_manifest_changed')


def rollback_update(root, transaction):
    root = Path(root).resolve()
    try:
        backup = validate_transaction(root, transaction)
        # Verify the complete backup before modifying any application file.
        for name in TARGET_FILES:
            if sha(child_path(backup, 'app/' + name)) != transaction['before_sha256'][name]:
                raise ValueError('rollback_backup_changed')
        if sha(backup / 'manifest.json') != transaction['before_manifest_sha256']:
            raise ValueError('rollback_manifest_changed')
        for name in TARGET_FILES:
            target = child_path(root, name)
            durable_copy(child_path(backup, 'app/' + name), target)
            if sha(target) != transaction['before_sha256'][name]:
                raise ValueError('rollback_copy_failed')
        durable_copy(backup / 'manifest.json', root.parent / 'manifest.json')
        if sha(root.parent / 'manifest.json') != transaction['before_manifest_sha256']:
            raise ValueError('rollback_manifest_failed')
        transaction['state'] = 'rolled_back'
        transaction.pop('handoff_sha256', None)
        atomic_json(state_dir(root) / 'update-transaction.json', transaction)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RollbackError('update_rollback_failed') from error


def recover_interrupted_update(root, handoff_token=''):
    try:
        transaction = read_json(state_dir(root) / 'update-transaction.json')
    except FileNotFoundError:
        return None
    validate_transaction(root, transaction)
    if transaction['state'] not in ('installing', 'pending_health'):
        return transaction if transaction['state'] == 'rolled_back' else None
    # A one-use process environment token permits the intended new supervisor
    # to check the worker. A fresh scheduled-task/PC start never has this token.
    if (transaction['state'] == 'pending_health' and handoff_token
            and digest(transaction.get('handoff_sha256'))
            and hashlib.sha256(handoff_token.encode()).hexdigest() == transaction['handoff_sha256']):
        try:
            verify_after(root, transaction)
            return transaction
        except (OSError, ValueError):
            rollback_update(root, transaction)
            return transaction
    rollback_update(root, transaction)
    return transaction
