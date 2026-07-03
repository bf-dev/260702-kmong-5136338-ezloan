# -*- coding: utf-8 -*-
"""자동 업데이트 - 실행 중에도 안전하게 새 버전으로 재시작.

동작:
  1) 60초마다 version-ezloan-desktop.json 을 폴링(백그라운드 스레드, 메인 등록 루프를 절대 막지 않음).
  2) 더 높은 버전이 있으면 새 exe 를 임시 경로에 '완전히' 내려받고 크기(>5MB)로 검증.
  3) 검증되면: 현재 등록 루프에 정지 신호를 보내(진행 중 HTTP 는 깔끔히 마치고 중단),
     현재 세션 쿠키를 디스크에 저장한 뒤, .bat 헬퍼로 exe 스왑+재실행을 예약하고 종료한다.

파일명 규약(중요, Cloudflare 캐시 교훈):
  새 빌드는 항상 버전 접미사가 붙은 파일명(ezloan-desktop-<version>.exe)으로만 배포하고
  version 파일의 exeUrl 이 그 새 경로를 가리키게 한다. 이미 서빙 중인 파일명을 덮어쓰면
  Cloudflare 엣지 캐시(max-age)가 오래된 바이트를 몇 시간 동안 내보내 재시작 루프가 생긴다.
  _check_once() 는 exeUrl 을 매번 동적으로 읽으므로 릴리스마다 코드 변경이 필요 없다.
"""

import os
import subprocess
import sys
import tempfile
import threading

import requests

import config
from bridge import remote_log

MIN_EXE_BYTES = 5_000_000  # 정상 onefile exe 는 20MB+ 이지만, 손상 다운로드만 걸러내면 됨


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


class UpdaterThread(threading.Thread):
    """백그라운드 업데이트 감시 스레드.

    stop_running_loop: 콜백. 새 버전이 준비되면 호출해 등록 루프를 깔끔히 멈춘다.
    snapshot_session: 콜백. 현재 로그인 쿠키(list[dict])를 반환하거나 None.
    status_cb: 사용자 상태 표시 콜백.
    """

    def __init__(self, stop_running_loop, snapshot_session, status_cb=None):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._stop_running_loop = stop_running_loop or (lambda: None)
        self._snapshot_session = snapshot_session or (lambda: None)
        self._status = status_cb or (lambda *_: None)

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception:
                pass  # 업데이트는 부가 기능이라 절대 앱을 죽이면 안 된다.
            self._stop.wait(config.UPDATE_CHECK_SECONDS)

    def _check_once(self):
        try:
            resp = requests.get(config.VERSION_URL, timeout=8,
                                headers={"Cache-Control": "no-cache"})
            if resp.status_code != 200:
                return
            data = resp.json()
        except Exception:
            return
        latest = str(data.get("version", "")).strip()
        exe_url = data.get("exeUrl")
        if not latest or not exe_url:
            return
        if _version_tuple(latest) <= _version_tuple(config.APP_VERSION):
            return

        tmp_path = self._download_verified(exe_url)
        if not tmp_path:
            return

        # 새 exe 검증 완료. 이제 실행 중이라도 안전하게 재시작한다.
        try:
            self._status(f"새 버전({latest})을 내려받았습니다. 세션을 저장하고 재시작합니다...")
            remote_log("update_downloaded",
                       f"{config.APP_VERSION} -> {latest} (exe={exe_url})", force=True)
            # 1) 등록 루프에 정지 신호(진행 중 HTTP 는 마치고 중단).
            try:
                self._stop_running_loop()
            except Exception:
                pass
            # 2) 현재 세션 쿠키 저장 -> 새 프로세스가 재로그인 없이 복구.
            try:
                cookies = self._snapshot_session()
                if cookies:
                    from session_store import save_session
                    save_session(cookies)
                    remote_log("update_session_saved",
                               f"쿠키 {len(cookies)}개 저장(재시작 후 복구)", force=True)
            except Exception:
                pass
            # 3) .bat 헬퍼로 스왑+재실행.
            self._schedule_restart(tmp_path, latest)
        except Exception:
            pass

    def _download_verified(self, exe_url):
        """새 exe 를 임시 경로에 완전히 내려받고 크기 검증. 성공 시 경로, 실패 시 None."""
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".exe")
            os.close(fd)
            total = 0
            with requests.get(exe_url, timeout=60, stream=True,
                              headers={"Cache-Control": "no-cache"}) as r:
                if r.status_code != 200:
                    os.unlink(tmp_path)
                    return None
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
            # Content-Length 가 있으면 완전 다운로드 확인
            expected = r.headers.get("Content-Length")
            if expected and expected.isdigit() and total != int(expected):
                os.unlink(tmp_path)
                remote_log("update_download_incomplete",
                           f"받음={total} 기대={expected}", force=True)
                return None
            if total < MIN_EXE_BYTES:
                os.unlink(tmp_path)
                remote_log("update_too_small", f"받음={total} < {MIN_EXE_BYTES}", force=True)
                return None
            return tmp_path
        except Exception as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            remote_log("update_download_failed", str(e)[:300], force=True)
            return None

    def _schedule_restart(self, new_exe_path, latest):
        current_exe = sys.executable if getattr(sys, "frozen", False) else None
        if not current_exe:
            # 개발 모드(frozen 아님)에서는 스왑 재시작을 건너뛴다.
            remote_log("update_skip_dev", "frozen 아님(개발 실행) - 재시작 생략", force=True)
            return
        current_pid = os.getpid()
        # .bat: 옛 프로세스 종료 대기 -> 새 exe 를 옛 경로에 복사 -> 새 exe 실행 -> 자기 삭제.
        # (실행 중인 exe 파일 잠금 문제를 우회하는 표준 패턴.)
        script = (
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {current_pid}" 2>NUL | find "{current_pid}" >NUL\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >NUL\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'copy /y "{new_exe_path}" "{current_exe}" >NUL\r\n'
            f'start "" "{current_exe}"\r\n'
            'del "%~f0"\r\n'
        )
        fd, bat_path = tempfile.mkstemp(suffix=".bat")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        remote_log("update_restart", f"{config.APP_VERSION} -> {latest} 재시작 예약", force=True)
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        os._exit(0)
