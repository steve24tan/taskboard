"""SQLite event store.

One table of events. A task's state for a period is derived, never stored:
  - the newest manual event in the period wins (tick / untick from the dashboard);
  - otherwise the best automatic event: done > partial > fail.
Every event carries a unique `key`, so collectors can rescan the same logs and insert
the same lines again without creating duplicates.
"""
from __future__ import annotations

import sqlite3
import threading
import time

STATUSES = ("done", "partial", "fail", "undone")
RANK = {"done": 3, "partial": 2, "fail": 1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task    TEXT NOT NULL,
    ts      REAL NOT NULL,
    status  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL,
    key     TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_task_ts ON events(task, ts);
"""


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def add(self, task: str, ts: float, status: str, detail: str, source: str, key: str) -> bool:
        if status not in STATUSES:
            raise ValueError("bad status %r" % status)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO events(task, ts, status, detail, source, key) VALUES (?,?,?,?,?,?)",
                (task, float(ts), status, detail or "", source, key))
            self._db.commit()
            return cur.rowcount == 1

    def add_manual(self, task: str, done: bool, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        self.add(task, ts, "done" if done else "undone", "手动", "manual", "manual:%s:%r" % (task, ts))

    def events(self, task: str, start: float, end: float):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM events WHERE task=? AND ts>=? AND ts<? ORDER BY ts", (task, start, end))]

    def recent(self, limit: int = 50):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]


def resolve(events: list[dict], target: int = 1, latest: bool = False) -> dict:
    """State of one task in one period from its events (sorted by ts).

    target  >1: the task needs that many automatic "done" events in the period; fewer is
            "partial" and the result carries progress "k/N".
    latest  the newest automatic event decides instead of the best one (for snapshot
            collectors whose later reading replaces the earlier one).
    """
    manual = [e for e in events if e["source"] == "manual"]
    if manual:
        m = manual[-1]
        return {"state": "done" if m["status"] == "done" else "pending", "event": m, "manual": True,
                "progress": None}
    auto = [e for e in events if e["status"] in RANK]
    progress = None
    if target > 1:
        done = [e for e in auto if e["status"] == "done"]
        progress = "%d/%d" % (len(done), target)
        if done:
            return {"state": "done" if len(done) >= target else "partial", "event": done[-1],
                    "manual": False, "progress": progress}
    if latest:
        best = auto[-1] if auto else None
    else:
        best = None
        for e in auto:
            r = RANK[e["status"]]
            if best is None or r > RANK[best["status"]] or (r == RANK[best["status"]] and e["ts"] > best["ts"]):
                best = e
    if best is None:
        return {"state": "pending", "event": None, "manual": False, "progress": progress}
    return {"state": best["status"], "event": best, "manual": False, "progress": progress}
