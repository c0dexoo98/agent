# -*- coding: utf-8 -*-
"""تست جامع قابلیت‌های v4 — همهٔ زیرسیستم‌های جدید، آفلاین و واقعی.

پوشش: RAG، Pin/Decay/Tags، بازتاب/زیرایجنت/چک‌پوینت/Resume موتور، نسخهٔ مهارت +
Diff + ترکیب + Marketplace، Audit زنجیرهٔ هش + کشف دستکاری، Usage، Approval
(بلوکه/تایید/رد)، Sandbox، زندان مسیر FS، http_get روی سرور محلی، TimeMachine،
Export/Import، Scheduler، سشن‌ها، جستجوی سراسری، خط زمانی، Persona.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from agent_loop import AgentEngine, EventBus, FakeBrain  # noqa: E402
from approval import ApprovalManager  # noqa: E402
from audit import AuditLog  # noqa: E402
from builtin_tools import register_builtin_tools  # noqa: E402
from checkpoint import CheckpointStore  # noqa: E402
from fs_tools import register_fs_tools  # noqa: E402
from memory import MemoryStore, register_memory_tools  # noqa: E402
from projects import ProjectManager  # noqa: E402
from rag import SemanticIndex  # noqa: E402
from sandbox import run_in_sandbox  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from sessions import SessionManager  # noqa: E402
from skills import SkillLibrary, register_skill_tools  # noqa: E402
from timemachine import TimeMachine  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402
from usage import UsageTracker  # noqa: E402
from web_tools import http_get  # noqa: E402
import bundles  # noqa: E402
import globalsearch  # noqa: E402
import timeline  # noqa: E402


def fresh(tmp: Path):
    """محیط ایزولهٔ کامل برای یک تست."""
    mgr = ProjectManager(memory_root=tmp / "memory", skills_root=tmp / "skills_learned",
                         legacy_memory_file=tmp / "memory" / "memory.json",
                         legacy_skills_dir=tmp / "skills_learned")
    rec = mgr.create("پروژهٔ تست")
    mem = MemoryStore(memory_dir=mgr.memory_dir(rec["key"]))
    sk = SkillLibrary(skills_dir=mgr.skills_dir(rec["key"]))
    reg = ToolRegistry()
    register_memory_tools(reg, mem)
    register_skill_tools(reg, sk)
    register_builtin_tools(reg)
    mgr.register_project_tools(reg)
    return mgr, rec, mem, sk, reg


def test_rag_and_memory(tmp: Path) -> None:
    mgr, rec, mem, sk, reg = fresh(tmp)
    reg.execute("save_memory", {"section": "fact", "text": "ریپوی اصلی پروژه agent است با زبان پایتون", "tags": ["repo", "main"]})
    reg.execute("save_memory", {"section": "note", "text": "قرار شد UI با تم قرمز باشد"})
    # جستجوی برداری: پرسش با واژه‌های متفاوت ولی مرتبط
    hits = mem.search("مخزن اصلی پایتون")
    assert hits and "agent" in hits[0]["text"], f"RAG نشد: {hits}"
    # سنجاق → همیشه اول
    items = mem.all_items("facts")
    mem.set_pinned("facts", items[0]["id"], True)
    ctx = mem.build_context("یک تسک کاملاً بی‌ربط 🚀")
    assert "📌" in ctx, "پین باید در context بیاید"
    # تگ و حذف
    mem.set_tags("facts", items[0]["id"], ["repo", "core"])
    assert mem.all_items("facts")[0]["tags"] == ["repo", "core"]
    res = reg.execute("delete_memory_item", {"section": "fact", "item_id": items[0]["id"]})
    assert res["ok"] and mem.counts()["facts"] == 0
    # فراموشی هوشمند: آیتم قدیمیِ بدون استفاده → آرشیو
    old = mem.add_note("یادداشت خیلی قدیمی")
    with mem._lock:
        for it in mem._data["notes"]:
            if it["id"] == old["item"]["id"]:
                it["ts"] = "2020-01-01T00:00:00+00:00"
    moved = mem.decay(max_age_days=30)
    assert moved["moved"] >= 1, f"باید کهنه به آرشیو برود: {moved}"
    remaining = [n["text"] for n in mem.all_items("notes")]
    assert all("قدیمی" not in t for t in remaining), "کهنه باید رفته باشد"
    assert mem.archived_items() and mem.restore_archived(mem.archived_items()[0]["ts"])["ok"]
    print("ok: RAG + pin/tags/delete + decay/archive/restore")


def test_skill_versioning_and_marketplace(tmp: Path) -> None:
    mgr, rec, mem, sk, reg = fresh(tmp)
    reg.execute("save_skill", {"name": "deploy", "steps": "1. build\n2. push", "description": "استقرار"})
    reg.execute("save_skill", {"name": "deploy", "steps": "1. test\n2. build\n3. push", "description": "استقرار کامل"})
    v = reg.execute("skill_versions", {"name": "deploy"})["data"]
    assert v["current_version"] == 2 and len(v["history"]) == 1
    d = reg.execute("skill_diff", {"name": "deploy"})["data"]
    assert "-" in d["diff"] and "+" in d["diff"], "دیف باید تغییر را نشان دهد"
    # ترکیب: skill-a از skill-b استفاده می‌کند
    reg.execute("save_skill", {"name": "base-steps", "steps": "0. آماده‌سازی محیط"})
    reg.execute("save_skill", {"name": "full", "steps": "1. اجرای اصلی", "uses": ["base-steps"]})
    g = reg.execute("get_skill", {"name": "full"})["data"]["skill"]
    assert "SUB-SKILL: base-steps" in g["steps"], "ترکیب انجام نشد"
    # Marketplace
    ex = reg.execute("export_skill", {"name": "deploy"})["data"]
    assert Path(ex["file"]).exists()
    im = reg.execute("import_skill", {"path": ex["file"]})["data"]
    assert im["ok"] and im["file"].endswith("deploy.md")
    print("ok: skill versioning + diff + composition + marketplace")


def test_audit_chain_and_usage(tmp: Path) -> None:
    al = AuditLog(tmp / "chain.jsonl")
    for i in range(5):
        al.append("tool_executed", {"i": i})
    v = al.verify()
    assert v["ok"] and v["entries"] == 5
    # دستکاری یک فیلد → کشف دقیق
    lines = (tmp / "chain.jsonl").read_text(encoding="utf-8").splitlines()
    e2 = json.loads(lines[2]); e2["data"]["i"] = 999
    lines[2] = json.dumps(e2, ensure_ascii=False)
    (tmp / "chain.jsonl").write_text("\n".join(lines), encoding="utf-8")
    v2 = AuditLog(tmp / "chain.jsonl").verify()
    assert not v2["ok"] and v2["broken_at"] == 2
    # Usage
    ut = UsageTracker(tmp / "usage.json")
    ut.record(1, True, rounds=3, tool_calls=4, duration_s=2.0, prompt_chars=800, answer_chars=400)
    ut.record(2, False, rounds=1, tool_calls=1, duration_s=1.0, prompt_chars=400, answer_chars=100)
    s = ut.summary(14)
    assert s["totals"]["tasks"] == 2 and s["totals"]["rounds"] == 4
    assert s["totals"]["est_tokens_in"] == 300 and s["totals"]["est_tokens_out"] == 125
    assert set(s["by_project"].keys()) == {"1", "2"}
    print("ok: audit hash-chain (verify+tamper detect) + usage")


def test_approval_gate(tmp: Path) -> None:
    """گیت تایید (رفتار کامل): execute منتظر تصمیم می‌ماند؛ تایید → اجرای واقعی ابزار؛ رد → پیام رد."""
    mgr, rec, mem, sk, reg = fresh(tmp)
    am = ApprovalManager(tmp / "approvals.json", ttl_s=5)
    reg.approvals = am
    reg.approval_policy = lambda name: True
    reg.project_provider = lambda: rec["id"]

    @reg.register(name="boom", description="خطرناک", parameters={"type": "object", "properties": {"x": {"type": "string"}}}, dangerous=True)
    def boom(x: str):
        return {"exploded": x}

    # مسیر تایید: نخِ ابزار بلوکه می‌شود تا کاربر تصمیم بگیرد
    outcome: dict = {}

    def caller():
        outcome["res"] = reg.execute("boom", {"x": "واقعی"})

    th = threading.Thread(target=caller); th.start()
    time.sleep(0.3)
    pend = am.pending()
    assert pend and pend[0]["tool"] == "boom", "درخواست تایید باید ساخته شده باشد"
    aid = pend[0]["id"]
    wait_out: dict = {}

    def waiter():
        wait_out["res"] = am.wait(aid, timeout_s=5)

    th2 = threading.Thread(target=waiter); th2.start()
    time.sleep(0.2)
    am.resolve(aid, True)
    th2.join(5)
    assert wait_out["res"]["status"] == "approved"
    th.join(5)
    assert outcome["res"]["ok"] and outcome["res"]["data"]["exploded"] == "واقعی", \
        f"پس از تایید، ابزار باید واقعاً اجرا شده باشد: {outcome['res']}"
    # مسیر رد: بدون اجرا، پیام رد به مدل می‌رسد
    res2: dict = {}

    def caller2():
        res2["res"] = reg.execute("boom", {"x": "دوم"})

    th3 = threading.Thread(target=caller2); th3.start()
    time.sleep(0.3)
    pend2 = am.pending()
    assert pend2, "درخواست دوم باید ساخته شود"
    am.resolve(pend2[0]["id"], False)
    th3.join(5)
    assert not res2["res"]["ok"] and "رد" in res2["res"]["error"], res2["res"]
    assert "exploded" not in (res2["res"].get("data") or {}), "ابزار ردشده نباید اجرا شده باشد"
    assert am.history() and am.pending() == []
    print("ok: approval gate (wait → executed on approve / rejected path)")


def test_sandbox_and_fs_jail(tmp: Path) -> None:
    ws = tmp / "workspace"
    res = run_in_sandbox(["python3", "-I", "-c", "print(2+3)"], workdir=ws, timeout=20)
    assert res["ok"] and "5" in res["stdout"]
    res2 = run_in_sandbox(["python3", "-c", "print('x'*200000)"], workdir=ws)
    assert res2["ok"] and len(res2["stdout"]) <= config.SANDBOX_MAX_OUTPUT + 60
    res3 = run_in_sandbox(["python3", "-c", "import time; time.sleep(99)"], workdir=ws, timeout=2)
    assert not res3["ok"] and "تایم‌اوت" in res3.get("error", "")
    # FS jail
    reg = ToolRegistry()
    register_fs_tools(reg, ws)
    reg.execute("fs_write", {"path": "a/b.txt", "content": "سلام"})
    r = reg.execute("fs_read", {"path": "a/b.txt"})
    assert r["ok"] and "سلام" in r["data"]["content"]
    esc1 = reg.execute("fs_read", {"path": "../outside.txt"})
    assert not esc1["ok"] and "workspace" in esc1["error"]
    esc2 = reg.execute("fs_read", {"path": "/etc/passwd"})
    assert not esc2["ok"]
    s = reg.execute("fs_search", {"pattern": "*.txt"})
    assert s["data"]["count"] == 1
    d = reg.execute("fs_delete", {"path": "a/b.txt"})
    assert d["ok"] and ".trash" in d["data"]["moved_to_trash"]
    print("ok: sandbox (timeout/output cap) + fs path jail + trash delete")


def test_http_get_local(tmp: Path) -> None:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><body><h1>DevX test page</h1><script>ignore()</script><p>ok-body</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # گارد SSRF: آدرس لوکال در http_get مسدود است (ضد واکشی سرویس محلی/متادیتا)
        blocked = http_get(f"http://127.0.0.1:{port}/", max_chars=2000)
        assert not blocked["ok"] and "مجاز نیست" in blocked["error"], blocked
        # استخراج HTML→متن همچنان با _fetch مستقیم (زیرِ گاردِ لایهٔ ابزار) سنجیده می‌شود
        from web_tools import _fetch, html_to_text
        raw = _fetch(f"http://127.0.0.1:{port}/", timeout=5)
        text = html_to_text(raw)
        assert "DevX test page" in text and "ignore()" not in text
    finally:
        srv.shutdown()
    print("ok: http_get (SSRF guard blocks localhost + html→text via direct fetch)")


def test_timemachine_and_bundles(tmp: Path) -> None:
    tm = TimeMachine(tmp / "snapshots")
    mdir = tmp / "mem"; sdir = tmp / "sk"
    mdir.mkdir(parents=True); sdir.mkdir(parents=True)
    (mdir / "memory.json").write_text(json.dumps({"facts": [{"id": 1, "text": "نسخهٔ ۱"}]}, ensure_ascii=False), encoding="utf-8")
    (sdir / "s1.md").write_text("---\nname: s1\n---\n1. a", encoding="utf-8")
    snap = tm.create("k1", mdir, sdir, label="v1")
    assert snap["ok"] and snap["files"] == 2
    # تغییر وضعیت و رول‌بک
    (mdir / "memory.json").write_text(json.dumps({"facts": [{"id": 2, "text": "نسخهٔ ۲"}]}, ensure_ascii=False), encoding="utf-8")
    res = tm.restore("k1", snap["id"], mdir, sdir)
    assert res["ok"] and res["restored"] == 2
    assert "نسخهٔ ۱" in (mdir / "memory.json").read_text(encoding="utf-8")
    assert tm.list("k1")[0]["id"] == snap["id"]

    # Export/Import کامل پروژه
    mgr = ProjectManager(memory_root=tmp / "mem2", skills_root=tmp / "sk2",
                         legacy_memory_file=tmp / "mem2" / "memory.json",
                         legacy_skills_dir=tmp / "sk2")
    a = mgr.create("اصل")
    amem = mgr.memory_dir(a["key"]); ask = mgr.skills_dir(a["key"])
    amem.mkdir(parents=True, exist_ok=True); ask.mkdir(parents=True, exist_ok=True)
    (amem / "memory.json").write_text(json.dumps({"facts": [{"id": 1, "text": "دادهٔ اصلی"}]}, ensure_ascii=False), encoding="utf-8")
    (ask / "skill.md").write_text("---\nname: sk\n---\n1. x", encoding="utf-8")
    exp = bundles.export_project(a, amem, ask, exports_dir=tmp / "exports")
    assert exp["ok"] and Path(exp["file"]).exists()
    im = bundles.import_project(Path(exp["file"]), mgr, tmp / "mem2", tmp / "sk2")
    assert im["ok"]
    b = im["project"]
    assert b["id"] != a["id"] and b["key"] != a["key"]  # کلید پایدار جدید — ایزولاسیون حفظ
    imported_mem = json.loads((mgr.memory_dir(b["key"]) / "memory.json").read_text(encoding="utf-8"))
    assert imported_mem["facts"][0]["text"] == "دادهٔ اصلی"
    # جستجوی سراسری + خط زمانی (فکت با ts کامل تا در خط زمانی بیاید)
    gs = globalsearch.search_all(mgr.list(), tmp / "mem2", tmp / "sk2", "دادهٔ اصلی")
    assert gs["count"] >= 1
    assert any(r["project_id"] == b["id"] for r in gs["results"]), "پروژهٔ واردشده باید در جستجوی سراسری باشد"
    facts = imported_mem["facts"]
    facts[0].setdefault("ts", "2026-11-15T00:00:00+00:00")
    (mgr.memory_dir(b["key"]) / "memory.json").write_text(
        json.dumps(imported_mem, ensure_ascii=False), encoding="utf-8")
    tl = timeline.build_timeline(b["key"], mgr.memory_dir(b["key"]), mgr.skills_dir(b["key"]),
                                 snapshots=tm.list(b["key"]))
    assert any(e["kind"] == "fact" for e in tl)
    print("ok: TimeMachine + export/import + global search + timeline")


def test_scheduler_sessions(tmp: Path) -> None:
    fired: list = []
    sc = Scheduler(tmp / "jobs.json", fire_callback=lambda job: fired.append(job["id"]))
    j = sc.add(1, "هر سه ثانیه", "تسک تست", {"kind": "interval", "every_s": 30})
    assert j["ok"]
    sc.start()
    # تسریع: next_run را به الان نزدیک کن
    from datetime import datetime, timedelta, timezone
    with sc._lock:
        sc._jobs[0]["next_run"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="seconds")
    deadline = time.time() + 35
    while time.time() < deadline and not fired:
        time.sleep(0.5)
    sc.stop()
    assert fired, "زمان‌بند اجرا نشد"
    assert sc.list()[0]["run_count"] >= 1

    # سشن‌ها
    sess = SessionManager("kx", tmp / "memdir")
    s2 = sess.create("دوم")
    assert sess.active_id() == s2["id"] and len(sess.list()) == 2
    sess.record_task(s2["id"])
    assert sess.list()[1]["task_count"] == 1
    assert sess.select(1)["id"] == 1 and sess.active()["title"] == "چت اصلی"
    assert sess.delete(2)["ok"] and len(sess.list()) == 1
    print("ok: scheduler fire + sessions CRUD")


def test_engine_v4(tmp: Path) -> None:
    """زیرایجنت + بازتاب + چک‌پوینت/Resume + persona + usage موتور."""
    mgr, rec, mem, sk, reg = fresh(tmp)
    projects2 = mgr
    ck = CheckpointStore(tmp / "cps")
    ut = UsageTracker(tmp / "usage.json")
    bus = EventBus()
    events = []
    bus.subscribe(lambda e, i: events.append((e, i)))

    # سناریو: والد → delegate به researcher → researcher یک ابزار اجرا و final می‌دهد
    # → والد final → DISTILL skip → REFLECT JSON
    brain = FakeBrain([
        json.dumps({"tool": "delegate_subtask", "arguments": {"role": "researcher", "task": "بررسی وضعیت"}}),
        # زیرایجنت researcher:
        json.dumps({"tool": "echo", "arguments": {"text": "sub"}}),
        json.dumps({"final_answer": "گزارش زیرایجنت: همه‌چیز اوکی"}),
        # والد بعد از گرفتن TOOL_RESULT:
        json.dumps({"final_answer": "پاسخ نهایی والد"}),
        json.dumps({"skip": True}),          # DISTILL
        json.dumps({"what_worked": "delegation", "what_failed": "", "improvement": "زیرایجنت برای تحقیق"}),  # REFLECT
    ])
    eng = AgentEngine(brain=brain, memory=mem, skills=sk, event_bus=bus, max_rounds=8,
                      usage=ut, checkpoints=ck)
    eng.set_registry(reg)
    eng.register_delegate_tool()
    eng.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"], "persona": "همیشه فارسی جواب بده"})
    report = eng.run_task("تسک با واگذاری")
    assert report["ok"] and "پاسخ نهایی والد" in report["answer"]
    assert report["reflection"] == "زیرایجنت برای تحقیق"
    # زیرایجنت در trace والد یک ابزار است؛ echo داخل subtask در subtask_end آمده
    kinds = [e for e, _ in events]
    assert "subtask_start" in kinds and "subtask_end" in kinds and "reflection" in kinds
    sub_end = next(i for e, i in events if e == "subtask_end")
    assert sub_end["ok"] and "گزارش زیرایجنت" in sub_end["answer"]
    # persona در پرامپت
    assert "PROJECT PERSONA" in brain.sent[0] and "همیشه فارسی" in brain.sent[0]
    # usage ثبت شد
    assert ut.summary(1)["totals"]["tasks"] == 1
    # چک‌پوینت‌های ذخیره‌شده
    cps = ck.list(5)
    assert cps and cps[0]["round"] >= 1
    # Resume: سناریوی ادامه از چک‌پوینت
    cp = cps[0]
    brain2 = FakeBrain([json.dumps({"final_answer": "ادامه داده شد و کامل شد"})])
    eng.brain = brain2
    r2 = eng.resume(cp["id"])
    assert r2["ok"] and "ادامه داده شد" in r2["answer"] and r2["resumed_from"] == cp["id"]
    assert "RESUMED TASK" in brain2.sent[0]
    assert ck.load(cp["id"])["status"] == "done"
    print("ok: engine v4 (subtask + reflection + persona + usage + checkpoint/resume)")


def test_chat_rollover(tmp: Path) -> None:
    """چرخش خودکار چت: سقف context → چت جدید + انتقال راهنمای پروژه + ادامه از همان نقطه."""
    mgr, rec, mem, sk, reg = fresh(tmp)
    bus = EventBus()
    events = []
    bus.subscribe(lambda e, i: events.append((e, i)))

    # ذخیرهٔ تنظیمات و آستانهٔ کوچک برای تست
    old = (config.DEEPSEEK_CONTEXT_TOKENS, config.ROLLOVER_AT_RATIO)
    config.DEEPSEEK_CONTEXT_TOKENS = 400
    config.ROLLOVER_AT_RATIO = 0.8  # آستانه = ۳۲۰ توکن ≈ ۱۲۸۰ کاراکتر
    try:
        # ── الف) چرخش تخمینی: نتیجهٔ بزرگِ ابزار → _send قبل از سقف می‌چرخد ──
        big = "x" * 2000
        brain = FakeBrain([
            json.dumps({"tool": "echo", "arguments": {"text": big}}),
            json.dumps({"final_answer": "پس از چرخش کامل شد"}),
            json.dumps({"skip": True}),   # DISTILL
            json.dumps({"skip": True}),   # REFLECT
        ])
        eng = AgentEngine(brain=brain, memory=mem, skills=sk, event_bus=bus, max_rounds=6)
        eng.set_registry(reg)
        eng.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"], "persona": "همیشه فارسی"})
        r = eng.run_task("تسک چرخش تخمینی")
        assert r["ok"] and r["rollovers"] == 1, r
        assert brain.new_chat_calls == 2, f"باید چت جدید در سایت ساخته می‌شد: {brain.new_chat_calls}"
        roll = [i for e, i in events if e == "chat_rollover"]
        assert roll and roll[0]["reason"] == "estimate" and roll[0]["rollover_no"] == 1
        handoff = brain.sent[1]
        # راهنمای کامل پروژه در پیام انتقال: persona + حافظه + تسک + پیشرفت + پیام معلق
        assert "[SYSTEM — CHAT ROLLOVER]" in handoff
        assert "PROJECT GUIDE" in handoff and "همیشه فارسی" in handoff
        assert "TASK IN PROGRESS" in handoff and "تسک چرخش تخمینی" in handoff
        assert "PROGRESS SO FAR" in handoff and "echo" in handoff
        assert "CONTINUE WITH THIS PENDING MESSAGE" in handoff and "TOOL_RESULT echo" in handoff
        assert r["chat_est_tokens"] > 0

        # ── ب) چرخش با خطای سایت (ContextLimitError از wait_response) ──
        class ContextLimitError(Exception):
            pass

        brain2 = FakeBrain([
            ContextLimitError("سایت اعلام کرد سقف context پر است"),
            json.dumps({"final_answer": "بعد از خطای سایت ادامه داد"}),
            json.dumps({"skip": True}),
            json.dumps({"skip": True}),
        ])
        eng2 = AgentEngine(brain=brain2, memory=mem, skills=sk, event_bus=bus, max_rounds=6)
        eng2.set_registry(reg)
        eng2.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"]})
        r2 = eng2.run_task("تسک خطای سایت")
        assert r2["ok"] and r2["rollovers"] == 1 and brain2.new_chat_calls == 2
        assert [i["reason"] for e, i in events if e == "chat_rollover"].count("site_error") == 1
        # آخرین پیام ارسالیِ قبل از خطا (پیام اول) در handoff ادغام شده تا تکرار نشود
        assert "تسک خطای سایت" in brain2.sent[1]

        # ── ج) گزارش تسک حاوی لاگ چرخش ──
        assert r["rollover_log"] and r["rollover_log"][0]["reason"] == "estimate"
        assert any(e == "chat_rollover" for e, _ in events)
        print("ok: chat rollover (estimate + site-error + handoff guide + new chat on site)")
    finally:
        config.DEEPSEEK_CONTEXT_TOKENS, config.ROLLOVER_AT_RATIO = old


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_v4_"))
    try:
        test_rag_and_memory(tmp / "a")
        test_skill_versioning_and_marketplace(tmp / "b")
        test_audit_chain_and_usage(tmp / "c")
        test_approval_gate(tmp / "d")
        test_sandbox_and_fs_jail(tmp / "e")
        test_http_get_local(tmp / "f")
        test_timemachine_and_bundles(tmp / "g")
        test_scheduler_sessions(tmp / "h")
        test_engine_v4(tmp / "i")
        test_chat_rollover(tmp / "j")
        print("\nALL V4 FEATURE TESTS PASSED ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
