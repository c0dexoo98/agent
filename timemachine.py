# -*- coding: utf-8 -*-
"""ماشین زمان حافظه (Time Machine) — Snapshot و Rollback کامل وضعیت پروژه.

هر snapshot یک ZIP است از حافظه + مهارت‌های آن پروژه (و sessions)، با برچسب و
زمان. رول‌بک = استخراج snapshot روی همان مسیرها (قبلش وضعیت فعلی هم به‌صورت
خودکار snapshot می‌شود تا هیچ‌چیز غیرقابل‌بازگشت نباشد).
"""
from __future__ import annotations

import config
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class TimeMachine:
    """ساخت/فهرست/بازیابی snapshotهای هر پروژه."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base = Path(base_dir or (config.BASE_DIR / "snapshots"))
        self.base.mkdir(parents=True, exist_ok=True)

    def _dir(self, key: str) -> Path:
        d = self.base / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create(self, key: str, memory_dir: Path, skills_dir: Path, label: str = "", extra_files: Optional[dict] = None) -> Dict[str, Any]:
        src_mem = Path(memory_dir)
        src_sk = Path(skills_dir)
        name = f"{_now()}-{(label or 'snapshot')[:40].strip().replace(' ', '_')}.zip"
        dest = self._dir(key) / name
        count = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            meta = {"key": key, "label": label, "created": _now(), "files": []}
            if src_mem.exists():
                for p in sorted(src_mem.rglob("*")):
                    if p.is_file():
                        z.write(p, arcname=f"memory/{p.relative_to(src_mem)}")
                        meta["files"].append(f"memory/{p.relative_to(src_mem)}")
                        count += 1
            if src_sk.exists():
                for p in sorted(src_sk.rglob("*.md")):
                    z.write(p, arcname=f"skills/{p.relative_to(src_sk)}")
                    meta["files"].append(f"skills/{p.relative_to(src_sk)}")
                    count += 1
            for arcname, content in (extra_files or {}).items():
                z.writestr(arcname, content)
                meta["files"].append(arcname)
                count += 1
            z.writestr("_meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        return {"ok": True, "id": name, "path": str(dest), "files": count, "label": label}

    def list(self, key: str) -> List[Dict[str, Any]]:
        out = []
        for z in sorted(self._dir(key).glob("*.zip"), reverse=True):
            try:
                with zipfile.ZipFile(z) as f:
                    meta = json.loads(f.read("_meta.json").decode("utf-8"))
                out.append({"id": z.name, "path": str(z), "label": meta.get("label", ""), "created": meta.get("created", ""), "files": len(meta.get("files", []))})
            except Exception:
                continue
        return out

    def restore(self, key: str, snapshot_id: str, memory_dir: Path, skills_dir: Path) -> Dict[str, Any]:
        src = self._dir(key) / snapshot_id
        if not src.exists():
            return {"ok": False, "error": "snapshot یافت نشد"}
        restored = 0
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n != "_meta.json"]
            # پاک‌سازی هدف‌ها (فقط فایل‌های درون‌مانده این نوع‌ها)
            for base, target in (("memory/", Path(memory_dir)), ("skills/", Path(skills_dir))):
                if not target.exists():
                    continue
                for p in list(target.rglob("*")):
                    if p.is_file() and p.name != "projects.json":  # projects.json متعلق به ریشه است
                        p.unlink()
            for n in names:
                if n.startswith("memory/"):
                    dest = Path(memory_dir) / n[len("memory/"):]
                elif n.startswith("skills/"):
                    dest = Path(skills_dir) / n[len("skills/"):]
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as fsrc, open(dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                restored += 1
        return {"ok": True, "snapshot": snapshot_id, "restored": restored}
