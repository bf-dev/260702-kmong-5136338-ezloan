#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure the register hot path. READ-ONLY: anonymous, never logs in, never writes.

The number that decides whether we take slot 1 is not "how fast is the network", it is
"how long after ezloan opens registration does our rq_addbanner arrive". That is:

    detection lag           U(0, FRONTIER_POLL_SECONDS), mean = tick/2
  + live-page probe RTT     GET /rq/{live id}, ~35KB gzipped, the round trip that
                            confirms the post exists
  + write RTT               POST /api/rq_addbanner, 47 bytes. Measured here with
                            /api/rq_addbanner_check instead: same endpoint class, same
                            payload size, same PHP path, and it spends no 배너잔여.

Both probes are anonymous, so this can be run against the live site while the customer's
real loop is registering, without touching their account or their credits.

    python3 measure_hotpath.py                 # 30 samples, prints the budget
    python3 measure_hotpath.py -n 60 --tick 0.08
"""

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402


BASE = "https://ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def newest_post_id(s):
    r = s.get(f"{BASE}/rq", timeout=20)
    import re
    ids = [int(m) for m in re.findall(r"/rq/(\d+)", r.text)]
    if not ids:
        raise SystemExit("could not read the /rq listing")
    return max(ids)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--tick", type=float, default=0.0,
                    help="FRONTIER_POLL_SECONDS to model (0 = read it from config)")
    ap.add_argument("--window", type=int, default=0)
    ap.add_argument("--proxy", default="", help="socks5h://127.0.0.1:PORT to measure the "
                                                "tunnelled path instead of direct")
    args = ap.parse_args()

    tick = args.tick
    window = args.window
    if not tick or not window:
        import config
        tick = tick or config.FRONTIER_POLL_SECONDS
        window = window or config.PROBE_WINDOW

    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.trust_env = False
    s.proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else {}

    own = s.get("https://api.ipify.org", timeout=15).text.strip()
    live = newest_post_id(s)
    future = live + 5000            # a number that will not exist for months
    print(f"egress {own}   newest live post {live}   "
          f"{'via ' + args.proxy if args.proxy else 'direct, no proxy'}")

    # warm the connection so we measure keep-alive, which is what the loop actually has
    for _ in range(3):
        s.get(f"{BASE}/rq/{future}", timeout=15)

    probe_live, probe_miss, write = [], [], []
    for _ in range(args.n):
        t = time.time(); r = s.get(f"{BASE}/rq/{live}", timeout=20); probe_live.append((time.time() - t) * 1000)
        t = time.time(); s.get(f"{BASE}/rq/{future}", timeout=20); probe_miss.append((time.time() - t) * 1000)
        t = time.time(); s.get(f"{BASE}/api/rq_addbanner_check/{future}", timeout=20); write.append((time.time() - t) * 1000)
        assert r.status_code == 200
        time.sleep(0.1)

    def row(label, xs):
        print(f"  {label:<34} p50 {statistics.median(xs):6.1f}  p90 {pct(xs, 90):6.1f}  "
              f"min {min(xs):6.1f}  max {max(xs):6.1f}   (n={len(xs)})")

    print(f"\nlegs, warm keep-alive, ms:")
    row("GET /rq/{live}  (~35KB, detect)", probe_live)
    row("GET /rq/{future} (353B, miss)", probe_miss)
    row("POST-class 47B api round trip", write)

    floor = statistics.median(probe_live) + statistics.median(write)
    print(f"\nhot path with tick={tick}s window={window}:")
    print(f"  floor  (detected the instant it opens)   {floor:6.1f} ms")
    print(f"  mean   (+ tick/2 detection lag)          {floor + 500 * tick:6.1f} ms")
    print(f"  worst  (+ full tick)                     {floor + 1000 * tick:6.1f} ms")
    fast_rate = (window + 1) / tick
    print(f"\nrequest rate: fast ticks {(1 / tick) - 1:.1f}/s x {window + 1} req = "
          f"{fast_rate - (window + 1):.1f}/s, plus 1 heavy tick/s x {6 + 1} req "
          f"= {fast_rate - (window + 1) + 7:.1f} req/s total, plus 1 x 309KB list/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
