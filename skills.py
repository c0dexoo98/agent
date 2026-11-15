# -*- coding: utf-8 -*-
"""کتابخانهٔ مهارت‌ها (Skill Library) — ایجنت در طول کار «یاد می‌گیرد».

هر مهارت یک فایل Markdown با سرآیند ساده (key: value) در skills_learned/ است:

    ---
    name: close-stale-issues
    description: یافتن و بستن Issueهای کهنه بدون پاسخ با کامنت مؤدبانه
    triggers: issue, stale, close, کهنه
    created: 2026-11-15T10:00:00+00:00
    usage: 4
    success: 3
    ---
    ## گام‌ها
    1. list_issues با state=open
    2. ...

چرخهٔ یادگیری:
  * بعد از هر تسک موفق، موتور ایجنت از مدل یک «مهارت قابل‌استفادهٔ مجدد»
    می‌پرسد (پروتکل DISTILL) و با save_skill_from_model ذخیره می‌شود.
  * ابتدای هر تسک، مهارت‌های مرتبط (بر اساس تریگر کلیدواژه‌ای) به پرامپت
    تزریق می‌شوند تا ایجنت از تجربه‌های قبلی‌اش استفاده کند.
  * با هر استفاده، usage/success به‌روز می‌شود؛ مهارت‌های پرکاربرد اول تزریق
    می‌شوند — یعنی ایجنت روی تجربه، «مسئول‌تر» و دقیق‌تر می‌شود.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\u0600-\u06FF-]+", "-", (name or "").lower().strip()).strip("-")
    return (slug or "skill")[:48]


class SkillLibrary:
    """نگهداری، جستجو و آمار مهارت‌های آموخته‌شده."""

    def __init__(self, skills_dir: Optional[Path] = None) -> None:
        self.dir = Path(skills_dir or config.SKILLS_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # خواندن/نوشتن فایل مهارت
    # ------------------------------------------------------------------

    @staticmethod
    def _render(skill: Dict[str, Any]) -> str:
        meta = {
            "name": skill.get("name", ""),
            "description": skill.get("description", ""),
            "triggers": ", ".join(skill.get("triggers", [])),
            "created": skill.get("created") or _now_iso(),
            "usage": int(skill.get("usage", 0)),
            "success": int(skill.get("success", 0)),
            "version": int(skill.get("version", 1)),
        }
        if skill.get("uses"):
            meta["uses"] = ", ".join(skill["uses"])  # مهارت‌های ترکیب‌کننده
        fm = "\n".join(f"{k}: {v}" for k, v in meta.items())
        body = (skill.get("steps") or "").strip()
        return f"---\n{fm}\n---\n\n{body}\n"

    @staticmethod
    def _parse(path: Path) -> Optional[Dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.DOTALL)
        if not m:
            return None
        fm, body = m.group(1), m.group(2)
        meta: Dict[str, Any] = {}
        for line in fm.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        return {
            "file": path.name,
            "name": meta.get("name", path.stem),
            "description": meta.get("description", ""),
            "triggers": [t.strip() for t in meta.get("triggers", "").split(",") if t.strip()],
            "created": meta.get("created", ""),
            "usage": int(meta.get("usage", 0) or 0),
            "success": int(meta.get("success", 0) or 0),
            "version": int(meta.get("version", 1) or 1),
            "uses": [u.strip() for u in (meta.get("uses", "") or "").split(",") if u.strip()],
            "steps": body.strip(),
        }

    def _versions_path(self, slug: str) -> Path:
        return self.dir / f"{slug}.versions.json"

    def _load_versions(self, slug: str) -> Dict[str, Any]:
        p = self._versions_path(slug)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"version": 1, "history": []}

    def _save_versions(self, slug: str, data: Dict[str, Any]) -> None:
        self._versions_path(slug).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------
    # API عمومی
    # ------------------------------------------------------------------

    def save_skill(
        self,
        name: str,
        description: str,
        triggers: List[str],
        steps: str,
        uses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """ذخیره/به‌روزرسانی مهارت + نسخه‌بندی خودکار: تغییر بدنه → نسخهٔ جدید با تاریخچه."""
        name = (name or "").strip()
        description = (description or "").strip()
        steps = (steps or "").strip()
        if not name or not steps:
            return {"ok": False, "error": "name و steps الزامی‌اند"}
        triggers = [t.strip().lower() for t in (triggers or []) if t.strip()][:10]
        if not triggers:
            triggers = self._triggers_from_text(name + " " + description)[:10]
        clean_uses = [u.strip() for u in (uses or []) if u.strip()][:6]
        with self._lock:
            filename = _slugify(name) + ".md"
            path = self.dir / filename
            old = self._parse(path) if path.exists() else None
            version = int(old["version"]) if old else 1
            # نسخه‌بندی: اگر بدنه تغییر کرده، نسخهٔ قبلی آرشیو شود
            if old and old.get("steps") != steps:
                vdata = self._load_versions(_slugify(name))
                vdata.setdefault("history", []).append({
                    "v": version, "ts": _now_iso(),
                    "steps": old["steps"], "description": old.get("description", ""),
                })
                vdata["history"] = vdata["history"][-20:]
                version += 1
                vdata["version"] = version
                self._save_versions(_slugify(name), vdata)
            skill = {
                "name": name,
                "description": description,
                "triggers": triggers,
                "created": old["created"] if old else _now_iso(),
                "usage": old["usage"] if old else 0,
                "success": old["success"] if old else 0,
                "version": version,
                "uses": clean_uses,
                "steps": steps,
            }
            path.write_text(self._render(skill), encoding="utf-8")
        return {"ok": True, "file": filename, "version": version,
                "skill": {k: v for k, v in skill.items() if k != "steps"}}

    def save_skill_from_model(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """ذخیرهٔ مهارت استخراج‌شده از پاسخ JSON مدل (پروتکل DISTILL)."""
        return self.save_skill(
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            triggers=[str(t) for t in payload.get("triggers", [])],
            steps=str(payload.get("steps") or ""),
            uses=[str(u) for u in payload.get("uses", [])],
        )

    # ------------------------------------------------------------------
    # نسخه‌ها / Diff / حذف
    # ------------------------------------------------------------------

    def versions(self, name: str) -> Dict[str, Any]:
        slug = _slugify(name)
        s = self.get_skill(name, compose=False)
        if "error" in s:
            return s
        vdata = self._load_versions(slug)
        return {
            "ok": True,
            "current_version": int(s.get("version", 1)),
            "history": vdata.get("history", []),
            "current_steps": s.get("steps", ""),
        }

    def diff(self, name: str, v1: Optional[int] = None, v2: Optional[int] = None) -> Dict[str, Any]:
        """دیف یونی‌فایمد بین دو نسخه (پیش‌فرض: آخرین تاریخچه ← نسخهٔ فعلی)."""
        s = self.get_skill(name, compose=False)
        if "error" in s:
            return s
        vdata = self._load_versions(_slugify(name))
        history = vdata.get("history", [])
        # پیش‌فرض: آخرین نسخهٔ قبلی ← نسخهٔ فعلی
        if v1 is None and history:
            v1 = history[-1]["v"]
        if v2 is None:
            v2 = int(s.get("version", 1))
        def _steps_of(v: int) -> Optional[str]:
            if v == int(s.get("version", 1)):
                return s.get("steps", "")
            for h in history:
                if h["v"] == int(v):
                    return h["steps"]
            return None
        left = _steps_of(v1)
        right = _steps_of(v2)
        if left is None or right is None:
            return {"ok": False, "error": "نسخهٔ درخواستی یافت نشد", "available": [h["v"] for h in history]}
        import difflib
        diff_text = "\n".join(difflib.unified_diff(
            left.splitlines(), right.splitlines(),
            fromfile=f"v{v1}",
            tofile=f"v{v2}",
            lineterm="",
        ))
        return {"ok": True, "skill": name, "diff": diff_text or "(بدون تغییر)"}

    def delete_skill(self, name: str) -> Dict[str, Any]:
        slug = _slugify(name)
        p = self.dir / f"{slug}.md"
        if not p.exists():
            return {"ok": False, "error": f"مهارت '{name}' یافت نشد"}
        trash = self.dir / ".trash"
        trash.mkdir(exist_ok=True)
        import time as _t
        p.rename(trash / f"{int(_t.time())}_{p.name}")
        vp = self._versions_path(slug)
        if vp.exists():
            vp.rename(trash / f"{int(_t.time())}_{vp.name}")
        return {"ok": True, "moved_to_trash": p.name}

    # ------------------------------------------------------------------
    # Marketplace — export/import فایل مهارت قابل‌اشتراک
    # ------------------------------------------------------------------

    def export_skill(self, name: str) -> Dict[str, Any]:
        s = self.get_skill(name, compose=False)
        if "error" in s:
            return s
        out_dir = config.EXPORTS_DIR / "skills"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{_slugify(name)}-v{int(s.get('version', 1))}.skill.md"
        dest = out_dir / fname
        dest.write_text(self._render(s), encoding="utf-8")
        return {"ok": True, "file": str(dest), "name": name, "version": int(s.get("version", 1))}

    def import_skill(self, path: str) -> Dict[str, Any]:
        """ورود مهارت از فایل .skill.md (مسیر کامل یا نام فایل در exports/skills)."""
        src = Path(path)
        if not src.exists():
            alt = config.EXPORTS_DIR / "skills" / path
            if alt.exists():
                src = alt
            else:
                return {"ok": False, "error": f"فایل یافت نشد: {path}"}
        s = self._parse(src)
        if not s:
            return {"ok": False, "error": "فایل مهارت معتبر نیست (سرآیند --- لازم است)"}
        return self.save_skill(
            name=s["name"], description=s["description"], triggers=s["triggers"],
            steps=s["steps"], uses=s.get("uses"),
        )

    def list_skills(self) -> List[Dict[str, Any]]:
        with self._lock:
            skills = []
            for p in sorted(self.dir.glob("*.md")):
                s = self._parse(p)
                if s:
                    skills.append(s)
        skills.sort(key=lambda s: (s["usage"], s["success"]), reverse=True)
        return skills

    def get_skill(self, name: str, compose: bool = True, _seen: Optional[set] = None) -> Dict[str, Any]:
        """خواندن مهارت با ترکیب خودکار وابستگی‌ها (uses) — یک سطح، ضدحلقه."""
        _seen = _seen if _seen is not None else set()
        target = _slugify(name)
        if target in _seen:
            return {"ok": False, "error": f"حلقهٔ وابستگی در مهارت '{name}'"}
        _seen.add(target)
        with self._lock:
            p = self.dir / f"{target}.md"
            if p.exists():
                s = self._parse(p)
                if s:
                    if compose:
                        # ترکیب: بدنهٔ مهارت‌های referenced در انتها inline می‌شود
                        deps: List[Dict[str, str]] = []
                        for dep_name in (s.get("uses") or [])[:6]:
                            dep = self.get_skill(dep_name, _seen=_seen)
                            if dep.get("ok", True) and "error" not in dep:
                                deps.append({"name": dep["name"], "steps": dep["steps"][:1200]})
                        if deps:
                            s["composed_from"] = [d["name"] for d in deps]
                            s["steps"] = s["steps"] + "\n\n" + "\n\n".join(
                                f"[SUB-SKILL: {d['name']}]\n{d['steps']}" for d in deps
                            )
                    return s
        return {"ok": False, "error": f"مهارت '{name}' یافت نشد"}

    def mark_used(self, name: str, ok: bool = True) -> None:
        """ثبت استفاده از مهارت (در یادگیری بعدی وزن می‌گیرد)."""
        with self._lock:
            p = self.dir / f"{_slugify(name)}.md"
            s = self._parse(p) if p.exists() else None
            if not s:
                return
            s["usage"] = int(s.get("usage", 0)) + 1
            if ok:
                s["success"] = int(s.get("success", 0)) + 1
            p.write_text(self._render(s), encoding="utf-8")

    # ------------------------------------------------------------------
    # انتخاب مهارت‌های مرتبط برای تزریق در پرامپت
    # ------------------------------------------------------------------

    @staticmethod
    def _triggers_from_text(text: str) -> List[str]:
        stop = {"the", "and", "for", "with", "from", "this", "that", "when", "then", "این", "که", "با", "از", "برای", "روی"}
        words = re.findall(r"[\w\u0600-\u06FF]{3,}", (text or "").lower())
        seen: List[str] = []
        for w in words:
            if w not in stop and w not in seen:
                seen.append(w)
        return seen

    def relevant_for(self, task: str, limit: int = 3) -> List[Dict[str, Any]]:
        """مهارت‌های مرتبط با تسک: امتیاز = هم‌پوشانی تریگرها × وزنِ استفاده."""
        qtokens = set(re.findall(r"[\w\u0600-\u06FF]{3,}", (task or "").lower()))
        scored: List[Dict[str, Any]] = []
        for s in self.list_skills():
            trig = set(t.lower() for t in s["triggers"])
            overlap = len(qtokens & trig)
            if overlap == 0:
                # هم‌پوشانی با متن توضیح هم امتیازِ کمتری می‌دهد
                desc_tokens = set(self._triggers_from_text(s["description"]))
                overlap = 0.5 * len(qtokens & desc_tokens)
            if overlap <= 0:
                continue
            weight = 1.0 + min(s["usage"], 10) * 0.1 + min(s["success"], 10) * 0.05
            scored.append({"skill": s, "score": overlap * weight})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return [x["skill"] for x in scored[: max(1, int(limit))]]

    def build_context(self, task: str) -> str:
        """زمینهٔ متنی مهارت‌های مرتبط برای تزریق در پرامپت تسک."""
        skills = self.relevant_for(task, limit=config.SKILLS_INJECT_LIMIT)
        if not skills:
            return ""
        blocks = []
        for s in skills:
            body = s["steps"][:1500]
            blocks.append(
                f"### SKILL: {s['name']}\n{('desc: ' + s['description']) if s['description'] else ''}\n"
                f"(learned — used {s['usage']}x, success {s['success']}x)\n{body}"
            )
        return (
            "\n\n=== LEARNED SKILLS (procedures you yourself learned in previous tasks; "
            "prefer them over improvising) ===\n" + "\n\n".join(blocks)
        )

    def stats(self) -> Dict[str, Any]:
        skills = self.list_skills()
        return {
            "count": len(skills),
            "most_used": skills[0]["name"] if skills else None,
            "total_uses": sum(s["usage"] for s in skills),
        }


# ----------------------------------------------------------------------
# ابزارهای رجیستری
# ----------------------------------------------------------------------

def register_skill_tools(registry: Any, lib: SkillLibrary) -> None:
    """ثبت ابزارهای مهارت در ToolRegistry — مدل می‌تواند خودش مهارت ذخیره/بخواند."""

    R = registry.register_function

    R(
        name="save_skill",
        description=(
            "ذخیرهٔ یک مهارت قابل‌استفادهٔ مجدد در کتابخانهٔ مهارت‌ها؛ steps را به‌صورت "
            "گام‌های شماره‌دار و دقیق بنویس (نام ابزار + آرگومان‌ها) تا دفعات بعد عیناً اجرا شود. "
            "uses: نام مهارت‌های دیگر که این مهارت از آن‌ها ترکیب می‌شود (اختیاری)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "triggers": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "string"},
                "uses": {"type": "array", "items": {"type": "string"}, "description": "مهارت‌های سازنده (composition)"},
            },
            "required": ["name", "steps"],
        },
        func=lambda name, steps, description="", triggers=None, uses=None: lib.save_skill(
            name=name, description=description, triggers=triggers or [], steps=steps, uses=uses
        ),
    )
    R(
        name="list_skills",
        description="فهرست مهارت‌های آموخته‌شده با آمار استفاده",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda: {"ok": True, "skills": [
            {k: s.get(k) for k in ("name", "description", "triggers", "usage", "success", "version", "uses")}
            for s in lib.list_skills()
        ]},
    )
    R(
        name="get_skill",
        description="خواندن جزئیات کامل یک مهارت (گام‌ها + وابستگی‌های ترکیب‌شده) برای اجرای عیناً",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        func=lambda name: _get_skill_or_error(lib, name),
    )
    R(
        name="skill_versions",
        description="تاریخچهٔ نسخه‌های یک مهارت (هر تغییر بدنه = نسخهٔ جدید)",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        func=lambda name: lib.versions(name),
    )
    R(
        name="skill_diff",
        description="مقایسهٔ دو نسخهٔ مهارت با دیف یونی‌فایمد (بدون آرگومان = آخرین تغییر با نسخهٔ فعلی)",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "v1": {"type": "integer"},
                "v2": {"type": "integer"},
            },
            "required": ["name"],
        },
        func=lambda name, v1=None, v2=None: lib.diff(name, v1, v2),
    )
    R(
        name="export_skill",
        description="خروجی گرفتن مهارت به فایل .skill.md قابل‌اشتراک (Marketplace)",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        func=lambda name: lib.export_skill(name),
    )
    R(
        name="import_skill",
        description="ورود مهارت از فایل .skill.md (مسیر کامل یا نام در exports/skills)",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        func=lambda path: lib.import_skill(path),
    )
    R(
        name="delete_skill",
        description="حذف مهارت به Trash — خطرناک: نیازمند تایید انسانی",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        dangerous=True,
        func=lambda name: lib.delete_skill(name),
    )


def _get_skill_or_error(lib: SkillLibrary, name: str) -> Dict[str, Any]:
    s = lib.get_skill(name)
    if "error" in s:
        return s
    return {"ok": True, "skill": s}
