# -*- coding: utf-8 -*-
"""v2.6.1 회귀 게이트: '사이트 42초 장애'가 등록 스레드를 죽이면 안 된다.

재현 대상 사고 (고객 5136338, 2026-08-22 04:55~04:56 UTC, 앱 v2.5.5):
  works.insu.ng 인제스트 로그에 그대로 남아 있는 실제 순서다.
    04:55:31  [frontier_resync] frontier=31985 > 실제최신(30104)+1+창 -> frontier=30105
              (원본이 죽자 Cloudflare 가 '한참 전에 캐시된 목록'을 200 으로 내려줬다)
    04:55:31  [register_skip] post=30103 status=521 note=check_http_error
    04:55:41  [register_skip] post=30088 status=521 note=check_http_error
    04:55:52  [register_skip] post=30103 status=521 note=check_http_error
    04:56:03  [register_skip] post=30103 status=521 note=check_http_error
    04:56:13  [register_skip] post=30088 status=521 note=check_http_error
    04:56:28  [run_error] urllib3.exceptions.ReadTimeoutError:
              HTTPSConnectionPool(host='ezloan.io', port=443): Read timed out. (read timeout=12)
              ... ezloan_bot.py run -> _handle -> register -> _check
    04:56:38  [session_expired] loop 예외 후 세션 무효 확인      <- 그리고 스레드 종료
  이지론은 1분 안에 회복했지만 앱은 17시간 57분 동안 '정지됨' 으로 서 있었고 그 사이의
  모든 글을 놓쳤다. run_stopped 로그가 한 줄도 없는 것이 위 `return` 으로 빠져나갔다는 증거다.

이 스크립트는 그 순서를 그대로 태운다: 521 x5 -> ReadTimeout -> 42초 뒤 사이트 정상 ->
그 다음에 새 글이 뜬다. 그리고 아래를 확인한다.
  1) 등록 루프가 살아남아 회복 후의 새 글을 실제로 등록한다.
  2) session_expired / session_invalid 가 한 번도 나가지 않는다(세션은 멀쩡했으므로).
  3) 장애 중에도 강제 재로그인(크롬 창)을 부르지 않는다.
  4) 장애 로그가 인제스트를 도배하지 않는다(42초 장애에 site_unreachable 몇 줄).
  5) 캐시된 옛 목록 때문에 프런티어가 되감기지 않고, 2주 전 옛 글에 쓰기를 쏘지 않는다.

가짜 시계로 돌린다(_wait 가 실제로 자지 않고 시계만 전진). 네트워크 접근 없음.
v2.6.0 이하 코드에서는 1)/2) 에서 실패한다.
"""
import requests

import config
import ezloan_bot as eb

REAL_MAX = 31984          # 사고 시점의 실제 최신 글
NEW_PID = REAL_MAX + 1    # 사이트 회복 후 뜨는 새 글 (= 31985)
EXISTING = set(range(REAL_MAX - 9, REAL_MAX + 1))
# Cloudflare 가 장애 중에 대신 내려준 '캐시된 옛 목록'(실측: 최신 글 30104).
STALE_IDS = list(range(30095, 30105))

OUTAGE_AT = 5.0           # 이 시각부터 사이트가 죽는다
OUTAGE_SECONDS = 42.0     # 실측 장애 길이(04:55:31 ~ 04:56:13 + 타임아웃)
OUTAGE_521_CALLS = 5      # 실측: 521 이 5번 찍힌 뒤 read timeout 이 났다
TIMEOUT_TAIL_SECONDS = 8.0  # 장애 마지막 8초는 read timeout 구간(실측 traceback)
NEW_POST_AFTER_RECOVERY = 8.0

FAKE_NOW = [0.0]
REGISTERED = set()
ADD_CALLS = []            # (시각, pid) - 실제로 나간 rq_addbanner(WRITE)
EVENTS = []               # remote() 로 나간 (event, detail)
STATUSES = []             # GUI 상태줄로 올라간 문구
RELOGIN_CALLS = [0]
STATE = {"http521": 0}


def _outage_now():
    return OUTAGE_AT <= FAKE_NOW[0] < OUTAGE_AT + OUTAGE_SECONDS


def _page_is_live(pid):
    if pid in EXISTING:
        return True
    if pid == NEW_PID:
        return FAKE_NOW[0] >= OUTAGE_AT + OUTAGE_SECONDS + NEW_POST_AFTER_RECOVERY
    return False


def _read_timeout():
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='ezloan.io', port=443): Read timed out. (read timeout=12)")


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


def _list_page(ids, logged_in=True):
    links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
    if logged_in:
        # 로그인 상태의 목록 페이지(실측 마커).
        return "로그아웃 광고 관리 " + links
    # 비로그인 목록 페이지(2026-08-23 KR egress 실측 마커). Cloudflare 가 장애 중
    # 내려주는 캐시본이 정확히 이 형태다 - 로그인 흔적이 없다.
    return ('<a href="https://ezloan.io/m/login" class="log in flex" title="로그인">'
            '<span>로그인</span></a><!-- // 비로그인 { -->' + links)


class FakeSession:
    """장애 중/후의 ezloan.io 를 실측 로그 그대로 흉내낸다."""

    def __init__(self):
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        if _outage_now():
            # 실측 순서(2026-08-22 로그): 먼저 521 이 몇 번 찍히는 구간, 그 다음 read timeout.
            # 그리고 그 내내 목록 URL 은 Cloudflare 가 캐시해 둔 '비로그인 옛 페이지'를
            # 200 으로 대신 내려준다(그래서 목록=20 인데 최신 글이 30104 였다).
            if FAKE_NOW[0] < OUTAGE_AT + OUTAGE_SECONDS - TIMEOUT_TAIL_SECONDS:
                if url.rstrip("/") == config.RQ_URL.rstrip("/"):
                    r = FakeResp(200, text=_list_page(STALE_IDS, logged_in=False))
                    r.url = url
                    return r
                STATE["http521"] += 1
                return FakeResp(521, text="<html>Web server is down (Error 521)</html>",
                                ctype="text/html")
            # 장애 후반: 목록까지 포함해 전부 read timeout(실측 traceback 그대로).
            raise _read_timeout()

        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            live = sorted(
                (i for i in list(EXISTING) + ([NEW_PID] if _page_is_live(NEW_PID) else [])),
                reverse=True)[: config.MAX_POSTS]
            r = FakeResp(200, text=_list_page(live))
            r.url = url
            return r
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            if _page_is_live(pid):
                body = "배너 등록을 눌러 주세요 js-memberConfirmView " + ("x" * 260000)
                if pid in REGISTERED:
                    body += (f'<a href="/l/585" class="item"><div class="name">'
                             f'{config.COMPANY_NAME}</div>')
                r = FakeResp(200, text=body)
            else:
                r = FakeResp(200, text="<script>alert('삭제되었거나 존재하지 않은 문의입니다');"
                                       + ("y" * 280) + "</script>")
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 491})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            ADD_CALLS.append((FAKE_NOW[0], pid))
            if _page_is_live(pid) and pid not in REGISTERED:
                REGISTERED.add(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def build_registrar():
    r = eb.Registrar.__new__(eb.Registrar)
    r.s = FakeSession()
    r.probe = FakeSession()
    r._probe_pool = InlinePool()
    r._last_check = None
    r._cookies_raw = []
    r._diag_sent = False
    r.log = lambda *a, **k: None
    r.status = lambda text: STATUSES.append(text)
    r.remote = lambda event, detail="", **k: EVENTS.append((event, detail))

    def _relogin():
        RELOGIN_CALLS[0] += 1
        return None
    r.relogin = _relogin
    r.seen_path = None
    # 재시작이 아니라 '이미 몇 시간째 돌고 있던 상태'를 재현한다.
    r.seen = {str(i) for i in EXISTING}
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
    r.status = lambda text: STATUSES.append(text)
    r._site_down_since = None
    r._site_down_reported = 0.0
    r._site_down_polls = 0
    r._relogin_attempts = 0
    r._max_live_id = 0
    r._stale_list_streak = 0
    r._post_absent_pid = None
    r._post_absent_streak = 0
    r._post_absent_giveup = set()
    r._last_heavy_tick = 0.0
    return r


def main():
    r = build_registrar()
    ticks = {"n": 0}
    new_post_at = OUTAGE_AT + OUTAGE_SECONDS + NEW_POST_AFTER_RECOVERY
    max_fake_seconds = new_post_at + 60.0

    def fake_wait(seconds):
        FAKE_NOW[0] += max(seconds, 1e-9)
        ticks["n"] += 1

    def should_stop():
        if str(NEW_PID) in r.seen and NEW_PID in REGISTERED:
            return True
        return FAKE_NOW[0] >= max_fake_seconds

    r._wait = fake_wait
    r.should_stop = should_stop

    orig_time = eb.time.time
    eb.time.time = lambda: FAKE_NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig_time

    names = [e for e, _d in EVENTS]
    print(f"가짜 시계 {FAKE_NOW[0]:.2f}s, tick {ticks['n']}회 "
          f"(장애 {OUTAGE_AT:.0f}~{OUTAGE_AT + OUTAGE_SECONDS:.0f}s, "
          f"새 글 {NEW_PID} 등장 {new_post_at:.0f}s)")
    print(f"이벤트: {len(EVENTS)}건, site_unreachable={names.count('site_unreachable')}, "
          f"site_recovered={names.count('site_recovered')}, "
          f"session_expired={names.count('session_expired')}, "
          f"run_error={names.count('run_error')}, "
          f"stale_list={names.count('stale_list')}, "
          f"frontier_resync={names.count('frontier_resync')}")

    # --- 검증 1: 42초 장애가 등록 스레드를 죽이지 않았고, 회복 후 새 글을 등록했다 ---
    assert NEW_PID in REGISTERED, (
        f"사이트 회복 후 뜬 새 글 {NEW_PID} 이 등록되지 않았다. "
        f"42초 장애로 run() 이 빠져나간 것으로 보인다(2026-08-22 사고 재현). "
        f"events={names[-6:]}")
    first_write = min(t for t, p in ADD_CALLS if p == NEW_PID)
    resume_lag = first_write - new_post_at
    assert resume_lag <= 1.0, (
        f"새 글이 뜬 뒤 {resume_lag:.2f}초 만에야 등록했다 - 사이트가 회복됐는데도 앱이 "
        "장애 백오프에 계속 앉아 있었다는 뜻이다(회복 즉시 정상 주기로 복귀해야 한다).")
    print(f"[OK] 42초 장애를 견디고 회복 뒤 새 글 {NEW_PID} 을 등록했다 "
          f"(글 등장 {new_post_at:.1f}s -> 발사 {first_write:.2f}s, 지연 {resume_lag:.2f}s).")

    # --- 검증 2: 세션은 멀쩡했으므로 session_expired 계열이 나가면 안 된다 ---
    for bad in ("session_expired", "session_invalid", "relogin_exhausted"):
        assert names.count(bad) == 0, (
            f"'{bad}' 가 {names.count(bad)}회 나갔다 - 사이트 장애(521/타임아웃)를 "
            "세션 사망으로 오판한 것이다. 이게 2026-08-22 사고의 직접 원인이었다.")
    print("[OK] session_expired / session_invalid / relogin_exhausted 0건 "
          "- 응답 실패를 세션 사망으로 읽지 않는다.")

    # --- 검증 3: 장애 중 크롬 재로그인 창을 띄우지 않았다 ---
    assert RELOGIN_CALLS[0] == 0, (
        f"강제 재로그인 콜백이 {RELOGIN_CALLS[0]}회 호출됐다 - 사이트 장애 중에는 "
        "고객 PC 에 크롬 로그인 창을 띄우면 안 된다(로그인도 어차피 실패한다).")
    print("[OK] 장애 중 강제 재로그인 0회.")

    # --- 검증 4: 장애를 인지해 알렸고, 그 로그가 인제스트를 도배하지 않았다 ---
    assert names.count("site_unreachable") >= 1, "사이트 장애를 한 번도 보고하지 않았다."
    assert names.count("site_unreachable") <= 5, (
        f"42초 장애에 site_unreachable 을 {names.count('site_unreachable')}회 보냈다 - "
        "인제스트를 도배한다(SITE_RETRY_REPORT_SECONDS 로 제한돼야 한다).")
    assert names.count("site_recovered") == 1, (
        f"사이트 회복 로그가 {names.count('site_recovered')}건 - 정확히 1건이어야 한다.")
    assert any("사이트 응답 없음" in s for s in STATUSES), (
        f"GUI 상태줄에 '사이트 응답 없음' 안내가 올라가지 않았다(statuses={STATUSES}).")
    print(f"[OK] site_unreachable {names.count('site_unreachable')}건 + site_recovered 1건, "
          f"상태줄 안내 '{[s for s in STATUSES if '사이트 응답 없음' in s][0]}'.")

    # --- 검증 5: 캐시된 옛 목록에 속아 프런티어를 되감거나 옛 글에 쓰지 않았다 ---
    assert names.count("stale_list") >= 1, (
        "캐시된 옛 목록(최신=30104)을 받고도 낡은 목록으로 인지하지 못했다 - "
        "재현 자체가 성립하지 않았거나 방어가 없다.")
    assert names.count("frontier_resync") == 0, (
        "캐시된 옛 목록(최신=30104)을 진짜로 믿고 프런티어를 되감았다 - "
        "실제 사고 로그의 'frontier=31985 -> 30105' 재현.")
    old_writes = [(t, p) for t, p in ADD_CALLS if p < 31000]
    assert not old_writes, (
        f"2주 전 옛 글에 rq_addbanner 를 {len(old_writes)}회 쐈다: {old_writes[:5]} - "
        "캐시된 목록을 안전망이 '새 글'로 오인한 것이다(배너 잔여 낭비).")
    print("[OK] 캐시된 옛 목록으로 프런티어 되감기 0회, 옛 글 쓰기 0회 "
          f"(stale_list {names.count('stale_list')}건으로 걸러냄).")

    print("\nALL SITE-OUTAGE RESILIENCE CHECKS PASSED")


if __name__ == "__main__":
    main()
