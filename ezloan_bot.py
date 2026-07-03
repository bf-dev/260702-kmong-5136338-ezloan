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
from pathlib import Path

import requests

import config

_RQ_LINK_RE = re.compile(r'/rq/(\d+)(?:["\'/?#]|$)')
NON_RETRYABLE = {"slots_full", "no_banner_amount", "no_ads", "no_payed_ads"}
# 세션이 없거나(로그인 만료/미인증) 서버가 인증을 거부할 때 나오는 신호.
# 실측: 미인증 상태에서 /api/rq_addbanner_check 는 result:false, msg:"404 error" 를 준다.
# ("no permission" 은 로그인은 됐으나 그 글은 등록 불가일 때 나온다 - 둘을 구분한다.)
SESSION_LOST_MSGS = {"404 error", "no permission", "no session", "session", "login", "unauthorized"}
_NOTE_MAP = {
    "no permission": "login_required",
    "404 error": "login_required",
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


def probe_state(s, pid):
    """open / ing / absent / blocked / no_session / error"""
    try:
        code, data = _check(s, pid)
    except Exception:
        return "error"
    if code != 200:
        return "error"
    if data.get("result") is True:
        return "open"
    msg = (data.get("msg") or "").strip().lower()
    # "404 error" 는 '글이 없음'이 아니라 '세션 없음' 신호다(실측). absent 로 착각하면 안 된다.
    if _is_session_lost_msg(msg):
        return "no_session"
    if "존재하지" in msg or "삭제" in msg:
        return "absent"
    if msg == "ing":
        return "ing"
    return "blocked"


def lookahead_ids(s, frontier, window):
    found, pid, end, absents = [], frontier, frontier + window, 0
    while pid < end:
        st = probe_state(s, str(pid))
        if st in ("open", "ing"):
            found.append(str(pid)); absents = 0
        elif st == "blocked":
            absents = 0
        elif st == "absent":
            absents += 1
            if absents >= 2:
                break
        elif st == "no_session":
            # 세션이 죽었다. lookahead 를 즉시 접고 상위 루프가 세션 점검을 하게 한다.
            break
        else:
            break
        pid += 1
    found.sort(key=int, reverse=True)
    return found


def company_rank(s, pid, company=config.COMPANY_NAME):
    try:
        r = s.get(f"{config.BASE_URL}/rq/{pid}", timeout=10)
    except Exception:
        return 0
    blocks = re.split(r"<li[\s>]", r.text)[1:]
    idx = 0
    for block in blocks:
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()
        if not plain or "배너 등록을 눌러 주세요" in plain:
            continue
        idx += 1
        if company in plain:
            return idx
    return 0


def register(s, pid, company=config.COMPANY_NAME):
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
                 seen_path=None):
        self.s = session_from_cookies(cookies)
        self._cookies_raw = cookies or []
        self._diag_sent = False   # auth_diag_dump 는 실행당 1회만
        self.log = log
        self.remote = remote or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        self.seen_path = Path(seen_path) if seen_path else None
        self.seen = self._read_seen()
        # 진단용 상태
        self._session_lost_streak = 0   # 연속 세션-없음 신호 카운트
        self._cycle = 0
        self._registered_total = 0
        self._last_session_save = 0.0   # 갱신된 쿠키를 디스크에 다시 저장한 시각
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

    def run(self):
        if not logged_in(self.s):
            self.log("세션이 유효하지 않습니다. 다시 로그인해 주세요.")
            self.remote("session_invalid", "requests 세션이 이지론 로그인 상태가 아님(등록 API 인증 실패 예상)", force=True)
            return
        self.log("자동 등록 시작됨")
        self.remote("run_started", "폴링 루프 시작", force=True)

        baseline = list_post_ids(self.s)
        if not self.seen:
            self.seen.update(baseline)
            self._write_seen()
        known_max = max((int(x) for x in list(self.seen) + baseline if str(x).isdigit()), default=0)
        frontier = known_max + 1
        self.remote("baseline", f"초기 목록 {len(baseline)}개, 최대번호={known_max}, frontier={frontier}", force=True)

        while not self.should_stop():
            try:
                self._cycle += 1
                # 1) look-ahead
                if config.LOOKAHEAD > 0:
                    for pid in lookahead_ids(self.s, frontier, config.LOOKAHEAD):
                        if int(pid) >= frontier:
                            frontier = int(pid) + 1
                        if pid in self.seen:
                            continue
                        self._handle(pid)
                        if self.should_stop():
                            break
                # 2) listing safety net
                ids = list_post_ids(self.s)
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

                # 2) 자가 치유로도 계속 거부되면(연속 4회) 조용히 도는 대신 크게 알리고 중단한다.
                if self._session_lost_streak >= 4:
                    if not logged_in(self.s):
                        self.log("로그인 세션이 만료되었습니다. 프로그램을 다시 시작해 로그인해 주세요. (재로그인이 필요합니다)")
                        self.remote(
                            "session_expired",
                            f"연속 {self._session_lost_streak}회 인증 실패(msg='404 error'/'no permission'), "
                            "중복 쿠키 정리+세션 갱신 후에도 회복 실패. 재로그인 필요.",
                            force=True,
                        )
                        return
                    # 목록 페이지는 로그인으로 보이는데 등록 API 만 계속 거부되는 특이 상황
                    self.remote(
                        "auth_mismatch",
                        f"목록은 로그인 상태로 보이나 등록 API 가 연속 {self._session_lost_streak}회 거부됨"
                        "(중복 쿠키 정리 후에도 지속).",
                        force=True,
                    )
                    self._session_lost_streak = 0
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

    def _handle(self, pid):
        result = register(self.s, pid)
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
            self.log(f"등록 완료: {pid} (순위 {result['rank']}위)")
            self.remote("registered", f"post={pid} rank={result['rank']} msg={msg}", force=True)
        elif result.get("ok"):
            self._registered_total += 1
            self._session_lost_streak = 0
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
        else:
            # 정상적으로 '등록 불가'인 글들(예: max/no ads 등)은 디바운스 로그.
            self.remote("register_skip", f"post={pid} status={status} msg={msg} note={note}")

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(0.1)
