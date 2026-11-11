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
from board import collectors  # noqa: E402
from board.collectors import (collect_csv_dates, collect_csv_due, collect_github_runs,  # noqa: E402
                              collect_http_ping, collect_maa_autorun, collect_march7th,
                              collect_maa_wrapper)
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

    def test_target_counts_done_events(self):
        one = [self.ev(1, "done")]
        two = one + [self.ev(2, "fail"), self.ev(3, "done")]
        self.assertEqual((resolve(one, 2)["state"], resolve(one, 2)["progress"]), ("partial", "1/2"))
        self.assertEqual((resolve(two, 2)["state"], resolve(two, 2)["progress"]), ("done", "2/2"))
        r = resolve([self.ev(1, "fail")], 2)
        self.assertEqual((r["state"], r["progress"]), ("fail", "0/2"))
        self.assertEqual(resolve([], 2)["progress"], "0/2")
        self.assertIsNone(resolve(one)["progress"])

    def test_latest(self):
        evs = [self.ev(1, "done"), self.ev(2, "fail")]
        self.assertEqual(resolve(evs)["state"], "done")
        self.assertEqual(resolve(evs, latest=True)["state"], "fail")
        self.assertEqual(resolve(evs + [self.ev(3, "done", "manual")], latest=True)["state"], "done")


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

    def test_maa_autorun(self):
        d = date.today().strftime("%Y%m%d")
        day = TODAY
        write(os.path.join(self.root, "autorun_%s.log" % d), "﻿" + "\n".join([
            "[%s 00:20:00] ========== dual-account daily autorun START ==========" % day,
            "[%s 00:21:00] TW : launching MAA (timeout=45 min)" % day,
            "[%s 00:40:00] TW : all daily tasks completed [OK]" % day,
            "[%s 00:41:00] Global : task chain finished, but these failed: Fight, Mall [WARN]" % day,
            "[%s 00:42:00] Global : retry attempt 2 of 2" % day,
            "[%s 01:00:00] Global : did not finish before timeout [WARN] forcing cleanup" % day,
            "[%s 01:01:00] Global : NOT stamped done -- wrote needs_sup_Global.txt for the supervisor" % day,
            "[%s 01:02:00] JP : all daily tasks completed [OK]" % day,
            "[%s 01:03:00] TW : already done for game-day %s, skipping" % (day, day),
        ]) + "\n")
        write(os.path.join(self.root, "autorun_19990101.log"), "[1999-01-01 00:00:00] TW : all daily tasks completed [OK]\n")
        events, health = collect_maa_autorun({"log_dir": self.root, "accounts": {"TW": "ark.tw", "Global": "ark.gl"}})
        self.assertTrue(health["ok"])
        self.assertEqual([(e["task"], e["status"], e["detail"]) for e in events], [
            ("ark.tw", "done", "完成"),
            ("ark.gl", "partial", "没做完: Fight, Mall"),
            ("ark.gl", "fail", "超时"),
            ("ark.gl", "fail", "没完成，交给 supervisor"),
        ])
        self.assertEqual(events[0]["ts"], collectors._local_ts(day, "00:40:00"))
        self.assertFalse(collect_maa_autorun({"log_dir": os.path.join(self.root, "nope")})[1]["ok"])

    def csv(self, rows):
        path = os.path.join(self.root, "episode_log.csv")
        write(path, "﻿ep,title_en,posted_date,yt_views_d7\n" + "".join(r + "\n" for r in rows))
        return path

    def test_csv_dates(self):
        recent = (date.today() - timedelta(days=2)).isoformat()
        path = self.csv(["ep01,Welcome,%s 18:30,1200" % recent, "ep02,At Your Service,,", "ep00,Old,2001-01-01,"])
        events, health = collect_csv_dates({"path": path, "task": "ga.post", "date_column": "posted_date",
                                            "key_column": "ep", "detail_columns": ["ep", "title_en"]})
        self.assertTrue(health["ok"])
        self.assertEqual([(e["task"], e["status"], e["detail"]) for e in events], [("ga.post", "done", "ep01 Welcome")])
        self.assertEqual(datetime.fromtimestamp(events[0]["ts"]).strftime("%H:%M"), "18:30")
        _, h = collect_csv_dates({"path": path, "task": "t", "date_column": "nope"})
        self.assertFalse(h["ok"])
        self.assertFalse(collect_csv_dates({"path": path + ".x", "task": "t", "date_column": "d"})[1]["ok"])

    def test_csv_due(self):
        old = (date.today() - timedelta(days=9)).isoformat()
        fresh = (date.today() - timedelta(days=2)).isoformat()
        cfg = {"task": "ga.d7", "date_column": "posted_date", "key_column": "ep",
               "require_columns": ["yt_views_d7"], "due_days": 7}
        cfg["path"] = self.csv(["ep01,A,%s,1200" % old, "ep02,B,%s," % old, "ep03,C,%s," % fresh])
        (e,), h = collect_csv_due(cfg)
        self.assertEqual((e["status"], e["detail"]), ("fail", "待补: ep02"))
        self.assertIn("2 行到期", h["message"])
        cfg["path"] = self.csv(["ep01,A,%s,1200" % old, "ep02,B,%s,800" % old, "ep03,C,%s," % fresh])
        (e2,), _ = collect_csv_due(cfg)
        self.assertEqual((e2["status"], e2["detail"]), ("done", "都补了（2 行到期）"))
        self.assertNotEqual(e["key"], e2["key"])
        cfg["path"] = self.csv([])
        self.assertEqual(collect_csv_due(cfg)[0][0]["detail"], "还没有到期的")

    def test_github_runs(self):
        out = json.dumps([
            {"databaseId": 1, "conclusion": "success", "status": "completed", "updatedAt": "2026-11-09T09:20:00Z", "event": "schedule"},
            {"databaseId": 2, "conclusion": "failure", "status": "completed", "updatedAt": "2026-11-02T09:20:00Z", "event": "schedule"},
            {"databaseId": 3, "conclusion": "cancelled", "status": "completed", "updatedAt": "2026-11-01T09:20:00Z", "event": "push"},
            {"databaseId": 4, "conclusion": "", "status": "in_progress", "updatedAt": "2026-11-10T09:20:00Z", "event": "schedule"},
        ])
        calls = []

        class P:
            returncode, stdout, stderr = 0, out, ""

        real = collectors.subprocess.run
        collectors.subprocess.run = lambda cmd, **kw: calls.append(cmd) or P
        collectors._GH_CACHE.clear()
        try:
            cfg = {"repo": "me/meta", "workflow": "build.yml", "task": "ga.meta"}
            events, health = collect_github_runs(cfg)
            self.assertEqual([(e["status"], e["key"]) for e in events], [("done", "gh:me/meta:1"), ("fail", "gh:me/meta:2")])
            self.assertEqual(events[0]["ts"], datetime(2026, 11, 9, 9, 20, tzinfo=timezone.utc).timestamp())
            self.assertTrue(health["ok"])
            collect_github_runs(cfg)
            self.assertEqual(len(calls), 1)                       # second call served from cache
            P.returncode, P.stderr = 1, "HTTP 404: Not Found\n"
            collectors._GH_CACHE.clear()
            self.assertEqual(collect_github_runs(cfg), ([], {"ok": False, "message": "gh: HTTP 404: Not Found"}))
        finally:
            collectors.subprocess.run = real
            collectors._GH_CACHE.clear()

    def test_http_ping_down(self):
        events, health = collect_http_ping({"url": "http://127.0.0.1:9/", "timeout": 1})
        self.assertEqual(events, [])
        self.assertFalse(health["ok"])


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

    def test_target_progress_and_ping(self):
        cfg = {"groups": [{"name": "G", "tasks": [
            {"id": "post", "name": "发片", "cadence": "weekly", "target": 2}]}],
            "collectors": [{"type": "http_ping", "name": "self", "url": self.base + "/nope"}]}
        board = Board(cfg, os.path.join(self.tmp.name, "t.db"))
        try:
            board.store.add("post", time.time(), "done", "ep01", "api", "k1")
            board.scan()
            self.assertTrue(board.health["self"]["ok"])           # a 404 still means the server is up
            cur = board.status()["groups"][0]["tasks"][0]["current"]
            self.assertEqual((cur["state"], cur["detail"]), ("partial", "1/2 · ep01"))
            with self.assertRaises(ValueError):
                Board({"groups": [{"name": "G", "tasks": [{"id": "x", "name": "x", "target": 0}]}]},
                      os.path.join(self.tmp.name, "u.db"))
        finally:
            board.store._db.close()

    def test_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"TaskBoard", r.read())


if __name__ == "__main__":
    unittest.main()
