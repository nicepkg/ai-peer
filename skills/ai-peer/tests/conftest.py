"""Shared fixtures for ai-peer tests."""
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs running daemon (slow)")


@pytest.fixture(scope="session")
def daemon():
    """Start daemon for integration tests, stop after all tests."""
    from ai_peer.client import ensure_daemon, PeerClient
    from ai_peer.constants import PID_FILE

    client = ensure_daemon()
    yield client

    # Cleanup — use subprocess-agnostic termination (works on Windows too)
    import os
    import subprocess
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # os.kill + SIGTERM is not portable; use psutil-free approach
            import signal as _sig
            sig = getattr(_sig, "SIGTERM", None)
            if sig is not None:
                os.kill(pid, sig)
            else:
                # Windows fallback
                subprocess.call(["taskkill", "/PID", str(pid), "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (ProcessLookupError, ValueError, OSError):
            pass
        PID_FILE.unlink(missing_ok=True)
