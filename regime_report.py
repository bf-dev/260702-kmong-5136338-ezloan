#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recompute the head-to-head regime table and the 배너잔여 drain projection.

Everything the previous three sessions did by hand, in one read-only pass, so the next
session does not redo the arithmetic (or get a different number from a different ad-hoc
snippet). Customer 5136338 / 더원대부 585 vs 옥자대부 544.

Inputs (all read-only):
  --audit  slot_audit_*.jsonl   page-truth achieved slots from slot_audit_kr.py (repeatable)
  --summary race_summary.jsonl  the sampler's real-time arrival capture; used ONLY to fill
                                a post whose /rq page has since expired (exists=false), so
                                an expired page is a filled row, not a silent gap
  gateway Postgres              the loop's own [cycle] lines, for 배너잔여 over time

A post counts as head-to-head only when BOTH 585 and 544 are on the banner list; a win is
slot_ours < slot_rival. Wilson score interval, 95%, because the normal approximation is
wrong at these n and at rates near 1.

    python3 regime_report.py --audit out/260823_full/slot_audit_combined.jsonl \
                             --audit out/260824_full/slot_audit_32042_32052.jsonl \
                             --summary out/260824_full/race_summary.jsonl \
                             --write-combined out/260824_full/slot_audit_combined.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import re

ME = "585"
RIVAL = "544"
CUSTOMER_ID = "5136338"

# Regime cutoffs. Add a letter ONLY when the loop is restarted or moved to another host;
# do not silently re-slice an existing regime, the whole point is that C is one config.
REGIMES = [
    ("A", "customer PC, v2.5.5 (Windows, home net)", 31960, 31984),
    ("B", "server run, before the 04:56Z restart", 32003, 32014),
    ("C", "server run on external-8, direct KR egress", 32015, 99999),
]

PSQL = ["docker", "exec", "neoworks-postgres", "psql", "-U", "neoworks", "-d", "neoworks",
        "-A", "-F", "\t", "-t", "-c"]
CYCLE_SQL = (
    "SELECT to_char(\"createdAt\",'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'), text "
    "FROM \"IngestedLog\" WHERE \"customerKey\"='%s' AND text LIKE '%%[cycle]%%' "
    "ORDER BY \"createdAt\"" % CUSTOMER_ID)
_BAL = re.compile(r"배너잔여=(\d+)")
_EPOCH_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)\.(\d+)Z")


def wilson(k: int, n: int, z: float = 1.959963985):
    """95% Wilson score interval for k successes in n trials, as (lo, hi) in percent."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half) * 100, min(1.0, centre + half) * 100)


def epoch(ts: str) -> float:
    m = _EPOCH_RE.match(ts)
    if not m:
        return 0.0
    import calendar
    y, mo, d, h, mi, s, frac = m.groups()
    base = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0))
    return base + float("0." + frac)


def load_audits(paths):
    """post id -> audit row. A readable row always beats an exists=false row for the same
    post (32041 was audited twice: once before its page rendered, once after)."""
    rows = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                p = int(r["post"])
                old = rows.get(p)
                if old is None or (r.get("slot_ours") and not old.get("slot_ours")):
                    rows[p] = r
    return rows


def fill_from_summary(rows, summary_path):
    """Fill posts whose page has expired from the sampler's own real-time capture.

    The sampler recorded the arrival order live, so it is the same measurement the page
    would have shown; the page merely stopped existing (requester deleted the 대출문의).
    """
    filled = []
    if not summary_path or not os.path.exists(summary_path):
        return filled
    with open(summary_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            s = json.loads(line)
            p = int(s["post"])
            cur = rows.get(p)
            if cur is not None and cur.get("slot_ours"):
                continue
            order = [a["advertiser"] for a in s.get("arrivals", [])]
            if ME not in order or RIVAL not in order:
                continue
            rows[p] = {"post": p, "exists": True, "status": 200, "order": order,
                       "names": {a["advertiser"]: a.get("name", "") for a in s.get("arrivals", [])},
                       "slot_ours": order.index(ME) + 1, "slot_rival": order.index(RIVAL) + 1,
                       "field": len(order), "source": "race_summary(page expired)"}
            filled.append(p)
    return filled


def balance_series():
    raw = subprocess.run(PSQL + [CYCLE_SQL], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    pts = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        m = _BAL.search(parts[1])
        if m:
            pts.append((parts[0].strip(), int(m.group(1))))
    return pts


def last_topup(pts):
    """The account is topped up by hand every week or two, so the series is NOT monotonic:
    do not anchor a drain rate on the global max without finding the top-up first."""
    prev = None
    anchor = pts[0] if pts else None
    for ts, bal in pts:
        if prev is not None and bal - prev > 1:
            anchor = (ts, bal)
        prev = bal
    return anchor


def window(pts, since_ts):
    sel = [(t, b) for t, b in pts if t >= since_ts]
    if len(sel) < 2:
        return None
    hours = (epoch(sel[-1][0]) - epoch(sel[0][0])) / 3600.0
    per_h = (sel[0][1] - sel[-1][1]) / hours if hours else 0.0
    return sel[0][0], sel[0][1], sel[-1][0], sel[-1][1], hours, per_h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="append", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--write-combined")
    ap.add_argument("--no-drain", action="store_true")
    ap.add_argument("--drain-since", default="2026-08-23T01:56:06.399Z",
                    help="anchor for the current-run drain window (the loop's last cold start)")
    a = ap.parse_args()

    rows = load_audits(a.audit)
    filled = fill_from_summary(rows, a.summary)
    if a.write_combined:
        with open(a.write_combined, "w", encoding="utf-8") as fh:
            for p in sorted(rows):
                fh.write(json.dumps(rows[p], ensure_ascii=False) + "\n")

    print("audited posts: %d (%d-%d), filled from sampler capture: %s"
          % (len(rows), min(rows), max(rows), filled or "none"))
    print()
    print("regime                                            posts        h2h  wins   rate     95% CI            +-")
    for letter, label, lo, hi in REGIMES:
        sel = [r for p, r in sorted(rows.items()) if lo <= p <= hi]
        h2h = [r for r in sel if r.get("slot_ours") and r.get("slot_rival")]
        wins = [r for r in h2h if r["slot_ours"] < r["slot_rival"]]
        n, k = len(h2h), len(wins)
        clo, chi = wilson(k, n)
        span = "%d-%d" % (min((r["post"] for r in sel), default=0),
                          max((r["post"] for r in sel), default=0))
        print("%s  %-42s %-12s %3d %5d %6.1f%%  [%5.1f%%, %5.1f%%]  %4.1fpp"
              % (letter, label, span, n, k, (100.0 * k / n) if n else 0.0, clo, chi,
                 (chi - clo) / 2))
        if letter == "C":
            print("   wins: %s" % ",".join(str(r["post"]) for r in wins))
            print("   losses: %s" % ",".join(str(r["post"]) for r in h2h if r not in wins))

    if not a.no_drain:
        pts = balance_series()
        anchors = [("since last top-up", last_topup(pts)[0])]
        if a.drain_since:
            anchors.append(("current loop run", a.drain_since))
        print()
        for label, since in anchors:
            w = window(pts, since)
            if not w:
                continue
            f_ts, f_bal, l_ts, l_bal, hours, per_h = w
            print("배너잔여 %-18s %d (%s) -> %d (%s): %d over %.3fh = %.2f/hour"
                  % (label, f_bal, f_ts, l_bal, l_ts, f_bal - l_bal, hours, per_h))
            if per_h > 0:
                print("%22s days to zero from %d: %.2f days" % ("", l_bal, l_bal / per_h / 24))


if __name__ == "__main__":
    main()
