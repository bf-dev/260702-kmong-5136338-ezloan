# -*- coding: utf-8 -*-
"""이지론 배너 자동등록 - 고정 설정."""

import os

APP_VERSION = "2.3.9"
CUSTOMER_ID = "5136338"

# 이지론
# (아래 셀렉터/엔드포인트는 2026-07-03 실제 ezloan.io 라이브 사이트에서 검증됨)
BASE_URL = "https://ezloan.io"
COMPANY_NAME = "더원대부"           # 배너 등록 순위를 확인할 광고주 상호
LOGIN_URL = f"{BASE_URL}/m/login"   # "네이버로 로그인" 버튼이 있는 페이지 (검증: 2026-07-03)
RQ_URL = f"{BASE_URL}/rq"           # 실시간 대출 문의 목록 (검증: 2026-07-03)

# 네이버 로그인 (한국어 강제)
NAVER_LOGIN_URL = "https://nid.naver.com/nidlogin.login?locale=ko_KR"

# 등록 루프 파라미터
POLL_SECONDS = 1.5          # 목록 폴링 주기
LOOKAHEAD = 6               # 프런티어 앞으로 미리 확인할 순번 수 (0=끄기)
MAX_POSTS = 20             # 목록에서 확인할 최대 글 수

# 일일 운영 시간대 (한국 시간, KST/Asia/Seoul).
# 고객 요청(2026-07-06): "매일 오전 8시 ~ 밤 12시(자정)까지 돌려주세요".
# 이 시간대 안에서만 등록/스크래핑 루프를 돌리고, 밖(00:00~08:00)에서는
# 프로그램 창은 켜둔 채 API 를 두드리지 않고 대기하다가 08:00 이 되면 자동으로 재개한다.
# 시각은 [RUN_START_HOUR, RUN_END_HOUR) 반열린 구간(시 단위)으로, 08~24시.
# 예: 8, 24 -> 08:00:00 부터 23:59:59 까지 동작, 00:00(자정) 정각부터 대기.
# END_HOUR=24 는 "자정까지" 를 뜻한다(시 값 8..23 동작, 0..7 대기).
RUN_START_HOUR = 8
RUN_END_HOUR = 24
# 운영 시간대 자체를 끄고 24시간 돌리려면 아래를 False 로.
RUN_WINDOW_ENABLED = True
# 대기 상태에서 다음 시간대 진입을 확인하는 간격(초). 창은 살아있게 유지된다.
IDLE_CHECK_SECONDS = 30

# 원격 진단 / 캡차 중계용 (works.insu.ng 게이트웨이)
WORKS_API = "https://works.insu.ng/works/api"
STATIC_BASE = f"https://works.insu.ng/works/public/{CUSTOMER_ID}"
# 자동 업데이트용 버전 파일.
# 주의: {STATIC_BASE}/version.json 은 (지금은 보관된) 예전 '쿠키 붙여넣기' 데스크탑 앱이
# 여전히 쓰는 파일이다. 이 신규 앱은 절대 그 파일을 덮어쓰지 않고,
# 별도 파일(version-ezloan-desktop.json)로 자기 버전을 관리한다.
VERSION_URL = f"{STATIC_BASE}/version-ezloan-desktop.json"
UPDATE_CHECK_SECONDS = 60
# 자동 업데이트 스위치. 운영자 지시(2026-07-03)로 자기 자동 업데이트를 끈다.
# False 이면 앱은 더 이상 version-ezloan-desktop.json 을 폴링하거나 자기 exe 를
# 교체하지 않는다. 등록/스크래핑 등 나머지 동작은 그대로 유지된다.
AUTO_UPDATE_ENABLED = False

# 프로그램이 자체 관리하는 크롬(Chrome for Testing) / 프로필 위치
_HOME = os.path.expanduser("~")
CHROME_CACHE_DIR = os.path.join(_HOME, ".ezloan_bot", "chrome")
CHROME_PROFILE_DIR = os.path.join(_HOME, ".ezloan_bot", "profile")
APP_DIR = os.path.join(os.getenv("APPDATA", _HOME), "EzloanBot")
# 재시작 후 로그인 세션 복구용: 캡처한 이지론/네이버 쿠키를 여기에 저장한다.
SESSION_FILE = os.path.join(APP_DIR, "session.json")
