"""Prepared browser updates retain backups and refuse file/dependency drift."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_updates as updates
import update_guard
from control import atomic_json


class PanelUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Hosted Windows runners can expose TEMP through a short DOS path.
        # Match the updater's canonical paths so copy-fault injection fires.
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'app'
        self.root.mkdir()
        self.files = {name: b'# old\n' if name.endswith('.py') else b'old asset\n'
                      for name in updates.REQUIRED_FILES}
        self.files.update({'proxy.py': b'VERSION = "1.8.0"\n',
                           'requirements.txt': b'httpcore==1.0.9\n',
                           'update_guard.py': Path(update_guard.__file__).read_bytes()})
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = {'files': [{'path': 'app/' + name, 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}
                              for name, content in self.files.items()], 'proxy_version': '1.8.0'}
        self.manifest_bytes = (json.dumps(manifest) + '\n').encode()
        (self.base / 'manifest.json').write_bytes(self.manifest_bytes)
        self.commit = 'a' * 40
        self.folder = self.root / 'logs/updates' / self.commit
        self.candidate = self.folder / 'candidate'
        self.new = {**self.files, 'proxy.py': b'VERSION = "1.8.1"\n', 'control.py': b'# new\n', 'panel/index.html': b'new panel'}
        for name, content in self.new.items():
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        atomic_json(self.folder / 'prepared.json', {'state': 'prepared', 'commit': self.commit, 'version': '1.8.1', 'probe_passed': True,
                    'hashes': {name: hashlib.sha256(content).hexdigest() for name, content in self.new.items()}})
        self.env = mock.patch.dict(os.environ, {'PROXY_CONTROL_UPDATES': '1',
                                               'PROXY_CONTROL_STATE_DIR': str(self.root / 'logs')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def assert_previous(self):
        for name, content in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertEqual((self.base / 'manifest.json').read_bytes(), self.manifest_bytes)

    def test_install_and_rollback_preserve_config_and_backup(self):
        env = self.root / '.env'
        env.write_bytes(b'# do not alter\nTOKEN=synthetic\n')
        transaction = updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assertEqual(transaction['state'], 'pending_health')
        for name, content in self.new.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        updates.rollback_update(self.root, transaction)
        self.assert_previous()
        self.assertEqual(env.read_bytes(), b'# do not alter\nTOKEN=synthetic\n')
        self.assertTrue((self.root / 'logs/updates' / ('backup-' + 'b' * 32)).is_dir())

    def test_installed_drift_is_refused_without_modifying_other_files(self):
        (self.root / 'control.py').write_bytes(b'# custom change\n')
        with self.assertRaisesRegex(ValueError, 'runtime_drift'):
            updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assertEqual((self.root / 'proxy.py').read_bytes(), self.files['proxy.py'])

    def test_changed_dependency_is_refused_before_copy(self):
        (self.candidate / 'requirements.txt').write_bytes(b'httpcore==99\n')
        with self.assertRaisesRegex(ValueError, 'dependency_update_requires_admin'):
            updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assert_previous()

    def test_failed_copy_restores_previous_runtime(self):
        original_copy = updates.shutil.copyfile
        def corrupt(source, destination):
            result = original_copy(source, destination)
            if Path(source) == self.candidate / 'control.py':
                Path(destination).write_bytes(b'# corrupt\n')
            return result
        with mock.patch.object(updates.shutil, 'copyfile', side_effect=corrupt), self.assertRaises(ValueError):
            updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assert_previous()

    def test_interrupted_install_recovers_on_next_supervisor_start(self):
        updates.install_prepared(self.root, self.commit, 'b' * 32)
        updates.recover_interrupted_update(self.root)
        self.assert_previous()
        receipt = json.loads((self.root / 'logs/update-transaction.json').read_text())
        self.assertEqual(receipt['state'], 'rolled_back')

    def test_corrupt_mutable_modules_recover_with_only_the_bootstrap(self):
        import subprocess
        updates.install_prepared(self.root, self.commit, 'b' * 32)
        (self.root / 'panel_updates.py').write_bytes(b'not valid python {{{')
        (self.root / 'service.py').write_bytes(b'not valid python {{{')
        code = "import sys; sys.path.insert(0, sys.argv[1]); import update_guard; update_guard.recover_interrupted_update(sys.argv[1])"
        options = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(self.root)],
                                env=os.environ.copy(), capture_output=True, timeout=10, **options)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assert_previous()

    def test_backup_manifest_corruption_is_refused_before_any_restore(self):
        transaction = updates.install_prepared(self.root, self.commit, 'b' * 32)
        backup = self.root / 'logs/updates' / transaction['backup_name']
        (backup / 'manifest.json').write_bytes(b'{}')
        with self.assertRaisesRegex(update_guard.RollbackError, 'update_rollback_failed'):
            updates.rollback_update(self.root, transaction)
        self.assertEqual((self.root / 'proxy.py').read_bytes(), self.new['proxy.py'])

    def test_unsafe_identity_and_backup_paths_cannot_write_outside_state(self):
        for commit, identity in (('../outside', 'b' * 32), (self.commit, '../outside')):
            with self.assertRaises(ValueError):
                updates.install_prepared(self.root, commit, identity)
        transaction = updates.install_prepared(self.root, self.commit, 'b' * 32)
        transaction['backup_name'] = '../outside'
        with self.assertRaises(update_guard.RollbackError):
            updates.rollback_update(self.root, transaction)
        self.assertFalse((self.base / 'outside').exists())

    def test_changed_bootstrap_and_incomplete_prepared_hashes_are_refused(self):
        (self.candidate / 'update_guard.py').write_bytes(b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'bootstrap_update_requires_admin'):
            updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assert_previous()
        (self.candidate / 'update_guard.py').write_bytes(self.files['update_guard.py'])
        prepared = json.loads((self.folder / 'prepared.json').read_text())
        prepared['hashes'].pop('control.py')
        atomic_json(self.folder / 'prepared.json', prepared)
        with self.assertRaisesRegex(ValueError, 'update_prepared_invalid'):
            updates.install_prepared(self.root, self.commit, 'b' * 32)
        self.assert_previous()

    def test_invalid_manifest_paths_duplicates_and_oversize_are_refused(self):
        data = json.loads(self.manifest_bytes)
        for path in ('../outside', 'C:/outside', 'app\\proxy.py'):
            invalid = {**data, 'files': [*data['files'], {'path': path, 'sha256': 'a' * 64}]}
            atomic_json(self.base / 'manifest.json', invalid)
            with self.assertRaisesRegex(ValueError, 'unsafe_manifest'):
                updates.manifest_files(self.root)
        atomic_json(self.base / 'manifest.json', {**data, 'files': [*data['files'], data['files'][0]]})
        with self.assertRaisesRegex(ValueError, 'unsafe_manifest'):
            updates.manifest_files(self.root)
        (self.base / 'manifest.json').write_bytes(b' ' * (1024 * 1024 + 1))
        self.assertIsNone(updates.installed_commit(self.root))

    def remote(self, *, good_ci=True, extra=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, content in self.new.items():
                archive.writestr('fork-' + self.commit + '/' + name, content)
            if extra is not None:
                archive.writestr('fork-' + self.commit + '/' + extra[0], extra[1])
        checks = [{'name': f'{platform}-latest / Python {python}', 'head_sha': self.commit,
                   'status': 'completed', 'conclusion': 'success', 'app': {'slug': 'github-actions'}}
                  for platform in ('ubuntu', 'windows') for python in ('3.10', '3.12')]
        if not good_ci:
            checks[0]['conclusion'] = 'failure'
        def read(url, limit=updates.MAX_BYTES):
            if url.endswith('/commits/main'):
                return json.dumps({'sha': self.commit}).encode()
            if url.endswith('/check-runs'):
                return json.dumps({'check_runs': checks}).encode()
            return buffer.getvalue()
        return read

    def test_failed_ci_and_probe_keep_installed_runtime_untouched(self):
        import shutil
        shutil.rmtree(self.folder)
        with mock.patch.object(updates, 'read', side_effect=self.remote(good_ci=False)):
            self.assertEqual(updates.prepare_update(self.root, self.commit)['error_code'], 'update_ci_not_passed')
        with mock.patch.object(updates, 'read', side_effect=self.remote()), mock.patch.object(
                updates, 'probe_candidate', side_effect=ValueError('update_probe_failed')):
            self.assertEqual(updates.prepare_update(self.root, self.commit)['error_code'], 'update_probe_failed')
        self.assert_previous()

    def test_prepare_extracts_only_allowlisted_files_and_revalidates_cached_hashes(self):
        import shutil
        shutil.rmtree(self.folder)
        with mock.patch.object(updates, 'read', side_effect=self.remote(extra=('../../outside', b'ignored'))), mock.patch.object(
                updates, 'probe_candidate', return_value={'probe_passed': True, 'version': '1.8.1'}):
            self.assertEqual(updates.prepare_update(self.root, self.commit)['state'], 'prepared')
            self.assertEqual(set(str(path.relative_to(self.candidate)).replace('\\', '/') for path in self.candidate.rglob('*') if path.is_file()),
                             set(updates.REQUIRED_FILES))
            (self.candidate / 'panel/index.html').write_bytes(b'changed')
            self.assertEqual(updates.prepare_update(self.root, self.commit)['error_code'], 'update_prepared_changed')
        self.assert_previous()

    def test_failed_probe_leaves_no_poisoned_package_and_can_retry(self):
        import shutil
        shutil.rmtree(self.folder)
        with mock.patch.object(updates, 'read', side_effect=self.remote()), mock.patch.object(
                updates, 'probe_candidate', side_effect=ValueError('update_probe_failed')):
            self.assertEqual(updates.prepare_update(self.root, self.commit)['error_code'], 'update_probe_failed')
        self.assertFalse(self.folder.exists())
        self.assertFalse(list(self.folder.parent.glob('prepare-*')))
        self.assert_previous()
        with mock.patch.object(updates, 'read', side_effect=self.remote()), mock.patch.object(
                updates, 'probe_candidate', return_value={'probe_passed': True, 'version': '1.8.1'}):
            self.assertEqual(updates.prepare_update(self.root, self.commit)['state'], 'prepared')
        self.assertTrue((self.folder / 'prepared.json').is_file())
        self.assertFalse(list(self.folder.parent.glob('prepare-*')))
        self.assert_previous()

    def test_handoff_token_is_temporary_and_fresh_start_rolls_back(self):
        transaction = updates.install_prepared(self.root, self.commit, 'b' * 32)
        transaction['handoff_sha256'] = hashlib.sha256(b'synthetic-handoff').hexdigest()
        atomic_json(self.root / 'logs/update-transaction.json', transaction)
        result = update_guard.recover_interrupted_update(self.root, 'synthetic-handoff')
        self.assertEqual(result['state'], 'pending_health')
        self.assertEqual((self.root / 'proxy.py').read_bytes(), self.new['proxy.py'])
        update_guard.recover_interrupted_update(self.root)
        self.assert_previous()
