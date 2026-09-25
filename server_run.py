#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless server-side run of the SAME Registrar loop the customer's Windows app runs.

Customer 5136338 (더원대부). Their PC is unavailable, so we run the bot for them from our
server until they take it back. This file is the only new entry point: it imports
ezloan_bot / naver_login / browser / session_store / bridge unchanged, and never touches
app.py or captcha_dialog.py (the two modules that need tkinter).

Three things make this different from main.py, and all three are safety properties:

  1. NO TKINTER. tkinter is not installed on a server and a GUI would be pointless anyway.
     The captcha handler is bridge.OwnerCaptchaBridge (uploads the image to the Artifacts
     API, polls works.insu.ng for the answer), which already exists for exactly this case.

  2. KOREAN EGRESS, FAIL CLOSED. All ezloan.io and Naver traffic leaves through a dedicated
     `ssh -N -D` SOCKS tunnel to a Korean node. If that route is missing, wrong, or dies,
     the run refuses to start or stops. It never falls back to this host's own address:
     ezloan.io 403s a non-KR IP (loud), and a Naver login from a non-KR IP protection-LOCKS
     the customer's real Naver account (silent, and their problem to unwind, not ours).

  3. CREDENTIALS NEVER LAND. The id/pw arrive in the parent process's environment, are
     handed to the daemon over a pipe, and the daemon's own environment is scrubbed of them
     before it is spawned (so `ps eww` / /proc/<pid>/environ on the long-lived process shows
     nothing). They are held in memory only, redacted out of every log line and every
     Artifacts upload, and never written to disk.

ONE MORE THING, AND IT MATTERS MORE THAN ANY OF THE ABOVE: the ezloan account is
single-session. If our run and the customer's own copy are up at the same time, the two
race the same session and both degrade. Before telling the customer to restart their PC
app, stop this one:

    python3 server_run.py stop

Commands
--------
    server_run.py start                 start the real run (needs EZLOAN_NAVER_ID/PW)
    server_run.py start --dry-run       start WITHOUT credentials: egress + detection only,
                                        never logs in, never registers, never writes
    server_run.py start --foreground    run in this terminal instead of daemonising
    server_run.py stop                  stop it and hand control back to the customer
    server_run.py status                is it running, since when, what egress
    server_run.py selfcheck             one-shot preflight, exits non-zero on any failure
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Run state lives OUTSIDE the git repo on purpose: it holds session.json (live ezloan
# session cookies), which must never be committable by accident.
RUN_DIR = Path(os.getenv("EZLOAN_SERVER_DIR") or (Path.home() / ".ezloan-server" / "5136338"))
PID_FILE = RUN_DIR / "run.pid"
STOP_FILE = RUN_DIR / "STOP"
LOG_FILE = RUN_DIR / "run.log"
STATE_FILE = RUN_DIR / "state.json"

# Defaults for the Korean egress. external-1 measured fastest to ezloan.io from this host
# (warm keep-alive p50 77.5ms vs 90.0 external-6, 184.9 external-8) on 2026-08-23.
DEFAULT_SSH_HOST = os.getenv("EZLOAN_EGRESS_SSH", "unicorn@external-1")
DEFAULT_SOCKS_PORT = int(os.getenv("EZLOAN_EGRESS_PORT", "1085"))
DEFAULT_EXPECT_IP = os.getenv("EZLOAN_EGRESS_EXPECT_IP", "13.124.160.237")

# Env that must be in place BEFORE config is imported (config reads it at import time).
os.environ.setdefault("EZLOAN_APP_DIR", str(RUN_DIR))
os.environ.setdefault("EZLOAN_CHROME_PROFILE_DIR", str(RUN_DIR / "chrome-profile"))

def _app_version():
    import re
    try:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', (HERE / "config.py").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


os.environ.setdefault("EZLOAN_REMOTE_SOURCE", f"ezloan-server-v{_app_version()}")
# Present only while a live run holds a working session. scripts/server-watchdog.sh restarts
# the run (session-resume only, no credentials) after a crash or host reboot while it exists.
# Removed by `stop`, by a verification screen, and by an egress violation.
AUTORESTART_FILE = RUN_DIR / "AUTORESTART"


# --------------------------------------------------------------------------- logging
_redactions = []


def redact(text):
    """Strip credentials out of anything that is about to be logged or uploaded."""
    if not text:
        return text
    out = str(text)
    for secret in _redactions:
        if secret:
            out = out.replace(secret, "***")
    return out


def add_redaction(*secrets):
    for s in secrets:
        if s and len(s) >= 3:
            _redactions.append(s)


_log_lock = threading.Lock()
_log_fh = None


def log(text):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {redact(text)}"
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        if _log_fh is not None:
            try:
                _log_fh.write(line + "\n")
                _log_fh.flush()
            except Exception:
                pass


def open_log():
    """Tee log() into run.log as well as stdout.

    NOT called in the daemon (`_child`): there stdout is already redirected into run.log by
    the parent, so opening the file again would write every line twice.
    """
    global _log_fh
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _log_fh = open(LOG_FILE, "a", encoding="utf-8")


# --------------------------------------------------------------------------- tunnel
class KrTunnel:
    """A dedicated `ssh -N -D` SOCKS5 tunnel to a Korean host, owned by this process.

    Deliberately NOT the shared PM2 `kr-socks-navercafe` tunnel on 127.0.0.1:1080: that one
    is the Kmong egress path, and a 20 req/s registration loop has no business sharing it
    (nor should stopping our run be able to disturb it).

    Credential-free by construction, which is what lets Chrome take the identical route.
    """

    def __init__(self, ssh_host, port):
        self.ssh_host = ssh_host
        self.port = int(port)
        self.proc = None

    @property
    def url(self):
        return f"socks5h://127.0.0.1:{self.port}"

    def _port_open(self):
        import socket
        with socket.socket() as sock:
            sock.settimeout(1.0)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self, timeout=30):
        if self._port_open():
            raise RuntimeError(
                f"127.0.0.1:{self.port} is already in use. Another run may be live "
                f"(`server_run.py status`), or pick another EZLOAN_EGRESS_PORT.")
        cmd = [
            "ssh", "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
            "-D", f"127.0.0.1:{self.port}",
            self.ssh_host,
        ]
        log(f"[egress] opening KR tunnel: ssh -N -D 127.0.0.1:{self.port} {self.ssh_host}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"ssh tunnel to {self.ssh_host} exited immediately "
                    f"(rc={self.proc.returncode}). Check ssh key access to that host.")
            if self._port_open():
                log(f"[egress] tunnel up on {self.url}")
                return self.url
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"ssh tunnel to {self.ssh_host} did not open within {timeout}s")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# --------------------------------------------------------------------------- helpers
def read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_state(**kw):
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except Exception:
                state = {}
        state.update(kw)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


# --------------------------------------------------------------------------- child
def _run_child(args, creds):
    import config
    import egress
    from bridge import remote_log
    from ezloan_bot import Registrar

    stop_event = threading.Event()
    fatal = {"reason": None}

    def should_stop():
        return stop_event.is_set() or STOP_FILE.exists()

    def remote(event, detail="", snapshot="", force=False):
        try:
            remote_log(event, redact(detail), redact(snapshot), force=force)
        except Exception:
            pass

    def on_signal(signum, _frame):
        log(f"[stop] signal {signum} received, shutting the registration loop down")
        stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    tunnel = KrTunnel(args.ssh_host, args.socks_port)
    registrar = None
    rc = 0
    try:
        # --- 1. Korean egress, fail closed -------------------------------------
        proxy_url = tunnel.start()
        os.environ["EZLOAN_EGRESS_PROXY"] = proxy_url
        config.EGRESS_PROXY = proxy_url
        egress.configure(proxy_url, expect_country=args.expect_country,
                         expect_ip=args.expect_ip or None)
        ip, country = egress.require(check_url=config.RQ_URL)
        own = egress.direct_ip()
        log(f"[egress] VERIFIED  egress={ip} ({country})  this-host={own}  "
            f"ezloan.io=200 through the tunnel")
        write_state(egressIp=ip, egressCountry=country, hostIp=own,
                    sshHost=args.ssh_host, socksPort=args.socks_port,
                    loopHost=args.loop_host or "(this host, via the tunnel)",
                    loopExpectIp=args.loop_expect_ip or None,
                    loopTick=args.loop_tick or None, loopWindow=args.loop_window or None,
                    dryRun=bool(args.dry_run), startedAt=time.time(), pid=os.getpid())

        def on_egress_violation(reason):
            # When the loop runs HERE, this tunnel is the customer's only route and a
            # violation must stop everything. When the loop runs in Korea it is not in
            # the hot path at all: it exists only so the Chrome login has a KR route.
            # Killing a healthy remote loop because a login-only tunnel blipped is how we
            # lost 5 minutes at 04:09 today, so in that mode this is a repair, not a kill.
            if args.loop_host:
                log(f"[egress] the login tunnel is unusable ({reason}). The KR loop is "
                    f"unaffected (it has its own guard); reopening the tunnel.")
                remote("login_tunnel_degraded",
                       f"로그인용 터널 이상({reason}). KR 루프는 그대로 동작하며, "
                       f"터널만 다시 엽니다.", force=True)
                try:
                    tunnel.stop()
                    proxy = tunnel.start()
                    egress.configure(proxy, expect_country=args.expect_country,
                                     expect_ip=args.expect_ip or None)
                    egress.require()
                    log("[egress] login tunnel reopened and re-verified")
                except Exception as e:
                    log(f"[egress] could not reopen the login tunnel: {redact(str(e))}. "
                        f"A re-login will be refused until it comes back, which is the "
                        f"safe failure: a Naver login from a non-KR address locks the "
                        f"customer's account.")
                # Re-arm; start_guard's thread returns after any violation.
                egress.start_guard(on_egress_violation, interval=args.guard_interval,
                                   stop_event=stop_event)
                return
            fatal["reason"] = f"egress guard: {reason}"
            AUTORESTART_FILE.unlink(missing_ok=True)
            log(f"[egress] FATAL {reason} -- stopping the run rather than egressing "
                f"from this host")
            remote("server_egress_violation", f"{reason}. Run stopped.", force=True)
            stop_event.set()

        egress.start_guard(on_egress_violation, interval=args.guard_interval,
                           stop_event=stop_event)

        # --- 2. announce ourselves ---------------------------------------------
        remote("server_run_start",
               f"server-side headless run started for customer {config.CUSTOMER_ID} "
               f"(v{config.APP_VERSION}, dry_run={bool(args.dry_run)}). "
               f"egress={ip}/{country} via {args.ssh_host}. "
               f"Stop with: server_run.py stop", force=True)
        log("[run] " + ("DRY RUN: no login, no registration, no writes."
                        if args.dry_run else "live run"))
        log("[run] STOP THIS RUN BEFORE THE CUSTOMER RESTARTS THEIR OWN COPY: "
            f"python3 {HERE / 'server_run.py'} stop")

        if args.dry_run:
            rc = _dry_run_loop(args, should_stop, log, remote)
        else:
            cookies = _login(args, creds, should_stop, log, remote, ip)
            if cookies is None:
                rc = 4
            elif args.loop_host:
                # The hot loop runs IN Korea; the login stayed here. If the remote cannot
                # be kept alive we fall back to running it here through the tunnel, which
                # is slower but is exactly what we did before, so a broken loop host
                # degrades the latency instead of taking the customer's run offline.
                ok = _run_remote_loop(args, creds, cookies, ip, should_stop, stop_event,
                                      log, remote)
                if not ok and not should_stop():
                    log("[loop] falling back to running the loop HERE through the tunnel")
                    remote("loop_host_fallback",
                           f"{args.loop_host} 에서 루프를 유지하지 못해 게이트웨이(터널 경유)로 "
                           f"되돌립니다. 등록은 계속되지만 등록 지연이 커집니다.", force=True)
                    registrar = _local_registrar(args, creds, cookies, ip, should_stop,
                                                 log, remote)
                    registrar.run()
            else:
                registrar = _local_registrar(args, creds, cookies, ip, should_stop,
                                             log, remote)
                registrar.run()
        if fatal["reason"]:
            rc = 5
    except Exception:
        import traceback
        tb = redact(traceback.format_exc())
        log(f"[fatal] {tb}")
        remote("server_run_error", tb[:3000], force=True)
        rc = 3
    finally:
        if registrar is not None:
            try:
                registrar.close()
            except Exception:
                pass
        tunnel.stop()
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        reason = fatal["reason"] or ("stop requested" if rc == 0 else f"exit {rc}")
        log(f"[stopped] server run finished cleanly ({reason}). "
            f"The customer may restart their own copy now.")
        try:
            remote("server_run_stopped",
                   f"server-side run stopped ({reason}). The ezloan session is free; the "
                   f"customer's own copy can be restarted.", force=True)
            time.sleep(2)   # let the async upload thread finish before the process dies
        except Exception:
            pass
    return rc


def _local_registrar(args, creds, cookies, ip, should_stop, log, remote):
    """The Registrar running HERE, through the SOCKS tunnel. The original path."""
    import config
    from ezloan_bot import Registrar

    return Registrar(
        cookies, log=log, remote=remote, should_stop=should_stop,
        seen_path=os.path.join(config.APP_DIR, "seen-posts.json"),
        relogin=lambda: _login(args, creds, should_stop, log, remote, ip, forced=True),
        status=lambda t: log(f"[status] {t}"))


# --------------------------------------------------------- the loop, run in Korea
LOOP_FILES = ("remote_loop.py", "ezloan_bot.py", "config.py", "egress.py", "bridge.py",
              "session_store.py")

# remote_loop.py exit codes (kept in sync with that file)
_LOOP_EXIT_OK = 0
_LOOP_EXIT_SESSION_DEAD = 4
_LOOP_EXIT_EGRESS = 5
_LOOP_EXIT_ALREADY_RUNNING = 7
# Must be >= remote_loop.KEEPALIVE_TIMEOUT. When the remote did not report @@EXIT we do
# not know it is dead, so this is how long we wait for its own watchdog to kill it before
# we start any replacement loop. The ezloan account is single-session; a few seconds of
# no coverage is cheap, two loops racing the same session is not.
_LOOP_ORPHAN_GRACE = 55.0


def _deploy_loop(args, log):
    """Push the loop's source to the Korean node. Plain tar over ssh: no rsync there."""
    import tarfile
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in LOOP_FILES:
            tar.add(str(HERE / name), arcname=name)
    payload = buf.getvalue()
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=20", args.loop_host,
           f"mkdir -p {args.loop_dir} && tar xzf - -C {args.loop_dir}"]
    p = subprocess.run(cmd, input=payload, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"deploy to {args.loop_host} failed rc={p.returncode}: "
                           f"{(p.stdout or b'').decode('utf-8', 'replace')[:400]}")
    log(f"[loop] deployed {len(LOOP_FILES)} files to {args.loop_host}:{args.loop_dir} "
        f"({len(payload)} bytes)")


def _confirm_remote_dead(args, log, timeout=25):
    """Best effort: kill any surviving remote loop and confirm none is left.

    Returns True only when we positively observed zero remote_loop.py processes on the
    host. Unreachable host, ssh failure, ambiguous output: all False, because "I could not
    check" must never be read as "it is gone".
    """
    # The [l] is not a typo. pgrep -f matches against full command lines, and this very
    # ssh command IS a command line on that host, so a plain "remote_loop.py" pattern
    # counts itself and the check can never read zero. "remote_[l]oop.py" matches the real
    # process but not the literal text of the checking command.
    pat = "python3 -u remote_[l]oop.py"
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           "-o", f"ConnectTimeout={int(timeout)}", args.loop_host,
           f"pkill -f '{pat}'; sleep 2; pgrep -fc '{pat}' || echo 0"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout + 15)
    except Exception as e:
        log(f"[loop] could not reach {args.loop_host} to check for a surviving loop: {e}")
        return False
    out = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    tail = out[-1].strip() if out else ""
    if p.returncode != 0:
        log(f"[loop] orphan check on {args.loop_host} failed rc={p.returncode}: {tail[:200]}")
        return False
    if tail == "0":
        log(f"[loop] confirmed no remote loop is running on {args.loop_host}")
        return True
    log(f"[loop] {args.loop_host} still reports {tail} remote_loop.py process(es)")
    return False


def _read_seen():
    try:
        return list(json.loads((RUN_DIR / "seen-posts.json").read_text()).get("seen") or [])
    except Exception:
        return []


def _write_seen(obj):
    try:
        tmp = RUN_DIR / "seen-posts.json.tmp"
        tmp.write_text(json.dumps(obj, ensure_ascii=False))
        tmp.replace(RUN_DIR / "seen-posts.json")
    except Exception:
        pass


def _run_remote_loop(args, creds, cookies, ip, should_stop, stop_event, log, remote):
    """Supervise one Registrar loop running on the Korean node. Returns True if it ended
    because we asked it to, False if it died in a way that needs the local fallback.

    Two invariants this function exists to hold:

      1. EXACTLY ONE loop is live. The remote is launched over a single ssh whose stdin we
         hold open and heartbeat; the remote exits on its own if the heartbeat stops, so an
         ssh that dies badly cannot leave an orphan racing the same ezloan session.
      2. The gateway's copy of `seen` never falls behind the remote's, because the remote
         mirrors it down on every change. A fallback to the local loop therefore does not
         re-register posts the remote already did.
    """
    import config

    if not args.loop_expect_ip:
        log("[loop] --loop-host given without --loop-expect-ip; refusing to start a "
            "remote loop with no pinned Korean address")
        return False

    try:
        _deploy_loop(args, log)
    except Exception as e:
        log(f"[loop] {redact(str(e))}")
        return False

    env_overrides = {"EZLOAN_REMOTE_SOURCE": os.environ.get("EZLOAN_REMOTE_SOURCE", "")}
    if args.loop_tick:
        env_overrides["EZLOAN_FRONTIER_POLL_SECONDS"] = args.loop_tick
    if args.loop_window:
        env_overrides["EZLOAN_PROBE_WINDOW"] = args.loop_window

    attempts = 0
    max_attempts = 5
    while not should_stop():
        payload = {
            "cookies": cookies,
            "expect_ip": args.loop_expect_ip,
            "expect_country": args.expect_country,
            "guard_interval": args.guard_interval,
            "app_dir": f"{args.loop_dir}/state",
            "remote_source": os.environ.get("EZLOAN_REMOTE_SOURCE", ""),
            "env": env_overrides,
            "seen": _read_seen(),
        }
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "TCPKeepAlive=yes",
               args.loop_host,
               f"cd {args.loop_dir} && exec python3 -u remote_loop.py"]
        log(f"[loop] starting the Registrar on {args.loop_host} "
            f"(pinned egress {args.loop_expect_ip}, attempt {attempts + 1}/{max_attempts})")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=1, text=True,
                                encoding="utf-8", errors="replace")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as e:
            log(f"[loop] could not hand the remote its startup payload: {e}")
            try:
                proc.kill()
            except Exception:
                pass
            attempts += 1
            if attempts >= max_attempts:
                return False
            time.sleep(5)
            continue

        exit_code = {"v": None, "reason": ""}
        started = time.time()

        def _beat():
            """Heartbeat + stop signal. The remote dies on its own if this stops."""
            while proc.poll() is None:
                if should_stop():
                    try:
                        proc.stdin.write("STOP\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                    return
                try:
                    proc.stdin.write("PING\n")
                    proc.stdin.flush()
                except Exception:
                    return
                time.sleep(10)

        beat = threading.Thread(target=_beat, name="ezloan-loop-beat", daemon=True)
        beat.start()

        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith("@@LOG "):
                log("[kr] " + line[6:])
            elif line.startswith("@@SEEN "):
                try:
                    _write_seen(json.loads(line[7:]))
                except Exception:
                    pass
            elif line.startswith("@@EXIT "):
                bits = line[7:].split(" ", 1)
                try:
                    exit_code["v"] = int(bits[0])
                except Exception:
                    exit_code["v"] = None
                exit_code["reason"] = bits[1] if len(bits) > 1 else ""
            elif line.strip():
                log("[kr!] " + line[:400])

        proc.wait(timeout=30)
        rc = exit_code["v"] if exit_code["v"] is not None else proc.returncode
        uptime = time.time() - started
        log(f"[loop] the remote loop exited rc={rc} after {uptime:.0f}s "
            f"({exit_code['reason'] or 'no reason reported'})")

        # If the remote never reported @@EXIT, the ssh died without us hearing from the
        # loop, so we do NOT know it is dead. Try to kill it, and if we cannot even reach
        # the host, wait out its own keepalive watchdog before starting anything else.
        # This is the whole single-session guarantee: it must hold even when the network
        # is what broke.
        if exit_code["v"] is None and not should_stop():
            if not _confirm_remote_dead(args, log):
                log(f"[loop] could not confirm the remote loop is gone; waiting "
                    f"{_LOOP_ORPHAN_GRACE:.0f}s for its own watchdog before starting any "
                    f"replacement, so the ezloan session is never raced")
                if stop_event.wait(_LOOP_ORPHAN_GRACE):
                    return True

        if should_stop():
            return True
        if rc == _LOOP_EXIT_ALREADY_RUNNING:
            # Another loop holds the lock on that host. Never start a second one; wait for
            # the first to release it.
            log("[loop] a loop is already running on that host; backing off")
            attempts += 1
        elif rc == _LOOP_EXIT_OK:
            # It ended cleanly without us asking. Treat as a restart, not a fallback.
            attempts += 1
        elif rc == _LOOP_EXIT_SESSION_DEAD:
            log("[loop] the remote says the ezloan session is dead; re-logging in here")
            remote("loop_relogin", "KR 루프가 세션 만료를 보고해 게이트웨이에서 재로그인합니다.",
                   force=True)
            fresh = _login(args, creds, should_stop, log, remote, ip, forced=True)
            if not fresh:
                log("[loop] re-login failed; handing back to the local loop")
                return False
            cookies = fresh
            attempts = 0          # a successful re-login is progress, not a failure
            continue
        elif rc == _LOOP_EXIT_EGRESS:
            # The Korean node's own address moved or became unverifiable. Do NOT retry it:
            # the whole point of the pin is that we stop instead of guessing.
            log("[loop] the remote refused on egress grounds; not retrying that host")
            return False
        else:
            attempts += 1

        # A loop that stayed up for a good while and then died is worth restarting; one
        # that dies immediately, repeatedly, is a broken host.
        if uptime > 300:
            attempts = 1
        if attempts >= max_attempts:
            log(f"[loop] {attempts} consecutive failures on {args.loop_host}")
            return False
        delay = min(30.0, 3.0 * (2 ** (attempts - 1)))
        log(f"[loop] retrying in {delay:.0f}s")
        if stop_event.wait(delay):
            return True
    return True


def _dry_run_loop(args, should_stop, log, remote):
    """Everything except logging in: the real hot path, driven anonymously.

    Uses Registrar._scan_frontier (the actual v2.6.0 parallel probe + check tick) against
    real ezloan.io post ids with an EMPTY cookie jar, so the customer's account is never
    authenticated, never checked, and never written to. Proves the loop, the egress, the
    tick scheduler and the stop path without spending a single 배너잔여.
    """
    import config
    from ezloan_bot import Registrar, list_post_ids, new_probe_session

    probe = new_probe_session()
    ids = list_post_ids(probe)
    latest = max(int(x) for x in ids if str(x).isdigit())
    log(f"[dry] ezloan.io newest post id = {latest} (anonymous /rq list, {len(ids)} ids)")
    reg = Registrar([], log=log, remote=remote, should_stop=should_stop,
                    seen_path=None, relogin=None, status=lambda t: None)
    ticks = 0
    t_end = time.time() + float(args.dry_seconds) if args.dry_seconds else None
    try:
        while not should_stop():
            started = time.time()
            live, precheck = reg._scan_frontier(latest, config.PROBE_WINDOW)
            ticks += 1
            if ticks % 20 == 1:
                log(f"[dry] tick {ticks}: scanned {latest}..{latest + config.PROBE_WINDOW - 1} "
                    f"-> live={live} scan={1000 * (time.time() - started):.0f}ms "
                    f"precheck={'(none, anonymous)' if precheck is None else precheck[0]}")
            if t_end and time.time() >= t_end:
                log(f"[dry] {args.dry_seconds}s elapsed, {ticks} ticks done")
                break
            time.sleep(reg._fast_sleep(started))
    finally:
        reg.close()
        try:
            probe.close()
        except Exception:
            pass
    log(f"[dry] finished after {ticks} ticks, nothing was registered")
    return 0


def _login(args, creds, should_stop, log, remote, expected_ip, forced=False):
    """Naver -> ezloan login in a headless Chrome that is pinned to the Korean egress.

    Returns cookies (list[dict]) or None. The browser's OWN egress is verified before a
    single character of the password is typed: Selenium takes --proxy-server, and if that
    silently failed we would be logging into the customer's Naver account from a Japanese
    address, which locks it. So the check is not "nice to have", it is the gate.
    """
    import config
    import egress
    from browser import build_driver
    from bridge import OwnerCaptchaBridge
    from naver_login import NaverLogin, LoginTemporarilyUnavailable
    from session_store import save_session, validate_saved_session, clear_session

    if not forced:
        cookies, _s = validate_saved_session()
        if cookies:
            log(f"[login] reusing the saved ezloan session ({len(cookies)} cookies), "
                f"no Naver login needed")
            remote("server_session_recovered",
                   f"saved session still valid ({len(cookies)} cookies), "
                   f"registration resumes without a Naver login", force=True)
            return cookies
    else:
        clear_session()

    if not creds.get("id") or not creds.get("pw"):
        log("[login] no credentials in memory, cannot log in")
        # A resumed run cannot heal a dead session; stop the watchdog from looping on it.
        AUTORESTART_FILE.unlink(missing_ok=True)
        remote("server_login_no_creds",
               "forced relogin requested but no credentials are held in memory", force=True)
        return None

    proxy_arg = egress.chrome_proxy_arg()
    chrome_proxy = proxy_arg.split("=", 1)[1]
    driver = None
    try:
        log("[login] preparing the bundled Chrome (first run downloads it, 1-2 min)")
        driver = build_driver(headless=True, log=log, proxy=chrome_proxy)

        # Gate: prove the BROWSER, not just requests, leaves from Korea.
        driver.get("https://api.ipify.org/?format=json")
        body = driver.find_element("tag name", "body").text
        seen_ip = json.loads(body).get("ip", "")
        if seen_ip != expected_ip:
            remote("server_login_egress_mismatch",
                   f"Chrome egress {seen_ip} != verified egress {expected_ip}. "
                   f"Login ABORTED before any credential was typed, to avoid "
                   f"protection-locking the customer's Naver account.", force=True)
            log(f"[login] FATAL Chrome egress is {seen_ip}, expected {expected_ip}. "
                f"Aborting before typing any credential.")
            return None
        log(f"[login] Chrome egress verified: {seen_ip}")

        captcha = OwnerCaptchaBridge(log=log, status=lambda t: log(f"[captcha] {t}"),
                                     timeout=args.captcha_timeout)

        def on_verification(url, body, hit):
            # Naver wants the account owner (2-step, protection lock, new device). Retrying
            # from here only deepens the lock, so record the exact screen and stop the run.
            text = redact(body or "").strip()
            log(f"[login] VERIFICATION SCREEN (markers {hit}) at {redact(url)}\n"
                f"----- screen text -----\n{text[:3000]}\n----- end -----")
            remote("server_login_verification_required",
                   f"markers={hit} url={url}\n{text[:3000]}", force=True)
            AUTORESTART_FILE.unlink(missing_ok=True)
            try:
                STOP_FILE.write_text("naver verification screen, run stopped\n")
            except Exception:
                pass

        login = NaverLogin(driver, log=log, captcha_callback=captcha,
                           should_stop=should_stop, on_verification=on_verification)
        try:
            ok = login.login(creds["id"], creds["pw"])
        except LoginTemporarilyUnavailable as e:
            log(f"[login] temporarily unavailable after all retries: {redact(str(e))[:300]}")
            remote("server_login_temporarily_unavailable", str(e)[:400], force=True)
            return None
        if not ok:
            log("[login] Naver login failed (check the id/password)")
            remote("server_login_failed", "naver login returned False", force=True)
            return None

        driver.get(config.RQ_URL)
        time.sleep(1.0)
        cookies = driver.get_cookies()
        ez = [c for c in cookies if "ezloan" in (c.get("domain") or "")]
        names = sorted({c.get("name", "") for c in ez})
        has_sess = any("ezloan_sess" in n or "ci_session" in n for n in names)
        remote("server_login_success",
               f"cookies={len(cookies)}, ezloan-domain={len(ez)}, "
               f"ezloan_sess={'yes' if has_sess else 'no'}, names={names[:20]}", force=True)
        if not has_sess:
            log("[login] logged into Naver but no ezloan session cookie was issued")
            return None
        save_session(cookies, log=log)
        try:
            os.chmod(config.SESSION_FILE, 0o600)
        except Exception:
            pass
        log(f"[login] ezloan session acquired ({len(cookies)} cookies), registration starts")
        try:
            AUTORESTART_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ\n", time.gmtime()))
        except Exception:
            pass
        return cookies
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# --------------------------------------------------------------------------- selfcheck
def cmd_selfcheck(args):
    """One-shot preflight: egress, anonymous detection, Artifacts API. No login, no writes."""
    open_log()
    import config
    import egress
    from ezloan_bot import new_probe_session, post_live, list_post_ids

    tunnel = KrTunnel(args.ssh_host, args.socks_port)
    rc = 0
    try:
        proxy_url = tunnel.start()
        egress.configure(proxy_url, expect_country=args.expect_country,
                         expect_ip=args.expect_ip or None)
        ip, country = egress.require(check_url=config.RQ_URL)
        log(f"[selfcheck] egress={ip} ({country})  host={egress.direct_ip()}  ezloan.io=200")

        config.EGRESS_PROXY = proxy_url
        probe = new_probe_session()
        assert probe.proxies.get("https") == proxy_url, "probe session is not on the egress"
        ids = list_post_ids(probe)
        latest = max(int(x) for x in ids if str(x).isdigit())
        future = latest + 500
        t0 = time.time()
        live_real = post_live(probe, str(latest))
        t_real = (time.time() - t0) * 1000
        t0 = time.time()
        live_future = post_live(probe, str(future))
        t_future = (time.time() - t0) * 1000
        log(f"[selfcheck] detection: post {latest} -> {live_real} ({t_real:.0f}ms), "
            f"post {future} -> {live_future} ({t_future:.0f}ms)")
        if live_real is not True or live_future is not False:
            log("[selfcheck] FAIL: the anonymous detection path did not behave as expected")
            rc = 1

        # The login path is Selenium, and Selenium takes a different code path to the
        # proxy than requests does. Prove Chrome ALSO leaves from Korea, because that is
        # the check that stands between us and protection-locking the customer's Naver
        # account. No credentials are involved and Naver is never visited.
        if args.browser:
            from browser import build_driver
            driver = None
            try:
                driver = build_driver(headless=True, log=log,
                                      proxy=egress.chrome_proxy_arg().split("=", 1)[1])
                driver.get("https://api.ipify.org/?format=json")
                seen_ip = json.loads(driver.find_element("tag name", "body").text)["ip"]
                log(f"[selfcheck] chrome egress={seen_ip} (expected {ip})")
                if seen_ip != ip:
                    log("[selfcheck] FAIL: Chrome is not on the Korean route")
                    rc = 1
                # Walk the REAL navigation the login takes (ezloan /m/login -> click
                # "네이버로 로그인" -> Naver OAuth form) and check the selectors
                # naver_login.py actually drives. NOTHING is typed and no account is
                # identified, so this cannot touch the customer's Naver account.
                from naver_login import NaverLogin
                probe_login = NaverLogin(driver, log=lambda *a: None,
                                         should_stop=lambda: False)
                probe_login._open_naver_from_ezloan()
                html = driver.page_source
                found = {"#id": bool(driver.find_elements("css selector", "#id")),
                         "#pw": bool(driver.find_elements("css selector", "#pw"))}
                submit_hit = None
                for how, sel in NaverLogin.LOGIN_BUTTON_SELECTORS:
                    els = driver.find_elements(how, sel)
                    vis = [e for e in els if e.is_displayed() and e.is_enabled()]
                    found[f"{how}={sel}"] = f"{len(els)} ({len(vis)} visible)"
                    if vis and submit_hit is None:
                        submit_hit = sel
                # Guardrail evidence: prove that a bare `button.btn_done` would grab the
                # PASSKEY button. naver_login.py deliberately never uses that selector, and
                # this line is what keeps that claim measured instead of remembered.
                done_ids = [(e.get_attribute("id") or "?",
                             e.is_displayed()) for e in
                            driver.find_elements("css selector", "button.btn_done")]
                log(f"[selfcheck]   button.btn_done in DOM order={done_ids}")
                blocked = [m for m in ("보호조치", "새로운 기기", "idSafetyRelease", "점검")
                           if m in html]
                log(f"[selfcheck] naver oauth form at {driver.current_url[:70]}...: "
                    f"title={driver.title!r} submit={submit_hit!r} "
                    f"block_markers={blocked or 'none'}")
                log(f"[selfcheck]   selectors={found}")
                if not (found["#id"] and found["#pw"] and submit_hit):
                    log("[selfcheck] FAIL: the Naver login form selectors are missing")
                    rc = 1
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass

        if "tkinter" in sys.modules:
            log("[selfcheck] FAIL: something on the server path imported tkinter")
            rc = 1
        else:
            log("[selfcheck] no tkinter anywhere in the server run's import graph")

        import requests
        r = requests.post(config.WORKS_API, json={
            "customerId": config.CUSTOMER_ID,
            "source": config.REMOTE_SOURCE,
            "text": f"[server_selfcheck] egress={ip}/{country} latest_post={latest} "
                    f"detect_real={live_real} detect_future={live_future}",
        }, timeout=15)
        log(f"[selfcheck] artifacts POST -> {r.status_code} {r.text[:200]}")
        if r.status_code != 200 or not r.json().get("data", {}).get("matched"):
            log("[selfcheck] FAIL: the Artifacts upload was not matched to the customer")
            rc = 1
    except Exception as e:
        log(f"[selfcheck] FAIL: {redact(str(e))}")
        rc = 1
    finally:
        tunnel.stop()
    log(f"[selfcheck] {'PASS' if rc == 0 else 'FAIL'}")
    return rc


# --------------------------------------------------------------------------- commands
def cmd_start(args):
    pid = read_pid()
    if pid_alive(pid):
        print(f"already running (pid {pid}). Stop it first: server_run.py stop")
        return 1
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)

    creds = {"id": os.environ.get("EZLOAN_NAVER_ID", "").strip(),
             "pw": os.environ.get("EZLOAN_NAVER_PW", "")}
    if getattr(args, "resume", False):
        if not (RUN_DIR / "session.json").exists():
            print("--resume: no saved session, a credentialed start is needed")
            return 2
    elif not args.dry_run and (not creds["id"] or not creds["pw"]):
        print("EZLOAN_NAVER_ID / EZLOAN_NAVER_PW are required for a live run.\n"
              "  EZLOAN_NAVER_ID='...' EZLOAN_NAVER_PW='...' server_run.py start\n"
              "(or use --dry-run to exercise everything except the login)")
        return 2

    if args.foreground:
        add_redaction(creds["id"], creds["pw"])
        open_log()
        return _run_child(args, creds)

    # Daemonise. The child gets a scrubbed environment and receives the credentials over a
    # pipe, so the long-lived process never has them in /proc/<pid>/environ.
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("EZLOAN_NAVER_ID", "EZLOAN_NAVER_PW")}
    child_env["EZLOAN_APP_DIR"] = os.environ["EZLOAN_APP_DIR"]
    child_env["EZLOAN_CHROME_PROFILE_DIR"] = os.environ["EZLOAN_CHROME_PROFILE_DIR"]
    child_env["EZLOAN_REMOTE_SOURCE"] = os.environ["EZLOAN_REMOTE_SOURCE"]
    argv = [sys.executable, str(Path(__file__).resolve()), "_child",
            "--ssh-host", args.ssh_host, "--socks-port", str(args.socks_port),
            "--expect-ip", args.expect_ip, "--expect-country", args.expect_country,
            "--guard-interval", str(args.guard_interval),
            "--captcha-timeout", str(args.captcha_timeout)]
    if args.loop_host:
        argv += ["--loop-host", args.loop_host,
                 "--loop-expect-ip", args.loop_expect_ip,
                 "--loop-dir", args.loop_dir]
        if args.loop_tick:
            argv += ["--loop-tick", str(args.loop_tick)]
        if args.loop_window:
            argv += ["--loop-window", str(args.loop_window)]
    if args.dry_run:
        argv += ["--dry-run"]
        if args.dry_seconds:
            argv += ["--dry-seconds", str(args.dry_seconds)]
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    # Only tail what THIS run writes, not whatever the previous run left in the file.
    try:
        seen = len(LOG_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        seen = 0
    out = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(argv, env=child_env, stdin=subprocess.PIPE,
                            stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        proc.stdin.write((creds["id"] + "\n" + creds["pw"] + "\n").encode())
        proc.stdin.flush()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    print(f"started (pid {proc.pid}); log: {LOG_FILE}")
    print("waiting for the egress preflight...")
    deadline = time.time() + 90
    start_offset = seen
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        fresh = text[start_offset:]
        for line in text[seen:].splitlines():
            if "[egress]" in line or "[fatal]" in line or "[run]" in line:
                print("  " + line)
        seen = len(text)
        if "[egress] VERIFIED" in fresh or "[fatal]" in fresh:
            break
        time.sleep(1)
    if not pid_alive(proc.pid):
        print(f"the run exited during startup; see {LOG_FILE}")
        return 3
    print(f"\nSTOP IT WITH:  python3 {Path(__file__).resolve()} stop")
    return 0


def cmd_stop(args):
    pid = read_pid()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    AUTORESTART_FILE.unlink(missing_ok=True)
    STOP_FILE.write_text(f"stop requested {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
    if not pid_alive(pid):
        STOP_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        print("not running (nothing to stop). The customer's own copy is free to run.")
        return 0
    print(f"stopping pid {pid} ...")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"SIGTERM failed: {e}")
    deadline = time.time() + args.stop_timeout
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.5)
    if pid_alive(pid):
        print(f"still alive after {args.stop_timeout}s, sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        time.sleep(1)
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)
    print("=" * 72)
    print("  SERVER-SIDE EZLOAN RUN IS STOPPED (customer 5136338).")
    print("  The ezloan session is free. The customer may restart their own PC copy.")
    print("=" * 72)
    return 0


def cmd_status(args):
    pid = read_pid()
    alive = pid_alive(pid)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    print(f"running   : {alive}" + (f" (pid {pid})" if alive else ""))
    print(f"run dir   : {RUN_DIR}")
    print(f"log       : {LOG_FILE}")
    if state:
        started = state.get("startedAt")
        print(f"egress    : {state.get('egressIp')} ({state.get('egressCountry')}) "
              f"via {state.get('sshHost')}:{state.get('socksPort')}")
        print(f"host ip   : {state.get('hostIp')}")
        print(f"loop on   : {state.get('loopHost') or '(this host, via the tunnel)'}"
              + (f"  pinned {state.get('loopExpectIp')}" if state.get('loopExpectIp') else "")
              + (f"  tick={state.get('loopTick')}" if state.get('loopTick') else "")
              + (f" window={state.get('loopWindow')}" if state.get('loopWindow') else ""))
        print(f"dry run   : {state.get('dryRun')}")
        if started:
            print(f"started   : {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(started))} "
                  f"({(time.time() - started) / 60:.0f} min ago)")
    if alive:
        print(f"\nstop with : python3 {Path(__file__).resolve()} stop")
    return 0 if alive else 1


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--ssh-host", default=DEFAULT_SSH_HOST,
                        help="KR host for the dedicated ssh -D SOCKS tunnel")
        sp.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
        sp.add_argument("--expect-ip", default=DEFAULT_EXPECT_IP,
                        help="pin the egress address; empty string disables the pin")
        sp.add_argument("--expect-country", default="KR")
        sp.add_argument("--guard-interval", type=float, default=60.0)
        sp.add_argument("--captcha-timeout", type=int, default=900)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--dry-seconds", type=float, default=0.0)
        # --- run the hot loop ON the Korean node instead of tunnelling to it -------
        sp.add_argument("--loop-host", default=os.getenv("EZLOAN_LOOP_HOST", ""),
                        help="run the Registrar loop on this ssh host (it must already BE "
                             "in Korea). Empty = run it here through the tunnel, which is "
                             "~96ms/post slower. The Naver login always stays here.")
        sp.add_argument("--loop-expect-ip", default=os.getenv("EZLOAN_LOOP_EXPECT_IP", ""),
                        help="the loop host's OWN outbound address, pinned. Required with "
                             "--loop-host: the remote refuses to start without it.")
        sp.add_argument("--loop-dir", default=os.getenv("EZLOAN_LOOP_DIR", "~/ezloan-loop"),
                        help="where the loop code is deployed on the loop host")
        sp.add_argument("--loop-tick", type=float,
                        default=float(os.getenv("EZLOAN_LOOP_TICK", "0") or 0),
                        help="FRONTIER_POLL_SECONDS on the loop host (0 = leave default)")
        sp.add_argument("--loop-window", type=int,
                        default=int(os.getenv("EZLOAN_LOOP_WINDOW", "0") or 0),
                        help="PROBE_WINDOW on the loop host (0 = leave default)")
        return sp

    sp = common(sub.add_parser("start"))
    sp.add_argument("--foreground", action="store_true")
    sp.add_argument("--resume", action="store_true",
                    help="restart from the saved ezloan session without credentials "
                         "(used by scripts/server-watchdog.sh); exits if the session is dead")
    common(sub.add_parser("_child"))
    common(sub.add_parser("selfcheck")).add_argument(
        "--browser", action="store_true",
        help="also launch the headless Chrome the login uses and prove ITS egress is KR "
             "(downloads Chrome for Testing on first use; never visits Naver)")
    sp = sub.add_parser("stop")
    sp.add_argument("--stop-timeout", type=float, default=60.0)
    sub.add_parser("status")
    return p


def main():
    args = build_parser().parse_args()
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "selfcheck":
        return cmd_selfcheck(args)
    if args.cmd == "_child":
        creds = {"id": "", "pw": ""}
        if not args.dry_run:
            try:
                creds["id"] = (sys.stdin.readline() or "").rstrip("\n")
                creds["pw"] = (sys.stdin.readline() or "").rstrip("\n")
            except Exception:
                pass
            try:
                sys.stdin.close()
            except Exception:
                pass
        add_redaction(creds["id"], creds["pw"])
        # stdout of this process is run.log (the parent redirected it), so no tee here.
        return _run_child(args, creds)
    return 2


if __name__ == "__main__":
    sys.exit(main())
