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
_NOTE_MAP = {
    "no permission": "login_required",
    "no amount": "no_banner_amount",
    "no ads": "no_ads",
    "no payed ads": "no_payed_ads",
    "max": "slots_full",
    "ing": "already_registered_api",
}


def session_from_cookies(cookies):
    s = requests.Session()
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"], domain=(c.get("domain") or "ezloan.io").lstrip("."))
        except Exception:
            continue
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    })
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
    r = s.get(f"{config.BASE_URL}/api/rq_addbanner_check/{pid}", timeout=12)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def probe_state(s, pid):
    """open / ing / absent / blocked / error"""
    try:
        code, data = _check(s, pid)
    except Exception:
        return "error"
    if code != 200:
        return "error"
    if data.get("result") is True:
        return "open"
    msg = (data.get("msg") or "").strip().lower()
    if "404" in msg or "존재하지" in msg or "삭제" in msg:
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
        return {"ok": False, "rank": None, "note": "check_http_error"}
    if data.get("result") is not True:
        msg = (data.get("msg") or "unknown").strip().lower()
        return {"ok": msg == "ing", "rank": None, "note": _NOTE_MAP.get(msg, f"check_failed:{msg}")}
    try:
        r = s.get(f"{config.BASE_URL}/api/rq_addbanner/{pid}", timeout=12)
        add = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        return {"ok": False, "rank": None, "note": f"add_error:{e}"}
    if r.status_code != 200 or add.get("result") is not True:
        return {"ok": False, "rank": None, "note": "add_failed"}
    rank = company_rank(s, pid, company)
    for _ in range(3):
        if rank:
            break
        time.sleep(0.15)
        rank = company_rank(s, pid, company)
    return {"ok": True, "rank": rank, "note": "registered" if rank else "registered_not_verified"}


class Registrar:
    """로그인 후 requests 세션으로 등록 루프를 돈다."""

    def __init__(self, cookies, log=print, remote=None, should_stop=None,
                 seen_path=None):
        self.s = session_from_cookies(cookies)
        self.log = log
        self.remote = remote or (lambda *a, **k: None)
        self.should_stop = should_stop or (lambda: False)
        self.seen_path = Path(seen_path) if seen_path else None
        self.seen = self._read_seen()

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

    def run(self):
        if not logged_in(self.s):
            self.log("세션이 유효하지 않습니다. 다시 로그인해 주세요.")
            self.remote("session_invalid", "registrar start", force=True)
            return
        self.log("자동 등록 시작됨")
        self.remote("run_started", "폴링 루프 시작", force=True)

        baseline = list_post_ids(self.s)
        if not self.seen:
            self.seen.update(baseline)
            self._write_seen()
        known_max = max((int(x) for x in list(self.seen) + baseline if str(x).isdigit()), default=0)
        frontier = known_max + 1

        while not self.should_stop():
            try:
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
                for pid in sorted(new, key=int, reverse=True):
                    if pid.isdigit() and int(pid) >= frontier:
                        frontier = int(pid) + 1
                    self._handle(pid)
                    if self.should_stop():
                        break
                if not new:
                    self.log(f"모니터링 중... ({len(self.seen)}개 확인됨)")
            except Exception as e:
                self.log(f"오류(계속 시도 중): {e}")
                self.remote("run_error", str(e)[:500], force=True)
                if not logged_in(self.s):
                    self.log("로그인이 만료되었습니다. 다시 로그인해 주세요.")
                    self.remote("session_expired", "loop", force=True)
                    return
            self._wait(config.POLL_SECONDS)
        self.remote("run_stopped", "폴링 루프 중지", force=True)

    def _handle(self, pid):
        result = register(self.s, pid)
        note = result.get("note", "")
        if result.get("ok") or note in NON_RETRYABLE:
            self.seen.add(pid)
            self._write_seen()
        if result.get("ok") and result.get("rank"):
            self.log(f"등록 완료: {pid} (순위 {result['rank']}위)")
            self.remote("registered", f"post={pid} rank={result['rank']}", force=True)
        elif note in ("login_required",):
            self.log("로그인이 만료되었습니다. 다시 로그인해 주세요.")
            self.remote("session_expired", f"post={pid}", force=True)
        else:
            self.remote("register_skip", f"post={pid} note={note}")

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(0.1)
