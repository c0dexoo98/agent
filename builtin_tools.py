# -*- coding: utf-8 -*-
"""ابزارهای داخلی نمونه — برای نشان دادن الگوی پلاگین‌پذیر رجیستری.

اینجا دو ابزار سادهٔ مستقل از گیت‌هاب ثبت می‌شوند تا دیده شود که رجیستری فقط
برای GitHub نیست؛ بعداً می‌توانید ابزارهای جدید (جستجوی وب، خواندن فایل،
اجرای دستور و...) را دقیقاً به همین سبک — یا در ماژول جداگانه‌ای مثل
morx-gms2-mcp که ده‌ها ابزار به مدل می‌دهد — اضافه کنید.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from tool_registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """ثبت ابزارهای داخلی با دکوراتور register."""

    @registry.register(
        name="get_current_time",
        description="دریافت تاریخ و زمان فعلی در یک منطقهٔ زمانی (پیش‌فرض UTC) — برای نمونه‌برداری تاریخ، فریزن رلیز و...",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "نام IANA مثل UTC یا Asia/Tehran",
                    "default": "UTC",
                }
            },
            "required": [],
        },
    )
    def get_current_time(timezone: str = "UTC") -> Dict[str, Any]:
        now = datetime.now(ZoneInfo(timezone))
        return {
            "timezone": timezone,
            "iso": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "time": now.time().strftime("%H:%M:%S"),
        }

    @registry.register(
        name="echo",
        description="برگرداندن همان متن ورودی — ابزار تست سیم‌کشی رجیستری و حلقهٔ ایجنت",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def echo(text: str) -> Dict[str, Any]:
        return {"text": text}
