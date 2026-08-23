"""Competitor-timing sampler for ezloan.io 실시간 배너 (customer 5136338 / 더원대부, advertiser 585).

WHAT IT MEASURES, per newly published post:

  t0        the moment /rq/{id} first renders its banner <ul> (= registration opens)
  t(544)    when 옥자대부's banner appears, relative to t0
  t(585)    when OURS appears, relative to t0
  order     the resulting slot order, and our achieved slot

All timestamps are millisecond resolution, monotonic-clocked, anchored on t0.
Every post also carries `publish_bracket_s`: the gap between the last observation that
was NOT yet open and t0. That is the measurement error on every arrival time in that
row and it is reported, never hidden.

READ-ONLY. Anonymous GETs only. It never logs in, never writes, never spends 배너잔여.
It is completely independent of the live registration loop; nothing here can register.

WHERE IT RUNS: unicorn@external-2 (KR, 115.68.232.141), direct egress, no proxy.
Deliberately NOT the loop host (external-8) so a sampler burst cannot contend with the
loop's CPU or its socket budget. Never external-1: that host dropped off Tailscale on
2026-08-23 and took the previous sampler with it.

REQUEST PACING (this shares an origin with the customer's live loop, so it is metered):

  IDLE   post id not allocated yet, 353-byte miss    IDLE_THREADS / IDLE_PERIOD   ~7 req/s
  ARMED  id allocated, banner <ul> not there yet     ARMED_THREADS / ARMED_PERIOD ~60 req/s
  BURST  t0 .. BURST_SECONDS                         BURST_THREADS back-to-back   ~35 req/s
  MID    BURST_SECONDS .. MID_SECONDS                1 thread @ 0.5s              2 req/s
  TAIL   MID_SECONDS .. TAIL_SECONDS                 1 thread @ 3.0s              0.3 req/s

IDLE dominates wall-clock time (posts arrive every ~25-50 min); ARMED and BURST are
short bounded windows. Every phase is a CLI flag so the next session can throttle it
without editing code.

Results are uploaded to the Artifacts API after every post, so they survive this process,
this host, and the session that started it.

stdlib only: nothing has to be installed on the box.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import random
import re
import ssl
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

HOST = "ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

OURS = "585"            # 더원대부중개, customer 5136338
RIVAL = "544"           # 옥자대부, the only competitor in the millisecond game

_LIST_RE = re.compile(r'<ul class="section_body loan_list recommend">(.*?)</ul>', re.S)
_ITEM_RE = re.compile(r'<a href="/l/(\d+)" class="(item[^"]*)"[^>]*title="([^"]*)"', re.S)

MISS_MAX_BYTES = 1000       # a not-yet-allocated /rq/{id} is ~353 bytes
FULL_MIN_BYTES = 20000

WORKS_API = "https://works.insu.ng/works/api"
CUSTOMER_ID = "5136338"
SOURCE = "ezloan-race-sampler"


# --------------------------------------------------------------------------- http

class Conn:
    """One keep-alive HTTPS connection, reopened on any error."""

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

    def close(self):
        try:
            if self.c:
                self.c.close()
        except Exception:
            pass
        self.c = None


def parse(status, html):
    """-> (bytes, phase, banners). phase in {'miss','armed','full','err'}."""
    n = len(html)
    if status != 200:
        return n, "err", []
    if n < MISS_MAX_BYTES:
        return n, "miss", []
    m = _LIST_RE.search(html)
    if m is None or n < FULL_MIN_BYTES:
        return n, "armed", []
    banners = [(aid, title.split("-", 1)[0].strip(), cls.strip())
               for aid, cls, title in _ITEM_RE.findall(m.group(1))]
    return n, "full", banners


# ------------------------------------------------------------------- artifacts api

def _post_artifact(text, files=None, timeout=25):
    """Fire-and-forget upload. Returns the parsed 200 body or (None, reason). Never raises.

    NOTE: works.insu.ng sits behind Cloudflare, which 403s the default
    `Python-urllib/3.x` User-Agent. The browser UA below is load-bearing, do not drop it.
    """
    try:
        boundary = "----ezloanrace%s" % random.randint(10 ** 9, 10 ** 10)
        parts = []

        def field(name, value):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                          % (boundary, name)).encode())
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        field("customerId", CUSTOMER_ID)
        field("source", SOURCE)
        field("text", text)
        for fname, blob in (files or []):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                          "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                          % (boundary, fname)).encode())
            parts.append(blob)
            parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            WORKS_API, data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                     "Content-Length": str(len(body)),
                     "User-Agent": UA,
                     "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return None, "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return None, repr(e)[:200]


def upload_async(text, files=None, log=print):
    """Never blocks the sampler and never raises. Three tries, then give up: the data is
    already on disk and the next post re-uploads the whole summary anyway."""
    def run():
        for attempt in (1, 2, 3):
            res, why = _post_artifact(text, files)
            if res is not None:
                d = res.get("data") or {}
                log("[artifacts] id=%s matched=%s customer=%s"
                    % (d.get("id"), d.get("matched"), d.get("customerId")))
                return
            log("[artifacts] upload attempt %d failed: %s" % (attempt, why))
            time.sleep(3 * attempt)
        log("[artifacts] gave up for now (data is on disk, the next post re-uploads it)")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------- distribution

def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def distribution(rows):
    """Distribution over every summary row collected so far."""
    rival_t = [r["t_rival"] for r in rows if r.get("t_rival") is not None]
    ours_t = [r["t_ours"] for r in rows if r.get("t_ours") is not None]
    brackets = [r["publish_bracket_s"] for r in rows if r.get("publish_bracket_s") is not None]
    slots = {}
    for r in rows:
        s = r.get("slot_ours")
        slots[str(s)] = slots.get(str(s), 0) + 1
    return {
        "posts": len(rows),
        "rival_present": len(rival_t),
        "rival_ms": {
            "n": len(rival_t),
            "min": round(min(rival_t) * 1000) if rival_t else None,
            "p50": round(_pct(rival_t, 0.50) * 1000) if rival_t else None,
            "p90": round(_pct(rival_t, 0.90) * 1000) if rival_t else None,
            "max": round(max(rival_t) * 1000) if rival_t else None,
            "mean": round(statistics.fmean(rival_t) * 1000) if rival_t else None,
        },
        "ours_ms": {
            "n": len(ours_t),
            "min": round(min(ours_t) * 1000) if ours_t else None,
            "p50": round(_pct(ours_t, 0.50) * 1000) if ours_t else None,
            "p90": round(_pct(ours_t, 0.90) * 1000) if ours_t else None,
            "max": round(max(ours_t) * 1000) if ours_t else None,
        },
        "our_slot_histogram": slots,
        "our_slot1_posts": sum(1 for r in rows if r.get("slot_ours") == 1),
        "head_to_head": sum(1 for r in rows
                            if r.get("t_ours") is not None and r.get("t_rival") is not None),
        "head_to_head_wins": sum(1 for r in rows
                                 if r.get("t_ours") is not None
                                 and r.get("t_rival") is not None
                                 and r["t_ours"] < r["t_rival"]),
        "publish_bracket_ms": {
            "n": len(brackets),
            "p50": round(_pct(brackets, 0.50) * 1000) if brackets else None,
            "max": round(max(brackets) * 1000) if brackets else None,
        },
    }


def render_report(rows, dist):
    lines = []
    lines.append("ezloan 실시간 배너 경쟁 타이밍 샘플러 (customer %s / 더원대부 585)" % CUSTOMER_ID)
    lines.append("host unicorn@external-2 (KR 115.68.232.141), read-only anonymous GETs")
    lines.append("")
    lines.append("SAMPLE SIZE: n=%d posts observed from their publish moment" % dist["posts"])
    lines.append("             옥자대부(544) present on %d of them" % dist["rival_present"])
    r = dist["rival_ms"]
    lines.append("")
    lines.append("옥자대부(544) arrival after the banner list first renders, ms:")
    lines.append("  n=%s  min=%s  p50=%s  p90=%s  max=%s  mean=%s"
                 % (r["n"], r["min"], r["p50"], r["p90"], r["max"], r["mean"]))
    o = dist["ours_ms"]
    lines.append("우리(585) arrival, ms:")
    lines.append("  n=%s  min=%s  p50=%s  p90=%s  max=%s"
                 % (o["n"], o["min"], o["p50"], o["p90"], o["max"]))
    lines.append("우리 achieved slot histogram (slot->posts, null = not on the post): %s"
                 % json.dumps(dist["our_slot_histogram"], ensure_ascii=False))
    lines.append("우리 slot 1: %d post(s).  head-to-head vs 544: %d/%d won"
                 % (dist["our_slot1_posts"], dist["head_to_head_wins"], dist["head_to_head"]))
    b = dist["publish_bracket_ms"]
    lines.append("measurement error (publish bracket, ms): p50=%s max=%s  "
                 "-- every arrival above carries this" % (b["p50"], b["max"]))
    lines.append("")
    lines.append("per post (t=ms after the banner list first renders):")
    for row in rows[-25:]:
        arr = " ".join("%s(%s)@%dms" % (a["name"], a["advertiser"], round(a["t"] * 1000))
                       for a in row["arrivals"][:6])
        lines.append("  %s  bracket<=%sms slot_585=%s :: %s"
                     % (row["post"],
                        round((row.get("publish_bracket_s") or 0) * 1000),
                        row.get("slot_ours"), arr))
    return "\n".join(lines)


# ----------------------------------------------------------------------- sampling

class Sampler:
    def __init__(self, args):
        self.a = args
        self.detail_path = args.detail
        self.summary_path = args.summary
        self.log_lock = threading.Lock()

    def log(self, msg):
        with self.log_lock:
            print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
                  flush=True)

    # -- the pre-open wait -------------------------------------------------
    def wait_for_open(self, pid):
        """Poll /rq/{pid} until the banner <ul> renders.

        Returns ("ok", (state, prelive)) | ("skip", None) | ("retry", None).
        Escalates the poll rate once the post id is allocated."""
        path = "/rq/%d" % pid
        prelive = []
        stop = threading.Event()
        lock = threading.Lock()
        state = {"phase": "idle", "armed_at": None, "t0": None, "t0_wall": None,
                 "banners": None, "last_not_open": None}
        lookahead_deadline = [time.monotonic() + self.a.lookahead_seconds]

        def armed_now():
            at = state["armed_at"]
            if at is None:
                return False
            # runaway guard: an ezloan error page is also ">1000 bytes, no banner list",
            # so never let the escalated rate run for longer than this.
            return (time.monotonic() - at) <= self.a.armed_max_seconds

        def poller(idx):
            c = Conn()
            time.sleep(idx * 0.017)
            try:
                while not stop.is_set():
                    if idx >= self.a.idle_threads and not armed_now():
                        stop.wait(0.05)     # extra threads idle until the id is allocated
                        continue
                    t = time.monotonic()
                    n, phase, banners = parse(*c.get(path))
                    now = time.monotonic()
                    with lock:
                        if stop.is_set():
                            return
                        if len(prelive) < 4000:
                            prelive.append({"t": round(now, 4), "bytes": n, "phase": phase})
                        if phase == "full":
                            state["t0"] = now
                            state["t0_wall"] = time.time()
                            state["banners"] = banners
                            stop.set()
                            return
                        state["last_not_open"] = now
                        if phase == "armed" and state["armed_at"] is None:
                            state["armed_at"] = now
                            state["phase"] = "armed"
                            self.log("[post %d] ARMED: id allocated, %d bytes, banner list "
                                     "not there yet -> escalating to %dx%.2fs"
                                     % (pid, n, self.a.armed_threads, self.a.armed_period))
                    period = self.a.armed_period if armed_now() else self.a.idle_period
                    sleep = period - (time.monotonic() - t)
                    if sleep > 0:
                        stop.wait(sleep)
            except Exception:
                self.log("[post %d] poller %d died:\n%s" % (pid, idx, traceback.format_exc()))
            finally:
                c.close()

        threads = [threading.Thread(target=poller, args=(i,), daemon=True)
                   for i in range(max(self.a.idle_threads, self.a.armed_threads))]
        for th in threads:
            th.start()

        # supervisor: jump the frontier if ezloan skipped this id
        while not stop.wait(1.0):
            if not any(th.is_alive() for th in threads):
                self.log("[post %d] every poller died, restarting the wait" % pid)
                return "retry", None
            if time.monotonic() > lookahead_deadline[0]:
                lookahead_deadline[0] = time.monotonic() + self.a.lookahead_seconds
                if self.id_is_live(pid + self.a.lookahead_gap):
                    self.log("[post %d] SKIPPED: %d is already live, advancing frontier"
                             % (pid, pid + self.a.lookahead_gap))
                    stop.set()
                    for th in threads:
                        th.join(timeout=3)
                    return "skip", None
        for th in threads:
            th.join(timeout=3)
        return "ok", (state, prelive)

    def id_is_live(self, pid):
        c = Conn()
        try:
            _n, phase, _b = parse(*c.get("/rq/%d" % pid))
            return phase == "full"
        finally:
            c.close()

    # -- the burst ---------------------------------------------------------
    def sample_arrivals(self, pid, t0, first_banners):
        path = "/rq/%d" % pid
        lock = threading.Lock()
        seen, trace = {}, []
        stop = threading.Event()

        with lock:
            for slot, (aid, name, cls) in enumerate(first_banners, 1):
                seen[aid] = {"advertiser": aid, "name": name, "class": cls,
                             "t": 0.0, "slot_at_arrival": slot}
            trace.append({"t": 0.0, "order": [b[0] for b in first_banners]})

        def sampler(idx):
            c = Conn()
            time.sleep(idx * 0.017)
            try:
                while not stop.is_set():
                    t = time.monotonic()
                    elapsed = t - t0
                    if elapsed > self.a.tail_seconds:
                        return
                    _n, phase, banners = parse(*c.get(path))
                    el = round(time.monotonic() - t0, 3)
                    if phase == "full":
                        with lock:
                            for slot, (aid, name, cls) in enumerate(banners, 1):
                                if aid not in seen:
                                    seen[aid] = {"advertiser": aid, "name": name,
                                                 "class": cls, "t": el,
                                                 "slot_at_arrival": slot}
                                    self.log("[post %d]   +%s (%s) @ %.3fs slot%d"
                                             % (pid, name, aid, el, slot))
                            if len(trace) < 3000:
                                trace.append({"t": el, "order": [b[0] for b in banners]})
                    if elapsed >= self.a.burst_seconds and idx:
                        return              # only sampler 0 continues past the burst
                    if elapsed < self.a.burst_seconds:
                        period = 0.0
                    elif elapsed < self.a.mid_seconds:
                        period = self.a.mid_period
                    else:
                        period = self.a.tail_period
                    if period:
                        sleep = period - (time.monotonic() - t)
                        if sleep > 0:
                            stop.wait(sleep)
            finally:
                c.close()

        threads = [threading.Thread(target=sampler, args=(i,), daemon=True)
                   for i in range(self.a.burst_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        trace.sort(key=lambda s: s["t"])
        return seen, trace

    # -- one post ----------------------------------------------------------
    def watch_post(self, pid):
        self.log("[post %d] waiting for the banner list to render" % pid)
        status, payload = self.wait_for_open(pid)
        if status != "ok":
            return status
        state, prelive = payload
        t0, t0_wall = state["t0"], state["t0_wall"]
        bracket = (round(t0 - state["last_not_open"], 3)
                   if state["last_not_open"] is not None else None)
        armed_lead = (round(t0 - state["armed_at"], 3)
                      if state["armed_at"] is not None else None)
        self.log("[post %d] OPEN at %s  bracket<=%sms  armed_lead=%ss  first_banners=%d"
                 % (pid, time.strftime("%H:%M:%S", time.gmtime(t0_wall)),
                    round((bracket or 0) * 1000), armed_lead, len(state["banners"])))

        seen, trace = self.sample_arrivals(pid, t0, state["banners"])
        final = trace[-1]["order"] if trace else []
        arrivals = sorted(seen.values(), key=lambda d: (d["t"], d["slot_at_arrival"]))

        summary = {
            "post": pid,
            "open_wall_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0_wall))
                             + ".%03dZ" % int((t0_wall % 1) * 1000),
            "publish_bracket_s": bracket,
            "armed_lead_s": armed_lead,
            "banners_at_open": [b[0] for b in state["banners"]],
            "arrivals": arrivals,
            "t_rival": seen.get(RIVAL, {}).get("t") if RIVAL in seen else None,
            "t_ours": seen.get(OURS, {}).get("t") if OURS in seen else None,
            "final_order": final,
            "slot_ours": (final.index(OURS) + 1) if OURS in final else None,
            "slot_rival": (final.index(RIVAL) + 1) if RIVAL in final else None,
            "sampler_host": self.a.host_label,
        }
        detail = dict(summary)
        detail["prelive"] = prelive[-400:]
        detail["trace"] = trace

        _append(self.summary_path, summary)
        _append(self.detail_path, detail, cap_mb=self.a.detail_cap_mb)
        self.log("[post %d] DONE  t_544=%s  t_585=%s  slot_585=%s  order=%s"
                 % (pid, summary["t_rival"], summary["t_ours"], summary["slot_ours"],
                    ",".join(final)))
        self.report()
        return "ok"

    # -- reporting ---------------------------------------------------------
    def report(self):
        rows = _read_rows(self.summary_path)
        dist = distribution(rows)
        text = render_report(rows, dist)
        blob = gzip.compress(open(self.summary_path, "rb").read())
        files = [("ezloan-race-%s-summary.jsonl.gz" % CUSTOMER_ID, blob)] \
            if len(blob) < 3_000_000 else []
        upload_async(text, files, log=self.log)
        try:
            with open(self.a.report_path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass

    # -- main loop ---------------------------------------------------------
    def run(self):
        pid = self.a.start or self.discover_frontier(self.a.scan_from)
        self.log("[sampler] customer=%s ours=%s rival=%s frontier=%d host=%s"
                 % (CUSTOMER_ID, OURS, RIVAL, pid, self.a.host_label))
        self.log("[sampler] pacing idle=%dx%.2fs armed=%dx%.2fs burst=%dx%.1fs "
                 "mid=%.1fs tail=%.1fs"
                 % (self.a.idle_threads, self.a.idle_period, self.a.armed_threads,
                    self.a.armed_period, self.a.burst_threads, self.a.burst_seconds,
                    self.a.mid_seconds, self.a.tail_seconds))
        self.report()
        while True:
            try:
                outcome = self.watch_post(pid)
                if outcome == "retry":
                    time.sleep(2)
                    continue
                pid += 1
                _write_state(self.a.state, pid)
            except KeyboardInterrupt:
                raise
            except Exception:
                self.log("[sampler] post %d failed:\n%s" % (pid, traceback.format_exc()))
                time.sleep(5)

    def discover_frontier(self, start):
        st = _read_state(self.a.state)
        pid = st if st and st >= start else start
        self.log("[sampler] scanning upward from %d for the first unpublished id" % pid)
        while self.id_is_live(pid):
            self.log("[sampler] %d already live, skip" % pid)
            pid += 1
            time.sleep(0.2)
        return pid


# ------------------------------------------------------------------------- files

_file_lock = threading.Lock()


def _append(path, obj, cap_mb=None):
    with _file_lock:
        try:
            if cap_mb and os.path.exists(path) and os.path.getsize(path) > cap_mb * 1024 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            traceback.print_exc()


def _read_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh)["next_post"])
    except Exception:
        return None


def _write_state(path, pid):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"next_post": pid,
                       "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh)
    except Exception:
        pass


# -------------------------------------------------------------------------- main

def build_argparser():
    ap = argparse.ArgumentParser()
    home = os.path.expanduser("~/ezloan-sampler")
    ap.add_argument("--start", type=int, default=None,
                    help="first post id to watch (default: resume state, else scan)")
    ap.add_argument("--scan-from", type=int, default=32016)
    ap.add_argument("--summary", default=os.path.join(home, "race_summary.jsonl"))
    ap.add_argument("--detail", default=os.path.join(home, "race_detail.jsonl"))
    ap.add_argument("--state", default=os.path.join(home, "state.json"))
    ap.add_argument("--report-path", default=os.path.join(home, "report.txt"))
    ap.add_argument("--host-label", default=os.uname().nodename)
    ap.add_argument("--detail-cap-mb", type=float, default=64.0)
    # pacing
    ap.add_argument("--idle-threads", type=int, default=2)
    ap.add_argument("--idle-period", type=float, default=0.30)
    ap.add_argument("--armed-threads", type=int, default=3)
    ap.add_argument("--armed-period", type=float, default=0.05)
    ap.add_argument("--armed-max-seconds", type=float, default=120.0)
    ap.add_argument("--burst-threads", type=int, default=3)
    ap.add_argument("--burst-seconds", type=float, default=3.0)
    ap.add_argument("--mid-period", type=float, default=0.5)
    ap.add_argument("--mid-seconds", type=float, default=30.0)
    ap.add_argument("--tail-period", type=float, default=3.0)
    ap.add_argument("--tail-seconds", type=float, default=180.0)
    ap.add_argument("--lookahead-seconds", type=float, default=60.0)
    ap.add_argument("--lookahead-gap", type=int, default=2)
    ap.add_argument("--report-only", action="store_true",
                    help="print the distribution from the existing summary and exit")
    return ap


def main():
    args = build_argparser().parse_args()
    os.makedirs(os.path.dirname(args.summary), exist_ok=True)
    if args.report_only:
        rows = _read_rows(args.summary)
        print(render_report(rows, distribution(rows)))
        return 0
    Sampler(args).run()


if __name__ == "__main__":
    sys.exit(main() or 0)
