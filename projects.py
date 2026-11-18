# -*- coding: utf-8 -*-
"""مدیریت پروژه‌های DevX — هر چت/پروژه با آیدی و نام مستقل و حافظهٔ کاملاً ایزوله.

طراحی حرفه‌ای با دو لایهٔ شناسه:

  * ``id``     → آیدی نمایشی **فشرده** (۱، ۲، ۳، …). همیشه بدون فاصله: اگر پروژهٔ ۲
                 حذف شود، پروژهٔ ۳ خودکار به ۲ منتقل می‌شود («آیدی بعدی جایگزینِ
                 حذف‌شده می‌شود») و پروژه‌های بعدی شماره‌های بعدی را می‌گیرند.
  * ``key``    → کلید پایدار داخلی (uuid کوتاه) که **هرگز تغییر نمی‌کند**؛ دایرکتوری
                 حافظه و مهارت هر پروژه با کلید ساخته می‌شود، پس جابه‌جایی آیدی‌ها
                 هیچ‌وقت داده‌ها را جابه‌جا یا گم نمی‌کند.

ایزولاسیون کامل: هر پروژه ``memory/projects/<key>/memory.json`` و
``skills_learned/projects/<key>/*.md`` مخصوص خودش را دارد؛ اطلاعات هیچ دو پروژه‌ای
با هم قاطی نمی‌شود. سوییچ پروژه = سوییچ کامل حافظه + مهارت + آمار.

فایل مرکزی ``memory/projects.json``: رکوردها + ``active_key`` (پروژهٔ فعال بین
اجرها ماندگار است) + شمارندهٔ نام پیش‌فرض.
مهاجرت: اگر از نسخهٔ قبل ``memory/memory.json`` یا ``skills_learned/*.md`` مانده،
خودکار به یک پروژهٔ «مهاجرت‌شده» منتقل می‌شود تا چیزی از دست نرود.
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_key() -> str:
    return uuid.uuid4().hex[:10]


class ProjectManager:
    """ثبت/انتخاب/حذف/تغییرنام پروژه‌ها با آیدی فشرده و حافظهٔ ایزوله."""

    def __init__(
        self,
        memory_root: Optional[Path] = None,
        skills_root: Optional[Path] = None,
        legacy_memory_file: Optional[Path] = None,
        legacy_skills_dir: Optional[Path] = None,
    ) -> None:
        self.memory_root = Path(memory_root or config.MEMORY_DIR)          # memory/
        self.skills_root = Path(skills_root or config.SKILLS_DIR)          # skills_learned/
        self.legacy_memory_file = Path(legacy_memory_file or (self.memory_root / "memory.json"))
        self.legacy_skills_dir = Path(legacy_skills_dir or self.skills_root)
        self.file = self.memory_root / "projects.json"
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"projects": [], "active_key": None, "default_seq": 0, "id_seq": 0}
        self._load()
        self._migrate_legacy()

    # ------------------------------------------------------------------
    # خواندن/نوشتن اتمیک
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                projects = raw.get("projects", []) or []
                self._data = {
                    "projects": projects,
                    "active_key": raw.get("active_key"),
                    "default_seq": int(raw.get("default_seq", 0)),
                    # شمارندهٔ یکهٔ آیدی — مهاجرت فایل‌های قدیمی: حداقل بزرگ‌ترین آیدی موجود
                    "id_seq": max(
                        int(raw.get("id_seq", 0)),
                        max((int(p.get("id", 0)) for p in projects), default=0),
                    ),
                }
        except Exception as e:
            try:
                self.file.rename(self.file.with_suffix(".corrupt.bak"))
            except Exception:
                pass
            print(f"⚠️ projects.json خراب بود ({e}) — بکاپ و شروع از نو.")

    def _save(self) -> None:
        self.memory_root.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    # ------------------------------------------------------------------
    # مسیرهای ایزولهٔ هر پروژه
    # ------------------------------------------------------------------

    def memory_dir(self, key: str) -> Path:
        return self.memory_root / "projects" / key

    def skills_dir(self, key: str) -> Path:
        return self.skills_root / "projects" / key

    # ------------------------------------------------------------------
    # مهاجرت داده‌های نسخهٔ قبل
    # ------------------------------------------------------------------

    def _migrate_legacy(self) -> None:
        """حافظه/مهارت‌های قدیمیِ مشترک → یک پروژهٔ مستقل، تا چیزی گم نشود."""
        has_legacy_mem = self.legacy_memory_file.exists()
        has_legacy_skills = self.legacy_skills_dir.exists() and any(self.legacy_skills_dir.glob("*.md"))
        if not (has_legacy_mem or has_legacy_skills):
            return
        with self._lock:
            if any(p.get("key") == "legacy" for p in self._data["projects"]):
                return
            self._data["default_seq"] += 1
            # آیدی از id_seq می‌آید و آن را جلو می‌بریم — قبلاً «len+1» بود که
            # با id_seq هم‌گام نشد → آیدی تکراری با اولین create() بعد از مهاجرت
            # و حذف/تغییرنامِ پروژهٔ اشتباه
            self._data["id_seq"] = max(self._data["id_seq"], len(self._data["projects"])) + 1
            rec = {
                "key": "legacy",
                "id": self._data["id_seq"],
                "name": f"پروژه {self._data['default_seq']} (مهاجرت‌شده)",
                "created": _now_iso(),
                "last_used": _now_iso(),
                "task_count": 0,
            }
            self._data["projects"].append(rec)
            mdir = self.memory_dir("legacy")
            sdir = self.skills_dir("legacy")
            if has_legacy_mem:
                mdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.legacy_memory_file, mdir / "memory.json")
            if has_legacy_skills:
                sdir.mkdir(parents=True, exist_ok=True)
                for md_file in self.legacy_skills_dir.glob("*.md"):
                    shutil.copy2(md_file, sdir / md_file.name)
            if not self._data["active_key"]:
                self._data["active_key"] = "legacy"
            self._save()
            print(f"📦 داده‌های قبلی به «{rec['name']}» مهاجرت داده شد (آیدی {rec['id']}).")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, name: Optional[str] = None) -> Dict[str, Any]:
        """پروژهٔ جدید؛ اگر نام داده نشود «پروژه N» (N ادامهٔ شمارندهٔ همیشگی)."""
        name = (name or "").strip()
        with self._lock:
            self._data["default_seq"] += 1
            if not name:
                name = f"پروژه {self._data['default_seq']}"
            if len(name) > 60:
                name = name[:60]
            # آیدی یکه و پایدار: هرگز بازیافت یا جابه‌جا نمی‌شود — ارجاع‌های
            # jobs/watches/notifications به پروژهٔ اشتباه نیفتند
            self._data["id_seq"] += 1
            rec = {
                "key": _new_key(),
                "id": self._data["id_seq"],
                "name": name,
                "created": _now_iso(),
                "last_used": _now_iso(),
                "task_count": 0,
                "persona": "",                 # شخصیت/دستورالعمل اختصاصی پروژه
                "settings": {"allow_dangerous": False, "tool_groups": []},  # [] = همهٔ گروه‌ها
            }
            self._data["projects"].append(rec)
            self._data["active_key"] = rec["key"]  # ساخت = انتخاب خودکار
            self._save()
        return rec

    def save(self) -> None:
        """ذخیرهٔ صریح فایل پروژه‌ها (برای import/persona از بیرون)."""
        with self._lock:
            self._save()

    def set_persona(self, project_id: int, persona: str) -> Dict[str, Any]:
        """شخصیت/دستورالعمل همیشگی پروژه — در پرامپت سیستمی همهٔ تسک‌ها تزریق می‌شود."""
        with self._lock:
            rec = self.get(project_id)
            if "error" in rec:
                return rec
            rec["persona"] = (persona or "").strip()[:2000]
            rec["last_used"] = _now_iso()
            self._save()
        return {"ok": True, "id": rec["id"], "persona": rec["persona"]}

    def set_settings(self, project_id: int, settings: Dict[str, Any]) -> Dict[str, Any]:
        """تنظیمات پروژه: allow_dangerous (عبور از تایید؟) و tool_groups (گروه‌های مجاز)."""
        with self._lock:
            rec = self.get(project_id)
            if "error" in rec:
                return rec
            cur = rec.get("settings") or {}
            if "allow_dangerous" in settings:
                cur["allow_dangerous"] = bool(settings["allow_dangerous"])
            if "tool_groups" in settings:
                cur["tool_groups"] = [str(g) for g in (settings["tool_groups"] or []) if str(g).strip()]
            rec["settings"] = cur
            self._save()
        return {"ok": True, "settings": rec["settings"]}

    def delete(self, project_id: int) -> Dict[str, Any]:
        """حذف پروژه — آیدی پروژه‌های دیگر پایدار می‌ماند (بدون بازچینش).

        دایرکتوری‌های داده با کلید پایدار ساخته شده‌اند؛ دادهٔ پروژه‌های دیگر
        دست‌نخورده می‌ماند و فقط دادهٔ همان پروژه پاک می‌شود. آیدی‌ها دیگر
        بازچینش نمی‌شوند تا ارجاع‌های jobs/watches به پروژهٔ اشتباه نیفتند
        (باگ قدیمی: حذف یک پروژه، زمان‌بند/نگهبان پروژهٔ دیگر را می‌ربود).
        """
        with self._lock:
            victim = self.get(project_id)
            if "error" in victim:
                return victim
            self._data["projects"] = [p for p in self._data["projects"] if p["key"] != victim["key"]]
            if self._data["active_key"] == victim["key"]:
                self._data["active_key"] = self._data["projects"][-1]["key"] if self._data["projects"] else None
            self._save()
            # پاک‌سازی دادهٔ همان پروژه (حافظه + مهارت)
            for d in (self.memory_dir(victim["key"]), self.skills_dir(victim["key"])):
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            return {"ok": True, "deleted": victim}

    def rename(self, project_id: int, name: str) -> Dict[str, Any]:
        name = (name or "").strip()[:60]
        if not name:
            return {"ok": False, "error": "نام خالی است"}
        with self._lock:
            rec = self.get(project_id)
            if "error" in rec:
                return rec
            old = rec["name"]
            rec["name"] = name
            rec["last_used"] = _now_iso()
            self._save()
        return {"ok": True, "id": rec["id"], "old": old, "new": name}

    def get(self, project_id: int) -> Dict[str, Any]:
        with self._lock:
            for p in self._data["projects"]:
                if p["id"] == int(project_id):
                    return p
        return {"error": f"پروژه‌ای با آیدی {project_id} نیست"}

    def get_by_key(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        with self._lock:
            for p in self._data["projects"]:
                if p["key"] == key:
                    return p
        return None

    def select(self, project_id: int) -> Dict[str, Any]:
        rec = self.get(project_id)
        if "error" in rec:
            return rec
        with self._lock:
            self._data["active_key"] = rec["key"]
            rec["last_used"] = _now_iso()
            self._save()
        return {"ok": True, "project": rec}

    def record_task(self, key: str) -> None:
        with self._lock:
            p = self.get_by_key(key)
            if p:
                p["task_count"] = int(p.get("task_count", 0)) + 1
                p["last_used"] = _now_iso()
                self._save()

    # ------------------------------------------------------------------
    # خوانش‌ها
    # ------------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._data["projects"]]

    @property
    def active_key(self) -> Optional[str]:
        return self._data["active_key"]

    def active(self) -> Optional[Dict[str, Any]]:
        # قفلِ همان بقیهٔ نویسنده‌ها: این متد هم می‌نویسد (_save) — بدون قفل،
        # ذخیرهٔ همزمان روی projects.json.tmp یکسان → FileNotFoundError/گم‌شدن active_key
        with self._lock:
            p = self.get_by_key(self._data["active_key"])
            if p is None and self._data["projects"]:
                # فعالِ گم‌شده → آخرین پروژه
                self._data["active_key"] = self._data["projects"][-1]["key"]
                p = self.get_by_key(self._data["active_key"])
                self._save()
            return p

    def next_default_name(self) -> str:
        with self._lock:
            return f"پروژه {self._data['default_seq'] + 1}"

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            projects = self.list()
        return {
            "count": len(projects),
            "active": (self.active() or {}).get("name"),
            "total_tasks": sum(int(p.get("task_count", 0)) for p in projects),
        }

    # ------------------------------------------------------------------
    # ابزار رجیستری — مدل بداند در کدام پروژه است
    # ------------------------------------------------------------------

    def register_project_tools(self, registry: Any) -> None:
        """ابزار get_project_info: شناسنامهٔ پروژهٔ فعال (آیدی/نام/آمار)."""
        mgr = self

        def _info() -> Dict[str, Any]:
            p = mgr.active()
            if not p:
                return {"ok": False, "error": "هیچ پروژه‌ای فعال نیست"}
            return {
                "ok": True,
                "project": {
                    "id": p["id"],
                    "name": p["name"],
                    "created": p["created"],
                    "task_count": p.get("task_count", 0),
                    "memory_isolated": True,
                },
            }

        registry.register_function(
            name="get_project_info",
            description="شناسنامهٔ پروژهٔ فعال (آیدی، نام، تعداد تسک‌ها) — حافظهٔ هر پروژه ایزوله است",
            parameters={"type": "object", "properties": {}, "required": []},
            func=_info,
        )
