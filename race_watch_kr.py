"""High-resolution version of race_watch.py, meant to run ON the Korean box itself.

Read-only, anonymous, no login, no writes. Copy to unicorn@external-1 and run there:

    scp race_watch_kr.py unicorn@external-1:~/ && ssh unicorn@external-1 \\
        'nohup python3 race_watch_kr.py --from 32006 --minutes 200 --out ~/race_kr.jsonl &'

Why on the box: from this host through the SOCKS tunnel a live /rq/{id} fetch is 125ms,
so back-to-back sampling can only resolve ~140ms and that is the same order as the thing
being measured (옥자대부 lands within 140ms). From external-1 the same fetch is 81ms, and
with STAGGER parallel samplers offset by 81/STAGGER ms the effective resolution is ~27ms.

Uses only the stdlib so nothing has to be installed on the box.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import re
import ssl
import sys
import threading
import time

HOST = "ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

_LIST_RE = re.compile(r'<ul class="section_body loan_list recommend">(.*?)</ul>', re.S)
_ITEM_RE = re.compile(r'<a href="/l/(\d+)" class="(item[^"]*)"[^>]*title="([^"]*)"', re.S)
BOT_MARKERS = ("배너 등록을 눌러 주세요", "js-memberConfirmView", "rq_addbanner")
BOT_MIN_BYTES = 1000
FULL_MIN_BYTES = 20000

PROBE_PERIOD = 0.25
STAGGER = 3            # parallel samplers during the burst
BURST_SECONDS = 8.0
MID_PERIOD = 0.5
MID_SECONDS = 30.0
TAIL_PERIOD = 3.0
TAIL_SECONDS = 150.0


class Conn:
    def __init__(self):
        self.ctx = ssl.create_default_context()
        self.c = None

    def get(self, path):
        for attempt in (0, 1):
            try:
                if self.c is None:
                    self.c = http.client.HTTPSConnection(HOST, timeout=8, context=self.ctx)
                self.c.request("GET", path, headers={"User-Agent": UA,
                                                     "Accept-Encoding": "gzip"})
                r = self.c.getresponse()
                body = r.read()
                if r.getheader("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return r.status, body.decode("utf-8", "replace")
            except Exception:
                try:
                    self.c.close()
                except Exception:
                    pass
                self.c = None
                if attempt:
                    return None, ""
        return None, ""


def parse(status, html):
    n = len(html)
    bot_live = (status == 200 and n >= BOT_MIN_BYTES
                and any(m in html for m in BOT_MARKERS))
    full = status == 200 and n >= FULL_MIN_BYTES and "loan_list recommend" in html
    banners = []
    if full:
        m = _LIST_RE.search(html)
        body = m.group(1) if m else ""
        banners = [(aid, title.split("-", 1)[0].strip(), cls.strip())
                   for aid, cls, title in _ITEM_RE.findall(body)]
    return n, bot_live, full, banners


def watch_post(pid, out_fh):
    conn = Conn()
    path = f"/rq/{pid}"
    prelive, t_bot_live = [], None
    while True:
        t = time.monotonic()
        n, bot_live, full, banners = parse(*conn.get(path))
        prelive.append({"t": t, "bytes": n, "bot_live": bot_live, "full": full})
        if bot_live and t_bot_live is None:
            t_bot_live = t
        if full:
            t0, t0_wall = t, time.time()
            break
        sleep = PROBE_PERIOD - (time.monotonic() - t)
        if sleep > 0:
            time.sleep(sleep)
    last_absent = next((r["t"] for r in reversed(prelive[:-1]) if not r["full"]), None)
    for r in prelive:
        r["t"] = round(r["t"] - t0, 3)
    prelive = prelive[-160:]

    lock = threading.Lock()
    seen, samples, stop = {}, [], threading.Event()

    def sampler(idx):
        c = Conn()
        time.sleep(idx * 0.027)
        while not stop.is_set():
            t = time.monotonic()
            elapsed = t - t0
            if elapsed > TAIL_SECONDS:
                return
            _n, _bl, full, banners = parse(*c.get(path))
            el = round(time.monotonic() - t0, 3)
            with lock:
                for slot, (aid, name, cls) in enumerate(banners, 1):
                    if aid not in seen:
                        seen[aid] = {"advertiser": aid, "name": name, "class": cls,
                                     "t": el, "slot_at_arrival": slot}
                samples.append({"t": el, "order": [b[0] for b in banners]})
            if elapsed >= BURST_SECONDS and idx:
                return          # only sampler 0 continues past the burst
            period = 0.0 if elapsed < BURST_SECONDS else (
                MID_PERIOD if elapsed < MID_SECONDS else TAIL_PERIOD)
            if period:
                time.sleep(period)

    with lock:
        for slot, (aid, name, cls) in enumerate(banners, 1):
            seen.setdefault(aid, {"advertiser": aid, "name": name, "class": cls,
                                  "t": 0.0, "slot_at_arrival": slot})
        samples.append({"t": 0.0, "order": [b[0] for b in banners]})
    threads = [threading.Thread(target=sampler, args=(i,), daemon=True)
               for i in range(STAGGER)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    samples.sort(key=lambda s: s["t"])
    final = samples[-1]["order"] if samples else []
    rec = {
        "post": pid, "full_page_wall": t0_wall,
        "publish_bracket_s": round(t0 - last_absent, 3) if last_absent else None,
        "bot_live_minus_full_s": round(t_bot_live - t0, 3) if t_bot_live is not None else None,
        "arrivals": sorted(seen.values(), key=lambda d: (d["t"], d["slot_at_arrival"])),
        "final_order": final,
        "final_slot_585": (final.index("585") + 1) if "585" in final else None,
        "prelive": prelive, "samples": samples,
    }
    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_fh.flush()
    arr = ", ".join(f"{a['name']}({a['advertiser']})@{a['t']:.3f}s"
                    for a in rec["arrivals"][:5])
    print(f"[post {pid}] bracket<={rec['publish_bracket_s']}s "
          f"bot_live-full={rec['bot_live_minus_full_s']}s "
          f"slot_585={rec['final_slot_585']} :: {arr}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, required=True)
    ap.add_argument("--minutes", type=float, default=200.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    conn = Conn()
    pid = args.start
    deadline = time.monotonic() + args.minutes * 60
    with open(args.out, "a", encoding="utf-8") as fh:
        while True:
            _n, _bl, full, _b = parse(*conn.get(f"/rq/{pid}"))
            if not full:
                break
            print(f"[skip] {pid} already live", flush=True)
            pid += 1
        print(f"[watch] waiting on post {pid}", flush=True)
        while time.monotonic() < deadline:
            watch_post(pid, fh)
            pid += 1
    print("[watch] done", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
