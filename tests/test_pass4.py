# -*- coding: utf-8 -*-
"""تست‌های پاس ۴ — پوشش ماژول‌های کم‌تست: approval / sandbox / ratelimit /
scheduler / webhooks / notifications + رگرسیون فیکس next_run زمان‌بند.

اجرا:  python tests/test_pass4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from approval import ApprovalManager  # noqa: E402
from notifications import NotificationCenter  # noqa: E402
from ratelimit import RateLimiter, harden_github_tools, is_transient, with_retry  # noqa: E402
from sandbox import run_in_sandbox  # noqa: E402
from scheduler import Scheduler, parse_spec  # noqa: E402
from webhooks import WebhookWatcher  # noqa: E402


# ── Approval ─────────────────────────────────────────────────────────

def test_approval_flow(tmp: Path) -> None:
    ap = ApprovalManager(tmp / "ap.json")
    it = ap.request("delete_repo", {"repo": "x/y"}, project=3)
    assert it["id"] == "ap-1" and it["status"] == "pending"
    assert ap.pending(project=3) and not ap.pending(project=9)  # ایزوله per-project
    assert ap.request("run_shell", {"argv": ["ls"]})["id"] == "ap-2"  # بدون پروژه → همه‌جا دیده می‌شود
    assert ap.pending(project=9) and ap.pending(project=3)
    # تصمیم غیرهمزمان در نخ دیگر
    threading.Timer(0.2, lambda: ap.resolve(it["id"], True)).start()
    r = ap.wait(it["id"], timeout_s=5)
    assert r["status"] == "approved" and r["ok"] and r["item"]["resolved_by"] == "user"
    # دوباره تعیین تکلیف → خطا
    assert not ap.resolve(it["id"], False)["ok"]
    # رد
    it2 = ap.request("fs_delete", {"path": "a"})
    threading.Timer(0.1, lambda: ap.resolve(it2["id"], False)).start()
    assert ap.wait(it2["id"], timeout_s=5)["status"] == "rejected"
    # آیدی ناموجود
    assert ap.wait("ap-999", timeout_s=1)["status"] == "missing"
    print("ok: approval request→wait→resolve approve/reject/missing")


def test_approval_timeout_and_history(tmp: Path) -> None:
    ap = ApprovalManager(tmp / "ap.json")
    it = ap.request("run_shell", {"argv": ["ls"]})
    t0 = time.monotonic()
    r = ap.wait(it["id"], timeout_s=1)
    assert r["status"] == "timeout" and not r["ok"] and time.monotonic() - t0 < 3
    assert it["status"] == "timeout"
    h = ap.history(limit=10)
    assert h and h[0]["id"] == it["id"]
    print("ok: approval timeout (TTL) + history")


def test_approval_gc_and_reload(tmp: Path) -> None:
    ap = ApprovalManager(tmp / "ap.json")
    # ۲۵۰ آیتم: ۲۲۰ تکلیف‌شده + ۳۰ pending → gc باید فقط ۱۰۰ تکلیف‌شدهٔ آخر را نگه دارد
    for i in range(250):
        st = "pending" if i >= 220 else "approved"
        ap._items[f"ap-{i + 1}"] = {"id": f"ap-{i + 1}", "ts": "t", "tool": "t", "arguments": {},
                                    "project": None, "status": st, "resolved_ts": "t" if st != "pending" else None,
                                    "resolved_by": "u" if st != "pending" else None}
    ap.request("x", {})  # gc را صدا می‌زند (خودش یک pending تازه می‌سازد)
    resolved = [v for v in ap._items.values() if v["status"] != "pending"]
    pending = [v for v in ap._items.values() if v["status"] == "pending"]
    assert len(resolved) == 100 and len(pending) == 31, (len(resolved), len(pending))
    # ماندگاری
    ap2 = ApprovalManager(tmp / "ap.json")
    assert len(ap2._items) == len(ap._items) and ap2.pending()
    print("ok: approval gc (keeps last 100 resolved) + disk reload")


# ── Sandbox ──────────────────────────────────────────────────────────

def test_sandbox_runs(tmp: Path) -> None:
    r = run_in_sandbox(["python3", "-I", "-c", "print(2+3)"], workdir=tmp)
    assert r["ok"] and r["exit"] == 0 and r["stdout"].strip() == "5", r
    # محیط سفیدلیستی: توکن والد به فرزند نشت نمی‌کند
    os.environ["GH_TOKEN"] = "ghp_secret_for_test"
    try:
        r2 = run_in_sandbox(["python3", "-I", "-c", "import os; print('GH_TOKEN' in os.environ)"], workdir=tmp)
        assert r2["ok"] and r2["stdout"].strip() == "False", r2
    finally:
        del os.environ["GH_TOKEN"]
    # فرمان ناموجود
    r3 = run_in_sandbox(["no-such-cmd-xyz"], workdir=tmp)
    assert not r3["ok"] and "یافت نشد" in r3["error"], r3
    print("ok: sandbox run + env whitelist (no token leak) + missing cmd")


def test_sandbox_timeout_and_truncation(tmp: Path) -> None:
    r = run_in_sandbox(["python3", "-I", "-c", "import time; time.sleep(3)"], workdir=tmp, timeout=1)
    assert not r["ok"] and "تایم‌اوت" in r["error"] and r.get("timeout") == 1, r
    r2 = run_in_sandbox(["python3", "-I", "-c", "print('A'*200)"], workdir=tmp, max_output=10)
    assert r2["ok"] and len(r2["stdout"]) < 60 and "کوتاه‌شد" in r2["stdout"], r2["stdout"][:60]
    print("ok: sandbox hard timeout (killpg) + output truncation")


# ── Rate limit / retry ───────────────────────────────────────────────

def test_ratelimiter_and_retry() -> None:
    rl = RateLimiter(per_min=6000, burst=10)
    t0 = time.monotonic()
    for _ in range(10):
        rl.acquire()
    assert time.monotonic() - t0 < 1.5, "۱۰ acquire با burst=10 باید فوری باشد"

    calls = {"n": 0}

    class FlakyTimeout(TimeoutError):
        """خطای گذرای شبیه شبکه — is_transient از روی نام/وضعیت تشخیص می‌دهد."""

    @with_retry(max_attempts=3, base_delay=0.01, retry_when=is_transient)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise FlakyTimeout("net hiccup")
        return "ok"

    assert flaky() == "ok" and calls["n"] == 3

    @with_retry(max_attempts=3, base_delay=0.01, retry_when=is_transient)
    def permanent() -> str:
        calls["n"] += 1
        raise ValueError("permanent")
    try:
        permanent()
        raise AssertionError("باید فوراً raise می‌شد")
    except ValueError:
        assert calls["n"] == 4, "خطای غیرگذرا نباید بازتلاش شود"
    print("ok: token bucket burst + retry (transient retried, permanent not)")


def test_is_transient_matrix() -> None:
    class HttpErr(Exception):
        def __init__(self, status, data=None):
            super().__init__("x")
            self.status = status
            self.data = data or {}

    assert is_transient(HttpErr(429)) is True
    assert is_transient(HttpErr(503)) is True
    assert is_transient(HttpErr(403, {"message": "rate limit exceeded", "documentation_url": "u"})) is True
    assert is_transient(HttpErr(403, {"message": "not found", "documentation_url": "u"})) is False  # دسترسی، نه نرخ
    assert is_transient(HttpErr(404)) is False
    assert is_transient(TimeoutError("t")) is True
    assert is_transient(ValueError("v")) is False
    print("ok: is_transient matrix (429/5xx/403-rate vs 403-perm/404/timeout)")


def test_harden_github_tools() -> None:
    class DummyGH:
        def __init__(self):
            self.calls = 0

        def list_repos(self):
            self.calls += 1
            return {"ok": True}

        def _private(self):
            return "no"

    acquired = {"n": 0}

    class CountingLimiter(RateLimiter):
        def acquire(self) -> None:
            acquired["n"] += 1
            super().acquire()

    gh = DummyGH()
    n = harden_github_tools(gh, CountingLimiter(per_min=6000, burst=50), max_attempts=2)
    assert n == 1, f"فقط متد عمومی باید قفل شود: {n}"
    assert gh.list_repos()["ok"] and acquired["n"] == 1
    assert gh._private() == "no" and acquired["n"] == 1
    print("ok: harden_github_tools wraps public methods only, limiter acquired")


# ── Scheduler ────────────────────────────────────────────────────────

def test_scheduler_parse_spec() -> None:
    assert parse_spec({"kind": "interval", "every_s": 60}) == {"kind": "interval", "every_s": 60}
    assert parse_spec({"every_minutes": 5}) == {"kind": "interval", "every_s": 300}  # پیش‌فرض interval
    assert "error" in parse_spec({"kind": "interval", "every_s": 10})       # < ۳۰ ثانیه
    assert "error" in parse_spec({"kind": "interval", "every_s": "abc"})
    assert parse_spec({"kind": "daily", "at": "9:5"}) == {"kind": "daily", "at": "09:05"}
    assert "error" in parse_spec({"kind": "daily", "at": "25:00"})
    assert "error" in parse_spec({"kind": "daily", "at": "bad"})
    assert "error" in parse_spec({"kind": "weekly"})
    print("ok: parse_spec matrix (interval/daily/defaults/errors)")


def test_scheduler_next_run_fix(tmp: Path) -> None:
    """باگ پاس ۴: job تازه (حتی daily) قبلاً ۵ ثانیه بعد می‌فایرید."""
    sch = Scheduler(tmp / "j.json", fire_callback=None)
    j = sch.add(1, "i", "t", {"kind": "interval", "every_s": 3600})["job"]
    delta = (datetime.fromisoformat(j["next_run"]) - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta <= 3600, f"اولین اجرا باید بعد از یک فاصلهٔ کامل باشد: {delta:.0f}s"

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=2)).strftime("%H:%M")
    d = sch.add(1, "d", "t", {"kind": "daily", "at": future})["job"]
    nxt = datetime.fromisoformat(d["next_run"])
    assert nxt.strftime("%H:%M") == f"{int(future.split(':')[0]):02d}:{int(future.split(':')[1]):02d}"
    assert (nxt - now).total_seconds() > 3600, "موعد آینده نباید همین حالا فایر شود"

    past = (now - timedelta(hours=2)).strftime("%H:%M")
    d2 = sch.add(1, "d2", "t", {"kind": "daily", "at": past})["job"]
    nxt2 = datetime.fromisoformat(d2["next_run"])
    assert nxt2 > now and (nxt2 - now).total_seconds() < 26 * 3600, "موعد گذشته → فردا"

    # غیرفعال/فعال: فعال‌سازی دوباره هم نباید +5s بدهد
    sch.set_enabled(j["id"], False)
    j2 = sch.set_enabled(j["id"], True)["job"]
    delta2 = (datetime.fromisoformat(j2["next_run"]) - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta2 <= 3600, f"set_enabled هم باید فاصلهٔ کامل بدهد: {delta2:.0f}s"
    print("ok: scheduler next_run — interval=full period, daily=next occurrence (no instant fire)")


def test_scheduler_due_advance_persistence(tmp: Path) -> None:
    fired = []
    sch = Scheduler(tmp / "j.json", fire_callback=lambda j: fired.append(j["id"]))
    j = sch.add(7, "n", "t", {"kind": "interval", "every_s": 60})["job"]
    # موعد گذشته → due؛ پیشروی interval
    j["next_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert sch._due(j, datetime.now(timezone.utc)) is True
    sch._advance(j, datetime.now(timezone.utc))
    assert j["run_count"] == 1 and j["last_run"]
    assert (datetime.fromisoformat(j["next_run"]) - datetime.now(timezone.utc)).total_seconds() > 50
    # daily گذشته → فردا (مقایسهٔ تاریخ دقیق — وابسته به ساعتِ اجرای تست نباشد؛ باگ پاس ۸)
    t0 = datetime.now(timezone.utc)
    d = {"kind": "daily", "at": "00:00"}
    sch._advance(d, t0)
    expected = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    if expected <= t0:
        expected += timedelta(days=1)
    assert abs((datetime.fromisoformat(d["next_run"]) - expected).total_seconds()) < 2, (
        f"rollover اشتباه: {d['next_run']} ≠ {expected.isoformat()}")
    # disabled → due=False
    j["enabled"] = False
    assert sch._due(j, datetime.now(timezone.utc)) is False
    # ماندگاری + حذف
    sch2 = Scheduler(tmp / "j.json")
    assert [x["id"] for x in sch2.list(project_id=7)] == [j["id"]]
    assert sch2.remove(j["id"])["ok"] and not sch2.list()
    assert not sch2.remove(999)["ok"]
    print("ok: scheduler due/advance/daily-rollover/disabled/persistence/remove")


# ── Webhooks ─────────────────────────────────────────────────────────

class _FakeGH:
    def __init__(self, issues=None, prs=None, commits=None, raise_=False):
        self._issues, self._prs, self._commits, self._raise = issues or [], prs or [], commits or [], raise_

    def list_issues(self, repo, state="open", limit=30):
        if self._raise:
            raise RuntimeError("net down")
        return {"issues": self._issues}

    def list_pull_requests(self, repo, state="open", limit=30):
        return {"pull_requests": self._prs}

    def list_commits(self, repo, limit=1):
        return {"commits": self._commits}


def test_webhooks_crud_and_poll(tmp: Path) -> None:
    w = WebhookWatcher(tmp / "w.json")
    assert not w.add(1, "no-slash", ["issues"])["ok"]
    a = w.add(1, "o/r", ["issues", "bogus"])["watch"]
    assert a["events"] == ["issues"], "رویداد نامعتبر باید فیلتر شود"
    assert w.list(project_id=1) and not w.list(project_id=2)
    assert w.remove(a["id"]) and not w.remove(999)

    # پول: baseline بدون اعلان؛ تغییر → change_obj؛ حذف‌شده → None؛ خطا → error
    gh = _FakeGH(issues=[{"number": 5}])
    w = WebhookWatcher(tmp / "w2.json", gh_provider=lambda: gh)
    watch = w.add(2, "o/r", ["issues", "prs", "commits"])["watch"]
    assert w.poll_once(watch) is None, "اولین سنجش فقط baseline"
    gh2 = _FakeGH(issues=[{"number": 5}, {"number": 6}], prs=[{"number": 3}], commits=[{"sha": "abcdef1234"}])
    w.gh_provider = lambda: gh2
    ch = w.poll_once(watch)
    assert ch and ch["to"]["issues_open"] == 2 and ch["to"]["commit_latest"] == "abcdef1234" and ch["from"]["issues_open"] == 1
    assert w.poll_once(watch) is None, "بدون تغییر جدید → None"
    w.remove(watch["id"])
    assert w.poll_once(watch) is None, "واچ حذف‌شده → None"
    w.gh_provider = lambda: None
    b = w.add(2, "o/r2", ["issues"])["watch"]
    assert w.poll_once(b) is None
    w.gh_provider = lambda: _FakeGH(raise_=True)
    c = w.add(2, "o/r3", ["issues"])["watch"]
    assert w.poll_once(c) == {"error": "net down"}
    print("ok: webhooks CRUD + poll baseline/change/deleted/error")


# ── Notifications ────────────────────────────────────────────────────

def test_notifications(tmp: Path) -> None:
    nc = NotificationCenter(tmp / "n.json")
    a = nc.push("watch", "عنوان", "متن", project=1)
    nc.push("schedule", "دوم")
    nc.push("system", "سوم")
    assert nc.unread_count() == 3
    assert len(nc.list(unread_only=True)) == 3
    assert nc.mark_read(a["id"]) is True and nc.unread_count() == 2
    assert nc.mark_read(999) is False
    assert nc.mark_all_read() == 2 and nc.unread_count() == 0
    # سقف ۳۰۰ (۸ آیتم قدیمی حذف می‌شوند — همهٔ read ها میان آن‌ها)
    for i in range(305):
        nc.push("system", f"n{i}")
    assert len(nc._items) == 300
    # ماندگاری: همان ۳۰۰ آیتم، همهٔ باقی‌مانده خوانده‌نشده
    nc2 = NotificationCenter(tmp / "n.json")
    assert len(nc2._items) == 300 and nc2.unread_count() == 300, nc2.unread_count()
    print("ok: notifications push/unread/mark/cap300/persistence")


# ── devx.py import (لانچر اصلی — قبلاً صفر پوشش بود) ─────────────────

def test_devx_module_importable() -> None:
    import devx  # noqa: F401
    assert callable(devx.main)
    print("ok: devx.py (launcher) imports cleanly")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_pass4_"))
    try:
        test_approval_flow(tmp / "a")
        test_approval_timeout_and_history(tmp / "b")
        test_approval_gc_and_reload(tmp / "c")
        test_sandbox_runs(tmp / "d")
        test_sandbox_timeout_and_truncation(tmp / "d")
        test_ratelimiter_and_retry()
        test_is_transient_matrix()
        test_harden_github_tools()
        test_scheduler_parse_spec()
        test_scheduler_next_run_fix(tmp / "e")
        test_scheduler_due_advance_persistence(tmp / "f")
        test_webhooks_crud_and_poll(tmp / "g")
        test_notifications(tmp / "h")
        test_devx_module_importable()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print("\nALL PASS-4 TESTS PASSED ✅")
