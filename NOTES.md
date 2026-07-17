# ezloan-desktop — engineer notes (customer 5136338, 더원대부)

Windows (Python + PyInstaller) auto-registration tool. Logs into ezloan.io via Naver SSO
and auto-registers the customer's paid banner onto new loan-request posts (/rq, rq_addbanner).
Reports logs to the Artifacts API via bridge.py, keyed customerKey=5136338.

## Build / deliver
- CI: `.github/workflows/build.yml` on GitHub Actions `windows-latest` (repo bf-dev/260702-kmong-5136338-ezloan).
  Push to `main` builds + runs verify scripts + builds exe + GUI construct self-test + publishes to Release `latest`.
- Artifact upload usually FAILS (GitHub storage quota) — the exe is published to the Release
  instead (`gh release download latest -p "ezloan-desktop-<ver>.exe"`). This is expected; the run still shows success.
- AUTO_UPDATE_ENABLED = False (operator instruction). App does NOT self-update. Every build is delivered MANUALLY.
- Deliver as a VERSION-FREE link (message linter blocks version numbers in chat text):
  host at `/home/bfdev/neoworks/apps/gateway/artifacts/public/5136338/ezloan-desktop-update.exe`
  -> https://works.insu.ng/works/public/5136338/ezloan-desktop-update.exe (curl -I must be 200 before sending).
  Also keep a versioned copy `ezloan-desktop-<ver>.exe` next to it.

## How banner rank works (verified live 2026-07-17 via KR egress unicorn@external-8)
- Banner slot order on /rq/{id} is FIRST-COME-FIRST-SERVED: the advertiser whose rq_addbanner
  fires first sits at slot 1 (1등); everyone after stacks below. NOT paid-priority, NOT id-order
  (confirmed: advertiser 585 was slot1 while 596 was slot6). So rank 1 == win the registration race.
- Real banner list = `<a href="/l/{id}" class="item"><div class="name">상호</div>` entries, 1-9 per post.
- The `[registered] rank=N` log BEFORE v2.4.6 counted ALL page <li> (nav/footer) and was a constant
  ~147/148, decoupled from the real banner slot. company_rank() was fixed in v2.4.6 to count only real
  banner items -> the log rank now IS the true slot (1등/2등). Do not read old ~147 logs as a real rank.

## v2.4.6 (2026-07-17) — reclaim rank 1: cut new-post register latency
Root cause of customer's "recently only 2등": v2.4.x stability changes ADDED latency to the
new-post -> rq_addbanner hot path, so a competitor's bot could register first.
- v2.4.2 added post_exists(/rq/{id}, ~288KB) BEFORE registering an open post.
- v2.4.3 added a 2nd full /rq list fetch per cycle.
- Measured live (KR egress): fresh-post register path 133ms (v2.4.1) -> 288ms (v2.4.5) -> 87ms (v2.4.6 lean).
Fix (all v2.4.x correctness preserved; frontier-runaway + login-resilience repros still pass):
- probe_state returns "open" WITHOUT post_exists; register fires rq_addbanner immediately and
  REUSES the lookahead precheck (register(..., precheck=(code,data))) so no redundant rq_addbanner_check.
- Frontier safety now derives from the register RESULT: _handle(pid, precheck) returns exists-bool;
  post_absent (phantom future id) => False => main loop does NOT advance frontier past it (no runaway).
  lookahead_ids now returns [(pid, precheck), ...] and does NOT push safe_frontier past an unconfirmed open.
- One /rq list fetch per cycle (reused for frontier-resync + safety net), not two.
- POLL_SECONDS 1.5 -> 0.8 (cycle is much lighter now; site load stays below prior).
- CI locks it: verify_register_latency.py.

## Ceiling / honesty
Rank 1 depends on out-registering competing broker bots on the SAME post. We removed our
self-inflicted ~200ms and halved detection lag, so we now register as fast as a polling client
reasonably can. If a competitor uses a faster poller or a webhook/push, they could still
occasionally beat us on a given post — polling cannot guarantee 100% slot 1. In the live check
(2026-07-17, posts 30290-30317) our banner was already slot 1 on every post it was on, so in
practice we should be at/near 100% again after this speed fix.

## Hard-won gotchas (do NOT repeat)
- ezloan.io 403s non-Korean IPs. ANY live check must go through a KR egress: `unicorn@external-8`
  (KR host, has the Kmong SOCKS egress) is the reliable one. The kmong-egress CF Worker does NOT
  allowlist ezloan.io ("target host not allowed") — do not use it for ezloan.
- "404 error"/"no permission" from rq_addbanner are per-post ineligibility, NOT session death.
  logged_in()==False is the ONLY session-death authority. See memory ezloan-registration-error-semantics.
- Never overwrite an already-served exe filename on the static host (Cloudflare edge cache serves
  stale bytes -> restart loop). Versioned names only for anything auto-update might fetch.
