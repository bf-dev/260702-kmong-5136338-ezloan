"""Measure, on a live post, WHEN the post becomes registerable and WHEN each
advertiser's banner actually appears.

Read-only. Anonymous GETs of the public /rq pages through the Korean egress. It never
logs in, never registers, never spends 배너잔여. Safe to run next to a live registration
run (that is the point: it watches what our own bot and its competitors do).

    python3 race_watch.py --from 32006 --minutes 180 --out /path/race.jsonl

Two thresholds are tracked separately, and the gap between them is the whole point:

  bot_live  : ezloan_bot.post_live()'s test - >=1000 bytes and one of
              "배너 등록을 눌러 주세요" / "js-memberConfirmView" / "rq_addbanner".
              This is what makes the registration loop fire rq_addbanner_check.
  full      : the post page actually rendered with its banner <ul>. This is the
              earliest moment any banner can appear, i.e. when the race really starts.

On post 32005 (2026-08-23) the bot fired at 03:07:37 and was told `no permission`;
the full page only existed at 03:07:56.2 and 옥자대부's banner landed at 03:07:56.35.
The bot marked 32005 seen and never came back, so we were absent from that post
entirely. That 18.4s gap is what this instrument measures.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import rank_tools

PROBE_PERIOD = 0.25       # existence polling (s). The live bot runs 0.15; stay lighter.
PRELIVE_KEEP = 160        # how many pre-live samples to keep in the record
BURST_SECONDS = 6.0       # back-to-back sampling right after the full page appears
MID_PERIOD = 0.5
MID_SECONDS = 30.0
TAIL_PERIOD = 3.0
TAIL_SECONDS = 150.0

BOT_MARKERS = ("배너 등록을 눌러 주세요", "js-memberConfirmView", "rq_addbanner")
BOT_MIN_BYTES = 1000


def probe(s, pid):
    """-> (t_monotonic, status, nbytes, bot_live, full, banners)"""
    t = time.monotonic()
    try:
        r = s.get(f"{rank_tools.BASE}/rq/{pid}", timeout=8)
    except Exception as e:  # noqa: BLE001
        return t, f"err:{type(e).__name__}", 0, False, False, []
    html = r.text or ""
    n = len(html)
    bot_live = (r.status_code == 200 and n >= BOT_MIN_BYTES
                and any(m in html for m in BOT_MARKERS))
    full = rank_tools.page_exists(html, r.status_code)
    return t, r.status_code, n, bot_live, full, (rank_tools.banner_order(html) if full else [])


def watch_post(s, pid, out_fh, verbose=True):
    prelive = []
    t_bot_live = None
    while True:
        t, st, n, bot_live, full, banners = probe(s, pid)
        prelive.append({"t": t, "status": st, "bytes": n, "bot_live": bot_live, "full": full})
        if bot_live and t_bot_live is None:
            t_bot_live = t
        if full:
            t0 = t
            t0_wall = time.time()
            break
        sleep = PROBE_PERIOD - (time.monotonic() - t)
        if sleep > 0:
            time.sleep(sleep)
    last_absent = None
    for row in reversed(prelive[:-1]):
        if not row["full"]:
            last_absent = row["t"]
            break
    for row in prelive:
        row["t"] = round(row["t"] - t0, 3)
    prelive = prelive[-PRELIVE_KEEP:]

    seen, samples, k = {}, [], 0
    while True:
        elapsed = time.monotonic() - t0
        if elapsed > TAIL_SECONDS:
            break
        if k:
            _t, _st, _n, _bl, full, banners = probe(s, pid)
            elapsed = time.monotonic() - t0
            if not full:
                banners = []
        for slot, (aid, name, cls) in enumerate(banners, 1):
            if aid not in seen:
                seen[aid] = {"advertiser": aid, "name": name, "class": cls,
                             "t": round(elapsed, 3), "slot_at_arrival": slot, "sample": k}
        samples.append({"k": k, "t": round(elapsed, 3), "order": [b[0] for b in banners]})
        k += 1
        period = 0.0 if elapsed < BURST_SECONDS else (
            MID_PERIOD if elapsed < MID_SECONDS else TAIL_PERIOD)
        if period:
            time.sleep(period)

    final = samples[-1]["order"] if samples else []
    rec = {
        "post": pid,
        "full_page_wall": t0_wall,
        "publish_bracket_s": round(t0 - last_absent, 3) if last_absent else None,
        # negative = the bot's detector fired BEFORE the page could hold a banner
        "bot_live_minus_full_s": round(t_bot_live - t0, 3) if t_bot_live is not None else None,
        "arrivals": sorted(seen.values(), key=lambda d: (d["t"], d["slot_at_arrival"])),
        "final_order": final,
        "final_slot_585": (final.index("585") + 1) if "585" in final else None,
        "prelive": prelive,
        "samples": samples,
    }
    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_fh.flush()
    if verbose:
        arr = ", ".join(f"{a['name']}({a['advertiser']})@{a['t']:.2f}s"
                        for a in rec["arrivals"][:5])
        print(f"[post {pid}] bracket<={rec['publish_bracket_s']}s "
              f"bot_live-full={rec['bot_live_minus_full_s']}s "
              f"slot_585={rec['final_slot_585']} :: {arr}", flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, required=True)
    ap.add_argument("--minutes", type=float, default=180.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:1085")
    args = ap.parse_args()

    s = rank_tools.probe_session(args.proxy)
    deadline = time.monotonic() + args.minutes * 60
    pid = args.start
    with open(args.out, "a", encoding="utf-8") as fh:
        while True:
            _t, _st, _n, _bl, full, _b = probe(s, pid)
            if not full:
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
