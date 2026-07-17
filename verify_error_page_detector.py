# -*- coding: utf-8 -*-
"""_ezloan_error_page() 오탐/미탐 검증 (v2.4.5).

2026-07-17 사고 조사: ezloan.io 는 정상이었고(라이브 확인: HTTP 200, /m/login 에
'네이버로 로그인' 버튼 존재, error marker 0개), 로그인 실패는 일시 지연이었다.
그럼에도 예전 detector 의 맨숭한 "500" marker 는 '500만원' 같은 정상 문구에
false-positive 로 걸릴 수 있었다. 이 테스트는:
  1) 로그인 버튼이 있으면 어떤 문구든 오류로 보지 않는다(오탐 차단)
  2) 진짜 Whoops/점검 오류 페이지(버튼 없음)는 오류로 잡는다(미탐 방지)
  3) '500만원' 같은 정상 숫자 문구는 오류로 보지 않는다
를 가짜 드라이버로 실제 detector 코드를 태워 확인한다.
"""
import sys

from selenium.common.exceptions import NoSuchElementException

from naver_login import NaverLogin


class _El:
    def __init__(self, text=""):
        self.text = text


class _FakeDriver:
    """find_element 만 흉내: naver 버튼 존재 여부 + body.text 를 통제한다."""
    def __init__(self, body_text="", has_naver_btn=True):
        self._body = body_text
        self._btn = has_naver_btn

    def find_element(self, by, sel):
        s = str(sel)
        if "js-loginBtn" in s and "naver" in s:
            if self._btn:
                return _El("네이버로 로그인")
            raise NoSuchElementException("no naver button")
        if "body" in s.lower() or by == "tag name":
            return _El(self._body)
        # 태그명 body 는 By.TAG_NAME 로 들어온다
        if s == "body":
            return _El(self._body)
        raise NoSuchElementException(s)


def _det(body_text, has_naver_btn):
    nl = NaverLogin(driver=_FakeDriver(body_text, has_naver_btn), log=lambda *_: None)
    return nl._ezloan_error_page()


def _run(name, fn):
    fn()
    print(f"[OK] {name}")


def test_normal_login_page_with_button_is_not_error():
    """정상 로그인 페이지: 버튼 존재 -> 오류 아님(문구 무관)."""
    body = "로그인 네이버로 로그인 실시간 대출 문의 소액대출 500만원 당일대출"
    assert _det(body, has_naver_btn=True) is False, "정상 로그인 페이지를 오류로 오탐함"


def test_500_number_in_text_is_not_error_when_button_present():
    """'500만원' 같은 정상 숫자 문구는 버튼이 있으면 오류로 보지 않는다(핵심 오탐 차단)."""
    body = "최대 500만원 즉시 대출 로그인 네이버로 로그인"
    assert _det(body, has_naver_btn=True) is False, "'500만원' 을 500-서버오류로 오탐함"


def test_500_number_alone_without_button_is_not_error():
    """버튼도 없고 '500' 도 오류 맥락(error/server/http/오류) 없이 있으면 오류 아님."""
    body = "최대 500만원 즉시 대출"
    assert _det(body, has_naver_btn=False) is False, "맥락 없는 500 을 오류로 오탐함"


def test_whoops_page_without_button_is_error():
    """진짜 Whoops 오류 페이지(버튼 없음)는 오류로 잡는다(미탐 방지)."""
    body = "whoops, looks like something went wrong. try again later."
    assert _det(body, has_naver_btn=False) is True, "진짜 Whoops 오류를 놓침"


def test_maintenance_page_without_button_is_error():
    """점검 중 페이지(버튼 없음)는 오류로 잡는다."""
    body = "서비스 점검 중입니다. 잠시 후 다시 이용해 주세요."
    assert _det(body, has_naver_btn=False) is True, "점검 페이지를 놓침"


def test_http500_server_error_without_button_is_error():
    """'500' + 오류 맥락(server error) & 버튼 없음 -> 오류."""
    body = "http 500 internal server error"
    assert _det(body, has_naver_btn=False) is True, "500 server error 를 놓침"


if __name__ == "__main__":
    _run("normal login page (has button) -> NOT error", test_normal_login_page_with_button_is_not_error)
    _run("'500만원' with button -> NOT error (false-positive guard)", test_500_number_in_text_is_not_error_when_button_present)
    _run("bare 500 no context, no button -> NOT error", test_500_number_alone_without_button_is_not_error)
    _run("real Whoops page (no button) -> error", test_whoops_page_without_button_is_error)
    _run("maintenance page (no button) -> error", test_maintenance_page_without_button_is_error)
    _run("http 500 server error (no button) -> error", test_http500_server_error_without_button_is_error)
    print("\nALL ERROR-PAGE DETECTOR CHECKS PASSED: "
          "no false-positive on the real login page, still catches real outages.")
