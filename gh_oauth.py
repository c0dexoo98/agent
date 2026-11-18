# -*- coding: utf-8 -*-
"""ورود گیت‌هاب با نشست مرورگر — OAuth Device Flow روی کتابخانهٔ استاندارد خالص.

چرا؟ توکن کلاسیک باید دستی ساخته/کپی شود و PyGithub هم روی ترموکس نصب نمی‌شود.
Device Flow مثل مغز دیپ‌سیک کار می‌کند: هیچ کلیدی ساخته و کپی نمی‌شود — برنامه
یک کد کوتاه می‌گیرد، کاربر در مرورگری که از قبل به گیت‌هاب لاگین است (نشست/کوکی
مرورگر) صفحهٔ github.com/login/device را باز می‌کند، کد را وارد و تأیید می‌کند
و برنامه خودش توکن OAuth می‌گیرد. کل چرخه با urllib خالص است — صفر وابستگی.

جریان رسمی (RFC 8628 / مستندات GitHub OAuth):
  ۱) POST /login/device/code  → device_code, user_code, verification_uri, interval
  ۲) کاربر verification_uri را باز می‌کند و user_code را وارد می‌کند
  ۳) پول POST /login/oauth/access_token تا مجوز → access_token (gho_...)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

# client_id عمومی و متن‌باز GitHub CLI (در مخزن cli/cli منتشر شده است) —
# همان روشی که خود gh auth login --web استفاده می‌کند؛ نیازی به ساخت OAuth App نیست.
GH_CLI_CLIENT_ID = "178c6fc778ccc68e1d6a"

DEFAULT_SCOPES = "repo workflow read:org gist"


class DeviceFlowError(Exception):
    """خطای Device Flow با پیام فارسی واضح."""


def _post_form(url: str, params: Dict[str, str], timeout: int = 20) -> Dict[str, Any]:
    """POST فرم‌encoded با Accept: application/json — هستهٔ هر دو endpoint."""
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "devx-agent",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise DeviceFlowError(f"خطای HTTP {e.code} از گیت‌هاب: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DeviceFlowError(f"خطای شبکه در اتصال به گیت‌هاب: {e}") from e
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        raise DeviceFlowError("پاسخ گیت‌هاب JSON نبود — اتصال/پروکسی را بررسی کن.") from e


class DeviceFlow:
    """یک جلسهٔ ورود با مرورگر: start() کد می‌دهد، poll() وضعیت را چک می‌کند."""

    def __init__(
        self,
        client_id: str = GH_CLI_CLIENT_ID,
        scopes: str = DEFAULT_SCOPES,
        oauth_base: str = "https://github.com",
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id
        self.scopes = scopes
        self.oauth_base = oauth_base.rstrip("/")
        self.timeout = timeout
        self.device_code: Optional[str] = None
        self.user_code: Optional[str] = None
        self.verification_uri: Optional[str] = None
        self.interval: float = 5.0
        self.expires_in: int = 900

    def start(self) -> Dict[str, Any]:
        """گرفتن کد ورود — خروجی برای نمایش به کاربر."""
        resp = _post_form(f"{self.oauth_base}/login/device/code", {
            "client_id": self.client_id,
            "scope": self.scopes,
        }, timeout=self.timeout)
        self.device_code = resp.get("device_code")
        self.user_code = resp.get("user_code")
        self.verification_uri = resp.get("verification_uri", f"{self.oauth_base}/login/device")
        self.interval = float(resp.get("interval", 5))
        self.expires_in = int(resp.get("expires_in", 900))
        if not self.device_code or not self.user_code:
            raise DeviceFlowError(f"پاسخ نامنتظر از گیت‌هاب: {resp}")
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_in": self.expires_in,
            "interval": self.interval,
        }

    def poll(self) -> Dict[str, Any]:
        """یک چک وضعیت: pending / slow_down / authorized / expired / error."""
        if not self.device_code:
            raise DeviceFlowError("اول start() صدا زده شود")
        resp = _post_form(f"{self.oauth_base}/login/oauth/access_token", {
            "client_id": self.client_id,
            "device_code": self.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=self.timeout)
        err = resp.get("error")
        if not err:
            token = resp.get("access_token")
            if not token:
                return {"status": "error", "error": f"پاسخ بدون توکن: {resp}"}
            return {"status": "authorized", "token": token, "scopes": resp.get("scope", "")}
        if err == "authorization_pending":
            return {"status": "pending"}
        if err == "slow_down":
            self.interval = min(30.0, self.interval + float(resp.get("interval", 5)))
            return {"status": "slow_down", "interval": self.interval}
        if err in ("expired_token", "authorization_expired"):
            return {"status": "expired"}
        friendly = {
            "incorrect_client_id": "client_id نادرست است — تنظیمات برنامه را بررسی کن.",
            "unsupported_grant_type": "grant_type پشتیبانی نمی‌شود — نسخهٔ برنامه را به‌روز کن.",
            "access_denied": "درخواست را در مرورگر رد کردی — اگر پشیمانی دوباره «ورود با مرورگر» را بزن.",
        }.get(err, f"خطای گیت‌هاب: {err} ({resp.get('error_description', '')})")
        return {"status": "error", "error": friendly}

    def wait(self, sleep_fn: Callable[[float], None] = time.sleep, max_s: int = 900) -> str:
        """حلقهٔ منتظر-ماندن تا مجوز (برای CLI و تست‌ها)؛ توکن یا خطا.

        خطای گذرای شبکه (قطع لحظه‌ای Wi-Fi/پروکسی) جریان را نمی‌کشد —
        device_code در گیت‌هاب معتبر مانده و پول بعدی ادامه می‌دهد (RFC 8628 §3.5)؛
        کاربر ممکن است همین حالا در حال تأیید کد در مرورگر باشد."""
        deadline = time.monotonic() + min(max_s, self.expires_in + 30)
        while time.monotonic() < deadline:
            try:
                res = self.poll()
            except DeviceFlowError:
                # خطای گذرای شبکه/مهلت درخواست — کد را دور نمی‌ریزیم
                sleep_fn(min(self.interval, 10.0))
                continue
            if res["status"] == "authorized":
                return res["token"]
            if res["status"] == "error":
                raise DeviceFlowError(res["error"])
            if res["status"] == "expired":
                raise DeviceFlowError("کد ورود منقضی شد — دوباره تلاش کن.")
            sleep_fn(self.interval)
        raise DeviceFlowError("زمان انتظار برای تأیید در مرورگر تمام شد — دوباره تلاش کن.")
