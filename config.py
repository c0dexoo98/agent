# -*- coding: utf-8 -*-
"""پیکربندی مرکزی ایجنت — همهٔ مقادیر قابل‌تنظیم از فایل .env خوانده می‌شوند.

فایل .env باید کنار همین فایل قرار بگیرد (نمونهٔ کامل در .env.example).
نکتهٔ امنیتی: توکن GitHub از نسخهٔ ۴.۲ به‌صورت رمزنگاری‌نشده در فایل محلی
.gh_session.json (دسترسی ۶۰۰، فقط همین دستگاه) ماندگار می‌شود تا بعد از
ری‌استارت نیاز به ورود دوباره نباشد؛ با «خروج» از UI کامل حذف می‌شود.
با GH_SESSION_PERSIST=false می‌توان به رفتار قدیمی (فقط حافظه) برگشت.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    # جایگزین مینیمال python-dotenv برای دستگاه‌هایی که pip ندارد/شکست می‌خورد (ترموکس، باگ پاس ۱۰):
    # خطوط KEY=VALUE سادهٔ .env را در os.environ می‌گذارد — کامنت و نقل‌قول پشتیبانی می‌شود.
    def load_dotenv(path, **_kw):  # type: ignore[misc]
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return False
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return True

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
# مغز API (جایگزین مرورگر — همه‌جا کار می‌کند، از جمله ترموکس/اندروید)
# ---------------------------------------------------------------------------
# هر سرویس سازگار با OpenAI-Chat-Completions: DeepSeek رسمی، OpenRouter، z.ai، ...
# اگر AI_API_KEY تنظیم شده باشد، در اولویت است و مرورگر اصلاً باز نمی‌شود.
# نکتهٔ امنیتی: کلید API در .env می‌ماند (روی دستگاه خودتان) یا فقط در حافظهٔ
# اجرا از طریق UI (/api/ai) داده می‌شود — هرگز در کد/لاگ/گیت ثبت نمی‌شود.
AI_API_KEY: str = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
AI_MODEL: str = os.getenv("AI_MODEL", "deepseek-chat").strip()
AI_TIMEOUT: int = int(os.getenv("AI_TIMEOUT", "240"))

# پایه‌های گیت‌هاب (پیش‌فرض واقعی؛ فقط تست‌ها بازنویسی می‌کنند)
GH_API_BASE: str = os.getenv("GH_API_BASE", "https://api.github.com").rstrip("/")
GH_OAUTH_BASE: str = os.getenv("GH_OAUTH_BASE", "https://github.com").rstrip("/")

# ماندگاری نشست گیت‌هاب: توکن در فایل محلی با دسترسی ۶۰۰ ذخیره می‌شود تا
# بعد از بستن ترمینال/ری‌استارت، لاگین باقی بماند؛ «خروج» فایل را کامل پاک می‌کند.
GH_SESSION_PERSIST: bool = _bool("GH_SESSION_PERSIST", "true")
GH_SESSION_FILE: Path = Path(
    os.getenv("GH_SESSION_FILE", str(BASE_DIR / ".gh_session.json"))
).expanduser().resolve()

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

# ---------------------------------------------------------------------------
# DevX — محیط وب ایجنت (سرور محلی + رابط HUD)
# ---------------------------------------------------------------------------

DEVX_HOST: str = os.getenv("DEVX_HOST", "127.0.0.1")
DEVX_PORT: int = int(os.getenv("DEVX_PORT", "8770"))

# ---------------------------------------------------------------------------
# حافظهٔ ماندگار (Memory)
# ---------------------------------------------------------------------------

MEMORY_DIR: Path = Path(os.getenv("MEMORY_DIR", str(BASE_DIR / "memory"))).expanduser().resolve()

# سقف تعداد آیتم‌های هر بخش حافظه (قدیمی‌ها آرشیو نمی‌شوند، حذف می‌شوند)
MEMORY_MAX_FACTS: int = int(os.getenv("MEMORY_MAX_FACTS", "120"))
MEMORY_MAX_LESSONS: int = int(os.getenv("MEMORY_MAX_LESSONS", "80"))
MEMORY_MAX_TASKS: int = int(os.getenv("MEMORY_MAX_TASKS", "80"))

# چند آیتم مرتبط از هر بخش در پرامپت تزریق شود
MEMORY_INJECT_FACTS: int = int(os.getenv("MEMORY_INJECT_FACTS", "6"))
MEMORY_INJECT_LESSONS: int = int(os.getenv("MEMORY_INJECT_LESSONS", "4"))
MEMORY_INJECT_TASKS: int = int(os.getenv("MEMORY_INJECT_TASKS", "3"))

# ---------------------------------------------------------------------------
# کتابخانهٔ مهارت‌ها (Skills) — یادگیری خودکار ایجنت
# ---------------------------------------------------------------------------

SKILLS_DIR: Path = Path(os.getenv("SKILLS_DIR", str(BASE_DIR / "skills_learned"))).expanduser().resolve()

# پس از هر تسک موفق، ایجنت تلاش کند یک مهارت قابل‌استفادهٔ مجدد استخراج و ذخیره کند؟
AUTO_LEARN_SKILL: bool = _bool("AUTO_LEARN_SKILL", "true")

# اگر در تسک خطای ابزار داشتیم، یک «درس» در حافظه ثبت شود؟
AUTO_LEARN_LESSON: bool = _bool("AUTO_LEARN_LESSON", "true")

# حداکثر مهارت‌های مرتبطی که ابتدای هر تسک به پرامپت تزریق می‌شود
SKILLS_INJECT_LIMIT: int = int(os.getenv("SKILLS_INJECT_LIMIT", "3"))

# ---------------------------------------------------------------------------
# v4 — حافظهٔ هوشمند: RAG / Pin / Decay
# ---------------------------------------------------------------------------

# جستجوی برداری سبک (TF-IDF + کسینوس) علاوه بر جستجوی کلیدواژه‌ای
RAG_ENABLED: bool = _bool("RAG_ENABLED", "true")

# فراموشی هوشمند: آیتم‌های بدون استفاده و کهنه → آرشیو (از context حذف می‌شوند)
MEMORY_DECAY_ENABLED: bool = _bool("MEMORY_DECAY_ENABLED", "true")
MEMORY_DECAY_DAYS: int = int(os.getenv("MEMORY_DECAY_DAYS", "30"))

# ---------------------------------------------------------------------------
# v4 — امنیت: تایید انسانی / سندباکس / محدودسازی نرخ
# ---------------------------------------------------------------------------

# ابزارهای خطرناک قبل از اجرا باید در UI تایید شوند؟
APPROVAL_REQUIRED: bool = _bool("APPROVAL_REQUIRED", "true")
# چند ثانیه موتور منتظر تصمیم کاربر بماند (پس از آن «تایم‌اوت» به مدل می‌رود)
APPROVAL_TIMEOUT: int = int(os.getenv("APPROVAL_TIMEOUT", "180"))
# عمر درخواست‌های تایید بی‌پاسخ در صف
APPROVAL_TTL: int = int(os.getenv("APPROVAL_TTL", "600"))

# سقف‌های سندباکس اجرای کد
SANDBOX_TIMEOUT: int = int(os.getenv("SANDBOX_TIMEOUT", "30"))
SANDBOX_MAX_OUTPUT: int = int(os.getenv("SANDBOX_MAX_OUTPUT", "8000"))

# محدودسازی نرخ گیت‌هاب + بازتلاش
RATELIMIT_GH_PER_MIN: float = float(os.getenv("RATELIMIT_GH_PER_MIN", "60"))
RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))

# ---------------------------------------------------------------------------
# v4 — ایجنت: بازتاب / زیرایجنت / چک‌پوینت
# ---------------------------------------------------------------------------

# پس از هر تسک، مدل یک بازتاب کوتاه (چه خوب شد/چه بد شد) در حافظه ثبت کند؟
REFLECTION_ENABLED: bool = _bool("REFLECTION_ENABLED", "true")
# سقف دورهای حلقهٔ زیرایجنت در delegate_subtask
SUBAGENT_MAX_ROUNDS: int = int(os.getenv("SUBAGENT_MAX_ROUNDS", "6"))

# ذخیرهٔ چک‌پوینت در پایان هر دور (برای Resume بعد از قطعی)
CHECKPOINTS_ENABLED: bool = _bool("CHECKPOINTS_ENABLED", "true")

# ---------------------------------------------------------------------------
# v4.1 — چرخش خودکار چت (Auto Chat Rollover) هنگام نزدیک‌شدن به سقف context
# ---------------------------------------------------------------------------

# سقف context هر چت دیپ‌سیک (توکن). اگر سایت عدد دقیق‌تری اعلام کرد فقط همین را عوض کن؛
# چون تعداد توکن واقعی از DOM خواندنی نیست، مصرف چت با «کاراکتر/۴» تخمین زده می‌شود.
DEEPSEEK_CONTEXT_TOKENS: int = int(os.getenv("DEEPSEEK_CONTEXT_TOKENS", "1000000"))
# کِی چرخش کنیم؟ وقتی تخمینِ مصرفِ چتِ جاری از این نسبتِ سقف گذشت (۰٫۸۵ = ۸۵٪ سقف)
ROLLOVER_AT_RATIO: float = float(os.getenv("ROLLOVER_AT_RATIO", "0.85"))
# چرخش خودکار فعال؟ (چت جدید در سایت + انتقال راهنمای پروژه + ادامهٔ تسک از همان نقطه)
ROLLOVER_ENABLED: bool = _bool("ROLLOVER_ENABLED", "true")

# ---------------------------------------------------------------------------
# v4 — اتوماسیون و مسیرهای داده
# ---------------------------------------------------------------------------

SCHEDULER_ENABLED: bool = _bool("SCHEDULER_ENABLED", "true")
WATCHER_ENABLED: bool = _bool("WATCHER_ENABLED", "true")
WATCH_INTERVAL_DEFAULT: int = int(os.getenv("WATCH_INTERVAL_DEFAULT", "300"))

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "workspace"))).expanduser().resolve()
EXPORTS_DIR: Path = Path(os.getenv("EXPORTS_DIR", str(BASE_DIR / "exports"))).expanduser().resolve()
SNAPSHOTS_DIR: Path = Path(os.getenv("SNAPSHOTS_DIR", str(BASE_DIR / "snapshots"))).expanduser().resolve()
AUDIT_DIR: Path = Path(os.getenv("AUDIT_DIR", str(BASE_DIR / "audit"))).expanduser().resolve()
CHECKPOINTS_DIR: Path = Path(os.getenv("CHECKPOINTS_DIR", str(BASE_DIR / "checkpoints"))).expanduser().resolve()

