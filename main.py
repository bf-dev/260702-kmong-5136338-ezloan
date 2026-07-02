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

    root.mainloop()


if __name__ == "__main__":
    main()
