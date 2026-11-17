# -*- coding: utf-8 -*-
"""تست ایستا: بدون شبکه — ایمپورت‌ها، رجیستری، شِماها و پارسر پروتکل."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_imports():
    import agent
    import builtin_tools
    import config
    import deepseek_client
    import github_tools
    import system_prompt
    import tool_registry

    assert hasattr(agent, "run_cli") and hasattr(agent, "build_registry")
    assert hasattr(deepseek_client.DeepSeekClient, "send_message")
    assert hasattr(deepseek_client.DeepSeekClient, "set_on_delta")
    assert hasattr(github_tools.GitHubTools, "validate_token")
    # ماژول‌های v4
    import approval, audit, bundles, checkpoint, devx, fs_tools, globalsearch, notifications  # noqa: F401
    import rag, ratelimit, sandbox, scheduler, sessions, skills, timeline, timemachine  # noqa: F401
    import usage, web_tools, webhooks  # noqa: F401
    print("ok: imports")


def test_registry_schema():
    from github_tools import register_github_tools
    from tool_registry import ToolRegistry

    registry = ToolRegistry()
    register_github_tools(registry, github_tools_stub())
    from builtin_tools import register_builtin_tools

    register_builtin_tools(registry)

    expected_github = {
        "get_authenticated_user", "get_rate_limit", "list_repos", "get_repo",
        "get_repo_status", "create_repo", "delete_repo", "get_file_contents",
        "create_or_update_file", "list_repo_files", "list_branches",
        "create_branch", "create_issue", "list_issues", "get_issue",
        "comment_on_issue", "update_issue_state", "create_pull_request",
        "list_pull_requests", "get_pull_request_status", "comment_on_pr",
        "merge_pull_request", "list_commits", "create_release",
        "list_workflows", "trigger_workflow", "get_workflow_runs",
        "fork_repo", "search_repositories",
    }
    expected_builtin = {"get_current_time", "echo"}
    names = set(registry.names())
    missing = (expected_github | expected_builtin) - names
    assert not missing, f"ابزارهای غایب: {missing}"

    # هر ابزار باید شِمای معتبر object با properties داشته باشد
    for name in registry.names():
        schema = registry.get(name).parameters
        assert schema["type"] == "object", name
        assert isinstance(schema.get("properties"), dict), name
        for r in schema.get("required", []):
            assert r in schema["properties"], name
        assert isinstance(schema.get("required", []), list), name
        for prop in schema["properties"].values():
            assert "type" in prop, (name, prop)
    print(f"ok: registry schemas ({len(registry)} tools)")


def github_tools_stub():
    """GitHubTools بدون توکن — فقط برای ثبت ابزارها (کلاینت تنبل است)."""
    from github_tools import GitHubTools

    return GitHubTools(token=None)


def test_execute_builtin():
    from builtin_tools import register_builtin_tools
    from tool_registry import ToolRegistry

    registry = ToolRegistry()
    register_builtin_tools(registry)
    res = registry.execute("echo", {"text": "سلام"})
    assert res["ok"] is True and res["data"]["text"] == "سلام"

    bad = registry.execute("echo", {"wrong_arg": 1})
    assert bad["ok"] is False and "expected_parameters" in bad

    missing = registry.execute("nope", {})
    assert missing["ok"] is False and "nope" in missing["error"]
    print("ok: registry execute (success, bad args, unknown tool)")


def test_dangerous_flag():
    from github_tools import GitHubTools, register_github_tools
    from tool_registry import ToolRegistry

    registry = ToolRegistry()
    register_github_tools(registry, GitHubTools(token=None))
    assert registry.get("delete_repo").dangerous is True
    assert registry.get("merge_pull_request").dangerous is False
    print("ok: dangerous flag")


def test_parse_model_action():
    from system_prompt import parse_model_action

    a = parse_model_action('{"tool": "list_repos", "arguments": {"limit": 5}}')
    assert a == {"tool": "list_repos", "arguments": {"limit": 5}}

    b = parse_model_action('```json\n{"tool": "echo", "arguments": {"text": "hi"}}\n```')
    assert b["tool"] == "echo"

    c = parse_model_action('می‌خواهم ابزار را صدا بزنم: {"tool": "get_repo", "arguments": {"repo": "a/b"}}')
    assert c["tool"] == "get_repo" and c["arguments"]["repo"] == "a/b"

    d = parse_model_action('{"final_answer": "انجام شد ✅"}')
    assert d["final_answer"] == "انجام شد ✅"

    e = parse_model_action("این یک پاسخ معمولی بدون JSON است")
    assert e is None

    f = parse_model_action("")
    assert f is None
    print("ok: parse_model_action")


def test_jsonify_truncation():
    from tool_registry import ToolRegistry

    registry = ToolRegistry()

    @registry.register(name="big_text", description="t", parameters={"type": "object", "properties": {}})
    def big_text():
        return {"blob": "x" * 50000}

    res = registry.execute("big_text", {})
    assert res["ok"] is True
    assert len(res["data"]["blob"]) < 50000, "خروجی باید کوتاه شده باشد"

    @registry.register(name="weird", description="t", parameters={"type": "object", "properties": {}})
    def weird():
        from datetime import datetime

        return {"created": datetime(2026, 1, 2, 3, 4, 5)}

    res2 = registry.execute("weird", {})
    assert res2["ok"] is True and "2026-01-02" in res2["data"]["created"]
    print("ok: jsonify + truncation")


if __name__ == "__main__":
    test_imports()
    test_registry_schema()
    test_execute_builtin()
    test_dangerous_flag()
    test_parse_model_action()
    test_jsonify_truncation()
    print("\nALL STATIC TESTS PASSED ✅")
