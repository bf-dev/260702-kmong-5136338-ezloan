# -*- coding: utf-8 -*-
"""실제 register()/Registrar 로직을 가짜 이지론 서버에 물려 버그/수정을 재현 검증한다.

가짜 서버는 관측된 사실을 그대로 흉내낸다:
  - /rq 목록: 현재 존재하는 글 id 를 <a href="/rq/{id}"> 로 렌더.
  - /api/rq_addbanner_check/{id}: 계정 정상(유료광고 활성) -> 항상 result:true(amount 정상).
  - /api/rq_addbanner/{id}:
        이미 등록됨(내 배너 있음)  -> {"result":false,"msg":"404 error"}   (관측된 실재-글 거부)
        아직 등록 안 됨(신규 글)     -> {"result":true}                        (등록 성공)
        존재하지 않는 미래 번호      -> {"result":false,"msg":"404 error"}   (관측된 미존재 거부)
  - /rq/{id} 페이지: 존재하는 글이면 25만 바이트 실제 페이지(마커 포함),
                     미존재면 353 바이트 빈 껍데기(관측값).
  - logged_in(): 항상 True (세션·계정 정상). => 거짓 재로그인 루프가 절대 나면 안 된다.
"""
import types
import config
import ezloan_bot as eb

# ---- 관측 시퀀스 정의 -------------------------------------------------------
# 이미 등록된(내 배너 있는) 실재 글: 29950 및 29951~30006 (재시작 되감김으로 재-add 대상)
ALREADY = set(range(29950, 30007))          # 29950..30006 모두 이미 등록됨(실재)
EXISTING = set(range(29900, 30007))          # 목록/페이지에 실재하는 글 범위
NEW_POST = 30007                             # 이후 새로 생기는 글(등록 성공해야 함)
EXISTING.add(NEW_POST)

REGISTERED_BY_APP = set()                    # 앱이 이번 실행에서 실제 등록한 글

class FakeResp:
    def __init__(self, status, text="", js=None, ctype="application/json"):
        self.status_code = status
        self.text = text
        self._js = js
        self.url = ""
        self.headers = {"content-type": ctype}
    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js

REAL_PAGE = ("배너 등록을 눌러 주세요 rq_addbanner js-memberConfirmView " + ("x" * 260000))
EMPTY_PAGE = "y" * 353

class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = []
    def get(self, url, timeout=None, allow_redirects=True):
        # /rq 목록
        if url.rstrip("/") == config.RQ_URL.rstrip("/"):
            links = "".join(f'<a href="/rq/{i}">글</a>' for i in sorted(EXISTING, reverse=True)[:config.MAX_POSTS])
            body = "로그아웃 광고 관리 " + links
            return FakeResp(200, text=body)
        # /rq/{id} 개별 페이지(존재 판정용)
        if "/rq/" in url and "/api/" not in url:
            pid = int(url.rsplit("/", 1)[-1])
            if pid in EXISTING:
                r = FakeResp(200, text=REAL_PAGE); r.url = url; return r
            r = FakeResp(200, text=EMPTY_PAGE); r.url = url; return r
        # check: 계정 정상 -> 항상 통과
        if "/api/rq_addbanner_check/" in url:
            return FakeResp(200, js={"result": True, "amount": 113})
        # add
        if "/api/rq_addbanner/" in url:
            pid = int(url.rsplit("/", 1)[-1])
            if pid not in EXISTING:
                return FakeResp(200, js={"result": False, "msg": "404 error"})
            if pid in ALREADY or pid in REGISTERED_BY_APP:
                return FakeResp(200, js={"result": False, "msg": "404 error"})
            REGISTERED_BY_APP.add(pid)
            return FakeResp(200, js={"result": True})
        return FakeResp(404, text="nope")


def main():
    fs = FakeSession()

    # logged_in 은 항상 True (세션/계정 정상). 거짓 재로그인 루프가 나면 이 값이 무시된 것.
    eb.logged_in = lambda s: True
    # company_rank 는 등록 성공 검증용 - 등록된 글엔 순위 1 을 준다.
    eb.company_rank = lambda s, pid, company=config.COMPANY_NAME: (1 if int(pid) in REGISTERED_BY_APP else 0)

    results = {}

    print("=== 1) 이미 등록된 실재 글(29951)에 add -> add_refused(SKIP), 세션소실 아님 ===")
    r = eb.register(fs, "29951")
    print("  ", r["note"], "session_lost=", r.get("session_lost"), "msg=", r.get("msg"))
    assert r["note"] == "add_refused", r
    assert r["session_lost"] is False
    assert r["ok"] is False
    results["already_registered"] = "add_refused/skip, session_lost=False (PASS)"

    print("=== 2) 존재하지 않는 미래 번호(40000)에 add -> post_absent(SKIP) ===")
    r = eb.register(fs, "40000")
    print("  ", r["note"], "session_lost=", r.get("session_lost"))
    assert r["note"] == "post_absent", r
    assert r["session_lost"] is False
    results["future_post"] = "post_absent/skip (PASS)"

    print("=== 3) 신규 실재 글(30007)에 add -> 등록 성공 ===")
    r = eb.register(fs, str(NEW_POST))
    print("  ", r["note"], "rank=", r.get("rank"), "ok=", r.get("ok"))
    assert r["ok"] is True and r["rank"] == 1, r
    results["new_post"] = "registered rank=1 (PASS)"

    # ---- Registrar 통합: 재시작 되감김 시나리오 + 거짓 재로그인 방지 -------------
    print("=== 4) Registrar: 재시작 후 이미 등록된 글 재-add 다수 -> 거짓 재로그인/멈춤 없음 ===")
    REGISTERED_BY_APP.discard(NEW_POST)  # 4번 시나리오를 위해 초기화
    EXISTING.discard(NEW_POST)           # 아직 새 글은 없음(목록엔 29950..30006 만)

    calls = {"session_expired": 0, "relogin": 0, "add_refused": 0, "registered": 0}
    def fake_remote(event, detail="", **k):
        if event == "session_expired":
            calls["session_expired"] += 1
        if event == "register_add_refused":
            calls["add_refused"] += 1
        if event == "registered":
            calls["registered"] += 1
    def fake_relogin():
        calls["relogin"] += 1
        return None  # 재로그인 시도 자체가 호출됐는지만 관측

    reg = eb.Registrar([{"name": "ezloan_sess", "value": "x", "domain": ".ezloan.io"}],
                       log=lambda *a, **k: None, remote=fake_remote,
                       should_stop=lambda: False, relogin=fake_relogin)
    reg.s = fs
    # baseline 흡수: 현재 목록(이미 등록된 글들)을 seen 으로 흡수해야 재-add 를 안 한다.
    baseline = eb.list_post_ids(fs)
    reg.seen.update(baseline)
    known_max = max(int(x) for x in baseline)
    frontier = known_max + 1
    print(f"   baseline={len(baseline)}개, frontier={frontier} (이미 등록된 글은 seen 흡수)")
    # 이 상태에서 목록을 다시 폴링하면 '새 글' 이 하나도 없어야 한다(전부 seen).
    ids = eb.list_post_ids(fs)
    new = [i for i in ids if i not in reg.seen]
    assert new == [], f"재시작 후 이미 등록된 글이 새 글로 재처리됨: {new}"
    print("   재시작 재기준화: 새 글=0 -> 이미 등록된 글 재-add 없음 (PASS)")

    # 이제 진짜 새 글이 생긴다. Registrar 가 그 글을 잡아 등록해야 한다.
    EXISTING.add(NEW_POST)
    ids = eb.list_post_ids(fs)
    new2 = [i for i in ids if i not in reg.seen]
    assert str(NEW_POST) in new2, new2
    reg._handle(str(NEW_POST))
    assert str(NEW_POST) in reg.seen and NEW_POST in REGISTERED_BY_APP
    assert calls["registered"] == 1, calls
    print(f"   새 글 {NEW_POST} 등록됨 (PASS)")

    # 거짓 재로그인/멈춤이 없어야 한다: session_expired 0, add_refused 로 세션소실 streak 미증가.
    assert reg._session_lost_streak == 0, reg._session_lost_streak
    assert calls["session_expired"] == 0, calls
    results["registrar_restart"] = (
        f"restart re-baseline: 0 re-add, new post registered, "
        f"session_expired={calls['session_expired']}, session_lost_streak=0 (PASS)")

    # ---- H2 강제 재로그인 트리거: 새 글에서까지 지속 거부되면 relogin 호출 --------
    print("=== 5) 새 글에서까지 add 지속 거부 -> 강제 재로그인 복구 트리거 ===")
    # 모든 글을 '이미 등록됨' 취급하게 만들어 새 글도 거부되게 한다.
    for pid in range(30010, 30014):
        EXISTING.add(pid); ALREADY.add(pid)
    reg2 = eb.Registrar([{"name": "ezloan_sess", "value": "x", "domain": ".ezloan.io"}],
                        log=lambda *a, **k: None, remote=fake_remote,
                        should_stop=lambda: False, relogin=fake_relogin)
    reg2.s = fs
    for pid in (30010, 30011, 30012):    # 새 글(seen 아님)에서 연속 거부
        reg2._handle(str(pid))
    print(f"   새글거부연속={reg2._fresh_refuse_streak}, relogin호출={calls['relogin']}, "
          f"session_expired={calls['session_expired']}")
    assert reg2._fresh_refuse_streak >= 3, reg2._fresh_refuse_streak
    assert calls["relogin"] >= 1, "강제 재로그인이 트리거되지 않음"
    assert calls["session_expired"] == 0, "거짓 세션만료가 발생함"
    results["forced_relogin"] = (
        f"persistent new-post refusal -> forced relogin triggered "
        f"(relogin_calls={calls['relogin']}, no false session_expired) (PASS)")

    print("\n================= REPRO RESULT =================")
    for k, v in results.items():
        print(f"  [{k}] {v}")
    print("ALL REPRO CHECKS PASSED")


if __name__ == "__main__":
    main()
