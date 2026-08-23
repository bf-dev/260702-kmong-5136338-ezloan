# -*- coding: utf-8 -*-
"""Korean egress for server-side runs. Fail closed, never fall back to the host IP.

Why this module exists
----------------------
The customer's own copy of this program runs on their PC in Korea, so it never needed a
proxy: `EZLOAN_EGRESS_PROXY` is unset there and every function here is a no-op. When WE run
the same Registrar loop from our own server (which egresses from Japan), two separate things
break if the traffic leaves from the host IP:

  1. ezloan.io answers 403 to non-Korean IPs, so nothing works at all (loud, harmless).
  2. A Naver login attempted from a non-Korean IP trips Naver's account protection and
     LOCKS the customer's real Naver account (silent, and expensive for the customer).

Because of (2), "no proxy" and "proxy is down" must both mean STOP, never "go direct".
requests only falls back to a direct connection if `proxies` is empty or `trust_env` picks
up an env var, so `apply()` sets both explicitly on every session the bot builds, and
`require()` refuses to let a run start unless the route is proven to be Korean AND proven
to be different from this host's own address.

The proxy URL must be credential-free (e.g. `socks5h://127.0.0.1:1085`, a dedicated
`ssh -N -D` tunnel to a Korean node). Chrome cannot authenticate to a SOCKS5 proxy, so a
user:pass URL would silently work for `requests` and silently NOT work for the Selenium
login, which is exactly the fail-open we cannot have. `chrome_proxy_arg()` raises instead.
"""

import threading
import time

import requests


class EgressError(RuntimeError):
    """Raised when the Korean route is missing, wrong, or unverifiable."""


_lock = threading.RLock()
_proxy = None
_direct = False
_expect_country = "KR"
_expect_ip = None
_observed = {"ip": None, "country": None, "at": 0.0}

# Order matters: ipinfo first (stable, gives ip+country in one call), ip-api as backstop.
_GEO_ENDPOINTS = (
    ("https://ipinfo.io/json", "ip", "country"),
    ("http://ip-api.com/json", "query", "countryCode"),
)


def configure(proxy_url, expect_country="KR", expect_ip=None):
    """Install the egress. Everything else in this module is a no-op until this is called."""
    global _proxy, _direct, _expect_country, _expect_ip
    with _lock:
        _proxy = (proxy_url or "").strip() or None
        _direct = False
        _expect_country = (expect_country or "").strip().upper() or None
        _expect_ip = (expect_ip or "").strip() or None
        _observed.update({"ip": None, "country": None, "at": 0.0})
    return _proxy


def configure_direct(expect_ip, expect_country="KR"):
    """The egress IS this host's own address, because this host is already in Korea.

    Only legal when the loop runs ON the Korean node (external-1), which is the whole
    point of moving it there: no tunnel means no 40ms round trip on the hot path.

    The fail-closed guarantee is preserved, it just moves: instead of "the proxy must be
    Korean and must not be this host", the rule becomes "this host's own outbound address
    must BE the pinned Korean address". `expect_ip` is therefore REQUIRED here. Without a
    pin there is nothing to fail closed against, and an unpinned direct mode on the Tokyo
    gateway would be exactly the silent non-KR egress this module exists to prevent.
    """
    global _proxy, _direct, _expect_country, _expect_ip
    pin = (expect_ip or "").strip()
    if not pin:
        raise EgressError(
            "direct egress mode requires a pinned expect_ip. Refusing to run without one: "
            "an unpinned 'direct' egress is indistinguishable from egressing off this host, "
            "which 403s on ezloan.io and protection-locks the customer's Naver account."
        )
    with _lock:
        _proxy = None
        _direct = True
        _expect_country = (expect_country or "").strip().upper() or None
        _expect_ip = pin
        _observed.update({"ip": None, "country": None, "at": 0.0})
    return pin


def is_direct():
    return _direct


def is_configured():
    return bool(_proxy) or _direct


def proxy_url():
    return _proxy


def proxies():
    return {"http": _proxy, "https": _proxy} if _proxy else None


def apply(session):
    """Pin a requests.Session to the egress. No-op when nothing is configured.

    `trust_env = False` is not cosmetic: without it a stray HTTP_PROXY/NO_PROXY in the
    environment can re-route or un-route the session behind our back.
    """
    if _direct:
        # Not a no-op: pinning proxies to {} with trust_env off is what stops a stray
        # HTTP_PROXY/HTTPS_PROXY in the environment from silently routing the customer's
        # traffic somewhere that is not this verified Korean host.
        session.proxies = {}
        session.trust_env = False
        return session
    if not _proxy:
        return session
    session.proxies = {"http": _proxy, "https": _proxy}
    session.trust_env = False
    return session


def chrome_proxy_arg():
    """`--proxy-server=` value for Selenium, or None when no egress is configured."""
    if _direct:
        return None
    if not _proxy:
        return None
    if "@" in _proxy:
        raise EgressError(
            "the egress proxy URL carries credentials; Chrome cannot authenticate to a "
            "SOCKS5 proxy, so the Naver login would leave from this host's own IP. "
            "Use a credential-free local tunnel (ssh -N -D 127.0.0.1:PORT <kr-host>)."
        )
    # Chrome resolves DNS through the proxy for socks5://, which is what socks5h:// means
    # to requests. Normalise so both clients take the same route.
    return "--proxy-server=" + _proxy.replace("socks5h://", "socks5://")


def _lookup(session, timeout):
    last = None
    for url, ip_key, country_key in _GEO_ENDPOINTS:
        try:
            r = session.get(url, timeout=timeout, headers={"Cache-Control": "no-cache"})
            data = r.json()
            ip = str(data.get(ip_key) or "").strip()
            country = str(data.get(country_key) or "").strip().upper()
            if ip:
                return ip, (country or None)
        except Exception as e:  # try the next endpoint
            last = e
    if last:
        raise EgressError(f"could not read the egress address: {last}")
    raise EgressError("could not read the egress address")


def observe(timeout=15, max_age=0.0):
    """Return (ip, country) as seen THROUGH the egress. Raises EgressError if unusable."""
    if not _proxy and not _direct:
        raise EgressError("no egress proxy configured")
    with _lock:
        if max_age and _observed["ip"] and (time.time() - _observed["at"]) < max_age:
            return _observed["ip"], _observed["country"]
    s = requests.Session()
    apply(s)
    try:
        ip, country = _lookup(s, timeout)
    finally:
        try:
            s.close()
        except Exception:
            pass
    with _lock:
        _observed.update({"ip": ip, "country": country, "at": time.time()})
    return ip, country


def direct_ip(timeout=10):
    """This host's own egress address, WITHOUT the proxy. Used to prove the proxy is real."""
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    try:
        return s.get("https://api.ipify.org", timeout=timeout).text.strip()
    finally:
        try:
            s.close()
        except Exception:
            pass


def require(timeout=20, check_url=None):
    """Preflight. Returns (ip, country) or raises EgressError. Every branch fails closed.

    Checks, in order:
      1. an egress is configured at all
      2. it is credential-free (so Chrome takes the same route as requests)
      3. it answers, and reports an address
      4. that address is in the expected country (and equals the pinned IP when one is set)
      5. that address is NOT this host's own address
      6. the target site actually answers through it (optional but on by default)
    """
    if not _proxy and not _direct:
        raise EgressError(
            "no Korean egress configured. This run is refused rather than falling back to "
            "this host's own IP: ezloan.io 403s a non-KR address and a Naver login from one "
            "protection-locks the customer's account."
        )
    if _direct and not _expect_ip:
        # configure_direct() already refuses this, so reaching it means someone poked the
        # module state by hand. Refuse again rather than trust it.
        raise EgressError(
            "direct egress mode is active with no pinned address. Refusing to start."
        )
    chrome_proxy_arg()  # raises if the URL carries credentials

    ip, country = observe(timeout=timeout)
    if _expect_ip and ip != _expect_ip:
        raise EgressError(
            f"egress address {ip} does not match the pinned EZLOAN_EGRESS_EXPECT_IP "
            f"{_expect_ip}. Refusing to start."
        )
    if _expect_country and country and country != _expect_country:
        raise EgressError(
            f"egress address {ip} is in {country}, not {_expect_country}. Refusing to start."
        )
    if _expect_country and not country and not _expect_ip:
        raise EgressError(
            f"could not confirm the country of egress address {ip} and no "
            f"EZLOAN_EGRESS_EXPECT_IP is pinned. Refusing to start."
        )
    if not _direct:
        # Proxy mode: the egress must NOT be this host, or the tunnel is not really in
        # the path. In direct mode the two are the same address by definition, and the
        # pin check above is what proves that address is the Korean one.
        try:
            own = direct_ip(timeout=10)
        except Exception:
            own = None
        if own and own == ip:
            raise EgressError(
                f"the proxy egress ({ip}) is identical to this host's own address; the "
                f"traffic is not actually going through the Korean route. Refusing to start."
            )
    if check_url:
        s = requests.Session()
        apply(s)
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
        try:
            r = s.get(check_url, timeout=timeout)
            if r.status_code != 200:
                raise EgressError(
                    f"{check_url} answered HTTP {r.status_code} through the egress "
                    f"(expected 200). Refusing to start.")
        finally:
            try:
                s.close()
            except Exception:
                pass
    return ip, country


def start_guard(on_violation, interval=60.0, timeout=15, stop_event=None):
    """Re-verify the egress forever in the background; call on_violation(reason) if it moves.

    A tunnel can die or be re-pointed long after the preflight passed. The loop below is the
    only thing standing between that and the whole registration loop quietly egressing from
    Japan, so a violation must stop the run, not just log.
    """
    def _loop():
        while True:
            if stop_event is None:
                time.sleep(interval)
            elif stop_event.wait(interval):
                return
            try:
                ip, country = observe(timeout=timeout)
            except Exception as e:
                on_violation(f"egress unverifiable: {e}")
                return
            if _expect_ip and ip != _expect_ip:
                on_violation(f"egress moved to {ip}, pinned {_expect_ip}")
                return
            if _expect_country and country and country != _expect_country:
                on_violation(f"egress moved to {ip} ({country}), expected {_expect_country}")
                return

    t = threading.Thread(target=_loop, name="ezloan-egress-guard", daemon=True)
    t.start()
    return t
