# -*- coding: utf-8 -*-
"""24/7 게이팅 해제 검증: RUN_WINDOW_ENABLED=False 이면 어떤 시각에도 대기하지 않는다."""
import sys
from datetime import datetime, timedelta, timezone

import config
import ezloan_bot as eb

KST = timezone(timedelta(hours=9))

assert config.RUN_WINDOW_ENABLED is False, "RUN_WINDOW_ENABLED must be False"
assert config.APP_VERSION == "2.4.2", f"version bump missing: {config.APP_VERSION}"

# 1) in_run_window() 은 하루 24시간 어느 시각에도 True 여야 한다.
bad = []
for h in range(24):
    dt = datetime(2026, 7, 10, h, 30, tzinfo=KST)
    if not eb.in_run_window(dt):
        bad.append(h)
assert not bad, f"in_run_window returned False at hours {bad} with window disabled"
print("[OK] in_run_window() True for all 24 hours (incl. 3am):",
      all(eb.in_run_window(datetime(2026, 7, 10, h, 0, tzinfo=KST)) for h in (0, 3, 7, 12, 23)))

# 2) _idle_outside_window 은 어떤 시각에도 즉시 True 를 돌려주고 절대 대기(_wait)하지 않는다.
class FakeBot:
    should_stop = staticmethod(lambda: False)
    log = staticmethod(lambda *a, **k: None)
    remote = staticmethod(lambda *a, **k: None)
    def _wait(self, s):
        raise AssertionError(f"_idle_outside_window slept {s}s -> time gating still active!")

_idle = eb.Registrar._idle_outside_window
_label = eb.Registrar._window_label
fb = FakeBot()

# now_kst 를 3am 으로 몽키패치해서 '운영 시간대 밖'이었던 시각을 강제.
orig_now = eb.now_kst
for test_h in (3, 1, 6, 0, 23):
    eb.now_kst = lambda h=test_h: datetime(2026, 7, 10, h, 15, tzinfo=KST)
    res = _idle(fb)
    assert res is True, f"_idle_outside_window returned {res} at {test_h}:00 (should run immediately)"
eb.now_kst = orig_now
print("[OK] _idle_outside_window returns True immediately (no _wait) at 3am/1am/6am/0am/23pm")

# 3) 창 라벨은 24시간으로 표기.
assert _label(fb) == "24시간", _label(fb)
print("[OK] window label = 24시간")

print("\nALL CHECKS PASSED: loop runs 24/7, never idles on time-of-day. Only 정지 stops it.")
