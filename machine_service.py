"""Protected entrypoint for the shared Windows installation."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for name in ('CODEX_GATEWAY_API_KEY', 'AGENTROUTER_API_KEY', 'OPENAI_API_KEY'):
    os.environ.pop(name, None)
os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8:replace',
                  PROXY_AUTO_UPDATE='0', PROXY_CONTROL_UPDATES='1',
                  PROXY_SERVICE_ENTRY=str(Path(__file__).resolve()))
os.environ.setdefault('PROXY_CONTROL_STATE_DIR', str(ROOT.parent / 'control'))
sys.dont_write_bytecode = True


if __name__ == '__main__':
    try:
        # This bootstrap is excluded from remote update targets. It must remain
        # importable when a power loss interrupts mutable Python file writes.
        import update_guard
        update_guard.recover_interrupted_update(ROOT, os.environ.get('PROXY_UPDATE_HANDOFF', ''))
        import service
        raise SystemExit(service.main())
    except Exception:
        log = ROOT / 'logs/machine-launch-error.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text(traceback.format_exc(), encoding='utf-8')
        # A cold import/main failure must also restore the last working runtime.
        # Relaunch at most once, in a fresh interpreter after verified rollback.
        if os.environ.get('PROXY_RECOVERY_ATTEMPT') != '1':
            try:
                import update_guard
                transaction = update_guard.read_json(update_guard.state_dir(ROOT) / 'update-transaction.json')
                if transaction['state'] not in ('installing', 'pending_health'):
                    raise ValueError('no_pending_update')
                update_guard.rollback_update(ROOT, transaction)
                os.environ.pop('PROXY_UPDATE_HANDOFF', None)
                os.environ['PROXY_RECOVERY_ATTEMPT'] = '1'
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000 | 0x00000008 | 0x00000200 if sys.platform == 'win32' else 0)
            except (OSError, ValueError, KeyError):
                pass
        raise
