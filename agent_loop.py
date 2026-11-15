# -*- coding: utf-8 -*-
"""موتور ایجنت (AgentEngine) — هستهٔ حرفه‌ای نسخهٔ DevX.

تفاوت با حلقهٔ سادهٔ قبلی:
  * روبه‌رویدار است (EventBus): سرور DevX و CLI هر دو مشترک همان موتور می‌شوند
    و هر دور/ابزار/نتیجه/پاسخ نهایی به‌صورت رویداد منتشر می‌شود.
  * ابتدای هر تسک، «حافظهٔ مرتبط» + «مهارت‌های آموخته‌شدهٔ مرتبط» به پرامپت
    تزریق می‌شود (recall خودکار) — ایجنت از تجربه‌های گذشته استفاده می‌کند.
  * پایان هر تسک: رکورد تسک در حافظه ذخیره می‌شود؛ اگر تسک موفق بود یک
    «مهارت قابل‌استفادهٔ مجدد» از مدل استخراج و در کتابخانه ذخیره می‌شود
    (پروتکل DISTILL)؛ اگر خطای ابزار داشت، یک «درس» در حافظه ثبت می‌شود.
  * معماری Brain-agnostic: هر کلاینتی که send/wait_response/new_chat دارد
    (دیپ‌سیک، FakeBrain تست، یا در آینده API رسمی) می‌تواند مغز ایجنت باشد.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import config
from memory import MemoryStore
from skills import SkillLibrary
from system_prompt import build_system_prompt, parse_model_action
from tool_registry import ToolRegistry

_JSON_BRACES = re.compile(r"\{.*\}", re.DOTALL)


# ======================================================================
# باس رویداد
# ======================================================================

class EventBus:
    """انتشار رویدادهای موتور به مشترکین (UI، CLI، لاگ) + بافر چرخه‌ای."""

    def __init__(self, history_max: int = 400) -> None:
        self._subs: List[Callable[[str, Dict[str, Any]], None]] = []
        self._history: deque = deque(maxlen=history_max)
        self._lock = threading.RLock()

    def subscribe(self, fn: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def emit(self, event: str, **payload: Any) -> None:
        item = {"event": event, "ts": time.time(), **payload}
        with self._lock:
            self._history.append(item)
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event, item)
            except Exception:
                pass  # مشترک خراب نباید موتور را بکشد

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)[-limit:]


# ======================================================================
# مغزها (Brain-agnostic)
# ======================================================================

class DeepSeekBrain:
    """آداپتور DeepSeekClient به رابط Brain موردنیاز موتور."""

    def __init__(self, client: Any) -> None:  # deepseek_client.DeepSeekClient
        self.client = client

    def new_chat(self) -> None:
        self.client.new_chat()

    def send(self, text: str) -> None:
        self.client.send_message(text)

    def wait_response(self) -> str:
        return self.client.wait_for_response()


class FakeBrain:
    """مغز اسکریپت‌شده برای تست آفلاین موتور — پاسخ‌ها را به‌ترتیب می‌خواند."""

    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.sent: List[str] = []

    def new_chat(self) -> None:
        pass

    def send(self, text: str) -> None:
        self.sent.append(text)

    def wait_response(self) -> str:
        if not self.responses:
            raise RuntimeError("FakeBrain exhausted")
        return self.responses.pop(0)


def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """استخراج اولین آبجکت JSON از پاسخ مدل (برای پاسخ‌های متای DISTILL)."""
    if not text:
        return None
    for cand in (text.strip(), *re.findall(_JSON_BRACES, text)):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# ======================================================================
# موتور ایجنت
# ======================================================================

SKILL_DISTILL_PROMPT = (
    "[SYSTEM META-REQUEST — the user task is complete]\n"
    "Distill a REUSABLE SKILL from how you solved this task, so that future similar "
    "tasks can be executed faster and more reliably. Return ONLY one JSON object:\n"
    '{"name": "<short-kebab-name>", "description": "<one line>", '
    '"triggers": ["<keyword>", ...], "steps": "1. tool_name(arg=value) 2. tool_name(...) '
    '... (concrete reusable procedure with exact tool calls)"}\n'
    "If the task was trivial or one-off and not worth remembering, return exactly: {\"skip\": true}"
)

LESSON_DISTILL_PROMPT = (
    "[SYSTEM META-REQUEST]\n"
    "Some tool calls failed during this task. Distill ONE short, durable lesson that "
    "will prevent the same failure next time. Return ONLY one JSON object:\n"
    '{"lesson": "<one actionable sentence naming the exact tool and cause>"}\n'
    "If nothing generalizable, return exactly: {\"skip\": true}"
)


class AgentEngine:
    """حلقهٔ ایجنت حرفه‌ای: حافظه + مهارت + رویداد + یادگیری خودکار."""

    def __init__(
        self,
        brain: Any,
        memory: MemoryStore,
        skills: SkillLibrary,
        event_bus: Optional[EventBus] = None,
        max_rounds: Optional[int] = None,
    ) -> None:
        self.brain = brain
        self.memory = memory
        self.skills = skills
        self.bus = event_bus or EventBus()
        self.max_rounds = max_rounds or config.MAX_ROUNDS
        self._registry: Optional[ToolRegistry] = None
        self._registry_version = 0
        self._system_prompt_cache: Optional[str] = None
        self._lock = threading.RLock()
        self.busy = False  # یک تسک در حال اجراست

    # ------------------------------------------------------------------
    # رجیستری (قابل تعویض پس از دریافت توکن گیت‌هاب)
    # ------------------------------------------------------------------

    def set_registry(self, registry: ToolRegistry) -> None:
        with self._lock:
            self._registry = registry
            self._registry_version += 1
            self._system_prompt_cache = None  # پرامپت با ابزارهای جدید بازساخته شود

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            raise RuntimeError("رجیستری ابزار هنوز نصب نشده است")
        return self._registry

    # ------------------------------------------------------------------
    # پرامپت سیستمی + زمینهٔ حافظه/مهارت
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        with self._lock:
            if self._system_prompt_cache is None:
                self._system_prompt_cache = build_system_prompt(self.registry, self.max_rounds)
            return self._system_prompt_cache

    def _first_message(self, task: str) -> str:
        memory_ctx = self.memory.build_context(task)
        skills_ctx = self.skills.build_context(task)
        used = bool(memory_ctx or skills_ctx)
        if used:
            self.bus.emit("context", memory=bool(memory_ctx), skills=bool(skills_ctx))
        return f"{self._system_prompt()}{memory_ctx}{skills_ctx}\n\n=== USER TASK ===\n{task}"

    # ------------------------------------------------------------------
    # اجرای تسک
    # ------------------------------------------------------------------

    def run_task(self, task: str) -> Dict[str, Any]:
        """اجرای کامل یک تسک با حلقهٔ ابزار + یادگیری پایان کار. Thread-safe: نوبتی."""
        with self._lock:
            if self.busy:
                raise RuntimeError("یک تسک در حال اجراست — صبر کن تمام شود")
            self.busy = True
        started = time.time()
        self.bus.emit("task_start", task=task, max_rounds=self.max_rounds)
        tools_used: List[str] = []
        errors = 0
        trace: List[Dict[str, Any]] = []
        answer = ""
        try:
            self.brain.new_chat()
            self.brain.send(self._first_message(task))

            for round_no in range(1, self.max_rounds + 1):
                self.bus.emit("round", round=round_no, max_rounds=self.max_rounds)
                raw = self.brain.wait_response()
                action = parse_model_action(raw)

                if action is None:
                    # خروج از پروتکل → متن خام همان پاسخ نهایی است
                    answer = raw
                    break

                if "final_answer" in action:
                    answer = str(action.get("final_answer") or "").strip() or raw
                    break

                tool_name = str(action.get("tool") or "").strip()
                arguments = dict(action.get("arguments") or {})
                if not tool_name:
                    answer = raw
                    break

                self.bus.emit("tool_call", round=round_no, tool=tool_name, arguments=arguments)
                result = self.registry.execute(tool_name, arguments)
                ok = bool(result.get("ok"))
                if not ok:
                    errors += 1
                tools_used.append(tool_name)
                preview = json.dumps(result, ensure_ascii=False)[:160]
                self.bus.emit("tool_result", tool=tool_name, ok=ok, preview=preview)
                trace.append({"round": round_no, "tool": tool_name, "arguments": arguments, "ok": ok})

                if tool_name == "get_skill" and ok and arguments.get("name"):
                    # استفاده از مهارت آموخته‌شده ثبت شود (وزن یادگیری بعدی)
                    try:
                        self.skills.mark_used(str(arguments["name"]), ok=True)
                    except Exception:
                        pass

                payload = f"TOOL_RESULT {tool_name}:\n" + json.dumps(result, ensure_ascii=False)
                self.brain.send(payload)
            else:
                answer = (
                    f"⚠️ پس از {self.max_rounds} دورِ ابزار، پاسخ نهایی کامل نشد؛ "
                    "خلاصهٔ کارهای انجام‌شده در رویدادها آمده است."
                )

            duration = time.time() - started
            task_ok = bool(answer) and not answer.startswith("⚠️")
            self.bus.emit("final", answer=answer, ok=task_ok)

            # ---------- یادگیری پایان کار ----------
            skill_name = self._learn_skill(task_ok)
            lesson = self._learn_lesson(errors)
            self.memory.add_task_record(
                task=task,
                rounds=len(trace),
                tools_used=tools_used,
                ok=task_ok,
                skill_learned=skill_name,
                duration_s=duration,
            )
            self.bus.emit("memory_updated", counts=self.memory.counts())

            report = {
                "ok": task_ok,
                "answer": answer,
                "rounds": len(trace),
                "tools": trace,
                "errors": errors,
                "skill_learned": skill_name,
                "lesson_learned": lesson,
                "duration_s": round(duration, 1),
            }
            self.bus.emit("task_end", **report)
            return report
        finally:
            with self._lock:
                self.busy = False

    # ------------------------------------------------------------------
    # یادگیری خودکار (پروتکل DISTILL)
    # ------------------------------------------------------------------

    def _meta_ask(self, prompt: str) -> Optional[Dict[str, Any]]:
        """پرسش متای کوتاه از مدل در همان گفت‌وگو؛ خطاها را نرم می‌بلعد."""
        try:
            self.brain.send(prompt)
            raw = self.brain.wait_response()
            return extract_json_block(raw)
        except Exception:
            return None

    def _learn_skill(self, task_ok: bool) -> Optional[str]:
        if not config.AUTO_LEARN_SKILL or not task_ok:
            return None
        obj = self._meta_ask(SKILL_DISTILL_PROMPT)
        if not obj or obj.get("skip") or not obj.get("steps"):
            return None
        res = self.skills.save_skill_from_model(obj)
        if res.get("ok"):
            self.bus.emit("skill_learned", name=obj.get("name"), file=res.get("file"))
            return str(obj.get("name"))
        return None

    def _learn_lesson(self, errors: int) -> Optional[str]:
        if not config.AUTO_LEARN_LESSON or errors == 0:
            return None
        obj = self._meta_ask(LESSON_DISTILL_PROMPT)
        if not obj or obj.get("skip") or not obj.get("lesson"):
            return None
        self.memory.add_lesson(str(obj["lesson"]))
        self.bus.emit("lesson_learned", text=str(obj["lesson"]))
        return str(obj["lesson"])
