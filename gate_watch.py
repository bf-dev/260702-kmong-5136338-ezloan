"""Read-only gate watcher for ezloan.io /rq/{id} (customer 5136338, advertiser 585).

Answers, per new post, from an anonymous KR vantage point:
  t_alloc   first moment /rq/{id} stops being the ~353-byte "missing" stub
  markers   which of the app's post_live markers that first page carries, i.e. the
            moment the shipped v2.6.2 post_live() would have said True
  t_full    first moment the banner <ul class="loan_list recommend"> renders
  order     banner order at first render and over the following seconds
  srv_ts    the server's own unix second (<body data-cache="...">) on each phase

Anonymous GETs only. Never logs in, never writes, never spends 배너잔여.
Pacing: 1 connection every --idle-period while the id is missing, 2 connections every
--armed-period for at most --armed-max seconds once allocated, then 1/s up to --tail.

Usage (on a KR host, e.g. unicorn@external-8):
  python3 gate_watch.py --start 34152 --out ~/ezl263/gate.jsonl
"""

import argparse
import gzip
import http.client
import json
import os
import re
import ssl
import threading
import time

HOST = "ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
LIST_RE = re.compile(r'<ul class="section_body loan_list recommend">(.*?)</ul>', re.S)
ITEM_RE = re.compile(r'<a href="/l/(\d+)" class="(item[^"]*)"[^>]*title="([^"]*)"', re.S)
CACHE_RE = re.compile(r'data-cache="(\d+)"')
MARKERS = ("배너 등록을 눌러 주세요", "js-memberConfirmView", "rq_addbanner")


class Conn:
    def __init__(self):
        self.ctx = ssl.create_default_context()
        self.c = None

    def get(self, path):
        for attempt in (0, 1):
            try:
                if self.c is None:
                    self.c = http.client.HTTPSConnection(HOST, timeout=8, context=self.ctx)
                t = time.time()
                self.c.request("GET", path, headers={"User-Agent": UA, "Accept-Encoding": "gzip",
                                                     "Referer": "https://ezloan.io/rq"})
                r = self.c.getresponse()
                body = r.read()
                if r.getheader("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return r.status, body.decode("utf-8", "replace"), t, time.time()
            except Exception:
                try:
                    self.c.close()
                except Exception:
                    pass
                self.c = None
                if attempt:
                    return None, "", time.time(), time.time()
        return None, "", time.time(), time.time()


def classify(status, html):
    n = len(html)
    marks = [m for m in MARKERS if m in html]
    # exactly the shipped v2.6.2 post_live() rule
    live262 = bool(status == 200 and n >= 1000 and marks)
    m = LIST_RE.search(html)
    phase = "err" if status != 200 else ("miss" if n < 1000 else ("full" if m else "armed"))
    banners = []
    if m:
        banners = [a for a, _c, _t in ITEM_RE.findall(m.group(1))]
    cm = CACHE_RE.search(html)
    return {"n": n, "phase": phase, "live262": live262, "marks": marks,
            "banners": banners, "srv": int(cm.group(1)) if cm else None}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".%03dZ" % int((ts % 1) * 1000)


def watch(pid, a, log):
    path = "/rq/%d" % pid
    st = {"alloc": None, "live262": None, "full": None, "first_full_banners": None,
          "alloc_info": None, "last_miss": None, "trace": [], "stop": False,
          "first_seen": {}, "in_list": None}
    lock = threading.Lock()
    rq_link = re.compile(r'/rq/%d["\'/?#]' % pid)

    def lister():
        # 1 list fetch/s while armed: when does the id show up in /rq (the heavy-tick
        # safety-net path of the app)?
        c = Conn()
        while True:
            with lock:
                if st["stop"] or st["in_list"] is not None:
                    return
                alloc = st["alloc"]
            if alloc is None:
                time.sleep(0.2)
                continue
            if time.time() - alloc > a.armed_max:
                return
            status, html, t0, t1 = c.get("/rq")
            if status == 200 and rq_link.search(html):
                with lock:
                    st["in_list"] = (t0 + t1) / 2
                return
            time.sleep(max(0, 1.0 - (time.time() - t0)))

    def poller(idx):
        c = Conn()
        time.sleep(idx * 0.05)
        while True:
            with lock:
                if st["stop"]:
                    return
                alloc = st["alloc"]
            now = time.time()
            if alloc is None:
                if idx:
                    time.sleep(0.05)
                    continue
                period = a.idle_period
            else:
                age = now - alloc
                if age > a.tail:
                    with lock:
                        st["stop"] = True
                    return
                if age <= a.armed_max:
                    period = a.armed_period
                else:
                    if idx:
                        return
                    period = 1.0
            status, html, t0, t1 = c.get(path)
            info = classify(status, html)
            tm = (t0 + t1) / 2
            with lock:
                if info["phase"] == "miss":
                    st["last_miss"] = tm
                elif info["phase"] in ("armed", "full"):
                    if st["alloc"] is None:
                        st["alloc"] = tm
                        st["alloc_info"] = dict(info, banners=None)
                        log("[%d] ALLOC n=%d phase=%s marks=%s srv=%s" % (pid, info["n"], info["phase"],
                                                                        info["marks"], info["srv"]))
                    if info["live262"] and st["live262"] is None:
                        st["live262"] = tm
                    if info["phase"] == "full":
                        if st["full"] is None:
                            st["full"] = tm
                            st["first_full_banners"] = info["banners"]
                            log("[%d] FULL +%.3fs banners=%s" % (pid, tm - st["alloc"], info["banners"]))
                        for slot, adv in enumerate(info["banners"], 1):
                            if adv not in st["first_seen"]:
                                st["first_seen"][adv] = round(tm - st["alloc"], 3)
                    if len(st["trace"]) < 4000:
                        st["trace"].append({"t": round(tm - st["alloc"], 3), "phase": info["phase"],
                                            "n": info["n"], "srv": info["srv"],
                                            "order": info["banners"][:6]})
            sl = period - (time.time() - now)
            if sl > 0:
                time.sleep(sl)

    th = [threading.Thread(target=poller, args=(i,), daemon=True) for i in range(2)]
    th.append(threading.Thread(target=lister, daemon=True))
    for t in th:
        t.start()
    # skip detection: if pid+gap is already live while pid is still missing, move on
    last_skip_check = time.time()
    while any(t.is_alive() for t in th):
        time.sleep(0.5)
        with lock:
            allocd = st["alloc"] is not None
        if not allocd and time.time() - last_skip_check > 60:
            last_skip_check = time.time()
            s, h, _a, _b = Conn().get("/rq/%d" % (pid + 1))
            if classify(s, h)["phase"] in ("armed", "full"):
                log("[%d] skipped by ezloan (%d is live)" % (pid, pid + 1))
                with lock:
                    st["stop"] = True
                return None
    final = st["trace"][-1]["order"] if st["trace"] else []
    return {
        "post": pid,
        "alloc_utc": iso(st["alloc"]) if st["alloc"] else None,
        "alloc_bracket_s": round(st["alloc"] - st["last_miss"], 3) if st["alloc"] and st["last_miss"] else None,
        "alloc_phase": (st["alloc_info"] or {}).get("phase"),
        "alloc_bytes": (st["alloc_info"] or {}).get("n"),
        "alloc_marks": (st["alloc_info"] or {}).get("marks"),
        "alloc_srv": (st["alloc_info"] or {}).get("srv"),
        "live262_after_alloc_s": round(st["live262"] - st["alloc"], 3) if st["live262"] else None,
        "full_after_alloc_s": round(st["full"] - st["alloc"], 3) if st["full"] else None,
        "full_utc": iso(st["full"]) if st["full"] else None,
        "in_list_after_alloc_s": round(st["in_list"] - st["alloc"], 3) if st["in_list"] and st["alloc"] else None,
        "first_full_banners": st["first_full_banners"],
        "first_seen_s": st["first_seen"],
        "final_order": final,
        "trace": st["trace"][:400],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--out", default=os.path.expanduser("~/ezl263/gate.jsonl"))
    ap.add_argument("--idle-period", type=float, default=0.25)
    ap.add_argument("--armed-period", type=float, default=0.10)
    ap.add_argument("--armed-max", type=float, default=40.0)
    ap.add_argument("--tail", type=float, default=90.0)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    def log(msg):
        print(time.strftime("%H:%M:%S", time.gmtime()) + "Z " + msg, flush=True)

    pid = a.start
    for _ in range(a.count):
        log("[%d] waiting" % pid)
        row = watch(pid, a, log)
        if row:
            with open(a.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            log("[%d] DONE alloc=%s live262=+%s full=+%s first=%s final=%s" % (
                pid, row["alloc_utc"], row["live262_after_alloc_s"], row["full_after_alloc_s"],
                row["first_full_banners"], row["final_order"][:5]))
        pid += 1


if __name__ == "__main__":
    main()
