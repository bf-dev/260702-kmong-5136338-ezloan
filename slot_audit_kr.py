#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Achieved-slot audit of 더원대부 (585) vs 옥자대부 (544), read from the PAGE.

Runs ON a Korean host with direct egress (unicorn@external-2). Stdlib only, so there is
nothing to install. Every request is an anonymous GET of the public /rq/{id} page: no
login, no cookies, no write path. Safe to run next to the live registration loop.

Why not the app's own rank field: `ezloan_bot.company_rank()` used a regex that could not
match a paid `ad_sm` advertiser's `<div class="name">` (a `<span>` badge sits inside it),
so it dropped 옥자대부 from the list and logged `rank=1` on 43 posts where we were 2nd or
3rd. This reads the real DOM order of `<ul class="section_body loan_list recommend">`,
which is the on-screen order (verified against real pixels at mobile and desktop widths)
and therefore the registration order.

    python3 slot_audit_kr.py 31960 32022 --out audit.jsonl --upload

`--upload` posts the rendered summary plus the gzipped rows to the Artifacts API
(source `ezloan-race-slotaudit`, customer 5136338) so the dataset survives the host it was
collected on. The last sampler died with external-1 and took the only sample with it.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.request
import uuid

BASE = "https://ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

ME = "585"
RIVAL = "544"
CUSTOMER_ID = "5136338"
WORKS_API = "https://works.insu.ng/works/api"

_ITEM_RE = re.compile(r'<a href="/l/(\d+)" class="(item[^"]*)"[^>]*title="([^"]*)"', re.S)
_LIST_RE = re.compile(r'<ul class="section_body loan_list recommend">(.*?)</ul>', re.S)

POST_PAGE_MIN_BYTES = 20000
POST_PAGE_MARKERS = ("loan_list recommend", "rq_addbanner")


def banner_order(html: str):
    m = _LIST_RE.search(html)
    body = m.group(1) if m else html
    out = []
    for aid, cls, title in _ITEM_RE.findall(body):
        out.append((aid, title.split("-", 1)[0].strip(), cls.strip()))
    return out


def fetch(pid: int, timeout: float = 15.0):
    req = urllib.request.Request(
        "%s/rq/%d" % (BASE, pid),
        headers={"User-Agent": UA, "Referer": BASE + "/rq", "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
            status = resp.status
    except Exception as exc:  # noqa: BLE001
        return False, [], "err:%s" % type(exc).__name__
    if status != 200 or len(html) < POST_PAGE_MIN_BYTES:
        return False, [], status
    if not any(mk in html for mk in POST_PAGE_MARKERS):
        return False, [], status
    return True, banner_order(html), status


def upload(text, blob_name=None, blob=None, timeout=30):
    """POST to the Artifacts API. Never raises: a failed upload must not lose the run."""
    try:
        boundary = "----ezloanslot" + uuid.uuid4().hex
        body = b""

        def field(name, value):
            nonlocal body
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, name, value)).encode("utf-8")

        field("customerId", CUSTOMER_ID)
        field("source", "ezloan-race-slotaudit")
        field("text", text[:60000])
        if blob is not None:
            body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                     "filename=\"%s\"\r\nContent-Type: application/gzip\r\n\r\n"
                     % (boundary, blob_name)).encode("utf-8")
            body += blob + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(
            WORKS_API, data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                     "User-Agent": "ezloan-slot-audit/1.0 (+kmong %s)" % CUSTOMER_ID,
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--out")
    ap.add_argument("--gap", type=float, default=0.4,
                    help="seconds between requests; keep it polite, the loop is live")
    ap.add_argument("--upload", action="store_true",
                    help="post the rows + summary to the Artifacts API when the pass ends")
    args = ap.parse_args()

    fh = open(args.out, "w", encoding="utf-8") if args.out else None
    lines = []
    on = slot1 = head = wins = 0
    for pid in range(args.start, args.end + 1):
        exists, banners, status = fetch(pid)
        order = [b[0] for b in banners]
        slot_ours = order.index(ME) + 1 if ME in order else None
        slot_rival = order.index(RIVAL) + 1 if RIVAL in order else None
        row = {
            "post": pid, "exists": exists, "status": status,
            "order": order,
            "names": {b[0]: b[1] for b in banners},
            "slot_ours": slot_ours, "slot_rival": slot_rival,
            "field": len(order),
        }
        lines.append(json.dumps(row, ensure_ascii=False))
        if fh:
            fh.write(lines[-1] + "\n")
            fh.flush()
        if slot_ours:
            on += 1
            if slot_ours == 1:
                slot1 += 1
        if slot_ours and slot_rival:
            head += 1
            if slot_ours < slot_rival:
                wins += 1
        print("%5d exists=%s ours=%s rival=%s field=%d %s" % (
            pid, exists, slot_ours, slot_rival, len(order), ",".join(order)), flush=True)
        time.sleep(args.gap)
    summary = (
        "ezloan 달성 슬롯 감사 (customer %s / 더원대부 %s vs 옥자대부 %s)\n"
        "posts %d-%d, read anonymously off the live page from a KR host, %s\n"
        "posts we are on: %d   our slot 1: %d\n"
        "head to head vs %s: %d won / %d posts where both of us are on the list\n"
        "The slot is the DOM order of <ul class=\"section_body loan_list recommend\">, "
        "which is the on-screen order and therefore the registration order. It is NOT "
        "the app's rank= field, which dropped every paid ad_sm advertiser and logged "
        "rank=1 on 43 posts where we were 2nd or 3rd."
        % (CUSTOMER_ID, ME, RIVAL, args.start, args.end,
           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           on, slot1, RIVAL, wins, head))
    print("\n" + summary)
    if fh:
        fh.close()
    if args.upload:
        blob = gzip.compress("\n".join(lines).encode("utf-8"))
        res = upload(summary, "ezloan-slotaudit-%s-%d-%d.jsonl.gz"
                     % (CUSTOMER_ID, args.start, args.end), blob)
        print("[slotaudit] upload -> %s" % json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
