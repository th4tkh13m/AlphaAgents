"""Task-scoped shared device queue with trial fairness and bounded shutdown."""

from __future__ import annotations

import threading
from collections import Counter, deque
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Any, Callable


class DevicesUnavailable(RuntimeError):
    pass


class UnhealthyDevice(RuntimeError):
    """A callback could not confirm bounded cleanup; quarantine this device."""


@dataclass(frozen=True)
class TaskJob:
    id: str
    batch: str
    stage: str
    estimated_seconds: float
    run: Callable[[str], Any]

    def __post_init__(self):
        import math
        if not self.id or not self.batch or self.stage not in {"screen", "s0", "targeted", "selection"}:
            raise ValueError("Task job needs exact identity, batch, and permitted stage")
        if isinstance(self.estimated_seconds, bool) or not math.isfinite(self.estimated_seconds) or self.estimated_seconds < 0:
            raise ValueError("Expected task duration must be finite and nonnegative")

    @property
    def critical(self):
        return self.stage != "selection"


class DeviceScheduler:
    """At most six workers, each owning exactly one configured device.

    All callbacks execute outside the queue lock. Stop admission does not cancel
    already submitted jobs. Callback supervisors must bound real subprocesses;
    the scheduler never assumes a Python thread can terminate device work.
    """

    def __init__(self, devices: tuple[str, ...], *, autostart=True):
        if not devices or len(devices) > 6 or len(set(devices)) != len(devices):
            raise ValueError("Configure one to six distinct devices")
        self.devices = devices
        self.condition = threading.Condition()
        self.pending: list[tuple[TaskJob, Future]] = []
        self.running = Counter()
        self.round_robin = deque()
        self.quarantined: dict[str, str] = {}
        self.accepting = True
        self.started = False
        self.workers = []
        self.ids = set()
        self.history = []
        if autostart:
            self.start()

    def start(self):
        with self.condition:
            if self.started:
                return
            self.started = True
            self.workers = [threading.Thread(target=self._worker, args=(device,),
                                             name=f"device-{device}", daemon=True) for device in self.devices]
            for worker in self.workers:
                worker.start()

    def submit(self, job: TaskJob) -> Future:
        with self.condition:
            if not self.accepting:
                raise RuntimeError("Device scheduler admission has closed")
            if job.id in self.ids:
                raise ValueError("Duplicate scheduled job identity")
            if len(self.quarantined) == len(self.devices):
                raise DevicesUnavailable("All configured devices are quarantined")
            self.ids.add(job.id)
            future = Future()
            self.pending.append((job, future))
            if job.batch not in self.round_robin:
                self.round_robin.append(job.batch)
            self.condition.notify_all()
            return future

    def _pick(self):
        self.pending = [(job, future) for job, future in self.pending if not future.cancelled()]
        if not self.pending:
            return None
        critical = [(job, future) for job, future in self.pending if job.critical]
        candidates = critical or self.pending
        queued_batches = {job.batch for job, _ in candidates}
        # Borrowing is allowed only when the competing batch has no queued work.
        cap = 3 if critical and len(queued_batches) >= 2 else len(self.devices)
        eligible = {job.batch for job, _ in candidates if self.running[job.batch] < cap}
        if not eligible:
            return None
        for _ in range(len(self.round_robin)):
            batch = self.round_robin[0]
            self.round_robin.rotate(-1)
            if batch in eligible:
                break
        else:
            raise AssertionError("Eligible batch missing from round-robin queue")
        selected = min((pair for pair in candidates if pair[0].batch == batch),
                       key=lambda pair: (-pair[0].estimated_seconds, pair[0].id))
        self.pending.remove(selected)
        self.running[batch] += 1
        return selected

    def promote_dependency(self, id, batch, stage):
        """A queued promotion needed by search inherits critical priority."""
        if stage == "selection":
            return
        with self.condition:
            for index, (job, future) in enumerate(self.pending):
                if job.id == id and not job.critical:
                    self.pending[index] = (replace(job, batch=batch, stage=stage), future)
                    if batch not in self.round_robin:
                        self.round_robin.append(batch)
                    self.condition.notify_all()
                    return

    def _worker(self, device):
        while True:
            with self.condition:
                while True:
                    if device in self.quarantined:
                        return
                    selected = self._pick()
                    if selected:
                        break
                    if not self.accepting and not self.pending:
                        return
                    self.condition.wait()
                job, future = selected
                active = dict(self.running)
                self.history.append({"event": "start", "job": job.id, "batch": job.batch,
                                     "device": device, "running": active})
            error = None
            result = None
            execute = future.set_running_or_notify_cancel()
            try:
                if execute:
                    result = job.run(device)
            except BaseException as failure:
                error = failure
            finally:
                stranded = []
                with self.condition:
                    if isinstance(error, UnhealthyDevice):
                        self.quarantined[device] = str(error)
                    self.running[job.batch] -= 1
                    self.history.append({"event": "release", "job": job.id, "batch": job.batch, "device": device})
                    if len(self.quarantined) == len(self.devices):
                        stranded = [queued for _, queued in self.pending if not queued.done()]
                        self.pending.clear()
                    self.condition.notify_all()
                # Settle scheduler ownership before waking callers. Future
                # callbacks can acquire evaluator/database locks, so never
                # execute them while holding the scheduler condition.
                if execute:
                    if error is None:
                        future.set_result(result)
                    else:
                        future.set_exception(error)
                for queued in stranded:
                    queued.set_exception(DevicesUnavailable("All devices quarantined during admitted work"))

    def close(self, *, wait=True):
        """Drain all admitted jobs; no hidden cancellation or spending cutoff."""
        with self.condition:
            self.accepting = False
            self.condition.notify_all()
        if wait:
            if not self.started:
                self.start()
            for worker in self.workers:
                worker.join()

    def status(self):
        with self.condition:
            return {"devices": list(self.devices), "quarantined": dict(self.quarantined),
                    "queued": len(self.pending), "running": dict(self.running), "accepting": self.accepting}
