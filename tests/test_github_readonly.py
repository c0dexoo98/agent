# -*- coding: utf-8 -*-
"""تست فقط‌خواندنی ابزارهای گیت‌هاب با API واقعی — هیچ داده‌ای ساخته یا حذف نمی‌شود.

اجرا (توکن از متغیر محیطی خوانده می‌شود، هرگز در کد نوشته نمی‌شود):
    GH_TOKEN=ghp_xxx python tests/test_github_readonly.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from github_tools import GitHubTools, TokenInvalidError, register_github_tools  # noqa: E402
from tool_registry import ToolRegistry  # noqa: E402


def main() -> None:
    token = os.environ.get("GH_TOKEN", "").strip()
    assert token, "GH_TOKEN در متغیرهای محیطی تنظیم نشده است"

    gh = GitHubTools(token)
    info = gh.validate_token()
    assert info["login"], "login نباید خالی باشد"
    print(f"✅ validate_token → login={info['login']}  scopes={info['token_scopes']}")

    registry = ToolRegistry()
    register_github_tools(registry, gh)

    # هر تست = صدا زدن ابزار از طریق رجیستری (همان مسیر حلقهٔ ایجنت)
    cases: list[tuple[str, dict, callable]] = []

    def check(name: str, args: dict, validator):
        cases.append((name, args, validator))

    check("get_authenticated_user", {}, lambda r: assert_ok(r, lambda d: d["data"]["login"] == info["login"]))
    check("get_rate_limit", {}, lambda r: assert_ok(r, lambda d: d["data"]["remaining"] > 0))
    check("list_repos", {"limit": 5}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["repos"], list)))
    check("get_repo", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: d["data"]["full_name"].endswith("/agent")))
    check("get_repo_status", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: d["data"]["default_branch"] == "main"))
    check("list_branches", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: d["data"]["count"] >= 1))
    check("get_file_contents", {"repo": "agent", "path": "README.md"}, lambda r: assert_ok(r, lambda d: d["data"]["type"] == "file"))
    check("list_repo_files", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: len(d["data"]["files"]) >= 1))
    check("list_issues", {"repo": "agent", "state": "all"}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["issues"], list)))
    check("list_pull_requests", {"repo": "agent", "state": "all"}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["pull_requests"], list)))
    check("list_commits", {"repo": "agent", "limit": 3}, lambda r: assert_ok(r, lambda d: d["data"]["count"] >= 1))
    check("list_workflows", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["workflows"], list)))
    check("get_workflow_runs", {"repo": "agent"}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["workflow_runs"], list)))
    check("search_repositories", {"query": "playwright in:name", "limit": 3}, lambda r: assert_ok(r, lambda d: isinstance(d["data"]["repositories"], list)))

    passed = 0
    for name, args, validator in cases:
        result = registry.execute(name, args)
        validator(result)
        passed += 1
        preview = str(result["data"])[:90]
        print(f"  ✅ {name} → {preview}")

    # تست خطای طبیعی: مخزن ناموجود باید ok=False بدهد، نه کرش
    res = registry.execute("get_repo", {"repo": "nonexistent-repo-xyz-12345/does-not-exist"})
    assert res["ok"] is False, "مخزن ناموجود باید خطا برگرداند"
    print(f"  ✅ خطای مخزن ناموجود درست هندل شد → {res['error'][:80]}")

    print(f"\nALL {passed + 1} READ-ONLY API TESTS PASSED ✅")


def assert_ok(result: dict, validator) -> None:
    assert result["ok"] is True, f"ابزار خطا داد: {result}"
    validator(result)


if __name__ == "__main__":
    try:
        main()
    except TokenInvalidError as e:
        print(f"❌ توکن نامعتبر: {e}")
        sys.exit(1)
