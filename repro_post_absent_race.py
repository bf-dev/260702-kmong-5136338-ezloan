# -*- coding: utf-8 -*-
"""v2.5.0 실제 재현(2026-07-27, 고객 5136338): '체크는 open인데 글 페이지가 아직 안 뜬' 찰나의
경쟁(post_absent)을 겪은 글이 영원히 등록되지 않는 버그를 실제 Registrar 루프 로직으로 재현한다.

관측(고객 로그, artifacts-check 5136338):
  02:43:18 [registered] post=30819 rank=1 msg=success
  02:43:19 [register_post_absent] post=30820 status=200 msg=404 error note=post_absent
  (이후 02:56:25 까지 13분간 post=30820 관련 register 로그가 전혀 없음. frontier 는 30820 에 고정.)
  02:56:25 frontier 가 30820 -> 30821 로 조용히(등록 로그 없이) 전진.
  고객 스크린샷: 그 사이 ezloan.io/rq/30820 은 "3분전" 글로 실재했고, 더원대부(585) 배너는
  끝내 그 글에 없었다.

근본 원인: v2.4.6 이 'open' 후보를 post_exists 확인 없이 바로 rq_addbanner 로 등록 시도하도록
바꾸면서(속도 최적화), '계정 체크(rq_addbanner_check)는 통과했지만 글 페이지가 아직 안 뜬'
찰나의 경쟁이 실전에서 실제로 발생한다(이지론 서버의 글번호 채번과 페이지 서빙 사이 지연).
그 순간엔 register()가 정확히 post_absent 를 돌려준다. 그런데 NON_RETRYABLE 에 post_absent
가 들어있어 _handle() 이 그 pid 를 self.seen 에 영구 등록해 버린다. lookahead 는 seen 여부와
무관하게 매 사이클 같은 frontier pid 를 다시 probe 하지만('open' 이면 즉시 break), 상위 루프는
'pid in seen' 이면 register() 를 다시 부르지 않는다 -> 그 글이 실제로 뜬 뒤에도 재시도가 전혀
일어나지 않아 영원히 등록되지 않는다(고객이 낸 유료 "빠른 1등 등록"의 정확한 반대 결과).

기대(수정 후): post_absent 는 RETRYABLE 이어야 한다. 글이 실제로 뜨는 즉시(다음 사이클) 같은
frontier pid 를 재시도해 등록에 성공한다.
"""
import config
import ezloan_bot as eb

REAL_MAX = 30819                     # 등록 직전까지의 실제 최신 글
EXISTING = set(range(30700, REAL_MAX + 1))
REGISTERED = []
# 이 글번호는 "체크는 통과하지만(계정 정상) 페이지가 아직 안 뜬" 상태로 시작한다.
RACING_ID = REAL_MAX + 1             # 30820
PAGE_LIVE_AFTER_CYCLE = 3            # 이 사이클부터 실제 페이지가 뜬다(찰나의 경쟁 재현)
_cycle_counter = {"n": 0}

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


def _page_is_live(pid):
    if pid in EXISTING:
        return True
    if pid == RACING_ID:
        return _cycle_counter["n"] >= PAGE_LIVE_AFTER_CYCLE
    return False


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            ids = sorted(EXISTING, reverse=True)[: config.MAX_POSTS]
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
            return FakeResp(200, text="로그아웃 광고 관리 " + links)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            r = FakeResp(200, text=REAL_PAGE if _page_is_live(pid) else EMPTY_PAGE)
            r.url = url
            return r
        # check: 계정은 항상 정상(open). 이지론의 check API 는 '글 존재'를 검사하지 않는다
        # (관측된 사실 - probe_state 의 주석과 동일).
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 186})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            if pid not in REGISTERED and _page_is_live(pid):
                REGISTERED.append(pid)
                return FakeResp(200, js={"result": True})
            # 페이지가 아직 안 떴으면 실제 이지론처럼 '404 error'
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
    r._last_amount = None
    r._last_session_save = 1e18
    r._fresh_refuse_streak = 0
    r._no_perm_streak = 0
    r._no_perm_warned = False
    r._relogin_done = False
    r._auth_mismatch_streak = 0
    r._post_absent_pid = None
    r._post_absent_streak = 0
    r._wait = lambda s: None
    return r


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
        if pid in r.seen:
            continue
        exists = r._handle(pid, precheck=precheck)
        if exists and pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
    new = [i for i in ids if i not in r.seen]
    for pid in sorted(new, key=int, reverse=True):
        if pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
        r._handle(pid)
    return frontier


def main():
    r = build_registrar()
    r.seen.update(eb.list_post_ids(r.s))
    frontier = REAL_MAX + 1  # 30820
    print(f"시작 frontier={frontier} (30819 방금 등록됨을 가정), 경쟁 대상={RACING_ID}")

    # 사이클 1~2: 글 페이지가 아직 안 떠 있음(찰나의 경쟁) -> post_absent 가 나야 정상.
    for i in range(1, PAGE_LIVE_AFTER_CYCLE):
        _cycle_counter["n"] = i
        frontier = one_cycle(r, frontier)
        print(f"  사이클#{i}: frontier={frontier} 등록됨={REGISTERED} seen에있음={str(RACING_ID) in r.seen}")

    assert RACING_ID not in REGISTERED, "아직 페이지가 안 떴는데 등록되면 테스트 전제가 틀렸음"

    # 사이클 3+: 이제 실제로 글이 떴다(고객 스크린샷의 '3분전 글' 상태). 계속 폴링하면
    # 재시도해서 등록에 성공해야 한다(유료 업그레이드가 약속한 신뢰성).
    for i in range(PAGE_LIVE_AFTER_CYCLE, PAGE_LIVE_AFTER_CYCLE + 5):
        _cycle_counter["n"] = i
        frontier = one_cycle(r, frontier)
        if RACING_ID in REGISTERED:
            break

    print(f"최종: frontier={frontier} 등록됨={REGISTERED}")
    assert RACING_ID in REGISTERED, (
        f"FAIL(재현된 버그): 글 {RACING_ID} 이 실제로 존재하게 된 뒤에도 재시도 없이 영원히 "
        f"등록되지 않음. post_absent 가 NON_RETRYABLE 이라 self.seen 에 영구 등록되었기 때문. "
        f"seen에있음={str(RACING_ID) in r.seen}")
    print(f"[OK] 찰나의 post_absent 경쟁을 겪은 뒤에도 글이 실제로 뜨자 재시도해 등록에 성공함.")


if __name__ == "__main__":
    main()
