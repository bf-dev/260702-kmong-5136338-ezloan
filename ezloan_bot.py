# -*- coding: utf-8 -*-
"""이지론 실시간 배너 등록 루프.

네이버 로그인으로 확보한 세션 쿠키를 requests.Session 으로 옮겨 빠른 HTTP 루프를 돈다.
  1) look-ahead: 프런티어(다음에 생길 글 번호)를 check API 로 미리 찔러 새 글이 생기는
     즉시 등록 -> 상위 노출을 잡는다.
  2) 목록(/rq) 폴링: look-ahead 가 놓친 글의 안전망.
글마다 rq_addbanner_check -> (등록 가능하면) rq_addbanner -> 순위 확인 순으로 처리한다.

세션 쿠키는 IP 에 묶여 있지 않으므로, 로그인만 되면 이후 등록은 순수 HTTP 로 충분하다.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config

# 한국 표준시(KST)는 UTC+9 고정(서머타임 없음). 고객 PC 의 로컬 시간대 설정이나
# PyInstaller onefile 에 타임존 DB 가 안 실리는 문제와 무관하게 항상 KST 를 계산하려고
# 시스템 로컬타임 대신 UTC 에 +9 를 더해 한국 시각을 얻는다.
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(timezone.utc).astimezone(KST)


def in_run_window(dt=None):
    """지금이 고객이 지정한 일일 운영 시간대(기본 08:00~23:00 KST) 안이면 True.

    [RUN_START_HOUR, RUN_END_HOUR) 반열린 구간(시 단위). RUN_WINDOW_ENABLED 가 꺼져 있으면
    항상 True(24시간 동작). 자정을 넘기는 설정(start > end)도 지원한다.
    """
    if not getattr(config, "RUN_WINDOW_ENABLED", True):
        return True
    dt = dt or now_kst()
    h = dt.hour
    start = config.RUN_START_HOUR
    end = config.RUN_END_HOUR
    if start <= end:
        return start <= h < end
    # 자정을 넘기는 구간(예: 22~6시)
    return h >= start or h < end

_RQ_LINK_RE = re.compile(r'/rq/(\d+)(?:["\'/?#]|$)')
# 실측(2026-07-10): 아직 생기지 않은(존재하지 않는) 글 번호의 /rq/{id} 페이지는 항상
# 300~400바이트대의 빈 껍데기(실제 글 페이지는 25만 바이트대)로 온다. 이 하한을 넘지
# 못하고 아래 글-존재 마커도 없으면 그 번호의 글은 '아직 없음'으로 확정한다.
_POST_PAGE_MIN_BYTES = 1000
_POST_PAGE_MARKERS = ("배너 등록을 눌러 주세요", "js-memberConfirmView", "rq_addbanner")
NON_RETRYABLE = {"slots_full", "no_banner_amount", "no_ads", "no_payed_ads", "no_permission",
                 # 존재하지 않는(아직 안 생긴) 글에 add 를 걸면 rq_addbanner 가 '404 error'.
                 # 세션/유료광고 문제가 아니라 '글 없음'이므로 재시도하지 않고 건너뛴다.
                 "post_absent",
                 # 글은 실재하지만 이 회원 배너가 이미 있거나(재등록 불가) 지금 계정 상태로
                 # 등록 대상이 아님. rq_addbanner 가 result:false('404 error'/'no permission')
                 # 를 주는 개별-글 등록 거부. 세션 사망 아님 -> seen 처리하고 프런티어 전진.
                 "add_refused"}
# 진짜 세션 소실/미인증(로그아웃)일 때만 나오는 신호만 여기 둔다.
# "404 error" 와 "no permission" 은 절대 넣지 않는다(아래 근거).
#
# 실측 근거(2026-07-10, 고객 5136338 세션으로 라이브 확인):
#  - 이 세션은 12:43 에 rq_addbanner_check 가 success/amount:113 을 줬고 post 29950 을
#    실제로 등록(rank 148)했다. 즉 세션·유료광고·잔여 모두 정상이었다.
#  - 그런데 재시작으로 프런티어가 30466(look-ahead 유령 번호까지 폭주한 값)에서 실제
#    최신글 기준 29952 로 되감겨, 이미 등록한 실재 글(29956~29981)을 다시 add 하자
#    rq_addbanner 가 전부 '404 error' 를 줬다. 이건 세션 사망이 아니라 '이 글엔 이미
#    내 배너가 있음/재등록 불가'라는 개별-글 거부다.
#  - 같은 세션으로 몇 시간 뒤 다시 찔러 보면 check 가 최상단 실재 글에도 'no permission'
#    을 준다. 쿠키는 '동일'한데 결과만 시간에 따라 바뀐다 -> 원인은 낡은 쿠키가 아니라
#    서버측 계정 할당/기간 제한 상태다(재로그인해도 같은 계정이라 안 바뀜).
#  - 사이트 자체 script.js 의 rq_addbanner msgMap 도 '404 error' 를 'no amount'/'no ads'/
#    'no payed ads' 와 나란히 '유료 광고를 진행 해주세요'로 렌더할 뿐, 로그아웃시키지 않는다.
# 따라서 '404 error'/'no permission' 을 세션소실로 오분류하면 세션이 멀쩡한데도 streak 이
# 올라 거짓 session_expired('재로그인 필요')/auth_mismatch backoff 에 갇힌다.
# 진짜 세션 사망 여부는 항상 logged_in()==False 로만 판정한다.
SESSION_LOST_MSGS = {"no session", "session expired", "unauthorized", "logout", "please login"}
_NOTE_MAP = {
    # 로그인은 유효하나 이 글/계정이 지금 등록 대상이 아님(세션소실 아님). 재시도 없이 넘긴다.
    "no permission": "no_permission",
    # 개별 글 등록 거부(이미 등록됨/미존재/계정상태상 불가). register() 가 post_exists 로
    # 실존/미존재를 갈라 post_absent(미존재) 또는 add_refused(실존·재등록불가)로 확정한다.
    "404 error": "add_refused",
    "no amount": "no_banner_amount",
    "no ads": "no_ads",
    "no payed ads": "no_payed_ads",
    "max": "slots_full",
    "ing": "already_registered_api",
}


def _is_session_lost_msg(msg):
    m = (msg or "").strip().lower()
    return any(k in m for k in SESSION_LOST_MSGS)


def _safe_msg(r):
    try:
        return (r.json().get("msg") or "").strip()
    except Exception:
        return ""


def _sync_csrf_header(s):
    """요청마다 세션 쿠키 항아리(jar)의 현재 csrf_cookie_ezloan 값을 X-CSRF-TOKEN 헤더로 동기화.

    CodeIgniter double-submit 방어: 헤더는 반드시 '지금 이 순간의' csrf 쿠키와 같아야 한다.
    서버가 csrf 를 회전시키면 캡처 시점의 고정 값은 낡아 거부될 수 있으므로,
    매 API 호출 직전 jar 에서 최신 값을 다시 읽어 헤더에 반영한다(회전이 없으면 무해).
    """
    try:
        for c in s.cookies:
            if c.name == "csrf_cookie_ezloan" and c.value:
                s.headers["X-CSRF-TOKEN"] = c.value
                return c.value
    except Exception:
        pass
    return s.headers.get("X-CSRF-TOKEN")


def session_from_cookies(cookies):
    s = requests.Session()
    csrf = None
    for c in cookies:
        try:
            # 캡처된 도메인을 '있는 그대로' 유지한다. 앞의 점(.ezloan.io)을 제거하면
            # 안 된다: 서버의 Set-Cookie 는 domain=.ezloan.io 로 오므로, 점을 떼어
            # ezloan.io 로 넣어두면 requests 가 두 항목을 '다른 도메인'으로 취급해
            # ezloan_sess 가 중복 저장된다. 그러면 서버가 세션을 회전시켜도(슬라이딩)
            # 낡은 ezloan.io 값이 덮이지 않고 남아, 요청 Cookie 헤더에 낡은 값이 먼저
            # 실려 나간다 -> 서버(PHP $_COOKIE)는 첫 값을 읽어 만료된 세션으로 판정 ->
            # msg:"404 error" 무한 루프. 도메인을 보존하면 Set-Cookie 가 같은 항목을
            # 정확히 '덮어써' 언제나 최신 세션 한 개만 전송된다.
            s.cookies.set(
                c["name"], c["value"],
                domain=(c.get("domain") or "ezloan.io"),
                path=(c.get("path") or "/"),
            )
            if c["name"] == "csrf_cookie_ezloan":
                csrf = c["value"]
        except Exception:
            continue
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        # 실측(2026-07-03): 이지론 API(/api/rq_addbanner_check, rq_addbanner)는
        # 동일 출처 Referer 가 없으면 유효한 세션 쿠키가 있어도 msg:"404 error" 로 거부한다.
        # (Referer 없음/외부 Referer -> "404 error", 동일 출처 Referer -> 정상 판정)
        # 브라우저는 항상 Referer 를 보내므로 페이지는 로그인 상태로 보이지만,
        # 앱의 순수 requests 세션은 Referer 가 없어 API 마다 거부돼 무한 인증실패 루프가 났다.
        "Referer": config.RQ_URL,
        "Origin": config.BASE_URL,
    })
    # CodeIgniter double-submit 방어 대비: csrf 쿠키 값을 헤더로도 되돌려 준다(무해).
    if csrf:
        s.headers["X-CSRF-TOKEN"] = csrf
    return s


def logged_in(s):
    try:
        r = s.get(config.RQ_URL, timeout=10, allow_redirects=True)
    except Exception:
        return False
    if r.status_code != 200 or "login" in r.url.lower():
        return False
    t = r.text
    return ("로그아웃" in t or "광고 관리" in t) and "로그인 해주세요" not in t


def list_post_ids(s, max_posts=config.MAX_POSTS):
    r = s.get(config.RQ_URL, timeout=10)
    ids, seen = [], set()
    for pid in _RQ_LINK_RE.findall(r.text):
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
            if len(ids) >= max_posts:
                break
    return ids


def _check(s, pid):
    # CodeIgniter double-submit: 매 호출 직전 jar 의 최신 csrf 값을 헤더에 동기화(회전 대비).
    _sync_csrf_header(s)
    r = s.get(f"{config.BASE_URL}/api/rq_addbanner_check/{pid}", timeout=12)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def post_exists(s, pid):
    """해당 글 번호가 실제로 존재하는 글인지(아직 안 생긴 미래 번호가 아닌지) 확인.

    실측(2026-07-10): 존재하지 않는 미래 글 번호의 /rq/{id} 는 300~400바이트대 빈 껍데기,
    실제 글은 25만 바이트대 + 아래 마커를 포함한다. rq_addbanner_check 는 계정의 유료광고/
    배너 잔여만 검사하고 '글 존재'는 검사하지 않으므로(미래 번호에도 result:true 를 준다),
    등록 전에 반드시 이 존재 확인을 거쳐야 rq_addbanner 가 '404 error'(글 없음)로 실패하며
    프런티어가 유령 번호로 폭주하는 것을 막는다.
    """
    try:
        r = s.get(f"{config.BASE_URL}/rq/{pid}", timeout=10, allow_redirects=True)
    except Exception:
        # 확인 실패 시엔 존재한다고 보수적으로 가정(실제 글을 놓치지 않도록).
        return True
    if r.status_code != 200:
        return False
    body = r.text or ""
    if len(body) < _POST_PAGE_MIN_BYTES:
        return False
    return any(m in body for m in _POST_PAGE_MARKERS)


def probe_state(s, pid):
    """(state, data) 반환. state: open / ing / absent / future / blocked / no_session / error

    핫패스 속도(새 글 1등 경쟁): result:true('open' 후보)일 때는 여기서 무거운 post_exists
    (/rq/{id} ~288KB 전체 페이지)를 부르지 않는다. 예전엔 open 판정 전에 post_exists 를
    반드시 통과시켜 유령(미래) 번호가 프런티어를 폭주시키는 것을 막았는데, 그 288KB 왕복이
    '경쟁사보다 먼저 rq_addbanner 를 쏴야 하는' 바로 그 글의 등록을 늦춰 2등으로 밀리는
    원인이 됐다(실측 2026-07-17: 새 글 등록 핫패스가 v2.4.1 133ms -> v2.4.5 288ms 로 2배↑).
    이제 open 후보는 존재 확인 없이 즉시 등록을 시도한다. 유령(미래) 번호면 register() 가
    rq_addbanner '404 error' + post_exists=False 로 post_absent 를 돌려주고, 상위 루프는
    그 결과를 보고 프런티어를 전진시키지 않는다 -> 폭주 방지는 그대로 유지되면서, 실제 글에는
    무거운 존재 확인을 아예 하지 않는다(post_exists 는 이제 거부/404 가 났을 때만 지연 호출).
    """
    try:
        code, data = _check(s, pid)
    except Exception:
        return "error", None
    if code != 200:
        return "error", None
    if data.get("result") is True:
        return "open", (code, data)
    msg = (data.get("msg") or "").strip().lower()
    # 진짜 세션 소실 신호(로그아웃/미인증)일 때만 no_session. '404 error'/'no permission' 은
    # 세션 사망이 아니라 계정상태상 지금 등록 불가일 뿐이므로 아래 blocked 로 떨어뜨린다.
    if _is_session_lost_msg(msg):
        return "no_session", None
    if "존재하지" in msg or "삭제" in msg:
        return "absent", None
    if msg == "ing":
        return "ing", None
    # result:false + (세션소실/삭제/ing 아님) = 계정상태상 지금 등록 불가(예: 'no permission',
    # '404 error'). 여기서 반드시 글 실존을 확인한다. rq_addbanner_check 는 '글 존재'를 검사
    # 하지 않고 계정 상태만 본다(미래 번호에도 동일 응답). 따라서 계정이 'no permission' 상태로
    # 들어가면 실재 글이든 아직 안 생긴 미래 번호든 전부 이 분기로 떨어진다. 존재 확인 없이
    # 이를 'blocked'(지나가도 됨)로 처리하면, look-ahead 가 미래(유령) 번호를 blocked 로 보고
    # safe_frontier 를 끝없이 밀어 프런티어가 실제 최신글보다 수백~수천 위로 폭주한다(실측
    # 2026-07-13: 계정 no_permission 중 프런티어 30031->31213, 미존재 번호로 +6/사이클 runaway).
    # 그러면 나중에 실제 새 글이 생겨도 이미 프런티어보다 아래라 look-ahead 가 못 잡아 즉시-등록
    # (상위 노출)을 놓친다. 실존이 확인된 글만 blocked(전진 가능), 미존재면 future(전진 금지).
    return ("blocked", None) if post_exists(s, pid) else ("future", None)


def lookahead_ids(s, frontier, window):
    """프런티어 앞쪽을 미리 찔러 '실제로 존재하며 등록 가능한' 새 글 번호를 찾는다.

    반환: (등록 후보 리스트, safe_frontier).
      - 등록 후보: (pid, precheck) 튜플. precheck 는 그 글의 rq_addbanner_check 결과
        (code, data) 이며 register() 가 재검사 없이 그대로 재사용한다(핫패스 왕복 절감).
    safe_frontier = '전진해도 안전(존재가 확정)한 다음 번호'. 상위 루프는 프런티어를 이 값
    이상으로 올리면 안 된다. 그렇지 않으면 유령(미래) 번호를 지나쳐 프런티어가 폭주하고,
    나중에 그 번호로 실제 글이 생겨도 이미 프런티어보다 아래라 영원히 건너뛴다(핵심 버그).

    핵심(v2.4.6): 'open' 후보는 이제 존재 확인(post_exists) 없이 잡으므로 '실존 확정'이
    아니다. 따라서 open 을 만나면 safe_frontier 를 그 번호 너머로 밀지 않고 멈춘다. 실존
    확정과 프런티어 전진은 상위 루프가 register() 결과(post_absent 면 미전진, 그 외 실존)로
    처리한다 -> 프런티어 폭주 방지는 그대로 유지되면서, 실제 새 글 등록은 즉시 이뤄진다.
    """
    found, pid, end, absents = [], frontier, frontier + window, 0
    safe_frontier = frontier
    while pid < end:
        st, precheck = probe_state(s, str(pid))
        if st == "open":
            # 존재 미확정 후보. 즉시 등록 시도 대상으로 넘기고, 여기서 멈춘다(safe_frontier
            # 를 밀지 않음 -> 유령 번호가 open 으로 잡혀도 프런티어가 넘어가지 않는다).
            found.append((str(pid), precheck))
            break
        elif st == "ing":
            # 이미 내 배너가 붙은 실존 글. 존재 확정이므로 전진해도 안전.
            found.append((str(pid), precheck)); absents = 0
            safe_frontier = pid + 1
        elif st == "blocked":
            absents = 0
            safe_frontier = pid + 1  # 존재하나 지금은 등록 불가(권한/마감 등) - 지나가도 됨.
        elif st == "absent":
            absents += 1
            safe_frontier = pid + 1  # 삭제/존재하지 않는 과거 번호 - 지나가도 됨.
            if absents >= 2:
                break
        elif st == "future":
            # check 는 통과했지만 글이 아직 존재하지 않는 미래 번호. 여기서 멈춘다.
            # safe_frontier 를 올리지 않으므로 프런티어가 유령 번호를 넘어가지 않는다.
            break
        elif st == "no_session":
            # 세션이 죽었다. lookahead 를 즉시 접고 상위 루프가 세션 점검을 하게 한다.
            break
        else:
            break
        pid += 1
    found.sort(key=lambda t: int(t[0]), reverse=True)
    return found, safe_frontier


# 실측(2026-07-17): /rq/{pid} 의 실제 '배너 목록'은 <a href="/l/{광고주id}" class="item ...">
# 항목들이며(각 항목에 <div class="name">상호</div>), 이 순서가 곧 배너 노출 순위다(1번째=1등).
# 순위는 등록 '시각' 순(먼저 rq_addbanner 를 쏜 광고주가 위)이다(광고주 id 순 아님 - 실측 확인).
# 예전 company_rank 는 페이지 전체 <li>(네비/푸터 포함)를 세어 상호가 늘 ~147번째로 나와
# 실제 배너 순위(1~9위대)와 무관한 값을 로그에 남겼다. 이제 '진짜 배너 항목'만 세어 실제
# 상단 순위(1등/2등)를 보고한다 -> 로그의 rank 가 곧 고객이 사이트에서 보는 그 순위다.
_BANNER_ITEM_RE = re.compile(
    r'<a href="/l/\d+" class="item[^"]*"[^>]*>\s*<div class="name">([^<]*)</div>')

def company_rank(s, pid, company=config.COMPANY_NAME):
    try:
        r = s.get(f"{config.BASE_URL}/rq/{pid}", timeout=10)
    except Exception:
        return 0
    names = [n.strip() for n in _BANNER_ITEM_RE.findall(r.text)]
    for idx, name in enumerate(names, 1):
        if company in name:
            return idx
    return 0


def register(s, pid, company=config.COMPANY_NAME, precheck=None):
    # precheck: (code, data) 를 lookahead 의 probe_state 에서 이미 받아 왔으면 재사용한다.
    # 새 글 경쟁에서 상단(1등)을 잡으려면 rq_addbanner 를 최대한 빨리 쏴야 하므로,
    # 같은 rq_addbanner_check 를 두 번 치지 않는다(핫패스 왕복 1회 절감).
    if precheck is not None:
        code, data = precheck
    else:
        code, data = _check(s, pid)
    if code != 200:
        return {"ok": False, "rank": None, "note": "check_http_error",
                "status": code, "msg": None}
    if data.get("result") is not True:
        raw_msg = (data.get("msg") or "unknown").strip()
        msg = raw_msg.lower()
        note = _NOTE_MAP.get(msg, f"check_failed:{msg}")
        return {"ok": msg == "ing", "rank": None, "note": note,
                "status": code, "msg": raw_msg,
                "session_lost": _is_session_lost_msg(msg)}
    try:
        _sync_csrf_header(s)
        r = s.get(f"{config.BASE_URL}/api/rq_addbanner/{pid}", timeout=12)
        add = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        return {"ok": False, "rank": None, "note": f"add_error:{e}", "status": None, "msg": None}
    add_msg = (add.get("msg") or "").strip()
    if r.status_code != 200 or add.get("result") is not True:
        # 여기까지 왔다는 건 rq_addbanner_check 가 result:true 였다는 뜻이다
        # (= 세션 유효 + 계정 유료광고/배너 잔여 정상). 그런데도 add 가 result:false 라면
        # 그건 세션 사망이 아니라 '이 글은 지금 등록 대상이 아님'이라는 개별-글 거부다.
        # 두 경우로 갈린다(실측 2026-07-10):
        #  1) 글이 실제로 없음(아직 안 생긴 미래 번호/삭제 번호): check 는 글 존재를 검사
        #     하지 않아 미래 번호에도 통과를 주고, add 는 '404 error'. -> post_absent.
        #  2) 글은 실재하지만 이미 내 배너가 있거나 계정상태상 등록 불가: add 가 '404 error'/
        #     'no permission' 등. 재시작으로 프런티어가 되감겨 '이미 등록한 실재 글'을 다시
        #     add 할 때 나온다. -> add_refused. (예전엔 이걸 세션소실로 오분류해 거짓
        #     session_expired/auth_mismatch backoff 에 갇혔다.)
        # 어느 쪽이든 세션 사망이 아니므로 session_lost=False. 실재 여부만 post_exists 로 가른다.
        low = add_msg.lower()
        if low in ("404 error", "no permission"):
            exists = post_exists(s, pid)
            return {"ok": False, "rank": None,
                    "note": "add_refused" if exists else "post_absent",
                    "status": r.status_code, "msg": add_msg,
                    "session_lost": False,
                    "body": (r.text or "")[:300]}
        return {"ok": False, "rank": None, "note": "add_failed",
                "status": r.status_code, "msg": add_msg,
                "session_lost": _is_session_lost_msg(add_msg),
                "body": (r.text or "")[:300]}
    rank = company_rank(s, pid, company)
    for _ in range(3):
        if rank:
            break
        time.sleep(0.15)
        rank = company_rank(s, pid, company)
    return {"ok": True, "rank": rank, "note": "registered" if rank else "registered_not_verified",
            "status": 200, "msg": add_msg or "success"}


class Registrar:
    """로그인 후 requests 세션으로 등록 루프를 돈다."""

    def __init__(self, cookies, log=print, remote=None, should_stop=None,
                 seen_path=None, relogin=None):
        self.s = session_from_cookies(cookies)
        self._cookies_raw = cookies or []
        self._diag_sent = False   # auth_diag_dump 는 실행당 1회만
        self.log = log
        self.remote = remote or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        # 강제 재로그인 콜백(선택). 등록이 '새 글'에서까지 지속적으로 거부되는데 세션은
        # logged_in()==True 로 멀쩡한 경우, 낡은 세션(연장 전 상태를 물고 있는 쿠키)일 수
        # 있어 새 이지론 세션을 다시 받아 오게 하는 복구 훅. 반환: 새 cookies(list) 또는 None.
        self.relogin = relogin
        self.seen_path = Path(seen_path) if seen_path else None
        self.seen = self._read_seen()
        # 진단용 상태
        self._session_lost_streak = 0   # 연속 세션-없음 신호 카운트
        self._cycle = 0
        self._registered_total = 0
        self._last_session_save = 0.0   # 갱신된 쿠키를 디스크에 다시 저장한 시각
        # '새로 생긴(프런티어) 글'에서 add 가 거부된 연속 횟수. 이미 등록한 옛 글의 거부
        # (add_refused, 재시작 되감김)와 구분한다. 새 글에서까지 계속 거부되면 세션 자체가
        # 낡았을(연장 전 엔타이틀먼트를 물고 있는) 가능성이 있어 강제 재로그인 복구를 시도한다.
        self._fresh_refuse_streak = 0
        # 새 글에서 'no permission'(계정상태상 등록 대상 아님) 이 연속된 횟수. 개별-글 스킵은
        # 조용히 넘기고(고객의 계정은 멀쩡하므로 매번 '계정/배너 확인' 재알람을 띄우지 않는다),
        # '진짜 새로 생긴 글마다 전부' 거부되는 경우에만(=연속 임계 초과) 계정 힌트를 한 번 알린다.
        self._no_perm_streak = 0
        self._no_perm_warned = False   # 계정 힌트 알림은 상태당 1회만(재알람 방지)
        self._relogin_done = False   # 강제 재로그인은 실행당 1회만(무한 루프 방지)
        # '목록은 로그인인데 등록 API 만 404 error 로 거부'되는 상황(auth_mismatch)의 연속 횟수.
        # 세션이 살아있음이 logged_in() 로 확인됐는데도 등록만 계속 거부되면, 이건 세션 사망이
        # 아니라 계정/게시글 측 등록 불가 상태다. 조용히 초당 수십 회 재시도하며 사이트를
        # 두드리는 대신, 폴링 간격을 늘려(backoff) 부담을 줄이고 원인을 명확히 알린다.
        self._auth_mismatch_streak = 0
        # 세션 쿠키 진단(ezloan_sess 유무). 값은 절대 로그로 보내지 않는다.
        try:
            names = sorted({c.get("name", "") for c in cookies if c.get("name")})
        except Exception:
            names = []
        has_sess = any("ezloan_sess" in n or "ci_session" in n for n in names)
        self.remote(
            "registrar_init",
            f"쿠키 {len(cookies)}개, ezloan_sess={'있음' if has_sess else '없음'}, 쿠키명={names[:20]}",
            force=True,
        )

    def _send_auth_diag(self, pid):
        """등록 인증이 거부될 때(실행당 1회) 실제 요청/응답/쿠키 전체를 진단 API 로 덤프한다.

        사장님 요청: 추측 대신 실측 데이터로 원인 파악. 쿠키 값 전체와 실제로 나간 요청 헤더,
        응답 본문 전체를 그대로 보낸다(고객 세션 진단용, customerId 로 추적).
        """
        if self._diag_sent:
            return
        self._diag_sent = True
        try:
            # 1) 캡처된 쿠키 전체(이름/값/속성)
            cookie_dump = []
            for c in self._cookies_raw:
                cookie_dump.append({
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "path": c.get("path"),
                    "secure": c.get("secure"),
                    "httpOnly": c.get("httpOnly"),
                    "expiry": c.get("expiry") or c.get("expires"),
                })
            csrf_val = next((c.get("value") for c in self._cookies_raw
                             if c.get("name") == "csrf_cookie_ezloan"), None)

            # 2) 실패하는 register(check) 요청을 한 번 실제로 날려 요청/응답을 그대로 캡처
            url = f"{config.BASE_URL}/api/rq_addbanner_check/{pid}"
            r = self.s.get(url, timeout=12)
            req = r.request
            out_req = {
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "cookie_header": req.headers.get("Cookie"),
                "cookie_attached": bool(req.headers.get("Cookie")),
            }
            resp = {
                "status": r.status_code,
                "headers": dict(r.headers),
                "body": r.text,   # 전체 본문(작으므로 자르지 않음)
            }

            payload = {
                "csrf_cookie_ezloan": csrf_val,
                "cookies": cookie_dump,
                "session_headers_default": dict(self.s.headers),
                "request": out_req,
                "response": resp,
                "note": "same-origin Referer 없으면 msg='404 error'; Referer 있으면 인증 평가됨",
            }
            self.remote(
                "auth_diag_dump",
                f"post={pid} status={resp['status']} msg={_safe_msg(r)} "
                f"csrf_present={bool(csrf_val)} referer={self.s.headers.get('Referer')}",
                snapshot=json.dumps(payload, ensure_ascii=False),
                force=True,
            )
        except Exception as e:
            self.remote("auth_diag_dump", f"진단 덤프 실패: {e}", force=True)

    def _read_seen(self):
        if self.seen_path and self.seen_path.exists():
            try:
                return set(json.loads(self.seen_path.read_text(encoding="utf-8")).get("seen", []))
            except Exception:
                return set()
        return set()

    def _write_seen(self):
        if not self.seen_path:
            return
        try:
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
            vals = list(self.seen)[-1000:]
            self.seen_path.write_text(json.dumps({"seen": vals}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _persist_session(self, force=False):
        """이지론 서버가 갱신해 준 슬라이딩 쿠키(ezloan_sess Max-Age=7200)를 디스크에 다시 저장.

        저장을 안 하면 재시작 후 복구 세션이 만료된 옛 쿠키라 죽는다(배너봇에서 겪은 교훈).
        3분마다 한 번씩만 저장한다.
        """
        now = time.time()
        if not force and (now - self._last_session_save) < 180:
            return
        self._last_session_save = now
        try:
            from session_store import save_session
            cookies = [
                {"name": c.name, "value": c.value,
                 "domain": c.domain, "path": c.path or "/"}
                for c in self.s.cookies
            ]
            if cookies:
                save_session(cookies, log=self.log)
        except Exception:
            pass

    def _force_relogin(self):
        """새 글에서까지 등록이 지속 거부될 때, 새 이지론 세션을 다시 받아 온다(실행당 1회).

        가설(고객 문의): 광고 상품을 연장했는데도 앱이 '연장 전' 세션 쿠키를 그대로 물고
        있어 서버가 그 세션을 미-엔타이틀먼트로 취급할 수 있다. logged_in()==True 라 세션
        사망은 아니지만, 새 글에서까지 거부가 이어지면 세션 자체를 새로 발급받아 본다.
        relogin 콜백은 GUI 스레드에서 네이버 재로그인을 수행하고 새 cookies(list)를 준다.
        실측(2026-07-10)으로는 '동일 쿠키가 시간에 따라 success->no permission'으로 바뀌어
        원인이 서버측 계정 할당일 가능성이 크지만, 낡은-세션 케이스까지 커버하는 안전장치다.
        """
        self._relogin_done = True
        self.log("등록이 계속 거부되어 로그인 세션을 새로 받아옵니다(재로그인)...")
        self.remote("force_relogin_start",
                    "새 글에서도 등록 지속 거부(logged_in=True) -> 강제 재로그인으로 새 세션 확보 시도",
                    force=True)
        try:
            new_cookies = self.relogin()
        except Exception as e:
            new_cookies = None
            self.remote("force_relogin_error", f"재로그인 콜백 예외: {e}", force=True)
        if not new_cookies:
            self.remote("force_relogin_fail",
                        "재로그인 실패/취소 - 기존 세션으로 계속 진행", force=True)
            return False
        self.s = session_from_cookies(new_cookies)
        self._cookies_raw = new_cookies
        self._fresh_refuse_streak = 0
        self._persist_session(force=True)
        ok = logged_in(self.s)
        self.remote("force_relogin_done",
                    f"새 세션 확보(logged_in={ok}, 쿠키 {len(new_cookies)}개). 등록 재개.",
                    force=True)
        return ok

    def _heal_session(self):
        """중복 세션 쿠키를 정리하고 /rq 재조회로 슬라이딩 세션을 갱신한다.

        과거 버그(도메인 앞점 제거)로 만들어졌거나 복구된 세션에 ezloan_sess/csrf 가
        ezloan.io 와 .ezloan.io 두 도메인으로 중복 저장돼 있으면, 서버의 최신 값(.ezloan.io)
        대신 낡은 값(ezloan.io)이 요청에 먼저 실려 404 error 를 유발한다.
        같은 이름의 쿠키 중 앞점 없는(호스트 전용) 항목을 제거하고, /rq 를 다시 불러
        서버가 Set-Cookie 로 최신 세션을 심게 한 뒤 로그인 상태를 재확인한다.
        """
        try:
            dupe_names = ("ezloan_sess", "csrf_cookie_ezloan")
            # 이름별 도메인 목록 파악
            by_name = {}
            for c in self.s.cookies:
                if c.name in dupe_names:
                    by_name.setdefault(c.name, []).append(c.domain)
            for name, domains in by_name.items():
                has_dot = any(d.startswith(".") for d in domains)
                # 앞점 있는(.ezloan.io) 서버 표준 항목이 있으면, 앞점 없는 낡은 중복만 제거.
                if has_dot:
                    for d in list(domains):
                        if not d.startswith("."):
                            try:
                                self.s.cookies.clear(domain=d, path="/", name=name)
                            except Exception:
                                pass
            # /rq 재조회로 슬라이딩 세션 쿠키 갱신(서버가 최신 ezloan_sess Set-Cookie 를 준다)
            self.s.get(config.RQ_URL, timeout=10, allow_redirects=True)
            _sync_csrf_header(self.s)
            ok = logged_in(self.s)
            if ok:
                # 갱신된 단일 세션 쿠키를 디스크에도 반영.
                self._persist_session(force=True)
            return ok
        except Exception:
            return False

    def _backoff_seconds(self):
        """auth_mismatch 가 이어질수록 폴링 간격을 늘려 사이트 부담/차단 위험을 줄인다.

        기본 POLL_SECONDS(1.5s)에서 시작해 연속 mismatch 마다 지수적으로 늘리되 60초로 캡.
        정상(등록 재개)으로 돌아오면 _auth_mismatch_streak 가 0 이 되어 즉시 정상 주기로 복귀.
        """
        base = float(getattr(config, "POLL_SECONDS", 1.5))
        n = max(0, self._auth_mismatch_streak - 1)
        return min(60.0, base * (2 ** min(n, 5)))

    def _window_label(self):
        return (f"{config.RUN_START_HOUR:02d}:00~{config.RUN_END_HOUR:02d}:00 KST"
                if getattr(config, "RUN_WINDOW_ENABLED", True) else "24시간")

    def _idle_outside_window(self):
        """운영 시간대 밖이면(기본 23:00~08:00 KST) API 를 두드리지 않고 대기한다.

        창/프로세스는 그대로 살려두고, IDLE_CHECK_SECONDS 마다 시각만 확인한다.
        시간대 안으로 다시 들어오면 True 를 돌려주어 상위 루프가 정상 등록을 재개하게 한다.
        (should_stop 이 걸리거나 시간대에 들어오면 대기를 끝낸다.)
        """
        if in_run_window():
            return True
        started = now_kst()
        self.log(
            f"운영 시간대({self._window_label()})가 아니어서 대기합니다. "
            f"{config.RUN_START_HOUR:02d}시가 되면 자동으로 다시 시작합니다."
        )
        self.remote(
            "idle_outside_window",
            f"운영시간대 밖({started:%Y-%m-%d %H:%M} KST) - 등록 대기, "
            f"창은 유지. 재개 예정 {config.RUN_START_HOUR:02d}:00 KST, 시간대={self._window_label()}",
            force=True,
        )
        while not self.should_stop():
            if in_run_window():
                self.log(f"운영 시간대에 진입했습니다. 자동등록을 재개합니다.")
                self.remote(
                    "resume_in_window",
                    f"운영시간대 재진입({now_kst():%Y-%m-%d %H:%M} KST) - 등록 재개",
                    force=True,
                )
                return True
            self._wait(config.IDLE_CHECK_SECONDS)
        return False

    def run(self):
        if not logged_in(self.s):
            self.log("세션이 유효하지 않습니다. 다시 로그인해 주세요.")
            self.remote("session_invalid", "requests 세션이 이지론 로그인 상태가 아님(등록 API 인증 실패 예상)", force=True)
            return
        # 시작 시점이 운영 시간대 밖이면(기본 23:00~08:00 KST) 로그인 상태만 확인해 두고,
        # API 를 두드리지 않고 시간대 진입까지 대기한다(창은 유지).
        if not self._idle_outside_window():
            self.remote("run_stopped", "시작 대기 중 정지됨", force=True)
            return
        self.log("자동 등록 시작됨")
        self.remote("run_started", f"폴링 루프 시작(운영시간대 {self._window_label()})", force=True)

        # 시작/재시작 재기준화: 지금 목록의 '실제 최신 글'을 기준으로 프런티어를 잡고,
        # 현재 목록 전체를 seen 으로 흡수한다. 이렇게 하면 이후엔 '진짜 새로 생기는 글'만
        # 등록하고, 재시작 전에 이미 처리한(등록한) 옛 글들을 다시 add 로 두드리지 않는다.
        # (핵심 재현: 예전엔 look-ahead 로 프런티어가 유령 번호까지 폭주한 값이었다가, 재시작
        #  때 실제 최신글 기준으로 되감겨 '이미 등록한 실재 글'을 재-add -> 전부 '404 error'
        #  -> 등록 0 + 거짓 auth_mismatch 였다. 이제 목록을 seen 에 흡수해 재-add 를 막는다.
        #  혹시 look-ahead 가 그런 옛 글을 집더라도 register() 가 add_refused 로 조용히 건너뛴다.)
        baseline = list_post_ids(self.s)
        before = len(self.seen)
        self.seen.update(baseline)
        self._write_seen()
        known_max = max((int(x) for x in list(self.seen) + baseline if str(x).isdigit()), default=0)
        frontier = known_max + 1
        self.remote(
            "baseline",
            f"초기 목록 {len(baseline)}개(seen 흡수 {len(self.seen) - before}건 추가), "
            f"최대번호={known_max}, frontier={frontier}(재시작 시 옛 글 재등록 방지)",
            force=True,
        )

        while not self.should_stop():
            try:
                # 운영 시간대(기본 08:00~23:00 KST) 밖이면 등록을 멈추고 대기한다.
                # 창은 살아있고, 시간대 재진입 후 세션을 자가 치유한 뒤 baseline 을 다시 잡아
                # 대기 동안 쌓인 글을 한꺼번에 재등록하지 않도록 한다(이미 처리된 것으로 간주).
                if not in_run_window():
                    if not self._idle_outside_window():
                        break  # 대기 중 정지 요청
                    self._heal_session()
                    fresh = list_post_ids(self.s)
                    self.seen.update(fresh)
                    self._write_seen()
                    for x in fresh:
                        if str(x).isdigit() and int(x) >= frontier:
                            frontier = int(x) + 1
                    self._session_lost_streak = 0
                    self.remote(
                        "rebaseline_after_idle",
                        f"대기 후 목록 재기준화 {len(fresh)}개, frontier={frontier}",
                        force=True,
                    )

                self._cycle += 1
                # 0) 프런티어 자가 보정(runaway 회복): 프런티어는 '실제 최신 글 + 1'을 기준으로
                # look-ahead 창(LOOKAHEAD)만큼만 앞서야 한다. 계정이 잠시 no_permission 상태에
                # 빠지는 등으로 과거에 프런티어가 미존재(유령) 번호까지 폭주했다면, 재시작 없이도
                # 여기서 실제 목록 기준으로 되돌린다. 목록의 글은 이미 seen 에 흡수되므로 되돌려도
                # 재-add 가 나지 않고, look-ahead 가 다시 '진짜 새 글'을 즉시 잡을 수 있게 된다.
                # (되돌리는 하한은 실제 최신글 바로 다음 번호. 그보다 낮추지 않아 옛 글 재처리 없음.)
                # 목록은 사이클당 '한 번만' 가져온다(각 ~309KB). 예전엔 프런티어 재동기화용과
                # 안전망용으로 목록을 두 번 받았는데(v2.4.3~), 그 여분의 왕복이 사이클을 늘려
                # 새 글 감지·등록을 늦췄다. 같은 목록을 재동기화·안전망에 함께 쓴다.
                ids = list_post_ids(self.s)
                real_max = max((int(x) for x in ids if str(x).isdigit()), default=0)
                if real_max:
                    sane_frontier = real_max + 1
                    # 정상 앞섬(창 크기)보다 크게 벗어났을 때만 되돌린다(정상 미세 앞섬은 유지).
                    if frontier > sane_frontier + config.LOOKAHEAD:
                        self.remote(
                            "frontier_resync",
                            f"프런티어 폭주 감지: frontier={frontier} > 실제최신({real_max})+1+창"
                            f"({sane_frontier + config.LOOKAHEAD}). "
                            f"실제 목록 기준 frontier={sane_frontier} 로 되돌림(옛 글 재처리 없음).",
                            force=True,
                        )
                        frontier = sane_frontier
                # 1) look-ahead: 새 글을 가장 빨리 잡아 즉시 등록(상단 1등 경쟁의 핵심 경로).
                if config.LOOKAHEAD > 0:
                    ahead, safe_frontier = lookahead_ids(self.s, frontier, config.LOOKAHEAD)
                    # 프런티어는 '존재가 확인된 번호'까지만 전진시킨다. lookahead 가 돌려준
                    # safe_frontier 는 아직 안 생긴/미확정 첫 번호이므로, 이 값을 넘겨 올리면
                    # 유령(미래) 번호를 지나쳐 나중에 실제 글이 생겨도 못 잡는다.
                    if safe_frontier > frontier:
                        frontier = safe_frontier
                    for pid, precheck in ahead:
                        if pid in self.seen:
                            continue
                        # register() 가 precheck(rq_addbanner_check 결과)를 재사용하므로 이 글엔
                        # rq_addbanner 만 한 번 더 쏘면 된다 -> 경쟁사보다 먼저 상단을 잡는다.
                        registered_ok = self._handle(pid, precheck=precheck)
                        # 실존이 확정된(등록 성공/이미등록/거부-실재) 글이면 프런티어를 그 너머로
                        # 전진시킨다. post_absent(유령/미래 번호)면 전진하지 않아 폭주를 막는다.
                        if registered_ok and pid.isdigit() and int(pid) >= frontier:
                            frontier = int(pid) + 1
                        if self.should_stop():
                            break
                # 2) listing safety net (위에서 이미 받은 목록 재사용)
                new = [i for i in ids if i not in self.seen]
                # 사이클 진단: 목록 수 / 새 글 수 / 세션-없음 연속 카운트 (10초 디바운스)
                self.remote(
                    "cycle",
                    f"#{self._cycle} 목록={len(ids)} 새글={len(new)} "
                    f"누적확인={len(self.seen)} 등록={self._registered_total} "
                    f"세션없음연속={self._session_lost_streak} frontier={frontier}",
                )
                for pid in sorted(new, key=int, reverse=True):
                    if pid.isdigit() and int(pid) >= frontier:
                        frontier = int(pid) + 1
                    self._handle(pid)
                    if self.should_stop():
                        break
                if not new:
                    self.log(f"모니터링 중... ({len(self.seen)}개 확인됨)")

                # 갱신된 세션 쿠키를 주기적으로 디스크에 저장(재시작 복구가 살아있게 유지).
                self._persist_session()

                # 세션이 실제로 죽었는지 능동 점검.
                # 1) 먼저(연속 2회) 자가 치유 시도: 중복 세션 쿠키 정리 + /rq 재조회로
                #    슬라이딩 세션 쿠키를 갱신한 뒤 다시 돈다(재로그인 없이 회복).
                if self._session_lost_streak == 2:
                    healed = self._heal_session()
                    self.remote(
                        "auth_selfheal",
                        f"연속 {self._session_lost_streak}회 인증 거부 -> 중복 쿠키 정리+세션 갱신 시도"
                        f"(logged_in={healed}).",
                        force=True,
                    )
                    if healed:
                        # 갱신 성공. 스트릭을 반만 낮춰 회복 여부를 다음 사이클로 재평가한다.
                        self._session_lost_streak = 1

                # 2) 자가 치유로도 계속 거부되면(연속 4회) 조용히 도는 대신 크게 알린다.
                if self._session_lost_streak >= 4:
                    if not logged_in(self.s):
                        # logged_in() 이 유일한 세션 사망 판정 권한이다. 여기서만 재로그인을 요구한다.
                        self.log("로그인 세션이 만료되었습니다. 프로그램을 다시 시작해 로그인해 주세요. (재로그인이 필요합니다)")
                        self.remote(
                            "session_expired",
                            f"연속 {self._session_lost_streak}회 인증 실패(msg='404 error'), "
                            "중복 쿠키 정리+세션 갱신 후에도 회복 실패. 재로그인 필요.",
                            force=True,
                        )
                        return
                    # 목록 페이지는 로그인으로 보이는데 등록 API 만 계속 404 error 로 거부되는 상황.
                    # 세션은 살아있으므로(logged_in=True) 이건 세션 사망이 아니라 계정/게시글 측
                    # 등록 불가 상태다(예: 유료 배너 소진, 계정 권한/정지, 이지론 서버 일시 이상).
                    # 예전엔 이걸 매 사이클 초당 수십 회 auth_mismatch 로그로만 남기며 사이트를
                    # 계속 두드렸다(2026-07-08 로그: 15분간 auth_mismatch 수백 회, 등록은 1건).
                    # 이제는 backoff(폴링 간격을 늘림)로 부담을 줄이고, 원인을 사장님이 조치할 수
                    # 있게 명확한 문구로 알린다. 세션은 살아있으니 재로그인을 요구하지 않는다.
                    self._auth_mismatch_streak += 1
                    self._session_lost_streak = 0
                    if self._auth_mismatch_streak == 1:
                        # 정정(2026-07-10 실측): rq_addbanner 의 '404 error' 는 '유료 광고 없음'
                        # 을 뜻하지 않는다. rq_addbanner_check 가 유료광고/배너 잔여를 이미 통과
                        # (result:true, amount 정상)했다면 계정·유료광고는 정상이다. 이 경우 add
                        # '404 error' 는 대부분 '해당 글이 존재하지 않음'(아직 안 생긴 미래 번호/
                        # 삭제된 글)이며, 이제는 post_absent 로 걸러 세션·유료광고 오진 없이 건너뛴다.
                        # 그런데도 실제 존재하는 글에서까지 add 가 계속 404 라면 이지론 서버의 일시
                        # 이상이 가장 유력하므로, 재로그인/유료광고 연장을 요구하지 않고 잠시 뒤 자동
                        # 재시도한다(광고가 다시 진행되면/서버가 회복되면 자동으로 등록 재개).
                        self.log(
                            "로그인·배너 잔여 개수는 정상인데 등록 API 가 일시적으로 거부되고 있습니다. "
                            "자동으로 잠시 뒤 다시 시도합니다. (재로그인은 필요 없습니다. "
                            "이 상태가 오래 지속되면 이지론 사이트가 일시적으로 불안정한 것일 수 있습니다.)"
                        )
                    self.remote(
                        "auth_mismatch",
                        f"실제 존재하는 글에서도 rq_addbanner 가 계속 404 로 거부됨"
                        f"(logged_in=True, rq_addbanner_check=통과/amount 정상, post_exists=True, "
                        f"연속 {self._auth_mismatch_streak}회). 세션 사망 아님, 유료광고/글-미존재도 아님"
                        "(그건 post_absent 로 이미 분리). 남는 유력 원인은 이지론 서버 일시 이상. "
                        "재로그인/광고연장 요구하지 않고 backoff 후 자동 재시도. "
                        f"backoff 폴링={self._backoff_seconds():.0f}s.",
                        force=(self._auth_mismatch_streak <= 3 or self._auth_mismatch_streak % 20 == 0),
                    )
                    self._wait(self._backoff_seconds())
                    continue
                # 등록이 다시 되기 시작하면 backoff 를 푼다.
                if self._registered_total and self._auth_mismatch_streak:
                    self._auth_mismatch_streak = 0
            except Exception as e:
                import traceback
                self.log(f"오류(계속 시도 중): {e}")
                self.remote("run_error", traceback.format_exc()[:1500], force=True)
                if not logged_in(self.s):
                    self.log("로그인이 만료되었습니다. 다시 로그인해 주세요.")
                    self.remote("session_expired", "loop 예외 후 세션 무효 확인", force=True)
                    return
            self._wait(config.POLL_SECONDS)
        self.remote("run_stopped", "폴링 루프 중지", force=True)

    def _handle(self, pid, precheck=None):
        """이 글을 등록 시도한다. 반환값: 이 글이 '실존이 확정'됐는지(bool).

        상위 lookahead 루프는 이 반환값으로 프런티어 전진을 결정한다. post_absent(아직 안
        생긴 미래/유령 번호)만 False 이고, 그 외(등록 성공/이미 등록/실재하나 거부 등)는 전부
        실존이 확정된 것이므로 True. 안전망(목록) 경로는 반환값을 쓰지 않는다(무해).
        """
        result = register(self.s, pid, precheck=precheck)
        note = result.get("note", "")
        status = result.get("status")
        msg = result.get("msg")
        body = result.get("body")

        # 세션-없음 신호 추적: 연속 카운트로 '조용히 멈춤'을 불가능하게 만든다.
        if result.get("session_lost"):
            self._session_lost_streak += 1
        elif result.get("ok"):
            self._session_lost_streak = 0

        if result.get("ok") or note in NON_RETRYABLE:
            self.seen.add(pid)
            self._write_seen()

        if result.get("ok") and result.get("rank"):
            self._registered_total += 1
            self._session_lost_streak = 0
            self._auth_mismatch_streak = 0
            self._fresh_refuse_streak = 0
            self._no_perm_streak = 0
            self._no_perm_warned = False
            self.log(f"등록 완료: {pid} (순위 {result['rank']}위)")
            self.remote("registered", f"post={pid} rank={result['rank']} msg={msg}", force=True)
        elif result.get("ok"):
            self._registered_total += 1
            self._session_lost_streak = 0
            self._auth_mismatch_streak = 0
            self._fresh_refuse_streak = 0
            self._no_perm_streak = 0
            self._no_perm_warned = False
            self.remote("registered", f"post={pid} rank=미확인 note={note} msg={msg}", force=True)
        elif result.get("session_lost"):
            # 인증 실패. 실행당 1회, 요청/응답/쿠키 전체를 진단 API 로 덤프한다.
            self._send_auth_diag(pid)
            # 인증 실패. 개별 글마다 조용히 넘기지 않고 상태/응답을 그대로 보고한다.
            self.remote(
                "register_auth_fail",
                f"post={pid} status={status} msg={msg} note={note} "
                f"streak={self._session_lost_streak}"
                + (f" body={body}" if body else ""),
                force=(self._session_lost_streak <= 2),
            )
        elif note == "no_permission":
            # 로그인은 유효하나 이 글이 지금 이 계정의 등록 대상이 아님(개별-글 스킵). 세션소실이
            # 아니므로 session_lost streak 는 올리지 않는다(거짓 session_expired 방지).
            #
            # 재알람 방지(고객 이력): 이 고객은 예전에 '이지론 계정/배너 상태 확인'이 오진이었고
            # (계정은 정상, D-35, 수동 등록 정상), 그 문구가 불필요한 불안을 줬다. 그러므로 '한 건
            # 스킵'을 계정/배너 문제로 단정해 매번 확인을 요구하지 않는다. 개별-글 스킵은 조용히
            # (중립 문구로) 넘긴다. 계정 관련 힌트는 '진짜 새로 생긴 글마다 전부' 거부될 때만
            # (=연속 임계 초과) 딱 한 번 띄운다(실제 증거가 있을 때만).
            self._no_perm_streak += 1
            self.log("이미 처리했거나 지금 등록 대상이 아닌 건이라 건너뜁니다. (로그인·자동등록은 정상 동작 중)")
            self.remote(
                "register_no_permission",
                f"post={pid} status={status} msg={msg} note={note} "
                f"(로그인 유효, 이 글은 현재 등록 대상 아님 - 개별-글 스킵, "
                f"새글연속거부={self._no_perm_streak})",
                # 개별 스킵은 조용히(디바운스), 연속 거부가 쌓일 때만 크게 알린다.
                force=(self._no_perm_streak >= config.NO_PERM_WARN_STREAK),
            )
            # 새로 생기는 글마다 계속(임계 이상) 거부되면, 그건 개별-글이 아니라 계정 측
            # 등록 자격 문제일 수 있다. 이때만 계정 힌트를 딱 한 번 알린다(재알람 방지).
            if (self._no_perm_streak >= config.NO_PERM_WARN_STREAK
                    and not self._no_perm_warned):
                self._no_perm_warned = True
                self.log(
                    "새로 올라오는 글마다 계속 '등록 대상 아님'으로 거부되고 있습니다. "
                    "로그인은 정상이니 재로그인은 필요 없고, 이지론 광고 상품 상태(진행 여부/기간)를 "
                    "한 번 확인해 주세요. (상태가 정상이면 자동으로 등록이 재개됩니다.)"
                )
                self.remote(
                    "no_permission_persistent",
                    f"새 글에서 연속 {self._no_perm_streak}회 'no permission' 거부 "
                    "(logged_in=True). 개별-글이 아니라 계정 측 등록 자격 문제 가능성 - "
                    "계정 힌트 1회 알림.",
                    force=True,
                )
        elif note == "post_absent":
            # check 는 통과(세션/유료광고 정상)했으나 글이 아직 존재하지 않아 add 가 '404 error'.
            # 세션 사망도 유료광고 문제도 아니다. 조용히 건너뛰고(seen 처리됨) 프런티어만 전진.
            self.remote(
                "register_post_absent",
                f"post={pid} status={status} msg={msg} note={note} "
                "(글 미존재/미래 번호 - 세션·유료광고 정상, 건너뜀)",
            )
        elif note == "add_refused":
            # 글은 실재하는데 add 가 거부됨(이미 내 배너 있음/계정상태상 등록 불가). seen 에
            # 들어가 재-add 는 막힌다. _handle 은 'seen 에 없는 글'에만 불리므로, 여기 온 건
            # '새로 나타난 글'에서의 거부다. 이게 연속되면 세션이 낡아(연장 전 엔타이틀먼트를
            # 물고 있어) 새 글도 못 붙는 경우일 수 있으니, 강제 재로그인 복구를 한 번 시도한다.
            self._fresh_refuse_streak += 1
            self.remote(
                "register_add_refused",
                f"post={pid} status={status} msg={msg} note={note} "
                f"(글 실재하나 등록 거부 - 이미 등록됨/계정상태상 불가, 세션 사망 아님, "
                f"새글거부연속={self._fresh_refuse_streak})",
                force=(self._fresh_refuse_streak <= 2),
            )
            if (self._fresh_refuse_streak >= 3 and self.relogin
                    and not self._relogin_done and logged_in(self.s)):
                self._force_relogin()
        else:
            # 정상적으로 '등록 불가'인 글들(예: max/no ads 등)은 디바운스 로그.
            self.remote("register_skip", f"post={pid} status={status} msg={msg} note={note}")

        # 실존 확정 여부(상위 lookahead 의 프런티어 전진 판단용).
        #  - post_absent: 아직 안 생긴 미래/유령 번호 -> 실존 아님(전진 금지, 폭주 방지).
        #  - check_http_error / add_error / add_failed / session_lost: 존재를 판정 못 했으니
        #    보수적으로 '미확정'(False) -> 프런티어 전진하지 않고 다음 사이클에 다시 시도한다
        #    (실제 새 글을 성급히 지나쳐 놓치지 않도록).
        #  - 그 외(등록 성공/이미 등록/no_permission/add_refused): 글이 실재함 -> 전진 가능.
        if note == "post_absent":
            return False
        if result.get("ok"):
            return True
        if note in ("no_permission", "add_refused", "already_registered_api",
                    "slots_full", "no_banner_amount", "no_ads", "no_payed_ads"):
            return True
        return False

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(0.1)
