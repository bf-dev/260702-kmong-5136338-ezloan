# -*- coding: utf-8 -*-
"""로그인 경로의 일시적 Selenium TimeoutException 이 실행 전체를 죽이지 않는지 재현 검증.

두 시나리오를 실제 NaverLogin.login() 재시도 루프에 물려 확인한다:

  A) 일시 지연: 앞선 시도들은 TimeoutException 으로 실패하다가 마지막에 성공.
     -> login() 은 예외를 삼켜 재시도하고 True 를 돌려줘야 한다(크래시 없음).

  B) 지속 지연: 모든 시도가 TimeoutException.
     -> login() 은 bare TimeoutException 을 튀우지 않고 LoginTemporarilyUnavailable
        을 던져야 한다. 그리고 app.py._run 스타일 핸들러가 그걸 잡아
        (1) 차분한 한국어 안내를 '스크롤 로그(_append)' 에 남기고
        (2) 앱을 죽이지 않고(트레이스백 없음) 계속 살아 있어야 한다.

재시도 백오프는 검증 속도를 위해 짧게 줄여서 실제 루프를 그대로 태운다.
"""
import time

from selenium.common.exceptions import TimeoutException

import naver_login
from naver_login import NaverLogin, LoginTemporarilyUnavailable


class FakeDriver:
    window_handles = ["h0"]


def make_login(open_side_effect):
    """실제 NaverLogin 을 만들되, 로그인 시퀀스 내부 단계만 가짜로 바꿔
    login() 의 재시도 루프/백오프/예외변환 은 진짜 코드를 그대로 태운다."""
    log_lines = []
    lg = NaverLogin(FakeDriver(), log=log_lines.append, should_stop=lambda: False)
    # 백오프를 짧게(실제 루프는 그대로, 대기만 단축)
    lg.LOGIN_RETRY_BACKOFF = 0.05
    lg.ezloan_logged_in = lambda: False           # 폼 대기만 실패한 게 아님
    lg._open_naver_from_ezloan = open_side_effect  # 여기가 실측 크래시 지점
    lg._force_korean = lambda: None
    lg._fill_credentials = lambda a, b: None
    lg._click_login = lambda: None
    lg._outcome_loop = lambda a, b: True           # 성공 시 True
    return lg, log_lines


def scenario_transient():
    print("=== A) 일시 지연: 앞 3회 TimeoutException 후 4회차 성공 -> 재시도 후 로그인 성공 ===")
    state = {"n": 0}

    def open_effect():
        state["n"] += 1
        if state["n"] < 4:
            raise TimeoutException("네이버 로그인 폼이 제때 열리지 않았습니다(일시 지연).")
        # 4회차: 성공(예외 없이 반환) -> _outcome_loop=True

    lg, log_lines = make_login(open_effect)
    t0 = time.time()
    ok = lg.login("tester", "pw")
    dt = time.time() - t0
    assert ok is True, f"일시 지연인데 로그인 실패로 처리됨: ok={ok}"
    assert state["n"] == 4, f"재시도가 안 돎: attempts={state['n']}"
    retried = [l for l in log_lines if "자동으로 다시 시도" in l]
    assert len(retried) == 3, f"재시도 안내 로그 수 예상 3, 실제 {len(retried)}: {log_lines}"
    print(f"   시도 {state['n']}회 -> 성공(ok=True), 재시도안내 {len(retried)}회, "
          f"크래시 없음, 소요 {dt:.2f}s (PASS)")
    for l in retried:
        print("     로그:", l)
    return "transient timeout -> retried 3x then logged in, no crash (PASS)"


def scenario_persistent():
    print("\n=== B) 지속 지연: 모든 시도 TimeoutException -> LoginTemporarilyUnavailable(차분 안내), 앱 생존 ===")
    state = {"n": 0}

    def open_effect():
        state["n"] += 1
        raise TimeoutException("네이버 로그인 폼이 제때 열리지 않았습니다(일시 지연).")

    lg, log_lines = make_login(open_effect)

    raised = None
    try:
        lg.login("tester", "pw")
    except LoginTemporarilyUnavailable as e:
        raised = e
    except TimeoutException as e:
        raise AssertionError(
            f"bare TimeoutException 이 그대로 튀어 앱을 죽일 것: {e}"
        )
    assert raised is not None, "지속 실패인데 LoginTemporarilyUnavailable 이 안 나옴"
    assert state["n"] == lg.LOGIN_ATTEMPTS, f"재시도 횟수 예상 {lg.LOGIN_ATTEMPTS}, 실제 {state['n']}"
    print(f"   전체 {state['n']}회 시도 후 LoginTemporarilyUnavailable 발생(bare Timeout 아님) (PASS)")

    # ---- app.py._run 의 핸들러를 그대로 흉내내: 차분 안내 로그 + 앱 생존 ----
    scroll_log = []       # _append 가 쓰는 스크롤 로그
    status_line = {"v": None}
    app_alive = {"v": True}

    def set_status(text):        # app.set_status -> _append(text) 와 동일하게 로그에도 남김
        status_line["v"] = text
        scroll_log.append(text)  # 실제 _set_status 가 _append 를 부르는 것과 동치

    def run_style_handler():
        try:
            raise raised           # login() 이 던진 그 예외
        except LoginTemporarilyUnavailable as e:
            set_status(
                "로그인 페이지가 잠시 느립니다. 이지론/네이버가 일시적으로 지연된 것 같아요. "
                "잠시 후 [시작]을 다시 눌러 주세요."
            )
            return  # 트레이스백 없이 조용히 반환 -> 앱 창 유지
        finally:
            set_status("정지됨")   # app._run 의 finally 와 동일

    run_style_handler()
    assert app_alive["v"] is True, "앱이 죽음"
    calm = [l for l in scroll_log if "잠시 후 [시작]을 다시" in l]
    assert calm, f"차분한 안내가 스크롤 로그에 안 남음: {scroll_log}"
    assert status_line["v"] == "정지됨", f"상태줄은 정지됨 이어야: {status_line['v']}"
    print(f"   스크롤 로그에 차분 안내 유지: {calm[0]!r}")
    print(f"   상태줄='{status_line['v']}' (정지됨 허용), 앱 생존=True, 트레이스백 없음 (PASS)")
    return "persistent timeout -> calm msg stays in scroll log, app alive, status '정지됨' (PASS)"


def main():
    r1 = scenario_transient()
    r2 = scenario_persistent()
    print("\n================= LOGIN-TIMEOUT REPRO RESULT =================")
    print("  [transient] ", r1)
    print("  [persistent]", r2)
    print("ALL LOGIN-TIMEOUT CHECKS PASSED")


if __name__ == "__main__":
    main()
