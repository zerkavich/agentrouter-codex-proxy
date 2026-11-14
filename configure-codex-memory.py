"""Configure Codex memory models without changing other settings.

This is an installation tool. The proxy and its supervisor never import it.
Install requirements-installer.txt before calling it directly.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import uuid

import tomlkit

DEFAULT_MODEL = "gpt-6.1-sol"
MODEL_FIELDS = ("extract_model", "consolidation_model")


class MemorySetupError(ValueError):
    """A safe error message that contains no configuration values."""


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        target = Path(explicit).expanduser()
    else:
        home = os.environ.get("CODEX_HOME", "").strip()
        target = (Path(home).expanduser() if home else Path.home() / ".codex") / "config.toml"
    # Update the target of a shared/symlinked Codex home, preserving the link.
    return target.resolve()


def _parse(text: str):
    try:
        return tomlkit.parse(text)
    except (ValueError, TypeError):
        # Parser errors can include a line from config.toml, including secrets.
        raise MemorySetupError("Invalid Codex TOML configuration; no changes made.") from None


def _private_fd(path: Path) -> int:
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    # Windows ignores the POSIX 0600 mode. Give new backups/temp files access
    # only to their owner, administrators and SYSTEM, even in a shared home.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                      ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                      wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    descriptor = wintypes.LPVOID()
    if not convert("D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)", 1,
                   ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        handle = create(str(path), 0x40000000, 1, ctypes.byref(attrs), 1, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    finally:
        kernel.LocalFree(descriptor)


def _write_private(path: Path, content: bytes) -> None:
    descriptor = _private_fd(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != content:
            raise MemorySetupError("Could not verify the saved file; no configuration replaced.")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _replace_existing(path: Path, temporary: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                           wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        replace.restype = wintypes.BOOL
        # Unlike os.replace on Windows, ReplaceFile preserves the original
        # file's DACL. Do not ignore ACL-merge errors.
        if not replace(str(path), str(temporary), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        previous = path.stat()
        current = temporary.stat()
        if (current.st_uid, current.st_gid) != (previous.st_uid, previous.st_gid):
            os.chown(temporary, previous.st_uid, previous.st_gid)
        os.chmod(temporary, stat.S_IMODE(previous.st_mode))
        os.replace(temporary, path)


def configure(config: str | Path | None = None, model: str = DEFAULT_MODEL,
              *, overwrite: bool = False, dry_run: bool = False) -> dict:
    if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
        raise MemorySetupError("Memory model must be a non-empty name without control characters.")
    model = model.strip()
    path = config_path(config)
    original = path.read_bytes() if path.exists() else None
    encoded = original if original is not None else b""
    bom = encoded.startswith(b"\xef\xbb\xbf")
    try:
        text = encoded.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MemorySetupError("Codex config must be valid UTF-8; no changes made.") from None
    document = _parse(text)
    before = document.unwrap()
    memory = before.get("memories", {})
    if not isinstance(memory, dict):
        raise MemorySetupError("The memories setting must be a TOML table; no changes made.")
    fields = [name for name in MODEL_FIELDS if name not in memory or
              (overwrite and memory[name] != model)]
    if not overwrite and any(name in memory and
                             (not isinstance(memory[name], str) or not memory[name].strip())
                             for name in MODEL_FIELDS):
        raise MemorySetupError("Existing memory models must be non-empty names; no changes made.")
    result = {"config": str(path), "status": "unchanged", "fields": fields, "backup": None}
    if not fields:
        return result
    if "memories" not in document:
        document["memories"] = tomlkit.table()
    for name in fields:
        document["memories"][name] = model
    rendered = tomlkit.dumps(document)
    if "\r\n" in text and "\n" not in text.replace("\r\n", ""):
        rendered = rendered.replace("\r\n", "\n").replace("\n", "\r\n")
    expected = copy.deepcopy(before)
    expected.setdefault("memories", {}).update({name: model for name in fields})
    if _parse(rendered).unwrap() != expected:
        raise MemorySetupError("Other Codex settings would change; no changes made.")
    content = (b"\xef\xbb\xbf" if bom else b"") + rendered.encode("utf-8")
    if dry_run:
        result["status"] = "would_update"
        return result

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary = path.with_name(f".{path.name}.memory-{token}.tmp")
    if original is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.memory-{stamp}-{token[:8]}.bak")
        _write_private(backup, original)
        result["backup"] = str(backup)
    try:
        _write_private(temporary, content)
        if original is None:
            # Atomic creation that cannot replace a concurrently created file.
            os.link(temporary, path)
        else:
            if path.read_bytes() != original:
                raise MemorySetupError("Codex config changed during setup; retry without concurrent edits.")
            _replace_existing(path, temporary)
        result["status"] = "updated"
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Override CODEX_HOME/config.toml or ~/.codex/config.toml")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--overwrite", action="store_true", help="Replace explicitly selected memory models")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating any files")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    args = parser.parse_args(argv)
    try:
        result = configure(args.config, args.model, overwrite=args.overwrite, dry_run=args.dry_run)
    except MemorySetupError as error:
        print(f"[memory] {error}", file=sys.stderr)
        return 1
    except OSError:
        print("[memory] Cannot read or write the Codex config/backup. Check access and available disk space; "
              "no successful setup was recorded.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(f"[memory] {result['status']}: {result['config']}")
        if result["fields"]:
            print("[memory] Fields: " + ", ".join(result["fields"]))
        if result["backup"]:
            print("[memory] Backup: " + result["backup"])
        if result["status"] == "updated":
            print("[memory] Reload Codex / Developer: Reload Window in VS Code to apply.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
