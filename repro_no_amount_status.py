# -*- coding: utf-8 -*-
"""v2.6.4: when rq_addbanner answers "no amount" (no_banner_amount, 배너 잔여 0) the GUI status
line and log box must say 배너 잔여가 없습니다 ... and must stop printing 모니터링 중. The message is
cleared once a registration succeeds again. Customer case 2026-10-11: posts 34168-34172 all
answered msg=no amount while the window kept printing 모니터링 중.
Real Registrar.run() on a fake clock and a fake ezloan (harness from repro_gate_fire_armed.py).
"""
import config
import ezloan_bot as eb

REAL_MAX = 34167
P1, P2 = REAL_MAX + 1, REAL_MAX + 2
ALLOC = {P1: 3.0, P2: 40.0}
NOW = [0.0]
SCEN = {"amount": 0}
REG, STATUS, LOGS = {}, [], []


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
    return pid <= REAL_MAX or (pid in ALLOC and NOW[0] >= ALLOC[pid])


class S:
    headers, cookies = {}, []

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL:
            ids = [i for i in range(P2, REAL_MAX - 20, -1) if live(i)][:20]
            return R(text="로그아웃 " + "".join(f'<a href="/rq/{i}">x</a>' for i in ids))
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            return R(text=("js-memberConfirmView " + "x" * 3000) if live(pid) else "<script>존재하지 않은</script>")
        pid = int(url.rsplit("/", 1)[-1])
        if "/api/rq_addbanner_check/" in url:
            if SCEN["amount"] <= 0:
                return R(js={"result": False, "msg": "no amount"})
            return R(js={"result": True, "amount": SCEN["amount"]})
        if "/api/rq_addbanner/" in url:
            if not live(pid):
                return R(js={"result": False, "msg": "404 error"})
            if SCEN["amount"] <= 0:
                return R(js={"result": False, "msg": "no amount"})
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
    r.log = lambda t, *a, **k: LOGS.append((NOW[0], t))
    r.events = []
    r.remote = lambda ev, d="", *a, **k: r.events.append((NOW[0], ev, d))
    r.status = lambda t, *a, **k: STATUS.append((NOW[0], t))
    return r


def run(r, until, hook=None):
    def wait(sec):
        NOW[0] += max(sec, 1e-9)
        if hook:
            hook()
    r._wait = wait
    r.should_stop = lambda: NOW[0] >= until
    eb.in_run_window = lambda dt=None: True
    eb._sync_csrf_header = lambda s: None
    eb.threading.Thread = lambda target=None, daemon=None: type("T", (), {"start": lambda self: None})()
    orig = eb.time.time
    eb.time.time = lambda: NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig


MSG = eb.NO_AMOUNT_STATUS
assert MSG == "배너 잔여가 없습니다. 이지론에서 배너를 충전해 주세요."


def topup():
    if NOW[0] >= 30.0:
        SCEN["amount"] = 5


r = build()
run(r, 60.0, hook=topup)

# A: balance 0 on P1 -> status line shows the message (status_cb also appends to the log box)
shown = [t for t, s in STATUS if s == MSG]
assert shown and shown[0] >= ALLOC[P1], f"no-amount status never shown: {STATUS[:5]}"
assert str(P1) not in REG
mon_while_empty = [t for t, l in LOGS if "모니터링 중" in l and shown[0] < t < 30.0]
assert not mon_while_empty, f"모니터링 중 printed {len(mon_while_empty)}x while 배너 잔여 0"
print(f"[OK] A: no amount on post {P1} -> status '{MSG}' at t={shown[0]:.2f}s, "
      f"0x 모니터링 중 while empty")

# B: after the top-up the next post registers and the message is cleared
assert P2 in REG, "post after top-up never registered"
cleared = [t for t, s in STATUS if s == "자동등록 진행 중" and t >= REG[P2] - 1e-6]
assert cleared, f"no-amount status not cleared after success: {STATUS[-5:]}"
assert r._no_amount is False
mon_after = [t for t, l in LOGS if "모니터링 중" in l and t > REG[P2]]
assert mon_after, "모니터링 중 should resume after a successful registration"
assert any(e == "no_amount_shown" for _t, e, _d in r.events)
assert any(e == "no_amount_cleared" for _t, e, _d in r.events)
print(f"[OK] B: top-up -> post {P2} registered at t={REG[P2]:.2f}s, status back to '자동등록 진행 중', "
      f"모니터링 중 resumes ({len(mon_after)}x)")
print("ALL NO-AMOUNT CHECKS PASSED")
