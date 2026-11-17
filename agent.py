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

import config
from agent_loop import AgentEngine, DeepSeekBrain, EventBus
from approval import ApprovalManager
from audit import AuditLog
from builtin_tools import register_builtin_tools
from checkpoint import CheckpointStore
from deepseek_client import DeepSeekClient
from fs_tools import register_fs_tools
from github_tools import (
    GITHUB_LIB,
    GitHubTools,
    GithubException,   # از github_tools می‌آید: با/بدون PyGithub کار می‌کند (ترموکس)
    TokenInvalidError,
    register_github_tools,
)
from memory import MemoryStore, register_memory_tools
from notifications import NotificationCenter
from projects import ProjectManager
from ratelimit import RateLimiter, harden_github_tools
from sandbox import register_code_tools
from scheduler import Scheduler
from sessions import SessionManager
from skills import SkillLibrary, register_skill_tools
from timemachine import TimeMachine
from tool_registry import ToolRegistry
from usage import UsageTracker
from web_tools import register_web_tools
from webhooks import WebhookWatcher

BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║   DevX Agent (CLI) v4 — حافظه + مهارت + زیرایجنت + اتوماسیون       ║
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
    if not GITHUB_LIB:
        print("⚠️ PyGithub نصب نیست — ابزارهای گیت‌هاب غیرفعال‌اند و بدون توکن ادامه می‌دهیم.")
        print("   روی ترموکس: pkg install python-cryptography python-pynacl و بعد pip install PyGithub\n")
        return None
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
                   projects: Optional[ProjectManager] = None, workspace: Optional[Any] = None,
                   approvals: Optional[ApprovalManager] = None, audit: Optional[AuditLog] = None,
                   active_project_id: Optional[int] = None) -> ToolRegistry:
    """ساخت رجیستری v4: گیت‌هاب + حافظه + مهارت + پروژه + وب + فایل/کد سندباکسی + داخلی.

    حافظه/مهارت ایزولهٔ پروژهٔ فعال‌اند؛ با سوییچ پروژه، رجیستری با نمونه‌های
    جدید بازساخته می‌شود. دروازهٔ تایید انسانی + ممیزی هم در صورت ارسال فعال می‌شود.
    """
    registry = ToolRegistry()
    if gh_tools is not None:
        register_github_tools(registry, gh_tools)
    register_memory_tools(registry, memory)
    register_skill_tools(registry, skills)
    register_web_tools(registry)
    if workspace is not None:
        register_fs_tools(registry, workspace)
        register_code_tools(registry, workspace)
    if projects is not None:
        projects.register_project_tools(registry)
    if approvals is not None:
        registry.approvals = approvals
        registry.audit = audit
        registry.project_provider = lambda: active_project_id
        allow = False
        if projects is not None and active_project_id is not None:
            rec = projects.get(active_project_id)
            allow = bool((rec.get("settings") or {}).get("allow_dangerous"))
        registry.approval_policy = lambda name: not allow
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
    elif event == "chat_rollover":
        print(f"🔄 چرخش خودکار چت — سقف context؛ چت جدید ساخته شد و راهنمای پروژه منتقل شد "
              f"(تخمین {item.get('est_tokens_before', '—')} توکن، دلیل: "
              f"{'پیام سایت' if item.get('reason') == 'site_error' else 'نزدیکِ سقف'}) — ادامه از همان نقطه…")


def run_cli() -> None:
    print(BANNER)

    gh_tools = obtain_github_tools()

    # ── پروژه‌ها: انتخاب/ساخت هنگام شروع (نام پرسیده می‌شود، آیدی خودکار ثبت می‌شود) ──
    projects = ProjectManager()
    rec = cli_pick_project(projects)
    memory = MemoryStore(memory_dir=projects.memory_dir(rec["key"]))
    skills = SkillLibrary(skills_dir=projects.skills_dir(rec["key"]))
    sessions = SessionManager(rec["key"], projects.memory_dir(rec["key"]))
    print(f"🎯 پروژهٔ فعال: [{rec['id']}] {rec['name']} — حافظهٔ ایزولهٔ خودش بارگذاری شد (سشن: {sessions.active()['title']})")

    # ── زیرساخت v4 ──
    audit = AuditLog(config.AUDIT_DIR / "chain.jsonl")
    usage = UsageTracker(config.BASE_DIR / "usage.json")
    notifications = NotificationCenter(config.BASE_DIR / "notifications.json")
    approvals = ApprovalManager(config.BASE_DIR / "approvals.json", ttl_s=config.APPROVAL_TTL)
    timemachine = TimeMachine(config.SNAPSHOTS_DIR)
    checkpoints = CheckpointStore(config.CHECKPOINTS_DIR)
    rate_limiter = RateLimiter(config.RATELIMIT_GH_PER_MIN)
    if gh_tools is not None:
        hardened = harden_github_tools(gh_tools, rate_limiter, max_attempts=config.RETRY_MAX_ATTEMPTS)
        print(f"🛡 ابزارهای گیت‌هاب محافظت شدند (نرخ {config.RATELIMIT_GH_PER_MIN}/min + بازتلاش ×{config.RETRY_MAX_ATTEMPTS}) — {hardened} متد")
    notifications_ref = notifications  # همان مرکز اعلان ساخته‌شده در بالا
    # موتور بعداً ساخته می‌شود (بعد از ورود مرورگر) — holder تا فایرِ زودهنگامِ زمان‌بند NameError ندهد
    engine_holder: List[AgentEngine] = []

    def _scheduled_fire(job: Dict[str, Any]) -> None:
        """اجرای job زمان‌بندی در CLI — اگر موتور هنوز آماده/آزاد نبود، فقط اعلان."""
        eng = engine_holder[0] if engine_holder else None
        if eng is None:
            notifications_ref.push("schedule", f"⏰ {job['name']}: موتور هنوز آماده نیست — این‌بار اجرا نشد")
            return
        if eng.busy:
            notifications_ref.push("schedule", f"⏰ {job['name']}: موتور مشغول است — این‌بار اجرا نشد")
            return
        _run(eng, job["task"], projects)

    scheduler = Scheduler(config.BASE_DIR / "jobs.json", fire_callback=_scheduled_fire)
    if config.SCHEDULER_ENABLED:
        scheduler.start()

    def _on_watch_change(watch: Dict[str, Any], change: Dict[str, Any]) -> None:
        """تغییر مخزن زیرنظر → اعلان در مرکز (بدون اجرای خودکار در CLI)."""
        import json as _json
        notifications_ref.push("watch", f"👀 تغییر در {watch.get('repo')}",
                               body=_json.dumps(change.get("to", change), ensure_ascii=False)[:180])

    watcher = WebhookWatcher(config.BASE_DIR / "watches.json", gh_provider=lambda: gh_tools,
                             on_change=_on_watch_change)
    if config.WATCHER_ENABLED:
        watcher.start()

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
        usage=usage,
        checkpoints=checkpoints,
    )
    workspace = config.WORKSPACE_DIR / str(rec["key"])
    engine.set_registry(build_registry(
        gh_tools, memory, skills, projects, workspace=workspace,
        approvals=approvals, audit=audit, active_project_id=rec["id"],
    ))
    engine.register_delegate_tool()
    engine.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"], "persona": rec.get("persona", "")})
    engine_holder.append(engine)  # از این نقطه jobهای زمان‌بندی مجاز به اجرا هستند

    def _cli_apply(rec2: Dict[str, Any]) -> None:
        """سوییچ کامل CLI به پروژهٔ دیگر (حافظه/مهارت/رجیستری/سشن)."""
        nonlocal memory, skills, sessions, rec
        rec = rec2
        memory = MemoryStore(memory_dir=projects.memory_dir(rec2["key"]))
        skills = SkillLibrary(skills_dir=projects.skills_dir(rec2["key"]))
        sessions = SessionManager(rec2["key"], projects.memory_dir(rec2["key"]))
        engine.memory = memory
        engine.skills = skills
        workspace2 = config.WORKSPACE_DIR / str(rec2["key"])
        engine.set_registry(build_registry(
            gh_tools, memory, skills, projects, workspace=workspace2,
            approvals=approvals, audit=audit, active_project_id=rec2["id"],
        ))
        engine.register_delegate_tool()
        engine.set_project({"id": rec2["id"], "name": rec2["name"], "key": rec2["key"], "persona": rec2.get("persona", "")})
        engine.session_id = sessions.active_id()

    engine.session_id = sessions.active_id()

    counts = memory.counts()
    print(f"🧠 حافظه: {counts['facts']} حقیقت، {counts['lessons']} درس، {counts['tasks']} تسک | 🧰 مهارت‌ها: {len(skills.list_skills())}")
    print_tools(engine.registry)
    print("✅ آماده است. تسک بده یا از دستورات استفاده کن:\n"
          "   /tools /projects /project N /newproject نام /rmproject N /sessions /newsession\n"
          "   /approve /schedule /watch /snapshot /export /import /persona /search عبارت /audit /usage /risk /exit\n")

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
                    _cli_apply(rec2)
                    print(f"🎯 سوییچ شد به [{rec2['id']}] {rec2['name']} — حافظهٔ ایزولهٔ همان پروژه\n")
                else:
                    print("❌ آیدی نامعتبر — /projects را ببین\n")
                continue
            if task.startswith("/newproject"):
                name = task[len("/newproject"):].strip()
                if not name:
                    name = input("🆕 نام پروژهٔ جدید: ").strip()
                rec2 = projects.create(name or projects.next_default_name())
                _cli_apply(rec2)
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
                print("🗑 حذف شد (آیدی پروژه‌های دیگر پایدار می‌ماند): " + ", ".join(f"{p['id']}={p['name']}" for p in projects.list()) + "\n")
                new_active = projects.active()
                if new_active:
                    _cli_apply(new_active)
                    print(f"🎯 پروژهٔ فعال: [{new_active['id']}] {new_active['name']}\n")
                continue
            if task == "/sessions":
                for s in sessions.list():
                    mark = " ← فعال" if s["id"] == sessions.active_id() else ""
                    print(f"  [{s['id']}] {s['title']} — {s.get('task_count', 0)} تسک{mark}")
                continue
            if task == "/newsession":
                s = sessions.create(input("💬 عنوان سشن جدید (خالی=خودکار): ").strip())
                engine.session_id = s["id"]
                print(f"✅ سشن «{s['title']}» ساخته و فعال شد — حافظهٔ پروژه مشترک، گفت‌وگو ایزوله\n")
                continue
            if task.startswith("/approve"):
                pend = approvals.pending()
                if not pend:
                    print("چیزی در صف تایید نیست.\n")
                    continue
                for p in pend:
                    print(f"  [{p['id']}] {p['tool']} ← {json.dumps(p['arguments'], ensure_ascii=False)[:120]}")
                aid = input("شناسهٔ درخواست + y/n (مثل ap-1 y): ").strip().split()
                if len(aid) == 2 and aid[1] in ("y", "n"):
                    res = approvals.resolve(aid[0], aid[1] == "y")
                    print(("✅ تایید شد — موتور ادامه می‌دهد" if aid[1] == "y" else "⛔ رد شد") + "\n" if res.get("ok") else "❌ ناموفق\n")
                continue
            if task == "/schedule":
                t = input("⏰ تسک خودکار: ").strip()
                if not t:
                    print("تسک خالی است\n")
                    continue
                kind = input("kind (interval/daily): ").strip() or "interval"
                if kind == "daily":
                    spec = {"kind": "daily", "at": input("ساعت HH:MM (UTC): ").strip()}
                else:
                    try:
                        every = int(input("هر چند ثانیه [3600]: ").strip() or 3600)
                    except ValueError:
                        print("❌ عدد نامعتبر\n")
                        continue
                    spec = {"kind": "interval", "every_s": every}
                res = scheduler.add(rec["id"], t[:30], t, spec)
                print(("✅ ثبت شد — " + json.dumps(res["job"], ensure_ascii=False)[:140] + "\n") if res.get("ok") else f"❌ {res.get('error')}\n")
                continue
            if task == "/watch":
                repo = input("👀 owner/repo: ").strip()
                auto = input("تسک خودکار (خالی=فقط اعلان، {{change}} جایگزین می‌شود): ").strip()
                res = watcher.add(rec["id"], repo, ["issues", "prs", "commits"], config.WATCH_INTERVAL_DEFAULT, auto)
                print(("✅ زیر نظر رفت\n" if res.get("ok") else f"❌ {res.get('error')}\n"))
                continue
            if task == "/snapshot":
                label = input("💾 برچسب snapshot: ").strip() or "cli"
                res = timemachine.create(rec["key"], projects.memory_dir(rec["key"]), projects.skills_dir(rec["key"]), label=label)
                print(("✅ گرفته شد: " + res["id"] + "\n") if res.get("ok") else "❌ ناموفق\n")
                continue
            if task == "/export":
                from bundles import export_project
                res = export_project(rec, projects.memory_dir(rec["key"]), projects.skills_dir(rec["key"]))
                print(("📦 بسته: " + res["file"] + f" ({res['files']} فایل)\n") if res.get("ok") else "❌ ناموفق\n")
                continue
            if task == "/import":
                from bundles import import_project, list_exports
                for x in list_exports()[:10]:
                    print(f"  - {x['name']} ({x.get('project_name')})")
                name = input("نام بسته: ").strip()
                res = import_project(config.EXPORTS_DIR / name, projects, projects.memory_root, projects.skills_root)
                if res.get("ok"):
                    _cli_apply(projects.active())
                    print(f"↗ وارد شد به‌عنوان [{res['project']['id']}] {res['project']['name']}\n")
                else:
                    print(f"❌ {res.get('error')}\n")
                continue
            if task == "/persona":
                print(f"persona فعلی: {(rec.get('persona') or '(خالی)')[:200]}")
                new_p = input("🎭 persona جدید (خالی=پاک کردن): ").strip()
                projects.set_persona(rec["id"], new_p)
                rec = projects.get(rec["id"])
                engine.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"], "persona": rec.get("persona", "")})
                print("✅ ذخیره شد\n")
                continue
            if task.startswith("/search"):
                q = task[len("/search"):].strip() or input("🔎 عبارت: ").strip()
                from globalsearch import search_all
                res = search_all(projects.list(), projects.memory_root, projects.skills_root, q)
                for r in res.get("results", [])[:10]:
                    print(f"  [{r['kind']}] #{r['project_id']} {r['project_name']} — {r['text'][:80]} (score {r['score']})")
                if not res.get("results"):
                    print("چیزی پیدا نشد.")
                print()
                continue
            if task == "/audit":
                v = audit.verify()
                print(f"🛡 زنجیرهٔ ممیزی: {'✅ سالم' if v['ok'] else '⛔ دستکاری‌شده در خط ' + str(v.get('broken_at'))} — {v['entries']} رویداد")
                for e in audit.list(10):
                    print(f"  [{e['seq']}] {e['event']} {(json.dumps(e.get('data') or {}, ensure_ascii=False)[:80])}")
                print()
                continue
            if task == "/usage":
                s = usage.summary(14)
                t = s["totals"]
                print(f"📊 کل: {t['tasks']} تسک، {t['rounds']} دور، {t['tool_calls']} ابزار، ~{t['est_tokens_in'] + t['est_tokens_out']} توکن، {round(t['duration_s'])}s")
                for d in s["trend"][-7:]:
                    print(f"  {d['date']}: {d['tasks']}T · {d['rounds']}R · ~{d['est_tokens']}tok")
                print()
                continue
            if task == "/risk":
                cur = bool((rec.get("settings") or {}).get("allow_dangerous"))
                conf = input(("⚠️ ابزارهای خطرناک بدون تایید اجرا شوند؟ (y/N): ") if not cur else "بازگشت به حالت تایید؟ (y/N): ").strip().lower()
                if conf == "y":
                    projects.set_settings(rec["id"], {"allow_dangerous": not cur})
                    rec = projects.get(rec["id"])
                    _cli_apply(rec)
                    print(("⚠️ risky mode روشن شد" if not cur else "✅ تایید انسانی فعال شد") + "\n")
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
