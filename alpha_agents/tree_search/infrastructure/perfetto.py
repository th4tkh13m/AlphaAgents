"""Automatic, read-only Perfetto snapshots for the production MCTS application.

Uses Chrome trace JSON and the standard library; importing this module never
starts a thread. SearchApplication owns the watcher, including its final flush.
"""

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from .trace_export import atomic, process_stats, read_json, sample_host, snapshot

LOG = logging.getLogger(__name__)


class TraceWatcher:
    """One exporter per run; failures are observable and never abort search.

    Resume appends resource observations and retains immutable minute snapshots.
    The exporter uses its own short-lived read-only SQLite connection per tick.
    It does not hold a search transaction or change the experiment identity.
    """

    def __init__(self, directory, *, devices=(), interval=5):
        if interval < 1:
            raise ValueError("Trace interval must be at least one second")
        self.directory = Path(directory).resolve()
        self.output = self.directory / "profiling"
        self.devices = tuple(devices)
        self.interval = interval
        self.started = time.time()
        self.stop = threading.Event()
        self.thread = None
        self.lock = None
        self.samples = []
        self.previous = {}
        self.last_archive = 0
        self.last_success = None
        self.owner = None

    def _acquire(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.output / "watcher.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise
        sample_path = self.output / "resource_samples.jsonl"
        if sample_path.exists():
            with sample_path.open() as stream:
                for line in stream:
                    try:
                        self.samples.append(json.loads(line))
                    except ValueError:
                        continue  # An interrupted final append is not a sample.
            if self.samples:
                self.previous = self.samples[-1]["devices"]
            # Preserve a separator if a killed writer left a partial final line.
            if sample_path.stat().st_size:
                with sample_path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    terminated = stream.read(1) == b"\n"
                if not terminated:
                    with sample_path.open("a") as stream:
                        stream.write("\n")

    def _release(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def export(self, *, status="watching"):
        """Publish a complete atomic snapshot. Caller must own watcher.lock."""
        begun = time.monotonic()
        now = time.time()
        sample = sample_host(self.directory, now, self.previous)
        self.previous = sample["devices"]
        self.samples.append(sample)
        with (self.output / "resource_samples.jsonl").open("a") as stream:
            stream.write(json.dumps(sample, separators=(",", ":")) + "\n")
        trace = snapshot(self.directory, now, self.samples, self.devices, self.started)
        payload = json.dumps(trace, separators=(",", ":"), allow_nan=False).encode()
        atomic(self.output / "live_trace.json", payload)
        compressed = gzip.compress(payload, mtime=0)
        atomic(self.output / "live_trace.json.gz", compressed)
        if now - self.last_archive >= 60:
            history = self.output / "snapshots"
            history.mkdir(exist_ok=True)
            stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime(
                "%Y%m%dT%H%M%S.%fZ"
            )
            atomic(history / (stamp + ".json.gz"), compressed)
            self.last_archive = now
        self.last_success = trace["otherData"]["observed_at_utc"]
        self._status(
            status,
            events=len(trace["traceEvents"]),
            samples=len(self.samples),
            trace_bytes=len(payload),
            compressed_bytes=len(compressed),
            export_seconds=time.monotonic() - begun,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def _status(self, status, **fields):
        record = {
            "pid": os.getpid(),
            "interval_seconds": self.interval,
            "status": status,
            "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "last_success_at_utc": self.last_success,
            **fields,
        }
        atomic(
            self.output / "watcher_status.json",
            (json.dumps(record, indent=2) + "\n").encode(),
        )

    def _safe_export(self, *, status="watching"):
        try:
            self.export(status=status)
            return True
        except Exception as error:
            LOG.warning("Profiling snapshot failed: %s", error)
            try:
                with (self.output / "watcher_errors.log").open("a") as stream:
                    stream.write(
                        f"{dt.datetime.now(dt.timezone.utc).isoformat()} {type(error).__name__}: {error}\n"
                    )
                self._status("error", error_type=type(error).__name__)
            except OSError:
                LOG.exception("Cannot publish profiling failure status")
            return False

    def _loop(self):
        try:
            while not self.stop.is_set():
                if self.owner is not None and process_stats(self.owner) is None:
                    self.stop.set()
                    break
                begun = time.monotonic()
                self._safe_export()
                self.stop.wait(max(0.1, self.interval - (time.monotonic() - begun)))
            self._safe_export(status="stopped")
        finally:
            self._release()

    def start(self):
        if self.thread is not None:
            return self.thread.is_alive()
        try:
            self._acquire()
            self.thread = threading.Thread(
                target=self._loop, name="perfetto-watcher", daemon=True
            )
            self.thread.start()
            return True
        except Exception:
            self._release()
            LOG.warning(
                "Cannot start trace watcher (another watcher may own this run)",
                exc_info=True,
            )
            return False

    def close(self):
        self.stop.set()
        if self.thread is not None:
            # Queries have a two-second busy timeout; finish the current export
            # and a final snapshot before the application closes its state store.
            self.thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument(
        "--owner-record",
        type=Path,
        help="Attach to this live PID/birth record and stop when it exits",
    )
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("Interval must be at least one second")
    watcher = TraceWatcher(args.run, interval=args.interval)
    if args.owner_record:
        watcher.owner = read_json(args.owner_record, {})
        if process_stats(watcher.owner) is None:
            parser.error("Owner record must identify a live process")
    if not args.watch:
        try:
            watcher._acquire()
            watcher.export(status="snapshot")
        finally:
            watcher._release()
        return
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: watcher.stop.set())
    if not watcher.start():
        raise SystemExit("Could not acquire trace watcher ownership")
    try:
        while not watcher.stop.wait(1):
            pass
    finally:
        watcher.close()


if __name__ == "__main__":
    main()
