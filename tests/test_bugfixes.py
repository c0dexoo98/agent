# -*- coding: utf-8 -*-
"""تست‌های رگرسیون بازبینی کامل — هر فیکس با تست مستقل."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from approval import ApprovalManager
from audit import AuditLog
from bundles import import_project
from checkpoint import CheckpointStore
from fs_tools import register_fs_tools
from memory import MemoryStore
from sandbox import run_in_sandbox
from timemachine import TimeMachine
from tool_registry import ToolRegistry
from web_tools import http_get
from webhooks import WebhookWatcher


def test_watcher_state_and_keys(tmp: Path) -> None:
    """watcher: last_state روی رکورد واقعی ذخیره شود + کلیدهای صحیح gh."""
    changes = []

    class FakeGH:
        def list_issues(self, repo, state="open", limit=30):
            return {"count": 1, "issues": [{"number": 12}]}  # بدون کلید ok — مثل متدهای مستقیم
        def list_pull_requests(self, repo, state="open", limit=30):
            return {"count": 0, "pull_requests": []}  # کلید درست: pull_requests نه prs
        def list_commits(self, repo, limit=1):
            return {"count": 1, "commits": [{"sha": "abcdef1234567890"}]}

    w = WebhookWatcher(tmp / "watches.json", gh_provider=lambda: FakeGH(),
                       on_change=lambda watch, change: changes.append(change))
    res = w.add(1, "me/repo", ["issues", "prs", "commits"], interval_s=60)
    wid = res["watch"]["id"]
    # پول اول: مبنا (بدون اعلان)
    assert w.poll_once({"id": wid, "repo": "me/repo", "events": ["issues", "prs", "commits"]}) is None
    stored = w.list()[0]
    assert stored["last_state"]["issues_open"] == 1, "last_state باید روی رکورد واقعی ذخیره شود"
    assert stored["last_state"]["commit_latest"] == "abcdef1234"
    # پول دوم بعد از تغییر → اعلان داده شود
    FakeGH.list_issues = lambda self, repo, state="open", limit=30: {"count": 2, "issues": [{"number": 13}, {"number": 12}]}
    ch = w.poll_once({"id": wid, "repo": "me/repo", "events": ["issues", "prs", "commits"]})
    assert ch and ch["to"]["issues_open"] == 2 and ch["from"]["issues_open"] == 1, ch
    print("ok: watcher (real-state update + direct-gh keys)")


def test_approval_gate_executes(tmp: Path) -> None:
    """گیت تایید: execute منتظر می‌ماند → تایید → ابزار واقعاً اجرا می‌شود؛ رد → پیام رد."""
    reg = ToolRegistry()
    ap = ApprovalManager(tmp / "ap.json", ttl_s=5)
    audit = AuditLog(tmp / "chain.jsonl")
    reg.approvals = ap
    reg.audit = audit
    reg.approval_policy = lambda name: True  # همه dangerous
    events = []
    reg.on_approval_requested = lambda pend: events.append(pend)

    reg.register_function(
        name="danger_write", description="x", dangerous=True,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        func=lambda text: {"written": text},
    )

    # مسیر تایید: execute در نخ دیگر بلوکه می‌شود؛ بعد resolve(approve)
    box: dict = {}
    th = threading.Thread(target=lambda: box.update(res=reg.execute("danger_write", {"text": "سلام"})))
    th.start()
    time.sleep(0.3)
    pend = ap.pending()
    assert pend and pend[0]["tool"] == "danger_write", "درخواست تایید باید ساخته شده باشد"
    assert events and events[0]["tool"] == "danger_write", "callback اعلان باید صدا شده باشد"
    ap.resolve(pend[0]["id"], True)
    th.join(timeout=5)
    assert box["res"]["ok"] and box["res"]["data"]["written"] == "سلام", box["res"]

    # مسیر رد
    res2 = {}
    th2 = threading.Thread(target=lambda: res2.update(r=reg.execute("danger_write", {"text": "دو"})))
    th2.start()
    time.sleep(0.3)
    ap.resolve(ap.pending()[0]["id"], False)
    th2.join(timeout=5)
    assert not res2["r"]["ok"] and "رد" in res2["r"]["error"], res2["r"]
    assert audit.verify()["ok"]
    print("ok: approval gate (wait → execute on approve / reject path)")


def test_zip_slip_blocked(tmp: Path) -> None:
    """Zip-Slip: مسیر ../ در snapshot/import رد شود و بیرون ننویسد."""
    tm = TimeMachine(tmp / "snaps")
    key = "k1"
    mdir = tmp / "mem"; sdir = tmp / "sk"
    mdir.mkdir(); (mdir / "memory.json").write_text("{}", encoding="utf-8")
    res = tm.create(key, mdir, sdir, label="good")
    assert res["ok"]
    # zip مخرب دستی: entry با ../
    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("memory/../../evil.txt", "pwned")
        z.writestr("_meta.json", json.dumps({"created": "x", "label": "evil", "files": []}))
    evil_path = tm._dir(key) / "evil-snap.zip"
    evil.rename(evil_path)
    r = tm.restore(key, "evil-snap.zip", mdir, sdir)
    assert r["ok"] and r["restored"] == 0 and "ناامن" in (r.get("note") or ""), r
    assert not (tmp / "evil.txt").exists(), "نباید بیرون از مقصد نوشته شود"

    # bundles: import با مسیر مخرب
    from projects import ProjectManager
    pm = ProjectManager(memory_root=tmp / "memroot", skills_root=tmp / "skroot")
    bad = tmp / "bad_bundle.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("_meta.json", json.dumps({"format": 1, "project": {"name": "بد"}}))
        z.writestr("memory/../../../escape.txt", "pwned")
    out_dir = tmp / "exports"; out_dir.mkdir()
    res2 = import_project(bad, pm, tmp / "memroot", tmp / "skroot")
    assert res2["ok"] and res2["restored"] == 0, res2
    assert not (tmp / "escape.txt").exists()
    print("ok: zip-slip blocked (timemachine + bundles)")


def test_restore_selective_wipe(tmp: Path) -> None:
    """restore فقط بخش‌های موجود در snapshot را پاک می‌کند — skills حفظ می‌شود."""
    tm = TimeMachine(tmp / "snaps")
    key = "k2"
    mdir = tmp / "mem2"; sdir = tmp / "sk2"
    mdir.mkdir(); (mdir / "memory.json").write_text('{"facts": []}', encoding="utf-8")
    sdir.mkdir(); (sdir / "my-skill.md").write_text("---\nname: my-skill\n---\nsteps", encoding="utf-8")
    # snapshot فقط از memory (بدون skills)
    zip_only_mem = tm._dir(key) / "only-mem.zip"
    with zipfile.ZipFile(zip_only_mem, "w") as z:
        z.write(mdir / "memory.json", arcname="memory/memory.json")
        z.writestr("_meta.json", json.dumps({"created": "x", "label": "only", "files": ["memory/memory.json"]}))
    r = tm.restore(key, "only-mem.zip", mdir, sdir)
    assert r["ok"] and r["restored"] == 1
    assert (sdir / "my-skill.md").exists(), "skills نباید وقتی در snapshot نیست پاک شود"
    print("ok: restore selective wipe (skills preserved)")


def test_memory_id_unique(tmp: Path) -> None:
    """ID حافظه حتی در میلی‌ثانیهٔ مشترک یکتا بماند."""
    mem = MemoryStore(memory_dir=tmp / "m")
    ids = []
    for i in range(300):
        ids.append(mem.add_fact(f"fact {i} — متن تست")["item"]["id"])
    assert len(set(ids)) == 300, "برخورد شناسه!"
    print("ok: memory id uniqueness (300 rapid adds)")


def test_checkpoint_safety(tmp: Path) -> None:
    """checkpoint: شناسهٔ یکتا + رد تراویسل مسیر در load/mark."""
    ck = CheckpointStore(tmp / "cps")
    c1 = ck.save("تست", {"id": 1, "name": "p", "key": "k"}, 1, 3, [{"round": 1, "tool": "echo", "ok": True}])
    c2 = ck.save("تست۲", {"id": 1, "name": "p", "key": "k"}, 1, 4, [])
    assert c1["id"] != c2["id"], "برخورد شناسه در همان ms"
    assert ck.load(c1["id"])["round"] == 3
    # تراویسل
    assert ck.load("../usage") is None and ck.load("cp-abc") is None and ck.load("cp-../../memory/projects/k/memory") is None
    ck.mark("../usage", "done")  # نباید فایل بیرونی بسامد
    assert not (tmp / "usage.json").exists()
    print("ok: checkpoint (unique ids + path-traversal rejected)")


def test_fs_delete_root_blocked(tmp: Path) -> None:
    """fs_delete ریشهٔ workspace را رد کند."""
    reg = ToolRegistry()
    ws = tmp / "ws"; ws.mkdir(parents=True); (ws / "a.txt").write_text("x", encoding="utf-8")
    register_fs_tools(reg, ws)
    reg.approvals = None  # bypass gate برای تست مستقیم
    res = reg.execute("fs_delete", {"path": "."})
    assert not res["data"]["ok"] and "ریشه" in res["data"]["error"], res
    assert (ws / "a.txt").exists(), "workspace نباید جابه‌جا شود"
    # حذف عادی هنوز کار می‌کند
    res2 = reg.execute("fs_delete", {"path": "a.txt"})
    assert res2["data"]["ok"], res2
    print("ok: fs_delete root blocked (normal delete still works)")


def test_http_get_ssrf_blocked() -> None:
    """http_get آدرس‌های لوکال/خصوصی را رد کند (ضد SSRF)."""
    for url in ("http://127.0.0.1:8770/api/state",
                "http://169.254.169.254/latest/meta-data/",
                "http://localhost:8080/",
                "http://192.168.1.5/router",
                "http://[::1]/"):
        res = http_get(url)
        assert not res["ok"] and "مجاز نیست" in res["error"], (url, res)
    # دامنهٔ عمومی از فیلتر رد نمی‌شود (خطای شبکه اشکالی ندارد — فقط فیلتر نباید بخورد)
    res = http_get("https://example.com/", timeout=5)
    assert "مجاز نیست" not in res.get("error", ""), res
    print("ok: http_get SSRF guard (private/loopback blocked)")


def test_sandbox_timeout_kills_group(tmp: Path) -> None:
    """تایم‌اوت سندباکس کل گروه پروسه را بکشد (فرزند سرگردان)."""
    r = run_in_sandbox(["python3", "-c",
                        "import subprocess,sys,time;"
                        "subprocess.Popen(['sleep','30']);"
                        "print('started',flush=True);time.sleep(30)"],
                       workdir=tmp, timeout=3)
    assert not r["ok"] and "گروه پروسه" in r["error"], r
    r2 = run_in_sandbox(["python3", "-c", "print(2+2)"], workdir=tmp)
    assert r2["ok"] and "4" in r2["stdout"], r2
    print("ok: sandbox (group kill on timeout + normal run)")


def test_registry_total_cap(tmp: Path) -> None:
    """سقف کل خروجی ابزار — dict با کلیدهای زیاد کوتاه‌سازی کل می‌شود."""
    reg = ToolRegistry()
    reg.register_function(
        name="flood", description="x",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda: {f"k{i}": "x" * 1000 for i in range(300)},  # ~۳۰۰ هزار کاراکتر
    )
    res = reg.execute("flood", {})
    assert res["ok"], res
    data = res["data"]
    if isinstance(data, dict) and data.get("truncated"):
        assert len(data["preview"]) <= config.TOOL_RESULT_MAX_CHARS
    else:
        # اگر ابزار کوچک شد ناقص است — باید کپ فعال شده باشد
        raise AssertionError(f"سقف کل فعال نشد: {len(json.dumps(data))}")
    # خروجی عادی دست‌نخورده می‌ماند
    reg.register_function(
        name="small", description="x", parameters={"type": "object", "properties": {}, "required": []},
        func=lambda: {"text": "سلام"},
    )
    res2 = reg.execute("small", {})
    assert res2["data"]["text"] == "سلام"
    print("ok: tool output total cap (flood truncated, normal intact)")


def test_server_input_robustness(tmp: Path) -> None:
    """ورودی غیرعددی در مسیرهای عددی سرور → ۵۰۰ JSON نه قطع اتصال."""
    from server import DevXHandler  # فقط ایمپورت/کامپایل و مسیر _safe_int_call
    from server import _safe_int_call
    assert _safe_int_call({"id": "abc"}, "id", lambda i: {"ok": True})["ok"] is False
    print("ok: server input guards (non-numeric id rejected)")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_bugfix_"))
    try:
        test_watcher_state_and_keys(tmp / "a")
        test_approval_gate_executes(tmp / "b")
        test_zip_slip_blocked(tmp / "c")
        test_restore_selective_wipe(tmp / "d")
        test_memory_id_unique(tmp / "e")
        test_checkpoint_safety(tmp / "f")
        test_fs_delete_root_blocked(tmp / "g")
        test_http_get_ssrf_blocked()
        test_sandbox_timeout_kills_group(tmp / "h")
        test_registry_total_cap(tmp / "i")
        test_server_input_robustness(tmp / "j")
        print("\nALL BUGFIX REGRESSION TESTS PASSED ✅")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
