# -*- coding: utf-8 -*-
"""زمان‌بند تسک‌ها (Scheduler) — اجرای خودکار تسک با فاصلهٔ منظم یا ساعت روز.

هر job: {id, project_id, name, task, kind: interval|daily, every_s|at, enabled,
last_run, next_run, run_count}. نخ پس‌زمینه هر ۲۰ ثانیه چک می‌کند؛ سرِ موعد
`fire_callback(job)` صدا زده می‌شود (سرور: ساخت موتور گذرا برای پروژهٔ همان job
+ اجرای تسک + اعلان). jobها در jobs.json ماندگارند و از API/UI ساخت/ویرایش می‌شوند.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """تبدیل ورودی کاربر به kind/every_s/at معتبر — خطا با dict برمی‌گردد."""
    kind = (spec.get("kind") or "interval").lower()
    if kind == "interval":
        every = spec.get("every_s") or spec.get("every_minutes", 0) * 60
        try:
            every = int(every)
        except (TypeError, ValueError):
            return {"error": "every_s نامعتبر است"}
        if every < 30:
            return {"error": "حداقل فاصله ۳۰ ثانیه است"}
        return {"kind": "interval", "every_s": every}
    if kind == "daily":
        at = str(spec.get("at") or "")
        try:
            hh, mm = at.split(":")
            assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        except Exception:
            return {"error": "at باید HH:MM باشد (به وقت UTC)"}
        return {"kind": "daily", "at": f"{int(hh):02d}:{int(mm):02d}"}
    return {"error": "kind باید interval یا daily باشد"}


class Scheduler:
    """زمان‌بند ماندگار با نخ پس‌زمینه."""

    TICK_S = 20

    def __init__(self, path: Optional[Path] = None, fire_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.path = Path(path)
        self.fire_callback = fire_callback
        self._lock = threading.RLock()
        self._jobs: List[Dict[str, Any]] = []
        self._next_id = 1
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._jobs = raw.get("jobs", [])
            self._next_id = int(raw.get("next_id", len(self._jobs) + 1))
        except Exception:
            self._jobs, self._next_id = [], 1

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self._next_id, "jobs": self._jobs}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------------

    @staticmethod
    def _next_after(job: Dict[str, Any], now: datetime) -> str:
        """سرِ موعد بعدی بر اساس نوع job.

        باگ پاس ۴: قبلاً همهٔ jobها (حتی daily) ۵ ثانیه بعد از ساخت می‌فایریدند —
        jobِ «هر روز ساعت ۹» بلافاصله بعد از ساخت اجرا می‌شد. حالا:
        interval → یک فاصلهٔ کامل بعد؛ daily → نزدیک‌ترین موعدِ at (امروز یا فردا).
        """
        if job.get("kind") == "daily":
            hh, mm = str(job.get("at", "00:00")).split(":")
            nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            return _iso(nxt)
        return _iso(now + timedelta(seconds=int(job.get("every_s", 60))))

    def add(self, project_id: int, name: str, task: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        parsed = parse_spec(spec)
        if "error" in parsed:
            return {"ok": False, "error": parsed["error"]}
        with self._lock:
            job = {
                "id": self._next_id, "project_id": int(project_id),
                "name": (name or "job")[:60], "task": (task or "").strip()[:400],
                **parsed, "enabled": True,
                "last_run": None, "run_count": 0,
            }
            job["next_run"] = self._next_after(job, _now())
            self._next_id += 1
            self._jobs.append(job)
            self.save()
        return {"ok": True, "job": job}

    def remove(self, job_id: int) -> Dict[str, Any]:
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j["id"] != int(job_id)]
            self.save()
        return {"ok": len(self._jobs) < before}

    def set_enabled(self, job_id: int, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            for j in self._jobs:
                if j["id"] == int(job_id):
                    j["enabled"] = bool(enabled)
                    if enabled:
                        j["next_run"] = self._next_after(j, _now())  # نه now+5s — باگ پاس ۴
                    self.save()
                    return {"ok": True, "job": j}
        return {"ok": False, "error": "job یافت نشد"}

    def list(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = [dict(j) for j in self._jobs]
        if project_id is not None:
            jobs = [j for j in jobs if j["project_id"] == int(project_id)]
        return jobs

    # ------------------------------------------------------------------

    def _due(self, job: Dict[str, Any], now: datetime) -> bool:
        if not job.get("enabled"):
            return False
        try:
            nxt = datetime.fromisoformat(job["next_run"])
        except Exception:
            return True
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return now >= nxt

    def _advance(self, job: Dict[str, Any], now: datetime) -> None:
        if job["kind"] == "interval":
            job["next_run"] = _iso(now + timedelta(seconds=int(job["every_s"])))
        else:
            hh, mm = job["at"].split(":")
            nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            job["next_run"] = _iso(nxt)
        job["last_run"] = _iso(now)
        job["run_count"] = int(job.get("run_count", 0)) + 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = _now()
            due_jobs = []
            with self._lock:
                for j in self._jobs:
                    if self._due(j, now):
                        self._advance(j, now)
                        due_jobs.append(dict(j))
                if due_jobs:
                    self.save()
            for j in due_jobs:
                try:
                    if self.fire_callback:
                        self.fire_callback(j)
                except Exception:
                    pass
            self._stop.wait(self.TICK_S)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="devx-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
