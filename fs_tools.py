# -*- coding: utf-8 -*-
"""ابزارهای فایل‌سیستم پروژه‌محور (FS Tools) — زندانِ مسیر (Path Jail).

همهٔ مسیرها باید داخل workspace پروژهٔ فعال باشند؛ هر تلاش خروج (../، مسیر مطلق
سیستمی، symlink-تراوِرس) رد می‌شود. حذف‌ها علامت خطرناک دارند و به Trash پوشه
منتقل می‌شوند (قابل بازیابی) — و در حالت پیش‌فرض از دروازهٔ تایید انسانی می‌گذرند.
"""
from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path
from typing import Any, Dict, List

import config

IGNORED_NAMES = {".git", "__pycache__", "node_modules", ".venv", ".trash"}


def register_fs_tools(registry: Any, workspace: Path) -> None:
    """ثبت ابزارهای fs_* با محدودیت مطلق به پوشهٔ workspace پروژه."""
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    trash = root / ".trash"
    R = registry.register_function

    def _jail(p: str) -> Path:
        cand = (root / (p or ".")).resolve()
        if cand != root and root not in cand.parents:
            raise PermissionError(f"مسیر خارج از workspace پروژه است: {p}")
        return cand

    def _meta(p: Path) -> Dict[str, Any]:
        return {
            "name": p.name or str(p),
            "type": "dir" if p.is_dir() else "file",
            "size": p.stat().st_size if p.is_file() else None,
            "path": str(p.relative_to(root)),
        }

    R(
        name="fs_list",
        description="فهرست فایل/پوشه‌های workspace پروژه (مسیر نسبی؛ خالی = ریشه)",
        parameters={"type": "object", "properties": {"path": {"type": "string", "default": ""}}, "required": []},
        func=lambda path="": _fs_list(_jail(path), root),
    )
    R(
        name="fs_read",
        description="خواندن متن یک فایل از workspace (کوتاه‌سازی خودکار)",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        func=lambda path: _fs_read(_jail(path), int(config.TOOL_RESULT_MAX_CHARS)),
    )
    R(
        name="fs_write",
        description="نوشتن/ساخت فایل متنی در workspace (append برای الحاق)",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean", "default": False}},
            "required": ["path", "content"],
        },
        func=lambda path, content, append=False: _fs_write(_jail(path), content, bool(append)),
    )
    R(
        name="fs_delete",
        description="انتقال فایل/پوشه به .trash (قابل بازیابی) — خطرناک: نیازمند تایید انسانی",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        dangerous=True,
        func=lambda path: _fs_delete(_jail(path), trash),
    )
    R(
        name="fs_search",
        description="جستجوی نام فایل با الگوی glob (مثل *.py) در کل workspace",
        parameters={"type": "object", "properties": {"pattern": {"type": "string", "default": "*"}}, "required": []},
        func=lambda pattern="*": _fs_search(root, str(pattern), IGNORED_NAMES),
    )


def _fs_list(target: Path, root: Path) -> Dict[str, Any]:
    if not target.exists():
        return {"ok": False, "error": "مسیر وجود ندارد"}
    if target.is_file():
        return {"ok": True, "items": [_meta(target)]}
    items = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in IGNORED_NAMES:
            continue
        try:
            items.append(_meta(child))
        except OSError:
            continue
    return {"ok": True, "root": str(target.relative_to(root)) or ".", "items": items}


def _fs_read(path: Path, limit: int) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "فایل یافت نشد"}
    text = path.read_text(encoding="utf-8", errors="replace")
    short = text[:limit]
    if len(text) > limit:
        short += f"\n... [کوتاه‌شد از {len(text)} کاراکتر]"
    return {"ok": True, "path": path.name, "size": len(text), "content": short}


def _fs_write(path: Path, content: str, append: bool) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as f:
        f.write(content or "")
    return {"ok": True, "path": str(path), "size": path.stat().st_size, "mode": "append" if append else "write"}


def _fs_delete(path: Path, trash: Path) -> Dict[str, Any]:
    if path == trash.parent:
        return {"ok": False, "error": "حذف ریشهٔ workspace مجاز نیست"}
    if path == path.resolve().parent:
        return {"ok": False, "error": "حذف ریشه مجاز نیست"}
    if not path.exists():
        return {"ok": False, "error": "مسیر وجود ندارد"}
    trash.mkdir(parents=True, exist_ok=True)
    import time
    dest = trash / f"{int(time.time())}_{path.name}"
    shutil.move(str(path), str(dest))
    return {"ok": True, "moved_to_trash": str(dest)}


def _fs_search(root: Path, pattern: str, ignored: set) -> Dict[str, Any]:
    hits: List[str] = []
    for p in root.rglob("*"):
        if any(part in ignored for part in p.parts):
            continue
        if not p.is_file():
            continue
        # تطبیق هم با نام فایل و هم با مسیر نسبی (posix) — «src/*.py» هم کار کند
        rel = p.relative_to(root).as_posix()
        if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(rel, pattern):
            hits.append(rel)
            if len(hits) >= 200:
                break
    return {"ok": True, "count": len(hits), "files": hits}
