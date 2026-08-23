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


# 실제 이지론 마크업 그대로. 유료 등급(ad_sm/ad_lg) 광고주는 name div 안에 배지 <span> 이
# 하나 더 붙는다 - 2026-08-23 까지 쓰던 ([^<]*)</div> 정규식은 바로 이 항목들을 통째로
# 놓쳤고, 그래서 옥자대부(544)가 1슬롯인 글에서도 rank=1 이 찍혔다. 픽스처에 배지가 붙은
# 항목을 반드시 하나 넣어 그 회귀를 다시 잡는다.
def banner_page(items):
    """items = [(advertiser_id, 상호, ad_class_or_None)] in exposure order."""
    lis = []
    for aid, name, cls in items:
        badge = ' <span class="m_hide">정식등록 8개월</span>' if cls else ""
        lis.append(
            f'<li><a href="/l/{aid}" class="item {cls or ""}" title="{name}-홍보문구 01000000000">'
            f'<div class="name">{name}{badge}</div></a></li>')
    return ('<ul class="section_body loan_list recommend">' + "".join(lis) + "</ul>")


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
            # 배너 3개, 우리가 slot1. 경쟁A 는 배지 달린 유료 등급(ad_sm)이다.
            return Resp(text=banner_page([(585, COMPANY, None),
                                          (408, "경쟁A", "ad_sm"),
                                          (530, "경쟁B", None)]), ct="text/html")
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
# 배지 달린 유료 광고주(ad_sm)가 우리 위에 있는 페이지: 반드시 2가 나와야 한다.
# 예전 정규식은 이 항목을 못 읽어 1을 돌려줬다 - 그게 이 고객 진단을 한 달간 틀리게 만든 버그다.
class Sess2(Sess):
    def get(self, url, **kw):
        if url.endswith("/rq/100"):
            return Resp(text=banner_page([(544, "옥자대부", "ad_sm"),
                                          (585, COMPANY, None)]), ct="text/html")
        return super().get(url, **kw)
assert B.company_rank(Sess2(), "100") == 2, "slot2 case must return 2"
order = B.banner_order(banner_page([(544, "옥자대부", "ad_sm"), (585, COMPANY, None)]))
assert [a for a, _n, _c in order] == ["544", "585"], order
res2 = B.register(Sess2(), "100", COMPANY)
assert res2["rank"] == 2 and res2["above"] == ["옥자대부(544)"], res2
print("[OK] company_rank: 배지(ad_sm) 붙은 경쟁사도 세어 실제 순위 + 위에 누가 있는지 보고")

# --- 4) 유령 미래 번호: 프런티어 폭주 방지 ---
s3 = Sess()
found, safe = B.lookahead_ids(s3, 101, 6)
assert found == [("101", (200, {"result": True, "amount": 50}))], found
assert safe == 101, f"safe_frontier must not pass phantom: {safe}"
res3 = B.register(s3, "101", precheck=found[0][1])
assert res3["note"] == "post_absent", res3
print("[OK] 유령 번호: safe_frontier 미전진 + register -> post_absent(폭주 방지 유지)")

print("\nALL CHECKS PASSED: fast register hot-path + true-rank logging, correctness preserved.")
