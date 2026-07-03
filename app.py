# -*- coding: utf-8 -*-
"""이지론 배너 자동등록 - 데스크탑 GUI (Tkinter).

아이디/비밀번호를 넣고 [시작]을 누르면:
  전용 크롬 준비(최초 1회 자동설치) -> 네이버 자동 로그인(보안문자는 사장님이 처리)
  -> 이지론 세션 확보 -> 실시간 배너 등록 루프.
크롬 창은 로그인 동안만 열리고, 로그인 후에는 순수 HTTP 로 등록을 돌린다.
"""

import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox

import config
from bridge import remote_log
from captcha_dialog import TkCaptchaHandler


class App:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self._stop = threading.Event()
        self.driver = None

        root.title(f"이지론 배너 자동등록 v{config.APP_VERSION}")
        root.geometry("520x430")
        root.minsize(480, 400)

        pad = {"padx": 10, "pady": 4}
        form = tk.Frame(root)
        form.pack(fill="x", **pad)

        tk.Label(form, text="네이버 아이디", width=12, anchor="w").grid(row=0, column=0, sticky="w", pady=4)
        self.id_var = tk.StringVar()
        tk.Entry(form, textvariable=self.id_var, width=32).grid(row=0, column=1, sticky="we", pady=4)

        tk.Label(form, text="비밀번호", width=12, anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self.pw_var = tk.StringVar()
        tk.Entry(form, textvariable=self.pw_var, width=32, show="*").grid(row=1, column=1, sticky="we", pady=4)
        form.columnconfigure(1, weight=1)

        note = tk.Label(
            root,
            text="첫 실행 시 전용 크롬을 자동 설치합니다(1~2분). 크롬 미설치 PC도 동작합니다.\n"
                 "네이버 보안문자가 나오면 작은 입력창이 뜹니다. 사진을 보고 답을 입력해 주세요.",
            fg="#888", justify="left", anchor="w",
        )
        note.pack(fill="x", padx=10)

        btns = tk.Frame(root)
        btns.pack(fill="x", **pad)
        self.start_btn = tk.Button(btns, text="시작", width=14, height=1, command=self.on_start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = tk.Button(btns, text="정지", width=14, height=1, command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="대기 중")
        tk.Label(root, textvariable=self.status_var, fg="#2563eb", anchor="w").pack(fill="x", padx=10, pady=2)

        self.log_box = scrolledtext.ScrolledText(root, height=13, state=tk.DISABLED,
                                                 font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=6)

        remote_log("app_started", f"버전 {config.APP_VERSION}", force=True)

    # ---- UI thread helpers ----------------------------------------------
    def set_status(self, text):
        self.root.after(0, self._set_status, text)

    def _set_status(self, text):
        self.status_var.set(text)
        self._append(text)

    def log(self, text):
        self.root.after(0, self._append, text)

    def _append(self, text):
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    # ---- events ----------------------------------------------------------
    def on_start(self):
        nid = self.id_var.get().strip()
        npw = self.pw_var.get()
        if not nid or not npw:
            messagebox.showinfo("알림", "네이버 아이디와 비밀번호를 입력해 주세요.")
            return
        self._stop.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.worker = threading.Thread(target=self._run, args=(nid, npw), daemon=True)
        self.worker.start()

    def on_stop(self):
        self._stop.set()
        self.set_status("정지 요청... 정리 중입니다.")
        self.stop_btn.config(state=tk.DISABLED)

    def _finish(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    # ---- worker ----------------------------------------------------------
    def _run(self, nid, npw):
        from browser import build_driver
        from naver_login import NaverLogin
        from ezloan_bot import Registrar
        import os

        try:
            self.set_status("전용 크롬 준비 중... (최초 1회 설치, 1~2분)")
            self.driver = build_driver(headless=False, log=self.log)

            self.set_status("네이버 로그인 중...")
            captcha = TkCaptchaHandler(self.root, log=self.log, should_stop=self._stop.is_set)
            login = NaverLogin(self.driver, log=self.log, captcha_callback=captcha,
                               should_stop=self._stop.is_set)
            ok = login.login(nid, npw)
            if not ok:
                self.set_status("로그인 실패. 아이디/비밀번호를 확인해 주세요.")
                remote_log("login_failed", "naver login", force=True)
                return

            self.set_status("로그인 완료! 배너 자동등록을 시작합니다.")
            # 등록 API 는 이지론(ezloan.io) 세션 쿠키로 인증한다.
            # 네이버 도메인 쿠키만 있고 ezloan_sess 가 없으면 이후 등록이 전부 실패하므로,
            # 로그인 직후 반드시 이지론 도메인으로 이동한 뒤 그 도메인의 쿠키를 수집한다.
            try:
                self.driver.get(config.RQ_URL)
                import time as _t
                _t.sleep(1.0)
            except Exception:
                pass
            cookies = self.driver.get_cookies()
            ez = [c for c in cookies if "ezloan" in (c.get("domain") or "")]
            names = sorted({c.get("name", "") for c in ez})
            has_sess = any("ezloan_sess" in n or "ci_session" in n for n in names)
            remote_log(
                "login_success",
                f"전체쿠키 {len(cookies)}개, ezloan도메인 {len(ez)}개, "
                f"ezloan_sess={'있음' if has_sess else '없음'}, 쿠키명={names[:20]}",
                force=True,
            )
            if not has_sess:
                remote_log(
                    "login_no_ezloan_session",
                    "네이버 로그인은 됐으나 ezloan.io 세션 쿠키가 없음. "
                    "등록 API 인증이 전부 실패할 것으로 예상됨.",
                    force=True,
                )
            # 로그인 후에는 브라우저가 필요 없으므로 닫아 리소스를 아낀다.
            self._quit_driver()

            seen_path = os.path.join(config.APP_DIR, "seen-posts.json")
            registrar = Registrar(cookies, log=self.log, remote=remote_log,
                                  should_stop=self._stop.is_set, seen_path=seen_path)
            registrar.run()
        except Exception as e:
            import traceback
            self.set_status(f"오류: {e}")
            remote_log("run_error", traceback.format_exc()[:3000], force=True)
        finally:
            self._quit_driver()
            self.root.after(0, self._finish)
            self.set_status("정지됨")

    def _quit_driver(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def close(self):
        self._stop.set()
        self._quit_driver()
