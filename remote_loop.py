#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The Registrar loop, running ON the Korean node instead of tunnelling to it.

Customer 5136338 (더원대부). This is the half of `server_run.py` that has to be fast.

Why this file exists
--------------------
The gateway host is in Tokyo. Every ezloan request from there crosses a SOCKS tunnel to
Seoul and back, ~40ms each way, and the hot path (probe the post page, then write the
banner) spends two of those round trips. Measured 2026-08-23:

    leg                             main -> tunnel -> external-1     external-1 direct
    /rq/{future} miss, 215 B                  90.0 ms                     46 ms
    /api/rq_addbanner_check                   83.2 ms                     44 ms

So the single largest win available is not a smarter algorithm, it is running the loop in
the country. That is all this file does: the identical `ezloan_bot.Registrar`, driven from
external-1, with no proxy in the path at all.

What stays on the gateway (`server_run.py`)
-------------------------------------------
The Naver login. external-1 is a 417MB AWS nano with no room for headless Chrome (and no
sudo for us, so no swapfile to buy room with). So `server_run.py` still owns the browser,
still owns the credentials, and hands this process nothing but the resulting cookies. This
process never sees a password and never opens a browser.

The egress guarantee, moved not weakened
----------------------------------------
On the gateway the rule was "the proxy must be Korean AND must not be this host". Here
there is no proxy, so the rule becomes "this host's own outbound address must BE the
pinned Korean address", enforced by `egress.configure_direct()` which REFUSES to run
without a pin. Same three properties as before:

  * it is checked before a single ezloan request is made (`egress.require`),
  * it is re-checked forever in the background (`egress.start_guard`), and a violation
    stops the loop rather than letting it egress from somewhere else,
  * every session the bot builds is pinned with `proxies={} / trust_env=False`, so a
    stray HTTP_PROXY in the environment cannot silently re-route the customer's traffic.

The stdin watchdog is a safety property, not plumbing
-----------------------------------------------------
The ezloan account is single-session. If the ssh that launched this process dies but this
process keeps running, we get a second loop racing the first and both degrade. So the
parent writes a keepalive line every few seconds and this process exits if it stops
hearing them. An orphan cannot outlive its parent by more than KEEPALIVE_TIMEOUT.

Protocol (stdout, line based, consumed by server_run.py)
--------------------------------------------------------
    @@LOG  <text>          a log line, teed into run.log on the gateway
    @@SEEN <json>          the seen-post set changed, mirror it down to the gateway
    @@EXIT <code> <reason> final line before exit

Exit codes: 0 stop requested, 3 crash, 4 session dead (parent should re-login and
relaunch), 5 egress violation, 6 keepalive lost.
"""

import fcntl
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# How long an orphan may outlive the gateway that launched it. Deliberately short: the
# gateway waits this out before it dares start a replacement loop anywhere, so it is also
# the worst-case gap in coverage after an ssh dies badly.
KEEPALIVE_TIMEOUT = 45.0
SEEN_WATCH_SECONDS = 5.0

EXIT_OK = 0
EXIT_CRASH = 3
EXIT_SESSION_DEAD = 4
EXIT_EGRESS = 5
EXIT_KEEPALIVE = 6
EXIT_ALREADY_RUNNING = 7


_out_lock = threading.Lock()


def emit(kind, text):
    """One line to the parent. Never raise: a broken pipe must not kill the loop."""
    try:
        with _out_lock:
            sys.stdout.write(f"@@{kind} {text}\n")
            sys.stdout.flush()
    except Exception:
        pass


def log(text):
    emit("LOG", str(text).replace("\n", " ⏎ ")[:2000])


def acquire_single_instance_lock():
    """At most one Registrar per host, enforced by the OS rather than by good intentions.

    The ezloan account is single-session. The gateway already refuses to launch a second
    loop, but an ssh that dies badly can leave this process running while the gateway,
    seeing its channel drop, reasonably concludes it should start a replacement. A flock
    is the only thing that makes "exactly one" true no matter how the ssh died: the second
    process cannot take the lock, so it exits instead of racing the first one.

    Returns the held file object (which must stay referenced for the lock to persist), or
    None if another instance holds it.
    """
    path = os.path.join(HERE, "loop.lock")
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    fh.flush()
    return fh


def main():
    lock = acquire_single_instance_lock()
    if lock is None:
        emit("EXIT", f"{EXIT_ALREADY_RUNNING} another remote_loop.py already holds the "
                     f"single-instance lock on this host; refusing to race the same "
                     f"ezloan session")
        return EXIT_ALREADY_RUNNING

    raw = sys.stdin.readline()
    if not raw.strip():
        emit("EXIT", f"{EXIT_CRASH} no startup payload on stdin")
        return EXIT_CRASH
    cfg = json.loads(raw)

    app_dir = cfg.get("app_dir") or os.path.join(os.path.expanduser("~"), ".ezloan-loop")
    os.makedirs(app_dir, exist_ok=True)
    os.environ["EZLOAN_APP_DIR"] = app_dir
    os.environ.setdefault("EZLOAN_REMOTE_SOURCE", cfg.get("remote_source") or "ezloan-server-kr")
    # Tick/window overrides land in the environment BEFORE config is imported, because
    # config reads them at import time.
    for k, v in (cfg.get("env") or {}).items():
        if v is not None:
            os.environ[str(k)] = str(v)

    import config
    import egress

    expect_ip = (cfg.get("expect_ip") or "").strip()
    expect_country = (cfg.get("expect_country") or "KR").strip().upper()

    # --- egress: fail closed BEFORE anything touches ezloan -----------------------
    try:
        egress.configure_direct(expect_ip, expect_country=expect_country)
        ip, country = egress.require(check_url=config.RQ_URL)
    except Exception as e:
        emit("EXIT", f"{EXIT_EGRESS} egress refused: {e}")
        return EXIT_EGRESS
    log(f"[egress] DIRECT VERIFIED  this-host={ip} ({country}) is the pinned KR address; "
        f"no proxy in the path, ezloan.io=200")
    log(f"[loop] tick={config.FRONTIER_POLL_SECONDS}s window={config.PROBE_WINDOW} "
        f"list={config.LIST_POLL_SECONDS}s lookahead={config.LOOKAHEAD} "
        f"(fast-tick req/s = {(config.PROBE_WINDOW + 1) / config.FRONTIER_POLL_SECONDS:.1f})")

    from bridge import remote_log
    from ezloan_bot import Registrar

    stop_event = threading.Event()
    fatal = {"code": EXIT_OK, "reason": "stop requested"}

    def should_stop():
        return stop_event.is_set()

    def remote(event, detail="", snapshot="", force=False):
        try:
            remote_log(event, detail, snapshot, force=force)
        except Exception:
            pass

    def stop_with(code, reason):
        if fatal["code"] == EXIT_OK:
            fatal["code"] = code
            fatal["reason"] = reason
        stop_event.set()

    # --- keepalive watchdog: an orphan must not outlive its parent -----------------
    last_beat = {"at": time.time()}

    def _stdin_reader():
        try:
            for line in sys.stdin:
                if line.strip() == "STOP":
                    log("[loop] STOP received from the gateway")
                    stop_with(EXIT_OK, "stop requested")
                    return
                last_beat["at"] = time.time()
        except Exception:
            pass
        # EOF: the ssh channel closed, so the parent is gone.
        log("[loop] the gateway closed the control channel")
        stop_with(EXIT_OK, "control channel closed")

    def _watchdog():
        while not stop_event.wait(5.0):
            if time.time() - last_beat["at"] > KEEPALIVE_TIMEOUT:
                log(f"[loop] no keepalive for {KEEPALIVE_TIMEOUT:.0f}s, exiting so the "
                    f"ezloan session is never raced by an orphaned second loop")
                stop_with(EXIT_KEEPALIVE, "keepalive lost")
                return

    threading.Thread(target=_stdin_reader, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()

    # --- egress guard -------------------------------------------------------------
    def on_egress_violation(reason):
        log(f"[egress] FATAL {reason} -- stopping rather than egressing from a "
            f"non-Korean address")
        remote("server_egress_violation",
               f"external-1 direct egress: {reason}. Loop stopped.", force=True)
        stop_with(EXIT_EGRESS, f"egress guard: {reason}")

    egress.start_guard(on_egress_violation,
                       interval=float(cfg.get("guard_interval") or 60.0),
                       stop_event=stop_event)

    # --- seen mirror --------------------------------------------------------------
    seen_path = os.path.join(app_dir, "seen-posts.json")
    if cfg.get("seen"):
        try:
            with open(seen_path, "w", encoding="utf-8") as fh:
                json.dump({"seen": list(cfg["seen"])}, fh, ensure_ascii=False)
        except Exception as e:
            log(f"[loop] could not seed seen-posts.json: {e}")

    def _seen_mirror():
        last_sig = None
        while not stop_event.wait(SEEN_WATCH_SECONDS):
            try:
                sig = os.path.getmtime(seen_path)
            except Exception:
                continue
            if sig == last_sig:
                continue
            last_sig = sig
            try:
                with open(seen_path, encoding="utf-8") as fh:
                    emit("SEEN", json.dumps(json.load(fh), ensure_ascii=False))
            except Exception:
                pass

    threading.Thread(target=_seen_mirror, daemon=True).start()

    # --- the loop -----------------------------------------------------------------
    registrar = None
    try:
        registrar = Registrar(
            cfg.get("cookies") or [],
            log=log,
            remote=remote,
            should_stop=should_stop,
            seen_path=seen_path,
            # No browser on this host. A dead session is reported UP to the gateway,
            # which owns Chrome and the credentials, and it relaunches us with fresh
            # cookies. Returning None here makes Registrar treat it as "cannot relogin".
            relogin=lambda: stop_with(EXIT_SESSION_DEAD, "session dead, gateway must re-login"),
            status=lambda t: log(f"[status] {t}"))
        registrar.run()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        log(f"[fatal] {tb}")
        remote("server_run_error", tb[:3000], force=True)
        stop_with(EXIT_CRASH, "crash")
    finally:
        if registrar is not None:
            try:
                registrar.close()
            except Exception:
                pass
        # Final mirror so the gateway's copy of seen is never behind ours.
        try:
            with open(seen_path, encoding="utf-8") as fh:
                emit("SEEN", json.dumps(json.load(fh), ensure_ascii=False))
        except Exception:
            pass
        emit("EXIT", f"{fatal['code']} {fatal['reason']}")
        time.sleep(1.5)   # let bridge's async upload thread finish
    return fatal["code"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        emit("EXIT", f"{EXIT_CRASH} {traceback.format_exc()[:500]}")
        sys.exit(EXIT_CRASH)
