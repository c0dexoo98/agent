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
                    "playwright نصب نیست — مرورگر دیپ‌سیک روی ترموکس/اندروید قابل اجرا نیست "
                    "(کرومیوم برای playwright در اندروید وجود ندارد). بقیهٔ امکانات کار می‌کند؛ "
                    "برای اجرای کامل تسک‌ها از کامپیوتر استفاده کن."
                )
            raise DeepSeekError(
                "playwright نصب نیست — مرورگر دیپ‌سیک روی این دستگاه قابل اجرا نیست. "
                "برای نصب: pip install playwright && playwright install chromium"
            )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
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
        self._page = self._ctx.pages[-1] if self._ctx.pages else self._ctx.new_page()
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
            raise LoginRequired(
                "اکانت DeepSeek لاگین نیست و در حالت headless نمی‌توان دستی وارد شد؛ "
                "یک‌بار با DEEPSEEK_HEADLESS=false اجرا کن و در پنجرهٔ مرورگر وارد شو."
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

    def send_message(self, text: str) -> None:
        """ارسال پیام: پرکردن textarea#chat-input و فشردن Enter."""
        self._wait_chat_input_ready()
        assert self._page is not None
        box = self._page.query_selector(self.CHAT_INPUT_SELECTOR)
        assert box is not None
        # Enter در دیپ‌سیک پیام را می‌فرستد؛ Shift+Enter خط جدید — پیام تک‌خطی می‌فرستیم
        single_line = text.replace("\r", "").replace("\n", "\\n")
        box.fill("")
        box.fill(single_line)
        box.press("Enter")
        self.chat_chars_sent += len(text)

    def new_chat(self) -> None:
        """شروع گفت‌وگوی تازه (رفتن به صفحهٔ اصلی = نشست جدید) + صفر کردن پایش مصرف."""
        self._goto(self.url)
        self._wait_chat_input_ready(timeout=90)
        self.chat_chars_sent = 0
        self.chat_chars_received = 0

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

    def _page_mentions_limit(self) -> bool:
        """اسکن سبک متن صفحه برای پیام‌های «سقف context پر شد» (best-effort)."""
        assert self._page is not None
        try:
            body = self._page.query_selector("body")
            if body is None:
                return False
            low = (body.inner_text() or "").lower()
            return any(p in low for p in self.LIMIT_PATTERNS)
        except Exception:
            return False

    def wait_for_response(self, timeout: Optional[int] = None) -> str:
        """انتظار برای کامل شدن پاسخ آخرین پیام دستیار و برگرداندن متن آن.

        منطق تشخیص «کامل شدن»: آخرین بلاک `.ds-markdown--block` باید غیرخالی
        باشد، چند پول متوالی بدون تغییر بماند و textarea#chat-input دوباره
        فعال شود (حین تولید، ورودی غیرفعال است). حین تولید، تغییر متن با
        `on_delta` به بیرون استریم می‌شود تا UI زنده تایپ کند.

        اگر مدل بدون تولیدِ هیچ بلاکِ پاسخی متوقف شد و ورودی دوباره فعال بود
        و متن صفحه نشانه‌های سقف context داشت → ContextLimitError (موتور
        همان را می‌گیرد و چت را می‌چرخاند).
        """
        assert self._page is not None
        timeout = timeout or self.response_timeout
        deadline = time.time() + timeout
        last_text = ""
        last_delta = ""
        stable = 0
        empty_ready = 0
        while time.time() < deadline:
            text = ""
            try:
                blocks = self._page.locator(self.MARKDOWN_SELECTOR)
                n = blocks.count()
                if n > 0:
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
            if not text and input_ready:
                # مدل تمام شد ولی هیچ پاسخی تولید نشد — احتمال پیام خطای سقف context
                empty_ready += 1
                if empty_ready >= 2 and self._page_mentions_limit():
                    raise ContextLimitError(
                        "سایت دیپ‌سیک اعلام کرد context این گفت‌وگو پر است — چرخش خودکار چت لازم است."
                    )
            else:
                empty_ready = 0
            time.sleep(self.poll_interval)
        raise ResponseTimeout(
            f"پاسخ دیپ‌سیک در {timeout} ثانیه کامل نشد (RESPONSE_TIMEOUT را در .env بیشتر کن)."
        )

    def ask(self, message: str, timeout: Optional[int] = None) -> str:
        """میان‌بر: ارسال پیام + انتظار پاسخ (بدون مدیریت مکالمهٔ چنددوری)."""
        self.send_message(message)
        return self.wait_for_response(timeout)
