# -*- coding: utf-8 -*-
"""جستجوی سراسری (Global Search) — یک جستجو روی همهٔ پروژه‌ها/مهارت‌ها/اعلان‌ها.

فیلترها: نوع (fact/note/lesson/task/skill/project)، پروژه، بازهٔ تاریخ. برای
Cmd+K رابط وب و ابزار search_everything مدل. خواندن مستقیم از فایل‌های حافظهٔ
هر پروژه — بدون نیاز به سوییچ کردن حافظهٔ فعال.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory import MemoryStore  # فقط برای _tokenize/_score استاتیک


def search_all(
    projects: List[Dict[str, Any]],
    memory_root: Path,
    skills_root: Path,
    query: str,
    kind: Optional[str] = None,
    project_id: Optional[int] = None,
    limit_per: int = 5,
) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "عبارت جستجو خالی است"}
    results: List[Dict[str, Any]] = []

    for rec in projects:
        if project_id is not None and rec.get("id") != int(project_id):
            continue
        key = rec.get("key")
        mfile = Path(memory_root) / "projects" / str(key) / "memory.json"
        if mfile.exists():
            try:
                data = json.loads(mfile.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for section, item_kind in (("facts", "fact"), ("notes", "note"), ("lessons", "lesson")):
                if kind and kind not in (item_kind,):
                    continue
                for it in data.get(section, []):
                    score = MemoryStore._score(MemoryStore._tokenize(q), it.get("text", ""))
                    if score >= 0.15:
                        results.append({
                            "kind": item_kind, "project_id": rec["id"], "project_name": rec["name"],
                            "text": it.get("text", ""), "score": round(score, 3), "ts": it.get("ts"),
                        })
            for t in data.get("tasks", [])[-40:]:
                if kind and kind != "task":
                    continue
                score = MemoryStore._score(MemoryStore._tokenize(q), t.get("task", ""))
                if score >= 0.15:
                    results.append({"kind": "task", "project_id": rec["id"], "project_name": rec["name"],
                                    "text": t.get("task", ""), "score": round(score, 3), "ts": t.get("ts"), "ok": t.get("ok")})

        sdir = Path(skills_root) / "projects" / str(key)
        if sdir.exists():
            for md in sdir.glob("*.md"):
                try:
                    raw = md.read_text(encoding="utf-8")
                except OSError:
                    continue
                score = MemoryStore._score(MemoryStore._tokenize(q), raw[:2000])
                if score >= 0.15 and (not kind or kind == "skill"):
                    name = md.stem
                    for line in raw.splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                            break
                    results.append({"kind": "skill", "project_id": rec["id"], "project_name": rec["name"],
                                    "text": name, "score": round(score, 3), "ts": None})

    results.sort(key=lambda r: r["score"], reverse=True)
    return {"ok": True, "count": len(results), "results": results[: max(1, int(limit_per) * 8)]}
