# -*- coding: utf-8 -*-
"""진입점. 빌드/실행 공통."""

import os
import threading
import tkinter as tk

from app import App


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))

    # CI 빌드 검증용: 창을 만들고 바로 닫아 정상 종료를 확인(네트워크/로그인 없음).
    if os.getenv("DIAG_AUTO") == "1":
        # 일반 실행 경로에서 사용하는 스레드 시작도 패키지된 EXE에서 검증한다.
        probe = threading.Thread(target=lambda: None)
        probe.start()
        probe.join(timeout=2)

        screenshot_path = os.getenv("DIAG_SCREENSHOT")

        def _finish_diagnostic():
            try:
                if screenshot_path:
                    from PIL import ImageGrab

                    root.lift()
                    root.attributes("-topmost", True)
                    root.update_idletasks()
                    x = root.winfo_rootx()
                    y = root.winfo_rooty()
                    width = root.winfo_width()
                    height = root.winfo_height()
                    ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(screenshot_path)
            finally:
                root.destroy()

        root.after(1500 if screenshot_path else 500, _finish_diagnostic)
        root.mainloop()
        print("selftest ok")
        return

    # 시작 직후 저장된 로그인 세션 복구 시도(자동 업데이트 재시작/재실행 후 재로그인 방지).
    # 백그라운드에서 검증하고, 유효하면 등록을 자동 재개한다.
    def _auto_recover():
        try:
            app.try_recover_session()
        except Exception:
            pass
    threading.Thread(target=_auto_recover, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
