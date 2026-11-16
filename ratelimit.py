# -*- coding: utf-8 -*-
"""محدودسازی نرخ + تلاش مجدد هوشمند (Rate Limit & Backoff).

دو لایهٔ محافظت:
  ۱) سطل توکن (Token Bucket) برای کند کردن مجموعهٔ فراخوانی‌های API گیت‌هاب
     تا نرخ مجاز — قبل از ارسال، نه بعد از خطا.
  ۲) بازتلاش نمایی با Jitter برای خطاهای گذرا (429/403-نرخ/5xx/شبکه/تایم‌اوت)
     — هم برای GitHub API و هم برای خواندن DOM دیپ‌سیک.

`harden_github_tools(gh_tools)` همهٔ متدهای عمومی GitHubTools را بدون تغییر کدِ
آن کلاس شفاف می‌پیچد (الگوی Decorator داینامیک) — یعنی هر ابزار گیت‌هاب به‌طور
خودکار محدود/بازتلاش می‌شود.
"""
from __future__ import annotations

import inspect
import random
import threading
import time
from functools import wraps
from typing import Any, Callable, Iterable, Optional


class RateLimiter:
    """سطل توکن: حداکثر `per_min` فراخوانی در دقیقه، هموارشده."""

    def __init__(self, per_min: float = 60.0, burst: Optional[float] = None) -> None:
        self.rate = max(0.1, float(per_min)) / 60.0     # توکن بر ثانیه
        self.burst = float(burst if burst is not None else max(1.0, per_min / 6.0))
        self._tokens = self.burst
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(min(wait, 2.0))


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    factor: float = 2.0,
    retry_when: Optional[Callable[[BaseException], bool]] = None,
) -> Callable:
    """دکوراتور بازتلاش نمایی؛ شرط بازتلاش با `retry_when` قابل‌تخصیص است."""

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt >= max_attempts:
                        raise
                    if retry_when is not None and not retry_when(e):
                        raise
                    delay = base_delay * (factor ** (attempt - 1)) * (0.8 + 0.4 * random.random())
                    time.sleep(min(delay, 20.0))
            raise last  # pragma: no cover

        return wrapper

    return decorator


# خطاهای گذرا: 429، 403 (نرخ ثانویه)، 5xx و خطاهای شبکه/تایم‌اوت
_TRANSIENT_STATUS = {403, 429, 500, 502, 503, 504}


def is_transient(e: BaseException) -> bool:
    status = getattr(e, "status", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        # 403 می‌تواند دسترسی باشد نه نرخ — اگر هدر x-ratelimit-remaining صفر باشد گذراست
        if status == 403:
            data = getattr(e, "data", None)
            if isinstance(data, dict) and data.get("documentation_url") and "rate limit" not in str(data.get("message", "")).lower():
                return False
        return True
    name = type(e).__name__.lower()
    return any(k in name for k in ("timeout", "connection", "network", "retry"))


def harden_github_tools(gh_tools: Any, limiter: RateLimiter, max_attempts: int = 3) -> int:
    """پیچیدن شفاف همهٔ متدهای عمومی یک GitHubTools با محدودساز + بازتلاش.

    خروجی: تعداد متدهای محافظت‌شده. ایمن است چون فقط متدهای عمومی (بدون _) را
    می‌پوشاند و متدهای property-مانند (validate_token و…) هم درست کار می‌کنند.
    """
    count = 0
    for name, fn in inspect.getmembers(gh_tools, predicate=inspect.ismethod):
        if name.startswith("_") or getattr(fn, "_devx_hardened", False):
            continue
        # متدهای لازم برای خودِ harden نباید دوباره بسته شوند
        @wraps(fn)
        def hardened(*args, _fn=fn, **kwargs):
            limiter.acquire()
            return with_retry(max_attempts=max_attempts, retry_when=is_transient)(_fn)(*args, **kwargs)

        hardened._devx_hardened = True
        try:
            setattr(gh_tools, name, hardened)
            count += 1
        except Exception:
            pass
    return count
