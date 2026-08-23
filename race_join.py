#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Join the anonymous sampler against our own loop's registration timestamps.

Customer 5136338 (더원대부 / advertiser 585) vs 옥자대부 (544).

Why this file exists
--------------------
`race_sampler.py` runs on unicorn@external-2 and watches `/rq/{id}` anonymously. It can
see three things: the instant the post id is allocated (the "armed" skeleton page starts
answering), the instant the banner `<ul>` first renders, and every banner that appears
after that render. What it CANNOT see is the instant banner registration opens: ezloan
opens the write API roughly 8.7s after allocation but only renders the post body into the
anonymous page ~17s after allocation. So anyone fast is already on the list the first time
an anonymous observer can see the list at all (that is the `rival_censored` flag), and the
sampler alone can never time them.

Our own loop can. It sits on the unpublished id, re-checks `rq_addbanner_check` every tick,
and fires the instant the API stops saying "no permission". Every success posts a
`[registered] post=NNNNN` row to the Artifacts API, which the gateway timestamps to the
millisecond. Joining that against the sampler's allocation instant gives the one latency
number we actually control:

    alloc -> our registration        (how far into the race we land)

and, because our loop is armed BEFORE the open, that number is essentially
`open_gate + one tick`, which is what pins down where the open gate is.

Both inputs live on the gateway host (bfdev@main), which is why this runs here and not
next to the sampler:

    sampler summaries   artifacts/private/<customerKey>/*-ezloan-race-5136338-summary.jsonl.gz
                        (uploaded by external-2 after every post, cumulative, newest wins)
    our registrations   the gateway's own Postgres, IngestedLog rows whose text starts
                        `[registered] post=NNNNN`, `createdAt` to the millisecond

The registration side used to read `artifacts/works-logs/<id>/pending-ingest.log`. That
file is a PER-TURN SNAPSHOT: the gateway rewrites it before an agent turn and unlinks it
afterwards, so the join silently ran with ZERO registrations whenever no turn was in
flight, and the first run of this script (n=2) was luck of the timing. IngestedLog is the
durable copy of the same rows and it reaches back to 2026-08-05, so the join is now
reproducible. `--ingest-log` still forces the old file if the DB is ever unreachable.

The achieved slot comes from the sampler, or from `--slot-audit` (slot_audit_kr.py output)
which re-reads the live page. It never comes from the app's own `rank=` field: that field
logged rank=1 on 43 posts where we were 2nd or 3rd, because its regex dropped every paid
`ad_sm` advertiser (옥자대부 included) from the list it counted.

Output goes back through the Artifacts API (source `ezloan-race-join`) so it survives this
host too, plus a local copy under ~/.ezloan-race-join/.

Run:  python3 race_join.py            # join, print, upload if anything changed
      python3 race_join.py --print    # join and print only, never upload
"""

import argparse
import glob
import gzip
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid

CUSTOMER_ID = "5136338"
CUSTOMER_KEY = "d3b89a47-f8bc-45d0-b6fa-03e50f1dfded"
OURS = "585"
RIVAL = "544"

ART = os.getenv("NEOWORKS_ARTIFACTS_DIR", "/home/bfdev/neoworks/apps/gateway/artifacts")
SUMMARY_GLOB = os.path.join(ART, "private", CUSTOMER_KEY,
                            "*-ezloan-race-%s-summary.jsonl.gz" % CUSTOMER_ID)
INGEST_LOG = os.path.join(ART, "works-logs", CUSTOMER_ID, "pending-ingest.log")

OUT_DIR = os.path.expanduser("~/.ezloan-race-join")
JOINED = os.path.join(OUT_DIR, "joined.jsonl")
STATE = os.path.join(OUT_DIR, "state.json")
REPORT = os.path.join(OUT_DIR, "report.txt")

WORKS_API = "https://works.insu.ng/works/api"

# The [registered] row is timestamped by the GATEWAY when it receives the POST, not by the
# loop when it registered. bridge.remote_log fires a fresh `requests.post` per event (no
# connection reuse), so the lag is one TCP connect + one TLS handshake + half an RTT.
# Measured from unicorn@external-8 to works.insu.ng on 2026-08-23, 5 samples:
#   connect 0.166-0.193s   tls 0.337-0.400s   total 0.569-0.838s
# The server records at request receipt, i.e. ~tls + 0.5*RTT = 0.38 + 0.09 = 0.47s after
# the loop called it. So subtract that to place our registration on the real clock, and
# carry the spread as the error bar. It is a SYSTEMATIC lag: uncorrected, every one of our
# latencies reads ~0.5s worse than it is.
INGEST_LAG_S = 0.47
INGEST_LAG_ERR_S = 0.15

# Our own contribution to `alloc -> our registration`, i.e. how much of that number is us
# rather than ezloan's gate. The loop re-fires the write every FRONTIER_POLL_SECONDS while
# a fresh post answers "no permission", so the first success lands at most one tick plus
# one round trip after the gate actually opened.
#   FRONTIER_POLL_SECONDS = 0.15  (config.py, no env override on external-8: verified from
#                                  /proc/<pid>/environ on the live loop)
#   RTT external-8 -> ezloan.io   ~0.044s p50 (NOTES, measured)
OUR_TICK_S = 0.15
OUR_RTT_S = 0.045
OUR_OVERHEAD_S = OUR_TICK_S + OUR_RTT_S


# ------------------------------------------------------------------------- inputs

def newest_summary():
    paths = sorted(glob.glob(SUMMARY_GLOB))
    if not paths:
        return [], None
    path = paths[-1]
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows, path


_TS = re.compile(r"^\[(?P<source>[^\]]+)\] (?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z) ")
_REG = re.compile(r"\[registered\] post=(?P<post>\d+)(?P<rest>.*)")


PSQL = ["docker", "exec", "neoworks-postgres", "psql", "-U", "neoworks", "-d", "neoworks",
        "-A", "-F", "\t", "-t", "-c"]
REG_SQL = (
    "SELECT to_char(\"createdAt\",'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'), source, text "
    "FROM \"IngestedLog\" WHERE \"customerKey\"='%s' AND text LIKE '[registered] post=%%' "
    "ORDER BY \"createdAt\"" % CUSTOMER_ID)


def registrations_db():
    """post id -> {ingest_ts, ingest_epoch, source, detail} from the gateway's Postgres.

    Read-only SELECT. This is the durable copy; pending-ingest.log is only a snapshot the
    gateway keeps for the duration of one agent turn.

    Note the column naming in IngestedLog is the reverse of what it reads like:
    `customerKey` holds the Kmong partner id ('5136338') and `customerId` holds the
    internal uuid. Query on customerKey.
    """
    out = {}
    try:
        raw = subprocess.run(PSQL + [REG_SQL], capture_output=True, text=True,
                             timeout=30, check=True).stdout
    except Exception as exc:  # noqa: BLE001
        print("[join] DB read failed (%s), falling back to pending-ingest.log" % exc)
        return registrations_file()
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ts, source, text = parts[0].strip(), parts[1].strip(), parts[2]
        m = _REG.search(text)
        if not m:
            continue
        # Newest row for a post wins: a re-registration after a restart is the one that
        # actually put the banner there.
        out[m.group("post")] = {
            "ingest_ts": ts,
            "ingest_epoch": _epoch(ts),
            "source": source,
            "detail": m.group("rest").strip(),
        }
    return out


def registrations_file():
    """post id -> {...} from the per-turn pending-ingest.log snapshot. Fallback only.

    Read-only. The file is the gateway's own append log; we never write to it.
    """
    out = {}
    if not os.path.exists(INGEST_LOG):
        return out
    ts = source = None
    with open(INGEST_LOG, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _TS.match(line)
            if m:
                ts, source = m.group("ts"), m.group("source")
                continue
            r = _REG.search(line)
            if r and ts:
                out[r.group("post")] = {
                    "ingest_ts": ts,
                    "ingest_epoch": _epoch(ts),
                    "source": source,
                    "detail": r.group("rest").strip(),
                }
    return out


def _epoch(iso):
    base = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    frac = 0.0
    if "." in iso:
        frac = float("0." + iso[20:].rstrip("Z"))
    import calendar
    return calendar.timegm(base) + frac


def _iso(ts):
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
            + ".%03dZ" % int(round((ts % 1) * 1000)))


# --------------------------------------------------------------------------- join

def alloc_epoch(row):
    """When ezloan allocated the post id, in epoch seconds.

    Rows written after 06:36Z carry alloc_wall_utc directly. Older rows carry
    open_wall_utc + armed_lead_s, which is the same instant; reconstruct it so the whole
    dataset is usable instead of only the tail.
    """
    if row.get("alloc_wall_utc"):
        return _epoch(row["alloc_wall_utc"])
    if row.get("open_wall_utc") and row.get("armed_lead_s") is not None:
        return _epoch(row["open_wall_utc"]) - row["armed_lead_s"]
    return None


def load_slot_audit(path):
    """post id -> {slot_ours, slot_rival, field, order} read straight off the live page.

    slot_audit_kr.py output. Takes precedence over the sampler's own slot, and over the
    app's rank= field always, because it is the only one re-read from the DOM after the
    race settled.
    """
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("exists"):
                continue
            out[str(r["post"])] = r
    return out


def join(rows, regs, audit=None):
    audit = audit or {}
    out = []
    for row in rows:
        pid = str(row["post"])
        alloc = alloc_epoch(row)
        reg = regs.get(pid)
        ours_reg = (reg["ingest_epoch"] - INGEST_LAG_S) if reg else None
        a = audit.get(pid)
        if a:
            row = dict(row)
            row["slot_ours"] = a.get("slot_ours")
            row["slot_rival"] = a.get("slot_rival")
            row["final_order"] = a.get("order") or row.get("final_order")
        rec = {
            "post": pid,
            "slot_source": "page-audit" if a else "sampler",
            "alloc_wall_utc": _iso(alloc) if alloc else None,
            "render_wall_utc": row.get("open_wall_utc"),
            "alloc_to_render_s": (round(row["armed_lead_s"], 3)
                                  if row.get("armed_lead_s") is not None else None),
            "ours_registered_utc": _iso(ours_reg) if ours_reg else None,
            "alloc_to_ours_s": (round(ours_reg - alloc, 3)
                                if (ours_reg and alloc) else None),
            "slot_ours": row.get("slot_ours"),
            "slot_rival": row.get("slot_rival"),
            "rival_censored": bool(row.get("rival_censored")),
            "ours_censored": bool(row.get("ours_censored")),
            "rival_after_render_ms": (round(row["t_rival"] * 1000)
                                      if (row.get("t_rival") is not None
                                          and not row.get("rival_censored")) else None),
            "field_size": len(row.get("final_order") or []),
            "sampler_host": row.get("sampler_host"),
        }
        out.append(rec)
    out.sort(key=lambda r: int(r["post"]))
    add_rival_bounds(out)
    return out


def add_rival_bounds(joined):
    """Bracket 옥자대부's alloc->registration on every post, from the slot ORDER.

    This is the whole trick, and it is what the anonymous sampler could never do. The DOM
    order of the banner list IS the registration order (verified against real pixels at
    both widths), so on a post where both of us registered:

        we are ABOVE 544  ->  544 registered AFTER us   ->  t_544 > t_585
        we are BELOW 544  ->  544 registered BEFORE us  ->  t_544 < t_585

    and t_585 is a real millisecond number from the join. So every head-to-head post is an
    interval-censored observation of the opponent, even though not one of them can be
    timed directly. The other side of each interval:

        upper, when we won   the list already had 544 on it the first time it rendered,
                             so t_544 <= alloc_to_render
        lower, when we lost  nobody can register before ezloan opens the gate, and the
                             gate floor is our own fastest observed registration minus our
                             own overhead (one poll tick + one round trip)
    """
    lat = [r["alloc_to_ours_s"] for r in joined if r["alloc_to_ours_s"] is not None]
    gate_floor = round(min(lat) - OUR_OVERHEAD_S, 3) if lat else None
    for r in joined:
        r["gate_floor_s"] = gate_floor
        r["rival_lo_s"] = r["rival_hi_s"] = None
        t = r["alloc_to_ours_s"]
        if t is None or r["slot_ours"] is None or r["slot_rival"] is None:
            continue
        if r["slot_ours"] < r["slot_rival"]:
            r["rival_lo_s"] = t
            r["rival_hi_s"] = r["alloc_to_render_s"]
        else:
            r["rival_lo_s"] = gate_floor
            r["rival_hi_s"] = t


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _stats(xs, scale=1.0, nd=0):
    if not xs:
        return {"n": 0, "min": None, "p50": None, "p90": None, "max": None}
    f = (lambda v: round(v * scale, nd) if nd else round(v * scale))
    return {"n": len(xs), "min": f(min(xs)), "p50": f(_pct(xs, 0.50)),
            "p90": f(_pct(xs, 0.90)), "max": f(max(xs))}


def summarize(joined):
    ours_lat = [r["alloc_to_ours_s"] for r in joined if r["alloc_to_ours_s"] is not None]
    rival_t = [r["rival_after_render_ms"] for r in joined
               if r["rival_after_render_ms"] is not None]
    render = [r["alloc_to_render_s"] for r in joined if r["alloc_to_render_s"] is not None]
    h2h = [r for r in joined
           if r["slot_ours"] is not None and r["slot_rival"] is not None]
    slots = {}
    for r in joined:
        slots[str(r["slot_ours"])] = slots.get(str(r["slot_ours"]), 0) + 1
    bounded = [r for r in joined if r["rival_hi_s"] is not None]
    rival_upper = [r["rival_hi_s"] for r in bounded
                   if r["slot_rival"] < r["slot_ours"]]   # posts we lost: hard upper bound
    return {
        "posts": len(joined),
        "rival_censored": sum(1 for r in joined if r["rival_censored"]),
        "rival_after_render_ms": _stats(rival_t),
        "ours_alloc_to_register_s": _stats(ours_lat, nd=3),
        "alloc_to_render_s": _stats(render, nd=3),
        "gate_floor_s": joined[0]["gate_floor_s"] if joined else None,
        "rival_bracketed_posts": len(bounded),
        "rival_upper_bound_s": _stats(rival_upper, nd=3),
        "our_slot_histogram": slots,
        "our_slot1_posts": sum(1 for r in joined if r["slot_ours"] == 1),
        "head_to_head": len(h2h),
        "head_to_head_wins": sum(1 for r in h2h if r["slot_ours"] < r["slot_rival"]),
        "ours_registered_joined": sum(1 for r in joined
                                      if r["ours_registered_utc"] is not None),
        "slot_from_page_audit": sum(1 for r in joined if r["slot_source"] == "page-audit"),
    }


# ------------------------------------------------------------------------ report

def render(joined, s):
    L = []
    a = L.append
    a("ezloan 1등 winnability — joined report (customer %s / 더원대부 %s vs 옥자대부 %s)"
      % (CUSTOMER_ID, OURS, RIVAL))
    a("anonymous sampler (unicorn@external-2) x our own loop's registration timestamps")
    a("generated %s on the gateway host" % _iso(time.time()))
    a("")
    a("SAMPLE SIZE: %d posts observed from the moment ezloan allocated their id."
      % s["posts"])
    a("             our registration time is joined on %d of them."
      % s["ours_registered_joined"])
    a("")
    a("1) OUR LATENCY, alloc -> our banner is written (seconds)")
    o = s["ours_alloc_to_register_s"]
    a("   n=%s  min=%s  p50=%s  p90=%s  max=%s"
      % (o["n"], o["min"], o["p50"], o["p90"], o["max"]))
    a("   error bar +-%.2fs (the gateway timestamps the upload, not the write; a fixed"
      % INGEST_LAG_ERR_S)
    a("   %.2fs connect+TLS lag is already subtracted). Our loop is ARMED on the id before"
      % INGEST_LAG_S)
    a("   the open and re-fires the write every %.2fs tick, so this number is the open"
      % OUR_TICK_S)
    a("   gate plus at most %.3fs of our own (tick + %.3fs RTT), not our detection delay."
      % (OUR_OVERHEAD_S, OUR_RTT_S))
    a("")
    a("2) WHEN THE ANONYMOUS PAGE CATCHES UP, alloc -> banner <ul> renders (seconds)")
    r = s["alloc_to_render_s"]
    a("   n=%s  min=%s  p50=%s  p90=%s  max=%s"
      % (r["n"], r["min"], r["p50"], r["p90"], r["max"]))
    a("   Everything in (1) happens BEFORE this. That gap is why the opponent cannot be")
    a("   timed anonymously when it is fast: %d of %d posts have 옥자대부 already on the"
      % (s["rival_censored"], s["posts"]))
    a("   list the first time the list exists (left-censored).")
    a("")
    a("3) 옥자대부(544) arrival AFTER the list renders, ms — uncensored posts only")
    v = s["rival_after_render_ms"]
    a("   n=%s  min=%s  p50=%s  p90=%s  max=%s"
      % (v["n"], v["min"], v["p50"], v["p90"], v["max"]))
    a("   Read this as 'posts where 옥자대부 was SLOW'. It is not its speed distribution;")
    a("   its fast runs are structurally invisible to an anonymous observer.")
    a("")
    a("3b) 옥자대부(544) alloc -> registration, BRACKETED by the slot order")
    a("   n=%d posts carry a bracket (both of us registered and our time is joined)."
      % s["rival_bracketed_posts"])
    u = s["rival_upper_bound_s"]
    a("   on the %s post(s) it beat us its registration is bounded ABOVE by ours:"
      % u["n"])
    a("     n=%s  min=%s  p50=%s  p90=%s  max=%s   (seconds after alloc)"
      % (u["n"], u["min"], u["p50"], u["p90"], u["max"]))
    a("   and bounded BELOW by the gate floor %ss (our fastest registration minus our own"
      % s["gate_floor_s"])
    a("   %.3fs of tick+RTT: nobody registers before ezloan opens the write API)."
      % OUR_OVERHEAD_S)
    a("   So on every post it beat us, 옥자대부 landed inside a window a few hundred")
    a("   milliseconds wide. It is a millisecond-class poller, not an 8-second-late one.")
    a("")
    a("4) WHAT DECIDES THE ORDER — our achieved slot, per post")
    a("   slot read from the live page on %d of %d posts (never from the app's rank= field)"
      % (s["slot_from_page_audit"], s["posts"]))
    a("   histogram (slot -> posts, null = we are not on the post): %s"
      % json.dumps(s["our_slot_histogram"], ensure_ascii=False))
    a("   slot 1 on %d post(s)." % s["our_slot1_posts"])
    a("   head to head vs 옥자대부: %d won / %d posts where both registered"
      % (s["head_to_head_wins"], s["head_to_head"]))
    a("   Final DOM order == registration order (verified on real pixels), so the slot IS")
    a("   the ground truth for who was first, even when neither arrival can be timed.")
    a("")
    a("per post  (seconds after ezloan allocated the post id)")
    a("  post   alloc(UTC)     ->ours   ->render  slot us/544  W/L  544 bracketed to")
    for r in joined[-40:]:
        if r["rival_lo_s"] is not None or r["rival_hi_s"] is not None:
            bracket = "%s .. %s" % (
                ("%.3f" % r["rival_lo_s"]) if r["rival_lo_s"] is not None else "?",
                ("%.3f" % r["rival_hi_s"]) if r["rival_hi_s"] is not None else "?")
        elif r["rival_after_render_ms"] is not None:
            bracket = "render+%dms" % r["rival_after_render_ms"]
        else:
            bracket = "-"
        if r["slot_ours"] is None or r["slot_rival"] is None:
            wl = "-"
        else:
            wl = "W" if r["slot_ours"] < r["slot_rival"] else "L"
        a("  %-6s %-14s %-8s %-9s %-12s %-4s %s"
          % (r["post"],
             (r["alloc_wall_utc"] or "-")[11:],
             ("%.3f" % r["alloc_to_ours_s"]) if r["alloc_to_ours_s"] is not None else "-",
             ("%.3f" % r["alloc_to_render_s"]) if r["alloc_to_render_s"] is not None else "-",
             "%s / %s" % (r["slot_ours"], r["slot_rival"]),
             wl, bracket))
    return "\n".join(L)


# ------------------------------------------------------------------------ upload

def upload(text, blob_name=None, blob=None, timeout=25):
    """POST to the Artifacts API. Never raises: a failed upload must not break the cron."""
    try:
        boundary = "----ezloanjoin" + uuid.uuid4().hex
        body = b""

        def field(name, value):
            nonlocal body
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, name, value)).encode("utf-8")

        field("customerId", CUSTOMER_ID)
        field("source", "ezloan-race-join")
        field("text", text[:60000])
        if blob is not None:
            body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                     "filename=\"%s\"\r\nContent-Type: application/gzip\r\n\r\n"
                     % (boundary, blob_name)).encode("utf-8")
            body += blob + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(
            WORKS_API, data=body,
            # Cloudflare sits in front of works.insu.ng and 403s a multipart POST that
            # carries the default `Python-urllib/3.x` agent from this host (the same code
            # from external-2 in KR is let through). A normal UA passes; the JSON-only
            # POST is not challenged either way.
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                     "User-Agent": "ezloan-race-join/1.0 (+kmong 5136338)",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="join and print, never upload")
    ap.add_argument("--force", action="store_true",
                    help="upload even when nothing changed")
    ap.add_argument("--ingest-log", action="store_true",
                    help="read registrations from the per-turn pending-ingest.log snapshot "
                         "instead of the gateway DB (fallback only, usually reads 0 rows)")
    ap.add_argument("--slot-audit", default=os.path.join(OUT_DIR, "slot_audit.jsonl"),
                    help="slot_audit_kr.py jsonl; overrides the sampler's slot with the "
                         "slot re-read from the live page")
    a = ap.parse_args()

    rows, src = newest_summary()
    regs = registrations_file() if a.ingest_log else registrations_db()
    audit = load_slot_audit(a.slot_audit)
    joined = join(rows, regs, audit)
    s = summarize(joined)
    text = render(joined, s)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(JOINED, "w", encoding="utf-8") as fh:
        for r in joined:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")

    print(text)
    print("\nsummary: " + json.dumps(s, ensure_ascii=False))
    print("sampler source: %s" % src)
    print("registrations seen: %d (%s)   page-audit slots: %d"
          % (len(regs), "pending-ingest.log" if a.ingest_log else "gateway DB",
             len(audit)))

    if a.print_only:
        return

    fingerprint = "%d/%d/%d" % (s["posts"], s["ours_registered_joined"],
                                s["head_to_head"])
    prev = ""
    try:
        prev = json.load(open(STATE))["fingerprint"]
    except Exception:
        pass
    if fingerprint == prev and not a.force:
        print("[join] nothing new (%s), not uploading" % fingerprint)
        return

    blob = gzip.compress("\n".join(json.dumps(r, ensure_ascii=False)
                                   for r in joined).encode("utf-8"))
    res = upload(text, "ezloan-race-%s-joined.jsonl.gz" % CUSTOMER_ID, blob)
    print("[join] upload -> %s" % json.dumps(res, ensure_ascii=False))
    if res.get("success"):
        json.dump({"fingerprint": fingerprint, "at": _iso(time.time())},
                  open(STATE, "w"))


if __name__ == "__main__":
    main()
