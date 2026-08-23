# -*- coding: utf-8 -*-
"""라이브 재현(글 32005, 2026-08-23): '아직 등록이 안 열린' 새 글을 영영 놓치던 버그.

관측된 사실(모두 실측):
  03:07:37.778  우리 봇: post_live=True -> rq_addbanner_check -> {result:false,"no permission"}
                예전 코드는 no_permission 을 NON_RETRYABLE 로 봐서 seen 처리 + 프런티어 전진.
  03:07:56.208  /rq/32005 가 배너 <ul> 를 가진 완전한 페이지로 처음 렌더(= 등록 개시)
  03:07:56.35   옥자대부(544) 배너 등장(개시 +0.14s)
  최종          배너 7개, 우리(585) 없음  <- 글 하나를 통째로 잃었다

즉 이지론은 글 번호를 먼저 채번해 페이지를 부분적으로 서빙하고, 배너 등록은 18.4초 뒤에
열린다. 그 창 안의 'no permission' 은 계정 거부가 아니라 타이밍이다.

이 재현은 그 타임라인을 그대로 흉내낸다: 첫 N 사이클은 check 가 'no permission',
그 뒤로는 열려서 성공. 기대 동작 = 열리는 즉시 등록되고, 그 전에는 seen/프런티어가
그 글을 지나치지 않는다.
"""
import time

import config
import ezloan_bot as eb

PID = "32005"
OPEN_AFTER_CALLS = 40      # 이만큼의 check 이후에 등록이 열린다(실측 18.4초 ≈ fast tick 120회)


class Resp:
    def __init__(self, status=200, js=None, text="", ct="application/json"):
        self.status_code = status
        self._js = js
        self.text = text
        self.headers = {"content-type": ct if js is not None else "text/html"}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class Site:
    """check 는 처음엔 'no permission', OPEN_AFTER_CALLS 회 뒤에 열린다."""

    def __init__(self):
        self.checks = 0
        self.adds = 0
        self.cookies = []
        self.headers = {}

    def get(self, url, **kw):
        if "rq_addbanner_check/" in url:
            self.checks += 1
            if self.checks <= OPEN_AFTER_CALLS:
                return Resp(js={"result": False, "msg": "no permission"})
            return Resp(js={"result": True, "amount": 483})
        if "rq_addbanner/" in url:
            self.adds += 1
            return Resp(js={"result": True, "msg": "success"})
        if url.endswith(f"/rq/{PID}"):
            return Resp(text=('<ul class="section_body loan_list recommend">'
                              '<li><a href="/l/544" class="item ad_sm" title="옥자대부-BEST">'
                              '<div class="name">옥자대부 <span>정식등록</span></div></a></li>'
                              '<li><a href="/l/585" class="item " title="더원대부중개-24시">'
                              '<div class="name">더원대부중개 </div></a></li></ul>'),
                        ct="text/html")
        return Resp(status=404)


eb._sync_csrf_header = lambda s: None
eb.logged_in = lambda s: True

site = Site()
reg = eb.Registrar.__new__(eb.Registrar)
reg.s = site
reg.seen = set()
reg.log = lambda *a, **k: None
reg.remote = lambda *a, **k: None
reg.should_stop = lambda: False
reg._write_seen = lambda: None
reg.relogin = None
reg._registered_total = 0
reg._session_lost_streak = 0
reg._auth_mismatch_streak = 0
reg._fresh_refuse_streak = 0
reg._no_perm_streak = 0
reg._no_perm_warned = False
reg._no_perm_retry_pid = None
reg._no_perm_retry_since = 0.0
reg._no_perm_retry_n = 0
reg._relogin_done = False
reg._post_absent_pid = None
reg._post_absent_streak = 0
reg._post_absent_giveup = set()
reg._last_amount = None
reg._send_auth_diag = lambda pid: None
reg._force_relogin = lambda: None

# --- 1) 등록이 열리기 전: 절대 seen 에 넣지도, 프런티어를 전진시키지도 않는다 ---
for i in range(OPEN_AFTER_CALLS):
    advanced = reg._handle(PID)
    assert advanced is False, f"cycle {i}: frontier must NOT advance past a not-yet-open post"
    assert PID not in reg.seen, f"cycle {i}: post must NOT be marked seen while waiting"
print(f"[OK] 등록 개시 전 {OPEN_AFTER_CALLS}회: seen 미등록 + 프런티어 정지(글을 잃지 않음)")
assert site.adds == 0, "no rq_addbanner (write) may be fired while the check refuses"
print("[OK] 대기 중에는 쓰기(rq_addbanner) 0회 - 배너잔여 소모 없음")

# --- 2) 열리는 즉시 등록되고 실제 순위를 읽는다 ---
advanced = reg._handle(PID)
assert advanced is True, "must advance once the post is registered"
assert site.adds == 1, f"exactly one write expected, got {site.adds}"
assert PID in reg.seen, "registered post must be marked seen"
assert reg._registered_total == 1, reg._registered_total
print("[OK] 개시되는 tick 에서 즉시 등록(쓰기 1회) + seen 처리 + 프런티어 전진")

# --- 3) 대기 창을 넘기면 예전대로 포기한다(무한 정체 방지) ---
site2 = Site()
site2.checks = -10 ** 9      # never opens
reg2 = eb.Registrar.__new__(eb.Registrar)
for k, v in reg.__dict__.items():
    setattr(reg2, k, v)
reg2.s = site2
reg2.seen = set()
reg2._no_perm_retry_pid = None
reg2._no_perm_streak = 0
assert reg2._handle("32999") is False
reg2._no_perm_retry_since = time.time() - config.NO_PERM_RETRY_SECONDS - 1
advanced = reg2._handle("32999")
assert advanced is True, "after the wait window a real account refusal must give up"
assert "32999" in reg2.seen, "gave-up post must be marked seen so the loop moves on"
print(f"[OK] {config.NO_PERM_RETRY_SECONDS:.0f}초를 넘겨도 계속 거부되면 예전대로 포기(정체 없음)")

print("\nALL CHECKS PASSED: 'no permission' on a fresh post is a timing window, not a give-up.")
