# -*- coding: utf-8 -*-
"""v2.5.2 검증: post_absent 재시도의 FAST_RETRY/BACKOFF 동작을 실제 Registrar._handle 로직으로
확인한다.

목적(2026-07-27, 운영자 지시): v2.5.1은 post_absent 를 매 사이클(0.8s) 무조건 재시도해
rq_addbanner(WRITE)를 계속 쏜다. 실제 페이지-반영 지연은 실측상 길어야 수초~수십초이므로,
그 구간(FAST_RETRY_CYCLES)까지는 매 사이클 즉시 재시도해 유료 "1등 등록속도"를 그대로
지키되, 그 구간을 넘어서면(진짜 없는 번호이거나 서버 이상 가능성) BACKOFF_INTERVAL 사이클
마다 한 번만 재시도해 WRITE 엔드포인트 부담을 줄인다(더 보수적/사이트 친화적).

이 스크립트는 두 가지를 검증한다:
  1) FAST_RETRY 구간 안(수십 초)에서 페이지가 뜨면 v2.5.1 과 동일하게 다음 사이클 즉시
     등록된다(속도 회귀 없음).
  2) FAST_RETRY 구간을 넘어 페이지가 늦게 뜨는 경우, 그 구간 동안 실제 rq_addbanner 호출
     (WRITE) 횟수가 사이클 수보다 훨씬 적다(=백오프가 실제로 걸린다는 증거)면서도, 결국
     페이지가 뜨면 등록에 성공한다(무한 방치 아님).
"""
import config
import ezloan_bot as eb
from repro_post_absent_race import (
    FakeResp, EXISTING, REAL_MAX, RACING_ID, REAL_PAGE, EMPTY_PAGE, build_registrar,
)

ADD_CALLS = {"n": 0}


class BackoffFakeSession:
    def __init__(self, page_live_after_cycle):
        self.headers = {}
        self.cookies = []
        self.page_live_after_cycle = page_live_after_cycle
        self.cycle = {"n": 0}
        self.registered = []

    def _page_is_live(self, pid):
        if pid in EXISTING:
            return True
        if pid == RACING_ID:
            return self.cycle["n"] >= self.page_live_after_cycle
        return False

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            ids = sorted(EXISTING, reverse=True)[: config.MAX_POSTS]
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
            return FakeResp(200, text="로그아웃 광고 관리 " + links)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            r = FakeResp(200, text=REAL_PAGE if self._page_is_live(pid) else EMPTY_PAGE)
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 186})
        if "/api/rq_addbanner/" in url:
            ADD_CALLS["n"] += 1
            pid = int(url.rsplit("/", 1)[-1])
            if pid not in self.registered and self._page_is_live(pid):
                self.registered.append(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def one_cycle(r, frontier):
    ids = eb.list_post_ids(r.s)
    real_max = max((int(x) for x in ids if str(x).isdigit()), default=0)
    if real_max:
        sane = real_max + 1
        if frontier > sane + config.LOOKAHEAD:
            frontier = sane
    ahead, safe_frontier = eb.lookahead_ids(r.s, frontier, config.LOOKAHEAD)
    if safe_frontier > frontier:
        frontier = safe_frontier
    for pid, precheck in ahead:
        if pid in r.seen or pid in r._post_absent_giveup:
            continue
        exists = r._handle(pid, precheck=precheck)
        if exists and pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
    return frontier


def scenario_fast_window_unaffected():
    """FAST_RETRY 구간 안에서 뜨면 v2.5.1 과 동일하게(매 사이클 즉시) 등록되어야 한다."""
    ADD_CALLS["n"] = 0
    r = build_registrar()
    r.s = BackoffFakeSession(page_live_after_cycle=5)
    r.seen.update(eb.list_post_ids(r.s))
    frontier = REAL_MAX + 1
    for i in range(1, 8):
        r.s.cycle["n"] = i
        frontier = one_cycle(r, frontier)
        if RACING_ID in r.s.registered:
            break
    assert RACING_ID in r.s.registered, "FAST_RETRY 구간 안에서도 등록 실패(속도 회귀)"
    assert i <= config.POST_ABSENT_FAST_RETRY_CYCLES, (
        f"FAST_RETRY 구간({config.POST_ABSENT_FAST_RETRY_CYCLES}) 안에서 해결됐는데 "
        f"백오프가 걸림(사이클#{i})")
    print(f"[OK] FAST_RETRY 구간 내 해결(사이클#{i}) - 매 사이클 즉시 재시도, 속도 회귀 없음. "
          f"WRITE 호출={ADD_CALLS['n']}회")


def scenario_backoff_reduces_write_load():
    """FAST_RETRY 구간을 넘겨도(계속 post_absent) WRITE 호출이 사이클 수보다 훨씬 적어야 한다."""
    ADD_CALLS["n"] = 0
    r = build_registrar()
    live_after = config.POST_ABSENT_FAST_RETRY_CYCLES + 25  # 백오프 구간에서 뜸
    r.s = BackoffFakeSession(page_live_after_cycle=live_after)
    r.seen.update(eb.list_post_ids(r.s))
    frontier = REAL_MAX + 1
    total_cycles = 0
    for i in range(1, live_after + 10):
        r.s.cycle["n"] = i
        frontier = one_cycle(r, frontier)
        total_cycles = i
        if RACING_ID in r.s.registered:
            break
    assert RACING_ID in r.s.registered, (
        f"백오프 구간을 넘겨 페이지가 떴는데도 끝내 등록 안 됨(무한 방치 버그) - "
        f"WRITE 호출={ADD_CALLS['n']}, 사이클={total_cycles}")
    # 매 사이클 WRITE 를 쐈다면 total_cycles 만큼 나왔을 것. 백오프가 걸렸다면 그보다 훨씬 적어야 함.
    assert ADD_CALLS["n"] < total_cycles, (
        f"백오프가 전혀 안 걸림: WRITE 호출({ADD_CALLS['n']}) >= 사이클({total_cycles})")
    expected_max = (config.POST_ABSENT_FAST_RETRY_CYCLES
                     + (total_cycles - config.POST_ABSENT_FAST_RETRY_CYCLES)
                       // config.POST_ABSENT_BACKOFF_INTERVAL + 2)
    assert ADD_CALLS["n"] <= expected_max, (
        f"WRITE 호출({ADD_CALLS['n']})이 백오프 산식 상한({expected_max})을 넘음")
    print(f"[OK] FAST_RETRY 구간을 넘겨도(사이클={total_cycles}) 백오프가 걸려 "
          f"WRITE 호출={ADD_CALLS['n']}회로 억제됐고, 끝내 등록에 성공함(무한 방치 아님).")


def main():
    scenario_fast_window_unaffected()
    scenario_backoff_reduces_write_load()
    print("\nALL POST_ABSENT BACKOFF CHECKS PASSED")


if __name__ == "__main__":
    main()
