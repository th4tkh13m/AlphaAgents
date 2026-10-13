"""Trusted pytest entry point for verification inside a network-disabled sandbox.

Some Linux seccomp policies reject sendto even on an anonymous AF_UNIX
socketpair. CPython uses that syscall for selector-loop thread notifications.
Use sendmsg on that same socketpair when this specific restriction is detected;
leave test collection, assertions, task execution and network permissions alone.
This file is executable directly, requiring no read grant to the parent package.
"""

from __future__ import annotations

import asyncio.selector_events
import errno
import logging
import socket
import sys
from pathlib import Path


def repair_selector_wakeup() -> bool:
    """Detect the syscall restriction before installing the equivalent wakeup."""
    if not hasattr(socket.socket, "sendmsg"):
        return False
    try:
        sender, receiver = socket.socketpair()
    except OSError:
        return False
    with sender, receiver:
        try:
            sender.send(b"\0")
            return False
        except OSError as error:
            if error.errno != errno.EPERM:
                return False
        try:
            sender.sendmsg([b"\0"])
            receiver.settimeout(0.1)
            if receiver.recv(1) != b"\0":
                return False
        except OSError:
            return False

    def write_to_self(self):
        channel = self._csock
        if channel is None:
            return
        try:
            channel.sendmsg([b"\0"])
        except OSError:
            # Match CPython's behavior when the loop closes during notification.
            if self._debug:
                logging.getLogger("asyncio").debug(
                    "Fail to write a null byte into the self-pipe socket",
                    exc_info=True,
                )

    asyncio.selector_events.BaseSelectorEventLoop._write_to_self = write_to_self
    return True


def main() -> int:
    # Preserve the working-directory import behavior of Python -m pytest.
    sys.path.insert(0, str(Path.cwd()))
    if repair_selector_wakeup():
        print("Verification runtime: AF_UNIX selector wakeup uses sendmsg (send denied).", file=sys.stderr)
    import pytest

    return int(pytest.main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
