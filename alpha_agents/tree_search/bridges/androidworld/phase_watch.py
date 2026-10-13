"""Trusted runner lifecycle markers for opt-in singleton phase supervision."""

import asyncio
import json
import os
import time
from pathlib import Path

# Publishing the phase transition consumes the existing task envelope.
TASK_HANDOFF_SECONDS = 1.0


async def run_task_bounded(awaitable, timeout, begin_cleanup=None):
    """On timeout, publish cleanup before awaiting agent cancellation.

    Supervised execution reserves one second inside the hard task cap for the
    phase handoff. Video/trace finalization and cancellation use the existing
    cleanup allowance. The subprocess supervisor kills a stuck cancellation.
    Unsupervised execution retains its original timeout allowance.
    """
    reserve = TASK_HANDOFF_SECONDS if begin_cleanup is not None else 0
    if timeout <= reserve:
        raise ValueError("Task timeout must exceed its phase-handoff allowance")
    task = asyncio.ensure_future(awaitable)
    try:
        completed, _ = await asyncio.wait({task}, timeout=timeout - reserve)
        if completed:
            return await task
        if begin_cleanup is not None:
            begin_cleanup()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            raise TimeoutError("Task execution deadline exceeded during cancellation") from error
        # Suppressing cancellation cannot turn a timeout into benchmark success.
        raise TimeoutError("Task execution deadline exceeded")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def enter_phase(path, phase):
    if path is None:
        return
    path = Path(path)
    events = json.loads(path.read_text())["events"] if path.exists() else []
    if phase != ["startup", "task", "cleanup", "finished"][len(events)]:
        raise ValueError("Singleton runner phase order changed")
    events.append({"phase": phase, "entered": time.monotonic()})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        json.dump({"events": events}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
