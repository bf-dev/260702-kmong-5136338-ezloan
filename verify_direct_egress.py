#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prove the DIRECT egress mode fails closed. Read-only: never logs in, never registers.

The whole risk of moving the loop onto external-1 is that "direct" could quietly become
"whatever address this box happens to have". These are the checks that make that
impossible, and they are written so the NEGATIVE cases are the important ones:

  1. configure_direct with no pin  -> refused (there is nothing to fail closed against)
  2. pinned to an address this host does NOT have -> require() refuses
  3. the background guard notices the pin no longer matches and calls on_violation
  4. every session apply() touches has proxies={} and trust_env=False, so a stray
     HTTP_PROXY in the environment cannot re-route the customer's traffic
  5. chrome_proxy_arg() is None in direct mode (Chrome would go direct, which is only
     correct ON the Korean node; the gateway must never reach this state)

Run it anywhere. On the gateway (Tokyo) checks 1-5 all exercise the refusal paths; on
external-1 add --expect-ip 13.124.160.237 to also prove the positive path.

    python3 verify_direct_egress.py
    python3 verify_direct_egress.py --expect-ip 13.124.160.237     # on external-1
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import egress  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"[OK] {label}" + (f"  {detail}" if detail else ""))
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILED.append(label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-ip", default="",
                    help="this host's real outbound address, to also test the pass path")
    args = ap.parse_args()

    own = egress.direct_ip()
    print(f"this host's own outbound address: {own}\n")

    # 1. no pin -> refused outright
    try:
        egress.configure_direct("")
        check("direct mode refuses to configure without a pinned IP", False,
              "it accepted an empty pin")
    except egress.EgressError as e:
        check("direct mode refuses to configure without a pinned IP", True, str(e)[:70])

    # 2. pinned to an address this host does not have -> require() refuses
    wrong = "203.0.113.7" if own != "203.0.113.7" else "198.51.100.7"
    egress.configure_direct(wrong, expect_country="KR")
    try:
        egress.require(timeout=20)
        check("require() refuses when this host is not the pinned address", False,
              f"it accepted {own} against pin {wrong}")
    except egress.EgressError as e:
        check("require() refuses when this host is not the pinned address", True,
              str(e)[:80])

    # 3. the background guard fires on a pin that no longer matches
    seen = {"reason": None}
    egress.start_guard(lambda r: seen.update(reason=r), interval=1.0, timeout=15)
    deadline = time.time() + 45
    while time.time() < deadline and not seen["reason"]:
        time.sleep(0.5)
    check("the background guard reports a violation when the pin stops matching",
          bool(seen["reason"]), (seen["reason"] or "guard stayed silent")[:80])

    # 4. apply() hard-pins the session against a stray proxy in the environment
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    try:
        egress.configure_direct(own or "203.0.113.7", expect_country="KR")
        s = egress.apply(requests.Session())
        check("apply() sets proxies={} and trust_env=False in direct mode",
              s.proxies == {} and s.trust_env is False,
              f"proxies={s.proxies} trust_env={s.trust_env}")
        # and prove it actually ignores the env: the stray proxy points at a dead port,
        # so if trust_env leaked we would get a connection error instead of an address.
        try:
            got = s.get("https://api.ipify.org", timeout=15).text.strip()
        except Exception as e:
            got = f"<{type(e).__name__}>"
        check("a stray HTTP_PROXY cannot re-route a pinned session", got == own,
              f"got {got}, own {own}")
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)

    # 5. Chrome takes no proxy in direct mode
    check("chrome_proxy_arg() is None in direct mode",
          egress.chrome_proxy_arg() is None, repr(egress.chrome_proxy_arg()))

    # 6. positive path, only meaningful when run ON the pinned host
    if args.expect_ip:
        egress.configure_direct(args.expect_ip, expect_country="KR")
        try:
            ip, country = egress.require(timeout=20, check_url="https://ezloan.io/rq")
            check("require() passes on the pinned Korean host, and ezloan.io answers 200",
                  ip == args.expect_ip and country == "KR", f"{ip} ({country})")
        except egress.EgressError as e:
            check("require() passes on the pinned Korean host", False, str(e)[:120])

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("ALL CHECKS PASSED: direct egress mode cannot silently become a non-KR egress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
