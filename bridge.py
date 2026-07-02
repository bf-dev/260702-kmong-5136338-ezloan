# -*- coding: utf-8 -*-
"""원격 진단 로그 + 보안문자(캡차) 사장님 중계.

- remote_log: works.insu.ng 게이트웨이로 진단 로그를 비동기 전송(실패해도 앱에 영향 없음).
- OwnerCaptchaBridge: 네이버 보안문자가 뜨면 이미지를 게이트웨이에 올려 사장님께 보이고,
  사장님이 올려준 정답 파일을 폴링해 받아온다. (고객에게는 묻지 않는다.)

  정답 회수 규약:
    프로그램이 캡차를 올릴 때 토큰 T 를 함께 보낸다.
    사장님은 `works/public/<customerId>/captcha_answer.txt` 에
    `T|정답` 또는 그냥 `정답` 을 넣는다. 프로그램은 그 파일을 폴링해 읽고 소비한다.
"""

import threading
import time

import requests

import config

_last = {}
_lock = threading.Lock()
_DEBOUNCE = 10


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def remote_log(event, detail="", snapshot="", force=False):
    with _lock:
        if not force and (time.time() - _last.get(event, 0)) < _DEBOUNCE:
            return
        _last[event] = time.time()

    def _send():
        try:
            payload = {
                "customerId": config.CUSTOMER_ID,
                "source": f"ezloan-desktop-v{config.APP_VERSION}",
                "event": event,
                "detail": detail,
                "ts": now_iso(),
            }
            if snapshot:
                payload["snapshot"] = snapshot[:5000]
            requests.post(config.WORKS_API, json=payload, timeout=8)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


class OwnerCaptchaBridge:
    """사장님이 답하는 보안문자 콜백. NaverLogin(captcha_callback=...) 에 넣는다."""

    def __init__(self, log=print, status=None, timeout=600):
        self.log = log
        self.status = status or (lambda *_: None)
        self.timeout = timeout
        self._counter = 0

    def __call__(self, image_bytes, question):
        self._counter += 1
        token = f"{int(time.time())}-{self._counter}"
        self._upload(image_bytes, question, token)
        self.status("보안문자 확인 중입니다. 잠시만 기다려 주세요...")
        self.log(f"보안문자를 사장님께 전달했습니다. 정답 대기 중... (질문: {question})")
        answer = self._poll_answer(token)
        if answer is None:
            self.log("보안문자 정답을 시간 내에 받지 못했습니다.")
            return {"abort": True}
        self.log("보안문자 정답 수신")
        return {"answer": answer}

    def _upload(self, image_bytes, question, token):
        try:
            files = {"file": (f"captcha-{token}.png", image_bytes or b"", "image/png")}
            data = {
                "customerId": config.CUSTOMER_ID,
                "source": f"ezloan-captcha-v{config.APP_VERSION}",
                "text": f"[네이버 보안문자] token={token} 질문={question} "
                        f"— 정답을 captcha_answer.txt 에 '{token}|정답' 형식으로 올려주세요.",
            }
            requests.post(config.WORKS_API, data=data, files=files, timeout=15)
            remote_log("captcha_uploaded", f"token={token} q={question}", force=True)
        except Exception as e:
            self.log(f"보안문자 업로드 실패(계속 폴링): {e}")

    def _poll_answer(self, token):
        url = f"{config.STATIC_BASE}/captcha_answer.txt"
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=8, headers={"Cache-Control": "no-cache"})
                if r.status_code == 200 and r.text.strip():
                    raw = r.text.strip()
                    # 'token|answer' 는 토큰 일치할 때만, 아니면 그대로 정답
                    if "|" in raw:
                        t, ans = raw.split("|", 1)
                        if t.strip() == token and ans.strip():
                            return ans.strip()
                    else:
                        return raw
            except Exception:
                pass
            time.sleep(3)
        return None
