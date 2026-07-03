# -*- coding: utf-8 -*-
"""진입점. 빌드/실행 공통."""

import os
import tkinter as tk

from app import App


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))

    # CI 빌드 검증용: 창을 만들고 바로 닫아 정상 종료를 확인(네트워크/로그인 없음).
    if os.getenv("DIAG_AUTO") == "1":
        root.after(500, root.destroy)
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
