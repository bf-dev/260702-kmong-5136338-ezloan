"""Read-only detect-to-register benchmark against live ezloan.io (customer 5136338).

Anonymous only: the "register" leg is GET /api/rq_addbanner/{id} without a session, which
ezloan refuses with "no permission" (nothing registered, nothing spent). It has the same
route/RTT as the real write. Compares:
  v262: tick wait U(0,0.15) + parallel[post_live(frontier), post_live(frontier+1), check] -> add
  v263: tick wait U(0,0.075) + post_live(frontier) -> add (check moved to the 1s heavy tick)
  v263-armed: post already detected, gate polled with add every 0.1s -> gate-to-write U(0,0.1)+add
Prints p50/p90 ms of (detect-start -> add response) including the simulated tick wait.
Usage on a KR host: python3 hotpath_bench.py <live_post_id> [n]
"""
import random, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import DefaultCookiePolicy
import requests

B = "https://ezloan.io"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
LIVE = int(sys.argv[1]); N = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def sess():
    s = requests.Session(); s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    s.headers.update({"User-Agent": UA, "Referer": B + "/rq", "Accept-Encoding": "gzip, deflate"}); return s


probe, api = sess(), sess()
pool = ThreadPoolExecutor(4)
probe2 = sess()
for s in (probe, api, probe2):
    s.get(f"{B}/rq/{LIVE + 500}", timeout=8)


def pct(xs, p):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] * 1000


def v262():
    wait = random.uniform(0, 0.15)
    t = time.perf_counter()
    fs = [pool.submit(probe.get, f"{B}/rq/{LIVE}", timeout=8), pool.submit(probe2.get, f"{B}/rq/{LIVE + 1}", timeout=8),
          pool.submit(api.get, f"{B}/api/rq_addbanner_check/{LIVE}", timeout=8)]
    for f in fs: f.result()
    api.get(f"{B}/api/rq_addbanner/{LIVE}", timeout=8)
    return wait + time.perf_counter() - t


def v263():
    wait = random.uniform(0, 0.075)
    t = time.perf_counter()
    probe.get(f"{B}/rq/{LIVE}", timeout=8)
    api.get(f"{B}/api/rq_addbanner/{LIVE}", timeout=8)
    return wait + time.perf_counter() - t


def armed():
    wait = random.uniform(0, 0.1)
    t = time.perf_counter(); api.get(f"{B}/api/rq_addbanner/{LIVE}", timeout=8)
    return wait + time.perf_counter() - t


def armed262():
    # HEAD-before-2.6.3 pre-open wait: each 0.15s tick re-polls check, then add when it opens
    wait = random.uniform(0, 0.15)
    t = time.perf_counter(); api.get(f"{B}/api/rq_addbanner_check/{LIVE}", timeout=8); api.get(f"{B}/api/rq_addbanner/{LIVE}", timeout=8)
    return wait + time.perf_counter() - t


for name, fn in (("v2.6.2 open-at-detect path", v262), ("v2.6.3 open-at-detect path", v263),
                 ("pre-2.6.3 pre-open gate path (check->add)", armed262), ("v2.6.3 pre-open gate path (add only)", armed)):
    xs = []
    for _ in range(N):
        xs.append(fn()); time.sleep(0.4)
    print(f"{name:44s} n={N} p50={pct(xs,50):6.1f}ms p90={pct(xs,90):6.1f}ms mean={statistics.mean(xs)*1000:6.1f}", flush=True)
