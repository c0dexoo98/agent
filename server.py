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
from skills import SkillLibrary, register_skill_tools
from tool_registry import ToolRegistry

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"


class DevXState:
    """وضعیت مشترک سرور: توکن، مرورگر دیپ‌سیک، موتور، حافظه، مهارت‌ها."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.memory = MemoryStore()
        self.skills = SkillLibrary()
        self.engine = AgentEngine(
            brain=None,  # پس از اتصال دیپ‌سیک ست می‌شود
            memory=self.memory,
            skills=self.skills,
            event_bus=self.bus,
        )
        self.engine.set_registry(self._make_registry(None))

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

    def _make_registry(self, gh_tools: Optional[GitHubTools]) -> ToolRegistry:
        registry = ToolRegistry()
        if gh_tools is not None:
            register_github_tools(registry, gh_tools)
        register_memory_tools(registry, self.memory)
        register_skill_tools(registry, self.skills)
        register_builtin_tools(registry)
        return registry

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
        self.engine.set_registry(self._make_registry(gh))
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

        def worker() -> None:
            try:
                self.engine.run_task(task)
            except Exception as e:  # noqa: BLE001
                self.bus.emit("task_end", ok=False, answer=f"⛔ خطای موتور: {e}", rounds=0, tools=[], errors=1, duration_s=0)

        threading.Thread(target=worker, name="task-worker", daemon=True).start()
        return {"ok": True, "started": True}

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
