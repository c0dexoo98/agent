# -*- coding: utf-8 -*-
"""بستهٔ قابل‌حمل پروژه (Export / Import) — پروژه با کل حافظه/مهارت/سشن‌هایش.

export: ZIP با meta.json (نام/آمار/نسخهٔ فرمت) + کل دایرکتوری‌های memory و
skills آن پروژه — فایل در exports/ ذخیره می‌شود و از UI قابل دانلود است.
import: ساخت پروژهٔ جدید با همان نام و استخراج داده‌ها در دایرکتوری‌های key
جدید — ایزولاسیون حفظ می‌شود و آیدی خودکار چینش می‌شود. هر export می‌تواند به‌عنوان
قالب (Template) شروع یک پروژهٔ تازه هم استفاده شود.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import config

FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def export_project(
    rec: Dict[str, Any],
    memory_dir: Path,
    skills_dir: Path,
    exports_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    exports_dir = Path(exports_dir or (config.BASE_DIR / "exports"))
    exports_dir.mkdir(parents=True, exist_ok=True)
    name = f"project-{rec['id']}-{_now()}.zip"
    dest = exports_dir / name
    sessions_file = Path(memory_dir) / "sessions.json"
    meta = {
        "format": FORMAT_VERSION,
        "exported_at": _now(),
        "project": {k: rec.get(k) for k in ("id", "name", "created", "last_used", "task_count", "persona")},
        "sessions": sessions_file.read_text(encoding="utf-8") if sessions_file.exists() else None,
    }
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("_meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        for base, src in (("memory", Path(memory_dir)), ("skills", Path(skills_dir))):
            if not Path(src).exists():
                continue
            for p in sorted(Path(src).rglob("*")):
                if p.is_file() and p.name not in ("sessions.json", "projects.json"):
                    z.write(p, arcname=f"{base}/{p.relative_to(src)}")
                    count += 1
    return {"ok": True, "file": str(dest), "name": name, "files": count, "meta": meta}


def import_project(zip_path: Path, project_mgr: Any, memory_root: Path, skills_root: Path) -> Dict[str, Any]:
    """ساخت پروژهٔ جدید از ZIP (project_mgr: نمونهٔ ProjectManager) و برگرداندن رکورد."""
    src = Path(zip_path)
    if not src.exists():
        return {"ok": False, "error": "فایل بسته یافت نشد"}
    try:
        with zipfile.ZipFile(src) as z:
            meta = json.loads(z.read("_meta.json").decode("utf-8"))
            if int(meta.get("format", 0)) > FORMAT_VERSION:
                return {"ok": False, "error": "نسخهٔ فرمت بسته جدیدتر از این برنامه است"}
            old = meta.get("project") or {}
            name = old.get("name") or "پروژه واردشده"
            rec = project_mgr.create(name)
            if old.get("persona"):
                project_mgr.set_persona(rec["id"], str(old["persona"]))
            mdir = memory_root / "projects" / rec["key"]
            sdir = skills_root / "projects" / rec["key"]
            restored = 0
            for n in z.namelist():
                if n == "_meta.json":
                    continue
                if n.startswith("memory/"):
                    dest = mdir / n[len("memory/"):]
                elif n.startswith("skills/"):
                    dest = sdir / n[len("skills/"):]
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as fsrc, open(dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                restored += 1
            if meta.get("sessions"):
                mdir.mkdir(parents=True, exist_ok=True)
                (mdir / "sessions.json").write_text(meta["sessions"], encoding="utf-8")
            tc = int(old.get("task_count", 0) or 0)
            if tc and rec.get("key"):
                project_mgr.record_task(rec["key"]) if False else None  # شمارش تسک از رکورد اصلی حفظ شود
                rec["task_count"] = tc
                project_mgr.save()
        return {"ok": True, "project": rec, "restored": restored, "source": old}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "فایل ZIP معتبر نیست"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"import ناموفق: {e}"}


def list_exports(exports_dir: Optional[Path] = None) -> list:
    d = Path(exports_dir or (config.BASE_DIR / "exports"))
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(d.glob("project-*.zip"), reverse=True):
        meta = {}
        try:
            with zipfile.ZipFile(p) as z:
                meta = json.loads(z.read("_meta.json").decode("utf-8"))
        except Exception:
            pass
        out.append({"file": str(p), "name": p.name, "size": p.stat().st_size,
                    "project_name": (meta.get("project") or {}).get("name"), "exported_at": meta.get("exported_at")})
    return out
