# -*- coding: utf-8 -*-
"""سرور DevX v4 — بک‌اند کامل رابط وب ایجنت (کتابخانهٔ استاندارد، بدون فریم‌ورک).

معماری:
  * ThreadingHTTPServer + SSE برای استریم زندهٔ رویدادها (بدون WebSocket و بدون وابستگی)
  * چندسشن موازی: هر سشن یک AgentEngine مستقل با حافظهٔ مشترک پروژه؛ اجرا روی
    مغزِ مشترک با Semaphore سریال می‌شود (یک مرورگر دیپ‌سیک = یک تسک همزمان).
  * رویدادها با session_id/project مُهر می‌شوند و UI فیلتر می‌کند.
  * زیرساخت v4: Audit زنجیرهٔ هش، Usage، Approval (انسان در حلقه)، RateLimit+Backoff
    روی گیت‌هاب، Scheduler، Webhook Watcher، TimeMachine، Export/Import،
    Checkpoint/Resume، Persona هر پروژه، جستجوی سراسری، خط زمانی، اعلان‌ها.
  * توکن گیت‌هاب فقط در حافظهٔ همین فرایند نگه داشته می‌شود — هرگز لاگ/فایل نمی‌شود.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import bundles
import config
import globalsearch
import timeline
from agent_loop import AgentEngine, DeepSeekBrain, EventBus
from approval import ApprovalManager
from audit import AuditLog
from builtin_tools import register_builtin_tools
from checkpoint import CheckpointStore
from api_client import APIBrain, APIError
from deepseek_client import DeepSeekClient, auto_headless_background
from fs_tools import register_fs_tools
from gh_api import GhApiError
from github_tools import (
    GitHubTools,
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

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"


def short_error(e: BaseException, max_chars: int = 200) -> str:
    """خلاصهٔ کوتاه و قابل‌نمایش از یک خطا برای پیام وضعیت UI.

    استثناهای Playwright کل لاگ مرورگر (چند کیلوبایت متن فنی LTR) را داخل
    str(e) می‌آورند؛ فقط خطِ اولِ معنادار (پیام اصلی خطا) را نگه می‌داریم و
    تا ۲۰۰ نویسه محدود می‌کنیم تا نوار هشدار/UI را نگیرند. متن کامل در لاگ
    ترمینال سرور می‌ماند.
    """
    text = str(e).strip() or e.__class__.__name__
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    if len(first) > max_chars:
        first = first[: max_chars - 1].rstrip() + "…"
    return first


class DevXState:
    """وضعیت مشترک سرور v4: پروژه‌ها، سشن‌ها، موتورها، مغز، زیرساخت‌ها."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.projects = ProjectManager()

        # زیرساخت‌های v4
        self.audit = AuditLog(config.AUDIT_DIR / "chain.jsonl")
        self.usage = UsageTracker(config.BASE_DIR / "usage.json")
        self.notifications = NotificationCenter(config.BASE_DIR / "notifications.json")
        self.approvals = ApprovalManager(config.BASE_DIR / "approvals.json", ttl_s=config.APPROVAL_TTL)
        self.timemachine = TimeMachine(config.SNAPSHOTS_DIR)
        self.checkpoints = CheckpointStore(config.CHECKPOINTS_DIR)
        self.rate_limiter = RateLimiter(config.RATELIMIT_GH_PER_MIN)
        self.brain_sem = threading.Semaphore(1)   # مغز دیپ‌سیک = یک مرورگر → نوبتی

        self.gh_tools: Optional[GitHubTools] = None
        self.token_info: Optional[Dict[str, Any]] = None
        self.client: Optional[DeepSeekClient] = None
        self.brain: Optional[DeepSeekBrain] = None
        # مغز API: کلید فقط در حافظهٔ همین اجرا (از UI) یا از .env؛ هرگز روی دیسک نمی‌رود
        self._api_key_mem: Optional[str] = None
        self._gh_flow = None   # جلسهٔ جاری «ورود با مرورگر» (Device Flow) — اگر فعال باشد
        self._api_base: Optional[str] = None
        self._api_model: Optional[str] = None
        self.brain_kind: Optional[str] = None   # "api" | "browser" | None

        # حافظه/مهارت ایزولهٔ پروژهٔ فعال + سشن‌ها
        self.memory = self._memory_for(self.projects.active())
        self.skills = self._skills_for(self.projects.active())
        self.sessions = self._sessions_for(self.projects.active())
        self._session_engines: Dict[int, AgentEngine] = {}

        self.engine = self._build_engine(self._active_session_id())

        self.client: Optional[DeepSeekClient] = None  # (placeholder نگه‌داشته‌شده برای سازگاری)
        self.deepseek_started = False
        self.deepseek_ready = False
        self.deepseek_message = "در انتظار راه‌اندازی مرورگر…"
        self._connector_lock = threading.Lock()

        # اتوماسیون
        self.scheduler = Scheduler(config.BASE_DIR / "jobs.json", fire_callback=self._on_job_fire)
        self.watcher = WebhookWatcher(
            config.BASE_DIR / "watches.json",
            gh_provider=lambda: self.gh_tools,
            on_change=self._on_watch_change,
        )
        if config.SCHEDULER_ENABLED:
            self.scheduler.start()
        if config.WATCHER_ENABLED:
            self.watcher.start()

        self.bus.subscribe(self._on_engine_event)
        self.audit.append("server_started", {"version": "4.0"})

    # ------------------------------------------------------------------
    # کارخانه‌های پروژه‌محور
    # ------------------------------------------------------------------

    def _memory_for(self, project: Optional[Dict[str, Any]]) -> MemoryStore:
        """حافظهٔ ایزولهٔ یک پروژه (memory/projects/<key>/)."""
        if project:
            return MemoryStore.open(memory_dir=self.projects.memory_dir(project["key"]))
        return MemoryStore.open(memory_dir=self.projects.memory_root / "projects" / "_unassigned")

    def _skills_for(self, project: Optional[Dict[str, Any]]) -> SkillLibrary:
        if project:
            return SkillLibrary(skills_dir=self.projects.skills_dir(project["key"]))
        return SkillLibrary(skills_dir=self.projects.skills_root / "projects" / "_unassigned")

    def _sessions_for(self, project: Optional[Dict[str, Any]]) -> SessionManager:
        if project:
            return SessionManager(project["key"], self.projects.memory_dir(project["key"]))
        return SessionManager("_unassigned", self.projects.memory_root / "projects" / "_unassigned")

    def _active_session_id(self) -> Optional[int]:
        s = self.sessions.active()
        return s["id"] if s else None

    def _make_registry(self, gh_tools: Optional[GitHubTools], mem: MemoryStore, skills: SkillLibrary) -> ToolRegistry:
        registry = ToolRegistry()
        if gh_tools is not None:
            register_github_tools(registry, gh_tools)
        register_memory_tools(registry, mem)
        register_skill_tools(registry, skills)
        register_web_tools(registry)
        workspace = config.WORKSPACE_DIR / str((self.projects.active() or {}).get("key") or "_unassigned")
        register_fs_tools(registry, workspace)
        register_code_tools(registry, workspace)
        register_builtin_tools(registry)
        self.projects.register_project_tools(registry)
        # دروازهٔ تایید انسانی + ممیزی + پروژهٔ فعال
        registry.approvals = self.approvals
        registry.audit = self.audit
        registry.approval_policy = self._approval_policy
        registry.project_provider = lambda: (self.projects.active() or {}).get("id")
        registry.on_approval_requested = self._on_approval_requested
        return registry

    def _on_approval_requested(self, pend: Dict[str, Any]) -> None:
        """کارت/اعلان تایید انسانی در UI (از درون گیتِ رجیستری)."""
        self.notifications.push(
            "approval", f"⚠️ تایید لازم است: {pend.get('tool')}",
            body=json.dumps(pend.get("arguments", {}), ensure_ascii=False)[:200],
        )
        self.bus.emit("approval_requested", **pend)

    def _approval_policy(self, tool_name: str) -> bool:
        """سیاست تایید: پروژه با allow_dangerous=true از دروازه عبور می‌کند."""
        p = self.projects.active() or {}
        settings = p.get("settings") or {}
        return not bool(settings.get("allow_dangerous"))

    def _build_engine(self, session_id: Optional[int]) -> AgentEngine:
        """موتور جدید برای یک سشن — حافظه/مهارت مشترک پروژه، رجیستری مستقل."""
        engine = AgentEngine(
            brain=self.brain,
            memory=self.memory,
            skills=self.skills,
            event_bus=self.bus,
            usage=self.usage,
            checkpoints=self.checkpoints,
        )
        engine.set_registry(self._make_registry(self.gh_tools, self.memory, self.skills))
        if "delegate_subtask" not in engine.registry.names():
            engine.register_delegate_tool()  # بدون این، زیرایجنت فقط در CLI بود (باگ)
        p = self.projects.active()
        engine.set_project({"id": p["id"], "name": p["name"], "key": p["key"],
                            "persona": p.get("persona", "")} if p else {})
        engine.session_id = session_id
        # ارث‌بری مغز: پس از سوییچ پروژه/سشن، مغزِ تزریق‌شده (دیف‌سیک یا تست) حفظ شود
        if engine.brain is None:
            prev = getattr(self, "engine", None)
            if prev is not None and prev is not engine and prev.brain is not None:
                engine.brain = prev.brain
                engine.enable_streaming()
        return engine

    def _pub(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        return {k: rec[k] for k in ("id", "name", "created", "last_used", "task_count", "persona", "settings") if k in rec}

    def _sync_project_info(self) -> None:
        p = self.projects.active()
        self.engine.set_project(
            {"id": p["id"], "name": p["name"], "key": p["key"], "persona": p.get("persona", "")} if p else {}
        )

    # ------------------------------------------------------------------
    # مدیریت پروژه‌ها (مثل v3 + persona/settings)
    # ------------------------------------------------------------------

    def ensure_active_project(self) -> Dict[str, Any]:
        p = self.projects.active()
        if p:
            return p
        rec = self.projects.create()
        self._apply_active_project()
        self.bus.emit("project_created", project=self._pub(rec))
        self.bus.emit("project_switched", project=self._pub(rec))
        return rec

    def create_project(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "نام پروژه را وارد کن"}
        rec = self.projects.create(name)
        self._apply_active_project()
        self.audit.append("project_created", {"name": name}, project=rec["id"])
        self.bus.emit("project_created", project=self._pub(rec))
        self.bus.emit("project_switched", project=self._pub(rec))
        return {"ok": True, "project": self._pub(rec)}

    def select_project(self, project_id: int) -> Dict[str, Any]:
        res = self.projects.select(int(project_id))
        if not res.get("ok"):
            return res
        rec = res["project"]
        self._apply_active_project()
        self.audit.append("project_switched", {"name": rec["name"]}, project=rec["id"])
        self.bus.emit("project_switched", project=self._pub(rec))
        return {"ok": True, "project": self._pub(rec)}

    def delete_project(self, project_id: int) -> Dict[str, Any]:
        res = self.projects.delete(int(project_id))
        if not res.get("ok"):
            return res
        self._apply_active_project()
        self.audit.append("project_deleted", {"name": res["deleted"]["name"]}, project=int(project_id))
        self.bus.emit("project_deleted", deleted=self._pub(res["deleted"]), projects=self.projects_list())
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return {"ok": True, "deleted": self._pub(res["deleted"])}

    def rename_project(self, project_id: int, name: str) -> Dict[str, Any]:
        res = self.projects.rename(int(project_id), name)
        if not res.get("ok"):
            return res
        self._sync_project_info()
        rec = self.projects.get(int(project_id))
        self.audit.append("project_renamed", {"new": name}, project=int(project_id))
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return {"ok": True, "project": self._pub(rec)}

    def set_persona(self, project_id: int, persona: str) -> Dict[str, Any]:
        res = self.projects.set_persona(int(project_id), persona)
        if not res.get("ok"):
            return res
        self._sync_project_info()
        self._session_engines.clear()  # همهٔ موتورهای سشن persona جدید بگیرند
        self.audit.append("persona_set", {"len": len(persona)}, project=int(project_id))
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return res

    def set_project_settings(self, project_id: int, settings: Dict[str, Any]) -> Dict[str, Any]:
        res = self.projects.set_settings(int(project_id), settings)
        if not res.get("ok"):
            return res
        self._apply_active_project()  # رجیستری با سیاست جدید بازساخته شود
        self.audit.append("project_settings", settings, project=int(project_id))
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return res

    _apply_lock = threading.Lock()  # سوییچ همزمان پروژه از چند request → صف

    def _apply_active_project(self) -> None:
        """بازسازی موتور/حافظه/رجیستری برای پروژهٔ فعال — زیر قفل: هشت request
        همزمان بدون قفل، set_registryهای هم‌پوشان → 500 و createهای گم‌شده."""
        with type(self)._apply_lock:
            self._apply_active_project_locked()

    def _apply_active_project_locked(self) -> None:
        """سوییچ کامل: حافظه/مهارت/سشن‌ها/موتورها/رجیستری به پروژهٔ فعال."""
        p = self.projects.active()
        self.memory = self._memory_for(p)
        self.skills = self._skills_for(p)
        self.sessions = self._sessions_for(p)
        self._session_engines = {}
        self.engine = self._build_engine(self._active_session_id())
        self.bus.emit("memory_updated", counts=self.memory.counts())

    def projects_list(self) -> List[Dict[str, Any]]:
        return [self._pub(p) for p in self.projects.list()]

    def _active_pub(self) -> Optional[Dict[str, Any]]:
        p = self.projects.active()
        return self._pub(p) if p else None

    # ------------------------------------------------------------------
    # نصب کلاینت گیت‌هاب (توکن Device Flow) + محافظت نرخ
    # ------------------------------------------------------------------

    def _install_github(self, token: str, persist: bool = True) -> Dict[str, Any]:
        """نصب کلاینت گیت‌هاب با توکنِ «ورود با مرورگر» (Device Flow) + بازسازی ابزارها.

        persist=True (پیش‌فرض): توکن در فایل محلی با دسترسیٔ ۶۰۰ ذخیره می‌شود تا
        بعد از بستن ترمینال/ری‌استارت هم لاگین بماند؛ «خروج» آن را کامل پاک می‌کند."""
        gh = GitHubTools(token, api_base=config.GH_API_BASE)
        try:
            info = gh.validate_token()
        except TokenInvalidError:
            return {"ok": False, "invalid": True,
                    "error": "نشست نامعتبر است (401 از GitHub) — دوباره «ورود با مرورگر» را بزن."}
        except GhApiError as e:
            return {"ok": False, "error": f"خطای API گیت‌هاب ({e.status}) — بعداً تلاش کن."}
        harden_github_tools(gh, self.rate_limiter, max_attempts=config.RETRY_MAX_ATTEMPTS)
        self.gh_tools = gh
        self.token_info = info
        self._rebuild_registries()
        self.audit.append("github_token_set", {"login": info["login"]})
        if persist:
            self._save_gh_session(token, info)
        self.bus.emit("token_state", set=True, login=info["login"], scopes=info["token_scopes"])
        return {"ok": True, "login": info["login"], "scopes": info["token_scopes"]}

    # ------------------------------------------------------------------
    # سشن ماندگار گیت‌هاب — ذخیره / بازیابی خودکار / خروج
    # ------------------------------------------------------------------

    def _save_gh_session(self, token: str, info: Dict[str, Any]) -> None:
        """ذخیرهٔ توکن در فایل محلی با دسترسیٔ ۶۰۰ — فقط روی همین دستگاه.

        خطای نوشتن هرگز اتصالِ همین اجرا را خراب نمی‌کند (فقط ماندگاری از دست می‌رود)."""
        if not config.GH_SESSION_PERSIST:
            return
        try:
            data = json.dumps({
                "token": token,
                "login": info.get("login"),
                "scopes": info.get("token_scopes"),
                "saved_at": int(time.time()),
            }, ensure_ascii=False)
            config.GH_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.GH_SESSION_FILE.write_text(data, encoding="utf-8")
            import os as _os
            _os.chmod(config.GH_SESSION_FILE, 0o600)
        except OSError:
            pass

    def _gh_session_token(self) -> Optional[str]:
        """توکن ذخیره‌شده در فایل سشن (اگر معتبرِ شکلی باشد)."""
        try:
            data = json.loads(config.GH_SESSION_FILE.read_text(encoding="utf-8"))
            tok = str(data.get("token") or "")
            return tok if tok.startswith(("ghp_", "github_pat_", "gho_")) else None
        except (OSError, ValueError):
            return None

    def restore_github_session(self) -> None:
        """بازیابی خودکار لاگین گیت‌هاب از فایل سشن در شروع سرور.

        توکن منقضی‌شده (401) → فایل پاک می‌شود؛ خطای شبکه → فایل نگه داشته
        می‌شود تا اجرای بعدی دوباره تلاش کند. در پس‌زمینه صدا زده می‌شود تا
        بوتِ سرور را کند یا بلاک نکند."""
        if not config.GH_SESSION_PERSIST or self.gh_tools is not None:
            return
        token = self._gh_session_token()
        if not token:
            return
        res = self._install_github(token, persist=False)
        if res.get("ok"):
            self.audit.append("github_session_restored", {"login": res.get("login")})
            print(f"✅ نشست گیت‌هاب بازیابی شد ({res.get('login')}) — نیازی به ورود دوباره نیست")
            return
        if res.get("invalid"):
            # ۴۰۱ از خود گیت‌هاب: توکن واقعاً باطل شده، فایل دیگر بی‌فایده است
            try:
                config.GH_SESSION_FILE.unlink()
            except OSError:
                pass
            print("⚠️ نشست ذخیره‌شدهٔ گیت‌هاب منقضی شده بود — پاک شد؛ دوباره «ورود با مرورگر» را بزن")
        else:
            print("⚠️ بازیابی نشست گیت‌هاب موقتاً ممکن نشد (شبکه) — در اجرای بعدی دوباره تلاش می‌شود")

    def github_logout(self) -> Dict[str, Any]:
        """خروج از گیت‌هاب: پاک‌کردن توکن از حافظه + حذف فایل سشن + رویداد UI."""
        was = (self.token_info or {}).get("login")
        self.gh_tools = None
        self.token_info = None
        self._gh_flow = None
        self._rebuild_registries()
        try:
            if config.GH_SESSION_FILE.exists():
                config.GH_SESSION_FILE.unlink()
        except OSError:
            pass
        self.audit.append("github_logout", {"login": was})
        self.bus.emit("token_state", set=False, login=was)
        return {"ok": True, "login": was, "message": "از گیت‌هاب خارج شدی — سشن ذخیره‌شده هم پاک شد."}

    # ------------------------------------------------------------------
    # ورود با مرورگر (Device Flow) — بدون ساخت هیچ کلیدی، مثل مغز دیپ‌سیک
    # ------------------------------------------------------------------

    def github_login_start(self) -> Dict[str, Any]:
        """شروع ورود با مرورگر: کد کوتاه می‌گیرد تا کاربر در مرورگر لاگین‌شده
        تأیید کند. خروجی برای نمایش در UI."""
        from gh_oauth import DeviceFlow, DeviceFlowError
        try:
            flow = DeviceFlow(oauth_base=config.GH_OAUTH_BASE)
            info = flow.start()
        except DeviceFlowError as e:
            return {"ok": False, "error": str(e)}
        self._gh_flow = flow
        return {
            "ok": True,
            "user_code": info["user_code"],
            "verification_uri": info["verification_uri"],
            "expires_in": info["expires_in"],
            "interval": info["interval"],
            "message": f"در مرورگر باز کن: {info['verification_uri']} و کد {info['user_code']} را وارد کن",
        }

    def github_login_poll(self) -> Dict[str, Any]:
        """چک وضعیت ورود با مرورگر؛ اگر تأیید شده باشد کلاینت گیت‌هاب نصب می‌شود.

        جریان فقط روی وضعیت‌های نهایی (authorized/expired/error ساخت‌یافته)
        بسته می‌شود؛ خطاهای گذرای شبکه جریان را زنده نگه می‌دارند تا UI ادامه
        بدهد — تجربهٔ واقعی موبایل: کاربر تبِ گیت‌هاب را باز می‌کند، تبِ اپ فریز
        یا شبکه لحظه‌ای قطع می‌شود؛ نباید یک خطای گذرا کل ورود را بکشد.
        پاسخ‌های pending/slow_down هم «interval» لازم را برمی‌گردانند تا UI
        با ضرب‌آهنگ مجاز گیت‌هاب پول بزند (پول زودتر از interval = slow_down)."""
        flow = self._gh_flow
        if flow is None:
            return {"ok": False, "status": "none",
                    "error": "جلسهٔ ورود پیدا نشد (سرور ری‌استارت شده؟) — دوباره «ورود با مرورگر» را بزن."}
        from gh_oauth import DeviceFlowError
        try:
            res = flow.poll()
        except DeviceFlowError as e:
            # خطای گذرا (شبکه/پروکسی/مهلت) — device_code در گیت‌هاب معتبر مانده؛ جریان زنده می‌ماند
            return {"ok": False, "status": "network_error", "error": short_error(e),
                    "interval": max(5.0, flow.interval)}
        status = res.get("status")
        if status == "authorized":
            token = res.get("token") or ""
            self._gh_flow = None
            install = self._install_github(token)
            if install.get("ok"):
                return {"ok": True, "status": "authorized", "login": install.get("login")}
            return {"ok": False, "status": "error", "error": install.get("error")}
        if status in ("pending", "slow_down"):
            out = {"ok": True, "status": "pending", "user_code": flow.user_code,
                   "verification_uri": flow.verification_uri, "interval": flow.interval}
            if status == "slow_down":
                out["message"] = "گیت‌هاب گفت آهسته‌تر — فاصلهٔ پول‌ها بیشتر شد"
            return out
        if status == "expired":
            self._gh_flow = None
            return {"ok": False, "status": "expired", "error": "کد ورود منقضی شد — دوباره «ورود با مرورگر» را بزن."}
        self._gh_flow = None
        return {"ok": False, "status": "error", "error": res.get("error", "خطای ناشناختهٔ Device Flow")}

    def _rebuild_registries(self) -> None:
        """رجیستری همهٔ موتورهای زنده با ابزارهای گیت‌هاب تازه بازساخته شود."""
        self.engine.set_registry(self._make_registry(self.gh_tools, self.memory, self.skills))
        for eng in self._session_engines.values():
            eng.set_registry(self._make_registry(self.gh_tools, self.memory, self.skills))

    # ------------------------------------------------------------------
    # connector دیپ‌سیک (مثل v3 + استریم جزئی)
    # ------------------------------------------------------------------

    def connect_deepseek(self, visible_login: bool = False) -> None:
        def connector() -> None:
            with self._connector_lock:
                self.deepseek_started = False
                self.deepseek_ready = False
                # اولویت مغز API: اگر کلید API هست (حافظه یا .env) مرورگر اصلاً باز نمی‌شود —
                # روی ترموکس/اندروید کرومیوم وجود ندارد و روی PC هم API سبک‌تر است
                if self._api_key_mem or config.AI_API_KEY:
                    try:
                        self._apply_api_brain()
                        return
                    except APIError as e:
                        self.deepseek_message = short_error(e)
                        self.bus.emit("deepseek_state", ready=False, error=self.deepseek_message)
                        return
                # اتصالِ سالمِ موجود → بازراه‌اندازی نکن؛ reconnect در حین تسک،
                # مرورگر را وسط کار می‌کشت و deepseek_ready را بی‌دلیل False می‌کرد
                if not visible_login and self.client is not None and self.deepseek_ready:
                    self.bus.emit("deepseek_state", ready=True, message="اتصال از قبل برقرار است")
                    return
                try:
                    if self.client is not None:
                        self.client.close()
                except Exception:
                    pass
                self.client = DeepSeekClient(
                    user_data_dir=config.DEEPSEEK_USER_DATA_DIR,
                    url=config.DEEPSEEK_URL,
                    # اتصال پس‌زمینه روی لینوکسِ بدون نمایشگر (سرور/ترموکس بدون X11)
                    # خودکار headless می‌شود — پنجره‌ای برای بازشدن وجود ندارد
                    headless=(not visible_login)
                    and (config.DEEPSEEK_HEADLESS or auto_headless_background()),
                    response_timeout=config.RESPONSE_TIMEOUT,
                    poll_interval=config.RESPONSE_POLL_INTERVAL,
                    stable_checks=config.RESPONSE_STABLE_CHECKS,
                )
                try:
                    self.deepseek_message = "در حال باز کردن مرورگر…"
                    self.bus.emit("deepseek_state", ready=False, message=self.deepseek_message)
                    self.client.start()
                    self.deepseek_started = True
                except Exception as e:
                    self.deepseek_message = f"خطا در راه‌اندازی مرورگر: {short_error(e)}"
                    self.bus.emit("deepseek_state", ready=False, error=self.deepseek_message)
                    return

                if self.client.is_logged_in():
                    self._mark_ready()
                    return
                self.deepseek_message = (
                    "لاگین دیپ‌سیک لازم است — در پنجرهٔ مرورگر باز‌شده وارد اکانت شو"
                    if visible_login else
                    "لاگین دیپ‌سیک لازم است — دکمهٔ «ورود دستی» را بزن تا پنجرهٔ مرئی باز شود"
                )
                self.bus.emit("deepseek_state", ready=False, login_required=True, message=self.deepseek_message)
                try:
                    self.client.ensure_logged_in(wait_timeout=900)
                    self._mark_ready()
                except Exception as e:
                    self.deepseek_message = f"ورود انجام نشد: {short_error(e)}"
                    self.bus.emit("deepseek_state", ready=False, login_required=True, message=self.deepseek_message)

        threading.Thread(target=connector, name="deepseek-connector", daemon=True).start()

    def _mark_ready(self) -> None:
        # اگر کاربر در حین راه‌اندازیِ کُند مرورگر، مغز API فعال کرده، همان می‌ماند —
        # اتصال دیرهنگام مرورگر نباید مغزِ فعال را بازنویسی کند
        if self.brain is not None and getattr(self.brain, "is_api", False):
            try:
                if self.client is not None:
                    self.client.close()
                    self.client = None
            except Exception:
                pass
            return
        self.deepseek_ready = True
        self.deepseek_message = "اتصال برقرار است"
        self.brain = DeepSeekBrain(self.client)
        self.engine.brain = self.brain
        self.engine.enable_streaming()
        for eng in self._session_engines.values():
            eng.brain = self.brain
            eng.enable_streaming()
        self.bus.emit("deepseek_state", ready=True, message=self.deepseek_message)

    def _apply_api_brain(self, model: Optional[str] = None) -> None:
        """ساخت مغز API و روشن‌کردن آن روی موتور اصلی و همهٔ سشن‌ها."""
        base = self._api_base or config.AI_BASE_URL
        key = self._api_key_mem or config.AI_API_KEY
        mdl = model or self._api_model or config.AI_MODEL
        self.brain = APIBrain(base_url=base, api_key=key, model=mdl, timeout=config.AI_TIMEOUT)
        self.brain_kind = "api"
        self.deepseek_started = True
        self.deepseek_ready = True
        self.deepseek_message = f"مغز API فعال ({mdl}) — بدون مرورگر"
        self.engine.brain = self.brain
        self.engine.enable_streaming()
        for eng in self._session_engines.values():
            eng.brain = self.brain
            eng.enable_streaming()
        self.bus.emit("deepseek_state", ready=True, message=self.deepseek_message, kind="api")

    def connect_ai(self, api_key: str, base_url: str = "", model: str = "") -> Dict[str, Any]:
        """تنظیم کلید API از UI — اعتبارسنجی با GET /models و فعال‌سازی فوری.

        کلید فقط در حافظهٔ همین اجرا نگه داشته می‌شود (مثل توکن گیت‌هاب)؛
        برای ماندگاری، کاربر می‌تواند آن را در .env بگذارد.
        """
        key = (api_key or "").strip()
        if not key:
            return {"ok": False, "error": "کلید API خالی است — کلید سرویس (مثلاً sk-...) را بده."}
        base = (base_url or "").strip() or self._api_base or config.AI_BASE_URL
        mdl = (model or "").strip() or self._api_model or config.AI_MODEL
        probe = APIBrain(base_url=base, api_key=key, model=mdl, timeout=config.AI_TIMEOUT)
        try:
            models = probe.validate()
        except APIError as e:
            return {"ok": False, "error": str(e)}
        self._api_key_mem = key
        self._api_base = base.rstrip("/")
        self._api_model = mdl
        try:
            self._apply_api_brain()
        except APIError as e:
            return {"ok": False, "error": str(e)}
        self.audit.append("api_brain_set", {"model": mdl, "models_count": len(models)})
        return {"ok": True, "model": mdl, "models_count": len(models)}

    def api_status(self) -> Dict[str, Any]:
        key = self._api_key_mem or config.AI_API_KEY
        masked = (key[:5] + "…" + key[-4:]) if len(key) > 11 else ("" if not key else "…")
        return {
            "ok": True,
            "kind": self.brain_kind,
            "ready": self.deepseek_ready,
            "model": self._api_model or (config.AI_MODEL if config.AI_API_KEY else ""),
            "base_url": self._api_base or config.AI_BASE_URL,
            "key_masked": masked if (self._api_key_mem or config.AI_API_KEY) else "",
        }

    # ------------------------------------------------------------------
    # سشن‌ها و اجرای تسک (چندسشن موازی)
    # ------------------------------------------------------------------

    def _engine_for(self, session_id: Optional[int]) -> AgentEngine:
        sid = session_id or self._active_session_id()
        if sid == self._active_session_id() and self.engine.session_id == sid:
            return self.engine
        if sid not in self._session_engines:
            eng = self._build_engine(sid)
            self._session_engines[sid] = eng
        return self._session_engines[sid]

    def sessions_list(self) -> Dict[str, Any]:
        return {"sessions": self.sessions.list(), "active_id": self.sessions.active_id()}

    def create_session(self, title: str = "") -> Dict[str, Any]:
        s = self.sessions.create(title)
        self.audit.append("session_created", {"title": s["title"]})
        self.bus.emit("sessions_updated", **self.sessions_list())
        return {"ok": True, "session": s}

    def select_session(self, session_id: int) -> Dict[str, Any]:
        s = self.sessions.select(int(session_id))
        if "error" in s:
            return {"ok": False, "error": s["error"]}
        self.bus.emit("sessions_updated", **self.sessions_list())
        return {"ok": True, "session": s}

    def rename_session(self, session_id: int, title: str) -> Dict[str, Any]:
        res = self.sessions.rename(int(session_id), title)
        if res.get("ok"):
            self.bus.emit("sessions_updated", **self.sessions_list())
        return res

    def delete_session(self, session_id: int) -> Dict[str, Any]:
        res = self.sessions.delete(int(session_id))
        if res.get("ok"):
            self._session_engines.pop(int(session_id), None)
            self.bus.emit("sessions_updated", **self.sessions_list())
        return res

    def start_task(self, task: str, session_id: Optional[int] = None) -> Dict[str, Any]:
        task = (task or "").strip()
        if not task:
            return {"ok": False, "error": "متن تسک خالی است"}
        if not self.deepseek_ready and self.engine.brain is None:
            return {"ok": False, "error": self.deepseek_message or "دیپ‌سیک هنوز آماده نیست"}

        project = self.ensure_active_project()
        if session_id is not None:
            sel = self.sessions.select(int(session_id))
            if "error" in sel:
                return {"ok": False, "error": "سشن یافت نشد"}
        engine = self._engine_for(self._active_session_id())
        if engine.brain is None:
            engine.brain = self.engine.brain  # مغز تازه‌وصل‌شده به سشن‌های قبلی هم برسد
        if engine.brain is None:
            return {"ok": False, "error": self.deepseek_message or "دیپ‌سیک هنوز آماده نیست"}
        if engine.busy:
            return {"ok": False, "error": "یک تسک در این سشن در حال اجراست — صبر کن تمام شود"}
        sid = engine.session_id
        proj_key = project["key"]

        def worker() -> None:
            try:
                with self.brain_sem:  # مغز مشترک → اجرای نوبتی
                    report = engine.run_task(task)
                self.projects.record_task(proj_key)
                self.sessions.record_task(sid)
                return report
            except Exception as e:  # noqa: BLE001
                self.bus.emit("task_end", ok=False, answer=f"⛔ خطای موتور: {e}", rounds=0, tools=[], errors=1, duration_s=0)

        threading.Thread(target=worker, name="task-worker", daemon=True).start()
        self.audit.append("task_started", {"task": task[:160], "session": sid}, project=project["id"])
        return {"ok": True, "started": True, "project": self._pub(project), "session_id": sid}

    # ------------------------------------------------------------------
    # تایید انسانی
    # ------------------------------------------------------------------

    def resolve_approval(self, approval_id: str, approved: bool) -> Dict[str, Any]:
        res = self.approvals.resolve(str(approval_id), bool(approved))
        if res.get("ok"):
            item = res["item"]
            self.audit.append("approval_resolved", {"approval_id": approval_id, "approved": bool(approved), "tool": item["tool"]})
            self.notifications.push(
                "approval", f"{'✅ تایید شد' if approved else '⛔ رد شد'}: {item['tool']}",
                body=f"درخواست {approval_id}", project=item.get("project"),
            )
            self.bus.emit("approval_resolved", approval_id=approval_id, approved=bool(approved), tool=item["tool"])
        return res

    # ------------------------------------------------------------------
    # اتوماسیون: زمان‌بند + نگهبان + اعلان‌ها
    # ------------------------------------------------------------------

    def _make_registry_for(self, gh_tools, mem, skills, rec) -> ToolRegistry:
        """رجیستری برای پروژهٔ دلخواه (نه لزوماً فعال) — سیاست تایید همان پروژه."""
        registry = ToolRegistry()
        if gh_tools is not None:
            register_github_tools(registry, gh_tools)
        register_memory_tools(registry, mem)
        register_skill_tools(registry, skills)
        register_web_tools(registry)
        register_fs_tools(registry, config.WORKSPACE_DIR / str(rec["key"]))
        register_code_tools(registry, config.WORKSPACE_DIR / str(rec["key"]))
        register_builtin_tools(registry)
        self.projects.register_project_tools(registry)
        registry.approvals = self.approvals
        registry.audit = self.audit
        registry.project_provider = lambda: rec["id"]
        allow = bool((rec.get("settings") or {}).get("allow_dangerous"))
        registry.approval_policy = lambda name: not allow
        registry.on_approval_requested = self._on_approval_requested
        return registry

    def _ephemeral_run(self, project_id: int, task: str, source: str) -> None:
        """اجرای تسک در پس‌زمینه برای پروژهٔ مشخص (زمان‌بند/نگهبان)."""
        rec = self.projects.get(int(project_id))
        if "error" in rec:
            self.notifications.push("system", f"پروژهٔ {project_id} برای {source} یافت نشد")
            return
        if not self.deepseek_ready or self.brain is None:
            self.notifications.push("system", f"دیپ‌سیک آماده نیست — {source} اجرا نشد", body=task[:120], project=rec["id"])
            return

        mem = MemoryStore.open(memory_dir=self.projects.memory_dir(rec["key"]))
        sk = SkillLibrary(skills_dir=self.projects.skills_dir(rec["key"]))
        eng = AgentEngine(brain=self.brain, memory=mem, skills=sk, event_bus=self.bus,
                          usage=self.usage, checkpoints=self.checkpoints)
        eng.set_registry(self._make_registry_for(self.gh_tools, mem, sk, rec))
        if "delegate_subtask" not in eng.registry.names():
            eng.register_delegate_tool()
        eng.set_project({"id": rec["id"], "name": rec["name"], "key": rec["key"], "persona": rec.get("persona", "")})

        def worker() -> None:
            try:
                if not self.brain_sem.acquire(blocking=False):
                    self.notifications.push("system", f"مغز مشغول است — {source} این‌بار اجرا نشد", project=rec["id"])
                    return
                try:
                    report = eng.run_task(task)
                finally:
                    self.brain_sem.release()
                self.projects.record_task(rec["key"])
                self.notifications.push(
                    "schedule" if source == "schedule" else "watch",
                    f"{'⏰' if source == 'schedule' else '👀'} تسک خودکار تمام شد ({rec['name']})",
                    body=(report.get("answer") or "")[:180], project=rec["id"],
                )
            except Exception as e:  # noqa: BLE001
                self.notifications.push("system", f"خطای اجرای خودکار: {e}", project=rec["id"])

        threading.Thread(target=worker, name=f"auto-{source}", daemon=True).start()

    def _on_job_fire(self, job: Dict[str, Any]) -> None:
        self.audit.append("schedule_fired", {"job": job["name"], "task": job["task"][:120]}, project=job["project_id"])
        self.notifications.push("schedule", f"⏰ اجرای خودکار: {job['name']}", body=job["task"][:160], project=job["project_id"])
        # رویداد زنده برای UI (قبلاً فقط watch_changed emit می‌شد و case این رویداد در UI مرده بود)
        self.bus.emit("schedule_fired", job=job["name"], task=job["task"][:160], project_id=job["project_id"])
        self._ephemeral_run(job["project_id"], job["task"], "schedule")

    def _on_watch_change(self, watch: Dict[str, Any], change: Dict[str, Any]) -> None:
        desc = json.dumps(change.get("to", change), ensure_ascii=False)[:200]
        self.audit.append("watch_changed", {"repo": watch["repo"], "change": desc}, project=watch["project_id"])
        self.notifications.push("watch", f"👀 تغییر در {watch['repo']}", body=desc, project=watch["project_id"])
        self.bus.emit("watch_changed", repo=watch["repo"], change=change)
        if watch.get("auto_task"):
            task = str(watch["auto_task"]).replace("{{change}}", desc)
            self._ephemeral_run(watch["project_id"], task, "watch")

    # ------------------------------------------------------------------
    # Snapshot / Export-Import / Resume / جستجو / خط زمانی
    # ------------------------------------------------------------------

    def snapshot_create(self, project_id: Optional[int], label: str) -> Dict[str, Any]:
        rec = self.projects.get(int(project_id)) if project_id else self.projects.active()
        if not rec or "error" in rec:
            return {"ok": False, "error": "پروژه یافت نشد"}
        extra = {"_project.json": json.dumps(self._pub(rec), ensure_ascii=False)}
        res = self.timemachine.create(rec["key"], self.projects.memory_dir(rec["key"]),
                                      self.projects.skills_dir(rec["key"]), label=label, extra_files=extra)
        if res.get("ok"):
            self.audit.append("snapshot_created", {"label": label}, project=rec["id"])
            self.bus.emit("snapshot_created", project=rec["id"], label=label)
        return res

    def snapshot_restore(self, project_id: Optional[int], snapshot_id: str) -> Dict[str, Any]:
        rec = self.projects.get(int(project_id)) if project_id else self.projects.active()
        if not rec or "error" in rec:
            return {"ok": False, "error": "پروژه یافت نشد"}
        # موتورِ در حال کار با وضعیت pre-restore در حافظه‌اش، restore را بازنویسی می‌کرد
        if getattr(self.engine, "busy", False):
            return {"ok": False, "error": "تسکی در حال اجراست — بعد از پایان تسک snapshot را برگردان"}
        mdir = self.projects.memory_dir(rec["key"])
        sdir = self.projects.skills_dir(rec["key"])
        # قبل از رول‌بک، وضعیت فعلی هم snapshot شود (بازگشت دوباره ممکن بماند)
        self.timemachine.create(rec["key"], mdir, sdir, label="auto-before-restore")
        res = self.timemachine.restore(rec["key"], str(snapshot_id), mdir, sdir)
        if res.get("ok"):
            self.audit.append("snapshot_restored", {"snapshot": snapshot_id}, project=rec["id"])
            if rec.get("id") == (self.projects.active() or {}).get("id"):
                self._apply_active_project()
            self.bus.emit("snapshot_restored", project=rec["id"], snapshot=snapshot_id)
        return res

    def project_export(self, project_id: Optional[int]) -> Dict[str, Any]:
        rec = self.projects.get(int(project_id)) if project_id else self.projects.active()
        if not rec or "error" in rec:
            return {"ok": False, "error": "پروژه یافت نشد"}
        res = bundles.export_project(rec, self.projects.memory_dir(rec["key"]),
                                     self.projects.skills_dir(rec["key"]), config.EXPORTS_DIR)
        if res.get("ok"):
            self.audit.append("project_exported", {"file": res["name"]}, project=rec["id"])
        return res

    def project_import(self, zip_name: str) -> Dict[str, Any]:
        name = str(zip_name).strip()
        # اعتبارسنجی قبل از ساخت مسیر (پاس ۶: نام غول‌پیکر → Errno 36؛ مسیردار → پیمایش)
        if not name or len(name) > 200 or "/" in name or "\\" in name or ".." in name:
            return {"ok": False, "error": "نام بسته نامعتبر است"}
        path = (config.EXPORTS_DIR / name).resolve()
        if config.EXPORTS_DIR.resolve() not in path.parents or not path.exists():
            return {"ok": False, "error": "فایل بسته در exports یافت نشد"}
        res = bundles.import_project(path, self.projects, self.projects.memory_root, self.projects.skills_root)
        if res.get("ok"):
            self._apply_active_project()
            self.audit.append("import_done", {"name": path.name}, project=res["project"]["id"])
            self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return res

    def resume_task(self, checkpoint_id: str) -> Dict[str, Any]:
        cp = self.checkpoints.load(str(checkpoint_id))
        if not cp:
            return {"ok": False, "error": "چک‌پوینت یافت نشد"}
        engine = self._engine_for(self._active_session_id())
        if engine.brain is None:
            return {"ok": False, "error": self.deepseek_message or "دیپ‌سیک هنوز آماده نیست"}
        proj_id = (cp.get("project") or {}).get("id")
        if proj_id and proj_id != (self.projects.active() or {}).get("id"):
            sel = self.select_project(int(proj_id))
            if not sel.get("ok"):
                return sel
        engine = self._engine_for(self._active_session_id())
        if engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست"}

        def worker() -> None:
            try:
                with self.brain_sem:
                    engine.resume(str(checkpoint_id))
            except Exception as e:  # noqa: BLE001
                self.bus.emit("task_end", ok=False, answer=f"⛔ خطای resume: {e}", rounds=0, tools=[], errors=1, duration_s=0)

        threading.Thread(target=worker, name="task-resume", daemon=True).start()
        return {"ok": True, "resuming": checkpoint_id}

    def global_search(self, query: str, kind: Optional[str] = None, project_id: Optional[int] = None) -> Dict[str, Any]:
        return globalsearch.search_all(self.projects.list(), self.projects.memory_root,
                                       self.projects.skills_root, query, kind=kind, project_id=project_id)

    def build_timeline(self, project_id: Optional[int], limit: int = 80) -> Dict[str, Any]:
        rec = self.projects.get(int(project_id)) if project_id else self.projects.active()
        if not rec or "error" in rec:
            return {"ok": False, "error": "پروژه یافت نشد"}
        events = timeline.build_timeline(
            rec["key"], self.projects.memory_dir(rec["key"]), self.projects.skills_dir(rec["key"]),
            snapshots=self.timemachine.list(rec["key"]),
            audit_entries=self.audit.list(limit=200, project=rec["id"]),
            jobs=self.scheduler.list(project_id=rec["id"]),
            limit=limit,
        )
        return {"ok": True, "project": self._pub(rec), "events": events}

    # ------------------------------------------------------------------
    # رویدادهای موتور → ممیزی + اعلان تایید
    # ------------------------------------------------------------------

    def _on_engine_event(self, event: str, item: Dict[str, Any]) -> None:
        try:
            if event in ("task_start", "task_end", "final", "skill_learned", "reflection"):
                proj = (item.get("project") or {}).get("id")
                self.audit.append(event, {k: item.get(k) for k in ("task", "ok", "answer", "rounds", "name", "text") if k in item}, project=proj)
            if event == "chat_rollover":
                # چرخش خودکار چت — رویداد مهم، در ممیزی + اعلان
                proj = (item.get("project") or {}).get("id")
                self.audit.append("chat_rollover", {
                    "reason": item.get("reason"), "est_tokens_before": item.get("est_tokens_before"),
                    "rollover_no": item.get("rollover_no"),
                }, project=proj)
                self.notifications.push(
                    "system",
                    f"🔄 چت جدید ساخته شد (چرخش #{item.get('rollover_no')})",
                    body=f"سقف context: تخمین {item.get('est_tokens_before')} توکن — راهنمای پروژه منتقل شد، ادامه از همان نقطه",
                    project=proj,
                )
            if event == "tool_result" and item.get("pending"):
                pend = item["pending"]
                self.notifications.push(
                    "approval", f"⚠️ تایید لازم است: {pend.get('tool')}",
                    body=json.dumps(pend.get("arguments", {}), ensure_ascii=False)[:200],
                )
                self.bus.emit("approval_requested", **pend)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # snapshot وضعیت
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        pend = self.approvals.pending()
        return {
            "ok": True,  # یکدستی با بقیهٔ اندپوینت‌ها (پاس ۳)
            "token": {
                "set": self.token_info is not None,
                "login": (self.token_info or {}).get("login"),
                "scopes": (self.token_info or {}).get("token_scopes"),
            },
            "deepseek": {
                "started": self.deepseek_started,
                "ready": self.deepseek_ready,
                "message": self.deepseek_message,
            },
            "task": {"busy": self.engine.busy},
            "project": {
                "active": self._active_pub(),
                "list": self.projects_list(),
                "next_default_name": self.projects.next_default_name(),
            },
            "sessions": self.sessions_list(),
            "notifications": {"unread": self.notifications.unread_count()},
            "approvals": {"pending": len(pend), "items": pend[:10]},
            "audit": {"verify": self.audit.verify()},
            "usage": {"totals": self.usage.summary(days=14)["totals"]},
            "automation": {"jobs": len(self.scheduler.list()), "watches": len(self.watcher.list())},
            "memory": {"counts": self.memory.counts(), "stats": self.memory.stats()},
            "skills": self.skills.stats(),
            "tools": len(self.engine._registry) if self.engine._registry else 0,
            "max_rounds": self.engine.max_rounds,
        }

    def github_overview(self) -> Dict[str, Any]:
        if self.gh_tools is None:
            return {"ok": False, "error": "توکن گیت‌هاب تنظیم نشده"}
        user = self.gh_tools.get_authenticated_user()
        repos = self.gh_tools.list_repos(limit=8)
        rate = self.gh_tools.get_rate_limit()
        return {"ok": True, "user": user, "repos": repos.get("repos", []), "rate": rate}


STATE = DevXState()


# ======================================================================
# HTTP Handler
# ======================================================================

class DevXHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------------- helpers ----------------

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, filename: str, content_type: str = "application/zip") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # منفی → read تا EOF (بلاک همیشگی) و غول‌پیکر → اشغال حافظه
            if length <= 0 or length > 8 * 1024 * 1024:
                return {}
            raw = self.rfile.read(length)
            obj = json.loads(raw.decode("utf-8")) if raw else {}
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:  # بی‌سروصدا
        pass

    # ---------------- routes ----------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._do_get()
        except Exception as e:  # noqa: BLE001 — ورودی بد/خطای داخلی نباید اتصال را بی‌پاسخ بگذارد
            try:
                self._send_json({"ok": False, "error": f"خطای سرور: {e}"}, status=500)
            except Exception:
                pass

    def _do_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in ("/", "/ui", "/index.html"):
            self._serve_ui()
        elif path == "/api/state":
            self._send_json(STATE.snapshot())
        elif path == "/api/stream":
            self._serve_sse()
        elif path == "/api/history":
            self._send_json({"events": STATE.bus.history(150)})
        elif path == "/api/memory":
            section = (qs.get("section") or ["all"])[0]
            tag = (qs.get("tag") or [None])[0]
            if section == "tasks":
                self._send_json({"ok": True, "tasks": STATE.memory.recent_tasks(30)})
            elif section == "archived":
                self._send_json({"ok": True, "archived": STATE.memory.archived_items(80)})
            else:
                sections = [section] if section != "all" else ["facts", "lessons", "notes", "reflections"]
                data = {}
                for s in sections:
                    items = STATE.memory.all_items(s)
                    if tag:
                        items = [i for i in items if tag in (i.get("tags") or [])]
                    data[s] = items
                self._send_json({"ok": True, **data})
        elif path == "/api/skills":
            self._send_json({"ok": True, "skills": STATE.skills.list_skills()})
        elif path == "/api/skills/versions":
            self._send_json(STATE.skills.versions((qs.get("name") or [""])[0]))
        elif path == "/api/skills/diff":
            def _int(k):
                try:
                    return int(qs[k][0])
                except Exception:
                    return None
            self._send_json(STATE.skills.diff((qs.get("name") or [""])[0], _int("v1"), _int("v2")))
        elif path == "/api/projects":
            self._send_json({
                "ok": True,
                "projects": STATE.projects_list(),
                "active": STATE._active_pub(),
                "next_default_name": STATE.projects.next_default_name(),
            })
        elif path == "/api/sessions":
            self._send_json({"ok": True, **STATE.sessions_list()})
        elif path == "/api/ai":
            self._send_json(STATE.api_status())
        elif path == "/api/notifications":
            unread = (qs.get("unread") or ["0"])[0] == "1"
            self._send_json({"ok": True, "unread": STATE.notifications.unread_count(),
                             "items": STATE.notifications.list(unread_only=unread, limit=60)})
        elif path == "/api/approvals":
            self._send_json({"ok": True, "pending": STATE.approvals.pending(),
                             "history": STATE.approvals.history(30)})
        elif path == "/api/audit":
            if (qs.get("verify") or ["0"])[0] == "1":
                self._send_json(STATE.audit.verify())
            else:
                self._send_json({"ok": True, "entries": STATE.audit.list(
                    limit=_int_arg((qs.get("limit") or ["60"])[0], 60),
                    project=_int_arg(qs["project"][0]) if "project" in qs else None,
                    event=(qs.get("event") or [None])[0],
                )})
        elif path == "/api/usage":
                self._send_json({"ok": True, **STATE.usage.summary(days=_int_arg((qs.get("days") or ["14"])[0], 14))})
        elif path == "/api/schedule":
            self._send_json({"ok": True, "jobs": STATE.scheduler.list(
                project_id=_int_arg(qs["project"][0]) if "project" in qs else None)})
        elif path == "/api/watch":
            self._send_json({"ok": True, "watches": STATE.watcher.list(
                project_id=_int_arg(qs["project"][0]) if "project" in qs else None)})
        elif path == "/api/search":
            kind = (qs.get("kind") or [None])[0]
            pid = _int_arg(qs["project"][0]) if "project" in qs else None
            self._send_json(STATE.global_search((qs.get("q") or [""])[0], kind=kind, project_id=pid))
        elif path == "/api/timeline":
            pid = _int_arg(qs["project"][0]) if "project" in qs else None
            self._send_json(STATE.build_timeline(pid, limit=_int_arg((qs.get("limit") or ["80"])[0], 80)))
        elif path == "/api/snapshots":
            pid = _int_arg(qs["project"][0]) if "project" in qs else None
            rec = STATE.projects.get(pid) if pid else STATE.projects.active()
            if not rec or "error" in rec:
                self._send_json({"ok": False, "error": "پروژه یافت نشد"})
            else:
                self._send_json({"ok": True, "snapshots": STATE.timemachine.list(rec["key"])})
        elif path == "/api/checkpoints":
            self._send_json({"ok": True, "checkpoints": STATE.checkpoints.list(20)})
        elif path == "/api/exports":
            self._send_json({"ok": True, "exports": bundles.list_exports(config.EXPORTS_DIR)})
        elif path == "/api/exports/download":
            name = unquote((qs.get("file") or [""])[0])
            p = (config.EXPORTS_DIR / name).resolve()
            if config.EXPORTS_DIR.resolve() not in p.parents or not p.exists():
                self._send_json({"ok": False, "error": "فایل یافت نشد"}, status=404)
            else:
                self._send_bytes(p.read_bytes(), p.name)
        elif path == "/api/github/overview":
            try:
                self._send_json(STATE.github_overview())
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        else:
            self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001 — ورودی بد/خطای داخلی نباید اتصال را بی‌پاسخ بگذارد
            try:
                self._send_json({"ok": False, "error": f"خطای سرور: {e}"}, status=500)
            except Exception:
                pass

    def _do_post(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json_body()
        # ورود گیت‌هاب فقط با «ورود با مرورگر» (Device Flow) — مسیر توکن دستی حذف شده
        if path == "/api/task":
            self._send_json(STATE.start_task(
                str(body.get("task", "")),
                session_id=_int_arg(body.get("session_id")) if body.get("session_id") else None,
            ))
        elif path == "/api/projects":
            self._send_json(STATE.create_project(str(body.get("name", ""))))
        elif path == "/api/projects/select":
            self._send_json(_safe_int_call(body, "id", STATE.select_project))
        elif path == "/api/projects/delete":
            self._send_json(_safe_int_call(body, "id", STATE.delete_project))
        elif path == "/api/projects/rename":
            self._send_json(_safe_int_call(body, "id", lambda i: STATE.rename_project(i, str(body.get("name", "")))))
        elif path == "/api/persona":
            self._send_json(_safe_int_call(body, "id", lambda i: STATE.set_persona(i, str(body.get("persona", "")))))
        elif path == "/api/project/settings":
            self._send_json(_safe_int_call(body, "id", lambda i: STATE.set_project_settings(i, {
                "allow_dangerous": bool(body.get("allow_dangerous", False)),
                "tool_groups": body.get("tool_groups") or [],
            })))
        elif path == "/api/sessions":
            self._send_json(STATE.create_session(str(body.get("title", ""))))
        elif path == "/api/sessions/select":
            self._send_json(_safe_int_call(body, "id", STATE.select_session))
        elif path == "/api/sessions/rename":
            self._send_json(_safe_int_call(body, "id", lambda i: STATE.rename_session(i, str(body.get("title", "")))))
        elif path == "/api/sessions/delete":
            self._send_json(_safe_int_call(body, "id", STATE.delete_session))
        elif path == "/api/notifications/read":
            if body.get("id"):
                self._send_json({"ok": STATE.notifications.mark_read(_int_arg(body.get("id"), 0))})
            else:
                self._send_json({"ok": True, "marked": STATE.notifications.mark_all_read()})
        elif path == "/api/approvals/resolve":
            self._send_json(STATE.resolve_approval(str(body.get("id", "")), bool(body.get("approved", False))))
        elif path == "/api/schedule":
            self._send_json(STATE.scheduler.add(
                project_id=_int_arg(body.get("project_id")) or (STATE.projects.active() or {}).get("id") or 0,
                name=str(body.get("name", "job")), task=str(body.get("task", "")),
                spec={"kind": body.get("kind", "interval"), "every_s": body.get("every_s"), "at": body.get("at")},
            ))
        elif path == "/api/schedule/delete":
            self._send_json({"ok": STATE.scheduler.remove(_int_arg(body.get("id"), 0))})
        elif path == "/api/schedule/toggle":
            self._send_json(STATE.scheduler.set_enabled(_int_arg(body.get("id"), 0), bool(body.get("enabled", True))))
        elif path == "/api/watch":
            self._send_json(STATE.watcher.add(
                project_id=_int_arg(body.get("project_id")) or (STATE.projects.active() or {}).get("id") or 0,
                repo=str(body.get("repo", "")),
                events=[str(e) for e in (body.get("events") or ["issues", "prs", "commits"])],
                interval_s=_int_arg(body.get("interval_s"), config.WATCH_INTERVAL_DEFAULT),
                auto_task=str(body.get("auto_task", "")),
            ))
        elif path == "/api/watch/delete":
            self._send_json({"ok": STATE.watcher.remove(_int_arg(body.get("id"), 0))})
        elif path == "/api/snapshots/create":
            pid = _int_arg(body.get("project_id")) if body.get("project_id") else None
            self._send_json(STATE.snapshot_create(pid, str(body.get("label", "snapshot"))))
        elif path == "/api/snapshots/restore":
            pid = _int_arg(body.get("project_id")) if body.get("project_id") else None
            self._send_json(STATE.snapshot_restore(pid, str(body.get("id", ""))))
        elif path == "/api/export":
            pid = _int_arg(body.get("project_id")) if body.get("project_id") else None
            self._send_json(STATE.project_export(pid))
        elif path == "/api/import":
            self._send_json(STATE.project_import(str(body.get("name", ""))))
        elif path == "/api/resume":
            self._send_json(STATE.resume_task(str(body.get("checkpoint_id", ""))))
        elif path == "/api/deepseek/login":
            STATE.connect_deepseek(visible_login=True)
            self._send_json({"ok": True, "message": "پنجرهٔ مرئی برای ورود دستی باز می‌شود"})
        elif path == "/api/deepseek/reconnect":
            STATE.connect_deepseek(visible_login=False)
            self._send_json({"ok": True, "message": "اتصال مجدد آغاز شد"})
        elif path == "/api/ai":
            self._send_json(STATE.connect_ai(
                api_key=str(body.get("key", "")),
                base_url=str(body.get("base_url", "")),
                model=str(body.get("model", "")),
            ))
        elif path == "/api/github/login":
            self._send_json(STATE.github_login_start())
        elif path == "/api/github/poll":
            self._send_json(STATE.github_login_poll())
        elif path == "/api/github/logout":
            self._send_json(STATE.github_logout())
        else:
            self._send_json({"ok": False, "error": "not found"}, status=404)

    # ---------------- UI + SSE ----------------

    def _serve_ui(self) -> None:
        try:
            body = UI_FILE.read_bytes()
        except Exception as e:
            body = f"<h1>DevX UI یافت نشد</h1><p>{e}</p>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        """استریم زندهٔ رویدادها به رابط؛ تاریخچهٔ اخیر + heartbeat هر ۱۵ ثانیه."""
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=800)

        def push(event: str, item: Dict[str, Any]) -> None:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # اشتراک بعد از هدرها: اگر نوشتن هدر به‌خاطر اتصال مرده خطا بدهد،
        # مشترکِ یتیم روی bus نمی‌ماند (سخت‌گیری پاس ۴)
        STATE.bus.subscribe(push)

        def write_event(item: Dict[str, Any]) -> bool:
            try:
                line = f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        try:
            # تاریخچهٔ اخیر با برچسب replay — UI رویدادهای کارتی/توستی را تکرار نکند
            for item in STATE.bus.history(40):
                if not write_event({**item, "replay": True}):
                    return
            write_event({"event": "snapshot", **STATE.snapshot()})
            last_beat = time.time()
            while True:
                try:
                    item = q.get(timeout=2.0)
                    if not write_event(item):
                        return
                except queue.Empty:
                    pass
                if time.time() - last_beat > 15:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    last_beat = time.time()
        finally:
            STATE.bus.unsubscribe(push)


def _safe_int_call(body: Dict[str, Any], key: str, fn) -> Dict[str, Any]:
    try:
        return fn(int(body.get(key, 0)))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"{key} نامعتبر است"}


def _int_arg(value: Any, default: Optional[int] = None) -> Optional[int]:
    """تبدیل امن ورودی کاربر به عدد — ورودی خراب پیش‌فرض برمی‌گردد، نه استثنا.

    (پاس ۶: تا ۵۱ ترکیب fuzz به‌خاطر int() لخت روی query/body به ۵۰۰ می‌رسیدند)
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def serve(open_browser: bool = True) -> ThreadingHTTPServer:
    """ساخت/اجرای سرور DevX؛ آدرس را برمی‌گرداند."""
    server = ThreadingHTTPServer((config.DEVX_HOST, config.DEVX_PORT), DevXHandler)
    server.daemon_threads = True
    url = f"http://{config.DEVX_HOST}:{config.DEVX_PORT}"
    print(f"🚀 DevX v4 روی {url} اجرا شد")
    # بازیابی خودکار نشست گیت‌هاب در پس‌زمینه — شبکه نباید بوت را بلاک کند
    threading.Thread(target=STATE.restore_github_session, name="gh-session-restore", daemon=True).start()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return server
