# -*- coding: utf-8 -*-
"""이지론 네이버 자동 로그인 모듈.

이지론의 "네이버로 로그인"은 네이버 로그인 창(팝업/리다이렉트)으로 이어진다.
이 모듈은 ezloan.io/m/login -> 네이버 로그인 버튼 -> 아이디/비밀번호 자동 입력 ->
(필요 시) 보안문자 처리 -> 로그인 성공 판정(이지론 세션 확보)까지 담당한다.

보안문자(캡차):
  - 네이버가 로그인 실패시키며 보안문자를 요구하면 비밀번호가 지워진다.
    따라서 보안문자 답을 넣을 때 아이디/비밀번호를 다시 채워서 함께 제출한다.
  - 실제 정답은 captcha_callback(image_bytes, question)으로 외부(사장님/GUI)에 물어본다.
    반환은 dict: {"answer": str, "reload": bool, "abort": bool}

성공 판정:
  - 네이버 로그인 성공 시 팝업 창이 닫히고 제어가 이지론 창으로 돌아온다.
    그래서 성공은 '네이버 폼을 벗어났는가'가 아니라 '이지론 세션이 인증됐는가'로 본다.

주의(실측): 네이버 아이디 칸에는 '@naver.com'을 붙이지 않은 순수 아이디를 넣어야 한다.
"""

import base64
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException,
)

import config


class LoginTemporarilyUnavailable(Exception):
    """로그인 페이지가 (재시도까지 다 소진하도록) 일시적으로 느리거나 오류였음.

    이건 우리 앱 버그가 아니라 이지론/네이버 측 일시 지연/오류이므로,
    호출부(app.py)는 이 예외를 raw 트레이스백으로 죽이지 말고 차분한 한국어 안내로
    바꿔 사용자에게 '잠시 후 [시작]을 다시' 안내하고 앱 창은 살려 둔다.
    """


class NaverLogin:
    def __init__(self, driver, log=print, captcha_callback=None, should_stop=None):
        self.d = driver
        self.log = log
        self.captcha_callback = captcha_callback
        self.should_stop = should_stop or (lambda: False)

    # ---- public ----------------------------------------------------------
    # 로그인 페이지(이지론 로그인 폼 / 네이버 로그인 폼)가 순간적으로 느려
    # Selenium wait 이 TimeoutException 을 던지면, 예전(v2.4.3 이하)에는 그게
    # 그대로 튀어 app.py 의 run_error 로 잡히며 앱 실행이 통째로 멈췄다.
    # (2026-07-08, 2026-07-15 사고: naver_login.py:114 의 네이버 폼 대기 타임아웃)
    # 실제로는 재시작하면 곧 잘 되는 '일시적 지연'이므로, 여기서 로그인 시퀀스
    # (이지론 로그인 페이지 열기 -> 네이버 폼 -> 자격증명 -> 제출)을 몇 번 재시도한다.
    # v2.4.5: 예전(v2.4.4)에는 4번 짧게 재시도하고 포기한 뒤 사용자에게 [시작]을
    # 다시 누르라고 안내했다. 황금연휴처럼 이지론/네이버가 순간적으로 느릴 때는
    # 이 4번이 1분 안에 소진돼(백오프 4/8/12/16초), 실제로는 사이트가 곧 회복되는데도
    # 고객이 [시작]을 몇 번씩 손으로 다시 눌러야 했다(2026-07-17 사고: 고객이
    # "계속 다시 해보니 됐네용" 이라며 결국 수동 재시도로 로그인 성공).
    # 이제는 '수동 재클릭'을 없앤다: 짧은 재시도를 여러 번 한 뒤에도 안 되면
    # 더 긴 간격으로 계속 자동 재시도하며 사이트 회복을 스스로 기다린다.
    # 오직 (a) 로그인 성공, (b) 사용자가 [정지]를 누름, (c) 전체 대기 예산 소진
    # 셋 중 하나에서만 멈춘다.
    LOGIN_ATTEMPTS = 6              # 짧은 백오프로 도는 초기 재시도 횟수
    LOGIN_RETRY_BACKOFF = 4.0      # 초기 재시도 사이 기본 대기(초). 시도마다 늘린다.
    LOGIN_LONG_RETRY_WAIT = 45.0   # 초기 재시도 소진 후, 긴 간격 자동 재시도 주기(초)
    LOGIN_TOTAL_BUDGET = 1200.0    # 자동 재시도 총 예산(초, 기본 20분). 이후엔 안내하고 멈춤

    def login(self, naver_id, naver_pw):
        """이지론 -> 네이버 로그인 전체 플로우. 성공하면 True.

        일시적 TimeoutException(페이지 지연/오류)은 삼켜서 자동 재시도한다.
        초기 짧은 재시도(LOGIN_ATTEMPTS)를 소진해도 포기하지 않고, 더 긴 간격으로
        총 대기 예산(LOGIN_TOTAL_BUDGET)까지 계속 스스로 재시도한다. 그래서 사용자가
        [시작]을 손으로 다시 누를 필요가 없다. 예산까지 소진하면 그때서야
        LoginTemporarilyUnavailable 을 던져 차분한 안내를 하도록 한다.
        """
        if self.ezloan_logged_in():
            self.log("이미 로그인되어 있습니다.")
            return True

        clock = self._time  # 테스트에서 가짜 시계로 교체 가능(기본은 time.time).
        deadline = clock() + self.LOGIN_TOTAL_BUDGET
        last_err = None
        attempt = 0
        while clock() < deadline:
            if self.should_stop():
                self.log("중지 요청으로 로그인 중단")
                return False
            attempt += 1
            try:
                self._open_naver_from_ezloan()
                self._force_korean()
                self._fill_credentials(naver_id, naver_pw)
                self._click_login()
                return self._outcome_loop(naver_id, naver_pw)
            except (TimeoutException, WebDriverException) as e:
                # 중지 요청으로 인한 중단은 그대로 실패 처리(재시도 무의미).
                if self.should_stop():
                    self.log("중지 요청으로 로그인 중단")
                    return False
                last_err = e
                # 이미 이지론 세션이 서 있으면(폼 대기만 실패한 경우) 성공으로 본다.
                if self._safe(self.ezloan_logged_in):
                    self.log("네이버 로그인 폼 대기는 지연됐지만 이지론 세션이 확인됨 ✅")
                    return True
                # 초기엔 짧은 백오프, 그 뒤로는 긴 간격으로 '계속' 자동 재시도.
                if attempt < self.LOGIN_ATTEMPTS:
                    wait_s = self.LOGIN_RETRY_BACKOFF * attempt
                    self.log(
                        f"로그인 페이지가 잠시 느립니다({attempt}/{self.LOGIN_ATTEMPTS}). "
                        f"{int(wait_s)}초 후 자동으로 다시 시도합니다..."
                    )
                else:
                    wait_s = self.LOGIN_LONG_RETRY_WAIT
                    remaining = int(max(0, deadline - clock()))
                    self.log(
                        "이지론/네이버가 잠시 지연되는 것 같습니다. 사이트가 회복될 때까지 "
                        f"{int(wait_s)}초 간격으로 계속 자동 재시도합니다(남은 대기 약 {remaining//60}분). "
                        "[시작]을 다시 누르실 필요는 없습니다."
                    )
                # 다음 재시도가 대기 예산을 넘기면 더 돌지 않는다.
                if clock() + wait_s >= deadline:
                    break
                self._sleep_interruptible(wait_s)
                continue

        raise LoginTemporarilyUnavailable(str(last_err) if last_err else "로그인 페이지 지연")

    def _time(self):
        """login() 재시도 예산에 쓰는 시계. 테스트에서 가짜 시계로 교체 가능."""
        return time.time()

    def _sleep_interruptible(self, seconds):
        """중지 요청이 오면 즉시 깨어나는 대기."""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(0.5)

    def ezloan_logged_in(self):
        """이지론 창으로 전환해 인증 여부 확인."""
        if not self._ensure_window():
            return False
        target = None
        for h in list(self.d.window_handles):
            try:
                self.d.switch_to.window(h)
                if "ezloan.io" in (self.d.current_url or ""):
                    target = h
                    break
            except WebDriverException:
                continue
        if target is None and self.d.window_handles:
            self.d.switch_to.window(self.d.window_handles[0])
        try:
            self.d.get(config.RQ_URL)
            time.sleep(1.5)
            body = self.d.find_element(By.TAG_NAME, "body").text
            return ("로그아웃" in body or "광고 관리" in body) and "로그인 해주세요" not in body
        except WebDriverException:
            return False

    # ---- steps -----------------------------------------------------------
    def _open_naver_from_ezloan(self):
        # 이지론 로그인 페이지를 열고 "네이버로 로그인" 버튼을 누른다.
        # ezloan.io 가 일시적 서버 오류("Whoops! ... hit a snag" 류 500 페이지)를
        # 내려주면 버튼이 아예 없어 Selenium 이 15초 후 TimeoutException 을 던졌고,
        # 그게 그대로 튀어 앱이 원인 불명으로 멈췄다(2026-07-08 사고).
        # 이건 우리 앱 버그가 아니라 사이트 측 일시 오류이므로, 몇 번 새로고침하며
        # 회복을 기다리고, 그래도 안 되면 '사이트 일시 오류'라고 명확히 알린다.
        self.log("이지론 로그인 페이지 이동...")
        before = set(self.d.window_handles)
        btn = None
        for attempt in range(1, 5):  # 최대 4회(≈ 로드+대기 반복)
            if self.should_stop():
                raise WebDriverException("중지 요청으로 로그인 중단")
            self.d.get(config.LOGIN_URL)
            time.sleep(1.5)
            if self._ezloan_error_page():
                self.log(f"이지론 사이트 일시 오류 페이지 감지({attempt}/4). 잠시 후 다시 시도합니다...")
                time.sleep(4)
                continue
            btn = self._safe(lambda: WebDriverWait(self.d, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '.js-loginBtn[data-type="naver"]'))
            ))
            if btn is not None:
                break
            self.log(f"네이버 로그인 버튼을 찾지 못했습니다({attempt}/4). 페이지를 다시 불러옵니다...")
            time.sleep(3)
        if btn is None:
            # 여기서 던진 TimeoutException 은 상위 login() 자동 재시도 루프가 잡아
            # 더 긴 간격으로 계속 스스로 다시 시도한다(사용자 [시작] 재클릭 불필요).
            raise TimeoutException(
                "이지론 로그인 페이지가 정상적으로 열리지 않았습니다. "
                "이지론(ezloan.io) 사이트가 일시적으로 오류/점검 중일 수 있습니다."
            )
        btn.click()
        time.sleep(2.5)
        new = [h for h in self.d.window_handles if h not in before]
        if new:
            self.d.switch_to.window(new[-1])
        # 네이버 로그인 폼(#id) 이 뜰 때까지 대기. 여기서의 TimeoutException 이
        # 바로 2026-07-08/07-15 크래시 지점이었다. 이제는 login() 재시도 루프가
        # 이 예외를 잡아 이지론 로그인 페이지부터 다시 시도하므로, 여기서는
        # 명확한 메시지를 실어 TimeoutException 을 던지기만 한다.
        try:
            WebDriverWait(self.d, 20).until(
                lambda b: self._safe(lambda: b.find_element(By.ID, "id")) is not None
            )
        except TimeoutException:
            raise TimeoutException(
                "네이버 로그인 폼이 제때 열리지 않았습니다(네이버/이지론 일시 지연)."
            )
        self.log("네이버 로그인 폼 확인")

    def _ezloan_error_page(self):
        """지금 이지론 페이지가 서버 오류 페이지(Whoops!/snag 류)인지 판정.

        Laravel/CI 기본 오류 페이지는 로그인 폼 대신 짧은 안내문만 보여 준다.
        정상 로그인 페이지에는 항상 '네이버로 로그인' 버튼(.js-loginBtn[data-type=naver])이
        있으므로, 그 버튼이 있으면 어떤 문구가 보이든 오류 페이지로 보지 않는다
        (false-positive 방지). 버튼이 없을 때만 오류 문구로 판정한다.

        주의(2026-07-17 실측): 정상 /m/login 의 rendered text 에는 error marker 가
        하나도 없다(네이버 버튼 3개 존재). 예전 marker 목록의 맨숭한 "500" 은
        "500만원" 같은 정상 문구에도 걸릴 수 있어(false-positive), 오류 맥락과
        함께 나올 때만(server error / http 500 류) 오류로 본다.
        """
        # 로그인 버튼이 실제로 있으면 정상 페이지다(문구 무관). 오탐 차단.
        if self._safe(lambda: self.d.find_element(
                By.CSS_SELECTOR, '.js-loginBtn[data-type="naver"]')) is not None:
            return False
        try:
            body = (self._safe(lambda: self.d.find_element(By.TAG_NAME, "body").text) or "").lower()
        except WebDriverException:
            return False
        if not body:
            return False
        # 명확한 오류 문구.
        strong_markers = ("whoops", "hit a snag", "try again later",
                          "server error", "점검 중", "서비스 점검", "일시적으로 중단")
        if any(m in body for m in strong_markers):
            return True
        # 맨숭한 숫자 '500' 은 오류 맥락(error/server/http)과 함께일 때만 오류로 본다.
        if "500" in body and any(k in body for k in ("error", "server", "http", "오류")):
            return True
        return False

    def _force_korean(self):
        """네이버 로그인 UI(헤더 + 보안문자)를 한국어로 강제."""
        try:
            changed = self.d.execute_script(
                """
                var sels=document.querySelectorAll('select#locale_pop, select[name="locale"], .lang_area select, select');
                for (var s=0;s<sels.length;s++){var sel=sels[s];
                  for (var i=0;i<sel.options.length;i++){
                    var t=(sel.options[i].text||'')+(sel.options[i].value||'');
                    if (t.indexOf('한국어')>=0 || (sel.options[i].value||'').toLowerCase().indexOf('ko')>=0){
                      sel.selectedIndex=i; sel.dispatchEvent(new Event('change',{bubbles:true})); return 'select';
                    }}}
                var links=document.querySelectorAll('a');
                for (var j=0;j<links.length;j++){ if((links[j].textContent||'').indexOf('한국어')>=0){links[j].click(); return 'link';}}
                var lc=document.getElementById('locale'); if(lc){lc.value='ko_KR';}
                return 'none';
                """
            )
            if changed in ("select", "link"):
                time.sleep(2)
        except WebDriverException:
            pass

    def _fill_credentials(self, naver_id, naver_pw):
        idv = self._strip_naver_id(naver_id)
        self._type_secure(self.d.find_element(By.ID, "id"), idv)
        self._type_secure(self.d.find_element(By.ID, "pw"), naver_pw)
        self.log("아이디/비밀번호 입력 완료")

    def _refill_credentials(self, naver_id, naver_pw):
        idv = self._strip_naver_id(naver_id)
        try:
            id_el = self.d.find_element(By.ID, "id")
            if (id_el.get_attribute("value") or "") != idv:
                self._type_secure(id_el, idv)
        except NoSuchElementException:
            pass
        try:
            self._type_secure(self.d.find_element(By.ID, "pw"), naver_pw)  # pw는 항상 지워짐
        except NoSuchElementException:
            pass

    @staticmethod
    def _strip_naver_id(naver_id):
        v = (naver_id or "").strip()
        if v.lower().endswith("@naver.com"):
            v = v[: -len("@naver.com")]
        return v

    def _type_secure(self, element, text):
        try:
            element.click()
        except WebDriverException:
            pass
        try:
            element.clear()
        except WebDriverException:
            pass
        for ch in text:
            element.send_keys(ch)
            time.sleep(0.04)

    def _click_login(self):
        btn = None
        for how, sel in [
            (By.ID, "log.login"),
            (By.CSS_SELECTOR, "button.btn_login"),
            (By.CSS_SELECTOR, 'button[type="submit"]'),
        ]:
            try:
                btn = self.d.find_element(how, sel)
                break
            except NoSuchElementException:
                continue
        if btn is None:
            raise TimeoutException("네이버 로그인 버튼을 찾지 못했습니다.")
        try:
            btn.click()
        except WebDriverException:
            self.d.execute_script("arguments[0].click();", btn)
        self.log('"로그인" 제출')

    # ---- outcome ---------------------------------------------------------
    def _outcome_loop(self, naver_id, naver_pw):
        deadline = time.time() + 900
        captcha_tries = 0
        while time.time() < deadline:
            if self.should_stop():
                self.log("중지 요청으로 로그인 중단")
                return False
            time.sleep(1.2)

            # 팝업이 닫혔으면 이지론 인증 여부로 성공 판정
            if not self._ensure_window():
                if self.ezloan_logged_in():
                    self.log("네이버 로그인 성공 ✅ (이지론 인증 확인)")
                    return True
                self.log("창이 모두 닫혔고 로그인도 안 됨")
                return False

            url = self._safe(lambda: self.d.current_url) or ""

            # 보안문자
            if self._captcha_present():
                if captcha_tries >= 8:
                    self.log("보안문자 시도 횟수 초과 - 중단")
                    return False
                captcha_tries += 1
                if not self._handle_captcha(naver_id, naver_pw, captcha_tries):
                    return False
                continue

            # 새 기기 등록 안내 -> 등록 안 함
            if self._click_if_present([
                (By.ID, "new.dontsave"),
                (By.XPATH, '//a[contains(.,"등록안함")]'),
                (By.XPATH, '//button[contains(.,"등록안함")]'),
            ], "기기 등록 건너뜀"):
                continue

            # 정보제공 동의(있다면)
            if self._click_if_present([
                (By.XPATH, '//button[contains(.,"동의하기")]'),
                (By.XPATH, '//a[contains(.,"동의하기")]'),
                (By.XPATH, '//button[contains(.,"전체 동의")]'),
            ], "정보제공 동의"):
                continue

            # 로그인 에러
            err = self._error_message()
            if err:
                self.log(f"네이버 로그인 실패: {err}")
                return False

            # 네이버 폼을 벗어났으면 이지론 인증 확인
            if "naver.com" not in url:
                if self.ezloan_logged_in():
                    self.log("네이버 로그인 성공 ✅")
                    return True

        self.log("네이버 로그인 시간 초과")
        return False

    def _captcha_present(self):
        for how, sel in [(By.ID, "captchaimg"), (By.CSS_SELECTOR, ".captcha_wrap"),
                         (By.ID, "rcapt"), (By.ID, "captcha")]:
            el = self._safe(lambda how=how, sel=sel: self.d.find_element(how, sel))
            if el is not None and self._safe(lambda el=el: el.is_displayed()):
                return True
        return False

    def _handle_captcha(self, naver_id, naver_pw, attempt):
        if self.captcha_callback is None:
            self.log("⚠️ 보안문자 처리기가 없습니다.")
            return False
        image_bytes = self._captcha_image_bytes()
        question = self._captcha_question()
        self.log(f"⚠️ 네이버 보안문자({attempt}회): {question}")

        result = self.captcha_callback(image_bytes, question) or {}
        if result.get("abort"):
            self.log("보안문자 처리 취소")
            return False
        if result.get("reload"):
            self._reload_captcha()
            return True
        answer = (result.get("answer") or "").strip()
        if not answer:
            self.log("보안문자 정답이 비어 있음")
            return False
        self._refill_credentials(naver_id, naver_pw)
        try:
            self._type_secure(self.d.find_element(By.ID, "captcha"), answer)
        except NoSuchElementException:
            self.log("보안문자 입력칸을 찾지 못했습니다.")
            return False
        self.log("보안문자 정답 입력 후 재제출")
        self._click_login()
        return True

    def _captcha_image_bytes(self):
        try:
            img = self.d.find_element(By.ID, "captchaimg")
            src = img.get_attribute("src") or ""
            if src.startswith("data:image"):
                return base64.b64decode(src.split(",", 1)[1])
            return img.screenshot_as_png
        except Exception:
            try:
                return self.d.get_screenshot_as_png()
            except Exception:
                return b""

    def _captcha_question(self):
        for how, sel in [(By.ID, "captcha_info"), (By.CSS_SELECTOR, ".captcha_message"),
                         (By.CSS_SELECTOR, ".captcha_desc")]:
            el = self._safe(lambda how=how, sel=sel: self.d.find_element(how, sel))
            if el is not None:
                txt = self._safe(lambda el=el: el.text) or ""
                if txt.strip():
                    return txt.strip()
        return "이미지의 정답을 입력해 주세요."

    def _reload_captcha(self):
        try:
            self.d.find_element(By.ID, "reload").click()
            time.sleep(1.0)
        except Exception:
            pass

    def _error_message(self):
        for how, sel in [(By.CSS_SELECTOR, ".error_message"), (By.ID, "err_common"),
                         (By.CSS_SELECTOR, ".login_error_wrap .error_message")]:
            el = self._safe(lambda how=how, sel=sel: self.d.find_element(how, sel))
            if el is not None and self._safe(lambda el=el: el.is_displayed()):
                txt = (self._safe(lambda el=el: el.text) or "").strip()
                # 보안문자 안내문은 에러로 취급하지 않음
                if txt and "자동입력 방지" not in txt and "보안문자" not in txt:
                    return txt
        return ""

    # ---- helpers ---------------------------------------------------------
    def _ensure_window(self):
        try:
            _ = self.d.current_url
            return True
        except WebDriverException:
            pass
        for h in list(self.d.window_handles):
            try:
                self.d.switch_to.window(h)
                _ = self.d.current_url
                return True
            except WebDriverException:
                continue
        return False

    def _click_if_present(self, selectors, log_msg):
        for how, sel in selectors:
            el = self._safe(lambda how=how, sel=sel: self.d.find_element(how, sel))
            if el is not None and self._safe(lambda el=el: el.is_displayed()):
                try:
                    el.click()
                except WebDriverException:
                    self._safe(lambda el=el: self.d.execute_script("arguments[0].click();", el))
                self.log(log_msg)
                time.sleep(1.0)
                return True
        return False

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None
