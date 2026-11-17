# -*- coding: utf-8 -*-
"""تست runtime کلاینت تعاملی CLI (agent.run_cli) — پاس ۵.

run_cli تا این پاس هرگز در runtime اجرا نشده بود (پوشش صفر). این تست با
FakeBrain + stdin اسکریپت‌شده کل حلقهٔ تعاملی را می‌گرداند: انتخاب پروژه،
اجرای تسک، همهٔ دستورهای منو، سوییچ/حذف پروژه، و مسیر بازگشتی (پروژهٔ
موجود). نکتهٔ ظریف: run_cli از sys.argv حالت one-shot می‌سازد — argv باید
پچ شود وگرنه نام فایل تست به‌عنوان تسک اجرا می‌شود.

اجرا:  python tests/test_cli_runtime.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import agent  # noqa: E402
import config  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402


class _FakeClient:
    """جایگزین DeepSeekClient — همان رابطی که run_cli صدا می‌زند."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.new_chats = 0

    def start(self) -> None:
        self.started = True

    def ensure_logged_in(self) -> None:
        return None

    def new_chat(self) -> None:
        self.new_chats += 1

    def close(self) -> None:
        self.closed = True


_CFG_KEYS = (
    "BASE_DIR", "MEMORY_DIR", "SKILLS_DIR", "AUDIT_DIR", "SNAPSHOTS_DIR",
    "CHECKPOINTS_DIR", "WORKSPACE_DIR", "EXPORTS_DIR",
    "DEEPSEEK_USER_DATA_DIR", "SCHEDULER_ENABLED", "WATCHER_ENABLED",
)


def _run_cli(script: str, tmp: Path, responses: Optional[List[str]] = None
             ) -> Tuple[str, FakeBrain, _FakeClient, Dict[str, Any]]:
    """اجراهای run_cli با زیرساخت ایزوله در tmp — خروجی: (stdout, مغز، کلاینت، اطلاعات)."""
    brain = FakeBrain(list(responses or []))
    client = _FakeClient()
    saved_cfg = {k: getattr(config, k) for k in _CFG_KEYS}
    saved = {
        "argv": sys.argv,
        "stdin": sys.stdin,
        "gh": agent.obtain_github_tools,
        "ds_cls": agent.DeepSeekClient,
        "ds_brain": agent.DeepSeekBrain,
    }
    try:
        for k, v in {
            "BASE_DIR": tmp, "MEMORY_DIR": tmp / "memory", "SKILLS_DIR": tmp / "skills_learned",
            "AUDIT_DIR": tmp / "audit", "SNAPSHOTS_DIR": tmp / "snapshots",
            "CHECKPOINTS_DIR": tmp / "checkpoints", "WORKSPACE_DIR": tmp / "workspace",
            "EXPORTS_DIR": tmp / "exports",
            "DEEPSEEK_USER_DATA_DIR": tmp / "prof",
            "SCHEDULER_ENABLED": False, "WATCHER_ENABLED": False,
        }.items():
            setattr(config, k, v)
        sys.argv = ["agent.py"]  # وگرنه حالت one-shot فعال می‌شود
        sys.stdin = io.StringIO(script)
        agent.obtain_github_tools = lambda *a, **k: None      # بدون توکن — حالت بدون گیت‌هاب
        agent.DeepSeekClient = lambda **kw: client
        agent.DeepSeekBrain = lambda c: brain
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            agent.run_cli()
    finally:
        sys.argv, sys.stdin = saved["argv"], saved["stdin"]
        agent.obtain_github_tools = saved["gh"]
        agent.DeepSeekClient = saved["ds_cls"]
        agent.DeepSeekBrain = saved["ds_brain"]
        for k, v in saved_cfg.items():
            setattr(config, k, v)
    return out.getvalue(), brain, client, {"tmp": tmp}


def test_cli_fresh_flow(tmp: Path) -> None:
    """اجرای کامل: پروژهٔ جدید → تسک → دستورها → پروژهٔ دوم → حذف → خروج."""
    script = "\n".join([
        "پروژه-CLI",                      # نام پروژهٔ جدید (cli_pick_project)
        "",                               # تسک خالی → continue
        "/tools",
        "یک تسک آزمایشی از CLI",          # تسک → _run (FakeBrain پاسخ نهایی می‌دهد)
        "/memory",
        "/skills",
        "/stats",
        "/sessions",
        "/usage",
        "/audit",
        "/search هدف",
        "/schedule",
        "تسک زمان‌بندی تستی",              # ⏰ تسک خودکار
        "interval",                       # kind
        "120",                            # هر چند ثانیه
        "/watch",
        "octocat/hello-world",            # owner/repo
        "",                               # تسک خودکار (خالی=فقط اعلان)
        "/snapshot",
        "برچسب-تست",                      # برچسب
        "/persona",
        "تست-پرسونا",                     # persona جدید
        "/newsession",
        "سشن دوم",
        "/newproject پروژه-دوم",
        "/projects",
        "/project 1",
        "/rmproject 2",
        "y",                              # تایید حذف
        "/exit",
    ]) + "\n"
    out, brain, client, info = _run_cli(script, tmp, [
        json.dumps({"final_answer": "پاسخ نهایی آزمایشی ✅"}, ensure_ascii=False),
    ])
    # شروع و پایان درست
    assert "پروژهٔ فعال" in out and "خداحافظ!" in out, out[:400]
    assert "ابزار فعال" in out, "لیست ابزار باید چاپ شود"
    # تسک از طریق FakeBrain اجرا و پاسخ نهایی چاپ شد
    assert "پاسخ نهایی آزمایشی ✅" in out and "پاسخ نهایی (1 دور" in out, out[-600:]
    assert brain.sent, "پرامپت باید به مغز فرستاده شود"
    # دستورها جواب دادند
    assert "حافظه:" in out and "[1] پروژه-CLI" in out, out[:800]
    assert "سشن دوم" in out, "/newsession"
    assert "پروژهٔ «پروژه-دوم»" in out and "سوییچ شد به [1] پروژه-CLI" in out, "ساخت/سوییچ پروژه"
    assert "حذف شد" in out, "/rmproject"
    assert "برچسب-تست" in out or "snapshot" in out.lower(), "/snapshot"
    assert "تست-پرسونا" in out or "persona" in out.lower(), "/persona"
    # زیرساخت: یک پروژه ماند؛ job ثبت شد؛ watch ثبت شد
    pj = json.loads((tmp / "memory" / "projects.json").read_text(encoding="utf-8"))
    assert len(pj["projects"]) == 1 and pj["projects"][0]["name"] == "پروژه-CLI", pj
    jobs = json.loads((tmp / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert len(jobs) == 1 and jobs[0]["every_s"] == 120, jobs
    watches = json.loads((tmp / "watches.json").read_text(encoding="utf-8"))["watches"]
    assert len(watches) == 1 and watches[0]["repo"] == "octocat/hello-world", watches
    assert client.closed, "کلاینت دیپ‌سیک باید در finally بسته شود"
    print("ok: CLI fresh flow — پروژه/تسک/۱۵ دستور/حذف/خروج همه سالم")


def test_cli_reopen_existing(tmp: Path) -> None:
    """اجرای دوم با پروژهٔ موجود: Enter → پروژهٔ پیش‌فرض؛ بدون پرسش نام."""
    script = "\n/projects\n/exit\n"
    out, _, client, _ = _run_cli(script, tmp)  # Enter → default id
    assert "پروژهٔ فعال: [1] پروژه-CLI" in out, out[:400]
    assert "[1] پروژه-CLI" in out and "پروژه-دوم" not in out
    assert client.closed
    print("ok: CLI reopen — انتخاب پروژهٔ موجود با پیش‌فرض")


def test_cli_project_switch_and_memory_isolation(tmp: Path) -> None:
    """حافظهٔ پروژه‌های مختلف ایزوله است: فکت ثبت‌شده در پروژهٔ ۱ به ۲ نشت نمی‌کند."""
    # دو فکت در دو پروژهٔ متفاوت از طریق MemoryStore مستقیم می‌نویسیم
    from memory import MemoryStore
    from projects import ProjectManager
    pm = ProjectManager(memory_root=tmp / "memory", skills_root=tmp / "sk")
    MemoryStore(memory_dir=pm.memory_dir(pm.list()[0]["key"])).add_fact("فکت پروژه یک")
    # پروژهٔ دوم در تست قبلی حذف شد؛ یکی دیگر می‌سازیم و فکت جدا می‌گذاریم
    rec2 = pm.create("پروژه-سوم")
    MemoryStore(memory_dir=pm.memory_dir(rec2["key"])).add_fact("فکت پروژه سه")
    # خط اول: Enter → پروژهٔ پیش‌فرض (run_cli همیشه ابتدا پروژه می‌پرسد)
    script = "\n".join(["", "/project 3", "/memory", "/project 1", "/memory", "/exit"]) + "\n"
    out, _, _, _ = _run_cli(script, tmp)
    # شمارش هر پروژه فقط فکت خودش (بدون نشت) — با شمارش واقعی حافظه
    from projects import ProjectManager as PM2
    pm2 = PM2(memory_root=tmp / "memory", skills_root=tmp / "sk")
    counts = {p["name"]: MemoryStore(memory_dir=pm2.memory_dir(p["key"])).counts()["facts"] for p in pm2.list()}
    assert counts.get("پروژه-CLI") == 1 and counts.get("پروژه-سوم") == 1, counts
    assert "سوییچ شد به [3] پروژه-سوم" in out and "سوییچ شد به [1] پروژه-CLI" in out, out[:600]
    print("ok: CLI project switch — حافظهٔ پروژه‌ها ایزوله (بدون نشت فکت)")


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="devx_cli_"))
    try:
        test_cli_fresh_flow(tmp)
        test_cli_reopen_existing(tmp)
        test_cli_project_switch_and_memory_isolation(tmp)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print("\nALL CLI RUNTIME TESTS PASSED ✅")
