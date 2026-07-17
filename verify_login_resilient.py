# -*- coding: utf-8 -*-
"""로그인 복원력 검증: naver_login.py:114(네이버 폼 대기) 의 TimeoutException 이
   앱을 죽이지 않고 재시도 -> 성공, 재시도 소진 시 raw 트레이스백 대신
   LoginTemporarilyUnavailable(차분한 안내) 로 끝나는지 실제로 확인한다.

   2026-07-08 / 2026-07-15 사고 재현: _open_naver_from_ezloan 의 wait.until 이 타임아웃.
   이 테스트는 브라우저 없이(가짜 스텝) login() 의 재시도 루프를 실제로 실행한다.
"""
import sys

from selenium.common.exceptions import TimeoutException, WebDriverException

from naver_login import NaverLogin, LoginTemporarilyUnavailable


class _Harness(NaverLogin):
    """실제 login() 재시도 루프를 그대로 타되, 브라우저를 건드리는 내부 스텝만 가짜로 바꾼다."""

    def __init__(self, fail_first=0, fail_kind="timeout", budget=100.0, **kw):
        super().__init__(driver=object(), **kw)
        # v2.4.5: login() 은 성공/정지/예산소진까지 계속 자동 재시도한다. 실제 벽시계로
        # 검증하면 CI 가 오래 걸리고 불안정하므로, 이 하네스는 '가짜 시계'를 쓴다.
        # _sleep_interruptible 가 호출될 때마다 가짜 시계를 그만큼 앞으로 감아,
        # 대기 예산(budget) 소진 경로를 결정론적으로/즉시 검증한다.
        self.LOGIN_RETRY_BACKOFF = 4.0
        self.LOGIN_LONG_RETRY_WAIT = 45.0
        self.LOGIN_TOTAL_BUDGET = budget
        self._fail_first = fail_first
        self._fail_kind = fail_kind
        self.open_calls = 0
        self.reached_outcome = False
        self._fake_now = 1000.0

    # 가짜 시계: login() 이 참조하는 time.time() 을 결정론적으로 통제한다.
    def _time(self):
        return self._fake_now

    def _sleep_interruptible(self, seconds):
        # 실제로 자지 않고 가짜 시계만 앞으로 감는다(대기 예산이 유한 시간에 소진됨).
        self._fake_now += max(0.0, seconds)

    # login() 이 성공 판정 전 부르는 캐시 세션 체크: 처음부터 로그인돼 있진 않다.
    def ezloan_logged_in(self):
        return False

    def _open_naver_from_ezloan(self):
        self.open_calls += 1
        if self.open_calls <= self._fail_first:
            # 실제 크래시 지점(_open_naver_from_ezloan 내부 wait.until)이 던지던 그 예외.
            if self._fail_kind == "timeout":
                raise TimeoutException(
                    "네이버 로그인 폼이 제때 열리지 않았습니다(네이버/이지론 일시 지연)."
                )
            raise WebDriverException("일시 브라우저 오류")

    def _force_korean(self):
        pass

    def _fill_credentials(self, nid, npw):
        pass

    def _click_login(self):
        pass

    def _outcome_loop(self, nid, npw):
        self.reached_outcome = True
        return True


def _run(name, fn):
    fn()
    print(f"[OK] {name}")


def test_retries_then_succeeds():
    """처음 2번 타임아웃 -> 3번째 시도에서 로그인 성공. 예외 없이 True."""
    h = _Harness(fail_first=2, fail_kind="timeout")
    ok = h.login("someid", "somepw")
    assert ok is True, f"expected True, got {ok!r}"
    assert h.open_calls == 3, f"expected 3 login attempts, got {h.open_calls}"
    assert h.reached_outcome, "did not reach _outcome_loop (real login path not exercised)"


def test_webdriver_error_retries_then_succeeds():
    """WebDriverException(일시 브라우저 오류)도 동일하게 재시도 후 성공."""
    h = _Harness(fail_first=1, fail_kind="webdriver")
    ok = h.login("someid", "somepw")
    assert ok is True
    assert h.open_calls == 2, f"expected 2 attempts, got {h.open_calls}"


def test_exhausted_raises_calm_exception_not_traceback():
    """모든 시도가 타임아웃 -> app.py 를 죽이는 raw TimeoutException 이 아니라
       LoginTemporarilyUnavailable 로 끝나야 한다(호출부가 차분한 안내로 변환).

       v2.4.5: login() 은 4번 만에 포기하지 않고 예산(LOGIN_TOTAL_BUDGET)까지
       계속 자동 재시도한다. 그래서 (a) 초기 짧은 재시도(LOGIN_ATTEMPTS)보다 많이
       시도했고 (b) 예산 소진 후에야 차분한 예외로 끝나는지 확인한다."""
    h = _Harness(fail_first=99999, fail_kind="timeout", budget=600.0)
    try:
        h.login("someid", "somepw")
    except LoginTemporarilyUnavailable:
        pass  # 기대한 결과
    except TimeoutException as e:
        raise AssertionError(
            "raw TimeoutException 이 그대로 튀어나옴 -> 앱이 여전히 크래시함: " + str(e)
        )
    else:
        raise AssertionError("예외 없이 통과 -> 재시도 소진 처리 안 됨")
    # 4번 만에 포기하지 않고(v2.4.4 회귀 방지) 초기 재시도 횟수보다 더 시도했어야 한다.
    assert h.open_calls > h.LOGIN_ATTEMPTS, (
        f"expected more than {h.LOGIN_ATTEMPTS} auto-retries before giving up, "
        f"got {h.open_calls} (v2.4.4 처럼 너무 일찍 포기함)"
    )


def test_keeps_retrying_past_initial_attempts_then_succeeds():
    """v2.4.5 핵심: 초기 짧은 재시도(LOGIN_ATTEMPTS=6)를 넘겨 8번째에 성공해도
       [시작] 재클릭 없이 자동으로 성공해야 한다(수동 재시도 제거)."""
    h = _Harness(fail_first=7, fail_kind="timeout", budget=100000.0)
    ok = h.login("someid", "somepw")
    assert ok is True, f"expected True, got {ok!r}"
    assert h.open_calls == 8, f"expected 8 attempts (7 fail + 1 ok), got {h.open_calls}"
    assert h.open_calls > h.LOGIN_ATTEMPTS, "did not retry past the initial short-retry window"


def test_app_run_converts_to_calm_status_and_survives():
    """app.py 의 _run 이 실제로 LoginTemporarilyUnavailable 을 잡아
       (a) run_error/traceback 을 남기지 않고
       (b) 차분한 한국어 상태 안내를 남기고
       (c) 앱 창을 살려 둔 채(return) 끝나는지 확인."""
    import app as app_mod

    statuses = []
    remote_events = []

    # remote_log 를 가로채 run_error(크래시 로그) 가 안 남는지 확인.
    orig_remote = app_mod.remote_log
    app_mod.remote_log = lambda ev, msg="", **k: remote_events.append((ev, msg))

    class FakeStop:
        def is_set(self):
            return False

    class FakeApp:
        pass

    fake = FakeApp.__new__(app_mod.App)
    fake._stop = FakeStop()
    fake.set_status = lambda t: statuses.append(t)
    fake.log = lambda t: None
    fake._quit_driver = lambda: None
    fake.root = type("R", (), {"after": staticmethod(lambda *a, **k: None)})()
    fake._finish = lambda: None

    # build_driver / NaverLogin / captcha 를 login() 이 곧장 소진-예외를 던지도록 대체.
    import browser
    import naver_login as nl
    orig_build = browser.build_driver
    browser.build_driver = lambda **k: object()

    class AlwaysUnavailable:
        def __init__(self, *a, **k):
            pass
        def login(self, *a, **k):
            raise nl.LoginTemporarilyUnavailable("로그인 페이지 지연(테스트)")

    orig_nl = nl.NaverLogin
    nl.NaverLogin = AlwaysUnavailable
    # app._run 은 captcha 핸들러도 만든다: 무해한 더미로.
    orig_captcha = app_mod.TkCaptchaHandler
    app_mod.TkCaptchaHandler = lambda *a, **k: None

    try:
        app_mod.App._run(fake, "someid", "somepw")
    finally:
        browser.build_driver = orig_build
        nl.NaverLogin = orig_nl
        app_mod.remote_log = orig_remote
        app_mod.TkCaptchaHandler = orig_captcha

    # (a) run_error(raw 트레이스백) 이 남지 않았어야 한다.
    assert not any(ev == "run_error" for ev, _ in remote_events), (
        "run_error 가 기록됨 -> 여전히 크래시 경로로 감: " + repr(remote_events)
    )
    # login_temporarily_unavailable 이벤트로 차분하게 기록됐어야 한다.
    assert any(ev == "login_temporarily_unavailable" for ev, _ in remote_events), (
        "차분한 login_temporarily_unavailable 이벤트가 없음: " + repr(remote_events)
    )
    # (b) 사용자에게 '잠시 후 [시작]을 다시' 계열 안내가 상태로 남아야 한다.
    joined = " | ".join(statuses)
    assert "시작" in joined and ("느립니다" in joined or "지연" in joined), (
        "차분한 재시도 안내 상태가 없음: " + joined
    )


if __name__ == "__main__":
    _run("retries then succeeds (timeout x2 -> ok)", test_retries_then_succeeds)
    _run("retries then succeeds (webdriver error x1 -> ok)",
         test_webdriver_error_retries_then_succeeds)
    _run("keeps auto-retrying past initial window -> succeeds (no manual re-click)",
         test_keeps_retrying_past_initial_attempts_then_succeeds)
    _run("exhausted -> LoginTemporarilyUnavailable (no raw traceback, no crash)",
         test_exhausted_raises_calm_exception_not_traceback)
    _run("app._run catches it -> calm status, no run_error, app survives",
         test_app_run_converts_to_calm_status_and_survives)
    print("\nALL LOGIN-RESILIENCE CHECKS PASSED: "
          "TimeoutException at the login step now retries and never crashes the run.")
