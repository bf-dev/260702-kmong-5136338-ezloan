"""Audit the ACHIEVED banner slot of 더원대부 (advertiser 585) across a band of posts.

Read-only, anonymous, through the Korean egress. Never logs in, never writes.

    python3 rank_audit.py 31940 32004 --out /path/audit.jsonl

Prints, per post, the full DOM order of the banner list and our slot, then a summary:
how many posts we are on, how many of those we hold slot 1, and who is above us when
we do not.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import rank_tools

ME = "585"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:1085")
    args = ap.parse_args()

    sessions = [rank_tools.probe_session(args.proxy) for _ in range(args.workers)]

    def job(i_pid):
        i, pid = i_pid
        s = sessions[i % args.workers]
        for _ in range(3):
            exists, banners, dt, status = rank_tools.fetch_post(s, pid, timeout=20)
            if exists or status == 200:
                return pid, exists, banners, status
        return pid, exists, banners, status

    pids = list(range(args.start, args.end + 1))
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(job, enumerate(pids)))
    rows.sort(key=lambda r: r[0])

    out_fh = open(args.out, "w", encoding="utf-8") if args.out else None
    on, slot1, missing, absent_page = 0, 0, [], []
    beaten_by = Counter()
    slots = Counter()
    for pid, exists, banners, status in rows:
        if not exists:
            absent_page.append(pid)
            continue
        order = [b[0] for b in banners]
        names = {b[0]: b[1] for b in banners}
        slot = order.index(ME) + 1 if ME in order else None
        if slot is None:
            missing.append(pid)
        else:
            on += 1
            slots[slot] += 1
            if slot == 1:
                slot1 += 1
            else:
                for aid in order[:slot - 1]:
                    beaten_by[f"{names.get(aid,'?')}({aid})"] += 1
        if out_fh:
            out_fh.write(json.dumps({"post": pid, "slot_585": slot,
                                     "order": order, "names": names},
                                    ensure_ascii=False) + "\n")
        print(f"{pid} slot={slot if slot else '-'} n={len(order)} :: "
              + " > ".join(f"{names[a]}({a})" for a in order[:5]))
    if out_fh:
        out_fh.close()

    print("\n=== summary ===")
    print(f"posts scanned      : {len(pids)}  (page missing/未生成: {len(absent_page)})")
    print(f"posts with our 585 : {on}")
    print(f"  slot 1 (1등)     : {slot1}  ({100*slot1/on:.1f}% of the posts we are on)"
          if on else "  slot 1: n/a")
    print(f"  slot distribution: {dict(sorted(slots.items()))}")
    print(f"posts WITHOUT 585  : {len(missing)}  {missing[:40]}")
    if beaten_by:
        print("who is above us when we are not slot 1:")
        for k, v in beaten_by.most_common(10):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    sys.exit(main() or 0)
