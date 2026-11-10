#!/usr/bin/env python3
"""TaskBoard - did today's dailies / this week's weeklies get done?

    python taskboard.py            start the dashboard (http://localhost:8765)
    python taskboard.py --once     scan once and print the status, no server
"""
import argparse
import json
import os
import sys

from board.app import ROOT, Board, serve

for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

MARK = {"done": "OK ", "partial": "~~ ", "fail": "X  ", "pending": ".. "}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "board.db"))
    ap.add_argument("--once", action="store_true", help="scan once, print status, exit")
    args = ap.parse_args()

    if not os.path.isfile(args.config):
        print("No %s - copy config.example.json to config.json and fill in the paths." % args.config)
        return 1
    with open(args.config, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    os.makedirs(os.path.dirname(args.db), exist_ok=True)

    if not args.once:
        serve(cfg, args.db)
        return 0

    board = Board(cfg, args.db)
    print("new events: %d" % board.scan())
    for name, h in board.health.items():
        print("[%s] %s %s" % ("ok" if h["ok"] else "!!", name, h["message"]))
    st = board.status()
    for g in st["groups"]:
        print("\n" + g["name"])
        for t in g["tasks"]:
            c = t["current"]
            print("  %s%-16s %-7s %s" % (MARK[c["state"]], t["name"], t["cadence"], c["detail"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
