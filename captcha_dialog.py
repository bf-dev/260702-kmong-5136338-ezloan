# -*- coding: utf-8 -*-
"""네이버 보안문자(캡차) 입력 창 - Tkinter.

네이버가 로그인 중 보안문자를 요구하면, 크롬 창을 직접 만지지 않고 이 작은 창에서
[이미지 + 질문 + 정답 입력] 으로 처리한다. 기본 한국어.

워커(백그라운드) 스레드에서 호출되므로, 실제 창 생성/조작은 Tk 메인 스레드에서만
하도록 root.after 로 마샬링하고, 워커는 이벤트로 결과를 기다린다.
반환: {"answer": str} / {"reload": True} / {"abort": True}
"""

import base64
import threading
import tkinter as tk


class TkCaptchaHandler:
    """NaverLogin(captcha_callback=...) 에 넣는 콜러블."""

    def __init__(self, root, log=print, should_stop=None):
        self.root = root
        self.log = log
        self.should_stop = should_stop or (lambda: False)

    def __call__(self, image_bytes, question):
        result = {}
        done = threading.Event()
        self.root.after(0, self._show, image_bytes, question, result, done)
        # 워커 스레드: 사용자가 답할 때까지 대기(중지 시 취소)
        while not done.wait(0.3):
            if self.should_stop():
                return {"abort": True}
        return result

    def _show(self, image_bytes, question, result, done):
        win = tk.Toplevel(self.root)
        win.title("네이버 보안문자 입력")
        win.attributes("-topmost", True)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(
            win,
            text="네이버 보안문자(캡차)가 나왔습니다.\n아래 이미지를 보고 질문에 답해 주세요.",
            justify="left",
        ).pack(padx=16, pady=(14, 8), anchor="w")

        photo = self._make_photo(image_bytes)
        if photo is not None:
            lbl = tk.Label(win, image=photo, bd=1, relief="solid")
            lbl.image = photo  # keep ref
            lbl.pack(padx=16, pady=4)
        else:
            tk.Label(win, text="(이미지를 불러오지 못했습니다)", fg="#a00").pack(padx=16, pady=4)

        tk.Label(win, text=question or "이미지의 글자를 입력해 주세요.",
                 fg="#0b57d0", font=("", 11, "bold"), wraplength=380, justify="left").pack(
            padx=16, pady=(6, 2), anchor="w")

        var = tk.StringVar()
        entry = tk.Entry(win, textvariable=var, width=34)
        entry.pack(padx=16, pady=6)
        entry.focus_set()

        row = tk.Frame(win)
        row.pack(padx=16, pady=(4, 14), fill="x")

        def submit(_evt=None):
            ans = var.get().strip()
            if not ans:
                return
            result["answer"] = ans
            _close()

        def reload():
            result["reload"] = True
            _close()

        def abort():
            result["abort"] = True
            _close()

        def _close():
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass
            done.set()

        entry.bind("<Return>", submit)
        win.protocol("WM_DELETE_WINDOW", abort)
        tk.Button(row, text="새 문자", width=10, command=reload).pack(side="left")
        tk.Button(row, text="확인", width=12, command=submit, default="active").pack(side="right")

    @staticmethod
    def _make_photo(image_bytes):
        """네이버 보안문자 이미지는 보통 JPEG 라 Tk 기본 PhotoImage(PNG/GIF만) 로는 안 열린다.
        Pillow 로 어떤 포맷이든 디코딩해 Tk 이미지로 만든다(없으면 PNG 한정 폴백)."""
        if not image_bytes:
            return None
        # 1) Pillow (JPEG/PNG/무엇이든)
        try:
            import io
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(image_bytes))
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            if im.width > 400:
                ratio = 380.0 / im.width
                im = im.resize((380, max(1, int(im.height * ratio))))
            return ImageTk.PhotoImage(im)
        except Exception:
            pass
        # 2) 폴백: PNG 만 되는 Tk 기본 디코더
        try:
            photo = tk.PhotoImage(data=base64.b64encode(image_bytes).decode("ascii"))
            w = photo.width()
            if w > 400:
                factor = max(1, w // 380)
                if factor > 1:
                    photo = photo.subsample(factor, factor)
            return photo
        except Exception:
            return None
