# -*- coding: utf-8 -*-
"""کلاینت REST v3 گیت‌هاب روی کتابخانهٔ استاندارد خالص (بدون PyGithub).

همان GitHub REST API v3 رسمی با urllib — چرا خالص؟ روی ترموکس اندروید PyGithub
(به‌خاطر cryptography/pynacl) نصب نمی‌شود؛ این کلاینت صفر وابستگی دارد و روی
همه‌جا (PC، سرور، ترموکس) یکسان کار می‌کند. توکن از Device Flow یا کلاسیک است
و فقط در حافظه زندگی می‌کند.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class GhApiError(Exception):
    """خطای REST گیت‌هاب با کد وضعیت و پیام فارسی واضح."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _friendly(status: int, body: str, headers: Dict[str, str]) -> str:
    if status in (401,):
        return "توکن/نشست نامعتبر است (401) — با «ورود با مرورگر» دوباره وصل شو."
    if status == 403:
        remaining = headers.get("x-ratelimit-remaining", "?")
        if remaining == "0":
            reset = headers.get("x-ratelimit-reset", "")
            return f"سهمیهٔ API تمام شده (403 rate limit) — تا unix {reset} صبر کن."
        return "دسترسی ممنوع (403) — اسکوپ توکن یا دسترسی مخزن را بررسی کن."
    if status == 404:
        return "پیدا نشد (404) — نام مخزن/مسیر را بررسی کن (یا دسترسی نداری)."
    if status == 422:
        snippet = (body or "").strip()[:200]
        return f"اعتبارسنجی رد شد (422): {snippet}"
    if status == 409:
        return "تضاد وضعیت (409) — فایل/شاخه از قبل وجود دارد یا تغییر کرده."
    if status >= 500:
        return f"خطای سمت گیت‌هاب ({status}) — بعداً تلاش کن."
    snippet = (body or "").strip()[:200]
    return f"خطای API گیت‌هاب ({status}): {snippet}"


class PureGhClient:
    """REST v3 با احراز Bearer — متدهای لازم ابزارهای ایجنت."""

    def __init__(self, token: str, api_base: str = "https://api.github.com", timeout: int = 30) -> None:
        self.token = (token or "").strip()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.last_scopes: Optional[str] = None

    # ------------------------------------------------------------------
    # هستهٔ HTTP
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        raw_path: bool = False,
    ) -> Any:
        """یک درخواست REST؛ خروجی JSON (204 → {}). مسیر را URL-encode می‌کند."""
        url = self.api_base + path if raw_path else self.api_base + urllib.parse.quote(path, safe="/?=&{}")
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "devx-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                if hdrs.get("x-oauth-scopes"):
                    self.last_scopes = hdrs["x-oauth-scopes"]
                status = resp.status
        except urllib.error.HTTPError as e:
            b = ""
            try:
                b = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            h = {k.lower(): v for k, v in (e.headers or {}).items()}
            raise GhApiError(e.code, _friendly(e.code, b, h)) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GhApiError(0, f"خطای شبکه در اتصال به api.github.com: {e}") from e
        if status == 204 or not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise GhApiError(status, f"پاسخ JSON نبود (HTTP {status})") from e

    # ------------------------------------------------------------------
    # کاربر و سهمیه
    # ------------------------------------------------------------------

    def get_user(self) -> Dict[str, Any]:
        return self.request("GET", "/user")

    def get_rate_limit(self) -> Dict[str, Any]:
        return self.request("GET", "/rate_limit")

    def list_user_repos(self, affiliation: str = "owner", per_page: int = 100) -> List[Dict[str, Any]]:
        return self.request("GET", "/user/repos", params={"affiliation": affiliation, "per_page": per_page, "sort": "pushed"})

    # ------------------------------------------------------------------
    # مخزن
    # ------------------------------------------------------------------

    def get_repo(self, full_name: str) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}")

    def list_branches(self, full_name: str, per_page: int = 100) -> List[Dict[str, Any]]:
        return self.request("GET", f"/repos/{full_name}/branches", params={"per_page": per_page})

    def get_branch(self, full_name: str, branch: str) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/branches/{urllib.parse.quote(branch, safe='')}")

    def delete_repo(self, full_name: str) -> None:
        self.request("DELETE", f"/repos/{full_name}")

    def fork_repo(self, full_name: str) -> Dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/forks")

    def git_tree(self, full_name: str, ref: str, recursive: bool = True) -> Dict[str, Any]:
        return self.request(
            "GET",
            f"/repos/{full_name}/git/trees/{urllib.parse.quote(ref, safe='')}",
            params={"recursive": "1"} if recursive else None,
        )

    # ------------------------------------------------------------------
    # فایل‌ها (contents)
    # ------------------------------------------------------------------

    def get_contents(self, full_name: str, path: str, ref: Optional[str] = None) -> Any:
        p = urllib.parse.quote(path.lstrip("/"), safe="/")
        url = f"/repos/{full_name}/contents/{p}"
        params = {"ref": ref} if ref else None
        return self.request("GET", url, params=params)

    def put_contents(
        self,
        full_name: str,
        path: str,
        content: str,
        message: str,
        branch: Optional[str] = None,
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        p = urllib.parse.quote(path.lstrip("/"), safe="/")
        body: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha
        return self.request("PUT", f"/repos/{full_name}/contents/{p}", body=body)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def list_issues(self, full_name: str, state: str = "open", per_page: int = 100) -> List[Dict[str, Any]]:
        return self.request("GET", f"/repos/{full_name}/issues", params={"state": state, "per_page": per_page})

    def get_issue(self, full_name: str, number: int) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/issues/{int(number)}")

    def create_issue(self, full_name: str, title: str, body: Optional[str] = None, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        b: Dict[str, Any] = {"title": title}
        if body:
            b["body"] = body
        if labels:
            b["labels"] = labels
        return self.request("POST", f"/repos/{full_name}/issues", body=b)

    def add_issue_comment(self, full_name: str, number: int, body: str) -> Dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/issues/{int(number)}/comments", body={"body": body})

    def update_issue_state(self, full_name: str, number: int, state: str) -> Dict[str, Any]:
        return self.request("PATCH", f"/repos/{full_name}/issues/{int(number)}", body={"state": state})

    # ------------------------------------------------------------------
    # Pull Requestها
    # ------------------------------------------------------------------

    def list_pulls(self, full_name: str, state: str = "open", per_page: int = 100) -> List[Dict[str, Any]]:
        return self.request("GET", f"/repos/{full_name}/pulls", params={"state": state, "per_page": per_page})

    def get_pull(self, full_name: str, number: int) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/pulls/{int(number)}")

    def create_pull(self, full_name: str, title: str, head: str, base: str, body: Optional[str] = None) -> Dict[str, Any]:
        b: Dict[str, Any] = {"title": title, "head": head, "base": base}
        if body:
            b["body"] = body
        return self.request("POST", f"/repos/{full_name}/pulls", body=b)

    def merge_pull(
        self,
        full_name: str,
        number: int,
        merge_method: str = "merge",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        b: Dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            b["commit_title"] = commit_title
        if commit_message:
            b["commit_message"] = commit_message
        return self.request("PUT", f"/repos/{full_name}/pulls/{int(number)}/merge", body=b)

    # ------------------------------------------------------------------
    # کامیت‌ها و وضعیت CI
    # ------------------------------------------------------------------

    def list_commits(self, full_name: str, sha: Optional[str] = None, per_page: int = 30) -> List[Dict[str, Any]]:
        return self.request("GET", f"/repos/{full_name}/commits", params={"sha": sha, "per_page": per_page})

    def get_check_runs(self, full_name: str, ref: str, per_page: int = 20) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/commits/{urllib.parse.quote(ref, safe='')}/check-runs", params={"per_page": per_page})

    def get_combined_status(self, full_name: str, ref: str) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/commits/{urllib.parse.quote(ref, safe='')}/status")

    def create_ref(self, full_name: str, ref: str, sha: str) -> Dict[str, Any]:
        return self.request("POST", f"/repos/{full_name}/git/refs", body={"ref": ref, "sha": sha})

    # ------------------------------------------------------------------
    # Actions و Release و Search
    # ------------------------------------------------------------------

    def list_workflows(self, full_name: str, per_page: int = 100) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/actions/workflows", params={"per_page": per_page})

    def workflow_runs(self, full_name: str, per_page: int = 50) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{full_name}/actions/runs", params={"per_page": per_page})

    def dispatch_workflow(self, full_name: str, workflow: str, ref: str, inputs: Optional[Dict[str, str]] = None) -> None:
        w = urllib.parse.quote(str(workflow), safe="")
        self.request("POST", f"/repos/{full_name}/actions/workflows/{w}/dispatches",
                     body={"ref": ref, "inputs": inputs or {}})

    def create_release(
        self,
        full_name: str,
        tag: str,
        name: str,
        body: str = "",
        target_commitish: Optional[str] = None,
        draft: bool = False,
        prerelease: bool = False,
    ) -> Dict[str, Any]:
        b: Dict[str, Any] = {"tag_name": tag, "name": name, "body": body, "draft": draft, "prerelease": prerelease}
        if target_commitish:
            b["target_commitish"] = target_commitish
        return self.request("POST", f"/repos/{full_name}/releases", body=b)

    def search_repos(self, query: str, per_page: int = 50) -> Dict[str, Any]:
        return self.request("GET", "/search/repositories", params={"q": query, "per_page": per_page}, raw_path=True)

    def create_user_repo(self, name: str, description: Optional[str] = None, private: bool = True, auto_init: bool = True) -> Dict[str, Any]:
        b: Dict[str, Any] = {"name": name, "private": private, "auto_init": auto_init}
        if description:
            b["description"] = description
        return self.request("POST", "/user/repos", body=b)
