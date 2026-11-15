# -*- coding: utf-8 -*-
"""خط زمانی پروژه (Timeline) — روایت یکپارچهٔ چه‌چیزهایی کِی در این پروژه رخ داد.

منابع ادغام‌شده: تسک‌های حافظه، مهارت‌های ساخته/به‌روزشده، snapshotها، بازتاب‌ها
(reflections)، اجرای jobهای زمان‌بندی و رویدادهای ممیزی — همه مرتب‌شده بر زمان.
UI در پنل «خط زمانی» و API در /api/timeline از همین می‌خوانند.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_timeline(
    project_key: str,
    memory_dir: Path,
    skills_dir: Path,
    snapshots: Optional[List[Dict[str, Any]]] = None,
    audit_entries: Optional[List[Dict[str, Any]]] = None,
    jobs: Optional[List[Dict[str, Any]]] = None,
    limit: int = 80,
) -> List[Dict[str, Any]]:
    """ساخت خط زمانی ادغام‌شدهٔ پروژه (تازه‌ترین اول)."""
    events: List[Dict[str, Any]] = []

    memory_dir = Path(memory_dir)
    mem_file = memory_dir / "memory.json"
    if mem_file.exists():
        try:
            data = json.loads(mem_file.read_text(encoding="utf-8"))
            for t in data.get("tasks", []):
                events.append({"ts": t.get("ts"), "kind": "task", "ok": bool(t.get("ok")),
                               "text": t.get("task", ""), "rounds": t.get("rounds")})
            for s in data.get("facts", [])[-20:]:
                events.append({"ts": s.get("ts"), "kind": "fact", "text": s.get("text", "")})
            for l in data.get("lessons", [])[-20:]:
                events.append({"ts": l.get("ts"), "kind": "lesson", "text": l.get("text", "")})
            for r in data.get("reflections", [])[-20:]:
                events.append({"ts": r.get("ts"), "kind": "reflection", "text": (r.get("text") or "")[:160]})
        except Exception:
            pass

    skills_dir = Path(skills_dir)
    if skills_dir.exists():
        for md in skills_dir.glob("*.md"):
            try:
                ts = md.stat().st_mtime
                import datetime as _dt
                iso = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")
                events.append({"ts": iso, "kind": "skill", "text": md.stem})
            except OSError:
                continue

    for s in (snapshots or []):
        events.append({"ts": s.get("created"), "kind": "snapshot", "text": s.get("label") or s.get("id")})

    for e in (audit_entries or []):
        if e.get("event") in ("project_switched", "approval_resolved", "schedule_fired", "snapshot_restored", "import_done"):
            events.append({"ts": e.get("ts"), "kind": e["event"], "text": json.dumps(e.get("data", {}), ensure_ascii=False)[:140]})

    for j in (jobs or []):
        if j.get("last_run"):
            events.append({"ts": j.get("last_run"), "kind": "schedule", "text": f"{j.get('name')} — {j.get('task', '')[:80]}"})

    events = [e for e in events if e.get("ts")]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[: max(1, int(limit))]
