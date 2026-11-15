# -*- coding: utf-8 -*-
"""داشبورد مصرف (Usage & Cost) — شمارش تسک/دور/ابزار/توکن به‌تفکیک روز و پروژه.

توکن دقیق از DOM دیپ‌سیک قابل‌خواندن نیست، پس برآورد استاندارد صنعت استفاده
می‌شود: هر توکن ≈ ۴ کاراکتر (برای متن فارسی/انگلیسی مخلوط تقریب قابل‌قبولی است).
همهٔ اعداد با کد جمع می‌شوند؛ داشبورد UI و API از همین فایل می‌خوانند.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class UsageTracker:
    """نگهداری مصرف بر حسب روز/پروژه + جمع کل (ماندگار در usage.json)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"days": {}, "totals": self._empty()}
        self._load()

    @staticmethod
    def _empty() -> Dict[str, int]:
        return {"tasks": 0, "ok_tasks": 0, "rounds": 0, "tool_calls": 0,
                "duration_s": 0.0, "est_tokens_in": 0, "est_tokens_out": 0}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "days" in raw:
                self._data = {"days": raw.get("days", {}), "totals": raw.get("totals", self._empty())}
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------------

    def record(
        self,
        project_id: Optional[int],
        ok: bool,
        rounds: int = 0,
        tool_calls: int = 0,
        duration_s: float = 0.0,
        prompt_chars: int = 0,
        answer_chars: int = 0,
    ) -> None:
        """ثبت مصرف یک تسک تمام‌شده."""
        est_in = int(prompt_chars / 4)
        est_out = int(answer_chars / 4)
        with self._lock:
            day = self._data["days"].setdefault(_day(), {"projects": {}, "totals": self._empty()})
            targets = [day["totals"], self._data["totals"]]
            if project_id is not None:
                targets.append(day["projects"].setdefault(str(project_id), self._empty()))
            for t in targets:
                t["tasks"] += 1
                t["ok_tasks"] += 1 if ok else 0
                t["rounds"] += int(rounds)
                t["tool_calls"] += int(tool_calls)
                t["duration_s"] += round(float(duration_s), 1)
                t["est_tokens_in"] += est_in
                t["est_tokens_out"] += est_out
            self.save()

    # ------------------------------------------------------------------

    def summary(self, days: int = 14) -> Dict[str, Any]:
        """خلاصه برای داشبورد: روند روزانه + تفکیک پروژه + جمع کل."""
        with self._lock:
            all_days = dict(self._data["days"])
        keys = sorted(all_days.keys())[-max(1, int(days)):]
        trend = []
        for k in keys:
            d = all_days[k]
            trend.append({
                "date": k,
                "tasks": d["totals"]["tasks"],
                "rounds": d["totals"]["rounds"],
                "tool_calls": d["totals"]["tool_calls"],
                "est_tokens": d["totals"]["est_tokens_in"] + d["totals"]["est_tokens_out"],
            })
        by_project: Dict[str, Any] = {}
        for k in keys:
            for pid, stats in all_days[k].get("projects", {}).items():
                agg = by_project.setdefault(pid, self._empty())
                for f in agg:
                    agg[f] += stats.get(f, 0)
        return {"trend": trend, "by_project": by_project, "totals": dict(self._data["totals"])}
