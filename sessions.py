# -*- coding: utf-8 -*-
"""سشن‌های چت (Sessions) — چند گفت‌وگوی موازی روی یک پروژه با حافظهٔ مشترک.

هر پروژه چند سشن دارد (مثل تب‌های چت): هر سشن تاریخچهٔ تسک/رویداد جدا دارد ولی
حافظه و مهارت‌ها همان پروژه را می‌بینند — «حافظهٔ مشترک، مکالمهٔ ایزوله».
در فایل sessions.json داخل دایرکتوری حافظهٔ همان پروژه ذخیره می‌شود تا export
پروژه سشن‌هایش را هم ببرد. رویدادهای موتور با session_id برچسب می‌خورند و UI
بر اساس سشن فعال فیلتر می‌کند.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionManager:
    """مدیریت سشن‌های هر پروژه (per-project file)."""

    def __init__(self, project_key: str, memory_dir: Path) -> None:
        self.key = project_key
        self.path = Path(memory_dir) / "sessions.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sessions": [], "active_id": None, "seq": 0}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {
                        "sessions": raw.get("sessions", []),
                        "active_id": raw.get("active_id"),
                        "seq": int(raw.get("seq", 0)),
                    }
            except Exception:
                pass
        if not self._data["sessions"]:
            self._data["seq"] += 1
            self._data["sessions"].append({"id": self._data["seq"], "title": "چت اصلی", "created": _now_iso(), "last_active": _now_iso(), "task_count": 0})
            self._data["active_id"] = 1
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self._data["sessions"]]

    def active(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == self._data["active_id"]:
                    return s
            return self._data["sessions"][-1] if self._data["sessions"] else None

    def active_id(self) -> Optional[int]:
        s = self.active()
        return s["id"] if s else None

    def create(self, title: str = "") -> Dict[str, Any]:
        with self._lock:
            self._data["seq"] += 1
            s = {"id": self._data["seq"], "title": (title or f"چت {self._data['seq']}")[:60],
                 "created": _now_iso(), "last_active": _now_iso(), "task_count": 0}
            self._data["sessions"].append(s)
            self._data["active_id"] = s["id"]
            self._save()
        return s

    def select(self, session_id: int) -> Dict[str, Any]:
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == int(session_id):
                    self._data["active_id"] = s["id"]
                    s["last_active"] = _now_iso()
                    self._save()
                    return s
        return {"error": "سشن یافت نشد"}

    def rename(self, session_id: int, title: str) -> Dict[str, Any]:
        title = (title or "").strip()[:60]
        if not title:
            return {"ok": False, "error": "عنوان خالی است"}
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == int(session_id):
                    s["title"] = title
                    self._save()
                    return {"ok": True, "session": s}
        return {"ok": False, "error": "سشن یافت نشد"}

    def delete(self, session_id: int) -> Dict[str, Any]:
        with self._lock:
            if len(self._data["sessions"]) <= 1:
                return {"ok": False, "error": "آخرین سشن قابل حذف نیست"}
            self._data["sessions"] = [s for s in self._data["sessions"] if s["id"] != int(session_id)]
            if self._data["active_id"] == int(session_id):
                self._data["active_id"] = self._data["sessions"][-1]["id"]
            self._save()
        return {"ok": True}

    def record_task(self, session_id: Optional[int]) -> None:
        with self._lock:
            for s in self._data["sessions"]:
                if s["id"] == (session_id or self._data["active_id"]):
                    s["task_count"] = int(s.get("task_count", 0)) + 1
                    s["last_active"] = _now_iso()
                    self._save()
                    return
