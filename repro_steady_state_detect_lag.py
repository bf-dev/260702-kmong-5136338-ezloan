# -*- coding: utf-8 -*-
"""v2.6.0 회귀 게이트: '정상 대기 상태'에서 새 글이 뜬 순간부터 rq_addbanner 가 실제로
나갈 때까지의 지연이 FRONTIER_POLL_SECONDS 오더인지 확인한다.

왜 이 재현이 필요했나 (2026-08-05 라이브 로그, 고객 5136338):
  기존 CI 재현들은 전부 '새 글이 곧바로(수 초 안에) 뜨는' 배치만 돌렸다. 그런데 이 고객의
  실제 운영 상태는 정반대다 - 새 글은 보통 7~80분 간격으로 뜨고, 그 사이 내내 프런티어
  글번호는 존재하지 않는다. 그 구간에서 v2.5.5 는:
    1) rq_addbanner_check 가 (계정 자격만 보므로) 존재하지 않는 번호에도 result:true 를 줌
    2) -> register() 가 rq_addbanner 를 쏨 -> '404 error' -> post_absent
    3) -> POST_ABSENT_FAST_RETRY_CYCLES(32초)를 넘기면 BACKOFF_INTERVAL tick 마다만 쓰기 발사
    4) -> POST_ABSENT_GIVEUP_STREAK(~9.7분) 뒤엔 아예 포기하고 프런티어가 그 번호를 지나감
  즉 '새 글이 뜨는 바로 그 순간'에 앱은 감지기(쓰기)를 20 tick 에 한 번만 쏘고 있었다.
  라이브 실측 tick 은 287ms 였으므로 최대 5.7초(평균 2.9초)를 그냥 흘려보냈고, 10분을
  넘긴 뒤엔 그 번호를 아예 감시하지 않아 1초 주기 309KB 목록 안전망에 의존했다.

  이 스크립트는 그 운영 상태를 그대로 재현한다: 새 글은 FAST_RETRY 구간을 한참 넘긴
  시점(=백오프가 확실히 걸린 뒤)에 뜬다. v2.5.5 코드에서는 실패하고(등록이 backoff/목록
  주기만큼 늦음), v2.6.0(읽기로 존재 확정 -> 확정된 뒤에만 쓰기)에서는 통과한다.

가짜 시계로 돌린다(_wait 가 실제로 자지 않고 시계만 전진). 네트워크 접근 없음.
"""
import config
import ezloan_bot as eb

REAL_MAX = 31244
NEW_PID = REAL_MAX + 1
EXISTING = set(range(REAL_MAX - 30, REAL_MAX + 1))

# 새 글이 뜨는 시각. FAST_RETRY 구간(≈32초)을 확실히 넘긴 뒤로 잡아 '백오프가 걸린 상태에서
# 글이 뜨는' 실제 운영 상황을 만든다. 그리고 반드시 '백오프 재시도 tick 들 사이 한복판'에
# 뜨게 한다: 백오프 tick 에 딱 맞춰 뜨면 옛(v2.5.x) 코드도 우연히 즉시 발사한 것처럼 보여
# 재현이 무의미해진다(실제 운영에서 글이 뜨는 시각은 당연히 백오프 위상과 무관하다).
_BO = config.POST_ABSENT_BACKOFF_INTERVAL
NEW_POST_APPEARS_AT = (
    (config.POST_ABSENT_FAST_RETRY_CYCLES + _BO * 5 + _BO // 2) * config.FRONTIER_POLL_SECONDS)

FAKE_NOW = [0.0]
ADD_CALL_TIMES = []      # rq_addbanner(WRITE) 가 실제로 나간 시각들
ADD_CALLS_NEW_PID = []   # 그 중 NEW_PID 에 대한 것
LIST_CALL_TIMES = []


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
    def submit(self, fn, *args):
        return _InlineFuture(fn, args)


class FakeSession:
    """실제 ezloan.io 응답 형태를 그대로 흉내낸다(2026-08-05 KR egress 실측 기준).

    핵심: /api/rq_addbanner_check 는 '존재하지 않는 글 번호'에도 result:true 를 준다.
    존재 여부를 아는 유일한 읽기 경로는 /rq/{id} 의 크기/마커뿐이고, 쓰기(rq_addbanner)는
    존재하지 않는 번호에 '404 error' 를 준다.
    """

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
            if _page_is_live(pid):
                # 실제 글: 293KB + 배너 마커 (+ 등록 후에는 상호가 1등 슬롯에 보임)
                body = ("배너 등록을 눌러 주세요 js-memberConfirmView "
                        + ("x" * 260000))
                if pid in REGISTERED:
                    body += (f'<a href="/l/585" class="item"><div class="name">'
                             f'{config.COMPANY_NAME}</div>')
                r = FakeResp(200, text=body)
            else:
                # 미존재: 353바이트 alert 스크립트 한 조각
                r = FakeResp(200, text="<script>alert('삭제되었거나 존재하지 않은 문의입니다');"
                                       + ("y" * 280) + "</script>")
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            # 계정 자격만 본다 - 글 존재 여부와 무관하게 통과(실측 동작).
            return FakeResp(200, js={"result": True, "amount": 95})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            ADD_CALL_TIMES.append((FAKE_NOW[0], pid))
            if pid == NEW_PID:
                ADD_CALLS_NEW_PID.append(FAKE_NOW[0])
            if _page_is_live(pid) and pid not in REGISTERED:
                REGISTERED.add(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


REGISTERED = set()


def build_registrar():
    r = eb.Registrar.__new__(eb.Registrar)
    r.s = FakeSession()
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
    r._last_session_save = 1e18
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
    # 글이 뜨는 시각 + 여유 20초까지 돌린다.
    MAX_FAKE_SECONDS = NEW_POST_APPEARS_AT + 20.0

    def fake_wait(seconds):
        FAKE_NOW[0] += max(seconds, 1e-9)
        ticks["n"] += 1

    def should_stop():
        if str(NEW_PID) in r.seen:
            return True
        return FAKE_NOW[0] >= MAX_FAKE_SECONDS

    r._wait = fake_wait
    r.should_stop = should_stop

    orig_time = eb.time.time
    eb.time.time = lambda: FAKE_NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig_time

    print(f"가짜 시계 {FAKE_NOW[0]:.2f}s, tick {ticks['n']}회, "
          f"새 글 등장 {NEW_POST_APPEARS_AT:.2f}s")
    print(f"rq_addbanner(WRITE) 총 {len(ADD_CALL_TIMES)}회 "
          f"(그 중 새 글 {NEW_PID} 대상 {len(ADD_CALLS_NEW_PID)}회)")

    # --- 검증 1: 새 글이 실제로 등록됐다 ---
    assert str(NEW_PID) in r.seen, f"새 글 {NEW_PID} 이 등록되지 않음 (seen={sorted(r.seen)[-5:]})"

    # --- 검증 2: 글이 뜬 뒤 '첫 쓰기'까지의 지연이 tick 오더로 bound 된다 ---
    after = [t for t in ADD_CALLS_NEW_PID if t >= NEW_POST_APPEARS_AT]
    assert after, "글이 뜬 뒤 rq_addbanner 가 한 번도 안 나감"
    lag = after[0] - NEW_POST_APPEARS_AT
    # 백오프 간격(≈4초)이나 목록 주기(1초)에 묶이면 안 된다. tick 몇 번 안쪽이어야 한다.
    bound = config.FRONTIER_POLL_SECONDS * 2 + 1e-6
    backoff_seconds = config.POST_ABSENT_BACKOFF_INTERVAL * config.FRONTIER_POLL_SECONDS
    assert lag <= bound, (
        f"글이 뜬 뒤 등록 발사까지 {lag:.3f}s 걸림 - FRONTIER_POLL_SECONDS 2틱"
        f"({bound:.3f}s)을 초과했다. 백오프({backoff_seconds:.2f}s)나 목록 주기"
        f"({config.LIST_POLL_SECONDS}s)에 묶인 것으로 보인다(v2.5.5 회귀).")
    print(f"[OK] 글이 뜬 뒤 {lag:.3f}s 만에 rq_addbanner 발사 "
          f"(백오프 간격 {backoff_seconds:.2f}s / 목록 주기 {config.LIST_POLL_SECONDS}s 에 "
          f"묶이지 않음).")

    # --- 검증 3: 글이 뜨기 '전'에는 쓰기를 남발하지 않는다(존재 확정 후에만 쓰기) ---
    before = [t for t, _p in ADD_CALL_TIMES if t < NEW_POST_APPEARS_AT]
    assert len(before) == 0, (
        f"글이 존재하지 않는 동안에도 rq_addbanner 를 {len(before)}회 쐈다 - "
        "존재 확정 전 쓰기 발사(v2.5.x 방식)로 되돌아갔다.")
    print(f"[OK] 글이 뜨기 전 대기 {NEW_POST_APPEARS_AT:.1f}초 동안 쓰기(rq_addbanner) 0회 - "
          "존재가 확정된 뒤에만 쓴다(이지론 쓰기 엔드포인트 부담도 함께 제거).")

    # --- 검증 4: 프런티어가 유령 번호를 지나쳐 폭주하지 않았다 ---
    assert not r._post_absent_giveup, (
        f"post_absent GIVEUP 이 발동함: {r._post_absent_giveup} - "
        "존재 확정 전 쓰기를 하지 않으면 이 경로 자체가 발동하면 안 된다.")
    print("[OK] post_absent GIVEUP 미발동 - 프런티어가 유령 번호를 밟지 않음.")

    print("\nALL STEADY-STATE DETECTION-LAG CHECKS PASSED")


if __name__ == "__main__":
    main()
