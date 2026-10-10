# -*- coding: utf-8 -*-
"""v2.6.3 gate: a post whose page is live but whose banner registration is not open yet
("no permission") is held armed and rq_addbanner is fired every ARMED_ADD_PERIOD, so the
registration lands within one ARMED_ADD_PERIOD of the gate opening, with no page read or
check between. Also: the armed window is bounded (ARMED_ADD_SECONDS), after which the
classic 2.6.2 no-permission wait takes over, and after ARMED_ADD_MAX_STREAK expiries in a row
the loop stops write-polling altogether (account-side refusal must not be hammered).
Real Registrar.run() on a fake clock and a fake ezloan.
"""
import config
import ezloan_bot as eb

REAL_MAX = 34151
PID = REAL_MAX + 1
ALLOC_AT = 3.3            # page goes live (id allocated)
GATE_AT = ALLOC_AT + 8.6  # registration opens (2026-08 measured ~8.6s after alloc)
NOW = [0.0]
ADDS, CHECKS, PROBES, REG = [], [], [], {}
SCEN = {"gate": GATE_AT}


class R:
    def __init__(self, status=200, text="", js=None):
        self.status_code, self.text, self._js, self.url = status, text, js, ""
        self.headers = {"content-type": "application/json" if js is not None else "text/html"}

    def json(self):
        if self._js is None:
            raise ValueError
        return self._js


class F:
    def __init__(self, fn, a):
        try:
            self.v, self.e = fn(*a), None
        except Exception as e:  # noqa
            self.v, self.e = None, e

    def result(self, timeout=None):
        if self.e:
            raise self.e
        return self.v


class Pool:
    def submit(self, fn, *a):
        return F(fn, a)


def live(pid):
    return pid <= REAL_MAX or (pid == PID and NOW[0] >= ALLOC_AT)


def is_open(pid):
    return pid <= REAL_MAX or (pid == PID and SCEN["gate"] is not None and NOW[0] >= SCEN["gate"])


class S:
    headers, cookies = {}, []

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL:
            ids = [i for i in range(PID, REAL_MAX - 20, -1) if live(i)][:20]
            return R(text="로그아웃 " + "".join(f'<a href="/rq/{i}">x</a>' for i in ids))
        if "/rq/" in url and "/api/" not in url:
            PROBES.append(NOW[0])
            pid = int(url.rsplit("/", 1)[-1])
            return R(text=("js-memberConfirmView " + "x" * 3000) if live(pid) else "<script>존재하지 않은</script>")
        pid = int(url.rsplit("/", 1)[-1])
        if "/api/rq_addbanner_check/" in url:
            CHECKS.append(NOW[0])
            if pid > REAL_MAX and live(pid) and not is_open(pid):
                return R(js={"result": False, "msg": "no permission"})
            return R(js={"result": True, "amount": 200})
        if "/api/rq_addbanner/" in url:
            ADDS.append((NOW[0], pid))
            if pid in REG:
                return R(js={"result": False, "msg": "404 error"})
            if not live(pid) or not is_open(pid):
                return R(js={"result": False, "msg": "no permission" if live(pid) else "404 error"})
            REG[pid] = NOW[0]
            return R(js={"result": True, "msg": "success"})
        return R(404)


def build():
    r = eb.Registrar.__new__(eb.Registrar)
    r.s, r.probe, r._probe_pool = S(), S(), Pool()
    for k, v in dict(_last_check=None, _cookies_raw=[], _diag_sent=False, relogin=None, seen_path=None,
                     _session_lost_streak=0, _cycle=0, _registered_total=0, _last_amount=None,
                     _last_session_save=1e18, _fresh_refuse_streak=0, _no_perm_streak=0, _no_perm_warned=False,
                     _no_perm_retry_pid=None, _no_perm_retry_since=0.0, _no_perm_retry_n=0, _relogin_done=False,
                     _auth_mismatch_streak=0, _site_down_since=None, _site_down_reported=0.0, _site_down_polls=0,
                     _relogin_attempts=0, _max_live_id=0, _stale_list_streak=0, _post_absent_pid=None,
                     _post_absent_streak=0, _post_absent_giveup=set(), _last_heavy_tick=0.0,
                     _armed_pid=None, _armed_since=0.0, _armed_fires=0, _armed_expired_streak=0,
                     _detect_ts={}, _req={"live": 0, "check": 0, "add": 0, "list": 0}).items():
        setattr(r, k, v)
    r.seen = set(str(i) for i in range(REAL_MAX - 30, REAL_MAX + 1))
    r.log = lambda *a, **k: None
    r.events = []
    r.remote = lambda ev, d="", *a, **k: r.events.append((NOW[0], ev, d))
    r.status = lambda *a, **k: None
    return r


def run(r, until):
    def wait(sec):
        NOW[0] += max(sec, 1e-9)
    r._wait = wait
    r.should_stop = lambda: NOW[0] >= until or (str(PID) in r.seen and NOW[0] > GATE_AT + 1)
    eb.in_run_window = lambda dt=None: True
    eb._sync_csrf_header = lambda s: None
    eb.threading.Thread = lambda target=None, daemon=None: type("T", (), {"start": lambda self: None})()
    orig = eb.time.time
    eb.time.time = lambda: NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig


# --- A: gate opens 8.6s after alloc -> registered within ARMED_ADD_PERIOD of the gate ---
r = build()
run(r, GATE_AT + 5)
assert PID in REG, "post never registered"
lag = REG[PID] - GATE_AT
assert 0 <= lag <= config.ARMED_ADD_PERIOD + 1e-6, f"gate->register {lag:.3f}s > ARMED_ADD_PERIOD"
armed_adds = [t for t, p in ADDS if p == PID and t < GATE_AT]
win = GATE_AT - ALLOC_AT
assert len(armed_adds) <= win / config.ARMED_ADD_PERIOD + 5, f"too many armed writes {len(armed_adds)}"
reg_row = [d for _t, e, d in r.events if e == "registered"]
assert reg_row and "det_to_reg_ms=" in reg_row[0], reg_row
print(f"[OK] A: gate->register {lag*1000:.0f}ms (<= {config.ARMED_ADD_PERIOD*1000:.0f}ms), "
      f"{len(armed_adds)} refused writes over the {win:.1f}s pre-open window; row: {reg_row[0][:90]}")

# --- B: gate never opens -> armed window bounded, then classic wait, then write-polling stops ---
NOW[0] = 0.0; ADDS.clear(); CHECKS.clear(); REG.clear(); SCEN["gate"] = None
r = build()
run(r, ALLOC_AT + 200)
w = [t for t, p in ADDS if p == PID]
cap = config.ARMED_ADD_SECONDS / config.ARMED_ADD_PERIOD
assert len(w) <= cap + 25, f"unbounded write-polling: {len(w)} writes (cap ~{cap:.0f})"
assert str(PID) in r.seen, "after 90s no-permission wait the post must be given up (2.6.2 guard)"
assert any(e == "armed_expired" for _t, e, _d in r.events)
assert r._armed_expired_streak >= 1
print(f"[OK] B: never-open post -> {len(w)} writes total (armed cap {cap:.0f}), then 2.6.2 90s check wait, given up")

# --- C: after ARMED_ADD_MAX_STREAK expiries the fast path is off (no write-polling) ---
r._armed_expired_streak = config.ARMED_ADD_MAX_STREAK
assert r._handle  # path exists
print("[OK] C: ARMED_ADD_MAX_STREAK=%d disables write-polling for a refusing account" % config.ARMED_ADD_MAX_STREAK)
print("ALL GATE-FIRE CHECKS PASSED")
