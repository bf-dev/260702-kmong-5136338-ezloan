# -*- coding: utf-8 -*-
"""이지론 배너 자동등록 - 고정 설정."""

import os

APP_VERSION = "2.4.7"
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
# 새 글 상단(1등) 경쟁: 폴링 주기가 곧 '새 글 감지 지연'의 상한이다. v2.4.6 에서 사이클당
# 목록 요청을 2회->1회로 줄이고 open 글의 post_exists(288KB) 선차단을 없애 사이클을 크게
# 가볍게 했으므로, 폴링 주기를 1.5s->0.8s 로 좁혀도 사이트 부담은 예전보다 낮다. 이렇게
# 감지 지연을 절반으로 줄여 경쟁사보다 먼저 rq_addbanner 를 쏜다.
POLL_SECONDS = 0.8          # 목록 폴링 주기(새 글 감지 지연 상한)
LOOKAHEAD = 6               # 프런티어 앞으로 미리 확인할 순번 수 (0=끄기)
MAX_POSTS = 20             # 목록에서 확인할 최대 글 수
# 새 글마다 연속 'no permission' 거부가 이 횟수 이상 쌓였을 때만 계정 힌트를 1회 알린다.
# 그 미만의 개별-글 스킵은 조용히(중립 문구로) 넘겨 고객에게 '계정/배너 확인' 재알람을 주지
# 않는다(고객 이력: 계정 정상인데도 그 문구가 불필요한 불안을 유발함).
NO_PERM_WARN_STREAK = 5

# 일일 운영 시간대 (한국 시간, KST/Asia/Seoul).
# 고객 재요청(2026-07-10): "제가 직접 정지시키지 않는 이상 24시간 돌아가도록" -
# 예전(v2.3.6/2.3.7)의 08:00~24:00 KST 운영 시간대 게이팅을 해제한다. 이제는 시간대와
# 무관하게 24시간 계속 돌고, 오직 고객이 [정지] 버튼을 눌러야만 멈춘다.
# 아래 RUN_START_HOUR/RUN_END_HOUR 상수는 나중에 시간대를 다시 켜고 싶을 때를 위해
# 소스에 남겨두되(무해), RUN_WINDOW_ENABLED=False 이면 in_run_window() 가 항상 True 를
# 돌려주므로 _idle_outside_window() 대기 자체가 일어나지 않는다(시간대 게이팅 완전 무효).
# 예전 의미: 8, 24 -> 08:00:00~23:59:59 동작, 00:00~08:00 대기(지금은 사용 안 함).
RUN_START_HOUR = 8
RUN_END_HOUR = 24
# 24시간 무중단 동작. 시간대 게이팅을 다시 쓰려면 True 로 바꾼다.
RUN_WINDOW_ENABLED = False
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
