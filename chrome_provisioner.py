# -*- coding: utf-8 -*-
"""프로그램이 직접 관리하는 크롬(Chrome for Testing) 준비 모듈.

사용자 PC에 크롬이 설치되어 있지 않아도, 프로그램이 자기 전용 크롬 바이너리와
그에 딱 맞는 chromedriver 를 내려받아(최초 1회) 캐시에 두고 그걸로 구동한다.
Windows / macOS(Intel·Apple Silicon) / Linux 모두 동일 코드로 동작.

이렇게 하면
  - "설치된 크롬 버전"과 chromedriver 버전이 어긋나는 문제가 사라지고,
  - macOS 의 "Google Chrome for Testing이(가) 예기치 않게 종료되었습니다" (격리 속성)
    문제를 xattr 제거로 예방한다.
"""

import io
import json
import os
import platform
import stat
import subprocess
import sys
import zipfile
from urllib.request import urlopen, Request

import config

CFT_ENDPOINT = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)


def _platform_key():
    """Chrome for Testing 플랫폼 키 반환."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return "win64" if sys.maxsize > 2 ** 32 else "win32"
    if system == "Darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    return "linux64"


def _binary_rel_paths(plat):
    """(chrome 실행파일, chromedriver 실행파일) 상대경로."""
    if plat.startswith("win"):
        cdir = "chrome-win64" if plat == "win64" else "chrome-win32"
        ddir = "chromedriver-win64" if plat == "win64" else "chromedriver-win32"
        return (
            os.path.join(cdir, "chrome.exe"),
            os.path.join(ddir, "chromedriver.exe"),
        )
    if plat.startswith("mac"):
        return (
            os.path.join(
                f"chrome-{plat}",
                "Google Chrome for Testing.app",
                "Contents", "MacOS", "Google Chrome for Testing",
            ),
            os.path.join(f"chromedriver-{plat}", "chromedriver"),
        )
    return (
        os.path.join("chrome-linux64", "chrome"),
        os.path.join("chromedriver-linux64", "chromedriver"),
    )


def _fetch_json(url):
    req = Request(url, headers={"User-Agent": "kream-airpods-provisioner"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _download_and_extract(url, dest_dir, log):
    log(f"다운로드: {url.rsplit('/', 1)[-1]}")
    req = Request(url, headers={"User-Agent": "kream-airpods-provisioner"})
    with urlopen(req, timeout=180) as r:
        data = r.read()
    log(f"압축 해제 중... ({len(data) // (1024 * 1024)}MB)")
    # zipfile.extractall 은 유닉스 실행권한을 버린다 -> crashpad_handler 등 헬퍼가
    # 실행 불가가 되어 크롬이 "예기치 않게 종료"된다. external_attr 로 권한 복원.
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            extracted = z.extract(info, dest_dir)
            mode = (info.external_attr >> 16) & 0o7777
            if mode:
                try:
                    os.chmod(extracted, mode)
                except Exception:
                    pass


def _chmod_tree_executable(root):
    """추출된 크롬 트리의 모든 파일에 실행권한(+x)을 보장.

    크롬 앱은 crashpad_handler, 각종 헬퍼 실행파일에 +x 가 필요하다.
    데이터 파일에 +x 가 붙어도 무해하므로 전체에 부여한다.
    """
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            _make_executable(os.path.join(dirpath, name))


def _make_executable(path):
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _strip_quarantine_mac(app_root, log):
    """macOS: 다운로드 격리 속성 제거 -> '예기치 않게 종료' 예방."""
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", app_root],
            check=False, capture_output=True,
        )
        log("macOS 격리 속성 제거 완료")
    except Exception as e:
        log(f"격리 속성 제거 건너뜀: {e}")


def ensure_chrome(log=print):
    """크롬/드라이버를 준비하고 (chrome_path, driver_path, version) 반환.

    이미 캐시에 있으면 즉시 반환, 없으면 내려받는다.
    """
    plat = _platform_key()
    chrome_rel, driver_rel = _binary_rel_paths(plat)

    # 최신 Stable 버전 조회 (실패 시 캐시에 남은 버전 재사용)
    version = None
    downloads = None
    try:
        info = _fetch_json(CFT_ENDPOINT)
        stable = info["channels"]["Stable"]
        version = stable["version"]
        downloads = stable["downloads"]
    except Exception as e:
        log(f"버전 정보 조회 실패({e}) - 캐시된 크롬을 찾습니다.")

    # 캐시에서 사용 가능한 버전 탐색
    def _paths_for(ver):
        base = os.path.join(config.CHROME_CACHE_DIR, ver)
        return (
            os.path.join(base, chrome_rel),
            os.path.join(base, driver_rel),
            base,
        )

    if version is None:
        # 오프라인 폴백: 캐시 폴더 중 유효한 것 사용
        if os.path.isdir(config.CHROME_CACHE_DIR):
            for ver in sorted(os.listdir(config.CHROME_CACHE_DIR), reverse=True):
                cp, dp, _ = _paths_for(ver)
                if os.path.exists(cp) and os.path.exists(dp):
                    _make_executable(cp)
                    _make_executable(dp)
                    log(f"캐시된 크롬 사용: {ver}")
                    return cp, dp, ver
        raise RuntimeError("크롬을 내려받을 수 없고 캐시도 없습니다. 인터넷 연결을 확인하세요.")

    chrome_path, driver_path, base = _paths_for(version)
    if os.path.exists(chrome_path) and os.path.exists(driver_path):
        _make_executable(chrome_path)
        _make_executable(driver_path)
        log(f"크롬 준비 완료(캐시): {version}")
        return chrome_path, driver_path, version

    # 다운로드 필요
    os.makedirs(base, exist_ok=True)
    chrome_url = next(x["url"] for x in downloads["chrome"] if x["platform"] == plat)
    driver_url = next(x["url"] for x in downloads["chromedriver"] if x["platform"] == plat)

    log(f"전용 크롬 최초 설치 ({version}, {plat}) - 잠시만 기다려 주세요.")
    _download_and_extract(chrome_url, base, log)
    _download_and_extract(driver_url, base, log)

    # 크롬 트리 전체에 실행권한 보장 (crashpad_handler 등 헬퍼 포함)
    _chmod_tree_executable(os.path.join(base, os.path.dirname(chrome_rel).split(os.sep)[0]))
    _make_executable(chrome_path)
    _make_executable(driver_path)

    # macOS 격리 속성 제거 (.app 루트 기준)
    if plat.startswith("mac"):
        app_root = os.path.join(base, f"chrome-{plat}",
                                "Google Chrome for Testing.app")
        _strip_quarantine_mac(app_root, log)
        _strip_quarantine_mac(driver_path, log)

    if not (os.path.exists(chrome_path) and os.path.exists(driver_path)):
        raise RuntimeError("크롬 설치 후에도 실행 파일을 찾지 못했습니다.")

    log(f"전용 크롬 설치 완료: {version}")
    return chrome_path, driver_path, version
