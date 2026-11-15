# -*- coding: utf-8 -*-
"""حافظهٔ ماندگار ایجنت (Memory) — «همه چیز را یادش می‌ماند».

ساختار: یک فایل JSON (memory/memory.json) با چهار بخش:

  * facts    → حقایق و ترجیحات پایدار کاربر/پروژه (مثل «ریپوی اصلی من agent است»)
  * lessons  → درس‌های خطا: «اگر X خطا داد، Y کن» (از شکست‌ها یاد می‌گیرد)
  * tasks    → تاریخچهٔ تسک‌ها: چه کاری، چند دور، با چه ابزارهایی، موفق/ناموفق
  * notes    → یادداشت‌های آزادی که خود مدل ذخیره می‌کند

قابلیت‌ها:
  * جستجوی کلیدواژه‌ای چندکلمه‌ای با امتیازدهی (بدون نیاز به اینترنت/embedding)
  * ساخت پرامپت «زمینهٔ حافظه» برای تزریق در شروع هر تسک (recall خودکار)
  * ذخیرهٔ اتمیک (tmp + rename) و قفل نخ برای استفاده در سرور چندنخی
  * ابزارهای رجیستری: save_memory / search_memory / list_recent_tasks
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    """حافظهٔ بلندمدت ماندگار ایجنت."""

    SECTIONS = ("facts", "lessons", "tasks", "notes")

    def __init__(self, memory_dir: Optional[Path] = None) -> None:
        self.dir = Path(memory_dir or config.MEMORY_DIR)
        self.path = self.dir / "memory.json"
        self._lock = threading.RLock()
        self._data: Dict[str, List[Dict[str, Any]]] = {s: [] for s in self.SECTIONS}
        self._load()

    # ------------------------------------------------------------------
    # بارگذاری / ذخیرهٔ اتمیک
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for section in self.SECTIONS:
                items = raw.get(section, [])
                self._data[section] = items if isinstance(items, list) else []
        except Exception as e:
            # حافظهٔ خراب نباید ایجنت را ببندد — از نو شروع می‌کنیم و بکاپ می‌گذاریم
            try:
                self.path.rename(self.path.with_suffix(".corrupt.bak"))
            except Exception:
                pass
            print(f"⚠️ حافظه قابل خواندن نبود ({e}) — بکاپ گرفته شد و از نو شروع شد.")

    def save(self) -> None:
        """ذخیرهٔ اتمیک: اول در فایل موقت، بعد جایگزینی اتمی."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # ------------------------------------------------------------------
    # افزودن آیتم
    # ------------------------------------------------------------------

    def add_fact(self, text: str) -> Dict[str, Any]:
        return self._add("facts", text, config.MEMORY_MAX_FACTS)

    def add_lesson(self, text: str) -> Dict[str, Any]:
        return self._add("lessons", text, config.MEMORY_MAX_LESSONS)

    def add_note(self, text: str) -> Dict[str, Any]:
        return self._add("notes", text, 200)

    def add_task_record(
        self,
        task: str,
        rounds: int,
        tools_used: List[str],
        ok: bool,
        skill_learned: Optional[str] = None,
        duration_s: float = 0.0,
    ) -> Dict[str, Any]:
        rec = {
            "ts": _now_iso(),
            "task": task[:300],
            "rounds": rounds,
            "tools": tools_used,
            "ok": bool(ok),
            "skill": skill_learned,
            "duration_s": round(float(duration_s), 1),
        }
        with self._lock:
            self._data["tasks"].append(rec)
            self._data["tasks"] = self._data["tasks"][-config.MEMORY_MAX_TASKS:]
            self.save()
        return rec

    def _add(self, section: str, text: str, cap: int) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "متن خالی است"}
        with self._lock:
            items = self._data[section]
            # جلوگیری از تکرار دقیق
            for it in items:
                if it.get("text") == text:
                    it["hits"] = int(it.get("hits", 0)) + 1
                    it["updated"] = _now_iso()
                    self.save()
                    return {"ok": True, "dedup": True, "item": it}
            item = {"id": int(time.time() * 1000), "text": text, "ts": _now_iso(), "hits": 0}
            items.append(item)
            if len(items) > cap:
                del items[: len(items) - cap]
            self.save()
        return {"ok": True, "item": item}

    def bump(self, section: str, item_id: int) -> None:
        """افزایش شمارندهٔ استفاده (آیتم‌های پرکاربرد در recall بالاتر می‌آیند)."""
        with self._lock:
            for it in self._data.get(section, []):
                if it.get("id") == item_id:
                    it["hits"] = int(it.get("hits", 0)) + 1
            self.save()

    # ------------------------------------------------------------------
    # جستجوی کلیدواژه‌ای
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        # واژه‌های فارسی/انگلیسی/عدد — حداقل ۳ نویسه
        return [w for w in re.findall(r"[\w\u0600-\u06FF]+", (text or "").lower()) if len(w) >= 3]

    @classmethod
    def _score(cls, query_tokens: List[str], text: str) -> float:
        """امتیاز هم‌پوشانی با تطبیق پسوندی (برای صرف‌ونظرِ فارسی: «ریپو» با «ریپوی» می‌خواند)."""
        text_tokens = set(cls._tokenize(text))
        if not query_tokens or not text_tokens:
            return 0.0

        def match(q: str) -> bool:
            if q in text_tokens:
                return True
            if len(q) < 3:
                return False
            return any(t.startswith(q) or q.startswith(t) for t in text_tokens)

        overlap = sum(1 for t in set(query_tokens) if match(t))
        return overlap / len(set(query_tokens))

    def search(
        self,
        query: str,
        sections: Optional[List[str]] = None,
        limit: int = 5,
        min_score: float = 0.15,
    ) -> List[Dict[str, Any]]:
        """جستجوی امتیازمحور؛ مرتب‌سازی بر اساس (امتیاز، تعداد استفاده، تازگی)."""
        qtokens = self._tokenize(query)
        sections = sections or ["facts", "lessons", "notes"]
        results: List[Dict[str, Any]] = []
        with self._lock:
            for sec in sections:
                for idx, it in enumerate(self._data.get(sec, [])):
                    score = self._score(qtokens, it.get("text", ""))
                    if score >= min_score:
                        results.append(
                            {
                                "section": sec,
                                "text": it.get("text"),
                                "score": round(score, 3),
                                "hits": int(it.get("hits", 0)),
                                "recency": idx,  # بزرگ‌تر = تازه‌تر
                                "ts": it.get("ts"),
                            }
                        )
        results.sort(key=lambda r: (r["score"], r["hits"], r["recency"]), reverse=True)
        return results[: max(1, int(limit))]

    def recent_tasks(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._data["tasks"]))[: max(1, int(limit))]

    def all_items(self, section: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.get(section, []))

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {s: len(self._data[s]) for s in self.SECTIONS}

    def stats(self) -> Dict[str, Any]:
        """آمار کلی برای داشبورد: نرخ موفقیت، ابزارهای پرکاربرد، روند."""
        with self._lock:
            tasks = list(self._data["tasks"])
        total = len(tasks)
        ok = sum(1 for t in tasks if t.get("ok"))
        tool_counter: Dict[str, int] = {}
        for t in tasks:
            for name in t.get("tools", []):
                tool_counter[name] = tool_counter.get(name, 0) + 1
        top_tools = sorted(tool_counter.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "total_tasks": total,
            "ok_tasks": ok,
            "success_rate": round(ok / total, 2) if total else None,
            "top_tools": [{"tool": k, "count": v} for k, v in top_tools],
        }

    # ------------------------------------------------------------------
    # ساخت زمینهٔ حافظه برای تزریق در پرامپت
    # ------------------------------------------------------------------

    def build_context(self, task: str) -> str:
        """زمینهٔ متنی حافظهٔ مرتبط با تسک — در شروع هر تسک به پرامپت اضافه می‌شود.

        حقایق هسته‌ای: اگر جستجو چیزی مطابق نبود، هم ۲ حقیقت تازه‌ترین/پراستفاده‌ترین
        تزریق می‌شوند تا «همیشه یادش بماند».
        """
        parts: List[str] = []
        facts = self.search(task, sections=["facts"], limit=config.MEMORY_INJECT_FACTS)
        if len(facts) < 2:
            with self._lock:
                existing = {f["text"] for f in facts}
                core = [self._data["facts"][i] for i in range(max(0, len(self._data["facts"]) - 2), len(self._data["facts"]))]
                core = list(reversed(core))
            for it in core:
                if it.get("text") and it["text"] not in existing:
                    facts.append({"section": "facts", "text": it["text"], "score": 0, "hits": int(it.get("hits", 0)), "recency": 0, "ts": it.get("ts")})
            facts = facts[: max(2, config.MEMORY_INJECT_FACTS)]
        if facts:
            lines = "\n".join(f"- {f['text']}" for f in facts)
            parts.append(f"USER/PROJECT FACTS (always remember):\n{lines}")
        lessons = self.search(task, sections=["lessons"], limit=config.MEMORY_INJECT_LESSONS)
        if lessons:
            lines = "\n".join(f"- {l['text']}" for l in lessons)
            parts.append(f"LESSONS FROM PAST MISTAKES (follow these):\n{lines}")
        tasks = self.recent_tasks(config.MEMORY_INJECT_TASKS)
        if tasks:
            lines = "\n".join(
                f"- [{t['ts']}] ok={t['ok']} rounds={t['rounds']} :: {t['task'][:120]}"
                for t in tasks
            )
            parts.append(f"RECENT TASKS (avoid repeating failed approaches):\n{lines}")
        if not parts:
            return ""
        return "\n\n=== MEMORY CONTEXT (persistent, learned in previous sessions) ===\n" + "\n\n".join(parts)


# ----------------------------------------------------------------------
# ابزارهای رجیستری — مدل با این‌ها خودش حافظه را مدیریت می‌کند
# ----------------------------------------------------------------------

def register_memory_tools(registry: Any, mem: MemoryStore) -> None:
    """ثبت ابزارهای حافظه در ToolRegistry."""

    R = registry.register_function

    R(
        name="save_memory",
        description=(
            "ذخیرهٔ ماندگار یک آیتم در حافظهٔ ایجنت. section: "
            "fact (حقیقت/ترجیح پایدار)، lesson (درس از خطا)، note (یادداشت آزاد)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "text": {"type": "string"},
            },
            "required": ["section", "text"],
        },
        func=lambda section, text: (
            mem.add_fact(text) if section == "fact"
            else mem.add_lesson(text) if section == "lesson"
            else mem.add_note(text)
        ),
    )
    R(
        name="search_memory",
        description="جستجوی کلیدواژه‌ای در حافظهٔ ماندگار (حقایق، درس‌ها، یادداشت‌ها)",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        func=lambda query, limit=5: {"ok": True, "results": mem.search(query, limit=limit)},
    )
    R(
        name="list_recent_tasks",
        description="آخرین تسک‌های انجام‌شده با نتیجه و ابزارهای استفاده‌شده",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 5}},
            "required": [],
        },
        func=lambda limit=5: {"ok": True, "tasks": mem.recent_tasks(limit)},
    )
