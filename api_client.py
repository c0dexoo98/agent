# -*- coding: utf-8 -*-
"""مغز API — کلاینت سازگار با OpenAI-Chat-Completions روی کتابخانهٔ استاندارد خالص.

چرا؟ مرورگر دیپ‌سیک (playwright + کرومیوم) روی ترموکس/اندروید نصب نمی‌شود؛ این
کلاینت همان رابط Brain موتور را با یک API کلید پیاده می‌کند تا ایجنت روی همه‌جا
کار کند: PC، سرور لینوکس و ترموکس. هیچ وابستگی خارجی ندارد (فقط urllib/json).

سازگار با: DeepSeek API رسمی (api.deepseek.com)، OpenAI، OpenRouter، z.ai و
هر سرویس سازگار با OpenAI-Chat-Completions — کافی است AI_BASE_URL/AI_MODEL را
در .env تنظیم کنید.

قرارداد با موتور (همان رابط DeepSeekBrain در agent_loop):
  new_chat()          → گفت‌وگو از صفر
  send(text)          → پیام بعدی (پرامپت کامل شامل system را خود موتور می‌سازد)
  wait_response()     → متن خام پاسخ مدل (موتور خودش parse_model_action می‌زند)
  client.set_on_delta → استریم زنده به UI (مثل مرورگر)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional


class APIError(Exception):
    """خطای API با کد وضعیت — پیام فارسی واضح دارد."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ContextLimitError(APIError):
    """پنجرهٔ context مدل پر شده (مثلاً 400 «maximum context length»).

    نامش عمداً همان نام deepseek_client.ContextLimitError است — موتور
    (agent_loop._wait) brain-agnostic بر اساس «نام» استثنا چرخش چت را فعال
    می‌کند؛ بدون این، تسک‌های طولانی حالت API به‌جای rollover می‌مردند."""


def _friendly(status: int, body: str) -> str:
    """پیام خطای قابل‌فهم فارسی بر اساس کد وضعیت."""
    if status in (401, 403):
        return "کلید API نامعتبر است یا دسترسی ندارد ({} از سرویس).".format(status)
    if status == 402:
        return "اعتبار حساب API تمام شده است (402) — حساب را شارژ کنید."
    if status == 429:
        return "محدودیت نرخ درخواست API (429) — کمی بعد تلاش کنید."
    if status == 404:
        return "مسیر API پیدا نشد (404) — AI_BASE_URL را بررسی کنید (مثلاً https://api.deepseek.com)."
    if status >= 500:
        return f"خطای سرویس API ({status}) — بعداً تلاش کنید."
    snippet = (body or "").strip()[:180]
    return f"خطای API ({status}): {snippet}"


def _looks_like_context_limit(body: str) -> bool:
    """تشخیص «پنجرهٔ context پر شده» در بدنهٔ خطای 400 (بین سرویس‌های سازگار)."""
    low = (body or "").lower()
    return any(
        p in low
        for p in (
            "maximum context length",
            "context length limit",
            "context window is full",
            "context_length_exceeded",
            "input length and `max_tokens` exceed context limit",
            "too many tokens",
        )
    )


class APIBrain:
    """آداپتور API به رابط Brain موتور — خودش client هم هست (برای set_on_delta)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 240,
        temperature: float = 0.2,
        retries: int = 1,
    ) -> None:
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "deepseek-chat").strip()
        self.timeout = timeout
        self.temperature = temperature
        self.retries = retries
        self._messages: List[Dict[str, str]] = []
        self._on_delta: Optional[Callable[[str], None]] = None
        # برای سازگاری با ابزارهای پایش موتور (مثل chat_chars در rollover)
        self.chat_chars_sent = 0
        self.chat_chars_received = 0

    # موتور با این پرچم آستانهٔ چرخش کوچک‌تر (AI_CONTEXT_TOKENS) را برای مغز
    # API انتخاب می‌کند — پنجرهٔ deepseek-chat خیلی کوچک‌تر از چتِ وب است
    is_api = True

    # ------------------------------------------------------------------
    # رابط Brain
    # ------------------------------------------------------------------

    @property
    def client(self) -> "APIBrain":
        """موتور روی brain.client دنبال set_on_delta می‌گردد — خودم همانم."""
        return self

    def new_chat(self) -> None:
        self._messages.clear()

    def close(self) -> None:
        """منبعی برای بستن نیست (شبکهٔ بدون حالت) — قرارداد مشترک با مغز مرورگر."""
        return None

    def send(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
        self.chat_chars_sent += len(text)  # فقط پیام جدید (قبلاً کل تاریخچه در هر فراخوانی جمع می‌شد)

    def wait_response(self) -> str:
        """فراخوانی /chat/completions با کل تاریخچه؛ استریم SSE و جمع‌کردن دلتاها."""
        payload = {
            "model": self.model,
            "messages": self._messages,
            "stream": True,
            "temperature": self.temperature,
        }
        text = self._chat_stream(payload)
        self._messages.append({"role": "assistant", "content": text})
        return text

    def set_on_delta(self, cb: Optional[Callable[[str], None]]) -> None:
        self._on_delta = cb

    # ------------------------------------------------------------------
    # هستهٔ HTTP
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: Dict[str, Any], stream: bool = False) -> Any:
        """POST با احراز Bearer؛ یک تلاش دوباره روی خطاهای گذرا (429/5xx/شبکه)."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                return resp
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if e.code == 400 and _looks_like_context_limit(body):
                    # سقف پنجرهٔ مدل — موتور چرخش چت (rollover) را فعال می‌کند
                    raise ContextLimitError(
                        e.code, "پنجرهٔ context مدل پر شده — چرخش خودکار چت لازم است."
                    ) from e
                last_err = APIError(e.code, _friendly(e.code, body))
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise last_err
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = APIError(0, f"خطای شبکه در اتصال به API: {e}")
                if attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise last_err
        raise last_err or APIError(0, "خطای ناشناختهٔ API")

    def _chat_stream(self, payload: Dict[str, Any]) -> str:
        """خواندن پاسخ SSE و جمع‌کردن دلتاهای content تا [DONE].

        برخی سرویس‌های «سازگار با OpenAI» استریم را نادیده می‌گیرند و یک JSON
        کامل برمی‌گردانند — در آن حالت همان JSON را می‌خوانیم (fallback).
        on_delta هم مثل مغز مرورگر «متن تجمعی» می‌فرستد تا UI زنده درست کار کند."""
        acc: List[str] = []
        non_sse: List[str] = []
        resp = self._post("/chat/completions", payload, stream=True)
        try:
            with resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        if not line.startswith(":"):  # کامنت keep-alive را نگه نمی‌داریم
                            non_sse.append(line)
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        acc.append(delta)
                        if self._on_delta is not None:
                            try:
                                self._on_delta("".join(acc))  # تجمعی — قرارداد مشترک با مغز مرورگر
                            except Exception:
                                pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
        text = "".join(acc)
        if not text.strip() and non_sse:
            # پاسخ غیر SSE: JSON کامل chat-completion (سرویس‌هایی که stream را رعایت نمی‌کنند)
            try:
                obj = json.loads("\n".join(non_sse))
                msg = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                text = str(msg)
                if text and self._on_delta is not None:
                    try:
                        self._on_delta(text)
                    except Exception:
                        pass
            except (json.JSONDecodeError, ValueError, IndexError, AttributeError):
                pass
        self.chat_chars_received += len(text)
        if not text.strip():
            raise APIError(0, "پاسخ خالی از API برگشت — مدل یا پایگاه را بررسی کنید.")
        return text

    # ------------------------------------------------------------------
    # اعتبارسنجی (GET /models — ارزان و بدون مصرف توکن)
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        """کلید را با GET /models چک می‌کند؛ فهرست مدل‌های قابل‌دسترس برمی‌گرداند."""
        url = f"{self.base_url}/models"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}",
        }, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise APIError(e.code, _friendly(e.code, body))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise APIError(0, f"خطای شبکه در اتصال به API: {e}")
        return [m.get("id", "") for m in (obj.get("data") or []) if m.get("id")]
