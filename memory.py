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
from rag import SemanticIndex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_days(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(str(ts))
        return max(0.0, (datetime.now(timezone.utc) - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))).total_seconds() / 86400)
    except Exception:
        return 0.0


class MemoryStore:
    """حافظهٔ بلندمدت ماندگار ایجنت — با Pin/Decay/Tags/بازتاب و نمایهٔ برداری."""

    SECTIONS = ("facts", "lessons", "tasks", "notes", "reflections")
    DECAYABLE = ("facts", "lessons", "notes")  # بخش‌های مشمول فراموشی هوشمند

    def __init__(self, memory_dir: Optional[Path] = None) -> None:
        self.dir = Path(memory_dir or config.MEMORY_DIR)
        self.path = self.dir / "memory.json"
        self._lock = threading.RLock()
        self._data: Dict[str, List[Dict[str, Any]]] = {s: [] for s in self.SECTIONS}
        self._archived: List[Dict[str, Any]] = []
        # نمایهٔ برداری سبک برای بازیابی معنایی (اختیاری با RAG_ENABLED)
        self._index = SemanticIndex(self.dir / "index.json") if config.RAG_ENABLED else None
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
            self._archived = raw.get("archived", []) if isinstance(raw.get("archived", []), list) else []
        except Exception as e:
            # حافظهٔ خراب نباید ایجنت را ببندد — از نو شروع می‌کنیم و بکاپ می‌گذاریم
            try:
                self.path.rename(self.path.with_suffix(".corrupt.bak"))
            except Exception:
                pass
            print(f"⚠️ حافظه قابل خواندن نبود ({e}) — بکاپ گرفته شد و از نو شروع شد.")
        self._index_rebuild_if_empty()
        if config.MEMORY_DECAY_ENABLED:
            self.decay()

    def save(self) -> None:
        """ذخیرهٔ اتمیک: اول در فایل موقت، بعد جایگزینی اتمی."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = dict(self._data)
            payload["archived"] = self._archived
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            if self._index is not None:
                self._index.save()

    # ------------------------------------------------------------------
    # افزودن آیتم
    # ------------------------------------------------------------------

    def add_fact(self, text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._add("facts", text, config.MEMORY_MAX_FACTS, tags)

    def add_lesson(self, text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._add("lessons", text, config.MEMORY_MAX_LESSONS, tags)

    def add_note(self, text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._add("notes", text, 200, tags)

    def add_reflection(self, text: str) -> Dict[str, Any]:
        """بازتاب پس از تسک (SELF-REFLECT): چه خوب پیش رفت / چه چیزی اصلاح شود."""
        return self._add("reflections", text, 100)

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

    def _add(self, section: str, text: str, cap: int, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "متن خالی است"}
        clean_tags = [t.strip().lower()[:24] for t in (tags or []) if t and t.strip()][:6]
        with self._lock:
            items = self._data[section]
            # جلوگیری از تکرار دقیق
            for it in items:
                if it.get("text") == text:
                    it["hits"] = int(it.get("hits", 0)) + 1
                    it["updated"] = _now_iso()
                    if clean_tags:
                        merged = list(dict.fromkeys((it.get("tags") or []) + clean_tags))
                        it["tags"] = merged[:6]
                    if self._index is not None:
                        self._index.add(f"{section}:{it['id']}", text)
                    self.save()
                    return {"ok": True, "dedup": True, "item": it}
            item = {"id": int(time.time() * 1000), "text": text, "ts": _now_iso(), "hits": 0,
                    "pinned": False, "tags": clean_tags}
            items.append(item)
            if len(items) > cap:
                del items[: len(items) - cap]
            if self._index is not None:
                self._index.add(f"{section}:{item['id']}", text)
            self.save()
        return {"ok": True, "item": item}

    # ------------------------------------------------------------------
    # Pin / Tag / حذف / فراموشی هوشمند
    # ------------------------------------------------------------------

    def _find(self, section: str, item_id: int) -> Optional[Dict[str, Any]]:
        for it in self._data.get(section, []):
            if int(it.get("id", 0)) == int(item_id):
                return it
        return None

    def set_pinned(self, section: str, item_id: int, pinned: bool) -> Dict[str, Any]:
        with self._lock:
            it = self._find(section, item_id)
            if not it:
                return {"ok": False, "error": "آیتم یافت نشد"}
            it["pinned"] = bool(pinned)
            self.save()
        return {"ok": True, "item": {"id": it["id"], "pinned": it["pinned"]}}

    def set_tags(self, section: str, item_id: int, tags: List[str]) -> Dict[str, Any]:
        with self._lock:
            it = self._find(section, item_id)
            if not it:
                return {"ok": False, "error": "آیتم یافت نشد"}
            it["tags"] = [t.strip().lower()[:24] for t in (tags or []) if t and t.strip()][:6]
            self.save()
        return {"ok": True, "tags": it["tags"]}

    def delete_item(self, section: str, item_id: int) -> Dict[str, Any]:
        with self._lock:
            items = self._data.get(section, [])
            for i, it in enumerate(items):
                if int(it.get("id", 0)) == int(item_id):
                    del items[i]
                    if self._index is not None:
                        self._index.remove(f"{section}:{item_id}")
                    self.save()
                    return {"ok": True}
        return {"ok": False, "error": "آیتم یافت نشد"}

    def decay(self, max_age_days: Optional[int] = None) -> Dict[str, int]:
        """فراموشی هوشمند: بدون استفاده و کهنه (و بدون Pin) → آرشیو؛ سرآخر پرامپت سبک می‌ماند."""
        days = int(max_age_days or config.MEMORY_DECAY_DAYS)
        moved = 0
        with self._lock:
            for section in self.DECAYABLE:
                keep: List[Dict[str, Any]] = []
                for it in self._data[section]:
                    if (not it.get("pinned")) and int(it.get("hits", 0)) == 0 and _age_days(it.get("ts")) > days:
                        self._archived.append({**it, "archived_from": section, "archived_at": _now_iso()})
                        if self._index is not None:
                            self._index.remove(f"{section}:{it.get('id')}")
                        moved += 1
                    else:
                        keep.append(it)
                self._data[section] = keep
            if moved:
                self.save()
        return {"moved": moved}

    def archived_items(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._archived))[: max(1, int(limit))]

    def restore_archived(self, item_ts: str) -> Dict[str, Any]:
        """بازگرداندن یک آیتم آرشیو‌شده به بخش اصلی‌اش."""
        with self._lock:
            for i, it in enumerate(self._archived):
                if it.get("ts") == item_ts:
                    section = it.get("archived_from", "notes")
                    it.pop("archived_from", None)
                    it.pop("archived_at", None)
                    self._data.get(section, self._data["notes"]).append(it)
                    del self._archived[i]
                    if self._index is not None:
                        self._index.add(f"{section}:{it.get('id')}", it.get("text", ""))
                    self.save()
                    return {"ok": True, "section": section}
        return {"ok": False, "error": "آیتم آرشیو یافت نشد"}

    # ------------------------------------------------------------------
    # نمایهٔ برداری
    # ------------------------------------------------------------------

    def _index_rebuild_if_empty(self) -> None:
        """اگر نمایه خالی ولی حافظه پُر است (مثلاً مهاجرت پروژه قدیمی) → بازسازی."""
        if self._index is None:
            return
        with self._lock:
            if self._index._n > 0:
                return
            for section in ("facts", "lessons", "notes", "reflections"):
                for it in self._data[section]:
                    self._index.add(f"{section}:{it.get('id')}", it.get("text", ""))

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
        """جستجوی ترکیبی: کلیدواژه‌ای + برداری (RAG) — مرتب‌سازی بر اساس امتیازِ آمیخته.

        امتیاز نهایی = 0.65×کلیدواژه + 0.35×شباهت کسینوسی + پاداشِ Pin/استفاده/تازگی.
        """
        qtokens = self._tokenize(query)
        sections = sections or ["facts", "lessons", "notes"]
        results: Dict[int, Dict[str, Any]] = {}
        with self._lock:
            for sec in sections:
                for idx, it in enumerate(self._data.get(sec, [])):
                    kw = self._score(qtokens, it.get("text", ""))
                    if kw >= min_score:
                        results[int(it["id"])] = {
                            "section": sec,
                            "id": it.get("id"),
                            "text": it.get("text"),
                            "kw_score": round(kw, 3),
                            "sem_score": 0.0,
                            "hits": int(it.get("hits", 0)),
                            "recency": idx,
                            "ts": it.get("ts"),
                            "pinned": bool(it.get("pinned")),
                            "tags": it.get("tags") or [],
                        }
            # لایهٔ برداری — اسنادی که کلیدواژه‌ای نمی‌گرفت ولی معنایی نزدیک‌اند
            if self._index is not None:
                for hit in self._index.search(query, k=max(10, limit * 3)):
                    sec, _, iid = hit["id"].partition(":")
                    if sec not in sections or iid == "":
                        continue
                    it = self._find(sec, int(iid)) if hasattr(self, "_find") else None
                    if it is None:
                        continue
                    item_id = int(it["id"])
                    if item_id in results:
                        results[item_id]["sem_score"] = round(hit["score"], 3)
                    else:
                        results[item_id] = {
                            "section": sec, "id": it.get("id"), "text": it.get("text"),
                            "kw_score": 0.0, "sem_score": round(hit["score"], 3),
                            "hits": int(it.get("hits", 0)), "recency": 0, "ts": it.get("ts"),
                            "pinned": bool(it.get("pinned")), "tags": it.get("tags") or [],
                        }
        merged = [r for r in results.values() if (r["kw_score"] >= min_score or r["sem_score"] > 0.0)]
        for r in merged:
            r["score"] = round(
                0.65 * r["kw_score"] + 0.35 * min(1.0, r["sem_score"] * 2)
                + (0.25 if r["pinned"] else 0.0)
                + min(0.1, r["hits"] * 0.02),
                3,
            )
        merged.sort(key=lambda r: (r["score"], r["hits"], r["recency"]), reverse=True)
        return merged[: max(1, int(limit))]

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
        """زمینهٔ متنی حافظهٔ مرتبط با تسک — Pin شده‌ها همیشه اول، بعد ترکیب کلیدواژه+RAG.

        اگر جستجو چیزی نیافت، ۲ حقیقتِ تازه‌ترین/پراستفاده‌ترین تزریق می‌شود تا
        «همیشه یادش بماند». بازتاب‌های اخیر هم برای اصلاح رفتار تزریق می‌شوند.
        """
        parts: List[str] = []
        facts = self.search(task, sections=["facts"], limit=config.MEMORY_INJECT_FACTS)
        with self._lock:
            pinned = [f for f in self._data["facts"] if f.get("pinned")]
            existing = {f["text"] for f in facts}
        for it in reversed(pinned[-4:]):
            if it["text"] not in existing:
                facts.insert(0, {"section": "facts", "id": it.get("id"), "text": it["text"], "score": 1.0,
                                 "hits": int(it.get("hits", 0)), "recency": 0, "pinned": True, "tags": it.get("tags") or []})
                existing.add(it["text"])
        if len(facts) < 2:
            with self._lock:
                core = [self._data["facts"][i] for i in range(max(0, len(self._data["facts"]) - 2), len(self._data["facts"]))]
                core = list(reversed(core))
            for it in core:
                if it.get("text") and it["text"] not in existing:
                    facts.append({"section": "facts", "id": it.get("id"), "text": it["text"], "score": 0,
                                  "hits": int(it.get("hits", 0)), "recency": 0, "pinned": bool(it.get("pinned")), "tags": it.get("tags") or []})
            facts = facts[: max(2, config.MEMORY_INJECT_FACTS)]
        if facts:
            def _fmt(f: Dict[str, Any]) -> str:
                tag = f" [#{';'.join(f['tags'])}]" if f.get("tags") else ""
                pin = " 📌" if f.get("pinned") else ""
                return f"- {f['text']}{tag}{pin}"
            lines = "\n".join(_fmt(f) for f in facts)
            parts.append(f"USER/PROJECT FACTS (always remember; 📌 = pinned by user):\n{lines}")
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
        with self._lock:
            reflections = list(self._data["reflections"])[-2:]
        if reflections:
            lines = "\n".join(f"- {r['text'][:220]}" for r in reversed(reflections))
            parts.append(f"YOUR RECENT SELF-REFLECTIONS (apply these improvements):\n{lines}")
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
            "fact (حقیقت/ترجیح پایدار)، lesson (درس از خطا)، note (یادداشت آزاد). "
            "tags اختیاری برای دسته‌بندی (مثلاً [\"git\", \"priority\"])"
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["section", "text"],
        },
        func=lambda section, text, tags=None: (
            mem.add_fact(text, tags) if section == "fact"
            else mem.add_lesson(text, tags) if section == "lesson"
            else mem.add_note(text, tags)
        ),
    )
    R(
        name="search_memory",
        description="جستجوی ترکیبی (کلیدواژه + برداری) در حافظهٔ ماندگار",
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
    R(
        name="pin_memory",
        description="سنجاق کردن یک آیتم حافظه (fact/note/lesson) — همیشه در پرامپت تزریق می‌شود و مشمول فراموشی نمی‌شود",
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "item_id": {"type": "integer", "description": "شناسهٔ آیتم — از search_memory/all_memory"},
            },
            "required": ["section", "item_id"],
        },
        func=lambda section, item_id: mem.set_pinned(
            {"fact": "facts", "lesson": "lessons", "note": "notes"}[section], int(item_id), True
        ),
    )
    R(
        name="unpin_memory",
        description="برداشتن سنجاق از یک آیتم حافظه",
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "item_id": {"type": "integer"},
            },
            "required": ["section", "item_id"],
        },
        func=lambda section, item_id: mem.set_pinned(
            {"fact": "facts", "lesson": "lessons", "note": "notes"}[section], int(item_id), False
        ),
    )
    R(
        name="tag_memory",
        description="تعیین/بازنویسی تگ‌های یک آیتم حافظه برای دسته‌بندی و فیلتر",
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "item_id": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["section", "item_id", "tags"],
        },
        func=lambda section, item_id, tags: mem.set_tags(
            {"fact": "facts", "lesson": "lessons", "note": "notes"}[section], int(item_id), tags or []
        ),
    )
    R(
        name="delete_memory_item",
        description="حذف یک آیتم اشتباه/کهنه از حافظه (fact/lesson/note)",
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["fact", "lesson", "note"]},
                "item_id": {"type": "integer"},
            },
            "required": ["section", "item_id"],
        },
        func=lambda section, item_id: mem.delete_item(
            {"fact": "facts", "lesson": "lessons", "note": "notes"}[section], int(item_id)
        ),
    )
    R(
        name="all_memory",
        description="فهرست کامل یک بخش حافظه با idها (برای pin/tag/delete) — بخش‌ها: facts/lessons/notes/reflections",
        parameters={
            "type": "object",
            "properties": {"section": {"type": "string", "enum": ["facts", "lessons", "notes", "reflections"]}},
            "required": ["section"],
        },
        func=lambda section: {"ok": True, "items": [
            {"id": i.get("id"), "text": (i.get("text") or "")[:160], "pinned": bool(i.get("pinned")),
             "tags": i.get("tags") or [], "hits": int(i.get("hits", 0)), "ts": i.get("ts")}
            for i in mem.all_items(section)
        ]},
    )
