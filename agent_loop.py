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

REFLECT_PROMPT = (
    "[SYSTEM META-REQUEST — self-reflection]\n"
    "Evaluate your own performance on the task you just finished. Return ONLY one JSON object:\n"
    '{"what_worked": "<short>", "what_failed": "<short or empty>", '
    '"improvement": "<one concrete, durable improvement to remember>"}\n'
    "If nothing worth remembering, return exactly: {\"skip\": true}"
)

# نقش‌های زیرایجنت — هر نقش persona و سقف دور خودش را دارد
SUBAGENT_ROLES = {
    "researcher": (
        "You are a RESEARCHER sub-agent: gather facts with read-only tools, verify claims, "
        "and return a concise factual report. Do not modify anything."
    ),
    "coder": (
        "You are a CODER sub-agent: implement the requested change using file/code tools, "
        "run quick verification if possible, and report what you changed."
    ),
    "reviewer": (
        "You are a REVIEWER sub-agent: inspect the given work critically, list issues "
        "by severity with exact locations, and propose minimal fixes."
    ),
}


class AgentEngine:
    """حلقهٔ ایجنت حرفه‌ای v4: حافظه + مهارت + رویداد + یادگیری + بازتاب + زیرایجنت + چک‌پوینت."""

    def __init__(
        self,
        brain: Any,
        memory: MemoryStore,
        skills: SkillLibrary,
        event_bus: Optional[EventBus] = None,
        max_rounds: Optional[int] = None,
        usage: Any = None,               # usage.UsageTracker (اختیاری)
        checkpoints: Any = None,         # checkpoint.CheckpointStore (اختیاری)
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
        # شناسنامهٔ پروژهٔ فعال (سرور/CLI هنگام سوییچ پروژه ست می‌کنند)
        self.project_info: Dict[str, Any] = {}
        self.session_id: Optional[int] = None   # سشن چت جاری (چندسشن موازی)
        self.usage = usage                       # داشبورد مصرف
        self.checkpoints = checkpoints           # چک‌پوینت/ادامهٔ تسک
        self._subtask_depth = 0                  # عمق واگذاری (حداکثر ۱)

    def set_project(self, info: Dict[str, Any]) -> None:
        """تعیین پروژهٔ فعال؛ persona جدید → پرامپت سیستمی بازساخته شود."""
        with self._lock:
            self.project_info = dict(info or {})
            self._system_prompt_cache = None

    # ------------------------------------------------------------------
    # رویدادها با مُهرِ پروژه/سشن
    # ------------------------------------------------------------------

    def _emit(self, event: str, **payload: Any) -> None:
        """انتشار رویداد با خودکارِ برچسب پروژه + سشن (برای routing چندسشن در UI)."""
        if self.project_info:
            payload.setdefault("project", dict(self.project_info))
        if self.session_id is not None:
            payload.setdefault("session_id", self.session_id)
        self.bus.emit(event, **payload)

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

    def register_delegate_tool(self, registry: Optional[ToolRegistry] = None) -> None:
        """ابزار delegate_subtask — واگذاری کار به زیرایجنتِ نقش‌محور."""
        engine = self
        reg = registry or self._registry
        if reg is None:
            return

        def _delegate(role: str, task: str, max_rounds: int = 0) -> Dict[str, Any]:
            return engine.run_subtask(role, task, max_rounds=int(max_rounds or 0))

        reg.register_function(
            name="delegate_subtask",
            description=(
                "واگذاری یک زیرکار به زیرایجنت تخصصی. role: researcher (تحقیق فقط‌خواندنی)، "
                "coder (پیاده‌سازی)، reviewer (نقد و بازبینی). خروجی: گزارش نهایی زیرایجنت"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["researcher", "coder", "reviewer"]},
                    "task": {"type": "string", "description": "شرح دقیق زیرکار + زمینهٔ لازم"},
                    "max_rounds": {"type": "integer", "default": 0, "description": "۰ = پیش‌فرض موتور"},
                },
                "required": ["role", "task"],
            },
            func=_delegate,
        )

    # ------------------------------------------------------------------
    # پرامپت سیستمی + زمینهٔ حافظه/مهارت + persona
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        with self._lock:
            if self._system_prompt_cache is None:
                base = build_system_prompt(self.registry, self.max_rounds)
                persona = (self.project_info.get("persona") or "").strip()
                if persona:
                    base += (
                        "\n\n=== PROJECT PERSONA (user-defined; always apply in this project) ===\n"
                        + persona[:1500]
                    )
                self._system_prompt_cache = base
            return self._system_prompt_cache

    def _first_message(self, task: str) -> str:
        memory_ctx = self.memory.build_context(task)
        skills_ctx = self.skills.build_context(task)
        used = bool(memory_ctx or skills_ctx)
        if used:
            self._emit("context", memory=bool(memory_ctx), skills=bool(skills_ctx))
        proj = dict(self.project_info)
        project_ctx = (
            f"\n\n=== PROJECT ===\nid: {proj.get('id')} | name: {proj.get('name')} | "
            "(memory & learned skills are isolated to this project)"
            if proj else ""
        )
        return f"{self._system_prompt()}{project_ctx}{memory_ctx}{skills_ctx}\n\n=== USER TASK ===\n{task}"

    # ------------------------------------------------------------------
    # استریم جزئی پاسخ (وقتی مغز پشتیبانی کند)
    # ------------------------------------------------------------------

    def enable_streaming(self) -> None:
        """اگر کلاینت مغز set_on_delta داشته باشد، پاسخِ درحال‌شکل‌گیری به UI استریم می‌شود."""
        client = getattr(self.brain, "client", None)
        if client is not None and hasattr(client, "set_on_delta"):
            def _on_delta(text: str) -> None:
                self._emit("answer_delta", text=(text or "")[-1500:])
            try:
                client.set_on_delta(_on_delta)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # اجرای تسک
    # ------------------------------------------------------------------

    def run_task(self, task: str) -> Dict[str, Any]:
        """اجرای کامل یک تسک: حلقهٔ ابزار + چک‌پوینت + یادگیری + بازتاب + مصرف."""
        with self._lock:
            if self.busy:
                raise RuntimeError("یک تسک در حال اجراست — صبر کن تمام شود")
            self.busy = True
        started = time.time()
        proj = dict(self.project_info)
        self._emit("task_start", task=task, max_rounds=self.max_rounds)
        answer = ""
        trace: List[Dict[str, Any]] = []
        errors = 0
        cp_id: Optional[str] = None
        try:
            self.enable_streaming()
            self.brain.new_chat()
            self.brain.send(self._first_message(task))
            answer, trace, errors = self._loop(task, start_round=1, trace=trace)

            duration = time.time() - started
            task_ok = bool(answer) and not answer.startswith("⚠️")
            self._emit("final", answer=answer, ok=task_ok)

            # ---------- یادگیری پایان کار ----------
            skill_name = self._learn_skill(task_ok)
            lesson = self._learn_lesson(errors)
            self.memory.add_task_record(
                task=task,
                rounds=len(trace),
                tools_used=[t["tool"] for t in trace],
                ok=task_ok,
                skill_learned=skill_name,
                duration_s=duration,
            )
            reflection = self._reflect(task_ok)
            if self.usage is not None:
                try:
                    prompt_chars = len(self._first_message(task)) + sum(len(t.get("arguments") or {}) for t in trace) * 40
                    self.usage.record(
                        project_id=proj.get("id"), ok=task_ok, rounds=len(trace),
                        tool_calls=len(trace), duration_s=duration,
                        prompt_chars=prompt_chars, answer_chars=len(answer),
                    )
                except Exception:
                    pass
            self._emit("memory_updated", counts=self.memory.counts())

            report = {
                "ok": task_ok,
                "answer": answer,
                "rounds": len(trace),
                "tools": trace,
                "errors": errors,
                "skill_learned": skill_name,
                "lesson_learned": lesson,
                "reflection": reflection,
                "duration_s": round(duration, 1),
                "project": proj,
            }
            self._emit("task_end", **report)
            return report
        finally:
            with self._lock:
                self.busy = False

    def _loop(self, task: str, start_round: int, trace: List[Dict[str, Any]]) -> tuple:
        """حلقهٔ ابزار از دورِ start_round تا final_answer یا سقف دورها."""
        answer = ""
        errors = 0
        for round_no in range(start_round, self.max_rounds + 1):
            self._emit("round", round=round_no, max_rounds=self.max_rounds)
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

            self._emit("tool_call", round=round_no, tool=tool_name, arguments=arguments)
            result = self.registry.execute(tool_name, arguments)
            ok = bool(result.get("ok"))
            if not ok:
                errors += 1
            preview = json.dumps(result, ensure_ascii=False)[:160]
            self._emit("tool_result", tool=tool_name, ok=ok, preview=preview, pending=result.get("pending_approval"))
            trace.append({"round": round_no, "tool": tool_name, "arguments": arguments, "ok": ok})

            # چک‌پوینت در پایان هر دور (برای Resume پس از قطعی)
            if self.checkpoints is not None and config.CHECKPOINTS_ENABLED:
                try:
                    cp = self.checkpoints.save(
                        task, dict(self.project_info), self.session_id, round_no, trace,
                        status="running",
                    )
                    self._emit("checkpoint_saved", checkpoint_id=cp["id"], round=round_no)
                except Exception:
                    pass

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
        return answer, trace, errors

    # ------------------------------------------------------------------
    # ادامهٔ تسک از چک‌پوینت (Resume)
    # ------------------------------------------------------------------

    def resume(self, checkpoint_id: str) -> Dict[str, Any]:
        """ادامهٔ تسکِ نیمه‌کاره: خلاصهٔ پیشرفت قبلی تزریق و حلقه ادامه می‌یابد."""
        with self._lock:
            if self.busy:
                raise RuntimeError("یک تسک در حال اجراست")
            self.busy = True
        started = time.time()
        try:
            if self.checkpoints is None:
                return {"ok": False, "error": "چک‌پوینت غیرفعال است"}
            cp = self.checkpoints.load(checkpoint_id)
            if not cp:
                return {"ok": False, "error": "چک‌پوینت یافت نشد"}
            task = cp["task"]
            progress = "\n".join(
                f"- R{t['round']}: {t['tool']}({json.dumps(t.get('arguments', {}) , ensure_ascii=False)[:80]}) ok={t['ok']}"
                for t in cp.get("trace", [])
            ) or "(بدون ابزار)"
            self._emit("task_start", task=task, max_rounds=self.max_rounds, resumed=True, checkpoint=checkpoint_id)
            self.enable_streaming()
            self.brain.new_chat()
            first = (
                self._first_message(task)
                + f"\n\n=== RESUMED TASK (progress before interruption; do NOT redo completed steps) ===\n"
                + progress
            )
            self.brain.send(first)
            answer, trace, errors = self._loop(task, start_round=int(cp.get("round", 0)) + 1, trace=list(cp.get("trace", [])))
            duration = time.time() - started
            task_ok = bool(answer) and not answer.startswith("⚠️")
            self._emit("final", answer=answer, ok=task_ok)
            self.checkpoints.mark(checkpoint_id, "done" if task_ok else "failed")
            self.memory.add_task_record(
                task=task, rounds=len(trace), tools_used=[t["tool"] for t in trace],
                ok=task_ok, duration_s=duration,
            )
            report = {
                "ok": task_ok, "answer": answer, "rounds": len(trace), "tools": trace,
                "errors": errors, "skill_learned": None, "lesson_learned": None,
                "reflection": None, "duration_s": round(duration, 1),
                "project": dict(self.project_info), "resumed_from": checkpoint_id,
            }
            self._emit("task_end", **report)
            return report
        finally:
            with self._lock:
                self.busy = False

    # ------------------------------------------------------------------
    # زیرایجنت (Delegation) — حلقهٔ تو در تو در همان گفت‌وگو
    # ------------------------------------------------------------------

    def run_subtask(self, role: str, task: str, max_rounds: int = 0) -> Dict[str, Any]:
        """واگذاری زیرکار به نقش تخصصی؛ در همان چت اجرا می‌شود تا زمینهٔ والد حفظ شود."""
        role = (role or "").strip().lower()
        if role not in SUBAGENT_ROLES:
            return {"ok": False, "error": f"نقش '{role}' معتبر نیست (researcher/coder/reviewer)"}
        if self._subtask_depth >= 1:
            return {"ok": False, "error": "زیرایجنت تو در تو مجاز نیست"}
        if getattr(self, "busy", False) is False:
            # واگذاری فقط در جریان یک تسکِ والد معنا دارد
            return {"ok": False, "error": "delegate فقط در جریان یک تسک فعال قابل استفاده است"}
        cap = int(max_rounds or config.SUBAGENT_MAX_ROUNDS)
        self._subtask_depth += 1
        started = time.time()
        self._emit("subtask_start", role=role, task=task[:200])
        answer = ""
        trace: List[Dict[str, Any]] = []
        try:
            header = (
                f"=== DELEGATED SUBTASK — you are now a {role.upper()} ===\n"
                f"{SUBAGENT_ROLES[role]}\n"
                f"Work ONLY on this subtask, then end with "
                f'{{"final_answer": "<your subtask report>"}}\n\nSUBTASK: {task}'
            )
            self.brain.send(header)
            for r in range(1, cap + 1):
                raw = self.brain.wait_response()
                action = parse_model_action(raw)
                if action is None:
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
                result = self.registry.execute(tool_name, arguments)
                ok = bool(result.get("ok"))
                trace.append({"round": r, "tool": tool_name, "ok": ok})
                self.brain.send(f"TOOL_RESULT {tool_name}:\n" + json.dumps(result, ensure_ascii=False))
            else:
                answer = f"⚠️ زیرکار پس از {cap} دور ناتمام ماند؛ خلاصهٔ دستاوردها: {len(trace)} اجرای ابزار."
            duration = time.time() - started
            report = {
                "ok": bool(answer) and not answer.startswith("⚠️"),
                "role": role, "answer": answer[:4000], "rounds": len(trace),
                "tools": trace, "duration_s": round(duration, 1),
            }
            self._emit("subtask_end", **report)
            return report
        finally:
            self._subtask_depth -= 1

    # ------------------------------------------------------------------
    # یادگیری خودکار (پروتکل DISTILL) + بازتاب
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
            self._emit("skill_learned", name=obj.get("name"), file=res.get("file"), version=res.get("version"))
            return str(obj.get("name"))
        return None

    def _learn_lesson(self, errors: int) -> Optional[str]:
        if not config.AUTO_LEARN_LESSON or errors == 0:
            return None
        obj = self._meta_ask(LESSON_DISTILL_PROMPT)
        if not obj or obj.get("skip") or not obj.get("lesson"):
            return None
        self.memory.add_lesson(str(obj["lesson"]))
        self._emit("lesson_learned", text=str(obj["lesson"]))
        return str(obj["lesson"])

    def _reflect(self, task_ok: bool) -> Optional[str]:
        """بازتاب پس از تسک: چه خوب پیش رفت، چه چیزی باید اصلاح شود → حافظه."""
        if not config.REFLECTION_ENABLED:
            return None
        obj = self._meta_ask(REFLECT_PROMPT)
        if not obj or obj.get("skip"):
            return None
        improvement = str(obj.get("improvement") or "").strip()
        if not improvement:
            return None
        text = f"worked: {obj.get('what_worked', '')} | failed: {obj.get('what_failed', '')} | fix: {improvement}"
        self.memory.add_reflection(text[:400])
        self._emit("reflection", text=text[:400])
        return improvement
