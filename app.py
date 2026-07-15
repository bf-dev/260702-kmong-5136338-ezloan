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
from updater import UpdaterThread


class App:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self._stop = threading.Event()
        self.driver = None
        # 현재 로그인 쿠키(업데이트 재시작 시 저장 대상). 로그인/복구 성공 시 채워진다.
        self._cookies = None
        self._cookies_lock = threading.Lock()
        # 강제 재로그인 복구용 자격증명(메모리 전용). id/pw 로 시작한 경우에만 채워진다.
        self._creds = None

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

        # 자동 업데이트는 운영자 지시(2026-07-03)로 비활성화됨.
        # config.AUTO_UPDATE_ENABLED 가 True 일 때만 감시 스레드를 띄운다.
        # 꺼져 있으면 version 폴링/자기 exe 교체를 하지 않고 등록 루프만 돈다.
        self.updater = None
        if getattr(config, "AUTO_UPDATE_ENABLED", False):
            self.updater = UpdaterThread(
                stop_running_loop=self._stop.set,
                snapshot_session=self._snapshot_cookies,
                status_cb=self.set_status,
            )
            self.updater.start()
        else:
            remote_log("auto_update_disabled",
                       f"자동 업데이트 비활성(운영자 지시) 버전 {config.APP_VERSION}",
                       force=True)

    def _snapshot_cookies(self):
        with self._cookies_lock:
            return list(self._cookies) if self._cookies else None

    def _set_cookies(self, cookies):
        with self._cookies_lock:
            self._cookies = list(cookies) if cookies else None

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
    def try_recover_session(self):
        """시작 시 자동 호출: 저장된 쿠키가 유효하면 재로그인 없이 바로 등록을 재개한다.

        자동 업데이트 재시작이나 프로그램 재실행 때마다 네이버 로그인을 다시 하지 않도록,
        이전 세션을 복구한다. 쿠키가 실제로 만료/무효일 때만 로그인 폼으로 폴백한다.
        """
        from session_store import validate_saved_session
        cookies, _sess = validate_saved_session()
        if not cookies:
            remote_log("session_recover_none",
                       "저장된 세션 없음/무효 - 네이버 로그인 필요", force=True)
            return False
        remote_log("session_recovered",
                   f"저장된 세션 유효 - 재로그인 없이 등록 재개(쿠키 {len(cookies)}개)", force=True)
        self._set_cookies(cookies)
        self.set_status("이전 로그인 세션을 복구했습니다. 자동등록을 재개합니다.")
        # 위젯 상태 변경 + 워커 시작은 Tk 스레드에서 수행한다.
        self.root.after(0, self._begin_recovered, cookies)
        return True

    def _begin_recovered(self, cookies):
        self._stop.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.worker = threading.Thread(
            target=self._run_registrar, args=(cookies,), daemon=True)
        self.worker.start()

    def on_start(self):
        nid = self.id_var.get().strip()
        npw = self.pw_var.get()
        if not nid or not npw:
            messagebox.showinfo("알림", "네이버 아이디와 비밀번호를 입력해 주세요.")
            return
        # 강제 재로그인(세션 새로 발급) 복구에 쓰기 위해 자격증명을 메모리에만 보관한다.
        # 디스크/로그에는 절대 저장하지 않는다.
        self._creds = (nid, npw)
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
        from naver_login import NaverLogin, LoginTemporarilyUnavailable

        # 참고: 로그인 일시지연 시의 차분한 안내는 set_status()->_append() 로 이미
        # 스크롤 로그에 남는다. finally 가 상태줄을 "정지됨" 으로 바꿔도(시작 재활성)
        # 그 안내 문장은 로그에 그대로 보이므로 별도 상태줄 플래그는 두지 않는다.
        try:
            self.set_status("전용 크롬 준비 중... (최초 1회 설치, 1~2분)")
            self.driver = build_driver(headless=False, log=self.log)

            self.set_status("네이버 로그인 중...")
            captcha = TkCaptchaHandler(self.root, log=self.log, should_stop=self._stop.is_set)
            login = NaverLogin(self.driver, log=self.log, captcha_callback=captcha,
                               should_stop=self._stop.is_set)
            try:
                ok = login.login(nid, npw)
            except LoginTemporarilyUnavailable as e:
                # 재시도까지 다 소진한 일시 지연/오류. raw 트레이스백으로 앱을 죽이지 않고
                # 차분한 안내만 남긴 뒤, 창을 살려 사용자가 [시작]을 다시 누르게 한다.
                self.set_status(
                    "로그인 페이지가 잠시 느립니다. 이지론/네이버가 일시적으로 지연된 것 같아요. "
                    "잠시 후 [시작]을 다시 눌러 주세요."
                )
                remote_log("login_temporarily_unavailable",
                           f"로그인 재시도 소진(일시 지연/오류). detail={str(e)[:400]}",
                           force=True)
                return
            if not ok:
                self.set_status("로그인 실패. 아이디/비밀번호를 확인해 주세요.")
                remote_log("login_failed", "naver login", force=True)
                return

            self.set_status("로그인 완료! 세션을 확인합니다.")
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
            # 로그인으로 얻은 쿠키를 디스크에 저장 -> 재시작/업데이트 후 재로그인 없이 복구.
            self._set_cookies(cookies)
            if has_sess:
                from session_store import save_session
                save_session(cookies, log=self.log)
                remote_log("session_saved",
                           f"로그인 세션 저장 완료(쿠키 {len(cookies)}개, 재시작 복구용)",
                           force=True)
            # 로그인 후에는 브라우저가 필요 없으므로 닫아 리소스를 아낀다.
            self._quit_driver()

            self._run_registrar(cookies)
        except Exception as e:
            import traceback
            self.set_status(f"오류: {e}")
            remote_log("run_error", traceback.format_exc()[:3000], force=True)
        finally:
            self._quit_driver()
            self.root.after(0, self._finish)
            self.set_status("정지됨")

    def _run_registrar(self, cookies):
        """쿠키로 등록 루프를 돈다(로그인 경로/세션 복구 경로 공용)."""
        import os
        from ezloan_bot import Registrar
        try:
            self.set_status("로그인 완료! 배너 자동등록을 시작합니다.")
            seen_path = os.path.join(config.APP_DIR, "seen-posts.json")
            registrar = Registrar(cookies, log=self.log, remote=remote_log,
                                  should_stop=self._stop.is_set, seen_path=seen_path,
                                  relogin=self._forced_relogin)
            registrar.run()
        except Exception as e:
            import traceback
            self.set_status(f"오류: {e}")
            remote_log("run_error", traceback.format_exc()[:3000], force=True)
        finally:
            self.root.after(0, self._finish)
            self.set_status("정지됨")

    def _forced_relogin(self):
        """강제 재로그인 복구 콜백. Registrar 가 등록 지속 거부 시 호출한다(등록 스레드에서 실행).

        낡은/연장-전 세션 쿠키 가설(H2)을 위한 복구: 디스크의 캐시 세션을 지우고
        네이버->이지론으로 '새' 로그인을 수행해 새 ezloan 세션 쿠키를 받아온다.
        새 cookies(list[dict]) 를 반환하면 Registrar 가 그 세션으로 등록을 이어간다.
        자격증명이 없으면(세션 복구로만 시작한 경우) None 을 돌려주고 기존 세션을 유지한다.
        """
        creds = self._creds
        if not creds:
            remote_log(
                "forced_relogin_no_creds",
                "강제 재로그인 요청됐으나 저장된 자격증명 없음(세션 복구로 시작). "
                "기존 세션 유지. 새로 로그인하려면 [정지] 후 아이디/비밀번호로 [시작].",
                force=True,
            )
            return None
        nid, npw = creds
        from browser import build_driver
        from naver_login import NaverLogin
        from session_store import clear_session, save_session

        driver = None
        try:
            # 낡은 세션이 원인일 수 있으므로 캐시 세션을 먼저 비운다(강제 FRESH 로그인).
            clear_session()
            self.set_status("등록이 계속 거부되어 로그인 세션을 새로 발급받는 중...")
            driver = build_driver(headless=False, log=self.log)
            captcha = TkCaptchaHandler(self.root, log=self.log, should_stop=self._stop.is_set)
            login = NaverLogin(driver, log=self.log, captcha_callback=captcha,
                               should_stop=self._stop.is_set)
            # 캐시 세션을 지웠으므로 ezloan_logged_in() 이 아직 True 를 줄 수 있다(브라우저
            # 프로필에 남은 쿠키). 그래도 login() 은 이지론 세션이 인증돼 있으면 그 세션을
            # 그대로 쓰고, 아니면 네이버 폼으로 새로 로그인한다. 어느 쪽이든 아래에서 이지론
            # 도메인으로 이동해 '지금 유효한' 쿠키를 다시 수집하므로 최신 세션을 확보한다.
            if not login.login(nid, npw):
                remote_log("forced_relogin_login_fail", "네이버 재로그인 실패/취소", force=True)
                return None
            try:
                driver.get(config.RQ_URL)
                import time as _t
                _t.sleep(1.0)
            except Exception:
                pass
            cookies = driver.get_cookies()
            ez = [c for c in cookies if "ezloan" in (c.get("domain") or "")]
            has_sess = any(("ezloan_sess" in (c.get("name") or "") or
                            "ci_session" in (c.get("name") or "")) for c in ez)
            remote_log(
                "forced_relogin_cookies",
                f"재로그인 후 전체쿠키 {len(cookies)}개, ezloan {len(ez)}개, "
                f"ezloan_sess={'있음' if has_sess else '없음'}",
                force=True,
            )
            if not has_sess:
                return None
            self._set_cookies(cookies)
            save_session(cookies, log=self.log)
            return cookies
        except Exception as e:
            import traceback
            remote_log("forced_relogin_error", traceback.format_exc()[:1500], force=True)
            return None
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

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
