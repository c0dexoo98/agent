# -*- coding: utf-8 -*-
"""ابزارهای وب (Web Tools) — واکشی صفحه و جستجوی وب، بدون وابستگی خارجی.

  * http_get(url) → واکشی با urllib (تایم‌اوت + سقف حجم) و استخراج متن از HTML
  * web_search(query) → جستجوی DuckDuckGo (HTML Lite) و استخراج نتایج
هر دو خطای شبکه را با پیام مهربان برمی‌گردانند تا مدل مسیر جایگزین بخواند.
"""
from __future__ import annotations

import html as _html
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List
from urllib.parse import quote_plus, urlparse

UA = "Mozilla/5.0 (X11; Linux x86_64) DevX-Agent/4.0"
DEFAULT_TIMEOUT = 20
MAX_BYTES = 1_500_000

# محدوده‌های خصوصی/لوکال — ضد SSRF: مدل نباید سرویس محلی/شبکهٔ داخلی را واکشی کند
_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0|\[::1\]|::1|\[::ffff:127\.0\.0\.1\])$",
    re.IGNORECASE,
)


def _is_private_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return True
    if not host:
        return True
    if _PRIVATE_HOST_RE.match(host):
        return True
    # نام‌های داخلی رایج
    if host.endswith((".local", ".internal", ".lan", ".home")) or host == "metadata.google.internal":
        return True
    # hostname که اصلاً IP است و در regex نیفتاد — بررسی عددی
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return False  # نام دامنهٔ عمومی


def _fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fa,en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_BYTES)
        ctype = resp.headers.get("Content-Type", "")
    if "charset=" in ctype:
        enc = ctype.split("charset=")[1].split(";")[0].strip().strip('"')
    else:
        enc = "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def html_to_text(raw: str) -> str:
    """استخراج متن قابل‌خواندن از HTML (حذف script/style/تگ‌ها + فشرده‌سازی فاصله)."""
    raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def http_get(url: str, max_chars: int = 12000, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "error": "فقط URLهای http/https مجاز است"}
    if _is_private_host(url):
        return {"ok": False, "error": "واکشی آدرس‌های لوکال/شبکهٔ خصوصی (127.0.0.1، 10.x، 192.168.x، …) مجاز نیست"}
    try:
        raw = _fetch(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code} برای {url}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"واکشی ناموفق: {e}"}
    ctype_json = "json" in url.lower()
    text = raw if ctype_json else html_to_text(raw)
    content = text[: int(max_chars)]
    return {"ok": True, "url": url, "length": len(text), "content": content}


def web_search(query: str, limit: int = 8) -> Dict[str, Any]:
    """جستجوی وب با DuckDuckGo Lite — خروجی: [{title, url, snippet}]"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "عبارت جستجو خالی است"}
    try:
        raw = _fetch("https://html.duckduckgo.com/html/?q=" + quote_plus(q))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"جستجو ناموفق: {e}"}
    results: List[Dict[str, Any]] = []
    blocks = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, flags=re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, flags=re.DOTALL)
    for i, (href, title) in enumerate(blocks[: max(1, int(limit))]):
        # ریدایرکت داخلی DDG → URL واقعی
        m = re.search(r"uddg=([^&]+)", href)
        url = _html.unescape(m.group(1)) if m else href
        if url.startswith("//"):
            url = "https:" + url
        snippet = _html.unescape(re.sub(r"<[^>]+>", "", snippets[i])) if i < len(snippets) else ""
        results.append({"title": _html.unescape(re.sub(r"<[^>]+>", "", title)).strip(), "url": url, "snippet": snippet.strip()[:300]})
    return {"ok": bool(results), "count": len(results), "results": results}


def register_web_tools(registry: Any) -> None:
    R = registry.register_function
    R(
        name="http_get",
        description="واکشی یک URL و استخراج متن صفحه (HTML→متن)؛ برای مستندات/APIهای عمومی",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer", "default": 12000}},
            "required": ["url"],
        },
        func=lambda url, max_chars=12000: http_get(url, int(max_chars)),
    )
    R(
        name="web_search",
        description="جستجوی وب با DuckDuckGo — نتایج با عنوان/لینک/خلاصه",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}},
            "required": ["query"],
        },
        func=lambda query, limit=8: web_search(query, int(limit)),
    )
