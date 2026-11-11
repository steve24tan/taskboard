"""Collectors turn the tools' own files into events. They only read; they never start,
stop or configure the tools.

Each collector returns (events, health):
  events  list of dicts {task, ts, status, detail, key}
  health  {"ok": bool, "message": str}

Log line times are the local time of the machine that wrote them (the same machine this
board runs on), so they are converted with time.mktime.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone


def _local_ts(day: str, hms: str) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    hh, mm, ss = (int(x) for x in hms.split(":"))
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


def _recent_logs(log_dir: str, days: int, pattern: str = r"(\d{4})-(\d{2})-(\d{2})") -> list[tuple[str, str]]:
    """[(path, 'YYYY-MM-DD'), ...] for date-named logs of the last `days` days, oldest first.
    `pattern` matches the start of the file name, with groups (year, month, day)."""
    oldest = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for p in glob.glob(os.path.join(log_dir, "*.log")):
        m = re.match(pattern, os.path.basename(p))
        if m:
            day = "-".join(m.groups()[:3])
            if day >= oldest:
                out.append((p, day))
    return sorted(out, key=lambda x: x[1])


def _read_lines(path: str) -> list[str]:
    # utf-8-sig: PowerShell puts a BOM in front of the first line.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read().splitlines()


# --------------------------------------------------------------------------- MAA

_MAA_LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")


def collect_maa_wrapper(cfg: dict):
    """maa-tools/run_maa.py logs: logs/YYYY-MM-DD.log, lines "[HH:MM:SS] message".

    A run starts with a "=====" line. "跑 MAA: <command>" names the profile, mapped to a task
    by cfg["profiles"] (substring of the command -> task id). "OK 完成 - ..." is a done event,
    "X ..." lines are failures (a later successful retry in the same period still wins).
    """
    log_dir = cfg["log_dir"]
    if not os.path.isdir(log_dir):
        return [], {"ok": False, "message": "找不到日志目录 " + log_dir}
    profiles = sorted(cfg.get("profiles", {}).items(), key=lambda kv: -len(kv[0]))
    default = cfg.get("default_task")
    events = []
    files = _recent_logs(log_dir, int(cfg.get("days", 14)))
    for path, day in files:
        task = default
        for n, line in enumerate(_read_lines(path), 1):
            m = _MAA_LINE.match(line)
            if not m:
                continue
            hms, msg = m.groups()
            if msg.startswith("====="):
                task = default
                continue
            if msg.startswith("跑 MAA:"):
                cmd = msg[len("跑 MAA:"):].strip()
                task = next((t for sub, t in profiles if sub in cmd), default)
                continue
            if not task:
                continue
            key = "maa:%s:%d" % (os.path.basename(path), n)
            ts = _local_ts(day, hms)
            if msg.startswith("OK 完成"):
                events.append({"task": task, "ts": ts, "status": "done",
                               "detail": msg[len("OK 完成"):].lstrip(" -"), "key": key})
            elif msg.startswith("X ") and "跳过" not in msg:
                events.append({"task": task, "ts": ts, "status": "fail",
                               "detail": msg[2:].strip(), "key": key})
    note = "读了 %d 个日志" % len(files) if files else "日志目录里还没有日志"
    return events, {"ok": True, "message": note}


# --------------------------------------------------------------------- March7th

# Run markers below follow sheinlee/daily-hub (MIT), adapters/march7th.py, which checked them
# against March7thAssistant v2026.7.26.
M7A_START = re.compile(r"开始运行")
M7A_DONE = re.compile(r"停止运行")
# Only the final error ends the run; start-up attempts ("尝试启动游戏时发生错误") are retried.
M7A_FAIL = re.compile(r"(?<!尝试启动游戏时)发生错误|首次使用请先打开图形界面")
# The run finished but part of the daily did not get done.
M7A_PARTIAL = re.compile(r"清体力未完成|每日实训未完成|任务无法完成|遗器背包已满|战斗失败|战斗超时")

_M7A_TIME = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{2}:\d{2}:\d{2})")
_M7A_MSG = re.compile(r"\|\s*[A-Z]+\s*\|\s*(.*)$|\[[A-Z]+\]\s*(.*)$")
_YAML_TS = re.compile(r"^(\w+_timestamp):\s*([\d.]+)", re.M)


def _m7a_msg(line: str) -> str:
    m = _M7A_MSG.search(line)
    return (m.group(1) or m.group(2) or "").strip() if m else line.strip()


def collect_march7th(cfg: dict):
    """March7thAssistant: logs/YYYY-MM-DD.log for the daily run, config.yaml *_timestamp keys
    (written by M7A when a task completes) for weeklies such as 历战余响 / 差分宇宙."""
    tool_dir = cfg["tool_dir"]
    if not os.path.isdir(tool_dir):
        return [], {"ok": False, "message": "找不到三月七目录 " + tool_dir}
    events, notes = [], []

    daily = cfg.get("daily_task")
    files = _recent_logs(os.path.join(tool_dir, "logs"), int(cfg.get("days", 14))) if daily else []
    for path, day in files:
        last_ts = _local_ts(day, "00:00:00")
        run = None
        for n, line in enumerate(_read_lines(path), 1):
            m = _M7A_TIME.match(line)
            if m:
                last_ts = _local_ts(m.group(1) or day, m.group(2))
            msg = _m7a_msg(line)
            key = "m7a:%s:%d" % (os.path.basename(path), n)
            if M7A_START.search(msg):
                run = {"failed": False, "partial": []}
            elif M7A_FAIL.search(msg):
                events.append({"task": daily, "ts": last_ts, "status": "fail", "detail": msg[:80], "key": key})
                run = {"failed": True, "partial": []}
            elif M7A_PARTIAL.search(msg):
                run = run or {"failed": False, "partial": []}
                hit = M7A_PARTIAL.search(msg).group(0)
                if hit not in run["partial"]:
                    run["partial"].append(hit)
            elif M7A_DONE.search(msg):
                run = run or {"failed": False, "partial": []}
                if not run["failed"]:
                    status = "partial" if run["partial"] else "done"
                    events.append({"task": daily, "ts": last_ts, "status": status,
                                   "detail": "、".join(run["partial"]) or "完成", "key": key})
                run = None
    if daily:
        notes.append("读了 %d 个日志" % len(files) if files else "logs 里还没有日志")

    stamps = cfg.get("timestamps", {})
    if stamps:
        try:
            with open(os.path.join(tool_dir, "config.yaml"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
            notes.append("读不到 config.yaml")
        found = {k: float(v) for k, v in _YAML_TS.findall(text)}
        for k, task in stamps.items():
            ts = found.get(k, 0.0)
            if ts > 0:
                events.append({"task": task, "ts": ts, "status": "done", "detail": "config.yaml " + k,
                               "key": "m7a:%s:%r" % (k, ts)})
        missing = [k for k in stamps if k not in found]
        if text and missing:
            notes.append("config.yaml 里没有 " + ", ".join(missing))
    return events, {"ok": True, "message": "；".join(notes)}


# ------------------------------------------------------------ MAA autorun (PS1)

_AUTORUN_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] (\S+) : (.*)$")
_AUTORUN_RULES = [  # (pattern, status, detail); detail None = the message itself
    (re.compile(r"all daily tasks completed"), "done", "完成"),
    (re.compile(r"task chain finished, but these failed: (.*?)\s*\[WARN\]"), "partial", "没做完: %s"),
    (re.compile(r"did not finish before timeout"), "fail", "超时"),
    (re.compile(r"treating as failed"), "fail", "MAA 不见了"),
    (re.compile(r"NOT stamped done"), "fail", "没完成，交给 supervisor"),
    (re.compile(r"rerun stopped by lead"), "fail", None),
]


def collect_maa_autorun(cfg: dict):
    """run_dailies.ps1 logs: autorun_YYYYMMDD.log, lines "[YYYY-MM-DD HH:MM:SS] <account> : message".

    cfg["accounts"] maps the account prefix (TW, Global, ...) to a task id. A retry that ends
    in "all daily tasks completed" wins over the earlier failures of the same game day.
    """
    log_dir = cfg["log_dir"]
    if not os.path.isdir(log_dir):
        return [], {"ok": False, "message": "找不到日志目录 " + log_dir}
    accounts = cfg.get("accounts", {})
    events = []
    files = _recent_logs(log_dir, int(cfg.get("days", 14)), r"autorun_(\d{4})(\d{2})(\d{2})")
    for path, _ in files:
        for n, line in enumerate(_read_lines(path), 1):
            m = _AUTORUN_LINE.match(line)
            if not m or m.group(3) not in accounts:
                continue
            day, hms, acc, msg = m.groups()
            for rx, status, detail in _AUTORUN_RULES:
                hit = rx.search(msg)
                if not hit:
                    continue
                if detail is None:
                    detail = msg[:80]
                elif "%s" in detail:
                    detail = detail % hit.group(1)
                events.append({"task": accounts[acc], "ts": _local_ts(day, hms), "status": status,
                               "detail": detail, "key": "autorun:%s:%d" % (os.path.basename(path), n)})
                break
    note = "读了 %d 个日志" % len(files) if files else "日志目录里还没有日志"
    return events, {"ok": True, "message": note}


# ------------------------------------------------------------------ CSV sheets

def _parse_when(text: str):
    """'2026-11-12', '2026/11/12', '2026-11-12 18:30[:00]', '2026-11-12T18:30' -> local unix time.
    A bare date counts as 12:00 so it lands in that calendar day's period whatever the reset hour."""
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?",
                 (text or "").strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    return time.mktime((int(y), int(mo), int(d), int(hh or 12), int(mm or 0), int(ss or 0), 0, 0, -1))


def _read_csv(cfg: dict, needed: list):
    """(rows, error). error is a message when the file or one of the `needed` columns is missing."""
    path = cfg["path"]
    if not os.path.isfile(path):
        return None, "找不到 " + path
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = reader.fieldnames or []
    missing = [c for c in needed if c and c not in cols]
    if missing:
        return None, "表里没有这几栏: " + ", ".join(missing)
    return rows, None


def _cell(row: dict, col) -> str:
    return (row.get(col) or "").strip() if col else ""


def _row_id(row: dict, cfg: dict, line_no: int) -> str:
    return _cell(row, cfg.get("key_column")) or "第%d行" % line_no


def collect_csv_dates(cfg: dict):
    """Every row of a CSV whose `date_column` holds a date is one "done" event for `task`
    (one row per published episode -> "posted this week"). Rows without a date are skipped."""
    shown = list(cfg.get("detail_columns", []))
    rows, err = _read_csv(cfg, [cfg["date_column"], cfg.get("key_column")] + shown)
    if err:
        return [], {"ok": False, "message": err}
    oldest = time.time() - int(cfg.get("days", 90)) * 86400
    events = []
    for n, row in enumerate(rows, 2):   # line 1 is the header
        ts = _parse_when(row.get(cfg["date_column"]))
        if ts is None or ts < oldest:
            continue
        rid = _row_id(row, cfg, n)
        detail = " ".join(_cell(row, c) for c in shown).strip() or rid
        events.append({"task": cfg["task"], "ts": ts, "status": "done", "detail": detail[:80],
                       "key": "csv:%s:%s:%s:%r" % (os.path.basename(cfg["path"]), cfg["task"], rid, ts)})
    return events, {"ok": True, "message": "%d 行，%d 行有 %s" % (len(rows), len(events), cfg["date_column"])}


def collect_csv_due(cfg: dict):
    """Follow-up check on a CSV: a row is overdue once `due_days` have passed since its
    `date_column` while one of `require_columns` is still empty (D7 views not filled in).

    Each scan reports a snapshot: "done" when nothing is overdue, "fail" listing the overdue
    rows otherwise. Give its task `"resolve": "latest"` so the newest snapshot decides.
    """
    req = list(cfg.get("require_columns", []))
    rows, err = _read_csv(cfg, [cfg["date_column"], cfg.get("key_column")] + req)
    if err:
        return [], {"ok": False, "message": err}
    now, due_days = time.time(), float(cfg.get("due_days", 7))
    due, overdue = 0, []
    for n, row in enumerate(rows, 2):
        ts = _parse_when(row.get(cfg["date_column"]))
        if ts is None or ts + due_days * 86400 > now:
            continue
        due += 1
        if any(not _cell(row, c) for c in req):
            overdue.append(_row_id(row, cfg, n))
    status = "fail" if overdue else "done"
    if overdue:
        detail = "待补: " + ", ".join(overdue)
    else:
        detail = "都补了（%d 行到期）" % due if due else "还没有到期的"
    # Same day + same picture (status, rows due, rows overdue) = same key, so a rescan that sees
    # nothing new adds nothing, while every change is recorded and becomes the newest event.
    event = {"task": cfg["task"], "ts": now, "status": status, "detail": detail[:80],
             "key": "csvdue:%s:%s:%s:%d:%s" % (cfg["task"], time.strftime("%Y-%m-%d"), status, due,
                                               ",".join(overdue))}
    return [event], {"ok": True, "message": "%d 行到期，%d 行待补" % (due, len(overdue))}


# --------------------------------------------------------- GitHub Actions runs

_GH_CACHE: dict = {}
_GH_CONCLUSION = {"success": "done", "failure": "fail", "timed_out": "fail", "startup_failure": "fail"}


def collect_github_runs(cfg: dict):
    """Runs of one GitHub Actions workflow, read with the GitHub CLI (`gh run list`), so a
    private repo works with the account `gh auth login` set up. success -> done,
    failure / timeout -> fail; cancelled, skipped and unfinished runs are ignored.
    gh is called at most every `every_seconds` (default 3600); in between the last answer is reused."""
    ck = (cfg["repo"], cfg["workflow"])
    cached = _GH_CACHE.get(ck)
    if cached and time.time() - cached[0] < float(cfg.get("every_seconds", 3600)):
        return cached[1], cached[2]
    cmd = [cfg.get("gh", "gh"), "run", "list", "-R", cfg["repo"], "-w", cfg["workflow"],
           "-L", str(int(cfg.get("limit", 20))), "--json", "databaseId,conclusion,status,updatedAt,event"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return [], {"ok": False, "message": "找不到 gh（GitHub CLI）"}
    except subprocess.TimeoutExpired:
        return [], {"ok": False, "message": "gh run list 超时"}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines() or ["exit %d" % p.returncode]
        return [], {"ok": False, "message": "gh: " + err[-1][:120]}
    events = []
    for r in json.loads(p.stdout or "[]"):
        status = _GH_CONCLUSION.get(r.get("conclusion") or "")
        if r.get("status") != "completed" or not status:
            continue
        ts = datetime.strptime(r["updatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        events.append({"task": cfg["task"], "ts": ts, "status": status,
                       "detail": "%s · %s" % (r["conclusion"], r.get("event", "")),
                       "key": "gh:%s:%s" % (cfg["repo"], r["databaseId"])})
    health = {"ok": True, "message": "%s 最近 %d 次跑完的" % (cfg["workflow"], len(events))}
    _GH_CACHE[ck] = (time.time(), events, health)
    return events, dict(health)


# ------------------------------------------------------------------ HTTP ping

def collect_http_ping(cfg: dict):
    """Is a local service up? Shows only in the data-source health list, never as a task.
    Any HTTP answer (even 404) counts as up; a refused or timed-out connection does not."""
    url = cfg["url"]
    try:
        with urllib.request.urlopen(url, timeout=float(cfg.get("timeout", 3))) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as ex:  # URLError, socket timeout, connection reset ...
        return [], {"ok": False, "message": "连不上 %s（%s）" % (url, getattr(ex, "reason", ex))}
    return [], {"ok": True, "message": "在线 %s（HTTP %d）" % (url, code)}


COLLECTORS = {
    "maa_wrapper": collect_maa_wrapper,
    "maa_autorun": collect_maa_autorun,
    "march7th": collect_march7th,
    "csv_dates": collect_csv_dates,
    "csv_due": collect_csv_due,
    "github_runs": collect_github_runs,
    "http_ping": collect_http_ping,
}
