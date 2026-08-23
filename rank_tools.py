"""Read-only rank measurement helpers for the ezloan 1등 work (customer 5136338).

Nothing in here logs in, registers, or writes anything to ezloan. Every request is an
anonymous GET of a public /rq page through the Korean egress, exactly like the bot's
existence probe. Safe to run alongside a live registration run.

Why this file exists: `ezloan_bot.company_rank()` silently under-counts the banner list.
Its regex is

    <a href="/l/\\d+" class="item[^"]*"[^>]*>\\s*<div class="name">([^<]*)</div>

and the real markup for a paid "ad_sm" advertiser is

    <div class="name">옥자대부 <span class="m_hide">정식등록 8개월</span></div>

so `([^<]*)</div>` cannot match it and every ad_sm advertiser is dropped from the list.
On post 32004 that is 4 of 9 banners, including 옥자대부 (advertiser 544), the exact
competitor the customer keeps naming. `banner_list()` below parses the real DOM order.
"""

from __future__ import annotations

import re
import time
from http.cookiejar import DefaultCookiePolicy

import requests

BASE = "https://ezloan.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# 실제 배너 목록은 <ul class="section_body loan_list recommend"> 안의 <a href="/l/{id}"
# class="item ..."> 항목들이고, 이 DOM 순서가 곧 노출 순서다. 상호는 title 속성의
# "{상호}-{문구}" 앞부분이 가장 안전하다(name div 안에는 <span> 배지가 섞여 들어온다).
_ITEM_RE = re.compile(
    r'<a href="/l/(\d+)" class="(item[^"]*)"[^>]*title="([^"]*)"', re.S)
_LIST_RE = re.compile(
    r'<ul class="section_body loan_list recommend">(.*?)</ul>', re.S)

POST_PAGE_MIN_BYTES = 20000
POST_PAGE_MARKERS = ("loan_list recommend", "rq_addbanner")


def probe_session(proxy: str = "socks5h://127.0.0.1:1085") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": proxy, "https": proxy}
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    s.headers.update({
        "User-Agent": UA,
        "Referer": f"{BASE}/rq",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def banner_list(html: str):
    """[(advertiser_id, name, css_class), ...] in true DOM/exposure order."""
    m = _LIST_RE.search(html)
    body = m.group(1) if m else html
    out = []
    for aid, cls, title in _ITEM_RE.findall(body):
        name = title.split("-", 1)[0].strip()
        out.append((aid, name, cls.strip()))
    return out


def page_exists(html: str, status: int) -> bool:
    if status != 200 or len(html) < POST_PAGE_MIN_BYTES:
        return False
    return any(mk in html for mk in POST_PAGE_MARKERS)


def fetch_post(s: requests.Session, pid: int, timeout: float = 8.0):
    """-> (exists, banner_list, elapsed_seconds, status)"""
    t0 = time.monotonic()
    try:
        r = s.get(f"{BASE}/rq/{pid}", timeout=timeout, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return False, [], time.monotonic() - t0, f"err:{type(e).__name__}"
    dt = time.monotonic() - t0
    html = r.text or ""
    if not page_exists(html, r.status_code):
        return False, [], dt, r.status_code
    return True, banner_list(html), dt, r.status_code
