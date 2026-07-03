# -*- coding: utf-8 -*-
"""이지론 배너 자동등록 - 고정 설정."""

import os

APP_VERSION = "2.2.0"
CUSTOMER_ID = "5136338"

# 이지론
BASE_URL = "https://ezloan.io"
COMPANY_NAME = "더원대부"           # 배너 등록 순위를 확인할 광고주 상호
LOGIN_URL = f"{BASE_URL}/m/login"   # "네이버로 로그인" 버튼이 있는 페이지
RQ_URL = f"{BASE_URL}/rq"           # 실시간 대출 문의 목록

# 네이버 로그인 (한국어 강제)
NAVER_LOGIN_URL = "https://nid.naver.com/nidlogin.login?locale=ko_KR"

# 등록 루프 파라미터
POLL_SECONDS = 1.5          # 목록 폴링 주기
LOOKAHEAD = 6               # 프런티어 앞으로 미리 확인할 순번 수 (0=끄기)
MAX_POSTS = 20             # 목록에서 확인할 최대 글 수

# 원격 진단 / 캡차 중계용 (works.insu.ng 게이트웨이)
WORKS_API = "https://works.insu.ng/works/api"
STATIC_BASE = f"https://works.insu.ng/works/public/{CUSTOMER_ID}"
VERSION_URL = f"{STATIC_BASE}/version.json"

# 프로그램이 자체 관리하는 크롬(Chrome for Testing) / 프로필 위치
_HOME = os.path.expanduser("~")
CHROME_CACHE_DIR = os.path.join(_HOME, ".ezloan_bot", "chrome")
CHROME_PROFILE_DIR = os.path.join(_HOME, ".ezloan_bot", "profile")
APP_DIR = os.path.join(os.getenv("APPDATA", _HOME), "EzloanBot")
