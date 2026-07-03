# -*- coding: utf-8 -*-
"""로그인 세션(쿠키) 디스크 저장 / 복구 / 유효성 검증.

이 신규 앱은 예전 '쿠키 붙여넣기' 앱과 달리 네이버 자동 로그인으로 이지론 세션을
직접 확보한다. 자동 업데이트 재시작이나 프로그램 재실행 때마다 사장님이 매번
네이버 아이디/비밀번호를 다시 넣지 않도록, 로그인으로 얻은 쿠키를 디스크에 저장하고
다음 실행 때 그 쿠키로 바로 requests 세션을 복구한다.

유효성 검증은 ezloan_bot.logged_in() 과 같은 방식으로 /rq 페이지 로그인 표식을 확인한다.
(즉 '404 error' 세션 소실 판정과 동일한 로그인 근거를 쓴다.)
쿠키가 실제로 죽었을 때만 새 네이버 로그인으로 폴백한다.
"""

import json
import time
from pathlib import Path

import config


def save_session(cookies, log=print):
    """로그인 직후/재시작 직전에 호출. driver.get_cookies() 결과(list[dict])를 저장한다."""
    try:
        path = Path(config.SESSION_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "customerId": config.CUSTOMER_ID,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "appVersion": config.APP_VERSION,
            "cookies": cookies or [],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        try:
            log(f"세션 저장 실패(무시하고 계속): {e}")
        except Exception:
            pass
        return False


def load_cookies():
    """저장된 쿠키(list[dict])를 반환. 없으면 None."""
    try:
        path = Path(config.SESSION_FILE)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies")
        if isinstance(cookies, list) and cookies:
            return cookies
        return None
    except Exception:
        return None


def clear_session():
    try:
        Path(config.SESSION_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def validate_saved_session():
    """저장된 쿠키로 requests 세션을 만들어 이지론 로그인 상태인지 확인.

    반환: (cookies, session) 유효하면 튜플, 아니면 (None, None).
    ezloan_bot.logged_in() 과 동일한 로그인 판정을 재사용한다(중복 로직 방지).
    """
    cookies = load_cookies()
    if not cookies:
        return None, None
    try:
        from ezloan_bot import session_from_cookies, logged_in
    except Exception:
        return None, None
    try:
        s = session_from_cookies(cookies)
        if logged_in(s):
            return cookies, s
    except Exception:
        pass
    return None, None
