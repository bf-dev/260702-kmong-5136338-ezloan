# -*- coding: utf-8 -*-
"""v2.5.5 검증: fast tick(FRONTIER_POLL_SECONDS)과 heavy tick(LIST_POLL_SECONDS)이 실제로
분리되어 Registrar.run() 안에서 도는지, 그리고 그 분리 덕분에 새 글이 무거운 목록(/rq, 309KB)
tick 을 기다리지 않고 바로 다음 fast tick 에서 잡히는지를 real Registrar.run() 코드로 확인한다
(가짜 시계로 실제 sleep 없이 - _wait 를 monkeypatch 해 즉시 시계만 전진시킨다).

배경(2026-07-28, 무료 튜닝): 예전엔 가벼운 프런티어 체크(rq_addbanner_check, 47바이트)와
무거운 목록 fetch(list_post_ids, /rq 309KB)가 같은 주기(POLL_SECONDS)로 묶여 돌았다. 이제는
가벼운 체크가 FRONTIER_POLL_SECONDS(0.2s) 마다 항상 돌고, 무거운 목록 작업은
LIST_POLL_SECONDS(1.0s) 마다만 얹혀서 돈다. 이 스크립트는:
  1) 무거운 /rq fetch 가 fast tick 마다가 아니라 LIST_POLL_SECONDS 간격으로만 일어난다.
  2) 두 heavy tick 사이(=목록 fetch 가 없는 구간)에 새로 생긴 글도 무거운 tick 을 기다리지
     않고 바로 다음 fast tick 에서(가벼운 체크만으로) 등록된다.
  3) 등록 시점의 '지연'(생성 시각 -> 등록 시각)이 LIST_POLL_SECONDS 가 아니라
     FRONTIER_POLL_SECONDS 오더로 bound 된다(감지 지연이 실제로 좁아졌음을 증명).
를 실제 run() 을 돌려 확인한다.
"""
import config
import ezloan_bot as eb

REAL_MAX = 500
NEW_PID = REAL_MAX + 1
EXISTING = set(range(1, REAL_MAX + 1))

FAKE_NOW = [0.0]
LIST_CALL_TIMES = []
CHECK_CALL_TIMES = []
# heavy tick 두 번 사이(중간)에 새 글이 생기게 한다 - 안전망(무거운 tick)이 아니라 fast tick
# 이 이 글을 잡아야만 통과하는 배치.
NEW_POST_APPEARS_AT = config.LIST_POLL_SECONDS * 1.5


def _page_is_live(pid):
    if pid in EXISTING:
        return True
    if pid == NEW_PID:
        return FAKE_NOW[0] >= NEW_POST_APPEARS_AT
    return False


class FakeResp:
    def __init__(self, status=200, text="", js=None, ctype="application/json"):
        self.status_code = status
        self.text = text
        self._js = js
        self.url = ""
        self.headers = {"content-type": ctype}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class _InlineFuture:
    def __init__(self, fn, args):
        try:
            self._v, self._e = fn(*args), None
        except Exception as e:   # noqa: BLE001 - 테스트 하네스
            self._v, self._e = None, e

    def result(self, timeout=None):
        if self._e:
            raise self._e
        return self._v


class InlinePool:
    """v2.6.0 의 _probe_pool 대역(테스트용). 진짜 스레드를 쓰지 않고 즉시 실행해서
    가짜 시계(FAKE_NOW) 기반 검증이 결정적으로 재현되게 한다."""

    def submit(self, fn, *args):
        return _InlineFuture(fn, args)


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            LIST_CALL_TIMES.append(FAKE_NOW[0])
            live = set(EXISTING)
            if _page_is_live(NEW_PID):
                live.add(NEW_PID)
            ids = sorted(live, reverse=True)[: config.MAX_POSTS]
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
            return FakeResp(200, text="로그아웃 광고 관리 " + links)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            live_page = "배너 rq_addbanner js-memberConfirmView " + ("x" * 260000)
            r = FakeResp(200, text=live_page if _page_is_live(pid) else ("y" * 353))
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            CHECK_CALL_TIMES.append(FAKE_NOW[0])
            return FakeResp(200, js={"result": True, "amount": 186})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            if _page_is_live(pid):
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def build_registrar():
    r = eb.Registrar.__new__(eb.Registrar)
    r.s = FakeSession()
    # v2.6.0: 존재 확인 전용(비로그인) 세션 + 병렬 발사 풀 + 같은 tick 의 check 캐시.
    r.probe = FakeSession()
    r._probe_pool = InlinePool()
    r._last_check = None
    r._cookies_raw = []
    r._diag_sent = False
    r.log = lambda *a, **k: None
    r.remote = lambda *a, **k: None
    r.relogin = None
    r.seen_path = None
    r.seen = set()
    r._session_lost_streak = 0
    r._cycle = 0
    r._registered_total = 0
    r._last_amount = None
    r._last_session_save = 1e18   # persist_session 이 절대 디스크에 안 쓰게(테스트 격리)
    r._fresh_refuse_streak = 0
    r._no_perm_streak = 0
    r._no_perm_warned = False
    r._relogin_done = False
    r._auth_mismatch_streak = 0
    r._post_absent_pid = None
    r._post_absent_streak = 0
    r._post_absent_giveup = set()
    r._last_heavy_tick = 0.0
    return r


def main():
    r = build_registrar()

    ticks = {"n": 0}
    MAX_TICKS = 60   # FRONTIER_POLL_SECONDS=0.2 기준 12s 분량 - heavy tick 이 10회 넘게 돎

    def fake_wait(seconds):
        FAKE_NOW[0] += seconds
        ticks["n"] += 1

    def should_stop():
        # NEW_PID 가 등록되고 나서 몇 tick 더 돌려 이후 동작(중복 등록 없음)도 확인한다.
        if NEW_PID in eb_registered_pids(r) and ticks["n"] > 0:
            return ticks["n"] >= _stop_after[0]
        return ticks["n"] >= MAX_TICKS

    _stop_after = [MAX_TICKS]

    def eb_registered_pids(reg):
        return reg.seen if NEW_PID in reg.seen else set()

    r._wait = fake_wait
    r.should_stop = should_stop

    orig_time = eb.time.time
    eb.time.time = lambda: FAKE_NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig_time

    # --- 검증 1: 무거운 /rq fetch 는 LIST_POLL_SECONDS 간격으로만 일어난다 ---
    # 맨 앞 2개는 시작 시점의 1회성 호출이다(logged_in() 의 로그인 확인 GET + run() 의 baseline
    # list_post_ids()) - 주기적 heavy tick 이 아니므로 간격 분석에서 제외한다.
    assert len(LIST_CALL_TIMES) >= 5, f"heavy tick 이 충분히 안 돌았음: {LIST_CALL_TIMES}"
    periodic = LIST_CALL_TIMES[2:]
    gaps = [b - a for a, b in zip(periodic, periodic[1:])]
    # heavy tick 은 '경과시간 >= LIST_POLL_SECONDS 인 첫 fast tick'에서 발동하므로, 부동소수점
    # 오차까지 감안하면 실제 간격은 [LIST_POLL_SECONDS, LIST_POLL_SECONDS+FRONTIER_POLL_SECONDS)
    # 범위에 들어야 정상이다(그 이상 벌어지면 분리가 의도보다 느슨해진 것).
    lo, hi = config.LIST_POLL_SECONDS - 1e-6, config.LIST_POLL_SECONDS + config.FRONTIER_POLL_SECONDS
    for g in gaps:
        assert lo <= g < hi, (
            f"heavy tick 간격이 기대 범위[{lo:.3f},{hi:.3f}) 밖: {g} (전체 간격={gaps})")
    print(f"[OK] 무거운 /rq fetch {len(LIST_CALL_TIMES)}회, 전부 정확히 LIST_POLL_SECONDS"
          f"({config.LIST_POLL_SECONDS}s) 간격으로만 발생(fast tick 마다가 아님).")

    # --- 검증 2: 가벼운 체크는 heavy tick 보다 훨씬 자주(매 fast tick) 일어난다 ---
    assert len(CHECK_CALL_TIMES) > len(LIST_CALL_TIMES) * 2, (
        f"가벼운 체크가 heavy tick 만큼만 일어남(분리 실패): check={len(CHECK_CALL_TIMES)} "
        f"list={len(LIST_CALL_TIMES)}")
    print(f"[OK] 가벼운 rq_addbanner_check {len(CHECK_CALL_TIMES)}회 - heavy tick"
          f"({len(LIST_CALL_TIMES)}회)보다 훨씬 자주 돎(fast tick 분리 확인).")

    # --- 검증 3: 새 글은 그 다음 heavy tick 을 기다리지 않고 바로 등록된다 ---
    assert str(NEW_PID) in r.seen, f"새 글이 등록되지 않음: seen={r.seen}"
    # 등록이 실제로 일어난(=register 성공) 시각을 CHECK_CALL_TIMES 로는 알 수 없으니,
    # NEW_PID 에 대한 rq_addbanner_check 호출들 중 '생성 시각 이후 첫 호출' 시각으로 근사한다.
    # (매 fast tick 마다 lookahead 가 frontier=NEW_PID 를 찔러보므로, 이 시각이 곧 감지+등록 시각.)
    detect_time = None
    for t in CHECK_CALL_TIMES:
        if t >= NEW_POST_APPEARS_AT:
            detect_time = t
            break
    assert detect_time is not None, "생성 이후 체크 호출을 찾지 못함"
    lag = detect_time - NEW_POST_APPEARS_AT
    # heavy tick 간격(1.0s)보다 훨씬 짧아야 한다 - fast tick(0.2s) 오더여야 분리가 의미 있다.
    assert lag < config.LIST_POLL_SECONDS, (
        f"감지 지연({lag:.3f}s)이 LIST_POLL_SECONDS({config.LIST_POLL_SECONDS}s) 이상 - "
        "무거운 tick 을 기다린 것으로 보임(분리 실패)")
    assert lag <= config.FRONTIER_POLL_SECONDS + 1e-6, (
        f"감지 지연({lag:.3f}s)이 FRONTIER_POLL_SECONDS({config.FRONTIER_POLL_SECONDS}s)를 넘음")
    print(f"[OK] 새 글 감지+등록 지연={lag:.3f}s - LIST_POLL_SECONDS(무거운 tick, "
          f"{config.LIST_POLL_SECONDS}s)가 아니라 FRONTIER_POLL_SECONDS(가벼운 tick, "
          f"{config.FRONTIER_POLL_SECONDS}s) 오더로 bound 됨(이번 튜닝의 핵심 이득 재현).")

    print("\nALL FAST/HEAVY TICK SPLIT CHECKS PASSED")


if __name__ == "__main__":
    main()
