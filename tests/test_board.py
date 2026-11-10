"""python -m unittest discover -s tests   (from the repo root)"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from board import periods  # noqa: E402
from board.app import Board, make_handler  # noqa: E402
from board.collectors import collect_march7th, collect_maa_wrapper  # noqa: E402
from board.store import resolve  # noqa: E402

CN = timezone(timedelta(hours=8))


def cn(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CN).timestamp()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()

MAA_LOG_YESTERDAY = """[05:00:00] ==================================================
[05:00:00] 启动模拟器: MuMuPlayer.exe
[05:00:00] X 找不到模拟器: C:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\MuMuPlayer.exe
"""
MAA_LOG_TODAY = """[05:00:00] ==================================================
[05:00:01] 启动模拟器: MuMuPlayer.exe
[05:01:00] OK ADB 就绪 (55s) - 127.0.0.1:16384
[05:01:05] 跑 MAA: maa run daily
[05:01:06]   | X something MAA printed
[05:20:00] X 失败: 输出里出现失败讯号: 超时
[05:20:10] -- 第 2 次尝试:重开模拟器 --
[05:21:05] 跑 MAA: maa run daily
[05:40:00] OK 完成 - 理智 0/135 / 打了 22 次 (40 分钟)
[06:00:00] ==================================================
[06:00:00] X 另一个 run_maa 还在跑(10 分钟前开始),这次跳过
[07:00:00] ==================================================
[07:00:05] 跑 MAA: maa fight CE-6
[07:10:00] OK 完成 - 完成 (10 分钟)
"""

M7A_LOG_YESTERDAY = """%s 06:00:00 | INFO | 开始运行
%s 06:10:00 | WARNING | 尝试启动游戏时发生错误：timeout
%s 06:30:00 | WARNING | 清体力未完成
%s 06:40:00 | INFO | 停止运行
""" % (YESTERDAY, YESTERDAY, YESTERDAY, YESTERDAY)
M7A_LOG_TODAY = """%s 06:00:00 | INFO | 开始运行
===== 日常任务 =====
%s 06:05:00 | ERROR | 发生错误: 获取当前界面超时
%s 06:05:01 | INFO | 停止运行
""" % (TODAY, TODAY, TODAY)


class PeriodTests(unittest.TestCase):
    def test_daily_reset_at_4(self):
        self.assertEqual(periods.period_start(cn(2026, 11, 10, 3, 59), "daily"), cn(2026, 11, 9, 4))
        self.assertEqual(periods.period_start(cn(2026, 11, 10, 4, 0), "daily"), cn(2026, 11, 10, 4))

    def test_weekly_starts_monday_4(self):
        self.assertEqual(periods.period_start(cn(2026, 11, 10, 12), "weekly"), cn(2026, 11, 9, 4))   # Tue
        self.assertEqual(periods.period_start(cn(2026, 11, 9, 3), "weekly"), cn(2026, 11, 2, 4))    # Mon 03:00

    def test_monthly(self):
        self.assertEqual(periods.period_start(cn(2026, 12, 1, 3), "monthly"), cn(2026, 11, 1, 4))
        self.assertEqual(periods.period_start(cn(2026, 12, 15, 3), "monthly"), cn(2026, 12, 1, 4))

    def test_recent_periods_are_contiguous(self):
        ps = periods.recent_periods(cn(2026, 11, 10, 12), "daily", 14)
        self.assertEqual(len(ps), 14)
        self.assertEqual(ps[-1][0], cn(2026, 11, 10, 4))
        for (a, b), (c, _) in zip(ps, ps[1:]):
            self.assertEqual(b, c)


class ResolveTests(unittest.TestCase):
    def ev(self, ts, status, source="auto"):
        return {"ts": ts, "status": status, "source": source, "detail": ""}

    def test_done_beats_earlier_fail(self):
        self.assertEqual(resolve([self.ev(1, "fail"), self.ev(2, "done")])["state"], "done")
        self.assertEqual(resolve([self.ev(1, "done"), self.ev(2, "fail")])["state"], "done")

    def test_manual_wins(self):
        self.assertEqual(resolve([self.ev(1, "fail"), self.ev(2, "done", "manual")])["state"], "done")
        self.assertEqual(resolve([self.ev(1, "done"), self.ev(2, "undone", "manual")])["state"], "pending")

    def test_empty(self):
        self.assertEqual(resolve([])["state"], "pending")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_maa_wrapper(self):
        logs = os.path.join(self.root, "maa", "logs")
        write(os.path.join(logs, YESTERDAY + ".log"), MAA_LOG_YESTERDAY)
        write(os.path.join(logs, TODAY + ".log"), MAA_LOG_TODAY)
        write(os.path.join(logs, "1999-01-01.log"), "[05:00:00] OK 完成 - too old\n")
        events, health = collect_maa_wrapper({
            "log_dir": logs, "profiles": {"maa run daily": "ark.daily", "maa fight CE-6": "ark.ce"},
            "default_task": "ark.daily"})
        self.assertTrue(health["ok"])
        got = [(e["task"], e["status"], e["detail"][:6]) for e in events]
        self.assertEqual(got, [
            ("ark.daily", "fail", "找不到模拟器"),
            ("ark.daily", "fail", "失败: 输出"),
            ("ark.daily", "done", "理智 0/1"),
            ("ark.ce", "done", "完成 (10"),
        ])
        self.assertEqual(len({e["key"] for e in events}), 4)

    def test_maa_missing_dir(self):
        events, health = collect_maa_wrapper({"log_dir": os.path.join(self.root, "nope")})
        self.assertEqual(events, [])
        self.assertFalse(health["ok"])

    def test_march7th(self):
        tool = os.path.join(self.root, "m7a")
        write(os.path.join(tool, "logs", YESTERDAY + ".log"), M7A_LOG_YESTERDAY)
        write(os.path.join(tool, "logs", TODAY + ".log"), M7A_LOG_TODAY)
        write(os.path.join(tool, "config.yaml"),
              "echo_of_war_enable: true\necho_of_war_timestamp: 1762700000.5 # last done\n"
              "weekly_divergent_timestamp: 0\n")
        events, health = collect_march7th({
            "tool_dir": tool, "daily_task": "hsr.daily",
            "timestamps": {"echo_of_war_timestamp": "hsr.echo", "weekly_divergent_timestamp": "hsr.div",
                           "currencywars_timestamp": "hsr.cw"}})
        got = [(e["task"], e["status"]) for e in events]
        self.assertEqual(got, [("hsr.daily", "partial"), ("hsr.daily", "fail"), ("hsr.echo", "done")])
        self.assertEqual(events[0]["detail"], "清体力未完成")
        self.assertEqual(events[2]["ts"], 1762700000.5)
        self.assertIn("currencywars_timestamp", health["message"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        logs = os.path.join(self.tmp.name, "logs")
        write(os.path.join(logs, TODAY + ".log"), MAA_LOG_TODAY)
        cfg = {"token": "s3cret", "groups": [{"name": "G", "tasks": [
            {"id": "ark.daily", "name": "日常"}, {"id": "study", "name": "复习", "cadence": "weekly"}]}],
            "collectors": [{"type": "maa_wrapper", "log_dir": logs, "default_task": "ark.daily",
                            "profiles": {"maa fight": "ark.ce"}},
                           {"type": "march7th", "name": "m7a", "tool_dir": os.path.join(self.tmp.name, "x")}]}
        self.board = Board(cfg, os.path.join(self.tmp.name, "b.db"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.board))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.board.store._db.close()
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def task(self, st, tid):
        return next(t for g in st["groups"] for t in g["tasks"] if t["id"] == tid)

    def test_flow(self):
        code, r = self.call("/api/scan", {})
        self.assertEqual(code, 200)
        self.assertEqual(r["added"], 2)                     # fail + done for ark.daily; ark.ce not configured
        self.assertEqual(self.call("/api/scan", {})[1]["added"], 0)   # rescans are idempotent
        _, st = self.call("/api/status")
        self.assertFalse(st["health"]["m7a"]["ok"])
        self.assertIn("ark.ce", st["health"]["maa_wrapper"]["message"])
        self.assertEqual(len(self.task(st, "ark.daily")["history"]), 14)
        self.assertEqual(len(self.task(st, "study")["history"]), 8)

        self.assertEqual(self.call("/api/tick", {"task": "study", "done": True})[0], 200)
        _, st = self.call("/api/status")
        self.assertEqual(self.task(st, "study")["current"]["state"], "done")
        self.call("/api/tick", {"task": "study", "done": False})
        _, st = self.call("/api/status")
        self.assertEqual(self.task(st, "study")["current"]["state"], "pending")

    def test_event_endpoint(self):
        self.assertEqual(self.call("/api/event", {"task": "study"})[0], 401)
        hdr = {"X-Board-Token": "s3cret"}
        self.assertEqual(self.call("/api/event", {"task": "nope"}, hdr)[0], 400)
        self.assertEqual(self.call("/api/event", {"task": "study", "status": "weird"}, hdr)[0], 400)
        code, r = self.call("/api/event", {"task": "study", "status": "partial", "key": "k1", "detail": "3/5"}, hdr)
        self.assertEqual((code, r["added"]), (200, True))
        self.assertFalse(self.call("/api/event", {"task": "study", "key": "k1"}, hdr)[1]["added"])
        _, st = self.call("/api/status")
        self.assertEqual(self.task(st, "study")["current"]["state"], "partial")

    def test_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"TaskBoard", r.read())


if __name__ == "__main__":
    unittest.main()
