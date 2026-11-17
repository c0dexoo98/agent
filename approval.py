# -*- coding: utf-8 -*-
"""صف تایید عملیات خطرناک (Approval Workflow) — انسان در حلقه (Human-in-the-loop).

هر ابزار `dangerous=True` (delete_repo، fs_delete، run_shell و…) قبل از اجرا باید
تایید کاربر را بگیرد:
  ۱) مدل ابزار را صدا می‌زند → رجیستری/سرور به‌جای اجرا، یک درخواست تایید می‌سازد
     و به مدل «در انتظار تایید انسان» برمی‌گرداند.
  ۲) کاربر در UI کارت تایید را با پیش‌نمایش آرگومان‌ها می‌بیند → تایید/رد.
  ۳) نخِ موتور با `wait()` (شرط‌بندی + تایم‌اوت) منتظر تصمیم می‌ماند؛ در تایید،
     ابزار عیناً با همان آرگومان‌ها اجرا و در لاگ ممیزی ثبت می‌شود؛ در رد/تایم‌اوت،
     مدل خطای «رد شد» می‌گیرد و می‌تواند مسیر امن‌تر را انتخاب کند.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(obj: Any) -> Any:
    """آرگومان‌های غیر-JSON (بایت/ست/شیء دلخواه) نباید ذخیره‌سازی صف را بترکانند.

    باگ پاس ۹: ابزار خطرناک با آرگومان غیر-سریال‌پذیر، TypeError را از execute
    فرار می‌داد و حلقهٔ ایجنت را می‌کشت — اینجا در مرزِ صف، مقادیر عجیب به رشته تبدیل می‌شوند.
    """
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return json.loads(json.dumps(obj, default=repr))


class ApprovalManager:
    """مدیریت درخواست‌های تایید با انتظار بلوکه‌شده و TTL."""

    def __init__(self, path: Optional[Path] = None, ttl_s: int = 600) -> None:
        self.path = Path(path)
        self.ttl_s = int(ttl_s)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._items: Dict[str, Dict[str, Any]] = {}
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for it in raw.get("items", []):
                self._items[it["id"]] = it
            self._next_id = int(raw.get("next_id", len(self._items) + 1))
        except Exception:
            self._items, self._next_id = {}, 1

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"next_id": self._next_id, "items": list(self._items.values())}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ------------------------------------------------------------------

    def request(self, tool: str, arguments: Dict[str, Any], project: Optional[int] = None) -> Dict[str, Any]:
        """ساخت درخواست تایید — خروجی برای مدل: pending_approval."""
        with self._cond:
            self._gc()
            aid = f"ap-{self._next_id}"
            self._next_id += 1
            item = {
                "id": aid,
                "ts": _now_iso(),
                "tool": str(tool),
                "arguments": _json_safe(arguments or {}),
                "project": project,
                "status": "pending",
                "resolved_ts": None,
                "resolved_by": None,
            }
            self._items[aid] = item
            self.save()
            return item

    def _gc(self) -> None:
        """تکلیف‌شده‌های کهنه پاک می‌شوند (pendingها دست‌نخورده) — صف بی‌نهایت نشود."""
        if len(self._items) <= 200:
            return
        resolved = [k for k, v in self._items.items() if v.get("status") in ("approved", "rejected", "timeout")]
        for k in resolved[:-100]:  # آخرین ۱۰۰ تکلیف‌شده به‌عنوان تاریخچه می‌ماند
            del self._items[k]

    def wait(self, approval_id: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
        """انتظارِ نخِ موتور تا تایید/رد/تایم‌اوت — مقدار برشمردهٔ وضعیت نهایی."""
        deadline = time.monotonic() + (self.ttl_s if timeout_s is None else int(timeout_s))
        with self._cond:
            while True:
                item = self._items.get(approval_id)
                if not item:
                    return {"status": "missing", "ok": False}
                if item["status"] in ("approved", "rejected"):
                    return {"status": item["status"], "ok": item["status"] == "approved", "item": item}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    item["status"] = "timeout"
                    item["resolved_ts"] = _now_iso()
                    self.save()
                    return {"status": "timeout", "ok": False, "item": item}
                self._cond.wait(timeout=min(remaining, 5.0))

    def resolve(self, approval_id: str, approved: bool, by: str = "user") -> Dict[str, Any]:
        with self._cond:
            item = self._items.get(approval_id)
            if not item:
                return {"ok": False, "error": "درخواست تایید یافت نشد"}
            if item["status"] != "pending":
                return {"ok": False, "error": f"این درخواست قبلاً ({item['status']}) تعیین تکلیف شده"}
            item["status"] = "approved" if approved else "rejected"
            item["resolved_ts"] = _now_iso()
            item["resolved_by"] = by
            self.save()
            self._cond.notify_all()
            return {"ok": True, "item": item}

    def pending(self, project: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._cond:
            items = [dict(i) for i in self._items.values() if i["status"] == "pending"]
        if project is not None:
            items = [i for i in items if i.get("project") in (None, int(project))]
        return items

    def history(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._cond:
            items = [dict(i) for i in self._items.values()]
        items.sort(key=lambda i: i.get("resolved_ts") or i["ts"], reverse=True)
        return items[: max(1, int(limit))]
