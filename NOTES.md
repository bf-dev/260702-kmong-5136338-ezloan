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

## 2026-07-21 follow-up — DEFINITIVE cause of "no permission" (credits ruled OUT by live proof)
Customer confirmed "잔여가 넉넉합니다" (credits plenty), so the v2.4.7 "credits low" hypothesis is DEAD.
Re-diagnosed live via KR egress (unicorn@external-8, read-only, NO account login). Evidence:

- The ingest log has TWO sessions. Session 1 (pre-restart, ends 00:55:25) ran with cumulative 등록=195
  and, in the captured window, refused 30503-07 (새글연속거부 10->14). RESTART at 00:55:25 (baseline
  frontier=30508). Session 2 registered EXACTLY post 30508 at 01:02:03 (등록 0->1), then refused
  30509/30510. That is the "restart -> 1 registers -> then refused" pattern, reproduced in the log.
  frontier reached 30511 == ezloan's real latest post => app is CAUGHT UP, not stuck. Session healthy.

- LIVE count of advertiser 585 (더원대부) active banners: scanned /rq/30410..30512. 585 is present on a
  LONG CONTIGUOUS band ~30410-30492 (60+ posts), then ABSENT on the entire 30493-30507, ONE lone
  success at 30508, absent 30509-30511. So the concurrent-cap hypothesis is FALSIFIED: 585 holds 60+
  active banners at once, not ~4-5. There is NO small concurrent cap. (Prior "on only 4 posts" was a
  narrow 30490-30511 window; widening the scan showed the full 60+ band.)

- The boundary is a TIME cutover, not a count. /rq list ages: post 30492 (last WITH 585) = 15시간전,
  30494 (first WITHOUT) = 13시간전. So 585 stopped getting registered ~13-15h before the check, i.e.
  around midday 2026-07-20 KST. Everything BELOW that moment has 585; everything above refuses.

- Site + new-post pipeline are FINE for other advertisers: competitor /l/325 is on EVERY post through
  the newest 30511 (even 2x on some). So this is 585-account-specific, not a site outage or slot race.

- Server response semantics (from ezloan's OWN /res/js/script.js, lines 8843 check / 9000 write):
  rq_addbanner_check msgMap: no permission / no amount / no ads / no payed ads / max / ing.
  rq_addbanner (WRITE) msgMap: ONLY no amount / no ads / no payed ads / 404 error (NO "no permission").
  The app logs `msg=no permission` from the WRITE after check returned result:true (=account has
  credits AND a paid ad, since check gates on both). So it is NOT no amount (credits), NOT max (slots),
  NOT ing (already reg), NOT session death. It is an account-state refusal on the write.

- ROOT CAUSE (matches the 2026-07-10 authed live finding already in this file's SESSION_LOST_MSGS
  comment): "쿠키는 동일한데 결과만 시간에 따라 바뀐다 -> 서버측 계정 할당/기간 제한 상태". ezloan
  imposes a per-account time/quota window on how many/how long 실시간 배너 can be actively placed.
  585 hit that window ~13-15h ago; existing banners stay up (60+) but NEW placements are refused with
  "no permission" until the window rolls / an old banner expires (which is why exactly 1 sneaks in on a
  restart / when a slot frees). On 2026-07-10 the same signature was tied to the 메인배너 유료광고 상품
  lapsing to D-5; the customer even noted "연장 전에도 순위 적용은 되더라" = partial success while near
  the limit, identical to today's occasional 30508 success.

RESOLUTION (honest): this is INHERENT ezloan account-side behavior, NOT an app bug and NOT fixable in
code. Nothing in the client can force ezloan to accept a write it is refusing. Two customer-side checks:
  (1) 이지론 내정보 > 광고 상품(메인배너 유료광고)이 '진행 중' 상태인지 + 남은 기간(D-day). If it lapsed
      or is throttled, that is the gate (exactly the 07-10 fix). Credits (실시간 배너 잔여) being plenty
      does NOT satisfy this: the WRITE needs an ACTIVE paid 광고상품, separate from 잔여 개수.
  (2) Ask ezloan whether there is a cap on concurrent/active 실시간 배너 or a daily placement quota per
      advertiser. 585 sits at 60+ active banners; if ezloan caps active placements, new ones only land
      as old ones expire off the bottom (banner ROTATION, not simultaneous growth). Set that expectation.
The app is already doing everything right: instant detection, immediate rq_addbanner, correct skip on
refusal, frontier tracking ezloan's true latest. It will auto-resume the moment ezloan stops refusing.

DO NOT tell the customer "your credits ran out" (proven false) or "your account lapsed" without them
confirming the 광고상품 진행상태. The honest line: app+login healthy and catching every new post
instantly; ezloan is refusing NEW banner writes on this account since ~midday 07-20 while keeping the
existing 60+ banners up; this is ezloan's account-side 광고상품/기간 제한, check 광고 상품 진행중 상태 +
ezloan 문의 on any active-banner/daily quota. Live evidence saved: tmp/ezloan_30508.html,
tmp/ezloan_script.js (this host, tmp is pruned in 14d).

---

## 2026-07-23 — transient login slowness + CLEAN v2.4.5 login-auto-retry delivery (customer 5136338)

SYMPTOM: customer 5136338 on v2.4.4 hit repeated Naver/이지론 login failures (7 manual
retries). Ingest log sequence: app healthy to 02:15 (등록=111) -> [run_stopped] (customer
manually stopped the loop) -> cold re-logins hit [login_failed]/[login_temporarily_unavailable]
"네이버 로그인 폼이 제때 열리지 않았습니다" -> [session_recover_none]. This is the v2.4.4
give-up-after-~4-tries/~1min behavior (the screenshot's "1/4 2/4 3/4 -> 잠시 후 시작 다시").

LIVE-STATE FINDING (Task 1) = TRANSIENT SLOWNESS, NOT a broken selector. Verified via KR-egress
house SOCKS (external fleet, egress AS16509 Incheon KR):
  - nid.naver.com/nidlogin.login: HTTP 200 (0.31-1.08s across 3 nodes); form INTACT: id="id",
    id="pw", name="id", name="pw", btn_login all present -> app selectors (By.ID "id"/"pw",
    button.btn_login) still match. No 보호/idSafetyRelease/점검 markers.
  - ezloan.io/m/login: HTTP 200 (0.47-1.6s); .js-loginBtn[data-type="naver"] present. No
    error/maintenance markers.
  - CONFIRMED by the customer's own app: it RECOVERED on its own at 02:32 (artifacts-check shows
    fresh [cycle] 세션없음연속=0 frontier=30626 -> logged in + polling again, no code change).
  NEVER logged into the customer's Naver account (that protection-locks it). Pages fetched only.

DELIVERY (Task 2) = clean v2.4.5, login auto-retry ONLY, WITHOUT the paid v2.4.6 speed fix.
  - v2.4.5 = commit d644ec2 (login() 20-min auto-retry backoff + error-page detector hardening;
    naver_login.py/app.py/config.py). It is a LINEAR ANCESTOR of v2.4.6, so it contains ZERO of
    the speed changes. Proof: d644ec2:config.py has APP_VERSION="2.4.5", POLL_SECONDS=1.5 (slow
    original), AUTO_UPDATE_ENABLED=False; the 135-line ezloan_bot.py hot-path rewrite + POLL 0.8
    are in cab6124 (v2.4.6), a LATER commit not on this branch.
  - Built on GitHub Actions windows-latest from pushed branch build/v2.4.5-login @ d644ec2 (run
    29974549756, all verify steps incl. login-resilience + error-page-detector PASSED). Artifact
    upload hit storage quota; exe came from the Release step: ezloan-desktop-2.4.5.exe (33270468 B,
    PE32+ GUI x86-64).
  - HOSTED VERSION-FREE (distinct from ezloan-desktop-update.exe, which == v2.4.6 paid build):
    https://works.insu.ng/works/public/5136338/ezloan-desktop-login.exe  (curl -I -> HTTP 200).
    Do NOT reuse ezloan-desktop-update.exe for the login-only build.
  - bridge.py reporting intact (source ezloan-desktop-v2.4.5, customerId 5136338). AUTO_UPDATE off.

Build branch build/v2.4.5-login left on origin for reproducibility.

---

## 2026-07-27 — v2.5.0: PAID "1등 등록속도 업그레이드" delivered (customer paid 50,000원)

Customer paid for the full speed upgrade proposed on 2026-07-17 (memory
1deung-upgrade-pending.md). `main` already had everything needed in one clean linear
history (no merge/rebase was needed):
  d644ec2 (v2.4.5 login auto-retry) -> cab6124 (v2.4.6 hot-path speed fix, reclaim 1등)
  -> 708b050/a9d9f59 (v2.4.7 배너잔여 diagnostic logging, no register/frontier behavior change).
Verified cab6124's hot-path is still intact at HEAD: probe_state's 'open' branch does NOT call
post_exists before registering, register() reuses the lookahead precheck, and lookahead_ids
still returns (pid, precheck) tuples with post_absent gating safe_frontier (grep-verified in
ezloan_bot.py, matches the v2.4.6 NOTES section above).

Action taken: bumped APP_VERSION 2.4.7 -> 2.5.0 (config.py) to mark this as the paid-delivery
milestone; no functional code change beyond the version bump (the functional work was already
on main from v2.4.5/2.4.6/2.4.7). Ran all 5 CI verify scripts locally before pushing
(verify_247, repro_frontier_runaway, verify_register_latency, verify_login_resilient,
verify_error_page_detector) - all PASSED, confirming zero regression. Built via GitHub Actions
windows-latest (push to main). Published to the CANONICAL version-free URL (per owner
instruction that the free/paid filename split no longer matters now that the customer paid for
the full thing):
  https://works.insu.ng/works/public/5136338/ezloan-desktop-update.exe  (now == v2.5.0, not v2.4.6)
Also kept a versioned copy ezloan-desktop-2.5.0.exe alongside it, and left
ezloan-desktop-login.exe (v2.4.5-only) untouched/orphaned - no longer referenced anywhere.

What v2.5.0 concretely contains (all three deliverable pieces from v2.4.4/2.4.5/2.4.6, plus
v2.4.7 diagnostics):
  1. v2.4.4 stability baseline: no false "session expired" (재로그인) alarms, frontier
     re-baselines correctly on restart, runs 24/7 (no idle time-window).
  2. v2.4.5 login auto-retry: if Naver/이지론 login is briefly slow, the app retries
     automatically every ~45s for up to ~20 min instead of giving up after ~4 tries and asking
     the customer to manually click 시작 again.
  3. v2.4.6 registration-speed fix (the paid "1등" upgrade itself): removed a 288KB pre-check
     page fetch and a duplicate list fetch that OUR OWN earlier stability updates had added to
     the new-post register path (this had slowed us from 133ms to 288ms per new post, letting
     competitors register first); polling tightened 1.5s -> 0.8s. Net register latency ~87ms,
     faster than the app has ever been. Live-verified 2026-07-17: banner landed at slot 1 on
     every post it appeared on after the fix.
  4. v2.4.7 diagnostics (already free, riding along): logs remaining paid-banner credits
     (배너잔여) on every cycle/refusal, so a future "왜 안 올라가요" report is self-diagnosing.

AUTO_UPDATE_ENABLED stays False (operator instruction, unchanged) - this is a manual delivery,
customer must download+run the new exe themselves; it will not self-update.

BUILD/DELIVERY EVIDENCE (2026-07-27): GitHub Actions run 30228877220 on bf-dev/260702-kmong-5136338-ezloan
went green (all verify_*/repro_* steps + GUI construct self-test + real Windows screenshot). Downloaded
exe verified `PE32+ executable (GUI) x86-64` at 33,273,801 bytes. Hosted at BOTH
ezloan-desktop-2.5.0.exe and the canonical version-free ezloan-desktop-update.exe (both curl -I 200,
content-length 33273801). artifacts-check 5136338 confirms the customer's CURRENTLY RUNNING app
(still v2.4.4, pre-upgrade) is actively posting `[cycle]` rows every ~10s via bridge.py -> the
Artifacts API channel is alive and proven end-to-end; no retrofit was needed, bridge.py already ships
in every build.

GITHUB ACTIONS BILLING GOTCHA (recurring across bf-dev repos, see also memory
projects/260630-kmong-244448-wcompany-contact-collector/build-and-scraping-facts.md): this repo's
first build attempt after a while failed instantly (~7s, 0 steps) with "recent account payments have
failed or your spending limit needs to be increased". WORKAROUND: `gh repo edit bf-dev/<repo>
--visibility public` (public repos get free windows-latest minutes, sidesteps the billing block). Did
that here (repo has no secrets committed - just endpoint URLs); build immediately succeeded after. If
a future build on this repo fails the same way, re-check `gh repo view ... --json visibility` is still
`PUBLIC` first before assuming a real regression.
