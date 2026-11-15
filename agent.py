#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ایجنت خودمختار DevX (حالت ترمینال) — DeepSeek + GitHub + حافظه + مهارت.

این فایل حالت CLI است؛ رابط وب حرفه‌ای با `python devx.py` اجرا می‌شود.
هردو حالت از یک موتور مشترک (agent_loop.AgentEngine) استفاده می‌کنند که:
  * حافظهٔ ماندگار دارد و ابتدای هر تسک، حقایق/درس‌های مرتبط را یادش می‌آورد
  * پس از هر تسک موفق یک مهارت یاد می‌گیرد و به کتابخانه‌اش اضافه می‌کند
  * تاریخچهٔ تسک‌ها و آمار موفقیت را نگه می‌دارد

چرخهٔ کار CLI:
  ۱) دریافت GitHub Classic Token با getpass (github.com/settings/tokens —
     اسکوپ‌های repo و در صورت نیاز workflow) و اعتبارسنجی با GET /user؛
     توکن فقط در حافظهٔ همین اجرا نگه داشته می‌شود و هرگز ذخیره/لاگ نمی‌شود.
  ۲) مرورگر ماندگار Playwright با user_data_dir ثابت (از .env) باز می‌شود.
  ۳) موتور ایجنت روی اکانت chat.deepseek.com تسک‌ها را اجرا می‌کند.

اجرا:
    python agent.py                      # حالت تعاملی (REPL)
    python agent.py "متن تسک"            # حالت تک‌تسک
    python agent.py --tools              # فقط فهرست ابزارها (بدون مرورگر)

دستورات REPL:
    /tools   فهرست ابزارها     /memory  آمار حافظه    /skills فهرست مهارت‌ها
    /stats   آمار عملکرد       /newchat گفت‌وگوی تازه   /exit خروج
"""
from __future__ import annotations

import json
import sys
from getpass import getpass
from typing import Any, Dict, List, Optional

from github import GithubException

import config
from agent_loop import AgentEngine, DeepSeekBrain, EventBus
from builtin_tools import register_builtin_tools
from deepseek_client import DeepSeekClient, DeepSeekError
from github_tools import GitHubTools, TokenInvalidError, register_github_tools
from memory import MemoryStore, register_memory_tools
from projects import ProjectManager
from skills import SkillLibrary, register_skill_tools
from tool_registry import ToolRegistry

BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║   DevX Agent (CLI) — حافظه‌دار، مهارت‌آموز، ابزارمحور               ║
║   chat.deepseek.com (Playwright) + GitHub (PyGithub 2.x)          ║
║   رابط وب حرفه‌ای:  python devx.py  →  http://127.0.0.1:8770       ║
╚══════════════════════════════════════════════════════════════════╝
"""


# ======================================================================
# دریافت و اعتبارسنجی توکن گیت‌هاب در راه‌اندازی
# ======================================================================

def obtain_github_tools(max_attempts: Optional[int] = None) -> GitHubTools:
    """گرفتن توکن با getpass و اعتبارسنجی با GET https://api.github.com/user.

    اگر 401 برگشت، پیام واضح «توکن نامعتبر» داده می‌شود و از کاربر خواسته
    می‌شود دوباره وارد کند (تا TOKEN_MAX_ATTEMPTS تلاش).
    """
    attempts = max_attempts or config.TOKEN_MAX_ATTEMPTS
    print("🔑 برای عملیات گیت‌هاب به یک GitHub Classic Token نیاز است.")
    print("   توکن را از https://github.com/settings/tokens بسازید (Generate new token (classic)).")
    print("   اسکوپ‌های حداقلی: repo  — و در صورت نیاز به Actions: workflow")
    print("   ⚠️  توکن کلاسیک به‌طور پیش‌فرض به منابع سازمان‌ها هم دسترسی دارد؛ با احتیاط بسازید و پس از کار حذف کنید.")
    print("   🔒 توکن فقط در حافظهٔ همین اجرا نگه داشته می‌شود و در فایل/لاگ/کد ذخیره نمی‌شود.\n")

    for attempt in range(1, attempts + 1):
        try:
            token = getpass(f"GitHub Classic Token (ghp_...) [تلاش {attempt}/{attempts}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nخروج.")
            sys.exit(130)
        if not token:
            print("❌ هیچ توکنی وارد نشد — دوباره تلاش کنید.\n")
            continue
        if token.startswith("github_pat_"):
            print("❌ این یک Fine-grained Token است؛ طبق مشخصات، Classic Token با پیشوند ghp_ لازم است.\n")
            continue

        gh_tools = GitHubTools(token)  # توکن فقط در همین آبجکتِ درون‌حافظه‌ای زندگی می‌کند
        try:
            info = gh_tools.validate_token()
        except TokenInvalidError as e:
            print(f"❌ توکن نامعتبر است (401 از GitHub) — لطفاً دوباره وارد کنید. ({e})\n")
            continue
        except GithubException as e:
            print(f"❌ خطای API گیت‌هاب ({e.status}) — لطفاً دوباره تلاش کنید.\n")
            continue

        print(f"✅ توکن معتبر است — کاربر: {info['login']}  (اسکوپ‌ها: {info['token_scopes'] or 'نامشخص'})\n")
        return gh_tools

    print("⛔ تعداد تلاش‌های مجاز برای وارد کردن توکن به پایان رسید.")
    sys.exit(2)


# ======================================================================
# ساخت رجیستری و موتور
# ======================================================================

def build_registry(gh_tools: Optional[GitHubTools], memory: MemoryStore, skills: SkillLibrary,
                   projects: Optional[ProjectManager] = None) -> ToolRegistry:
    """ساخت رجیستری: گیت‌هاب (اگر توکن هست) + حافظه + مهارت + پروژه + ابزارهای داخلی.

    حافظه/مهارت ایزولهٔ پروژهٔ فعال‌اند؛ با سوییچ پروژه، رجیستری با نمونه‌های
    جدید بازساخته می‌شود تا ابزارها هم به حافظهٔ همان پروژه اشاره کنند.
    """
    registry = ToolRegistry()
    if gh_tools is not None:
        register_github_tools(registry, gh_tools)
    register_memory_tools(registry, memory)
    register_skill_tools(registry, skills)
    if projects is not None:
        projects.register_project_tools(registry)
    register_builtin_tools(registry)
    return registry


def cli_pick_project(projects: ProjectManager) -> Dict[str, Any]:
    """انتخاب/ساخت پروژه هنگام شروع CLI — نام پرسیده می‌شود، آیدی خودکار ثبت می‌شود."""
    items = projects.list()
    if not items:
        default = projects.next_default_name()
        try:
            name = input(f"🆕 نام پروژهٔ جدید (پیش‌فرض: {default}): ").strip()
        except (KeyboardInterrupt, EOFError):
            name = ""
        rec = projects.create(name or default)
        print(f"✅ پروژهٔ «{rec['name']}» با آیدی {rec['id']} ساخته شد (آیدی خودکار ثبت شد).")
        return rec

    print("\n📁 پروژه‌ها:")
    for p in items:
        mark = " ← فعال" if projects.active_key == p["key"] else ""
        print(f"  [{p['id']}] {p['name']} — {p.get('task_count', 0)} تسک{mark}")
    active = projects.active()
    default_id = active["id"] if active else items[-1]["id"]
    try:
        raw = input(f"شمارهٔ پروژه را بزن یا نامِ پروژهٔ جدید بنویس [پیش‌فرض: {default_id}]: ").strip()
    except (KeyboardInterrupt, EOFError):
        raw = ""
    if not raw:
        rec = projects.get(default_id) if "error" not in projects.get(default_id) else items[-1]
        projects.select(rec["id"])
        return rec
    if raw.isdigit() and "error" not in projects.get(int(raw)):
        projects.select(int(raw))
        return projects.get(int(raw))
    rec = projects.create(raw)
    print(f"✅ پروژهٔ «{rec['name']}» با آیدی {rec['id']} ساخته شد.")
    return rec


def cli_apply_project(engine: AgentEngine, projects: ProjectManager,
                      gh_tools: Optional[GitHubTools], rec: Dict[str, Any]) -> None:
    """سوییچ کامل موتور به پروژهٔ انتخاب‌شده: حافظه + مهارت + رجیستری + شناسنامه."""
    engine.memory = MemoryStore(memory_dir=projects.memory_dir(rec["key"]))
    engine.skills = SkillLibrary(skills_dir=projects.skills_dir(rec["key"]))
    engine.set_registry(build_registry(gh_tools, engine.memory, engine.skills, projects))
    engine.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"]})


def cli_event_printer(event: str, item: Dict[str, Any]) -> None:
    """چاپ زندهٔ رویدادهای موتور در ترمینال."""
    if event == "round":
        print(f"⏱  دور {item['round']}/{item['max_rounds']}")
    elif event == "tool_call":
        args = json.dumps(item["arguments"], ensure_ascii=False)[:150]
        print(f"🔧 ابزار «{item['tool']}» ← {args}")
    elif event == "tool_result":
        mark = "✅" if item["ok"] else "❌"
        print(f"   {mark} {item['preview']}")
    elif event == "skill_learned":
        print(f"🧠 مهارت جدید آموخته شد: {item['name']}")
    elif event == "lesson_learned":
        print(f"📚 درس ثبت شد: {item['text'][:120]}")


def run_cli() -> None:
    print(BANNER)

    gh_tools = obtain_github_tools()

    # ── پروژه‌ها: انتخاب/ساخت هنگام شروع (نام پرسیده می‌شود، آیدی خودکار ثبت می‌شود) ──
    projects = ProjectManager()
    rec = cli_pick_project(projects)
    memory = MemoryStore(memory_dir=projects.memory_dir(rec["key"]))
    skills = SkillLibrary(skills_dir=projects.skills_dir(rec["key"]))
    print(f"🎯 پروژهٔ فعال: [{rec['id']}] {rec['name']} — حافظهٔ ایزولهٔ خودش بارگذاری شد")

    bus = EventBus()
    bus.subscribe(cli_event_printer)

    brain_client = DeepSeekClient(
        user_data_dir=config.DEEPSEEK_USER_DATA_DIR,
        url=config.DEEPSEEK_URL,
        headless=config.DEEPSEEK_HEADLESS,
        response_timeout=config.RESPONSE_TIMEOUT,
        poll_interval=config.RESPONSE_POLL_INTERVAL,
        stable_checks=config.RESPONSE_STABLE_CHECKS,
    )
    print(f"🌐 راه‌اندازی مرورگر ماندگار (پروفایل: {config.DEEPSEEK_USER_DATA_DIR}) ...")
    brain_client.start()

    try:
        brain_client.ensure_logged_in()
    except Exception as e:
        print(f"⛔ {e}")
        brain_client.close()
        sys.exit(3)

    engine = AgentEngine(
        brain=DeepSeekBrain(brain_client),
        memory=memory,
        skills=skills,
        event_bus=bus,
        max_rounds=config.MAX_ROUNDS,
    )
    engine.set_registry(build_registry(gh_tools, memory, skills, projects))
    engine.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"]})

    counts = memory.counts()
    print(f"🧠 حافظه: {counts['facts']} حقیقت، {counts['lessons']} درس، {counts['tasks']} تسک | 🧰 مهارت‌ها: {len(skills.list_skills())}")
    print_tools(engine.registry)
    print("✅ آماده است. تسک بده (یا /tools /projects /project /newproject /rmproject /memory /skills /stats /newchat /exit)\n")

    args = [a for a in sys.argv[1:]]
    one_shot = " ".join(a for a in args if not a.startswith("--")).strip()

    try:
        if one_shot:
            _run(engine, one_shot, projects)
            return
        while True:
            try:
                task = input("🧑 > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nخروج.")
                break
            if not task:
                continue
            if task in ("/exit", "/quit", "exit", "quit"):
                print("خداحافظ!")
                break
            if task == "/tools":
                print_tools(engine.registry)
                continue
            if task == "/projects":
                for p in projects.list():
                    mark = " ← فعال" if p["key"] == projects.active_key else ""
                    print(f"  [{p['id']}] {p['name']} — {p.get('task_count', 0)} تسک{mark}")
                continue
            if task.startswith("/project "):
                arg = task.split(maxsplit=1)[1].strip()
                if arg.isdigit() and "error" not in projects.get(int(arg)):
                    projects.select(int(arg))
                    rec2 = projects.get(int(arg))
                    cli_apply_project(engine, projects, gh_tools, rec2)
                    print(f"🎯 سوییچ شد به [{rec2['id']}] {rec2['name']} — حافظهٔ ایزولهٔ همان پروژه\n")
                else:
                    print("❌ آیدی نامعتبر — /projects را ببین\n")
                continue
            if task.startswith("/newproject"):
                name = task[len("/newproject"):].strip()
                if not name:
                    name = input("🆕 نام پروژهٔ جدید: ").strip()
                rec2 = projects.create(name or projects.next_default_name())
                cli_apply_project(engine, projects, gh_tools, rec2)
                print(f"✅ پروژهٔ «{rec2['name']}» با آیدی {rec2['id']} ساخته و فعال شد (آیدی خودکار ثبت شد)\n")
                continue
            if task.startswith("/rmproject"):
                arg = task[len("/rmproject"):].strip()
                if not arg.isdigit():
                    print("❌ شکل درست: /rmproject 2\n")
                    continue
                victim = projects.get(int(arg))
                if "error" in victim:
                    print("❌ آیدی نامعتبر\n")
                    continue
                conf = input(f"⚠️ پروژهٔ [{victim['id']}] {victim['name']} با کل حافظه‌اش حذف شود؟ (y/N): ").strip().lower()
                if conf != "y":
                    print("لغو شد.\n")
                    continue
                res = projects.delete(victim["id"])
                print(f"🗑 حذف شد؛ آیدی‌ها بازچینش شدند: " + ", ".join(f"{p['id']}={p['name']}" for p in projects.list()) + "\n")
                new_active = projects.active()
                if new_active:
                    cli_apply_project(engine, projects, gh_tools, new_active)
                    print(f"🎯 پروژهٔ فعال: [{new_active['id']}] {new_active['name']}\n")
                continue
            if task == "/memory":
                print("🧠", memory.counts(), "| آمار:", memory.stats())
                continue
            if task == "/skills":
                for s in skills.list_skills():
                    print(f"  🧰 {s['name']} (استفاده {s['usage']}x، موفق {s['success']}x) — {s['description'][:60]}")
                continue
            if task == "/stats":
                print("📊", memory.stats())
                continue
            if task == "/newchat":
                brain_client.new_chat()
                print("✅ گفت‌وگوی تازه آغاز شد.\n")
                continue
            _run(engine, task, projects)
    finally:
        brain_client.close()


def _run(engine: AgentEngine, task: str, projects: Optional[ProjectManager] = None) -> None:
    proj = getattr(engine, "project_info", {}) or {}
    print(f"🤖 در حال کار (پروژه [{proj.get('id', '—')}] {proj.get('name', '—')})...\n")
    try:
        report = engine.run_task(task)
    except Exception as e:  # noqa: BLE001 — خطای حلقه نباید برنامه را ببندد
        print(f"⛔ خطا در حلقهٔ ایجنت: {e}\n")
        return
    if projects and proj.get("key"):
        projects.record_task(proj["key"])
    print("\n" + "─" * 64)
    print(f"🤖 پاسخ نهایی ({report['rounds']} دور، {report['duration_s']}s):")
    print(report["answer"])
    print("─" * 64 + "\n")


def print_tools(registry: ToolRegistry) -> None:
    print(f"\n🧰 {len(registry)} ابزار فعال:")
    for name in registry.names():
        tool = registry.get(name)
        mark = " ⚠️dangerous" if tool.dangerous else ""
        desc = tool.description.split(" — ")[0].split(". ")[0][:70]
        print(f"  • {name}{mark} — {desc}")
    print()


if __name__ == "__main__":
    run_cli()
