# -*- coding: utf-8 -*-
"""رجیستری پلاگین‌پذیر ابزارها (Tool Registry).

این ماژول هستهٔ الگوی «ابزارمحور» ایجنت است — مشابه الگویی که سرورهایی مثل
morx-gms2-mcp دنبال می‌کنند: به‌جای اینکه ایجنت فقط یک کار مشخص بلد باشد،
مجموعه‌ای از ابزارها با شِمای JSON به مدل معرفی می‌شوند و مدل تصمیم می‌گیرد
کدام ابزار را با چه آرگومان‌هایی صدا بزند.

افزودن ابزار جدید (مثلاً جستجوی وب، خواندن فایل، اجرای دستور و...) فقط با
دو روش ساده انجام می‌شود و هیچ تغییری در حلقهٔ ایجنت لازم نیست:

    ۱) با دکوراتور (برای توابع مستقل):
        registry = ToolRegistry()

        @registry.register(
            name="web_search",
            description="Search the web...",
            parameters={...JSON Schema...},
        )
        def web_search(query: str, limit: int = 5) -> dict:
            return {"results": [...]}

    ۲) با register_function (برای متدهای کلاس یا توابع بیرونی):
        registry.register_function(
            name="list_repos",
            description="...",
            parameters={...},
            func=gh_tools.list_repos,
        )

هر ابزار باید یک dict برگرداند (خروجی JSON-سازگار)؛ رجیستری خروجی را با
_jsonify امن‌سازی و کوتاه‌سازی می‌کند و خطاها را به‌شکل {"ok": False, "error": ...}
برمی‌گرداند تا مدل بتواند روی خطا واکنش مناسب نشان دهد.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from config import TOOL_RESULT_MAX_CHARS


@dataclass
class Tool:
    """تعریف کامل یک ابزار: نام، توضیح، شِمای آرگومان‌ها (JSON Schema) و تابع اجرا."""

    name: str
    description: str
    parameters: Dict[str, Any]          # JSON Schema: {"type":"object","properties":{...},"required":[...]}
    func: Callable[..., Dict[str, Any]]
    dangerous: bool = False             # ابزارهای مخاطره‌آمیز (مثل delete_repo) علامت‌گذاری می‌شوند
    meta: Dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """نگهداری و اجرای ابزارها؛ هستهٔ پلاگین‌پذیری ایجنت — با دروازهٔ تایید انسانی."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        # دروازهٔ Approval (سرور ست می‌کند): اگر ابزار dangerous بود، قبل از اجرا
        # درخواست تایید ساخته و تا تصمیم کاربر صبر می‌شود.
        self.approvals: Any = None                       # approval.ApprovalManager
        self.approval_policy: Any = None                 # callable(tool_name)->bool (نیاز به تایید؟)
        self.audit: Any = None                           # audit.AuditLog
        self.project_provider: Any = None                # callable()->Optional[int]  (پروژهٔ فعال)

    # ------------------------------------------------------------------
    # ثبت ابزار
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        dangerous: bool = False,
    ) -> Callable:
        """دکوراتور ثبت ابزار از روی یک تابع مستقل.

        پارامترهای تابع باید دقیقاً با کلیدهای properties در شِما هم‌نام باشند،
        چون فراخوانی به‌صورت func(**arguments) انجام می‌شود.
        """

        def decorator(func: Callable[..., Dict[str, Any]]) -> Callable:
            self.register_function(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                dangerous=dangerous,
            )
            return func

        return decorator

    def register_function(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        func: Callable[..., Dict[str, Any]],
        dangerous: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """ثبت مستقیم یک ابزار (مناسب متدهای کلاس و ماژول‌های افزونه)."""
        if name in self._tools:
            raise ValueError(f"ابزار تکراری: '{name}' قبلاً ثبت شده است")
        if not callable(func):
            raise TypeError(f"func برای ابزار '{name}' قابل‌فراخوانی نیست")
        self._validate_schema(name, parameters)
        self._tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            dangerous=dangerous,
            meta=meta or {},
        )

    @staticmethod
    def _validate_schema(name: str, parameters: Dict[str, Any]) -> None:
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"شِمای ابزار '{name}' باید type=object باشد")
        if not isinstance(parameters.get("properties", {}), dict):
            raise ValueError(f"properties ابزار '{name}' باید dict باشد")
        required = parameters.get("required", [])
        props = parameters.get("properties", {})
        if not isinstance(required, list) or any(r not in props for r in required):
            raise ValueError(f"required ابزار '{name}' باید زیرمجموعهٔ properties باشد")

    # ------------------------------------------------------------------
    # دسترسی و اجرا
    # ------------------------------------------------------------------

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"ابزار '{name}' یافت نشد")
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def _needs_approval(self, tool: Tool) -> bool:
        """آیا این ابزار باید از دروازهٔ تایید انسانی عبور کند؟"""
        if not tool.dangerous or self.approvals is None:
            return False
        if self.approval_policy is not None:
            try:
                return bool(self.approval_policy(tool.name))
            except Exception:
                return True
        return True

    def execute(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """اجرای امن یک ابزار و برگرداندن نتیجه به‌شکل {"ok":..., "data"/"error":...}.

        ابزارهای خطرناک با دروازهٔ Approval: درخواست تایید ساخته می‌شود، رویداد
        approval_requested منتشر و تا تصمیم کاربر/TTL صبر می‌شود؛ رد → پیام
        «رد شد» به مدل. هر عبور/رد در لاگ ممیزی زنجیره‌ای ثبت می‌شود.
        هر خطایی گرفته می‌شود تا یک خطای ابزار کل حلقهٔ ایجنت را نابود نکند.
        """
        arguments = dict(arguments or {})
        try:
            tool = self.get(name)
        except KeyError as e:
            return {"ok": False, "error": str(e), "available_tools": self.names()}

        # ── دروازهٔ تایید انسانی ──
        if self._needs_approval(tool):
            proj = None
            if self.project_provider is not None:
                try:
                    proj = self.project_provider()
                except Exception:
                    proj = None
            req = self.approvals.request(name, arguments, project=proj)
            if self.audit is not None:
                self.audit.append("approval_requested", {"approval_id": req["id"], "tool": name, "arguments": arguments}, project=proj)
            return {
                "ok": False,
                "pending_approval": {"id": req["id"], "tool": name, "arguments": arguments, "ttl": self.approvals.ttl_s},
                "error": (
                    f"ابزار '{name}' خطرناک است و به تایید انسانی نیاز دارد — "
                    f"درخواست {req['id']} ساخته شد؛ نتیجه پس از تصمیم کاربر به شما می‌رسد."
                ),
            }

        try:
            result = tool.func(**arguments)
        except TypeError as e:
            # آرگومان‌های ناهماهنگ با امضای تابع → راهنمایی به مدل
            return {
                "ok": False,
                "error": f"آرگومان‌های نامعتبر برای '{name}': {e}",
                "expected_parameters": tool.parameters.get("properties", {}),
            }
        except Exception as e:  # noqa: BLE001 — عمداً همه‌چیز گرفته می‌شود
            status = getattr(e, "status", None)
            msg = str(e)
            if status is not None:
                data = getattr(e, "data", None)
                api_msg = ""
                if isinstance(data, dict):
                    api_msg = str(data.get("message", ""))
                msg = f"GitHub API {status}: {api_msg or msg}"
            if self.audit is not None:
                self._audit_tool(name, arguments, {"ok": False, "error": msg[:200]})
            return {"ok": False, "error": msg}

        data = self._jsonify(result)
        if self.audit is not None:
            self._audit_tool(name, arguments, {"ok": bool(data.get("ok"))})
        return {"ok": True, "data": data}

    def _audit_tool(self, name: str, arguments: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        """ثبت اجرای ابزار در ممیزی — خطا نباید اجرای ابزار را بکشد."""
        try:
            proj = self.project_provider() if self.project_provider else None
            self.audit.append("tool_executed", {"tool": name, "outcome": outcome, "args_keys": sorted(arguments.keys())}, project=proj)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # آماده‌سازی برای مدل
    # ------------------------------------------------------------------

    def to_prompt_json(self) -> str:
        """خلاصهٔ JSON همهٔ ابزارها برای گنجاندن در پرامپت سیستمی مدل."""
        payload = []
        for tool in self._tools.values():
            payload.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    **({"dangerous": True} if tool.dangerous else {}),
                }
            )
        return json.dumps(payload, ensure_ascii=False, indent=1)

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """قالب tools سازگار با OpenAI Function Calling (برای مهاجرت آینده به API رسمی)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    # ------------------------------------------------------------------
    # امن‌سازی خروجی
    # ------------------------------------------------------------------

    def _jsonify(self, value: Any) -> Any:
        """تبدیل هر خروجی ابزار به ساختار JSON-سازگار + کوتاه‌سازی رشته‌های طولانی."""
        try:
            json.dumps(value, ensure_ascii=False)
            return self._truncate(value)
        except (TypeError, ValueError):
            return self._truncate(json.loads(json.dumps(value, ensure_ascii=False, default=str)))

    def _truncate(self, value: Any) -> Any:
        limit = TOOL_RESULT_MAX_CHARS
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + f"\n... [کوتاه‌شد از {len(value)} کاراکتر]"
        if isinstance(value, list):
            return [self._truncate(v) for v in value]
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if isinstance(v, str) and len(v) > limit:
                    out[k] = v[:limit] + f"... [کوتاه‌شد]"
                else:
                    out[k] = self._truncate(v)
            return out
        return value
