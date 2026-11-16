# -*- coding: utf-8 -*-
"""تست آفلاین مدیریت پروژه‌های DevX: آیدی فشرده، بازچینش پس از حذف، ایزولاسیون حافظه."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent_loop import AgentEngine, EventBus, FakeBrain  # noqa: E402
from builtin_tools import register_builtin_tools  # noqa: E402
from memory import MemoryStore, register_memory_tools  # noqa: E402
from projects import ProjectManager  # noqa: E402
from skills import SkillLibrary, register_skill_tools  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


def make_env(tmp: Path):
    mgr = ProjectManager(
        memory_root=tmp / "memory",
        skills_root=tmp / "skills_learned",
        legacy_memory_file=tmp / "memory" / "memory.json",
        legacy_skills_dir=tmp / "skills_learned",
    )
    return mgr


def fresh_engine_parts(mgr: ProjectManager, rec: dict):
    mem = MemoryStore(memory_dir=mgr.memory_dir(rec["key"]))
    skills = SkillLibrary(skills_dir=mgr.skills_dir(rec["key"]))
    reg = ToolRegistry()
    register_memory_tools(reg, mem)
    register_skill_tools(reg, skills)
    register_builtin_tools(reg)
    mgr.register_project_tools(reg)
    return mem, skills, reg


def test_create_and_dense_ids(tmp: Path) -> None:
    mgr = make_env(tmp)
    p1 = mgr.create("تست")          # نام دلخواه کاربر
    p2 = mgr.create()               # نام پیش‌فرض: «پروژه ۲» (شمارندهٔ همیشگی)
    p3 = mgr.create("وب‌سایت")
    assert (p1["id"], p2["id"], p3["id"]) == (1, 2, 3)
    assert p1["name"] == "تست" and p2["name"] == "پروژه 2"
    # ساخت → انتخاب خودکار
    assert mgr.active_key == p3["key"]
    # ماندگاری: نمونهٔ جدید همان داده را می‌بیند
    mgr2 = ProjectManager(memory_root=mgr.memory_root, skills_root=mgr.skills_root,
                          legacy_memory_file=mgr.legacy_memory_file, legacy_skills_dir=mgr.legacy_skills_dir)
    assert len(mgr2.list()) == 3 and mgr2.get(2)["name"] == "پروژه 2"
    print("ok: create (auto id + default name + auto-select + persist)")


def test_delete_renumber(tmp: Path) -> None:
    mgr = make_env(tmp)
    a, b, c = mgr.create("الف"), mgr.create("ب"), mgr.create("ج")
    # حذف آیدی 2 → پروژهٔ 3 به 2 منتقل می‌شود («آیدی بعدی جایگزین حذف‌شده»)
    res = mgr.delete(b["id"])
    assert res["ok"] and res["renumbered"]
    ids = [p["id"] for p in mgr.list()]
    assert ids == [1, 2], f"آیدی فشرده نیست: {ids}"
    assert mgr.get(2)["name"] == "ج" and mgr.get(2)["key"] == c["key"]  # داده با کلید پایدار دست‌نخورده
    # حذف فعال → فعال به آخرین می‌رود
    res2 = mgr.delete(c["id"])
    assert mgr.active_key == a["key"] and len(mgr.list()) == 1
    # آیدی جدید بعد از بازچینش = بعدِ آخر
    d = mgr.create("د")
    assert d["id"] == 2
    # حذف نامعتبر
    assert "error" in mgr.get(99)
    print("ok: delete → dense renumber + stable keys + active reassign")


def test_isolation(tmp: Path) -> None:
    mgr = make_env(tmp)
    pa = mgr.create("پروژه ای")
    mem_a, skills_a, reg_a = fresh_engine_parts(mgr, pa)
    reg_a.execute("save_memory", {"section": "fact", "text": "ریپوی پروژه ای: agent است"})
    reg_a.execute("save_skill", {"name": "skill-of-a", "steps": "1. echo"})

    pb = mgr.create("پروژه بی")
    mem_b, skills_b, reg_b = fresh_engine_parts(mgr, pb)
    # حافظهٔ پروژهٔ ب خالی است — اطلاعات ای قاطی نشده
    assert mem_b.counts()["facts"] == 0 and len(skills_b.list_skills()) == 0
    reg_b.execute("save_memory", {"section": "fact", "text": "ریپوی پروژه بی: devx است"})
    # جستجوی ای همچنان فقط دادهٔ خودش را می‌بیند
    hits_a = mem_a.search("ریپوی پروژه")
    assert len(hits_a) == 1 and "ای" in hits_a[0]["text"]
    hits_b = mem_b.search("ریپوی پروژه")
    assert len(hits_b) == 1 and "بی" in hits_b[0]["text"]
    # دایرکتوری‌های فیزیکی جدا
    assert mgr.memory_dir(pa["key"]) != mgr.memory_dir(pb["key"])
    assert (mgr.skills_dir(pa["key"]) / "skill-of-a.md").exists()
    assert not (mgr.skills_dir(pb["key"]) / "skill-of-a.md").exists()
    # ابزار get_project_info از داخل رجیستری
    mgr.select(pa["id"])
    info = reg_a.execute("get_project_info", {})
    assert info["data"]["project"]["id"] == pa["id"] and info["data"]["project"]["memory_isolated"]
    print("ok: isolation (facts/skills/dirs + get_project_info)")


def test_rename_select_persist(tmp: Path) -> None:
    mgr = make_env(tmp)
    a = mgr.create("قدیمی")
    r = mgr.rename(a["id"], "جدید")
    assert r["ok"] and mgr.get(a["id"])["name"] == "جدید" and mgr.get(a["id"])["id"] == 1
    mgr.select(a["id"])
    mgr.record_task(a["key"])
    mgr.record_task(a["key"])
    mgr2 = ProjectManager(memory_root=mgr.memory_root, skills_root=mgr.skills_root,
                          legacy_memory_file=mgr.legacy_memory_file, legacy_skills_dir=mgr.legacy_skills_dir)
    assert mgr2.active_key == a["key"] and mgr2.get(1)["task_count"] == 2
    assert mgr2.next_default_name() == "پروژه 2"  # شمارندهٔ همیشگی بدون تکرار نام
    print("ok: rename/select/record persist")


def test_legacy_migration(tmp: Path) -> None:
    mgr = make_env(tmp)
    # دادهٔ نسخهٔ قبل: memory/memory.json + skills_learned/*.md
    legacy_mem = tmp / "memory" / "memory.json"
    legacy_mem.parent.mkdir(parents=True, exist_ok=True)
    legacy_mem.write_text(json.dumps({"facts": [{"id": 1, "text": "حقیقت قدیمی", "ts": "2026-01-01", "hits": 1}]}), encoding="utf-8")
    (tmp / "skills_learned").mkdir(exist_ok=True)
    (tmp / "skills_learned" / "old-skill.md").write_text("---\nname: old-skill\ntriggers: a\nusage: 1\nsuccess: 1\n---\n1. echo", encoding="utf-8")
    # نمونهٔ جدید → مهاجرت خودکار
    mgr2 = ProjectManager(memory_root=mgr.memory_root, skills_root=mgr.skills_root,
                          legacy_memory_file=legacy_mem, legacy_skills_dir=tmp / "skills_learned")
    migrated = mgr2.list()
    assert len(migrated) == 1 and "مهاجرت‌شده" in migrated[0]["name"]
    mem = MemoryStore(memory_dir=mgr2.memory_dir(migrated[0]["key"]))
    assert mem.counts()["facts"] == 1
    sk = SkillLibrary(skills_dir=mgr2.skills_dir(migrated[0]["key"]))
    assert sk.list_skills()[0]["name"] == "old-skill"
    # مهاجرت تکراری دوباره انجام نشود
    mgr3 = ProjectManager(memory_root=mgr.memory_root, skills_root=mgr.skills_root,
                          legacy_memory_file=legacy_mem, legacy_skills_dir=tmp / "skills_learned")
    assert len(mgr3.list()) == 1
    print("ok: legacy migration (memory + skills, idempotent)")


def test_engine_project_events(tmp: Path) -> None:
    mgr = make_env(tmp)
    pa = mgr.create("پروژه موتور")
    mem, skills, reg = fresh_engine_parts(mgr, pa)
    bus = EventBus()
    events = []
    bus.subscribe(lambda e, i: events.append((e, i)))
    brain = FakeBrain([
        json.dumps({"final_answer": "پایان"}),
        json.dumps({"skip": True}),
    ])
    engine = AgentEngine(brain=brain, memory=mem, skills=skills, event_bus=bus, max_rounds=5)
    engine.set_registry(reg)
    engine.set_project({"id": pa["id"], "name": pa["name"], "key": pa["key"]})
    report = engine.run_task("تسک پروژه‌ای")
    assert report["project"]["id"] == pa["id"] and report["project"]["name"] == "پروژه موتور"
    start_evt = next(item for e, item in events if e == "task_start")
    assert start_evt["project"]["id"] == pa["id"]
    first_msg = brain.sent[0]
    assert "=== PROJECT ===" in first_msg and f"id: {pa['id']}" in first_msg and "پروژه موتور" in first_msg
    print("ok: engine project info (events + report + prompt)")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_projects_"))
    try:
        test_create_and_dense_ids(tmp / "a")
        test_delete_renumber(tmp / "b")
        test_isolation(tmp / "c")
        test_rename_select_persist(tmp / "d")
        test_legacy_migration(tmp / "e")
        test_engine_project_events(tmp / "f")
        print("\nALL PROJECT TESTS PASSED ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
