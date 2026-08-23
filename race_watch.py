"""Measure, on a live post, WHEN each advertiser's banner actually appears.

Read-only. Anonymous GETs of the public /rq pages through the Korean egress. It never
logs in, never registers, never spends 배너잔여. Safe to run next to a live registration
run (that is the point: it watches what our own bot and its competitors do).

    python3 race_watch.py --from 32005 --minutes 90 --out /path/race.jsonl

For each post id it:
  1. polls existence at PROBE_PERIOD until the post appears; the last "absent" and first
     "present" observations BRACKET the publish moment,
  2. samples the same page repeatedly and records, per advertiser, the first sample in
     which its banner is present and its slot at that moment,
  3. writes one JSON object per post to the jsonl file.

`t` values are seconds since `first_present` (the first observation where the post
existed). `publish_bracket` is how much uncertainty there is around the true publish
instant, i.e. how far BEFORE t=0 the post may already have been live.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import rank_tools

PROBE_PERIOD = 0.25       # existence polling (s). The live bot runs 0.15; stay lighter.
BURST_SECONDS = 6.0       # back-to-back sampling right after the post appears
MID_PERIOD = 0.5          # then this
MID_SECONDS = 30.0
TAIL_PERIOD = 3.0         # then this
TAIL_SECONDS = 150.0


def watch_post(s, pid, out_fh, verbose=True):
    last_absent = None
    first_present_wall = None
    while True:
        t = time.monotonic()
        exists, banners, dt, status = rank_tools.fetch_post(s, pid)
        if exists:
            first_present = t
            first_present_wall = time.time()
            bracket = (t - last_absent) if last_absent else None
            break
        last_absent = t
        sleep = PROBE_PERIOD - (time.monotonic() - t)
        if sleep > 0:
            time.sleep(sleep)

    seen = {}          # advertiser id -> {name, t, slot, sample}
    samples = []
    n = 0
    while True:
        elapsed = time.monotonic() - first_present
        if elapsed > TAIL_SECONDS:
            break
        if n:
            exists, banners, dt, status = rank_tools.fetch_post(s, pid)
            elapsed = time.monotonic() - first_present
            if not exists:
                banners = []
        order = [b[0] for b in banners]
        for slot, (aid, name, cls) in enumerate(banners, 1):
            if aid not in seen:
                seen[aid] = {"advertiser": aid, "name": name, "class": cls,
                             "t": round(elapsed, 3), "slot_at_arrival": slot,
                             "sample": n}
        samples.append({"n": n, "t": round(elapsed, 3), "order": order})
        n += 1
        if elapsed < BURST_SECONDS:
            period = 0.0
        elif elapsed < MID_SECONDS:
            period = MID_PERIOD
        else:
            period = TAIL_PERIOD
        if period:
            time.sleep(period)

    final = samples[-1]["order"] if samples else []
    rec = {
        "post": pid,
        "first_present_wall": first_present_wall,
        "publish_bracket_s": round(bracket, 3) if bracket else None,
        "arrivals": sorted(seen.values(), key=lambda d: (d["t"], d["slot_at_arrival"])),
        "final_order": final,
        "final_slot_585": (final.index("585") + 1) if "585" in final else None,
        "samples": samples,
    }
    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_fh.flush()
    if verbose:
        arr = ", ".join(f"{a['name']}({a['advertiser']})@{a['t']:.2f}s"
                        for a in rec["arrivals"][:6])
        print(f"[post {pid}] bracket<={rec['publish_bracket_s']}s "
              f"final_slot_585={rec['final_slot_585']} :: {arr}", flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, required=True)
    ap.add_argument("--minutes", type=float, default=90.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:1085")
    args = ap.parse_args()

    s = rank_tools.probe_session(args.proxy)
    deadline = time.monotonic() + args.minutes * 60
    pid = args.start
    with open(args.out, "a", encoding="utf-8") as fh:
        # skip forward over ids that are already live (we missed their publish)
        while True:
            exists, _b, _dt, _st = rank_tools.fetch_post(s, pid)
            if not exists:
                break
            print(f"[skip] {pid} already live", flush=True)
            pid += 1
        print(f"[watch] waiting on post {pid}", flush=True)
        while time.monotonic() < deadline:
            watch_post(s, pid, fh)
            pid += 1
    print("[watch] done", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
