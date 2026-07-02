# -*- coding: utf-8 -*-
"""Selenium 드라이버 생성 - 프로그램 전용 크롬 사용 + 일반 크롬처럼 위장.

- undetected-chromedriver 를 쓰지 않는다. (macOS 크래시/버전 꼬임 원인)
- chrome_provisioner 로 받은 전용 크롬 바이너리를 직접 지정해서 구동한다.
- user-agent 는 '평범한 최신 크롬' 문자열로 강제한다. (HeadlessChrome/Testing 흔적 제거)
- navigator.webdriver 등 자동화 흔적을 CDP 로 제거한다.
"""

import platform

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

import config
from chrome_provisioner import ensure_chrome


def _normal_user_agent(major):
    """플랫폼별 '평범한' 최신 크롬 UA 문자열 (reduced UA 포맷)."""
    system = platform.system()
    if system == "Windows":
        os_token = "Windows NT 10.0; Win64; x64"
    elif system == "Darwin":
        os_token = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def build_driver(headless=False, log=print, proxy=None):
    """전용 크롬으로 selenium 드라이버 생성.

    proxy: 예) "socks5://127.0.0.1:1080". 지정 시 해당 프록시로 트래픽을 보낸다.
    """
    chrome_path, driver_path, version = ensure_chrome(log=log)
    major = version.split(".")[0]
    user_agent = _normal_user_agent(major)

    options = webdriver.ChromeOptions()
    options.binary_location = chrome_path

    # 로그인 세션 유지용 프로필
    options.add_argument(f"--user-data-dir={config.CHROME_PROFILE_DIR}")

    options.add_argument("--start-maximized")
    options.add_argument("--lang=ko-KR")
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-infobars")
    options.add_argument("--password-store=basic")
    # macOS/리눅스 샌드박스 관련 종료 방지
    if platform.system() != "Windows":
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("prefs", {
        "intl.accept_languages": "ko-KR,ko",
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })

    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1360,960")

    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)

    # 자동화 흔적 제거 (모든 새 문서에 주입)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": (
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "Object.defineProperty(navigator,'languages',{get:()=>['ko-KR','ko']});"
                "window.chrome={runtime:{}};"
            )},
        )
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {"userAgent": user_agent, "acceptLanguage": "ko-KR,ko",
             "platform": "Win32" if platform.system() == "Windows" else "MacIntel"},
        )
    except Exception:
        pass

    try:
        driver.maximize_window()
    except Exception:
        pass
    return driver
