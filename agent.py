#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ایجنت خودمختار DeepSeek + GitHub — نقطهٔ ورود برنامه.

چرخهٔ کار:
  ۱) هنگام راه‌اندازی با getpass یک GitHub Classic Token از کاربر می‌گیرد
     (پیشوند ghp_، ساخته‌شده از github.com/settings/tokens با اسکوپ‌های
      repo و در صورت نیاز workflow) و اعتبار آن را با GET /user می‌سنجد؛
     توکن فقط در حافظهٔ همین اجرا نگه داشته می‌شود و هرگز ذخیره/لاگ نمی‌شود.
  ۲) مرورگر ماندگار Playwright را با user_data_dir ثابت (از .env) باز می‌کند
     و به اکانت chat.deepseek.com وصل می‌شود.
  ۳) حلقهٔ ایجنت (Agent Loop) را اجرا می‌کند: پیام کاربر + شِمای ابزارها به
     دیپ‌سیک می‌رود؛ اگر مدل ابزاری خواست، همان ابزار (مثلاً عملیات گیت‌هاب)
     اجرا و نتیجه به مدل برمی‌گردد تا پاسخ نهایی ساخته شود — حداکثر MAX_ROUNDS
     دور (پیش‌فرض ۱۰) تا از حلقهٔ بی‌پایان جلوگیری شود.

اجرا:
    python agent.py                      # حالت تعاملی (REPL)
    python agent.py "متن تسک"            # حالت تک‌تسک
    python agent.py --tools              # فقط فهرست ابزارها (بدون مرورگر)

دستورات REPL:
    /tools    فهرست ابزارهای فعال        /newchat  گفت‌وگوی تازه در دیپ‌سیک
    /reset    پاک کردن سابقهٔ نمایش      /exit یا /quit خروج
"""
from __future__ import annotations

import json
import sys
from getpass import getpass
from typing import Any, Dict, List, Optional

from github import GithubException

import config
from builtin_tools import register_builtin_tools
from deepseek_client import DeepSeekClient, DeepSeekError
from github_tools import GitHubTools, TokenInvalidError, register_github_tools
from system_prompt import build_system_prompt, parse_model_action
from tool_registry import ToolRegistry

BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║   DeepSeek × GitHub Agent  —  ابزارمحور، پلاگین‌پذیر، خودمختار     ║
║   chat.deepseek.com (Playwright) + GitHub REST (PyGithub 2.x)     ║
╚══════════════════════════════════════════════════════════════════╝
"""


# ======================================================================
# دریافت و اعتبارسنجی توکن گیت‌هاب در راه‌اندازی
# ======================================================================

def obtain_github_tools(max_attempts: Optional[int] = None) -> GitHubTools:
    """گرفتن توکن با getpass و اعتبارسنجی با GET https://api.github.com/user.

    اگر 401 برگشت، پیام واضح «توکن نامعتبر» داده می‌شود و از کاربر خواسته
    می‌شود دوباره وارد کند (تا TOKEN_MAX_ATTEMPTS تلاش).
    """
    attempts = max_attempts or config.TOKEN_MAX_ATTEMPTS
    print("🔑 برای عملیات گیت‌هاب به یک GitHub Classic Token نیاز است.")
    print("   توکن را از https://github.com/settings/tokens بسازید (Generate new token (classic)).")
    print("   اسکوپ‌های حداقلی: repo  — و در صورت نیاز به Actions: workflow")
    print("   ⚠️  توکن کلاسیک به‌طور پیش‌فرض به منابع سازمان‌ها هم دسترسی دارد؛ با احتیاط بسازید و پس از کار حذف کنید.")
    print("   🔒 توکن فقط در حافظهٔ همین اجرا نگه داشته می‌شود و در فایل/لاگ/کد ذخیره نمی‌شود.\n")

    for attempt in range(1, attempts + 1):
        try:
            token = getpass(f"GitHub Classic Token (ghp_...) [تلاش {attempt}/{attempts}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nخروج.")
            sys.exit(130)
        if not token:
            print("❌ هیچ توکنی وارد نشد — دوباره تلاش کنید.\n")
            continue
        if token.startswith("github_pat_"):
            print("❌ این یک Fine-grained Token است؛ طبق مشخصات، Classic Token با پیشوند ghp_ لازم است.\n")
            continue

        gh_tools = GitHubTools(token)  # توکن فقط در همین آبجکتِ درون‌حافظه‌ای زندگی می‌کند
        try:
            info = gh_tools.validate_token()
        except TokenInvalidError as e:
            print(f"❌ توکن نامعتبر است (401 از GitHub) — لطفاً دوباره وارد کنید. ({e})\n")
            continue
        except GithubException as e:
            print(f"❌ خطای API گیت‌هاب ({e.status}) — لطفاً دوباره تلاش کنید.\n")
            continue

        print(f"✅ توکن معتبر است — کاربر: {info['login']}  (اسکوپ‌ها: {info['token_scopes'] or 'نامشخص'})\n")
        return gh_tools

    print("⛔ تعداد تلاش‌های مجاز برای وارد کردن توکن به پایان رسید.")
    sys.exit(2)


# ======================================================================
# حلقهٔ ایجنت
# ======================================================================

def run_agent_task(
    client: DeepSeekClient,
    registry: ToolRegistry,
    system_prompt: str,
    task: str,
    max_rounds: int,
    trace: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """اجرای یک تسک با حلقهٔ ایجنت و برگرداندن پاسخ نهایی مدل.

    هر تسک در یک گفت‌وگوی تازهٔ دیپ‌سیک اجرا می‌شود؛ پیام اول = پرامپت سیستمی
    + تسک کاربر. اگر مدل JSON ابزار فرستاد، ابزار اجرا و نتیجه با پیشوند
    TOOL_RESULT در همان گفت‌وگو فرستاده می‌شود تا مدل دور بعد را تصمیم بگیرد.
    """
    client.new_chat()
    first_message = f"{system_prompt}\n\n=== USER TASK ===\n{task}"
    client.send_message(first_message)

    for round_no in range(1, max_rounds + 1):
        raw = client.wait_for_response()
        action = parse_model_action(raw)

        if action is None:
            # مدل از پروتکل خارج شد — متن خام را به‌عنوان پاسخ نهایی می‌پذیریم
            print("ℹ️  پاسخ خارج از پروتکل JSON بود؛ به‌عنوان پاسخ نهایی پذیرفته شد.")
            return raw

        if "final_answer" in action:
            return str(action.get("final_answer") or "").strip() or raw

        tool_name = str(action.get("tool") or "").strip()
        arguments = action.get("arguments") or {}
        if not tool_name:
            return str(arguments) or raw

        print(f"🔧 دور {round_no}/{max_rounds}: ابزار «{tool_name}» با آرگومان‌ها: {json.dumps(arguments, ensure_ascii=False)[:200]}")
        result = registry.execute(tool_name, arguments)
        status = "OK" if result.get("ok") else "ERROR"
        print(f"   ↳ [{status}] {json.dumps(result, ensure_ascii=False)[:160]}")

        if trace is not None:
            trace.append(
                {
                    "round": round_no,
                    "tool": tool_name,
                    "arguments": arguments,
                    "ok": bool(result.get("ok")),
                }
            )

        payload = f"TOOL_RESULT {tool_name}:\n" + json.dumps(result, ensure_ascii=False)
        client.send_message(payload)

    # سقف دورها پر شد — از حلقهٔ بی‌پایان جلوگیری می‌کنیم
    return (
        f"⚠️ پس از {max_rounds} دورِ ابزار، پاسخ نهایی کامل نشد. "
        "کارهای انجام‌شده بالا خلاصه شده‌اند؛ می‌توانی ادامهٔ کار را به‌صورت تسک جدید بدهی."
    )


# ======================================================================
# آماده‌سازی و رابط خط فرمان
# ======================================================================

def build_registry(gh_tools: GitHubTools) -> ToolRegistry:
    """ساخت رجیستری و ثبت همهٔ پلاگین‌ها (گیت‌هاب + داخلی)."""
    registry = ToolRegistry()
    register_github_tools(registry, gh_tools)
    register_builtin_tools(registry)
    return registry


def print_tools(registry: ToolRegistry) -> None:
    print(f"\n🧰 {len(registry)} ابزار فعال:")
    for name in registry.names():
        tool = registry.get(name)
        mark = " ⚠️dangerous" if tool.dangerous else ""
        desc = tool.description.split(" — ")[0].split(". ")[0][:70]
        print(f"  • {name}{mark} — {desc}")
    print()


def main() -> None:
    args = [a for a in sys.argv[1:]]
    show_tools_only = "--tools" in args

    print(BANNER)

    gh_tools = obtain_github_tools()
    registry = build_registry(gh_tools)

    if show_tools_only:
        print_tools(registry)
        return

    client = DeepSeekClient(
        user_data_dir=config.DEEPSEEK_USER_DATA_DIR,
        url=config.DEEPSEEK_URL,
        headless=config.DEEPSEEK_HEADLESS,
        response_timeout=config.RESPONSE_TIMEOUT,
        poll_interval=config.RESPONSE_POLL_INTERVAL,
        stable_checks=config.RESPONSE_STABLE_CHECKS,
    )
    print(f"🌐 راه‌اندازی مرورگر ماندگار (پروفایل: {config.DEEPSEEK_USER_DATA_DIR}) ...")
    client.start()

    try:
        client.ensure_logged_in()
    except Exception as e:
        print(f"⛔ {e}")
        client.close()
        sys.exit(3)

    system_prompt = build_system_prompt(registry, config.MAX_ROUNDS)
    print_tools(registry)
    print("✅ آماده است. تسک را بنویس (مثلاً: «در مخزن agent یک Issue با عنوان X بساز و اولین کامیت‌ها را خلاصه کن»).")
    print("   دستورات: /tools  /newchat  /exit\n")

    one_shot = " ".join(a for a in args if not a.startswith("--")).strip()

    try:
        if one_shot:
            _run_task(client, registry, system_prompt, one_shot)
            return
        while True:
            try:
                task = input("🧑 > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nخروج.")
                break
            if not task:
                continue
            if task in ("/exit", "/quit", "exit", "quit"):
                print("خداحافظ!")
                break
            if task == "/tools":
                print_tools(registry)
                continue
            if task == "/newchat":
                client.new_chat()
                print("✅ گفت‌وگوی تازه در دیپ‌سیک آغاز شد.\n")
                continue
            _run_task(client, registry, system_prompt, task)
    finally:
        client.close()


def _run_task(client: DeepSeekClient, registry: ToolRegistry, system_prompt: str, task: str) -> None:
    trace: List[Dict[str, Any]] = []
    print("🤖 در حال کار با دیپ‌سیک...\n")
    try:
        answer = run_agent_task(
            client=client,
            registry=registry,
            system_prompt=system_prompt,
            task=task,
            max_rounds=config.MAX_ROUNDS,
            trace=trace,
        )
    except (DeepSeekError, Exception) as e:  # noqa: BLE001 — خطای حلقه نباید برنامه را ببندد
        print(f"⛔ خطا در حلقهٔ ایجنت: {e}\n")
        return
    print("\n" + "─" * 64)
    print("🤖 پاسخ نهایی:")
    print(answer)
    print("─" * 64 + "\n")


if __name__ == "__main__":
    main()
