"""Task-scoped shared device queue with trial fairness and bounded shutdown."""

from __future__ import annotations

import threading
from collections import Counter, deque
from concurrent.futures import Future
from contextlib import contextmanager
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
    testing_owner: str | None = None

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
    """At most nine workers, each owning exactly one configured device.

    All callbacks execute outside the queue lock. Stop admission does not cancel
    already submitted jobs. Callback supervisors must bound real subprocesses;
    the scheduler never assumes a Python thread can terminate device work.
    """

    def __init__(self, devices: tuple[str, ...], *, autostart=True, implementor_limit=9, recover_device=None):
        if not devices or len(devices) > 9 or len(set(devices)) != len(devices):
            raise ValueError("Configure one to nine distinct devices")
        self.devices = devices
        self.condition = threading.Condition()
        self.pending: list[tuple[TaskJob, Future]] = []
        self.running = Counter()
        self.round_robin = deque()
        self.quarantined: dict[str, str] = {}
        self.recover_device = recover_device
        self.recovering: set[str] = set()
        self.accepting = True
        self.started = False
        self.workers = []
        self.ids = set()
        self.history = []
        if isinstance(implementor_limit, bool) or not isinstance(implementor_limit, int) or implementor_limit < 1:
            raise ValueError("Implementor limit must be a positive integer")
        self.implementor_limit = min(implementor_limit, len(devices))
        self.testing: dict[str, str | None] = {}
        self.waiting_implementors = deque()
        self.active_devices = {}
        if autostart:
            self.start()

    def _assign_testing(self):
        """Rebuild affinity after quarantine; borrowing never changes ownership."""
        assigned = set(self.testing.values())
        for owner, device in self.testing.items():
            if device is None or device in self.quarantined:
                available = [d for d in self.devices if d not in self.quarantined and d not in assigned]
                # After pool degradation existing implementors can share a
                # healthy physical lane serially; never strand one on None.
                if not available:
                    available = sorted((d for d in self.devices if d not in self.quarantined),
                                       key=lambda d: (list(self.testing.values()).count(d), d))
                if available:
                    self.testing[owner] = available[0]
                    assigned.add(available[0])
                else:
                    self.testing[owner] = None
        for owner, device in self.testing.items():
            active = self.active_devices.get(device)
            waiting = any(j.testing_owner == owner and not f.cancelled() for j, f in self.pending)
            if waiting and active is not None and active.testing_owner != owner:
                available = [d for d in self.devices if d not in self.quarantined
                             and d not in self.testing.values() and d not in self.active_devices]
                if available:
                    # A loan must not force a check to wait while another
                    # device is idle. Move only the logical reservation.
                    self.testing[owner] = available[0]

    @contextmanager
    def implementation(self, owner):
        """Admit a Codex implementor and give it one reclaimable testing lane."""
        with self.condition:
            if owner in self.testing or owner in self.waiting_implementors:
                raise ValueError("Duplicate implementation reservation")
            self.waiting_implementors.append(owner)
            try:
                while True:
                    if not self.accepting:
                        raise RuntimeError("Device scheduler admission has closed")
                    healthy = len(self.devices) - len(self.quarantined)
                    if not healthy and not self.recovering:
                        raise DevicesUnavailable("All configured devices are quarantined")
                    if (healthy and self.waiting_implementors[0] == owner and
                            len(self.testing) < min(self.implementor_limit, healthy)):
                        self.testing[owner] = None
                        self._assign_testing()
                        self.history.append({"event": "implementation_admit", "owner": owner,
                                             "device": self.testing[owner]})
                        break
                    self.condition.wait()
            finally:
                self.waiting_implementors.remove(owner)
                self.condition.notify_all()
        try:
            yield {"owner": owner, "device": self.testing[owner], "reclaim": "next-task-boundary"}
        finally:
            with self.condition:
                # The caller must drain checks before releasing invocation scope.
                while any(j.testing_owner == owner and not f.cancelled() for j, f in self.pending) or any(
                        j.testing_owner == owner for j in self.active_devices.values()):
                    self.condition.wait()
                self.testing.pop(owner, None)
                self._assign_testing()
                self.history.append({"event": "implementation_release", "owner": owner})
                self.condition.notify_all()

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
            if len(self.quarantined) == len(self.devices) and not self.recovering:
                raise DevicesUnavailable("All configured devices are quarantined")
            if job.testing_owner is not None and job.testing_owner not in self.testing:
                raise ValueError("Targeted job has no active implementation reservation")
            self.ids.add(job.id)
            future = Future()
            self.pending.append((job, future))
            if job.batch not in self.round_robin:
                self.round_robin.append(job.batch)
            self.condition.notify_all()
            return future

    def _pick(self, device):
        self.pending = [(job, future) for job, future in self.pending if not future.cancelled()]
        self._assign_testing()
        compatible = [(j, f) for j, f in self.pending if j.testing_owner is None or
                      self.testing.get(j.testing_owner) == device]
        if not compatible:
            return None
        checks = [(j, f) for j, f in compatible if j.testing_owner is not None]
        critical = [(job, future) for job, future in compatible if job.critical]
        candidates = checks or critical or compatible
        eligible = {job.batch for job, _ in candidates}
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
                    selected = self._pick(device)
                    if selected:
                        break
                    if not self.accepting and not self.pending:
                        return
                    self.condition.wait()
                job, future = selected
                self.active_devices[device] = job
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
                        if self.recover_device is not None:
                            self.recovering.add(device)
                    self.active_devices.pop(device, None)
                    self._assign_testing()
                    self.running[job.batch] -= 1
                    self.history.append({"event": "release", "job": job.id, "batch": job.batch, "device": device})
                    if len(self.quarantined) == len(self.devices) and not self.recovering:
                        stranded = [queued for _, queued in self.pending if not queued.done()]
                        self.pending.clear()
                    self.condition.notify_all()
                if device in self.recovering:
                    # No task owns this device now. Recovery runs outside the
                    # scheduler lock and is bounded by its subprocess supervisor.
                    restored = False
                    try:
                        restored = self.recover_device(device, str(error)) is True
                    except Exception:
                        pass  # Keep quarantine; failure never becomes a reward.
                    with self.condition:
                        self.recovering.discard(device)
                        if restored:
                            self.quarantined.pop(device, None)
                        self.history.append({"event": "device_recovery", "device": device, "healthy": restored})
                        self._assign_testing()
                        if len(self.quarantined) == len(self.devices) and not self.recovering:
                            stranded.extend(queued for _, queued in self.pending if not queued.done())
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
                    "recovering": sorted(self.recovering),
                    "active_device_count": len(self.active_devices),
                    "idle_healthy_device_count": len(self.devices) - len(self.quarantined) - len(self.active_devices),
                    "queued_by_stage": dict(Counter(j.stage for j, f in self.pending if not f.cancelled())),
                    "queued": len(self.pending), "running": dict(self.running), "accepting": self.accepting,
                    "active_devices": {d: {"job": j.id, "stage": j.stage, "testing_owner": j.testing_owner}
                                       for d, j in self.active_devices.items()},
                    "testing_reservations": dict(self.testing),
                    "waiting_implementors": list(self.waiting_implementors),
                    "implementor_limit": self.implementor_limit}
