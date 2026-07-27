# -*- coding: utf-8 -*-
"""2026-07-27 라이브 재현(고객 5136338, post=30834): post_absent GIVEUP 이후에도 그 pid 가
나중에(같은 실행 안에서) 실제 새 글로 뜨면 등록되어야 한다는 것을 실제 Registrar 루프
로직으로 검증한다.

라이브로 발견된 버그(artifacts-check 5136338, unicorn@external-8 KR egress 로 직접 확인):
  05:43:27 v2.5.2 재시작, baseline frontier=30834 (그 시점 실제 최신 글은 30833).
  05:51:38 post=30834 가 연속 500회(POST_ABSENT_GIVEUP_STREAK) post_absent -> GIVEUP.
           이 시점 코드는 self.seen.add(pid) 로 처리했다.
  06:10 경 curl https://ezloan.io/rq/30834 -> 291KB 의 진짜 글 페이지로 확인(제목/배너
        목록 존재, 585 없음, 슬롯 여유 있음). ezloan 목록(/rq)에도 30834 가 새로 등장.
  이후 로그: [cycle] 는 계속 새글=0, frontier=30837 근처를 맴돎 -> 30834 에 대한 register
  시도가 단 한 번도 없음(REGRESSION: self.seen 이 목록 안전망도 걸러내는 바로 그 집합이라,
  GIVEUP 이 영구 등록 거부를 만들어 버렸다).

기대(수정 후): GIVEUP 은 self.seen 이 아니라 별도 _post_absent_giveup 에 들어가 적극적
프런티어-probe 재시도만 멈추고, 목록 안전망(매 사이클 /rq 목록 재확인)은 그 pid 를 계속
감시해 실제로 뜨면 등록에 성공한다.
"""
import config
import ezloan_bot as eb
from repro_post_absent_race import FakeResp, build_registrar

REAL_MAX = 30833
EXISTING = set(range(30700, REAL_MAX + 1))
GIVEUP_ID = REAL_MAX + 1  # 30834: 처음엔 존재하지 않다가, GIVEUP 이후 실제로 생긴다.
REGISTERED = []
REAL_PAGE = ("배너 등록을 눌러 주세요 rq_addbanner js-memberConfirmView " + ("x" * 260000))
EMPTY_PAGE = "y" * 353

# 몇 번째 사이클부터 GIVEUP_ID 가 목록/페이지 상에 실제로 나타나는지(GIVEUP 이후로 설정).
LIVE_AFTER_CYCLE = {"n": None}
_cycle = {"n": 0}


def _is_live(pid):
    if pid in EXISTING:
        return True
    if pid == GIVEUP_ID:
        return LIVE_AFTER_CYCLE["n"] is not None and _cycle["n"] >= LIVE_AFTER_CYCLE["n"]
    return False


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def get(self, url, timeout=None, allow_redirects=True):
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            listed = set(EXISTING) | ({GIVEUP_ID} if _is_live(GIVEUP_ID) else set())
            ids = sorted(listed, reverse=True)[: config.MAX_POSTS]
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in ids)
            return FakeResp(200, text="로그아웃 광고 관리 " + links)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            r = FakeResp(200, text=REAL_PAGE if _is_live(pid) else EMPTY_PAGE)
            r.url = url
            return r
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 182})
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            if pid not in REGISTERED and _is_live(pid):
                REGISTERED.append(pid)
                return FakeResp(200, js={"result": True})
            return FakeResp(200, js={"result": False, "msg": "404 error"})
        return FakeResp(404, text="")


def one_cycle(r, frontier):
    ids = eb.list_post_ids(r.s)
    real_max = max((int(x) for x in ids if str(x).isdigit()), default=0)
    if real_max:
        sane = real_max + 1
        if frontier > sane + config.LOOKAHEAD:
            frontier = sane
    ahead, safe_frontier = eb.lookahead_ids(r.s, frontier, config.LOOKAHEAD)
    if safe_frontier > frontier:
        frontier = safe_frontier
    for pid, precheck in ahead:
        if pid in r.seen or pid in r._post_absent_giveup:
            continue
        exists = r._handle(pid, precheck=precheck)
        if exists and pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
    new = [i for i in ids if i not in r.seen]
    for pid in sorted(new, key=int, reverse=True):
        if pid.isdigit() and int(pid) >= frontier:
            frontier = int(pid) + 1
        r._handle(pid)
    return frontier


def main():
    r = build_registrar()
    r.s = FakeSession()
    r.seen.update(eb.list_post_ids(r.s))
    frontier = REAL_MAX + 1  # 30834

    # 1) GIVEUP_STREAK 만큼 사이클을 돌려 실제로 GIVEUP 이 발동하게 만든다(그동안 30834 는
    #    존재하지 않음 - LIVE_AFTER_CYCLE 미설정).
    giveup_cycle = None
    for i in range(1, config.POST_ABSENT_GIVEUP_STREAK + 5):
        _cycle["n"] = i
        frontier = one_cycle(r, frontier)
        if str(GIVEUP_ID) in r._post_absent_giveup:
            giveup_cycle = i
            break
    assert giveup_cycle is not None, "GIVEUP 이 발동하지 않음(테스트 전제 오류)"
    assert GIVEUP_ID not in REGISTERED, "아직 안 떴는데 등록되면 테스트 전제가 틀렸음"
    print(f"[OK] 사이클#{giveup_cycle}: post={GIVEUP_ID} GIVEUP 발동 "
          f"(seen 에 있음={GIVEUP_ID in r.seen}, giveup 집합에 있음="
          f"{str(GIVEUP_ID) in r._post_absent_giveup})")
    # 수정 검증 포인트 1: GIVEUP 은 self.seen 이 아니라 별도 giveup 집합에 들어가야 한다.
    assert str(GIVEUP_ID) not in r.seen, (
        "REGRESSION: GIVEUP 이 self.seen 에 들어감 - 목록 안전망도 영구히 걸러져 나중에 "
        "이 글이 실제로 떠도 절대 등록되지 않는다(2026-07-27 라이브 버그 재현).")

    # 2) GIVEUP 이후, 이 번호가 실제로 새 글로 뜬다(라이브에서 실제로 일어난 상황).
    LIVE_AFTER_CYCLE["n"] = giveup_cycle + 2
    for i in range(giveup_cycle + 1, giveup_cycle + 10):
        _cycle["n"] = i
        frontier = one_cycle(r, frontier)
        if GIVEUP_ID in REGISTERED:
            break

    print(f"최종: frontier={frontier} 등록됨={REGISTERED}")
    assert GIVEUP_ID in REGISTERED, (
        f"FAIL(라이브 재현된 버그): post={GIVEUP_ID} 가 GIVEUP 이후 실제로 새 글이 됐는데도 "
        f"끝내 등록되지 않음(목록 안전망이 self.seen 에 걸려 이 pid 를 계속 건너뜀). "
        f"seen에있음={str(GIVEUP_ID) in r.seen} "
        f"giveup집합에있음={str(GIVEUP_ID) in r._post_absent_giveup}")
    print("[OK] GIVEUP 이후에도 실제로 뜬 글은 목록 안전망이 잡아 정상 등록됨.")


if __name__ == "__main__":
    main()
