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

## v2.4.7 (2026-07-21) — log 배너잔여(amount) to answer "배너가 안 올라감" in one glance
Customer report: "프로그램이 배너가 자꾸 안올라가더라구요" + "재시작하면 한개 올라가더니 그 담부턴 또 안됨".
LIVE diagnosis (KR egress unicorn@external-8, do NOT log into their Naver — locks the account):
- App is HEALTHY and CAUGHT UP. Live ezloan latest /rq post id == app frontier (30511, then 30512
  as a new post appeared during the check and the app detected+advanced instantly). NOT frontier-stuck,
  NOT a runaway, session healthy (11 cookies incl ezloan_sess, login_success). Frontier advanced
  30503->30512 over the day, tracking new posts in real time. So (a) stuck / (b) app-bug are RULED OUT.
- Real cause: ezloan REFUSES rq_addbanner with "no permission" on ~most new posts. Live: advertiser 585
  (더원대부) present on only 4 of 22 recent posts (30490,91,92,08), ABSENT on the 3 newest (30509/10/11).
  Slots are NOT full (14-24 banners/post, room remains) so it's a refusal, not a race loss.
  Log: 새글연속거부 climbed to 14 on 30503-07, one success 30508, then 1-2 on 30509-10. Sporadic success.
- Per the source (probe_state/register comments, memory ezloan-registration-error-semantics): "no
  permission" reflects the ACCOUNT's paid-ad/실시간 배너 잔여 state (rq_addbanner_check checks account
  entitlement, NOT post existence; the SAME cookie flips success->no_permission as credits deplete).
  The behavioral signature (refused on most/newest posts, occasional success, healthy session) points
  to the real-time banner credits being LOW / intermittently exhausted. Their paid ad was extended to
  D-35 on 2026-07-10; ~11 days on, credits may be draining. BUT: v2.4.4 does NOT log the check 'amount'
  (배너 잔여 개수), so the exact balance was not observable — that is why this kept getting re-diagnosed.
- The "재시작하면 한개 올라감 then 안됨" pattern is explained: each restart resets _no_perm_warned/streak;
  after restart the first eligible post registers (streak 0), then refusals resume. It is NOT the loop
  stopping after one — frontier keeps advancing; ezloan just keeps refusing.

FIX (v2.4.7): thread rq_addbanner_check's `amount` (remaining 실시간 배너 잔여) into the register result
and surface it on EVERY cycle summary (배너잔여=N) and every register_no_permission / no_permission_persistent
line (마지막배너잔여=N). No behavior change to the register/frontier loop. Now the very next run of the
customer's app answers definitively: no_permission + 배너잔여=0 => credits exhausted, customer must
renew/충전 the 실시간 배너 상품 (account-side, not our bug); no_permission + healthy 배너잔여 => routine
per-post skip. CI: verify_247.py asserts amount threads through even on a no_permission refusal.

OPEN ITEM for next turn: once the customer runs v2.4.7 (or if we can get an authed check), read
배너잔여 from the log. If 0 -> tell customer to recharge/extend the 실시간 배너 상품 on ezloan (that is
the confirmed customer action). Until then the honest line to the customer: app + login are working
and detecting every new post instantly; ezloan is refusing registration ("등록 대상 아님") on most posts,
which is an account/광고상품 잔여 signal, and v2.4.7 will show the exact remaining count next run.
DO NOT flatly assert "your account lapsed" without the 배너잔여 number (that misdiagnosis burned trust twice).
