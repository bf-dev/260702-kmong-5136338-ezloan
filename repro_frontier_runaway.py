# -*- coding: utf-8 -*-
"""v2.4.3 재현/검증: '계정 no_permission 상태 -> 프런티어 미래 번호로 폭주(+6/사이클)' 버그를
실제 Registrar 루프 로직으로 재현하고, 수정 후 프런티어가 실제 최신 글을 넘어 폭주하지 않으며,
권한이 돌아오면 look-ahead 가 '진짜 새 글'을 즉시 등록함을 증명한다.

관측(2026-07-13, 고객 5136338 로그):
  - 실제 최신 글 = 30031. 계정이 no_permission 상태(post 30031 도 'no permission').
  - v2.4.2: 프런티어가 30031 -> 31213 로 +6/사이클 폭주(미존재 유령 번호를 blocked 로 처리).
  - 기대(v2.4.3): 프런티어는 실제 최신(30031)+1+창(6) 이내에 머무른다. 폭주 없음.
"""
import sys
import config
import ezloan_bot as eb

REAL_MAX = 30031                 # 실제 최신 글(목록 최상단). 그 위는 아직 안 생긴 미래 번호.
EXISTING = set(range(29900, REAL_MAX + 1))   # 목록/페이지에 실재하는 글 범위
ACCOUNT_NO_PERMISSION = True     # 계정이 지금 no_permission 상태(관측된 상황)
NEW_POST_HOLDER = {"id": None}   # 나중에 '진짜 새 글'을 하나 생성할 때 채운다
REGISTERED = []                  # 앱이 실제 등록(add 성공)한 글

REAL_PAGE = ("배너 등록을 눌러 주세요 rq_addbanner js-memberConfirmView " + ("x" * 260000))
EMPTY_PAGE = "y" * 353


class FakeResp:
    def __init__(self, status, text="", js=None, ctype="application/json"):
        self.status_code = status
        self.text = text
        self._js = js
        self.url = ""
        self.headers = {"content-type": ctype}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        # /rq 목록
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            ids = sorted(EXISTING, reverse=True)[: config.MAX_POSTS]
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
            return FakeResp(200, text="로그아웃 광고 관리 " + links)
        # /rq/{id} 개별 페이지(존재 판정용)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            r = FakeResp(200, text=REAL_PAGE if pid in EXISTING else EMPTY_PAGE)
            r.url = url
            return r
        # check: 계정 상태를 그대로 반영(글 존재는 검사하지 않음 - 관측된 사실).
        if "/api/rq_addbanner_check/" in url:
            if ACCOUNT_NO_PERMISSION:
                # 계정이 no_permission 이면 실재 글이든 미래 번호든 전부 result:false/no permission.
                return FakeResp(200, js={"result": False, "msg": "no permission"})
            return FakeResp(200, js={"result": True, "amount": 100})
        # add: 계정 정상일 때, 실재하는 미등록 글이면 성공.
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            if not ACCOUNT_NO_PERMISSION and pid in EXISTING and pid not in REGISTERED:
                REGISTERED.append(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def build_registrar():
    r = eb.Registrar.__new__(eb.Registrar)
    r.s = FakeSession()
    r._cookies_raw = []
    r._diag_sent = False
    r.log = lambda *a, **k: None
    r.remote = lambda *a, **k: None
    r.should_stop = lambda: False
    r.relogin = None
    r.seen_path = None
    r.seen = set()
    r._session_lost_streak = 0
    r._cycle = 0
    r._registered_total = 0
    r._last_session_save = 1e18   # persist 억제
    r._fresh_refuse_streak = 0
    r._no_perm_streak = 0
    r._no_perm_warned = False
    r._relogin_done = False
    r._auth_mismatch_streak = 0
    r._post_absent_pid = None
    r._post_absent_streak = 0
    r._post_absent_giveup = set()
    r._wait = lambda s: None
    return r


def one_cycle(r, frontier):
    """run() 루프의 한 사이클(v2.4.6 구조: 목록 1회 + look-ahead + 목록 안전망)을 그대로 실행.

    v2.4.6 변경 반영:
      - 목록은 사이클당 1회만 받아 프런티어 재동기화·안전망에 함께 쓴다.
      - lookahead 는 (pid, precheck) 튜플을 돌려주고, open 후보는 존재 미확정이라 safe_frontier
        를 넘기지 않는다. 프런티어 전진은 _handle 반환값(실존 확정 여부)으로 결정한다.
    """
    # 목록 1회
    ids = eb.list_post_ids(r.s)
    real_max = max((int(x) for x in ids if str(x).isdigit()), default=0)
    if real_max:
        sane = real_max + 1
        if frontier > sane + config.LOOKAHEAD:
            frontier = sane
    # 1) look-ahead
    ahead, safe_frontier = eb.lookahead_ids(r.s, frontier, config.LOOKAHEAD)
    if safe_frontier > frontier:
        frontier = safe_frontier
    for pid, precheck in ahead:
        if pid in r.seen or pid in r._post_absent_giveup:
            continue
        exists = r._handle(pid, precheck=precheck)
        if exists and pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
    # 2) 목록 안전망(위에서 받은 목록 재사용)
    new = [i for i in ids if i not in r.seen]
    for pid in sorted(new, key=int, reverse=True):
        if pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
        r._handle(pid)
    return frontier


def main():
    global ACCOUNT_NO_PERMISSION
    r = build_registrar()
    # 재기준화: 실제 최신 글 기준으로 시작(run() 의 baseline 과 동일).
    r.seen.update(eb.list_post_ids(r.s))
    frontier = REAL_MAX + 1
    start_frontier = frontier

    # --- Phase A: 계정 no_permission 상태로 60 사이클 돈다(관측된 폭주 조건) ---
    ACCOUNT_NO_PERMISSION = True
    for _ in range(60):
        frontier = one_cycle(r, frontier)

    ceiling = REAL_MAX + 1 + config.LOOKAHEAD
    print(f"[Phase A] no_permission 60 사이클 후 frontier={frontier} "
          f"(시작 {start_frontier}, 실제최신={REAL_MAX}, 허용상한 {ceiling})")
    assert frontier <= ceiling, (
        f"FAIL: 프런티어가 폭주함 frontier={frontier} > 상한 {ceiling} "
        f"(v2.4.2 버그: 미존재 blocked 로 +6/사이클 runaway)")
    print(f"[OK] 프런티어가 실제 최신 글 근처에 머무름(폭주 없음). 등록 없음={len(REGISTERED)==0}")

    # --- Phase B: 권한이 정상으로 돌아오고 '진짜 새 글' 30032 가 생긴다 ---
    ACCOUNT_NO_PERMISSION = False
    NEW_ID = REAL_MAX + 1        # 30032
    EXISTING.add(NEW_ID)
    frontier = one_cycle(r, frontier)   # look-ahead 가 즉시 잡아야 한다
    print(f"[Phase B] 권한 복구 + 새 글 {NEW_ID} 생성 -> 등록됨={REGISTERED}, frontier={frontier}")
    assert NEW_ID in REGISTERED, (
        f"FAIL: 새 글 {NEW_ID} 가 등록되지 않음(look-ahead 가 못 잡음). "
        "프런티어가 폭주했다면 새 글이 프런티어보다 아래라 영원히 스킵된다.")
    print(f"[OK] 권한 복구 즉시 look-ahead 로 새 글 {NEW_ID} 등록 성공(상위 노출 확보).")

    print("\nALL CHECKS PASSED: no frontier runaway, and new post is registered on permission return.")


if __name__ == "__main__":
    main()
