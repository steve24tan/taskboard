"""HTTP server + collector loop. Standard library only."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import periods
from .collectors import COLLECTORS
from .store import STATUSES, Store, resolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
HISTORY = {"daily": 14, "weekly": 8, "monthly": 6}


class Board:
    def __init__(self, cfg: dict, db_path: str):
        self.cfg = cfg
        self.store = Store(db_path)
        self.tz = float(cfg.get("tz_hours", 8))
        self.tasks = {}
        for g in cfg.get("groups", []):
            for t in g.get("tasks", []):
                t.setdefault("cadence", "daily")
                t.setdefault("reset_hour", g.get("reset_hour", cfg.get("reset_hour", 4)))
                if t["cadence"] not in periods.CADENCES:
                    raise ValueError("task %s: bad cadence %r" % (t["id"], t["cadence"]))
                self.tasks[t["id"]] = t
        self.health = {}
        self._scan_lock = threading.Lock()

    # ---------------------------------------------------------------- collect
    def scan(self) -> int:
        added = 0
        with self._scan_lock:
            for c in self.cfg.get("collectors", []):
                name = c.get("name") or c["type"]
                if not c.get("enabled", True):
                    continue
                try:
                    events, health = COLLECTORS[c["type"]](c)
                    unknown = sorted({e["task"] for e in events} - set(self.tasks))
                    for e in events:
                        if e["task"] in self.tasks:
                            added += self.store.add(e["task"], e["ts"], e["status"], e["detail"],
                                                    c["type"], e["key"])
                    if unknown:
                        health["message"] += "；config 里没有的任务: " + ", ".join(unknown)
                except Exception as ex:  # one broken collector must not stop the others
                    health = {"ok": False, "message": "%s: %s" % (type(ex).__name__, ex)}
                    traceback.print_exc()
                health["last_scan"] = time.time()
                self.health[name] = health
        return added

    def loop(self):
        every = max(30, int(self.cfg.get("scan_seconds", 300)))
        while True:
            self.scan()
            time.sleep(every)

    # ----------------------------------------------------------------- status
    def task_status(self, t: dict, now: float) -> dict:
        cad, rh = t["cadence"], int(t["reset_hour"])
        hist = []
        for start, end in periods.recent_periods(now, cad, HISTORY[cad], rh, self.tz):
            r = resolve(self.store.events(t["id"], start, end))
            ev = r["event"]
            hist.append({"label": periods.label(start, cad, self.tz), "start": start, "end": end,
                         "state": r["state"], "manual": r["manual"],
                         "ts": ev["ts"] if ev else None, "detail": ev["detail"] if ev else ""})
        cur = hist[-1]
        return {"id": t["id"], "name": t["name"], "cadence": cad, "reset_hour": rh,
                "current": cur, "next_reset": cur["end"], "history": hist}

    def status(self) -> dict:
        now = time.time()
        groups = []
        for g in self.cfg.get("groups", []):
            groups.append({"name": g["name"],
                           "tasks": [self.task_status(self.tasks[t["id"]], now) for t in g.get("tasks", [])]})
        names = {k: v["name"] for k, v in self.tasks.items()}
        recent = [dict(e, task_name=names.get(e["task"], e["task"])) for e in self.store.recent(30)]
        return {"now": now, "tz_hours": self.tz, "groups": groups, "health": self.health, "recent": recent}


def make_handler(board: Board):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 * 1024:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _authorized(self):
            token = board.cfg.get("token") or ""
            return not token or self.headers.get("X-Board-Token") == token

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._send(200, board.status())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            # The token guards the script-facing endpoint only; the dashboard's own buttons
            # (tick / rescan) stay usable from any browser on the LAN.
            if path == "/api/event" and not self._authorized():
                return self._send(401, {"error": "bad token"})
            try:
                body = self._json_body()
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            if path == "/api/scan":
                return self._send(200, {"added": board.scan()})
            task = body.get("task")
            if task not in board.tasks:
                return self._send(400, {"error": "unknown task %r" % task})
            if path == "/api/tick":
                board.store.add_manual(task, bool(body.get("done", True)))
                return self._send(200, {"ok": True})
            if path == "/api/event":
                # Generic entry point for anything else that wants to report progress
                # (a script, a study session, ...). `key` makes a retried POST idempotent.
                status = body.get("status", "done")
                if status not in STATUSES:
                    return self._send(400, {"error": "status must be one of %s" % ", ".join(STATUSES)})
                ts = float(body.get("ts") or time.time())
                key = "api:" + str(body.get("key") or "%s:%r" % (task, ts))
                added = board.store.add(task, ts, status, str(body.get("detail", ""))[:200], "api", key)
                return self._send(200, {"ok": True, "added": added})
            self._send(404, {"error": "not found"})

    return H


def serve(cfg: dict, db_path: str):
    board = Board(cfg, db_path)
    threading.Thread(target=board.loop, daemon=True).start()
    host, port = cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 8765))
    httpd = ThreadingHTTPServer((host, port), make_handler(board))
    print("TaskBoard on http://%s:%d  (Ctrl+C to stop)" % ("localhost" if host == "0.0.0.0" else host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
