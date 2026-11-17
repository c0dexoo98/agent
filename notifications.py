# -*- coding: utf-8 -*-
"""مرکز اعلان‌ها (Notification Center) — یک صدا برای همهٔ رویدادهای مهم.

منابع اعلان: Watcher گیت‌هاب (Issue/PR/push جدید)، زمان‌بند (اجرای job)، صف تایید
(عملیات خطرناک)، سلامت سیستم (خطای اتصال و…). هر اعلان read/unread دارد و UI با
زنگِ بالای صفحه و بجِ تعداد نشان می‌دهد؛ مدل هم با ابزار list_notifications می‌تواند بخواند.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_ITEMS = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NotificationCenter:
    """صف اعلان‌های ماندگار با وضعیت خوانده‌نشده."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = raw.get("items", []) if isinstance(raw, dict) else []
            self._next_id = int(raw.get("next_id", len(self._items) + 1))
        except Exception:
            self._items, self._next_id = [], 1

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"next_id": self._next_id, "items": self._items}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # ------------------------------------------------------------------

    def push(self, kind: str, title: str, body: str = "", project: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            item = {
                "id": self._next_id,
                "ts": _now_iso(),
                "kind": kind,          # watch | schedule | approval | system | task
                "title": title[:200],
                "body": (body or "")[:500],
                "project": project,
                "read": False,
            }
            self._next_id += 1
            self._items.append(item)
            if len(self._items) > MAX_ITEMS:
                del self._items[: len(self._items) - MAX_ITEMS]
            self.save()
        return item

    def list(self, unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(reversed(self._items))
        if unread_only:
            items = [i for i in items if not i["read"]]
        return items[: max(1, int(limit))]

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._items if not i["read"])

    def mark_all_read(self) -> int:
        with self._lock:
            n = 0
            for i in self._items:
                if not i["read"]:
                    i["read"] = True
                    n += 1
            self.save()
        return n

    def mark_read(self, item_id: int) -> bool:
        with self._lock:
            for i in self._items:
                if i["id"] == int(item_id):
                    i["read"] = True
                    self.save()
                    return True
        return False
