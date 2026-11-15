# -*- coding: utf-8 -*-
"""چک‌پوینت حلقهٔ ایجنت (Checkpoint / Resume) — نجات تسک از وسط راه.

در پایان هر دور، وضعیت (تسک، پروژه، سشن، شمارهٔ دور، ردابزارها) ذخیره می‌شود؛
اگر فرایند/اتصال وسط تسک بُرد یا کاربر خواست متوقف کند، با `resume` گفت‌وگوی
جددی باز می‌شود، خلاصهٔ پیشرفت قبلی به پرامپت تزریق می‌شود و حلقه از همان‌جا
ادامه می‌دهد — بدون تکرار کارهای انجام‌شده.
"""
from __future__ import annotations

import config
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CheckpointStore:
    """ذخیرهٔ چک‌پوینت‌های حلقه در checkpoints/<id>.json."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base = Path(base_dir or (config.BASE_DIR / "checkpoints"))
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def save(
        self,
        task: str,
        project: Dict[str, Any],
        session_id: Optional[int],
        round_no: int,
        trace: List[Dict[str, Any]],
        partial: str = "",
        status: str = "running",
    ) -> Dict[str, Any]:
        cp = {
            "id": f"cp-{int(datetime.now().timestamp() * 1000)}",
            "ts": _now_iso(),
            "task": task[:400],
            "project": {"id": project.get("id"), "name": project.get("name"), "key": project.get("key")},
            "session_id": session_id,
            "round": int(round_no),
            "trace": trace,
            "partial": (partial or "")[:2000],
            "status": status,
        }
        with self._lock:
            (self.base / f"{cp['id']}.json").write_text(
                json.dumps(cp, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            self._gc()
        return cp

    def _gc(self) -> None:
        """بیشینهٔ ۴۰ چک‌پوینت — قدیمی‌ها حذف می‌شوند."""
        files = sorted(self.base.glob("cp-*.json"))
        for f in files[:-40]:
            try:
                f.unlink()
            except OSError:
                pass

    def load(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        p = self.base / f"{checkpoint_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.base.glob("cp-*.json"), reverse=True)[: max(1, int(limit))]:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def mark(self, checkpoint_id: str, status: str) -> None:
        p = self.base / f"{checkpoint_id}.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["status"] = status
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
