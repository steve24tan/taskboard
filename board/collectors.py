"""Collectors turn the tools' own files into events. They only read; they never start,
stop or configure the tools.

Each collector returns (events, health):
  events  list of dicts {task, ts, status, detail, key}
  health  {"ok": bool, "message": str}

Log line times are the local time of the machine that wrote them (the same machine this
board runs on), so they are converted with time.mktime.
"""
from __future__ import annotations

import glob
import os
import re
import time
from datetime import date, timedelta


def _local_ts(day: str, hms: str) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    hh, mm, ss = (int(x) for x in hms.split(":"))
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


def _recent_logs(log_dir: str, days: int) -> list[tuple[str, str]]:
    """[(path, 'YYYY-MM-DD'), ...] for date-named logs of the last `days` days, oldest first."""
    oldest = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for p in glob.glob(os.path.join(log_dir, "*.log")):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", os.path.basename(p))
        if m and m.group(1) >= oldest:
            out.append((p, m.group(1)))
    return sorted(out, key=lambda x: x[1])


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
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


COLLECTORS = {
    "maa_wrapper": collect_maa_wrapper,
    "march7th": collect_march7th,
}
