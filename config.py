# -*- coding: utf-8 -*-
"""پیکربندی مرکزی ایجنت — همهٔ مقادیر قابل‌تنظیم از فایل .env خوانده می‌شوند.

فایل .env باید کنار همین فایل قرار بگیرد (نمونهٔ کامل در .env.example).
نکتهٔ امنیتی مهم: توکن GitHub هرگز در .env یا هیچ فایل دیگری ذخیره نمی‌شود؛
توکن فقط هنگام راه‌اندازی با getpass() از کاربر گرفته و صرفاً در حافظهٔ همان اجرا نگه داشته می‌شود.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# خواندن .env از کنار پروژه (اگر وجود داشته باشد)
load_dotenv(BASE_DIR / ".env")


def _bool(env_key: str, default: str = "false") -> bool:
    return os.getenv(env_key, default).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# DeepSeek (اتصال با Playwright به اکانت chat.deepseek.com)
# ---------------------------------------------------------------------------

# آدرس چت دیپ‌سیک
DEEPSEEK_URL: str = os.getenv("DEEPSEEK_URL", "https://chat.deepseek.com/")

# user_data_dir ثابت برای launch_persistent_context.
# این پوشهٔ پروفایل کرومیوم است؛ کوکی‌ها/نشست لاگین اکانت DeepSeek در آن ماندگار می‌شوند
# تا در اجراهای بعدی لازم نباشد دوباره وارد شویم. مسیر از .env خوانده می‌شود.
DEEPSEEK_USER_DATA_DIR: Path = Path(
    os.getenv("DEEPSEEK_USER_DATA_DIR", str(BASE_DIR / ".deepseek_profile"))
).expanduser().resolve()

# اجرای بدون پنجرهٔ مرورگر؟ برای ورود دستی اولین بار باید false باشد.
DEEPSEEK_HEADLESS: bool = _bool("DEEPSEEK_HEADLESS", "false")

# ---------------------------------------------------------------------------
# حلقهٔ ایجنت (Agent Loop)
# ---------------------------------------------------------------------------

# حداکثر تعداد دورِ ابزار در هر تسک، برای جلوگیری از حلقهٔ بی‌پایان
MAX_ROUNDS: int = int(os.getenv("MAX_ROUNDS", "10"))

# حداکثر زمان انتظار برای پاسخ کامل مدل دیپ‌سیک (ثانیه)
RESPONSE_TIMEOUT: int = int(os.getenv("RESPONSE_TIMEOUT", "240"))

# فاصلهٔ هر پول از DOM هنگام انتظار پاسخ (ثانیه)
RESPONSE_POLL_INTERVAL: float = float(os.getenv("RESPONSE_POLL_INTERVAL", "2"))

# چند پول متوالی باید پاسخ بدون تغییر باشد تا «کامل» تلقی شود
RESPONSE_STABLE_CHECKS: int = int(os.getenv("RESPONSE_STABLE_CHECKS", "3"))

# ---------------------------------------------------------------------------
# دریافت توکن گیت‌هاب هنگام راه‌اندازی
# ---------------------------------------------------------------------------

# حداکثر تلاش برای وارد کردن توکن معتبر در شروع برنامه
TOKEN_MAX_ATTEMPTS: int = int(os.getenv("TOKEN_MAX_ATTEMPTS", "3"))

# آدرس API گیت‌هاب (پیش‌فرض رسمی)
GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com")

# ---------------------------------------------------------------------------
# محدودیت‌های خروجی ابزارها
# ---------------------------------------------------------------------------

# خروجی ابزارها قبل از ارسال به دیپ‌سیک به این اندازه کوتاه می‌شود (کاراکتر)
TOOL_RESULT_MAX_CHARS: int = int(os.getenv("TOOL_RESULT_MAX_CHARS", "20000"))
