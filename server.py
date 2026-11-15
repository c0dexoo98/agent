# -*- coding: utf-8 -*-
"""سرور DevX — بک‌اند رابط وب ایجنت (فقط کتابخانهٔ استاندارد، بدون وابستگی).

معماری:
  * ThreadingHTTPServer + SSE (Server-Sent Events) برای استریم زندهٔ رویدادهای
    موتور به رابط (بدون WebSocket و بدون فریم‌ورک).
  * توکن گیت‌هاب از UI دریافت (POST /api/token)، با GET /user اعتبارسنجی و
    فقط در حافظهٔ همین فرایند نگه داشته می‌شود — هرگز لاگ/فایل نمی‌شود.
  * موتور ایجنت (agent_loop.AgentEngine) در نخ پس‌زمینه اجرا می‌شود و همهٔ
    رویدادها (دورها، ابزارها، یادگیری مهارت) زنده به UI استریم می‌شود.
  * DeepSeek: connector پس‌زمینه‌ای مرورگر را راه می‌اندازد و وضعیت لاگین را
    به UI می‌فرستد؛ اگر لاگین نبود، UI پنل چشمک‌زن هشدار می‌دهد و کاربر
    می‌تواند با POST /api/deepseek/login پنجرهٔ مرئی برای ورود دستی باز کند.
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
from urllib.parse import parse_qs, urlparse

from github import GithubException

import config
from agent_loop import AgentEngine, DeepSeekBrain, EventBus
from builtin_tools import register_builtin_tools
from deepseek_client import DeepSeekClient, LoginRequired
from github_tools import GitHubTools, TokenInvalidError, register_github_tools
from memory import MemoryStore, register_memory_tools
from projects import ProjectManager
from skills import SkillLibrary, register_skill_tools
from tool_registry import ToolRegistry

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"


class DevXState:
    """وضعیت مشترک سرور: توکن، مرورگر دیپ‌سیک، موتور، پروژه‌ها، حافظه، مهارت‌ها.

    هر پروژه حافظه/مهارت ایزولهٔ خودش را دارد؛ سوییچ پروژه یعنی سوییچ کامل
    MemoryStore + SkillLibrary + رجیستری ابزارها + شناسنامهٔ موتور.
    """

    def __init__(self) -> None:
        self.bus = EventBus()
        self.projects = ProjectManager()
        self.memory = self._memory_for(self.projects.active())
        self.skills = self._skills_for(self.projects.active())
        self.engine = AgentEngine(
            brain=None,  # پس از اتصال دیپ‌سیک ست می‌شود
            memory=self.memory,
            skills=self.skills,
            event_bus=self.bus,
        )
        self.engine.set_registry(self._make_registry(None, self.memory, self.skills))
        self._sync_project_info()

        self.gh_tools: Optional[GitHubTools] = None
        self.token_info: Optional[Dict[str, Any]] = None

        self.client: Optional[DeepSeekClient] = None
        self.brain: Optional[DeepSeekBrain] = None
        self.deepseek_started = False
        self.deepseek_ready = False
        self.deepseek_message = "در انتظار راه‌اندازی مرورگر…"
        self._connector_lock = threading.Lock()

        self.bus.subscribe(self._on_event)  # نقطهٔ اتصال برای واکنش‌های آینده به رویدادها

    # ------------------------------------------------------------------

    def _memory_for(self, project: Optional[Dict[str, Any]]) -> MemoryStore:
        """حافظهٔ ایزولهٔ یک پروژه (memory/projects/<key>/)."""
        if project:
            return MemoryStore(memory_dir=self.projects.memory_dir(project["key"]))
        # بدون پروژهٔ فعال → حافظهٔ موقت خالی (تا کاربر نام پروژه را انتخاب کند)
        return MemoryStore(memory_dir=self.projects.memory_root / "projects" / "_unassigned")

    def _skills_for(self, project: Optional[Dict[str, Any]]) -> SkillLibrary:
        """کتابخانهٔ مهارت ایزولهٔ یک پروژه (skills_learned/projects/<key>/)."""
        if project:
            return SkillLibrary(skills_dir=self.projects.skills_dir(project["key"]))
        return SkillLibrary(skills_dir=self.projects.skills_root / "projects" / "_unassigned")

    def _make_registry(self, gh_tools: Optional[GitHubTools], mem: MemoryStore, skills: SkillLibrary) -> ToolRegistry:
        registry = ToolRegistry()
        if gh_tools is not None:
            register_github_tools(registry, gh_tools)
        register_memory_tools(registry, mem)
        register_skill_tools(registry, skills)
        register_builtin_tools(registry)
        self.projects.register_project_tools(registry)
        return registry

    def _sync_project_info(self) -> None:
        """شناسنامهٔ پروژهٔ فعال → موتور (برای رویدادها و پرامپت)."""
        p = self.projects.active()
        self.engine.set_project(
            {"id": p["id"], "name": p["name"], "key": p["key"]} if p else {}
        )

    # ------------------------------------------------------------------
    # مدیریت پروژه‌ها (آیدی فشرده + حافظهٔ ایزوله)
    # ------------------------------------------------------------------

    def ensure_active_project(self) -> Dict[str, Any]:
        """اگر هیچ پروژه‌ای فعال نیست، پروژهٔ پیش‌فرض بساز («پروژه N»)."""
        p = self.projects.active()
        if p:
            return p
        rec = self.projects.create()  # نام پیش‌فرض: پروژه N
        self._apply_active_project()
        self.bus.emit("project_created", project=self._pub(rec))
        self.bus.emit("project_switched", project=self._pub(rec))
        return rec

    def _pub(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """نسخهٔ عمومی رکورد پروژه (کلید پایدار داخلی افشا نشود)."""
        return {k: rec[k] for k in ("id", "name", "created", "last_used", "task_count")}

    def create_project(self, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "نام پروژه را وارد کن"}
        if self.engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست — بعداً پروژه بساز"}
        rec = self.projects.create(name)
        self._apply_active_project()
        self.bus.emit("project_created", project=self._pub(rec))
        self.bus.emit("project_switched", project=self._pub(rec))
        return {"ok": True, "project": self._pub(rec)}

    def select_project(self, project_id: int) -> Dict[str, Any]:
        if self.engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست — سوییچ بعد از پایان تسک"}
        res = self.projects.select(int(project_id))
        if not res.get("ok"):
            return res
        rec = res["project"]
        self._apply_active_project()
        self.bus.emit("project_switched", project=self._pub(rec))
        return {"ok": True, "project": self._pub(rec)}

    def delete_project(self, project_id: int) -> Dict[str, Any]:
        if self.engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست — حذف بعد از پایان تسک"}
        res = self.projects.delete(int(project_id))
        if not res.get("ok"):
            return res
        # پس از حذف: اگر پروژهٔ فعال حذف شده، active_key به آخرین پروژه رفته — اعمال شود
        self._apply_active_project()
        self.bus.emit("project_deleted", deleted=self._pub(res["deleted"]), projects=self.projects_list())
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return {"ok": True, "deleted": self._pub(res["deleted"]), "renumbered": True}

    def rename_project(self, project_id: int, name: str) -> Dict[str, Any]:
        if self.engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست — بعداً تغییر نام بده"}
        res = self.projects.rename(int(project_id), name)
        if not res.get("ok"):
            return res
        self._sync_project_info()
        rec = self.projects.get(int(project_id))
        self.bus.emit("projects_updated", projects=self.projects_list(), active=self._active_pub())
        return {"ok": True, "project": self._pub(rec)}

    def _apply_active_project(self) -> None:
        """سوییچ کامل حافظه/مهارت/رجیستری به پروژهٔ فعال."""
        p = self.projects.active()
        self.memory = self._memory_for(p)
        self.skills = self._skills_for(p)
        self.engine.memory = self.memory
        self.engine.skills = self.skills
        self.engine.set_registry(self._make_registry(self.gh_tools, self.memory, self.skills))
        self._sync_project_info()

    def projects_list(self) -> List[Dict[str, Any]]:
        return [self._pub(p) for p in self.projects.list()]

    def _active_pub(self) -> Optional[Dict[str, Any]]:
        p = self.projects.active()
        return self._pub(p) if p else None

    def _on_event(self, event: str, item: Dict[str, Any]) -> None:
        if event in ("memory_updated", "skill_learned", "lesson_learned"):
            # اطمینان از ذخیرهٔ روی دیسک (ذخیرهٔ اصلی خودشان انجام می‌دهند؛ این فقط رویداد است)
            pass

    # ------------------------------------------------------------------
    # توکن گیت‌هاب
    # ------------------------------------------------------------------

    def set_token(self, token: str) -> Dict[str, Any]:
        """اعتبارسنجی توکن و سیم‌کشی ابزارهای گیت‌هاب در رجیستری (توکن فقط حافظه)."""
        token = (token or "").strip()
        if not token.startswith("ghp_"):
            return {"ok": False, "error": "توکن باید یک Classic Token با پیشوند ghp_ باشد (github.com/settings/tokens)."}
        gh = GitHubTools(token)
        try:
            info = gh.validate_token()
        except TokenInvalidError:
            return {"ok": False, "error": "توکن نامعتبر است (401 از GitHub) — دوباره توکن درستی وارد کن."}
        except GithubException as e:
            return {"ok": False, "error": f"خطای API گیت‌هاب ({e.status}) — بعداً تلاش کن."}
        self.gh_tools = gh
        self.token_info = info
        self.engine.set_registry(self._make_registry(gh, self.memory, self.skills))
        self.bus.emit("token_state", set=True, login=info["login"], scopes=info["token_scopes"])
        return {"ok": True, "login": info["login"], "scopes": info["token_scopes"]}

    # ------------------------------------------------------------------
    # connector دیپ‌سیک
    # ------------------------------------------------------------------

    def connect_deepseek(self, visible_login: bool = False) -> None:
        """راه‌اندازی مرورگر در نخ پس‌زمینه + اعلام وضعیت به UI.

        visible_login=True → مرورگر مرئی باز می‌شود و تا ورود دستی کاربر صبر می‌کند.
        """
        def connector() -> None:
            with self._connector_lock:
                self.deepseek_started = False
                self.deepseek_ready = False
                try:
                    if self.client is not None:
                        self.client.close()
                except Exception:
                    pass
                self.client = DeepSeekClient(
                    user_data_dir=config.DEEPSEEK_USER_DATA_DIR,
                    url=config.DEEPSEEK_URL,
                    headless=not visible_login and config.DEEPSEEK_HEADLESS,
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
                    self.deepseek_message = f"خطا در راه‌اندازی مرورگر: {e}"
                    self.bus.emit("deepseek_state", ready=False, error=str(self.deepseek_message))
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
                    self.deepseek_message = f"ورود انجام نشد: {e}"
                    self.bus.emit("deepseek_state", ready=False, login_required=True, message=self.deepseek_message)

        threading.Thread(target=connector, name="deepseek-connector", daemon=True).start()

    def _mark_ready(self) -> None:
        self.deepseek_ready = True
        self.deepseek_message = "اتصال برقرار است"
        self.brain = DeepSeekBrain(self.client)
        self.engine.brain = self.brain
        self.bus.emit("deepseek_state", ready=True, message=self.deepseek_message)

    # ------------------------------------------------------------------
    # اجرای تسک در پس‌زمینه
    # ------------------------------------------------------------------

    def start_task(self, task: str) -> Dict[str, Any]:
        task = (task or "").strip()
        if not task:
            return {"ok": False, "error": "متن تسک خالی است"}
        if self.engine.busy:
            return {"ok": False, "error": "یک تسک در حال اجراست — صبر کن تمام شود"}
        if not self.deepseek_ready or self.engine.brain is None:
            return {"ok": False, "error": self.deepseek_message or "دیپ‌سیک هنوز آماده نیست"}

        # بدون پروژهٔ فعال؟ خودکار «پروژه N» ساخته می‌شود (آیدی در پس‌زمینه ثبت می‌شود)
        project = self.ensure_active_project()
        proj_key = project["key"]

        def worker() -> None:
            try:
                report = self.engine.run_task(task)
                # آمار پروژهٔ فعال به‌روز شود (تسک روی همان پروژه ثبت می‌شود)
                self.projects.record_task(proj_key)
                return report
            except Exception as e:  # noqa: BLE001
                self.bus.emit("task_end", ok=False, answer=f"⛔ خطای موتور: {e}", rounds=0, tools=[], errors=1, duration_s=0)

        threading.Thread(target=worker, name="task-worker", daemon=True).start()
        return {"ok": True, "started": True, "project": self._pub(project)}

    # ------------------------------------------------------------------
    # snapshot وضعیت
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        return {
            "token": {
                "set": self.token_info is not None,
                "login": (self.token_info or {}).get("login"),
                "scopes": (self.token_info or {}).get("scopes"),
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
            "memory": {"counts": self.memory.counts(), "stats": self.memory.stats()},
            "skills": self.skills.stats(),
            "tools": len(self.engine._registry) if self.engine._registry else 0,
            "max_rounds": self.engine.max_rounds,
        }

    def github_overview(self) -> Dict[str, Any]:
        """خلاصهٔ فقط‌خواندنی گیت‌هاب برای پنل داشبورد."""
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

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            obj = json.loads(raw.decode("utf-8")) if raw else {}
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:  # بی‌سروصدا
        pass

    # ---------------- routes ----------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/ui", "/index.html"):
            self._serve_ui()
        elif path == "/api/state":
            self._send_json(STATE.snapshot())
        elif path == "/api/stream":
            self._serve_sse()
        elif path == "/api/history":
            self._send_json({"events": STATE.bus.history(150)})
        elif path == "/api/memory":
            qs = parse_qs(urlparse(self.path).query)
            section = (qs.get("section") or ["all"])[0]
            if section == "tasks":
                self._send_json({"ok": True, "tasks": STATE.memory.recent_tasks(30)})
            else:
                sections = [section] if section != "all" else ["facts", "lessons", "notes"]
                data = {s: STATE.memory.all_items(s) for s in sections}
                self._send_json({"ok": True, **data})
        elif path == "/api/skills":
            self._send_json({"ok": True, "skills": STATE.skills.list_skills()})
        elif path == "/api/projects":
            self._send_json({
                "ok": True,
                "projects": STATE.projects_list(),
                "active": STATE._active_pub(),
                "next_default_name": STATE.projects.next_default_name(),
            })
        elif path == "/api/github/overview":
            try:
                self._send_json(STATE.github_overview())
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        else:
            self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json_body()
        if path == "/api/token":
            self._send_json(STATE.set_token(str(body.get("token", ""))))
        elif path == "/api/task":
            self._send_json(STATE.start_task(str(body.get("task", ""))))
        elif path == "/api/projects":
            self._send_json(STATE.create_project(str(body.get("name", ""))))
        elif path == "/api/projects/select":
            try:
                self._send_json(STATE.select_project(int(body.get("id", 0))))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "آیدی نامعتبر است"})
        elif path == "/api/projects/delete":
            try:
                self._send_json(STATE.delete_project(int(body.get("id", 0))))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "آیدی نامعتبر است"})
        elif path == "/api/projects/rename":
            try:
                self._send_json(STATE.rename_project(int(body.get("id", 0)), str(body.get("name", ""))))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "آیدی نامعتبر است"})
        elif path == "/api/deepseek/login":
            STATE.connect_deepseek(visible_login=True)
            self._send_json({"ok": True, "message": "پنجرهٔ مرئی برای ورود دستی باز می‌شود"})
        elif path == "/api/deepseek/reconnect":
            STATE.connect_deepseek(visible_login=False)
            self._send_json({"ok": True, "message": "اتصال مجدد آغاز شد"})
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
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=500)

        def push(event: str, item: Dict[str, Any]) -> None:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

        STATE.bus.subscribe(push)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def write_event(item: Dict[str, Any]) -> bool:
            try:
                line = f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        try:
            # بازپخش تاریخچهٔ اخیر برای reconnect
            for item in STATE.bus.history(40):
                if not write_event(item):
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
            try:
                STATE.bus._subs.remove(push)
            except Exception:
                pass


def serve(open_browser: bool = True) -> ThreadingHTTPServer:
    """ساخت/اجرای سرور DevX؛ آدرس را برمی‌گرداند."""
    server = ThreadingHTTPServer((config.DEVX_HOST, config.DEVX_PORT), DevXHandler)
    server.daemon_threads = True
    url = f"http://{config.DEVX_HOST}:{config.DEVX_PORT}"
    print(f"🚀 DevX روی {url} اجرا شد")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return server
