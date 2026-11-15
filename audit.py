# -*- coding: utf-8 -*-
"""لاگ ممیزی غیرقابل‌دستکاری (Audit Log) — زنجیرهٔ هش append-only.

هر رویداد (اجرای ابزار، تصمیم مدل، تایید/رد عملیات خطرناک، سوییچ پروژه، اجرای
زمان‌بندی و…) با شمارهٔ ترتیبی و هشِ SHA-256 زنجیر می‌شود:

    hash_n = sha256( hash_{n-1} + json_canonical(entry_without_hash) )

اگر کسی حتی یک فیلد از خطوط قبلی را دستکاری کند، `verify()` شکستگی زنجیره را
دقیق با شمارهٔ خط گزارش می‌کند. فایل: `audit/chain.jsonl` (append-only).
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

GENESIS = "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class AuditLog:
    """دفتر رویدادهای زنجیره‌شده با هش — شواهد تغییرناپذیر."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._entries: List[Dict[str, Any]] = []
        self._prev = GENESIS
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                self._entries.append(e)
            if self._entries:
                self._prev = self._entries[-1].get("hash", GENESIS)
        except Exception:
            pass

    # ------------------------------------------------------------------

    def append(
        self,
        event: str,
        data: Optional[Dict[str, Any]] = None,
        project: Optional[int] = None,
        actor: str = "agent",
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "seq": len(self._entries) + 1,
            "ts": _now_iso(),
            "event": str(event),
            "project": project,
            "actor": actor,
            "data": data or {},
        }
        with self._lock:
            entry["prev"] = self._prev
            entry["hash"] = hashlib.sha256(_canon({k: v for k, v in entry.items() if k != "hash"}).encode("utf-8")).hexdigest()
            self._entries.append(entry)
            self._prev = entry["hash"]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # ------------------------------------------------------------------

    def verify(self) -> Dict[str, Any]:
        """بازمحاسبهٔ کل زنجیره — گزارش دقیق اولین شکستگی."""
        prev = GENESIS
        with self._lock:
            entries = list(self._entries)
        for i, e in enumerate(entries):
            expected_payload = {k: v for k, v in e.items() if k != "hash"}
            expected_payload["prev"] = prev
            h = hashlib.sha256(_canon(expected_payload).encode("utf-8")).hexdigest()
            if e.get("prev") != prev or e.get("hash") != h or e.get("seq") != i + 1:
                return {"ok": False, "broken_at": i, "entries": len(entries),
                        "reason": f"خط {i + 1} با زنجیره سازگار نیست — فایل دستکاری شده"}
            prev = e.get("hash", GENESIS)
        return {"ok": True, "broken_at": None, "entries": len(entries)}

    def list(self, limit: int = 50, project: Optional[int] = None, event: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._entries)
        if project is not None:
            items = [e for e in items if e.get("project") == int(project)]
        if event:
            items = [e for e in items if e.get("event") == event]
        return list(reversed(items))[: max(1, int(limit))]
