"""Installation-only Codex config changes, with disposable profiles/tasks."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import venv

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_memory_setup", ROOT / "configure-codex-memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(directory: Path, script: str) -> subprocess.CompletedProcess:
    runner = directory / "test-runner.ps1"
    # Windows PowerShell 5.1 requires a BOM for Unicode paths in scripts.
    runner.write_text(script, encoding="utf-8-sig")
    # CI starts Python from PowerShell 7. Let a Windows PowerShell 5.1 child
    # choose its own built-in modules instead of inheriting PS7-only DLLs.
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(runner)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=40,
                          env=environment,
                          creationflags=0x08000000 if os.name == "nt" else 0)


class MemoryConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "config.toml"

    def write(self, text: str):
        self.path.write_bytes(text.encode("utf-8"))

    def values(self):
        return tomlkit.parse(self.path.read_bytes().decode("utf-8-sig")).unwrap()

    def test_only_model_fields_change_and_backup_is_exact(self):
        source = ('# keep this comment\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n'
                  'model_provider = "Agentrouter"\n[model_providers.Agentrouter]\n'
                  'base_url = "http://127.0.0.1:8788/v1" # unchanged\n'
                  'experimental_bearer_token = "synthetic-private-token"\n'
                  '[features]\nmemories = true\nmulti_agent = true\n'
                  '[memories] # keep table comment\ngenerate_memories = false\nuse_memories = true\n')
        self.write(source)
        before = self.values()
        result = memory.configure(self.path)
        expected = before
        expected["memories"].update({key: memory.DEFAULT_MODEL for key in memory.MODEL_FIELDS})
        self.assertEqual(self.values(), expected)
        self.assertEqual(Path(result["backup"]).read_bytes(), source.encode())
        for comment in ("# keep this comment", "# unchanged", "# keep table comment"):
            self.assertIn(comment, self.path.read_text())
        self.assertNotIn("synthetic-private-token", json.dumps(result))

    def test_explicit_choices_and_original_bytes_are_preserved(self):
        source = '[memories]\nextract_model = "custom-a" # keep\nconsolidation_model = "custom-b"\n'
        self.write(source)
        result = memory.configure(self.path)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.path.read_bytes(), source.encode())
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_missing_choice_is_added_without_replacing_existing_one(self):
        self.write('[memories]\nextract_model = "custom"\n')
        result = memory.configure(self.path)
        self.assertEqual(result["fields"], ["consolidation_model"])
        self.assertEqual(self.values()["memories"],
                         {"extract_model": "custom", "consolidation_model": memory.DEFAULT_MODEL})

    def test_overwrite_is_explicit_and_preserves_enable_flags(self):
        self.write('[memories]\nextract_model = "terra" # selection\n'
                   'consolidation_model = "custom"\ngenerate_memories = false\n')
        memory.configure(self.path, "alternative", overwrite=True)
        self.assertEqual(self.values()["memories"], {"extract_model": "alternative",
                         "consolidation_model": "alternative", "generate_memories": False})
        self.assertIn("# selection", self.path.read_text())

    def test_second_run_changes_nothing_and_makes_no_backup(self):
        self.write('model = "main"\n')
        memory.configure(self.path)
        original = self.path.read_bytes()
        files = set(self.root.iterdir())
        self.assertEqual(memory.configure(self.path, overwrite=True)["status"], "unchanged")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(set(self.root.iterdir()), files)

    def test_table_spellings_and_layouts(self):
        for source in ('memories = { use_memories = false, extract_model = "custom" } # inline\n',
                       'memories.use_memories = false\nmemories.extract_model = "custom" # dotted\n',
                       '["memories"] # quoted\nuse_memories = false\nextract_model = "custom"\n',
                       "['memories']\nuse_memories = false\nextract_model = 'custom'\n",
                       '[memories.nested]\ncustom = "keep"\n'):
            with self.subTest(source=source):
                self.write(source)
                before = self.values()
                memory.configure(self.path)
                expected = before
                expected["memories"].setdefault("extract_model", memory.DEFAULT_MODEL)
                expected["memories"]["consolidation_model"] = memory.DEFAULT_MODEL
                self.assertEqual(self.values(), expected)
                for line in source.splitlines():
                    if "#" in line:
                        self.assertIn(line.split("#", 1)[1], self.path.read_text())

    def test_unicode_bom_and_crlf_are_preserved(self):
        self.path = self.root / "юзер config.toml"
        source = ('\ufeff# память 日本語\r\nmodel = "main"\r\n'
                  'description = """line one\r\nline two"""\r\n'
                  '[memories]\r\nuse_memories = false\r\n').encode()
        self.path.write_bytes(source)
        before = self.values()
        memory.configure(self.path)
        content = self.path.read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        self.assertIn("# память 日本語".encode(), content)
        self.assertEqual(self.values()["description"], before["description"])

    def test_dry_run_does_not_create_directories_or_config(self):
        path = self.root / "missing" / ".codex" / "config.toml"
        self.assertEqual(memory.configure(path, dry_run=True)["status"], "would_update")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_dry_run_does_not_change_existing_file_or_create_backup(self):
        self.write('model = "main"\n')
        source = self.path.read_bytes()
        self.assertEqual(memory.configure(self.path, dry_run=True)["status"], "would_update")
        self.assertEqual(self.path.read_bytes(), source)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_new_config_does_not_enable_memories_or_create_backup(self):
        result = memory.configure(self.path)
        self.assertIsNone(result["backup"])
        self.assertEqual(self.values(), {"memories": {key: memory.DEFAULT_MODEL for key in memory.MODEL_FIELDS}})

    def test_codex_home_explicit_path_and_default_home(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "shared")}), \
                mock.patch.object(memory.Path, "home", return_value=self.root / "user"):
            self.assertEqual(memory.config_path(), self.root / "shared" / "config.toml")
            self.assertEqual(memory.config_path(self.path), self.path)
            os.environ["CODEX_HOME"] = ""
            self.assertEqual(memory.config_path(), self.root / "user" / ".codex" / "config.toml")

    def test_symlink_target_is_updated_without_replacing_link(self):
        self.write('model = "main"\n')
        link = self.root / "linked.toml"
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest("Creating symlinks is unavailable for this user")
        result = memory.configure(link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(result["config"], str(self.path))
        self.assertIn("memories", self.values())

    def test_invalid_toml_or_encoding_has_no_mutation_and_safe_error(self):
        for source in (b'credential = "SYNTHETIC_SECRET"\n[memories\n',
                       b'memories = "SYNTHETIC_SECRET"\n', b'\xffbad utf8'):
            with self.subTest(source=source):
                self.path.write_bytes(source)
                output = io.StringIO()
                files = set(self.root.iterdir())
                with contextlib.redirect_stderr(output):
                    self.assertEqual(memory.main(["--config", str(self.path)]), 1)
                self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())
                self.assertEqual(self.path.read_bytes(), source)
                self.assertEqual(set(self.root.iterdir()), files)

    def test_invalid_model_names_are_refused_before_writing(self):
        for name in ("", "  ", "model\nsecret", "model\x00"):
            with self.subTest(name=name), self.assertRaises(memory.MemorySetupError):
                memory.configure(self.path, name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_backup_leaves_config_untouched(self):
        self.write('model = "main"\n')
        before = self.path.read_bytes()
        with mock.patch.object(memory, "_write_private", side_effect=PermissionError), self.assertRaises(OSError):
            memory.configure(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failed_replace_keeps_config_and_verified_backup(self):
        self.write('model = "main"\n')
        before = self.path.read_bytes()
        with mock.patch.object(memory, "_replace_existing", side_effect=PermissionError), self.assertRaises(OSError):
            memory.configure(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path.read_bytes() for path in self.root.glob("*.bak")], [before])
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_concurrent_edit_is_not_overwritten(self):
        self.write('model = "before"\n')
        original_write = memory._write_private
        def concurrent(path, content):
            original_write(path, content)
            if path.suffix == ".tmp":
                self.path.write_bytes(b'model = "concurrent"\n')
        with mock.patch.object(memory, "_write_private", side_effect=concurrent), \
                self.assertRaisesRegex(memory.MemorySetupError, "changed during setup"):
            memory.configure(self.path)
        self.assertEqual(self.path.read_bytes(), b'model = "concurrent"\n')
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "POSIX permission behavior")
    def test_posix_existing_permissions_and_private_backup(self):
        self.write('model = "main"\n')
        self.path.chmod(0o640)
        result = memory.configure(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(Path(result["backup"]).stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows ACL behavior")
    def test_windows_config_acl_is_preserved_and_backup_is_private(self):
        self.write('model = "main"\n')
        share = run_ps(self.root, "$ErrorActionPreference = 'Stop'\n"
                       f"$acl = Get-Acl -LiteralPath {ps_quote(self.path)}\n"
                       "$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')\n"
                       "$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'Read', 'Allow')\n"
                       "$acl.AddAccessRule($rule)\n"
                       f"Set-Acl -LiteralPath {ps_quote(self.path)} -AclObject $acl\n"
                       f"$acl = Get-Acl -LiteralPath {ps_quote(self.path)}\n"
                       "$acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::Access)\n")
        self.assertEqual(share.returncode, 0, share.stderr)
        result = memory.configure(self.path)
        inspect = run_ps(self.root, f"$acl = Get-Acl -LiteralPath {ps_quote(self.path)}\n"
                         f"$backup = Get-Acl -LiteralPath {ps_quote(result['backup'])}\n"
                         "@{ config = $acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::Access); "
                         "protected = $backup.AreAccessRulesProtected; sids = @($backup.Access | ForEach-Object { "
                         "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }) } | ConvertTo-Json -Compress\n")
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        actual = json.loads(inspect.stdout.lstrip("\ufeff"))
        self.assertEqual(actual["config"], share.stdout.strip())
        self.assertTrue(actual["protected"])
        self.assertEqual(set(actual["sids"]), {"S-1-5-18", "S-1-5-32-544", "S-1-3-4"})


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
class PowerShellMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "profile with spaces.toml"

    def assert_ps(self, script):
        result = run_ps(self.root, "$ErrorActionPreference = 'Stop'\n" + script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_all_modified_powershell_entrypoints_parse(self):
        for name in ("configure-codex-memory.ps1", "install-autostart.ps1", "start-proxy.ps1"):
            self.assert_ps("$tokens = $null; $errors = $null\n"
                           f"[void][System.Management.Automation.Language.Parser]::ParseFile({ps_quote(ROOT / name)}, [ref]$tokens, [ref]$errors)\n"
                           "if ($errors) { throw 'Invalid PowerShell syntax' }\n")

    def test_wrapper_updates_and_previews_only_the_explicit_config(self):
        self.config.write_bytes(b'model = "main"\n')
        helper = ROOT / "configure-codex-memory.ps1"
        arguments = f"-Python {ps_quote(sys.executable)} -CodexConfig {ps_quote(self.config)}"
        self.assert_ps(f"& {ps_quote(helper)} {arguments} -WhatIf_\n")
        self.assertEqual(self.config.read_bytes(), b'model = "main"\n')
        self.assertEqual(list(self.root.glob("*.bak")), [])
        self.assert_ps(f"& {ps_quote(helper)} {arguments} -MemoryModel test-model\n")
        values = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(values, {"model": "main", "memories": {key: "test-model" for key in memory.MODEL_FIELDS}})
        self.assertEqual(len(list(self.root.glob("*.bak"))), 1)

    def test_wrapper_whatif_without_parser_does_not_install_packages(self):
        isolated = self.root / "without-parser"
        venv.EnvBuilder(with_pip=False).create(isolated)
        interpreter = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        before = set(isolated.rglob("*"))
        error_file = self.root / "preview-error.txt"
        script = ("$ErrorActionPreference = 'Stop'\nRemove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue\n" +
                  f"try {{ & {ps_quote(ROOT / 'configure-codex-memory.ps1')} -Python {ps_quote(interpreter)} "
                  f"-CodexConfig {ps_quote(self.config)} -WhatIf_ }} catch {{ "
                  f"$_.Exception.Message | Set-Content -LiteralPath {ps_quote(error_file)} -Encoding UTF8; exit 1 }}\n")
        result = run_ps(self.root, script)
        self.assertNotEqual(result.returncode, 0)
        # Test the exception itself, independent of ANSI styling/line wrapping
        # in PowerShell 7's human-readable stderr renderer.
        self.assertIn("no packages or files were changed", error_file.read_text(encoding="utf-8-sig"))
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.root.glob("*.bak")), [])
        self.assertEqual(set(isolated.rglob("*")), before)

    def fixture(self):
        for name in ("start-proxy.ps1", "install-autostart.ps1"):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / "service.py").write_text("# synthetic launcher target\n")
        launcher = self.root / ".venv" / "Scripts" / "pythonw.exe"
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        helper = ("param($MemoryModel, $CodexConfig, [switch]$OverwriteMemoryModels, $Python)\n"
                  "@{ model = $MemoryModel; config = $CodexConfig; overwrite = [bool]$OverwriteMemoryModels } | "
                  f"ConvertTo-Json -Compress | Set-Content -LiteralPath {ps_quote(self.root / 'memory.json')} -Encoding UTF8\n"
                  "if ($env:MEMORY_HELPER_TEST_FAILURE -eq '1') { throw 'synthetic helper failure' }\n")
        (self.root / "configure-codex-memory.ps1").write_text(helper, encoding="utf-8-sig")
        return ("$script:registered = $false\n"
                "function Get-ScheduledTask { if ($script:registered) { [pscustomobject]@{TaskName='test'} } }\n"
                "function New-ScheduledTaskAction { [pscustomobject]@{} }\n"
                "function New-ScheduledTaskTrigger { [pscustomobject]@{Delay=''} }\n"
                "function New-ScheduledTaskPrincipal { [pscustomobject]@{} }\n"
                "function New-ScheduledTaskSettingsSet { [pscustomobject]@{Hidden=$false} }\n"
                "function Register-ScheduledTask { $script:registered = $true; "
                f"'registered' | Set-Content -LiteralPath {ps_quote(self.root / 'registered.txt')} }}\n"
                "function Start-ScheduledTask {}\n"
                "function Start-Sleep {}\n"
                "function Get-NetTCPConnection {}\n"
                "function Invoke-RestMethod { [pscustomobject]@{status='ok'; listen='127.0.0.1:18788'} }\n"
                f"function Start-Process {{ 'started' | Set-Content -LiteralPath {ps_quote(self.root / 'started.txt')} }}\n"
                f"function Get-Command {{ param($Name); if ($Name -eq 'python') {{ [pscustomobject]@{{Source={ps_quote(sys.executable)}}} }} "
                f"elseif ($Name -eq 'pythonw.exe') {{ [pscustomobject]@{{Source={ps_quote(launcher)}}} }} }}\n")

    def test_autostart_forwards_model_options_before_registration(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'install-autostart.ps1')} "
                       f"-CodexConfig {ps_quote(self.config)} -MemoryModel selected -OverwriteMemoryModels\n")
        arguments = json.loads((self.root / "memory.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(arguments, {"model": "selected", "config": str(self.config), "overwrite": True})
        self.assertTrue((self.root / "registered.txt").exists())

    def test_autostart_whatif_does_not_run_helper_or_register_task(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'install-autostart.ps1')} -WhatIf_\n")
        self.assertFalse((self.root / "memory.json").exists())
        self.assertFalse((self.root / "registered.txt").exists())
        self.assertFalse(self.config.exists())

    def test_autostart_skip_leaves_memory_configuration_alone(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'install-autostart.ps1')} -SkipMemoryModelSetup\n")
        self.assertFalse((self.root / "memory.json").exists())
        self.assertTrue((self.root / "registered.txt").exists())

    def test_autostart_helper_failure_prevents_registration(self):
        script = self.fixture()
        result = run_ps(self.root, "$ErrorActionPreference = 'Stop'\n" + script +
                        "$env:MEMORY_HELPER_TEST_FAILURE = '1'\n" +
                        f"& {ps_quote(self.root / 'install-autostart.ps1')}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "memory.json").exists())
        self.assertFalse((self.root / "registered.txt").exists())

    def test_background_start_configures_memory_without_starting_real_process(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'start-proxy.ps1')} -Background -Port 18788 "
                       f"-CodexConfig {ps_quote(self.config)} -MemoryModel selected\n")
        self.assertTrue((self.root / "started.txt").exists())
        arguments = json.loads((self.root / "memory.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(arguments["model"], "selected")
        self.assertEqual(arguments["config"], str(self.config))

    def test_service_flag_skips_memory_setup(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'start-proxy.ps1')} -Background -Service -Port 18788\n")
        self.assertTrue((self.root / "started.txt").exists())
        self.assertFalse((self.root / "memory.json").exists())

    def test_background_start_skip_leaves_memory_configuration_alone(self):
        script = self.fixture()
        self.assert_ps(script + f"& {ps_quote(self.root / 'start-proxy.ps1')} -Background -SkipMemoryModelSetup -Port 18788\n")
        self.assertTrue((self.root / "started.txt").exists())
        self.assertFalse((self.root / "memory.json").exists())


if __name__ == "__main__":
    unittest.main()
