"""Read-only latency legs to ezloan.io from a KR host (customer 5136338).

Measures, anonymously: DNS, TCP connect, TLS handshake, cold full GET, warm keep-alive GET
for /rq/{missing}, /api/rq_addbanner_check/{id} and /api/rq_addbanner/{id} (anonymous, so
the server answers with a refusal; no session, nothing is registered, nothing is spent).
Prints p50/p90 in ms.
"""
import socket, ssl, statistics, sys, time
import requests

HOST = "ezloan.io"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
MISS = int(sys.argv[1]) if len(sys.argv) > 1 else 34200
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def show(name, xs):
    print(f"{name:44s} n={len(xs):3d} p50={pct(xs,50)*1000:7.1f}ms p90={pct(xs,90)*1000:7.1f}ms min={min(xs)*1000:6.1f}")


dns, tcp, tls = [], [], []
for _ in range(N):
    t = time.perf_counter(); ip = socket.getaddrinfo(HOST, 443, socket.AF_INET)[0][4][0]; dns.append(time.perf_counter() - t)
    t = time.perf_counter(); s = socket.create_connection((ip, 443), timeout=5); tcp.append(time.perf_counter() - t)
    t = time.perf_counter(); ss = ssl.create_default_context().wrap_socket(s, server_hostname=HOST); tls.append(time.perf_counter() - t)
    ss.close(); time.sleep(0.2)
show("DNS getaddrinfo", dns); show("TCP connect", tcp); show("TLS handshake", tls)

hdr = {"User-Agent": UA, "Referer": "https://ezloan.io/rq", "Accept-Encoding": "gzip, deflate"}
cold = []
for _ in range(min(N, 15)):
    t = time.perf_counter(); requests.get(f"https://{HOST}/rq/{MISS}", headers=hdr, timeout=8); cold.append(time.perf_counter() - t); time.sleep(0.3)
show("cold GET /rq/{missing} (new conn each)", cold)

s = requests.Session(); s.headers.update(hdr)
s.get(f"https://{HOST}/rq/{MISS}")
for path, name in ((f"/rq/{MISS}", "warm GET /rq/{missing}"),
                   (f"/api/rq_addbanner_check/{MISS}", "warm GET rq_addbanner_check (anon)"),
                   (f"/api/rq_addbanner/{MISS}", "warm GET rq_addbanner (anon, refused)")):
    xs = []; body = ""
    for _ in range(N):
        t = time.perf_counter(); r = s.get(f"https://{HOST}{path}", timeout=8); xs.append(time.perf_counter() - t); body = r.text[:80]; time.sleep(0.25)
    show(name, xs); print("   sample body:", repr(body), "status", r.status_code, "srv", r.headers.get("server"), r.headers.get("cf-ray"))
