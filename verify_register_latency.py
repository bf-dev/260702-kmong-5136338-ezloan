# -*- coding: utf-8 -*-
"""v2.4.6 검증: 새 글 상단(1등) 경쟁을 위한 등록 핫패스 속도 회귀 방지.

증명 항목:
  1) open 글 등록 시 무거운 post_exists(/rq/{id} 전체 페이지)를 '먼저' 부르지 않는다.
  2) register() 는 lookahead 의 precheck(rq_addbanner_check 결과)를 재사용해 같은 check 를
     두 번 치지 않는다(핫패스 왕복 절감).
  3) company_rank() 는 페이지 전체 <li> 가 아니라 '진짜 배너 항목'만 세어 실제 상단 순위를
     돌려준다(로그의 rank 가 곧 사이트에서 보는 순위).
  4) 유령(미래) 번호는 open 후보로 잡혀도 프런티어를 넘기지 않고 register -> post_absent.
"""
import re
import config
import ezloan_bot as B

COMPANY = config.COMPANY_NAME


class Resp:
    def __init__(self, status=200, js=None, text="", ct="application/json"):
        self.status_code = status
        self._js = js
        self.text = text
        self.headers = {"content-type": ct if js is not None else "text/html"}

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class Sess:
    def __init__(self):
        self.calls = []
        self.cookies = []
        self.headers = {}

    def get(self, url, **kw):
        self.calls.append(url)
        if "rq_addbanner_check/100" in url:
            return Resp(js={"result": True, "amount": 50})
        if "rq_addbanner/100" in url:
            return Resp(js={"result": True, "msg": "success"})
        if url.endswith("/rq/100"):
            # 배너 3개, 우리가 slot1
            html = "".join(
                f'<a href="/l/{aid}" class="item"><div class="name">{name}</div></a>'
                for aid, name in [(585, COMPANY), (408, "경쟁A"), (530, "경쟁B")])
            return Resp(text="<ul>" + html + "</ul>", ct="text/html")
        if "rq_addbanner_check/101" in url:
            return Resp(js={"result": True, "amount": 50})  # 유령: check 통과
        if "rq_addbanner/101" in url:
            return Resp(js={"result": False, "msg": "404 error"})
        if url.endswith("/rq/101"):
            return Resp(text="tiny", ct="text/html")  # <1000B -> post_exists False
        return Resp(status=404)


B._sync_csrf_header = lambda s: None

# --- 1) + 2): open 글 핫패스 ---
s = Sess()
st, precheck = B.probe_state(s, "100")
assert st == "open", st
probe_calls = list(s.calls)
assert not any(u.endswith("/rq/100") for u in probe_calls), \
    f"post_exists(/rq/100) must NOT run before register: {probe_calls}"
res = B.register(s, "100", precheck=precheck)
assert res["ok"] and res["rank"] == 1, res
checks = [u for u in s.calls if "rq_addbanner_check/100" in u]
assert len(checks) == 1, f"redundant rq_addbanner_check: {checks}"
adds = [u for u in s.calls if "rq_addbanner/100" in u]
assert len(adds) == 1, adds
print("[OK] open 글: probe 에서 post_exists 미호출 + check 1회 재사용 + add 1회(핫패스 최소 왕복)")

# --- 3) company_rank 는 진짜 배너 항목만 센다 ---
r1 = B.company_rank(s, "100")
assert r1 == 1, f"company_rank should be true slot 1, got {r1}"
# 우리를 slot2 로 둔 페이지에서 2가 나와야 한다
class Sess2(Sess):
    def get(self, url, **kw):
        if url.endswith("/rq/100"):
            html = "".join(
                f'<a href="/l/{aid}" class="item"><div class="name">{name}</div></a>'
                for aid, name in [(408, "경쟁A"), (585, COMPANY)])
            return Resp(text="<ul>" + html + "</ul>", ct="text/html")
        return super().get(url, **kw)
assert B.company_rank(Sess2(), "100") == 2, "slot2 case must return 2"
print("[OK] company_rank: 진짜 배너 순위(slot1->1, slot2->2), 페이지 <li> 잡음 없음")

# --- 4) 유령 미래 번호: 프런티어 폭주 방지 ---
s3 = Sess()
found, safe = B.lookahead_ids(s3, 101, 6)
assert found == [("101", (200, {"result": True, "amount": 50}))], found
assert safe == 101, f"safe_frontier must not pass phantom: {safe}"
res3 = B.register(s3, "101", precheck=found[0][1])
assert res3["note"] == "post_absent", res3
print("[OK] 유령 번호: safe_frontier 미전진 + register -> post_absent(폭주 방지 유지)")

print("\nALL CHECKS PASSED: fast register hot-path + true-rank logging, correctness preserved.")
