# -*- coding: utf-8 -*-
"""تست‌های رگرسیون پاس ۲ — بازبینی مجدد کامل (فیکس‌های v4.2)."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fs_tools import register_fs_tools
from sessions import SessionManager
from skills import SkillLibrary
from timemachine import TimeMachine
from tool_registry import ToolRegistry
from usage import UsageTracker
from webhooks import WebhookWatcher


class FakeGH:
    """شبیه‌سازی متدهای مستقیم GitHubTools (بدون کلید ok — مثل واقعی)."""

    def __init__(self) -> None:
        self.issue_calls: list = []

    def list_issues(self, repo, state="open", limit=30):
        self.issue_calls.append(time.monotonic())
        return {"count": 1, "issues": [{"number": 1}]}

    def list_pull_requests(self, repo, state="open", limit=30):
        return {"count": 0, "pull_requests": []}

    def list_commits(self, repo, limit=1):
        return {"count": 1, "commits": [{"sha": "a" * 40}]}


def test_watcher_interval_respected(tmp: Path) -> None:
    """interval_s هر watch باید واقعاً رعایت شود — نه هر سیکلِ همه پول شوند."""
    gh = FakeGH()
    w = WebhookWatcher(tmp / "w.json", gh_provider=lambda: gh, on_change=lambda a, b: None)
    w.add(1, "me/repo", ["issues"], interval_s=3600)  # یک‌ساعته
    w.start()
    try:
        time.sleep(3.0)  # سیکل اول فوری پول می‌کند؛ سیکل بعدی باید ۳۰۰ثانیه صبر کند
    finally:
        w.stop()
    assert len(gh.issue_calls) == 1, \
        f"با interval=3600s نباید بیش از یک پول در ۳ ثانیه باشد: {len(gh.issue_calls)}"
    assert w._next_poll, "سرِ موعد per-watch باید ثبت شده باشد"
    print("ok: watcher per-watch interval (3600s watch polled exactly once in 3s)")


def test_fs_search_path_pattern(tmp: Path) -> None:
    """fs_search باید الگوی مسیردار (مثل src/*.py) را هم تطبیق دهد."""
    reg = ToolRegistry()
    ws = tmp / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.py").write_text("x", encoding="utf-8")
    (ws / "b.py").write_text("y", encoding="utf-8")
    register_fs_tools(reg, ws)
    r1 = reg.execute("fs_search", {"pattern": "sub/*.py"})["data"]
    assert r1["ok"] and r1["files"] == ["sub/a.py"], r1
    r2 = reg.execute("fs_search", {"pattern": "*.py"})["data"]
    assert set(r2["files"]) == {"sub/a.py", "b.py"}, r2
    print("ok: fs_search path-aware pattern (sub/*.py works)")


def test_fs_list_works(tmp: Path) -> None:
    """fs_list از ابتدا با NameError می‌شکست (_meta داخل closure بود و در سطح ماژول تعریف نبود)."""
    reg = ToolRegistry()
    ws = tmp / "ws2"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "a.txt").write_text("hi", encoding="utf-8")
    (ws / "top.txt").write_text("t", encoding="utf-8")
    register_fs_tools(reg, ws)
    r = reg.execute("fs_list", {"path": ""})["data"]
    assert r["ok"], r
    names = {i["name"] for i in r["items"]}
    assert names == {"sub", "top.txt"}, r
    r2 = reg.execute("fs_list", {"path": "sub"})["data"]
    assert r2["ok"] and r2["items"][0]["name"] == "a.txt" and r2["items"][0]["size"] == 2, r2
    r3 = reg.execute("fs_list", {"path": "sub/a.txt"})["data"]  # فایل تکی
    assert r3["ok"] and r3["items"][0]["type"] == "file", r3
    r4 = reg.execute("fs_list", {"path": "ناموجود"})["data"]
    assert not r4["ok"], r4
    print("ok: fs_list works (dir, single file, missing path)")


def test_import_skill_md_only(tmp: Path) -> None:
    """import_skill فقط فایل .md بپذیرد."""
    lib = SkillLibrary(skills_dir=tmp / "sk")
    good = tmp / "good.md"
    good.write_text("---\nname: t1\ndescription: d\ntriggers: x\n---\n1. گام", encoding="utf-8")
    bad = tmp / "bad.txt"
    bad.write_text("---\nname: t2\ndescription: d\ntriggers: x\n---\n1. گام", encoding="utf-8")
    assert lib.import_skill(str(good))["ok"], "ورود .md معتبر باید کار کند"
    r = lib.import_skill(str(bad))
    assert not r["ok"] and ".md" in r["error"], r
    print("ok: import_skill .md-only guard")


def test_zip_dir_entries_skipped(tmp: Path) -> None:
    """ورودی‌های دایرکتوری zip نباید فایل صفربایتی بسازند."""
    tm = TimeMachine(tmp / "snaps")
    mdir = tmp / "mem"
    mdir.mkdir(parents=True)
    zpath = tm._dir("k") / "with-dirs.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("memory/sub/", "")          # ورودی دایرکتوری
        z.writestr("memory/sub/a.json", "{}")  # فایل واقعی
        z.writestr("_meta.json", json.dumps({"created": "x", "label": "d", "files": []}))
    r = tm.restore("k", "with-dirs.zip", mdir, tmp / "sk")
    assert r["ok"] and r["restored"] == 1, r
    assert (mdir / "sub" / "a.json").exists()
    assert not (mdir / "sub").is_file(), "دایرکتوری نباید به‌شکل فایل ساخته شود"
    print("ok: zip directory entries skipped (no bogus zero-byte files)")


def test_sessions_active_fallback(tmp: Path) -> None:
    """fallback سشن فعال (id ناموجود) باید بدون کرش آخرین سشن را بدهد."""
    sm = SessionManager("k", tmp / "mem")
    sm._data["active_id"] = 999  # id ناموجود
    a = sm.active()
    assert a and a["id"] == 1 and a["title"] == "چت اصلی", a
    print("ok: sessions.active fallback (no crash on missing active_id)")


def test_usage_summary_totals(tmp: Path) -> None:
    """summary باید جمع کل را زیر قفل بخواند و درست بدهد."""
    ut = UsageTracker(tmp / "u.json")
    ut.record(1, True, rounds=2, tool_calls=3, duration_s=1.5, prompt_chars=400, answer_chars=100)
    s = ut.summary()
    assert s["totals"]["tasks"] == 1 and s["totals"]["rounds"] == 2
    assert s["totals"]["est_tokens_in"] == 100 and s["totals"]["est_tokens_out"] == 25
    print("ok: usage.summary totals under lock")


def test_jobs_survive_project_delete(tmp: Path) -> None:
    """بازچینش آیدیِ پروژه، ارجاع jobs/watches را به پروژهٔ اشتباه وصل می‌کرد — آیدی‌ها حالا پایدارند."""
    from projects import ProjectManager
    from scheduler import Scheduler

    mgr = ProjectManager(memory_root=tmp / "mem", skills_root=tmp / "sk")
    p1, _p2, p3 = mgr.create("یک"), mgr.create("دو"), mgr.create("سه")
    sch = Scheduler(tmp / "jobs.json", fire_callback=None)
    assert sch.add(p3["id"], "جاب", "تسک", {"kind": "daily", "at": "23:59"})["ok"]
    mgr.delete(p1["id"])  # قبلاً این کار p3 را از آیدی ۳ به ۲ می‌برد و جاب به «دو» اشاره می‌کرد
    assert mgr.get(3)["name"] == "سه"
    jobs = sch.list()
    assert jobs and jobs[0]["project_id"] == 3
    assert mgr.get(jobs[0]["project_id"])["key"] == p3["key"], "جاب باید همچنان به «سه» اشاره کند"
    # آیدی جدید بازیافت نمی‌شود
    assert mgr.create("چهار")["id"] == 4
    print("ok: scheduler job keeps pointing at the same project after another project is deleted")


def test_audit_chain_concurrent(tmp: Path) -> None:
    """زنجیرهٔ ممیزی زیر append همزمان نباید بشکند (seq قبلاً بیرون از قفل ساخته می‌شد)."""
    from audit import AuditLog

    log = AuditLog(tmp / "chain.jsonl")

    def hammer(n: int) -> None:
        for i in range(n):
            log.append("stress", {"i": i, "t": threading.get_ident() % 9999})

    threads = [threading.Thread(target=hammer, args=(25,)) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    v = log.verify()
    assert v["ok"] and v["broken_at"] is None and v["entries"] == 400, v
    seqs = [e["seq"] for e in log.list(limit=400)]
    assert sorted(seqs) == list(range(1, 401)), "seq ها باید یکتا و پیوسته باشند"
    print("ok: audit chain survives 400 concurrent appends (unique contiguous seqs)")


def test_sse_replay_marker() -> None:
    """تاریخچهٔ SSE باید برچسب replay بگیرد (UI کارت تکراری نسازد) و snapshot بدون برچسب بماند."""
    import server as srv

    srv.STATE.bus.emit("round", round=1, max_rounds=10)  # یک رویداد در تاریخچه
    server = srv.serve(open_browser=False)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.config.DEVX_PORT, timeout=6)
        conn.request("GET", "/api/stream")
        resp = conn.getresponse()
        buf = b""
        deadline = time.time() + 6
        while time.time() < deadline:
            if b'"replay": true' in buf and b'"event": "snapshot"' in buf:
                break
            try:
                chunk = resp.read(1024)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
        conn.close()
        assert b'"replay": true' in buf, "رویدادهای تاریخچه باید برچسب replay داشته باشند"
        for part in buf.split(b"\n\n"):
            if b'"event": "snapshot"' in part:
                assert b'"replay": true' not in part, "snapshot نباید replay باشد"
    finally:
        server.shutdown()
        server.server_close()
    print("ok: SSE history replay marker (snapshot clean)")


def test_ui_replay_guard_static() -> None:
    """UI باید گارد m.replay داشته باشد (بررسی ایستا)."""
    html = (PROJECT_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "m.replay" in html, "گارد replay در UI نیست"
    print("ok: UI replay guard present (static check)")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_bugfix2_"))
    try:
        test_watcher_interval_respected(tmp / "a")
        test_fs_search_path_pattern(tmp / "b")
        test_fs_list_works(tmp / "b2")
        test_import_skill_md_only(tmp / "c")
        test_zip_dir_entries_skipped(tmp / "d")
        test_sessions_active_fallback(tmp / "e")
        test_usage_summary_totals(tmp / "f")
        test_jobs_survive_project_delete(tmp / "g")
        test_audit_chain_concurrent(tmp / "h")
        test_sse_replay_marker()
        test_ui_replay_guard_static()
        print("\nALL PASS-2 REGRESSION TESTS PASSED ✅")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
