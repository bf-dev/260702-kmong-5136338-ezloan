# -*- coding: utf-8 -*-
"""이지론 배너 자동등록 - 고정 설정."""

import os

APP_VERSION = "2.6.0"
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
# v2.5.5 (2026-07-28, 무료 튜닝 - 1등 경쟁력 개선): 실측(KR egress unicorn@external-8) 결과
# /rq 목록 fetch 는 309KB 인데 rq_addbanner_check 는 47바이트다. 그런데 예전 루프는 이 둘을
# 같은 주기(POLL_SECONDS)로 함께 돌렸다 - 새 글 감지에 필요한 건 47바이트짜리 가벼운 체크뿐인데,
# 그걸 위해 매번 무거운 309KB 목록도 같이 받아왔다는 뜻. 그래서 감지 주기를 더 좁히려면
# 무거운 목록 fetch 도 그만큼 자주 돌아야 했고, 그게 감지 지연을 줄이는 데 실질적 한계였다.
# 이제 '프런티어 한 글만 보는 가벼운 체크'(step 1, look-ahead)와 '목록 기반 안전망/프런티어
# 재동기화'(step 0+2, 무거운 fetch)의 주기를 분리한다: 가벼운 체크는 FRONTIER_POLL_SECONDS
# 마다 항상 돌고, 무거운 목록 작업은 LIST_POLL_SECONDS 마다만 얹혀서 돈다. 결과: 감지 지연
# 상한이 0.8s -> 0.2s 로 4배 좁아지면서도(평균 감지 지연 ~0.4s -> ~0.1s), 무거운 309KB fetch
# 빈도는 오히려 줄어(0.8s당 1회 -> 1.0s당 1회) 초당 대역폭도 낮아진다(~386KB/s -> ~309KB/s).
# 등록 핫패스 자체(rq_addbanner_check/rq_addbanner 왕복, v2.4.6 실측 87ms)는 그대로 - 이건
# TCP/커넥션 재사용(urllib3 는 기본 TCP_NODELAY 적용, keep-alive 로 이미 웜 커넥션 유지, 실측
# 콜드 ~98ms -> 웜 ~35-40ms) 이미 최적이라 더 줄일 여지가 거의 없다(핑 RTT 1.4ms 대비 35-40ms
# 대부분은 이지론 서버측 처리시간 - 클라이언트에서 더 손댈 수 없는 구간).
# v2.6.0 (2026-08-05): 감지기를 '쓰기'에서 '읽기'로 바꾼 뒤의 값.
# 라이브 로그(고객 5136338, 2026-08-05) 실측으로 드러난 진짜 병목:
#   rq_addbanner_check 는 계정 자격만 보고 '글 존재'는 전혀 보지 않는다(아직 생기지도 않은
#   미래 번호에도 result:true). 그래서 v2.4.6~v2.5.5 는 '새 글이 떴는지'를 쓰기 엔드포인트
#   (rq_addbanner)가 404 를 주는지로 판단했고, 쓰기를 매 tick 쏠 수는 없으니
#   POST_ABSENT_BACKOFF_INTERVAL(20 tick ≈ 5.7초)로 throttle 했다. 즉 정상 대기 상태에서
#   글이 실제로 떠도 최대 5.7초(평균 2.9초) 뒤에야 등록 요청이 나갔다. 게다가 2000 tick
#   (≈9.7분) 뒤엔 giveup 해 프런티어가 그 번호를 지나가 버려서, 글 간격이 10분 넘는
#   대부분의 경우 등록은 '1초 주기 + 309KB 목록 안전망'으로만 이뤄졌다(실측: 31238 등).
# v2.6.0 은 /rq/{id} 읽기(미존재 353바이트 vs 실제 글 293KB)로 존재를 확정한 뒤에만
# 쓰기를 쏜다. 읽기는 353바이트라 촘촘히 돌려도 부담이 없고, 쓰기는 새 글당 딱 1회로 줄어든다
# (예전 하루 약 1.5만 회 -> 하루 약 40회).
# 실측(2026-08-05, KR egress unicorn@external-8): 웜 keep-alive 로 /rq/{미존재} p50 46.8ms,
# p90 50.2ms. 10 req/s 를 30초간 쏴도 301/301 전부 200, 지연 증가/차단/429 없음.
FRONTIER_POLL_SECONDS = 0.15  # 존재 확인 tick 주기(절대 스케줄) - 새 글 감지 지연 상한.
# 매 fast tick 에 '동시에' 찔러 볼 글 번호 개수(frontier, frontier+1, ...).
# 실제 글 번호는 거의 항상 연속이고 아주 가끔 하나를 건너뛰므로(실측: 31222 -> 31224),
# 창 2면 건너뛴 번호도 지연 없이 잡힌다. 요청은 스레드로 병렬 발사되므로 tick 의 벽시계
# 비용은 창 크기와 무관하게 1왕복이다. 창 전체(LOOKAHEAD)는 무거운 tick 에서만 훑는다.
PROBE_WINDOW = 2
LIST_POLL_SECONDS = 1.0       # 목록(/rq, 309KB) fetch + 안전망 + 프런티어 재동기화 주기.
# 구버전 POLL_SECONDS 는 auth_mismatch backoff 의 기준값(그리고 하위호환 fallback)으로만 남긴다.
# 위 두 값으로 분리되기 전에는 이게 유일한 루프 주기였다(v2.4.6~v2.5.4).
POLL_SECONDS = LIST_POLL_SECONDS
LOOKAHEAD = 6               # 프런티어 앞으로 미리 확인할 순번 수 (0=끄기)
MAX_POSTS = 20             # 목록에서 확인할 최대 글 수
# 새 글마다 연속 'no permission' 거부가 이 횟수 이상 쌓였을 때만 계정 힌트를 1회 알린다.
# 그 미만의 개별-글 스킵은 조용히(중립 문구로) 넘겨 고객에게 '계정/배너 확인' 재알람을 주지
# 않는다(고객 이력: 계정 정상인데도 그 문구가 불필요한 불안을 유발함).
NO_PERM_WARN_STREAK = 5
# post_absent(체크는 통과했으나 글 페이지가 아직 안 뜬 상태)를 같은 글번호로 이 사이클 수
# 만큼 연속으로 겪으면 그제서야 '진짜 없는 번호'로 보고 포기(seen 처리)한다. 2026-07-27
# 실측(고객 5136338)의 정상적인 페이지-반영 지연은 길어야 수 초~수십 초였고, 이 상한은
# 순전히 '영원히 존재하지 않을 번호'에 무한정 매달리지 않기 위한 방어선이다.
# v2.5.5: 이 스트릭은 이제 FRONTIER_POLL_SECONDS(0.2s) 마다 늘어난다(예전엔 POLL_SECONDS=
# 0.8s 마다). 체감 대기시간(giveup 까지 걸리는 실제 시간)을 그대로 유지하려고 아래 세 상수를
# 전부 4배(0.8s/0.2s) 스케일했다 - 벽시계 기준 giveup/backoff 타이밍은 v2.5.2~v2.5.4 와
# 완전히 동일하고, 다만 그 구간 안에서 재시도가 4배 더 자주(즉 더 빨리) 일어난다.
# 2000회 * 0.2s ≈ 6.7분(= 예전 500회 * 0.8s 와 동일).
# v2.6.0: 아래 세 상수의 의미가 크게 좁아졌다. 이제 rq_addbanner 는 /rq/{id} 읽기로 글이
# 실제로 뜬 것이 '확정'된 뒤에만 나가므로, post_absent(= 페이지는 떴는데 add 가 404) 는
# 예전처럼 '대기 상태의 기본값'이 아니라 진짜 찰나의 경쟁일 때만 생기는 드문 상태다.
# 그래도 벽시계 타이밍은 v2.5.x 와 동일하게 유지하려고 tick 0.2s -> 0.15s 만큼 재스케일했다:
#   3900 * 0.15s ≈ 9.75분(= 2000 * 0.2s 와 사실상 동일한 방어선).
POST_ABSENT_GIVEUP_STREAK = 3900
# v2.5.2 (2026-07-27, 운영자 지시 - 유료 "1등 등록속도" 는 유지하되 더 보수적으로): 위
# post_absent 재시도는 FAST_RETRY 구간 동안 매 사이클 rq_addbanner(WRITE)를 다시 쏜다.
# 실측(2026-07-27, 고객 5136338)상 정상적인 페이지-반영 지연은 길어야 수초~수십초였으므로,
# 그 구간까지는 매 사이클 즉시 재시도해 유료 기능이 약속한 속도를 그대로 지킨다. 그런데 그
# 구간을 넘어서도(예: 진짜로 없는 번호이거나 이지론 서버 이상) 계속 매 사이클 WRITE 엔드포인트를
# 두드리는 건 불필요하게 공격적이다. FAST_RETRY 구간 이후에는 BACKOFF_INTERVAL 사이클마다
# 한 번만 실제로 재시도해 사이트 부담을 줄인다. giveup 카운트(체감 대기 시간)는 그대로
# 유지되고(스킵한 사이클도 스트릭에 포함), 실제 rq_addbanner 호출 횟수만 줄어든다.
# v2.5.5: FRONTIER_POLL_SECONDS=0.2s 기준으로 4배 스케일(체감 시간은 이전과 동일):
# 160*0.2s=32s(FAST_RETRY 구간), 20*0.2s=4s(BACKOFF 간격) - 예전 40*0.8s=32s, 5*0.8s=4s 와
# 완전히 같은 실제 시간이지만, 그 32s 안에서는 재시도가 4배 더 촘촘해져(0.8s당->0.2s당)
# 페이지가 뜨는 순간을 그만큼 더 빨리 잡는다(이게 바로 이번 튜닝의 핵심 이득).
# v2.6.0: tick 이 0.15s 가 되었으므로 벽시계 기준으로 같은 32s / 4s 가 되게 다시 스케일한다.
# (이 구간이 실제로 쓰이는 빈도 자체는 v2.6.0 에서 크게 줄었다 - 위 GIVEUP 주석 참고.)
POST_ABSENT_FAST_RETRY_CYCLES = 213   # 0.15s * 213 ≈ 32s. 매 tick 즉시 재시도(속도 유지 구간).
POST_ABSENT_BACKOFF_INTERVAL = 27     # 그 이후엔 27tick(≈4s)마다 한 번만 실제 재시도.

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
# 자동 업데이트 스위치. 2026-07-03 운영자 지시로 껐던 것을 2026-07-27 운영자 지시로 다시 켰다가,
# 같은 날 실제 라이브 스왑에서 고객 PC가 "검은화면 뜨면서 꺼짐"(실행 안 됨)을 겪어 즉시 다시 끈다.
# 라이브 증거(artifacts-check 5136338, 06:20-06:23 구간): v2.5.3 프로세스는 [app_started] 딱 한
# 줄만 남기고 이후 어떤 로그도 없이 사라짐(session_recovered/registrar_init 없음 = 초기화 도중
# 죽음). 같은 시각 기존 v2.5.2 프로세스는 cycle 카운터가 끊김 없이 계속 올라감(그 프로세스는
# stop_running_loop 가 걸리지 않았다 = 자기 자신은 스왑을 못 감지했거나 스왑 스레드가 죽었다).
# updater.py._schedule_restart 는 remote_log(...)(비동기, 별도 스레드에서 HTTP POST)를 호출한
# 직후 바로 os._exit(0) 을 호출한다 - 데몬 스레드가 요청을 마치기 전에 프로세스 전체가 죽으므로
# update_downloaded/update_session_saved/update_restart 로그가 서버에 단 한 건도 도착하지
# 않았다(실측: 해당 이벤트 텍스트로 필터링해도 0건). 그래서 .bat copy/relaunch 자체가 성공했는지
# 실패했는지조차 원격에서 확인할 수 없는 상태였다 - 자동 업데이트가 자기 자신의 실패를 보고할
# 방법이 없는 구조였다는 뜻이므로, 원인을 완전히 특정하기 전까지는 다시 켜지 않는다. 재도입하려면
# 최소한 (1) _schedule_restart 의 remote_log 를 os._exit 전에 동기적으로 완료시키거나 join, (2)
# .bat 의 copy/start 실패를 감지해 원래 exe 로 안전하게 되돌리는 폴백을 먼저 넣을 것.
AUTO_UPDATE_ENABLED = False

# 프로그램이 자체 관리하는 크롬(Chrome for Testing) / 프로필 위치
_HOME = os.path.expanduser("~")
CHROME_CACHE_DIR = os.path.join(_HOME, ".ezloan_bot", "chrome")
CHROME_PROFILE_DIR = os.path.join(_HOME, ".ezloan_bot", "profile")
APP_DIR = os.path.join(os.getenv("APPDATA", _HOME), "EzloanBot")
# 재시작 후 로그인 세션 복구용: 캡처한 이지론/네이버 쿠키를 여기에 저장한다.
SESSION_FILE = os.path.join(APP_DIR, "session.json")
