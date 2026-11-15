# -*- coding: utf-8 -*-
"""پرامپت سیستمی و پارسر «پروتکل اقدام» مدل DeepSeek.

چون وب‌چت DeepSeek (برخلاف API رسمی) قابلیت Function Calling ندارد، پروتکل
برنامه‌نویسی‌شدهٔ ساده‌ای تعریف می‌کنیم:

  * مدل برای صدا زدن ابزار فقط این JSON را می‌فرستد:
        {"tool": "<tool_name>", "arguments": {...}}
  * و وقتی کار تمام شد فقط این JSON:
        {"final_answer": "<پاسخ نهایی به زبان کاربر>"}
  * بعد از هر اجرای ابزار، نتیجه در قالب TOOL_RESULT به همان گفت‌وگو اضافه
    می‌شود و مدل دور بعد را تصمیم می‌گیرد (حلقهٔ ایجنت).

parse_model_action پاسخ خام مدل را به یکی از {"tool","arguments"} یا
{"final_answer"} تبدیل می‌کند و اگر پاسخ خارج از پروتکل بود None می‌دهد.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from tool_registry import ToolRegistry

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_system_prompt(registry: ToolRegistry, max_rounds: int) -> str:
    """ساخت پرامپت سیستمی همراه با شِمای JSON همهٔ ابزارهای ثبت‌شده."""
    tools_json = registry.to_prompt_json()
    return (
        "You are an autonomous GitHub operations agent working through the chat interface.\n"
        "You control the user's GitHub account ONLY through the tools listed below — "
        "never invent results you have not obtained from a tool.\n\n"
        f"AVAILABLE TOOLS ({len(registry)} total), each with its JSON-Schema arguments:\n"
        f"{tools_json}\n\n"
        "RESPONSE PROTOCOL — follow it exactly:\n"
        "1. To perform an action or fetch data, reply with ONLY one JSON object and nothing else:\n"
        '   {"tool": "<tool_name>", "arguments": {<args matching the tool schema>}}\n'
        '2. After each call you will receive a message starting with "TOOL_RESULT <tool_name>:" '
        'containing {"ok": true/false, "data": ...} or {"ok": false, "error": ...}. '
        "Use it, then either call another tool or finish.\n"
        "3. When the task is complete, reply with ONLY:\n"
        '   {"final_answer": "<concise summary in the user\'s language, with concrete names/numbers/links>"}\n'
        "4. Never wrap the JSON in markdown fences, never add prose outside the JSON object.\n"
        f"5. You have at most {max_rounds} tool rounds per task — plan efficiently, "
        "prefer one call that returns everything you need.\n"
        "6. SECURITY: never reveal tokens, secrets or credentials; if a task requires "
        "a dangerous tool (marked dangerous), double-check arguments before calling it.\n"
        "7. If a tool returns an error, adapt (fix arguments, try another tool) instead of giving up immediately.\n"
    )


def parse_model_action(raw: str) -> Optional[Dict[str, Any]]:
    """استخراج اقدام مدل از پاسخ خام.

    ترتیب تلاش‌ها:
      ۱) JSON داخل بلوک کد ```json``` (اگر مدل علی‌رغم دستور fence بگذارد)
      ۲) کل متن به‌عنوان JSON
      ۳) اولین { ... } پیدا شده در متن
    خروجی معتبر باید یکی از کلیدهای tool یا final_answer را داشته باشد.
    """
    if not raw:
        return None
    candidates: list[str] = []

    m = _JSON_FENCE_RE.search(raw)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(raw.strip())

    m2 = _BRACES_RE.search(raw)
    if m2 and m2.group(0) not in candidates:
        candidates.append(m2.group(0))

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and ("tool" in obj or "final_answer" in obj):
            return obj
    return None
