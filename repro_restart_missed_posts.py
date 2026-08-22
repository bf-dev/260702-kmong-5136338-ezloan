# -*- coding: utf-8 -*-
"""v2.6.1 회귀 게이트: 재시작 중(프로그램이 꺼져 있던 동안) 올라온 글을 건너뛰면 안 된다.

발견 경위 (2026-08-05, 고객 5136338):
  라이브로 글 31150~31244 를 훑어보니 이 고객의 배너가 아예 없는 글이 10건 있었다 -
  31165~31174(아홉 연속) + 31203. 같은 날 로그에는 그 번호대에서 재시작 흔적
  (사이클 카운터 리셋 #283117 -> #23117, session_recovered)이 있었다.
  원인: run() 이 시작할 때 `self.seen.update(list_post_ids(...))` 로 '현재 목록 전체'를
  통째로 흡수했다. 꺼져 있는 동안 올라온 글도 '이미 본 글'로 도장이 찍혀 영원히
  등록 대상에서 빠졌다(안전망마저 걸러낸다).

수정: 흡수 기준을 '디스크에 저장된 seen 의 최대 글번호(prev_max)'로 잡는다.
  prev_max 이하 -> 재시작 전에 처리한 글이므로 흡수(옛 글 재-add 방지, 기존 동작 유지)
  prev_max 초과 -> 꺼져 있는 동안 올라온 글이므로 남겨서 등록을 시도
  단, 며칠 꺼져 있던 경우까지 전부 따라잡으면 옛 글에 배너 잔여를 낭비하므로
  최신 RESTART_CATCHUP_MAX 개까지만 따라잡는다.

두 시나리오를 돌린다.
  A) 짧은 재시작: 꺼져 있는 동안 10건 올라옴 -> 10건 전부 등록되어야 한다(v2.6.0 은 0건).
  B) 긴 정지: 20건 올라옴 -> 상한(RESTART_CATCHUP_MAX)만큼만 등록되고 나머지는 흡수.
     (배너 잔여를 옛 글에 무한정 쓰지 않는다는 보증)

가짜 시계로 돌린다. 네트워크 접근 없음.
"""
import config
import ezloan_bot as eb

PREV_MAX = 31160                                   # 재시작 직전까지 처리해 둔 최신 글
OLD_IDS = list(range(PREV_MAX - 9, PREV_MAX + 1))  # 31151~31160 (디스크 seen 에 있음)

FAKE_NOW = [0.0]


class FakeResp:
    def __init__(self, status=200, text="", js=None, ctype="application/json"):
        self.status_code = status
        self.text = text
        self._js = js
        self.url = ""
        self.headers = {"content-type": ctype}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class _InlineFuture:
    def __init__(self, fn, args):
        try:
            self._v, self._e = fn(*args), None
        except Exception as e:   # noqa: BLE001 - 테스트 하네스
            self._v, self._e = None, e

    def result(self, timeout=None):
        if self._e:
            raise self._e
        return self._v


class InlinePool:
    def submit(self, fn, *args):
        return _InlineFuture(fn, args)


class Scenario:
    """프로그램이 꺼져 있는 동안 missed 개의 글이 새로 올라온 상태를 만든다."""

    def __init__(self, missed):
        self.live = OLD_IDS + [PREV_MAX + i for i in range(1, missed + 1)]
        self.missed = [PREV_MAX + i for i in range(1, missed + 1)]
        self.registered = set()
        self.writes = []          # (시각, pid)

    def list_ids(self):
        return sorted(self.live, reverse=True)[: config.MAX_POSTS]


class FakeSession:
    def __init__(self, sc):
        self.sc = sc
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        sc = self.sc
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in sc.list_ids())
            r = FakeResp(200, text="로그아웃 광고 관리 " + links)
            r.url = url
            return r
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            if pid in sc.live:
                body = "배너 등록을 눌러 주세요 js-memberConfirmView " + ("x" * 260000)
                if pid in sc.registered:
                    body += (f'<a href="/l/585" class="item"><div class="name">'
                             f'{config.COMPANY_NAME}</div>')
                r = FakeResp(200, text=body)
            else:
                r = FakeResp(200, text="<script>alert('삭제되었거나 존재하지 않은 문의입니다');"
                                       + ("y" * 280) + "</script>")
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 491})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            sc.writes.append((FAKE_NOW[0], pid))
            if pid in sc.live and pid not in sc.registered:
                sc.registered.add(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def build_registrar(sc):
    r = eb.Registrar.__new__(eb.Registrar)
    r.s = FakeSession(sc)
    r.probe = FakeSession(sc)
    r._probe_pool = InlinePool()
    r._last_check = None
    r._cookies_raw = []
    r._diag_sent = False
    r.log = lambda *a, **k: None
    r.status = lambda *a, **k: None
    r.remote = lambda *a, **k: None
    r.relogin = None
    r.seen_path = None
    # 디스크에서 복구된 seen: 재시작 전에 처리해 둔 글들만 들어 있다.
    r.seen = {str(i) for i in OLD_IDS}
    r._session_lost_streak = 0
    r._cycle = 0
    r._registered_total = 0
    r._last_amount = None
    r._last_session_save = 1e18
    r._fresh_refuse_streak = 0
    r._no_perm_streak = 0
    r._no_perm_warned = False
    r._relogin_done = False
    r._auth_mismatch_streak = 0
    r._site_down_since = None
    r._site_down_reported = 0.0
    r._site_down_polls = 0
    r._relogin_attempts = 0
    r._max_live_id = 0
    r._stale_list_streak = 0
    r._post_absent_pid = None
    r._post_absent_streak = 0
    r._post_absent_giveup = set()
    r._last_heavy_tick = 0.0
    return r


def run_scenario(missed, seconds=20.0):
    FAKE_NOW[0] = 0.0
    sc = Scenario(missed)
    r = build_registrar(sc)
    expected = set(sc.missed[-int(getattr(config, "RESTART_CATCHUP_MAX", 10)):])

    def fake_wait(s):
        FAKE_NOW[0] += max(s, 1e-9)

    def should_stop():
        if expected and expected <= sc.registered:
            return True
        return FAKE_NOW[0] >= seconds

    r._wait = fake_wait
    r.should_stop = should_stop
    orig = eb.time.time
    eb.time.time = lambda: FAKE_NOW[0]
    try:
        r.run()
    finally:
        eb.time.time = orig
    return sc, expected


def main():
    cap = int(getattr(config, "RESTART_CATCHUP_MAX", 10))

    # --- A) 짧은 재시작: 꺼져 있는 동안 10건 ---------------------------------
    sc, expected = run_scenario(10)
    missed_now = sorted(set(sc.missed) - sc.registered)
    print(f"[A] 재시작 중 올라온 글 {sc.missed[0]}~{sc.missed[-1]} "
          f"-> 등록 {len(sc.registered & set(sc.missed))}건, 미등록 {missed_now}")
    assert not missed_now, (
        f"재시작 중 올라온 글 {missed_now} 이 영원히 건너뛰어졌다 - "
        "run() 이 현재 목록 전체를 seen 으로 흡수하는 옛 동작(2026-08-05 실측 배너 누락 10건).")
    old_writes = [p for _t, p in sc.writes if p <= PREV_MAX]
    assert not old_writes, (
        f"재시작 전에 이미 처리한 옛 글에 rq_addbanner 를 쐈다: {sorted(set(old_writes))} - "
        "옛 글 재-add 방지(흡수)가 깨졌다.")
    print(f"[OK] 놓친 글 10건 전부 등록, 옛 글({OLD_IDS[0]}~{PREV_MAX}) 재-add 0회.")

    # --- B) 긴 정지: 20건 올라옴 -> 상한만큼만 따라잡는다 ---------------------
    sc, expected = run_scenario(20)
    caught = sorted(sc.registered & set(sc.missed))
    print(f"[B] 정지 중 올라온 글 20건 -> 등록 {len(caught)}건 "
          f"({caught[0] if caught else '-'}~{caught[-1] if caught else '-'}), 상한 {cap}")
    assert len(caught) == cap, (
        f"따라잡기 상한이 지켜지지 않았다: {len(caught)}건 등록(RESTART_CATCHUP_MAX={cap}). "
        "오래된 글에 배너 잔여를 낭비할 수 있다.")
    assert set(caught) == set(sc.missed[-cap:]), (
        f"따라잡은 글이 '가장 최근 {cap}건'이 아니다: {caught}")
    print(f"[OK] 상한 {cap}건만, 그것도 가장 최근 {cap}건만 따라잡았다.")

    print("\nALL RESTART-CATCHUP CHECKS PASSED")


if __name__ == "__main__":
    main()
