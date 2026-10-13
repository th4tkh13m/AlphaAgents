"""Exercise actual cross-thread notification and executor teardown under EPERM."""

import os
import subprocess
import sys
from pathlib import Path

from alpha_agents.tree_search.mutators.coding_agents.codex.roles import verification


def test_restricted_socket_notification_finishes_thread_and_executor_shutdown():
    script = '''
import asyncio, errno, socket
from alpha_agents.tree_search.mutators.coding_agents.codex.roles.verification import repair_selector_wakeup
original_send = socket.socket.send
def restricted_send(self, *args, **kwargs):
    if self.family == socket.AF_UNIX:
        raise PermissionError(errno.EPERM, "sandbox sendto restriction")
    return original_send(self, *args, **kwargs)
def forbidden_endpoint(*args, **kwargs):
    raise AssertionError("notification must not connect or bind a network endpoint")
socket.socket.send = restricted_send
socket.socket.connect = forbidden_endpoint
socket.socket.bind = forbidden_endpoint
assert repair_selector_wakeup()
async def work():
    return await asyncio.to_thread(lambda: 42)
assert asyncio.run(work()) == 42
print("thread result and executor shutdown completed")
'''
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert "executor shutdown completed" in result.stdout


def test_unrelated_socket_failure_does_not_replace_event_loop(monkeypatch):
    import asyncio.selector_events
    import errno
    import socket

    original = asyncio.selector_events.BaseSelectorEventLoop._write_to_self
    def unavailable():
        raise OSError(errno.EMFILE, "no available descriptors")
    monkeypatch.setattr(socket, "socketpair", unavailable)
    assert verification.repair_selector_wakeup() is False
    assert asyncio.selector_events.BaseSelectorEventLoop._write_to_self is original
