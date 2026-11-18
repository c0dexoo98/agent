# -*- coding: utf-8 -*-
"""سندباکس اجرای کد (Code Sandbox) — اجرای Python/Shell در subprocess ایزوله.

محافظت‌ها:
  * پروسهٔ جداگانه با timeout سخت (kill کل گروه پروسه)
  * محدودیت خروجی (stdout/stderr) و کوتاه‌سازی
  * env سفید‌لیستی‌شده (بدون توکن‌ها/کلیدها) + cwd محدود به workspace پروژه
  * بدون ورودی استاندارد (stdin=DEVNULL) تا فرایند هنگ نکند
  * علامت‌گذاری `dangerous=True` → عبور از دروازهٔ تایید انسانی (Approval)
کاربرد: تست اسنیپت، محاسبه، پردازش متن/JSON — اجرا در پروژهٔ فعال.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

# فقط این متغیرهای محیطی به پروسهٔ فرزند تزریق می‌شود
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "PYTHONUNBUFFERED")


def run_in_sandbox(
    argv: List[str],
    workdir: Optional[Path] = None,
    timeout: Optional[int] = None,
    max_output: Optional[int] = None,
) -> Dict[str, Any]:
    """اجرای امن یک دستور؛ خروجی ساختاریافته برای مدل: {ok, exit, stdout, stderr, duration_s}

    تایم‌اوت سخت: کل گروه پروسه کشته می‌شود (نه فقط فرزند) تا فرزندهای سرگردان
    (چیلدهای اسکریپت) پاکسازی شوند — با start_new_session و killpg.
    """
    timeout = int(timeout or config.SANDBOX_TIMEOUT)
    # سقف مطلق: مدل هر عددی بدهد، بالاتر از SANDBOX_MAX_TIMEOUT صبر نمی‌کنیم —
    # وگرنه یک درخواستِ «timeout: 3600» موتور (و سراسر سرور) را قفل می‌کرد
    timeout = max(1, min(timeout, config.SANDBOX_MAX_TIMEOUT))
    max_output = int(max_output or config.SANDBOX_MAX_OUTPUT)
    if not argv or not isinstance(argv, list):
        return {"ok": False, "error": "argv خالی است"}
    workdir = Path(workdir or config.BASE_DIR)
    workdir.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env.setdefault("PYTHONUNBUFFERED", "1")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv لیستی است، شل درگیر نیست
            argv,
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            text=True,
            start_new_session=True,   # گروه پروسهٔ جدا → kill کل درخت
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            # communicate بدون timeout هرگز برنمی‌گشت اگر فرزندی write-end پایپ
            # stdout را ارث برده باشد (start_new_session در کد کاربر) → ددلاک
            # همیشگی موتور. حالا حداکثر ۵ ثانیه صبر و بعد پایپ‌ها را می‌بندیم.
            out, err = "", ""
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # زامبی هم‌راسته با پایپِ بسته دیگر موتور را نگه نمی‌دارد
            return {"ok": False, "error": f"تایم‌اوت {timeout}s — کل گروه پروسه کشته شد",
                    "timeout": timeout,
                    "stdout": (out or "")[:max_output], "stderr": (err or "")[:max_output]}
        out = out or ""
        err = err or ""
        out_full = out
        if len(out) > max_output:
            out = out[:max_output] + f"\n... [کوتاه‌شد از {len(out_full)} کاراکتر]"
        if len(err) > max_output:
            err = err[:max_output] + f"\n... [کوتاه‌شد از {len(err)} کاراکتر]"
        return {"ok": proc.returncode == 0, "exit": proc.returncode, "stdout": out, "stderr": err,
                "duration_s": round(time.monotonic() - started, 2), "workdir": str(workdir)}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"فرمان یافت نشد: {e.filename}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def register_code_tools(registry: Any, workspace: Path) -> None:
    """ابزارهای run_python / run_shell — خطرناک (عبور از تایید انسانی)."""

    R = registry.register_function

    R(
        name="run_python",
        description="اجرای یک اسنیپت Python در سندباکس ایزوله (subprocess با تایم‌اوت). برای محاسبه/تست/پردازش.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 30}},
            "required": ["code"],
        },
        dangerous=True,
        func=lambda code, timeout=30: run_in_sandbox(
            ["python3", "-I", "-c", code], workdir=workspace, timeout=timeout
        ),
    )
    R(
        name="run_shell",
        description="اجرای یک فرمان shell در سندباکس (بدون pipe پیچیده؛ argv امن). برای ls/cat/git و…",
        parameters={
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "description": "مثل [\"ls\",\"-la\"]"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["argv"],
        },
        dangerous=True,
        func=lambda argv, timeout=30: run_in_sandbox([str(a) for a in argv], workdir=workspace, timeout=timeout),
    )
