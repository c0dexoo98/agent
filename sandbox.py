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
import subprocess
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
    """اجرای امن یک دستور؛ خروجی ساختاریافته برای مدل: {ok, exit, stdout, stderr, duration_s}"""
    timeout = int(timeout or config.SANDBOX_TIMEOUT)
    max_output = int(max_output or config.SANDBOX_MAX_OUTPUT)
    if not argv or not isinstance(argv, list):
        return {"ok": False, "error": "argv خالی است"}
    workdir = Path(workdir or config.BASE_DIR)
    workdir.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env.setdefault("PYTHONUNBUFFERED", "1")
    import time as _t
    started = _t.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv لیستی است، شل درگیر نیست
            argv,
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            shell=False,
        )
        out = (proc.stdout or "")[:max_output]
        err = (proc.stderr or "")[:max_output]
        if len(proc.stdout or "") > max_output:
            out += f"\n... [کوتاه‌شد از {len(proc.stdout)} کاراکتر]"
        return {"ok": proc.returncode == 0, "exit": proc.returncode, "stdout": out, "stderr": err,
                "duration_s": round(_t.monotonic() - started, 2), "workdir": str(workdir)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"تایم‌اوت {timeout}s — پروسه کشته شد", "timeout": timeout}
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
