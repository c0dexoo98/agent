# -*- coding: utf-8 -*-
"""تست آفلاین هستهٔ DevX: حافظه، مهارت‌ها، موتور ایجنت (با FakeBrain) — بدون شبکه و مرورگر."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_loop import AgentEngine, EventBus, FakeBrain, extract_json_block  # noqa: E402
from builtin_tools import register_builtin_tools  # noqa: E402
from memory import MemoryStore, register_memory_tools  # noqa: E402
from skills import SkillLibrary, register_skill_tools  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


def fresh(tmp: Path) -> tuple[MemoryStore, SkillLibrary, ToolRegistry]:
    mem = MemoryStore(memory_dir=tmp / "memory")
    skills = SkillLibrary(skills_dir=tmp / "skills_learned")
    reg = ToolRegistry()
    register_memory_tools(reg, mem)
    register_skill_tools(reg, skills)
    register_builtin_tools(reg)
    return mem, skills, reg


def test_memory(tmp: Path) -> None:
    mem, _, reg = fresh(tmp)
    assert reg.execute("save_memory", {"section": "fact", "text": "ریپوی اصلی کاربر agent است"})["ok"]
    assert reg.execute("save_memory", {"section": "lesson", "text": "اگر create_branch خطای 422 داد یعنی شاخه هست"})["ok"]
    assert reg.execute("save_memory", {"section": "note", "text": "کاربر فارسی صحبت می‌کند"})["ok"]
    # تکرار دقیق → dedup با bump (خروجی رجیستری wrap شده: dedup داخل data)
    r = reg.execute("save_memory", {"section": "fact", "text": "ریپوی اصلی کاربر agent است"})
    assert r["ok"] and r["data"].get("dedup") is True

    # جستجو
    res = mem.search("شاخه branch خطا")
    assert res and res[0]["section"] == "lessons" and res[0]["score"] > 0.3
    # ابزار جستجو از مسیر رجیستری
    rr = reg.execute("search_memory", {"query": "ریپوی کاربر"})
    assert rr["ok"] and rr["data"]["results"]
    # رکورد تسک + آمار
    mem.add_task_record("ساخت issue تست", rounds=3, tools_used=["create_issue"], ok=True, duration_s=12.5)
    st = mem.stats()
    assert st["total_tasks"] == 1 and st["ok_tasks"] == 1 and st["success_rate"] == 1.0
    assert st["top_tools"][0]["tool"] == "create_issue"
    # زمینهٔ حافظه
    ctx = mem.build_context("می‌خواهم issue بسازم در ریپو")
    assert "MEMORY CONTEXT" in ctx and "ریپوی اصلی" in ctx
    # ماندگاری روی دیسک (نمونهٔ جدید از همان مسیر)
    mem2 = MemoryStore(memory_dir=tmp / "memory")
    assert mem2.counts()["facts"] == 1 and mem2.counts()["lessons"] == 1
    print("ok: memory (add/dedup/search/record/context/persist)")


def test_skills(tmp: Path) -> None:
    _, skills, reg = fresh(tmp)
    r = reg.execute("save_skill", {
        "name": "close-stale-issues",
        "description": "بستن Issueهای کهنه",
        "triggers": ["issue", "stale", "بستن"],
        "steps": "1. list_issues(state=open)\n2. comment_on_issue(number, 'مؤدبانه')\n3. update_issue_state(number, closed)",
    })
    assert r["ok"] and r["data"]["file"].endswith(".md")
    # خواندن از مسیر رجیستری
    g = reg.execute("get_skill", {"name": "close-stale-issues"})
    assert g["ok"] and "list_issues" in g["data"]["skill"]["steps"]
    # لیست + مرتب‌سازی بر اساس استفاده
    skills.mark_used("close-stale-issues", ok=True)
    skills.mark_used("close-stale-issues", ok=True)
    lst = skills.list_skills()
    assert lst[0]["name"] == "close-stale-issues" and lst[0]["usage"] == 2 and lst[0]["success"] == 2
    # انتخاب مرتبط: تسک با کلمهٔ issue
    rel = skills.relevant_for("بستن issueهای قدیمی در ریپو", limit=3)
    assert rel and rel[0]["name"] == "close-stale-issues"
    ctx = skills.build_context("بستن issue قدیمی")
    assert "LEARNED SKILLS" in ctx and "close-stale-issues" in ctx
    # زمینهٔ نامرتبط خالی
    assert skills.build_context("آشپزی ماکارانی") == ""
    # فایل خام قابل پارس مجدد
    s2 = SkillLibrary(skills_dir=skills.dir)
    assert s2.list_skills()[0]["usage"] == 2  # ماندگاری
    print("ok: skills (save/get/list/relevance/persist)")


def test_engine(tmp: Path) -> None:
    mem, skills, reg = fresh(tmp)
    mem.add_fact("ریپوی اصلی کاربر agent است")
    bus = EventBus()
    events = []
    bus.subscribe(lambda e, i: events.append(e))

    # سناریو: تسک → مدل ابزار echo را صدا می‌زند → پاسخ نهایی → مهارت می‌آموزد
    brain = FakeBrain([
        json.dumps({"tool": "echo", "arguments": {"text": "سلام"}}, ensure_ascii=False),
        json.dumps({"final_answer": "کار تمام شد ✅"}, ensure_ascii=False),
        json.dumps({
            "name": "echo-warmup",
            "description": "تست اتصال ابزار",
            "triggers": ["تست", "echo"],
            "steps": "1. echo(text='سلام')",
        }, ensure_ascii=False),
    ])
    engine = AgentEngine(brain=brain, memory=mem, skills=skills, event_bus=bus, max_rounds=10)
    engine.set_registry(reg)
    report = engine.run_task("یک تست echo اجرا کن")

    assert report["ok"] is True and report["answer"] == "کار تمام شد ✅"
    assert report["rounds"] == 1 and report["tools"][0]["tool"] == "echo"
    assert report["skill_learned"] == "echo-warmup"
    # حافظهٔ تسک ثبت شد
    tasks = mem.recent_tasks(1)
    assert tasks and tasks[0]["task"] == "یک تست echo اجرا کن" and tasks[0]["ok"]
    # مهارت ذخیره شد
    assert skills.get_skill("echo-warmup")["name"] == "echo-warmup"
    # پرامپت اول شامل حافظه + پرامپت سیستمی بوده
    first = brain.sent[0]
    assert "TOOLS" in first and "USER TASK" in first and "ریپوی اصلی" in first
    # توالی رویدادها (رویداد context به‌خاطر تزریق حافظه بین task_start و round می‌آید)
    assert "context" in events, "رویداد تزریق حافظه باید منتشر شود"
    seq = [e for e in events if e != "context"]
    assert seq[:6] == ["task_start", "round", "tool_call", "tool_result", "round", "final"]
    assert "skill_learned" in seq and "task_end" in seq and "memory_updated" in seq
    print("ok: engine happy path (context inject → tool → final → skill learned)")

    # سناریو خطا → درس ثبت می‌شود
    mem2, skills2, reg2 = fresh(tmp / "m2")
    brain2 = FakeBrain([
        json.dumps({"tool": "unknown_tool_xyz", "arguments": {}}),
        json.dumps({"final_answer": "با خطا مواجه شدم"}, ensure_ascii=False),
        json.dumps({"skip": True}),  # استخراج مهارت: تسک تکراری نیست
        json.dumps({"lesson": "unknown_tool_xyz وجود ندارد؛ ابتدا /tools را ببین"}, ensure_ascii=False),
    ])
    bus2 = EventBus()
    engine2 = AgentEngine(brain=brain2, memory=mem2, skills=skills2, event_bus=bus2, max_rounds=5)
    engine2.set_registry(reg2)
    report2 = engine2.run_task("ابزار گم‌شده را صدا بزن")
    assert report2["ok"] is True and report2["errors"] == 1
    lessons = mem2.all_items("lessons")
    assert lessons and "unknown_tool_xyz" in lessons[0]["text"]
    assert report2["lesson_learned"] == lessons[0]["text"]
    print("ok: engine failure → lesson learned")

    # قفل هم‌زمانی: تسک دوم هنگام busy رد می‌شود
    class SlowBrain:
        def new_chat(self): pass
        def send(self, t): pass
        def wait_response(self):
            import time; time.sleep(0.15)
            return json.dumps({"final_answer": "slow"})
    engine3 = AgentEngine(brain=SlowBrain(), memory=mem2, skills=skills2, event_bus=bus2, max_rounds=2)
    engine3.set_registry(reg2)

    import threading
    t = threading.Thread(target=lambda: engine3.run_task("تسک کند"))
    t.start()
    t.join(0.05)
    try:
        engine3.run_task("تسک دوم")
        raise AssertionError("باید busy رد می‌شد")
    except RuntimeError:
        pass
    t.join()
    print("ok: engine busy-lock")


def test_extract_json_block() -> None:
    assert extract_json_block('{"a":1}') == {"a": 1}
    assert extract_json_block('متن قبل {"skip": true} متن بعد') == {"skip": True}
    assert extract_json_block('بدون json') is None
    assert extract_json_block('```json\n{"x": [1,2]}\n```') == {"x": [1, 2]}
    print("ok: extract_json_block")


def test_ui_and_server_files() -> None:
    ui = PROJECT_ROOT / "ui" / "index.html"
    assert ui.exists() and ui.stat().st_size > 20000, "فایل UI باید کامل باشد"
    html = ui.read_text(encoding="utf-8")
    for token in ("DEVX", "/api/stream", "EventSource", "blink", "--teal", "--yellow", "/api/token", "sendTask"):
        assert token in html, f"UI فاقد {token}"
    from pathlib import Path as P
    import server as srv

    assert (P(srv.__file__).parent / "ui" / "index.html").exists()
    print("ok: ui/index.html + server wiring")


if __name__ == "__main__":
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="devx_core_"))
    try:
        test_memory(tmp)
        test_skills(tmp)
        test_engine(tmp)
        test_extract_json_block()
        test_ui_and_server_files()
        print("\nALL DEVX CORE TESTS PASSED ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
