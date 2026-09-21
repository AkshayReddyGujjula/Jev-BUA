"""Append-only JSONL evidence for one run.

Every decision records what was offered, what was answered, the confidence, which model
answered, and whether a cache served it; every execution is recorded BEFORE its result is
observed, so a stale post-action read cannot erase the fact that something ran. That is
the same rule the loop follows for its own history, applied to the audit trail.

Evidence must never break a run: all write failures are swallowed, and `record` is a
no-op once the writer failed to open.
"""

import json
import time
from pathlib import Path


class Evidence:
    def __init__(self, directory, run_id, **meta):
        self.path = None
        self._file = None
        self._started = time.perf_counter()
        self._run_id = run_id
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / f"{run_id}.jsonl"
            self._file = self.path.open("a", encoding="utf-8")
            self.record("run_start", **meta)
        except OSError:
            self._file = None

    def record(self, kind, **fields):
        if self._file is None:
            return
        try:
            self._file.write(json.dumps({
                "kind": kind,
                "run_id": self._run_id,
                "at_ms": round((time.perf_counter() - self._started) * 1000),
                **fields,
            }, default=str) + "\n")
            self._file.flush()
        except (OSError, TypeError, ValueError):
            pass

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
