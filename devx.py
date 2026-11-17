#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DevX — لانچر رابط وب حرفه‌ای ایجنت.

اجرا:
    python devx.py                 # سرور + باز شدن خودکار مرورگر
    python devx.py --no-browser    # فقط سرور (برای ریموت/سرور)
    python devx.py --port 9000     # پورت دلخواه

محیط: http://127.0.0.1:8770  (تم قرمز-مشکی با اکسنت فیروزه‌ای/زرد)
توکن گیت‌هاب را داخل خود UI وارد می‌کنی؛ فقط در حافظهٔ همین اجرا می‌ماند.
حافظهٔ ایجنت (memory/) و مهارت‌های آموخته‌شده (skills_learned/) ماندگارند.
"""
from __future__ import annotations

import argparse
import sys

import config
from server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="DevX — رابط وب ایجنت DeepSeek × GitHub")
    ap.add_argument("--host", default=config.DEVX_HOST, help="آدرس bind (پیش‌فرض 127.0.0.1)")
    ap.add_argument("--port", type=int, default=config.DEVX_PORT, help="پورت (پیش‌فرض 8770)")
    ap.add_argument("--no-browser", action="store_true", help="باز شدن خودکار مرورگر انجام نشود")
    args = ap.parse_args()

    if args.host != config.DEVX_HOST:
        config.DEVX_HOST = args.host
    if args.port != config.DEVX_PORT:
        config.DEVX_PORT = args.port

    print(
        r"""
  ██████╗ ███████╗██╗   ██╗██╗  ██╗
  ██╔══██╗██╔════╝██║   ██║╚██╗██╔╝
  ██║  ██║█████╗  ██║   ██║ ╚███╔╝
  ██║  ██║██╔══╝  ╚██╗ ██╔╝ ██╔██╗
  ██████╔╝███████╗ ╚████╔╝ ██╔╝ ██╗
  ╚═════╝ ╚══════╝  ╚═══╝  ╚═╝  ╚═╝
  ── DevX: DeepSeek × GitHub ────────
     حافظه‌دار • مهارت‌آموز • ابزارمحور
"""
    )
    print(f"  🧠 حافظه: {config.MEMORY_DIR}")
    print(f"  🧰 مهارت‌ها: {config.SKILLS_DIR}")
    print(f"  🌐 دیپ‌سیک: {config.DEEPSEEK_URL} (پروفایل: {config.DEEPSEEK_USER_DATA_DIR})")
    print()

    server = serve(open_browser=not args.no_browser)
    # اتصال خودکار مغز در شروع: اولویت با API (اگر AI_API_KEY هست) وگرنه مرورگر دیپ‌سیک
    try:
        from server import STATE as _STATE
        _STATE.connect_deepseek(visible_login=False)
    except Exception:
        pass
    print("  ⏹  برای خروج Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nخروج؛ بستن مرورگر دیپ‌سیک…")
        try:
            from server import STATE

            if STATE.client is not None:
                STATE.client.close()
        except Exception:
            pass
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
