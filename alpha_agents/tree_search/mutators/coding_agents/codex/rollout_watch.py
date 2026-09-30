"""Watch a persisted Codex rollout for repeated process-launch failures."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

LAUNCH_FAILURE = (
    "Failed to create unified exec process: No such file or directory (os error 2)"
)


def _seconds(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


class LaunchFailureDetector:
    def __init__(self, threshold: int = 3, window_seconds: float = 60.0):
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.failures: deque[tuple[float, str, int]] = deque()
        self.seen_calls: set[str] = set()
        self.triggered = False

    def feed(self, event: dict, line_number: int) -> dict | None:
        payload = event.get("payload") or {}
        kind = (event.get("type"), payload.get("type"))
        if kind == ("event_msg", "item_completed"):
            item = payload.get("item") or {}
            started = payload.get("started_at_ms")
            if (
                item.get("type") == "CommandExecution"
                and item.get("process_id") is not None
                and self.failures
                and started is not None
                and started / 1000 > self.failures[0][0]
            ):
                self.failures.clear()
            return None
        if kind != ("response_item", "custom_tool_call_output"):
            return None
        output = payload.get("output")
        output_text = output if isinstance(output, str) else json.dumps(output)
        if LAUNCH_FAILURE not in output_text:
            return None
        call_id = payload.get("call_id") or f"line:{line_number}"
        if call_id in self.seen_calls:
            return None
        self.seen_calls.add(call_id)
        when = _seconds(event["timestamp"])
        while self.failures and when - self.failures[0][0] > self.window_seconds:
            self.failures.popleft()
        self.failures.append((when, call_id, line_number))
        if not self.triggered and len(self.failures) >= self.threshold:
            self.triggered = True
            return {
                "timestamp": event["timestamp"],
                "line": line_number,
                "first_failure_line": self.failures[0][2],
                "call_ids": [entry[1] for entry in self.failures],
            }
        return None


class LiveRolloutWatcher:
    """Tail complete JSONL records; stop after one incident or normal completion."""

    def __init__(
        self,
        path: Path,
        on_incident: Callable[[dict], None],
        poll_seconds: float = 0.2,
        start_offset: int = 0,
    ):
        self.path = Path(path)
        self.on_incident = on_incident
        self.poll_seconds = poll_seconds
        self.start_offset = start_offset
        self.detector = LaunchFailureDetector()
        self.stop_event = threading.Event()
        self.incident: dict | None = None
        self.error: str | None = None
        self._thread = threading.Thread(
            target=self._run, name="codex-rollout-watch", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Codex rollout watcher did not stop")

    def _run(self) -> None:
        try:
            with self.path.open("rb") as stream:
                line_number = 0
                remaining = self.start_offset
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("rollout shrank before watcher started")
                    line_number += chunk.count(b"\n")
                    remaining -= len(chunk)
                stream.seek(self.start_offset)
                while not self.stop_event.is_set():
                    position = stream.tell()
                    line = stream.readline()
                    if not line:
                        self.stop_event.wait(self.poll_seconds)
                        continue
                    if not line.endswith(b"\n"):
                        stream.seek(position)
                        self.stop_event.wait(self.poll_seconds)
                        continue
                    line_number += 1
                    incident = self.detector.feed(json.loads(line), line_number)
                    if incident:
                        self.incident = incident
                        self.on_incident(incident)
                        return
        except Exception as exc:
            self.error = repr(exc)
