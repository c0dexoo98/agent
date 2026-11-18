# -*- coding: utf-8 -*-
"""کلاینت DeepSeek بر پایهٔ Playwright — اتصال به یک اکانت chat.deepseek.com.

روش کار (حفظ روش قبلی طبق مشخصات):
  * playwright.chromium.launch_persistent_context() با یک user_data_dir ثابت
    (از .env) → کوکی‌ها و نشست لاگین اکانت DeepSeek بین اجراها ماندگار می‌مانند.
  * تشخیص لاگین با input[type="tel"] یا input[placeholder*="Phone"] انجام می‌شود
    (اگر این فیلدها در صفحه باشند یعنی صفحهٔ ورود است و هنوز لاگین نکرده‌ایم).
  * ارسال پیام با textarea#chat-input و کلید Enter.
  * دریافت پاسخ از DOM (بلاک‌های Markdown آخرین پیام دستیار).

نکتهٔ مهم اولین اجرا: مرورگر با پنجرهٔ مرئی باز می‌شود (headless=false) و کاربر
یک‌بار باید دستی وارد اکانت شود؛ از آن بعد، پروفایل ماندگار لاگین را حفظ می‌کند.

ترموکس/اندروید: پلی‌رایتِ pip روی bionic اجرا نمی‌شود؛ نودِ سیستم معرفی می‌شود
(PLAYWRIGHT_NODEJS_PATH) و چون نودِ اندروید پلتفرم را «android» گزارش می‌دهد،
PLAYWRIGHT_BROWSERS_PATH هم ست می‌شود تا درایور موقع بالا آمدن نمرده
(«Unsupported platform: android»). کرومیومِ پکیج رسمیِ ترموکس هم خودکار پیدا
می‌شود. پنجرهٔ مرئی روی ترموکس به termux-x11 نیاز دارد؛ بدون آن، اتصال
پس‌زمینه خودکار headless می‌شود.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional

# playwright فقط برای مرورگر دیپ‌سیک لازم است؛ روی دستگاه‌هایی که کرومیوم ندارند
# (مثلاً ترموکس اندروید) بقیهٔ برنامه باید بدون آن هم بالا بیاید — باگ قابل‌حمل بودن پاس ۱۰
try:
    from playwright.sync_api import (
        BrowserContext,
        Page,
        Playwright,
        sync_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    BrowserContext = Page = Playwright = None  # فقط برای type hints (annotations تاخیری‌اند)
    sync_playwright = None


def _is_termux() -> bool:
    """تشخیص ترموکس/اندروید — برای پیام خطای صادقانه (متغیر TERMUX=1 برای تست)."""
    import os
    if os.environ.get("TERMUX") == "1":
        return True
    from pathlib import Path as _P
    if _P("/data/data/com.termux").exists():
        return True
    return "com.termux" in os.environ.get("PREFIX", "")


def _ensure_pw_env() -> None:
    """آماده‌سازی محیط درایور پلی‌رایت — فقط روی ترموکس دست می‌زنیم:

    ۱) PLAYWRIGHT_NODEJS_PATH → نودِ سیستم؛ نودِ داخل بستهٔ pip برای glibc
       بیلد شده و روی ترموکس (bionic) اجرا نمی‌شود → خطای
       «This installation of Playwright does not include Node.js».
    ۲) PLAYWRIGHT_BROWSERS_PATH → دایرکتوری قابل‌نوشتن؛ نود ۲۳+ روی اندروید
       process.platform را 'android' گزارش می‌دهد و درایور پلی‌رایت در
       computeDefaultCacheDirectory فقط linux/darwin/win32 می‌شناسد → کرش
       «Unsupported platform: android» هنگام بالا آمدن درایور که سمت پایتون
       به‌صورت «connection closed while reading from the driver» دیده می‌شود.
       ست‌کردن این متغیر همان چکِ پلتفرم را کامل دور می‌زند.
    روی لینوکس/مک/ویندوز هیچ‌چیز override نمی‌شود (نود و کرومیومِ خود بسته سالم‌اند)؛
    مقدارهایی که کاربر خودش ست کرده باشد هم دست‌نخورده می‌مانند.
    """
    import os
    import shutil
    if not _is_termux():
        return
    if not os.environ.get("PLAYWRIGHT_NODEJS_PATH"):
        node = shutil.which("node") or shutil.which("nodejs")
        if node:
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = node
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        cache_dir = Path.home() / ".cache" / "ms-playwright"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache_dir)


def _has_display() -> bool:
    """نمایشگر گرافیکی داریم؟ (X11 یا Wayland — termux-x11 هم DISPLAY را ست می‌کند)"""
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def auto_headless_background() -> bool:
    """اتصال پس‌زمینهٔ مغز روی لینوکسِ بدون نمایشگر (سرور/ترموکس بدون X11) باید
    headless شود — پنجره‌ای برای بازشدن وجود ندارد. مک/ویندوز دست‌نخورده می‌مانند."""
    import sys
    return sys.platform.startswith("linux") and not _has_display()


def _visible_launch_error(headless: bool) -> Optional[str]:
    """پیام خطای راهنما برای «پنجرهٔ مرئی روی دستگاه بدون نمایشگر»؛ وگرنه None.

    به‌جای کرش مبهم کرومیوم («Missing X server») کاربر می‌فهمد دقیقاً چه کند."""
    import sys
    if headless or sys.platform != "linux" or _has_display():
        return None
    if _is_termux():
        return (
            "پنجرهٔ مرئی مرورگر روی ترموکس فقط با نمایشگر X11 ممکن است: "
            "pkg install termux-x11 را نصب و termux-x11 را اجرا کن، بعد در ترموکس "
            "export DISPLAY=:1 بزن و دوباره «ورود دستی» را بزن. "
            "(راه بدون مرورگر: «کلید API» در تنظیمات.)"
        )
    return (
        "پنجرهٔ مرئی مرورگر روی این سرور ممکن نیست (DISPLAY تنظیم نیست) — "
        "با X forwarding یا روی دسکتاپ اجرا کن؛ برای کار بدون نمایشگر، کلید API بده."
    )


def _pw_driver_help(e: BaseException) -> Optional[str]:
    """تشخیص خطاهای مرگِ درایور پلی‌رایت → پیام راهنمای فارسی قابل‌اقدام."""
    text = str(e)
    if "PLAYWRIGHT_NODEJS_PATH" in text or "does not include Node.js" in text:
        return (
            "پلی‌رایت به Node.js نیاز دارد — روی ترموکس: pkg install nodejs-lts "
            "(مسیرش خودکار پیدا می‌شود؛ دستی: export PLAYWRIGHT_NODEJS_PATH=$PREFIX/bin/node)."
        )
    if "Unsupported platform" in text or "Connection closed" in text:
        return (
            "درایور پلی‌رایت موقع بالا آمدن بست — روی ترموکس دو متغیر لازم است: "
            "PLAYWRIGHT_NODEJS_PATH (نود سیستم) و PLAYWRIGHT_BROWSERS_PATH (فیکسِ "
            "«Unsupported platform: android» زیرا نودِ اندروید پلتفرم را android "
            "گزارش می‌دهد). برنامهٔ جدید خودکار هر دو را ست می‌کند؛ دستی: "
            "export PLAYWRIGHT_BROWSERS_PATH=$HOME/.cache/ms-playwright و "
            "pkg install nodejs-lts chromium"
        )
    return None


def _termux_chromium_path() -> Optional[str]:
    """مسیر کرومیومِ سیستم روی ترموکس — پلی‌رایت روی اندروید کرومیوم خودش را
    ندارد؛ `pkg install chromium` باینری $PREFIX/bin/chromium-browser می‌دهد."""
    if not _is_termux():
        return None
    import shutil
    for name in ("chromium-browser", "chromium", "chrome-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


class DeepSeekError(Exception):
    """خطای عمومی کلاینت دیپ‌سیک."""


class LoginRequired(DeepSeekError):
    """اکنون اکانت لاگین نیست — ورود دستی کاربر لازم است."""


class ResponseTimeout(DeepSeekError):
    """پاسخ مدل در مهلت مقرر کامل نشد."""


class ContextLimitError(DeepSeekError):
    """سایت دیپ‌سیک اعلام کرده context این گفت‌وگو به سقف رسیده.

    موتور ایجنت این خطا را می‌گیرد و «چرخش خودکار چت» انجام می‌دهد:
    چت جدید در سایت + انتقال راهنمای پروژه و خلاصهٔ پیشرفت + ادامهٔ تسک.
    """


class DeepSeekClient:
    """مدیریت مرورگر ماندگار و مکالمه با chat.deepseek.com."""

    # فیلدهای صفحهٔ ورود — نشانهٔ «لاگین نیستی»
    LOGIN_SELECTORS = [
        'input[type="tel"]',
        'input[placeholder*="Phone"]',
        'input[placeholder*="phone"]',
        'input[placeholder*="手机"]',
    ]
    # ورودی چت — نشانهٔ «لاگین هستی»
    CHAT_INPUT_SELECTOR = "textarea#chat-input"
    # بلاک‌های Markdown پاسخ‌های دستیار
    MARKDOWN_SELECTOR = ".ds-markdown--block"
    # نشانه‌های متنی «سقف context پر شد» در صفحه (چینیِ سایت + انگلیسی)
    LIMIT_PATTERNS = (
        "上下文长度已达上限",
        "上下文过长",
        "长度超过限制",
        "已达上限",
        "超出限制",
        "maximum context length",
        "context length limit",
        "exceeds the maximum context",
        "context window is full",
    )

    def __init__(
        self,
        user_data_dir: Path,
        url: str = "https://chat.deepseek.com/",
        headless: bool = False,
        response_timeout: int = 240,
        poll_interval: float = 2.0,
        stable_checks: int = 3,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.url = url
        self.headless = headless
        self.response_timeout = response_timeout
        self.poll_interval = poll_interval
        self.stable_checks = stable_checks
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._on_delta = None  # callback استریم جزئی پاسخ (UI زنده)
        # بیس‌لاین پاسخِ در انتظار: تعداد بلاک‌ها لحظهٔ ارسال + متن ارسالی —
        # تا پاسخِ «دور قبل» به‌عنوان پاسخ جدید برنگردد (باگ مکالمهٔ چنددوری)
        self._baseline_blocks = 0
        self._last_sent_text = ""
        # پایش مصرف context چت جاری (برای Auto Chat Rollover) — تخمین: توکن ≈ کاراکتر/۴
        self.chat_chars_sent = 0
        self.chat_chars_received = 0

    # ------------------------------------------------------------------
    # چرخهٔ عمر مرورگر
    # ------------------------------------------------------------------

    def start(self) -> None:
        """راه‌اندازی مرورگر کرومیوم با پروفایل ماندگار و بازکردن صفحهٔ چت."""
        if self._ctx is not None:
            return
        if not PLAYWRIGHT_AVAILABLE:
            if _is_termux():
                raise DeepSeekError(
                    "playwright نصب نیست — نصبش کن: pip install playwright "
                    "و بعد روی ترموکس: pkg install chromium nodejs-lts "
                    "(بقیهٔ امکانات بدون مرورگر هم کار می‌کند)."
                )
            raise DeepSeekError(
                "playwright نصب نیست — مرورگر دیپ‌سیک روی این دستگاه قابل اجرا نیست. "
                "برای نصب: pip install playwright && playwright install chromium"
            )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        # پنجرهٔ مرئی روی دستگاه بدون نمایشگر (سرور/ترموکس بدون X11) → پیام راهنما
        # به‌جای کرش مبهم کرومیوم؛ اینجا هنوز هیچ پروسه‌ای اسپاون نشده است
        vis_err = _visible_launch_error(self.headless)
        if vis_err:
            raise DeepSeekError(vis_err)
        # ترموکس: نود سیستم + دایرکتوری cache — فیکس «Unsupported platform: android»
        _ensure_pw_env()
        try:
            self._pw = sync_playwright().start()
        except Exception as e:  # noqa: BLE001 — خطاهای درایور را به راهنمای فارسی تبدیل می‌کنیم
            help_msg = _pw_driver_help(e)
            if help_msg:
                raise DeepSeekError(help_msg) from e
            raise
        launch_kwargs = dict(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",  # کاهش ردیابی اتوماسیون
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=IsolateOrigins",
            ],
        )
        # ترموکس: کرومیومِ پکیج رسمی به‌جای دانلودِ خود پلی‌رایت (که روی اندروید نیست)؛
        # سندباکس setuid ندارد و GPU اندروید در headless ناپایدار است → دو پرچم اضافی
        termux_exe = _termux_chromium_path()
        if termux_exe:
            launch_kwargs["executable_path"] = termux_exe
            launch_kwargs["args"] = launch_kwargs["args"] + ["--no-sandbox"]
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:  # noqa: BLE001 — مرگ درایور هنگام launch هم همان راهنما را بگیرد
            help_msg = _pw_driver_help(e)
            if help_msg:
                raise DeepSeekError(help_msg) from e
            raise
        self._page = self._ctx.pages[-1] if self._ctx.pages else self._ctx.new_page()
        self._baseline_blocks = 0
        self._last_sent_text = ""
        self._goto(self.url)

    def _goto(self, url: str) -> None:
        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            self._page.wait_for_selector("body", timeout=15_000)
        except Exception:
            pass

    def close(self) -> None:
        """بستن مرورگر و آزادسازی منابع."""
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._ctx = None
            self._pw = None
            self._page = None

    def __enter__(self) -> "DeepSeekClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # وضعیت لاگین
    # ------------------------------------------------------------------

    def is_logged_in(self) -> bool:
        """لاگین بودن = ورودی چت موجود است و هیچ فیلد تلفنِ صفحهٔ ورود دیده نمی‌شود."""
        assert self._page is not None
        try:
            if self._page.query_selector(self.CHAT_INPUT_SELECTOR):
                return True
        except Exception:
            pass
        for sel in self.LOGIN_SELECTORS:
            try:
                if self._page.query_selector(sel):
                    return False
            except Exception:
                continue
        # نه ورودی چت داریم نه صفحهٔ ورود (مثلاً صفحهٔ وسط یا تبلیغ)
        return False

    def ensure_logged_in(self, wait_timeout: int = 600) -> bool:
        """تضمین لاگین‌بودن اکانت؛ اگر لاگین نیست، منتظر ورود دستی کاربر می‌ماند.

        در حالت headless ورود دستی ممکن نیست، پس مستقیم خطا می‌دهیم تا کاربر
        یک‌بار با headless=false وارد شود.
        """
        if self.is_logged_in():
            return True
        if self.headless:
            # SPA ممکن است هنوز رندر نشده باشد (بعد از goto فقط DOMContentLoaded
            # صبر شده) — قبل از اعلام «لاگین نیست»، تا ۳۰ ثانیه فرصت می‌دهیم؛
            # وگرنه روی سرور بدون نمایشگر، سشنِ سالم همیشه «لاگین لازم» گزارش می‌شد
            spa_deadline = time.time() + 30
            while time.time() < spa_deadline:
                time.sleep(2)
                try:
                    if self.is_logged_in():
                        return True
                except Exception:
                    continue
            raise LoginRequired(
                "اکانت DeepSeek لاگین نیست و در حالت headless نمی‌توان دستی وارد شد — "
                "دکمهٔ «ورود دستی» را بزن تا پنجرهٔ مرئی باز شود "
                "(روی ترموکس: اول termux-x11 را نصب و اجرا کن و DISPLAY را تنظیم کن)."
            )
        print(
            "\n⚠️  اکانت DeepSeek لاگین نیست.\n"
            "    در پنجرهٔ مرورگری که باز شده، به حساب chat.deepseek.com وارد شو\n"
            f"    (تا {wait_timeout} ثانیه منتظر می‌مانم — پروفایل ماندگار است و فقط یک‌بار لازم است).\n"
        )
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            try:
                if self.is_logged_in():
                    print("✅ لاگین تشخیص داده شد — ادامهٔ کار...\n")
                    return True
            except Exception:
                continue
        raise LoginRequired("کاربر در مهلت مقرر وارد نشد — برنامه را دوباره اجرا کن.")

    # ------------------------------------------------------------------
    # گفت‌وگو
    # ------------------------------------------------------------------

    def _wait_chat_input_ready(self, timeout: int = 60) -> None:
        """انتظار برای آماده‌بودن textarea#chat-input (فعال و قابل‌نوشتن)."""
        assert self._page is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                el = self._page.query_selector(self.CHAT_INPUT_SELECTOR)
                if el and el.is_enabled() and el.is_visible():
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise DeepSeekError(
            "textarea#chat-input پیدا نشد یا آماده نیست — ممکن است UI دیپ‌سیک تغییر کرده باشد "
            "یا پیام قبلی هنوز در حال تولید باشد."
        )

    def _markdown_block_count(self) -> int:
        """تعداد بلاک‌های `.ds-markdown--block` فعلی صفحه (خطا → ۰)."""
        assert self._page is not None
        try:
            return self._page.locator(self.MARKDOWN_SELECTOR).count()
        except Exception:
            return 0

    def send_message(self, text: str) -> None:
        """ارسال پیام: پرکردن textarea#chat-input و فشردن Enter.

        بلافاصله «بیس‌لاین» تعداد بلاک‌های پاسخ ثبت می‌شود تا wait_for_response
        فقط بلاکِ «جدیدِ همین پیام» را به‌عنوان پاسخ بپذیرد — وگرنه در مکالمهٔ
        چنددوری، پاسخ دور قبل پس از ~۶ ثانیه به‌عنوان پاسخ جدید برمی‌گشت."""
        self._wait_chat_input_ready()
        assert self._page is not None
        box = self._page.query_selector(self.CHAT_INPUT_SELECTOR)
        assert box is not None
        # Enter در دیپ‌سیک پیام را می‌فرستد؛ Shift+Enter خط جدید — پیام تک‌خطی می‌فرستیم
        single_line = text.replace("\r", "").replace("\n", "\\n")
        box.fill("")
        box.fill(single_line)
        # بیس‌لاین قبل از Enter: بعضی UIها بلاک پاسخ را همان لحظهٔ keydown می‌سازند —
        # شمردن بعد از Enter یعنی بلاکِ همین پیام در بیس‌لاین حساب می‌شد و پاسخ هرگز «جدید» نبود
        self._baseline_blocks = self._markdown_block_count()
        box.press("Enter")
        self._last_sent_text = single_line
        self.chat_chars_sent += len(text)

    def new_chat(self) -> None:
        """شروع گفت‌وگوی تازه (رفتن به صفحهٔ اصلی = نشست جدید) + صفر کردن پایش مصرف."""
        self._goto(self.url)
        self._wait_chat_input_ready(timeout=90)
        self.chat_chars_sent = 0
        self.chat_chars_received = 0
        self._baseline_blocks = 0
        self._last_sent_text = ""

    def chat_usage(self) -> Dict[str, int]:
        """مصرف تخمینی چت جاری — برای داشبورد و تصمیم چرخش."""
        return {
            "chars_sent": self.chat_chars_sent,
            "chars_received": self.chat_chars_received,
            "est_tokens": (self.chat_chars_sent + self.chat_chars_received) // 4,
        }

    def set_on_delta(self, callback) -> None:
        """ثبت callback استریم جزئی: هر بار متن پاسخ تغییر کند صدا زده می‌شود."""
        self._on_delta = callback

    def _page_mentions_limit(self, exclude: str = "") -> bool:
        """اسکن سبک متن صفحه برای پیام‌های «سقف context پر شد» (best-effort).

        متن پیامِ ارسالیِ خودمان از اسکن حذف می‌شود — اگر تسک کاربر عبارتی مثل
        «maximum context length» را نقل کرده باشد، نباید چرخش اشتباه چت بدهد."""
        assert self._page is not None
        try:
            body = self._page.query_selector("body")
            if body is None:
                return False
            low = (body.inner_text() or "").lower()
            if exclude:
                low = low.replace(exclude.lower(), "")
            return any(p in low for p in self.LIMIT_PATTERNS)
        except Exception:
            return False

    def wait_for_response(self, timeout: Optional[int] = None, no_reply_timeout: int = 120) -> str:
        """انتظار برای کامل شدن پاسخِ «همین پیام» و برگرداندن متن آن.

        منطق تشخیص «پاسخ جدید»: فقط بلاکی که بعد از بیس‌لاینِ ثبت‌شده در
        send_message اضافه شده شمرده می‌شود (در مکالمهٔ چنددوری، بلاک‌های
        پاسخ‌های قبلی نادیده گرفته می‌شوند). «کامل شدن»: متن بلاک جدید چند پول
        متوالی بدون تغییر بماند و textarea#chat-input دوباره فعال شود (حین
        تولید، ورودی غیرفعال است). حین تولید، تغییر متن با on_delta به بیرون
        استریم می‌شود.

        اگر پیام فرستاده شد ولی هیچ بلاک جدیدی نیامد و ورودی هم فعال بود:
        بعد از چند پول، متن صفحه از نظر الگوهای «سقف context» اسکن می‌شود →
        ContextLimitError (موتور چت را می‌چرخاند)؛ اگر الگویی نبود و تا
        no_reply_timeout هم پاسخی نیامد، خطای صریح می‌دهیم (به‌جای برگرداندن
        پاسخ کهنهٔ دور قبل).
        """
        assert self._page is not None
        timeout = timeout or self.response_timeout
        deadline = time.time() + timeout
        no_reply_deadline = time.time() + no_reply_timeout
        baseline = self._baseline_blocks
        last_text = ""
        last_delta = ""
        stable = 0
        no_new_block = 0
        while time.time() < deadline:
            n = 0
            text = ""
            try:
                blocks = self._page.locator(self.MARKDOWN_SELECTOR)
                n = blocks.count()
                if n > baseline:
                    text = blocks.nth(n - 1).inner_text().strip()
            except Exception:
                text = ""
            input_ready = True
            try:
                el = self._page.query_selector(self.CHAT_INPUT_SELECTOR)
                input_ready = bool(el and el.is_enabled())
            except Exception:
                pass

            if text and text != last_delta and self._on_delta is not None:
                last_delta = text
                try:
                    self._on_delta(text)
                except Exception:
                    pass

            if text and text == last_text and input_ready:
                stable += 1
                if stable >= self.stable_checks:
                    self.chat_chars_received += len(text)
                    return text
            else:
                stable = 0
                if text:
                    last_text = text

            if n <= baseline and input_ready:
                # پیام فرستاده شده ولی بلاک جدیدی نیامده — یا سایت جواب نداد
                # یا پیام خطای «سقف context» را نمایش می‌دهد (بعد از دور اول
                # هم، چون بلاک‌های قدیمی در صفحه‌اند، از همین‌جا تشخیص می‌شود)
                no_new_block += 1
                if no_new_block >= 2 and self._page_mentions_limit(self._last_sent_text):
                    raise ContextLimitError(
                        "سایت دیپ‌سیک اعلام کرد context این گفت‌وگو پر است — چرخش خودکار چت لازم است."
                    )
                if time.time() >= no_reply_deadline:
                    raise DeepSeekError(
                        f"پیام فرستاده شد ولی تا {no_reply_timeout} ثانیه پاسخ جدیدی از دیپ‌سیک نیامد "
                        "(پاسخ قبلی به‌عنوان پاسخ جدید حساب نمی‌شود) — دوباره تلاش کن یا RESPONSE_TIMEOUT را چک کن."
                    )
            else:
                no_new_block = 0
            time.sleep(self.poll_interval)
        raise ResponseTimeout(
            f"پاسخ دیپ‌سیک در {timeout} ثانیه کامل نشد (RESPONSE_TIMEOUT را در .env بیشتر کن)."
        )

    def ask(self, message: str, timeout: Optional[int] = None) -> str:
        """میان‌بر: ارسال پیام + انتظار پاسخ (بدون مدیریت مکالمهٔ چنددوری)."""
        self.send_message(message)
        return self.wait_for_response(timeout)
