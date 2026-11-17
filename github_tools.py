# -*- coding: utf-8 -*-
"""ابزارهای GitHub برای ایجنت — کلاینت REST v3 خالص (بدون PyGithub).

چرا REST خالص؟ توکن کلاسیک باید دستی ساخته/کپی شود و PyGithub روی ترموکس
اندروید نصب نمی‌شود (cryptography/pynacl ویل ندارند و build با Rust شکست
می‌خورد). از پاس «ورود با مرورگر» به بعد این ماژول با gh_api.py (urllib خالص)
کار می‌کند و روی همه‌جا — PC، سرور لینوکس و ترموکس — یکسان است و به هیچ pip نیاز ندارد. ورود توکن با Device Flow انجام می‌شود: کاربر فقط در مرورگری که
از قبل لاگین است یک کد کوتاه را تأیید می‌کند (همان نشست/کوکی مرورگر) و هیچ
کلیدی ساخته یا کپی نمی‌شود؛ توکن فقط در حافظهٔ همین اجرا زندگی می‌کند و هرگز
در فایل، لاگ یا کد ذخیره/چاپ نمی‌شود.

اسکوپ‌های ورود با مرورگر: repo workflow read:org gist
(برای delete_repo به‌تنهایی اسکوپ delete_repo لازم است که پیش‌فرض نیست.)

هر ابزار یک متد مستقل است که ورودی‌های مشخص دارد و خروجی را به‌صورت dict
(JSON-سازگار) برمی‌گرداند تا مدل بتواند از آن استفاده کند.
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

from gh_api import GhApiError, PureGhClient

# سازگاری با کدهای قبلی: GITHUB_LIB و GithubException هنوز import می‌شوند
# (server.py/agent.py و تست‌ها به آن‌ها اشاره دارند) ولی دیگر هیچ عملیاتی
# به PyGithub وابسته نیست — پرچم فقط برای گزارش وضعیت کتابخانهٔ اختیاری می‌ماند.
try:
    from github import GithubException  # noqa: F401
    GITHUB_LIB = True
except ImportError:
    GITHUB_LIB = False

    class GithubException(Exception):
        """جایگزین امن برای except-clause ها وقتی کتابخانهٔ گیت‌هاب نصب نیست."""
        status: int = 0


class GitHubUnavailableError(Exception):
    """منسوخ — از ورود با مرورگر به بعد ابزارهای گیت‌هاب بدون PyGithub هم کار می‌کنند."""


class TokenInvalidError(Exception):
    """وقتی توکن/نشست ۴۰۱ بدهد (نامعتبر یا باطل‌شده)."""


def _sha(sha: Optional[str]) -> Optional[str]:
    return sha[:7] if sha else None


class GitHubTools:
    """ابزارهای گیت‌هاب روی کلاینت REST خالص (gh_api.PureGhClient).

    توکن/نشست فقط در self._token (حافظهٔ همین اجرا) زندگی می‌کند و هرگز
    serialize/لاگ نمی‌شود. بدون PyGithub هم کامل کار می‌کند (ترموکس).
    """

    def __init__(self, token: Optional[str], api_base: Optional[str] = None) -> None:
        self._token = token          # فقط در حافظه — هرگز serialize/لاگ نمی‌شود
        self._api_base = (api_base or "https://api.github.com").rstrip("/")
        self._client: Optional[PureGhClient] = None
        self._user: Optional[Dict[str, Any]] = None   # کاربر احرازهویت‌شده (پس از validate_token)
        self._login: Optional[str] = None
        self._scopes: Optional[str] = None  # مقدار هدر X-OAuth-Scopes از پاسخ /user

    # ------------------------------------------------------------------
    # هسته
    # ------------------------------------------------------------------

    @property
    def client(self) -> PureGhClient:
        """ساخت تنبل کلاینت REST خالص — بدون هیچ وابستگی خارجی."""
        if self._client is None:
            if not self._token:
                raise TokenInvalidError("توکنی در دسترس نیست — ابتدا ورود با مرورگر یا توکن انجام شود")
            self._client = PureGhClient(self._token, api_base=self._api_base)
        return self._client

    def validate_token(self) -> Dict[str, Any]:
        """اعتبارسنجی با GET https://api.github.com/user.

        اگر نام کاربری برگردد، نشست معتبر است؛ اگر 401/403 بدهد
        TokenInvalidError پرتاب می‌شود تا پیام واضح بدهیم و دوباره ورود بخواهیم.
        """
        try:
            data = self.client.get_user()
        except GhApiError as e:
            if e.status in (401, 403):
                raise TokenInvalidError(
                    f"توکن/نشست نامعتبر است یا اسکوپ کافی ندارد (HTTP {e.status})"
                ) from e
            raise
        self._user = data
        self._login = data.get("login")
        self._scopes = self.client.last_scopes
        return {
            "login": self._login,
            "name": data.get("name"),
            "html_url": data.get("html_url"),
            "token_scopes": self._scopes,
        }

    @property
    def login(self) -> Optional[str]:
        return self._login

    def _repo_name(self, repo: str) -> str:
        """«owner/name» یا فقط «name» (مالک = کاربر خودش) → نام کامل."""
        repo = str(repo).strip().strip("/")
        if "/" in repo:
            return repo
        if not self._user:
            self._user = self.client.get_user()
        return f"{self._user.get('login')}/{repo}"

    # ==================================================================
    # ابزارهای فقط‌خواندنی (Read-only)
    # ==================================================================

    def get_authenticated_user(self) -> Dict[str, Any]:
        """اطلاعات کاربرِ صاحب توکن + اسکوپ‌های توکن."""
        if not self._user:
            self.validate_token()
        assert self._user is not None
        return {
            "login": self._user.get("login"),
            "name": self._user.get("name"),
            "email": self._user.get("email"),
            "bio": self._user.get("bio"),
            "html_url": self._user.get("html_url"),
            "public_repos": self._user.get("public_repos"),
            "created_at": self._user.get("created_at"),
            "token_scopes": self._scopes,
        }

    def get_rate_limit(self) -> Dict[str, Any]:
        """سهمیهٔ فعلی API گیت‌هاب (برای عیب‌یابی خطاهای 403 rate-limit)."""
        data = self.client.get_rate_limit()
        core = (data.get("resources") or {}).get("core") or data.get("rate") or {}
        return {
            "limit": core.get("limit"),
            "remaining": core.get("remaining"),
            "reset_utc": core.get("reset"),
        }

    def list_repos(
        self,
        affiliation: str = "owner",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """فهرست مخازن کاربر صاحب توکن.

        affiliation یکی از: owner (مالک خودم)، collaborator، organization_member
        یا ترکیب با کاما مثل owner,collaborator.
        """
        repos = self.client.list_user_repos(affiliation=affiliation, per_page=max(1, min(int(limit), 100)))
        items = []
        for r in repos[: max(1, min(int(limit), 100))]:
            items.append(
                {
                    "full_name": r.get("full_name"),
                    "private": r.get("private"),
                    "description": r.get("description"),
                    "language": r.get("language"),
                    "stars": r.get("stargazers_count"),
                    "pushed_at": r.get("pushed_at"),
                    "html_url": r.get("html_url"),
                }
            )
        return {"count": len(items), "repos": items}

    def get_repo(self, repo: str) -> Dict[str, Any]:
        """متادیتای کامل یک مخزن («owner/name» یا «name» برای مخازن خود کاربر)."""
        r = self.client.get_repo(self._repo_name(repo))
        license_name = None
        lic = r.get("license") or {}
        license_name = lic.get("name") or lic.get("spdx_id")
        return {
            "full_name": r.get("full_name"),
            "private": r.get("private"),
            "description": r.get("description"),
            "html_url": r.get("html_url"),
            "default_branch": r.get("default_branch"),
            "language": r.get("language"),
            "size_kb": r.get("size"),
            "stars": r.get("stargazers_count"),
            "forks": r.get("forks_count"),
            "open_issues": r.get("open_issues_count"),
            "topics": list(r.get("topics") or []),
            "license": license_name,
            "created_at": r.get("created_at"),
            "pushed_at": r.get("pushed_at"),
        }

    def get_repo_status(self, repo: str) -> Dict[str, Any]:
        """وضعیت لحظه‌ای مخزن: شاخه‌ها، آخرین کامیت‌ها، تعداد Issues/PR باز و آخرین اجرای Actions."""
        full = self._repo_name(repo)
        r = self.client.get_repo(full)
        branches = self.client.list_branches(full, per_page=20)
        commits = []
        for c in (self.client.list_commits(full, per_page=5) or [])[:5]:
            ca = (c.get("commit") or {}).get("author") or {}
            commits.append(
                {
                    "sha": _sha(c.get("sha")),
                    "message": ((c.get("commit") or {}).get("message") or "")[:120],
                    "author": ca.get("name"),
                    "date": ca.get("date"),
                }
            )
        last_run = None
        try:
            runs = (self.client.workflow_runs(full, per_page=1) or {}).get("workflow_runs") or []
            if runs:
                run = runs[0]
                last_run = {
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "created_at": run.get("created_at"),
                }
        except GhApiError:
            last_run = None
        return {
            "full_name": r.get("full_name"),
            "default_branch": r.get("default_branch"),
            "branch_count": len(branches),
            "branches": [b.get("name") for b in branches],
            "recent_commits": commits,
            "open_issues": r.get("open_issues_count"),
            "last_workflow_run": last_run,
            "pushed_at": r.get("pushed_at"),
        }

    def list_branches(self, repo: str, limit: int = 50) -> Dict[str, Any]:
        """فهرست شاخه‌های مخزن."""
        full = self._repo_name(repo)
        branches = self.client.list_branches(full, per_page=max(1, min(int(limit), 100)))[: max(1, min(int(limit), 100))]
        items = [
            {
                "name": b.get("name"),
                "sha": _sha((b.get("commit") or {}).get("sha")),
                "protected": b.get("protected"),
            }
            for b in branches
        ]
        return {"count": len(items), "branches": items}

    def get_file_contents(self, repo: str, path: str, ref: Optional[str] = None) -> Dict[str, Any]:
        """محتوای یک فایل (یا فهرست محتویات یک پوشه) از مخزن."""
        full = self._repo_name(repo)
        data = self.client.get_contents(full, path, ref=ref)
        if isinstance(data, list):
            return {
                "type": "directory",
                "path": path,
                "entries": [
                    {"name": c.get("name"), "type": c.get("type"), "path": c.get("path"), "size": c.get("size")}
                    for c in data
                ],
            }
        decoded = ""
        try:
            raw = base64.b64decode(data.get("content") or "")
            decoded = raw.decode("utf-8", errors="replace")
        except Exception as e:
            decoded = f"[محتوای دودویی یا ناخوانا: {e}]"
        return {
            "type": "file",
            "path": data.get("path"),
            "size": data.get("size"),
            "encoding": data.get("encoding"),
            "content": decoded,
        }

    def list_repo_files(
        self,
        repo: str,
        path_prefix: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """فهرست همهٔ فایل‌های مخزن از روی git tree (فیلتر پیشوند مسیر اختیاری)."""
        full = self._repo_name(repo)
        default_branch = self.client.get_repo(full).get("default_branch") or "main"
        tree = self.client.git_tree(full, default_branch, recursive=True)
        prefix = (path_prefix or "").strip().strip("/")
        paths = [
            el.get("path")
            for el in (tree.get("tree") or [])
            if el.get("type") == "blob" and (not prefix or str(el.get("path", "")).startswith(prefix))
        ]
        truncated = len(paths) > int(limit) or bool(tree.get("truncated"))
        return {
            "count": min(len(paths), int(limit)),
            "truncated": truncated,
            "files": paths[: int(limit)],
        }

    def list_issues(
        self,
        repo: str,
        state: str = "open",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """فهرست Issues مخزن (Pull Requestها فیلتر می‌شوند). state: open/closed/all."""
        full = self._repo_name(repo)
        issues = self.client.list_issues(full, state=state, per_page=max(1, min(int(limit), 100)))
        items = []
        for i in issues[: max(1, min(int(limit), 100))]:
            if i.get("pull_request"):
                continue  # PRها در ابزار جداگانهٔ list_pull_requests هستند
            items.append(
                {
                    "number": i.get("number"),
                    "title": i.get("title"),
                    "state": i.get("state"),
                    "user": (i.get("user") or {}).get("login"),
                    "labels": [lb.get("name") for lb in (i.get("labels") or [])],
                    "comments": i.get("comments"),
                    "html_url": i.get("html_url"),
                    "created_at": i.get("created_at"),
                }
            )
        return {"count": len(items), "issues": items}

    def get_issue(self, repo: str, number: int) -> Dict[str, Any]:
        """جزئیات کامل یک Issue."""
        i = self.client.get_issue(self._repo_name(repo), int(number))
        return {
            "number": i.get("number"),
            "title": i.get("title"),
            "state": i.get("state"),
            "author": (i.get("user") or {}).get("login"),
            "body": (i.get("body") or "")[:4000],
            "labels": [lb.get("name") for lb in (i.get("labels") or [])],
            "assignees": [(a or {}).get("login") for a in (i.get("assignees") or [])],
            "comments": i.get("comments"),
            "html_url": i.get("html_url"),
            "created_at": i.get("created_at"),
            "closed_at": i.get("closed_at"),
        }

    def list_pull_requests(
        self,
        repo: str,
        state: str = "open",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """فهرست Pull Requestها. state: open/closed/all."""
        full = self._repo_name(repo)
        pulls = self.client.list_pulls(full, state=state, per_page=max(1, min(int(limit), 100)))
        items = []
        for pr in pulls[: max(1, min(int(limit), 100))]:
            items.append(
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "draft": pr.get("draft"),
                    "user": (pr.get("user") or {}).get("login"),
                    "head": (pr.get("head") or {}).get("ref"),
                    "base": (pr.get("base") or {}).get("ref"),
                    "html_url": pr.get("html_url"),
                    "created_at": pr.get("created_at"),
                }
            )
        return {"count": len(items), "pull_requests": items}

    def get_pull_request_status(self, repo: str, number: int) -> Dict[str, Any]:
        """وضعیت ادغام و CI یک Pull Request (mergeable + نتیجهٔ check runs)."""
        full = self._repo_name(repo)
        pr = self.client.get_pull(full, int(number))
        head_sha = (pr.get("head") or {}).get("sha")
        checks = []
        combined = None
        try:
            cr = self.client.get_check_runs(full, head_sha) if head_sha else {}
            checks = [
                {"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion")}
                for c in (cr.get("check_runs") or [])[:20]
            ]
        except GhApiError as e:
            combined = f"unavailable: check-runs HTTP {e.status}"
        try:
            combined = (self.client.get_combined_status(full, head_sha) if head_sha else {}).get("state")
        except GhApiError as e:
            combined = f"unavailable: status HTTP {e.status}"
        return {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged": pr.get("merged"),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state"),
            "head_sha": _sha(head_sha),
            "combined_status": combined,
            "check_runs": checks,
            "html_url": pr.get("html_url"),
        }

    def list_commits(self, repo: str, branch: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
        """کامیت‌های اخیر مخزن (روی یک شاخهٔ اختیاری)."""
        full = self._repo_name(repo)
        commits = self.client.list_commits(full, sha=branch, per_page=max(1, min(int(limit), 100)))
        items = []
        for c in commits[: max(1, min(int(limit), 100))]:
            ca = (c.get("commit") or {}).get("author") or {}
            items.append(
                {
                    "sha": _sha(c.get("sha")),
                    "message": ((c.get("commit") or {}).get("message") or "")[:200],
                    "author": ca.get("name"),
                    "date": ca.get("date"),
                    "html_url": c.get("html_url"),
                }
            )
        return {"count": len(items), "commits": items}

    def list_workflows(self, repo: str, limit: int = 30) -> Dict[str, Any]:
        """فهرست GitHub Actions workflows مخزن."""
        full = self._repo_name(repo)
        data = self.client.list_workflows(full, per_page=max(1, min(int(limit), 100)))
        items = [
            {"id": w.get("id"), "name": w.get("name"), "path": w.get("path"), "state": w.get("state")}
            for w in (data.get("workflows") or [])[: max(1, min(int(limit), 100))]
        ]
        return {"count": len(items), "workflows": items}

    def get_workflow_runs(self, repo: str, limit: int = 10) -> Dict[str, Any]:
        """آخرین اجراهای GitHub Actions مخزن."""
        full = self._repo_name(repo)
        data = self.client.workflow_runs(full, per_page=max(1, min(int(limit), 50)))
        items = []
        for run in (data.get("workflow_runs") or [])[: max(1, min(int(limit), 50))]:
            items.append(
                {
                    "id": run.get("id"),
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "event": run.get("event"),
                    "branch": run.get("head_branch"),
                    "head_sha": _sha(run.get("head_sha")),
                    "run_number": run.get("run_number"),
                    "html_url": run.get("html_url"),
                    "created_at": run.get("created_at"),
                }
            )
        return {"count": len(items), "workflow_runs": items}

    def search_repositories(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """جستجوی سراسری مخازن در گیت‌هاب (همان نحو جستجوی سایت)."""
        data = self.client.search_repos(query=query, per_page=max(1, min(int(limit), 50)))
        items = []
        for r in (data.get("items") or [])[: max(1, min(int(limit), 50))]:
            items.append(
                {
                    "full_name": r.get("full_name"),
                    "description": r.get("description"),
                    "stars": r.get("stargazers_count"),
                    "html_url": r.get("html_url"),
                }
            )
        return {"query": query, "count": len(items), "repositories": items}

    # ==================================================================
    # ابزارهای نوشتنی (Write)
    # ==================================================================

    def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = True,
        auto_init: bool = True,
    ) -> Dict[str, Any]:
        """ساخت مخزن جدید برای کاربر صاحب توکن (پیش‌فرض: پرایوت با README)."""
        r = self.client.create_user_repo(name=name, description=description or None, private=private, auto_init=auto_init)
        return {"full_name": r.get("full_name"), "private": r.get("private"), "html_url": r.get("html_url")}

    def delete_repo(self, repo: str, confirm: bool = False) -> Dict[str, Any]:
        """حذف مخزن (غیرقابل‌بازگشت!) — نیازمند پارامتر confirm=True و اسکوپ delete_repo."""
        if confirm is not True:
            return {
                "ok": False,
                "error": "برای حذف مخزن باید confirm=true ارسال شود. این عملیات غیرقابل‌بازگشت است.",
            }
        full = self._repo_name(repo)
        self.client.delete_repo(full)
        return {"deleted": full}

    def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ساخت یا به‌روزرسانی یک فایل در مخزن با یک کامیت."""
        full = self._repo_name(repo)
        sha = None
        try:
            existing = self.client.get_contents(full, path, ref=branch)
            if not isinstance(existing, list):
                sha = existing.get("sha")
        except GhApiError as e:
            if e.status != 404:
                raise
        res = self.client.put_contents(full, path, content, message, branch=branch, sha=sha)
        commit_sha = (res.get("commit") or {}).get("sha")
        repo_meta = self.client.get_repo(full)
        default_branch = repo_meta.get("default_branch") or "main"
        return {
            "path": path,
            "branch": branch or default_branch,
            "commit_sha": _sha(commit_sha),
            "html_url": repo_meta.get("html_url") + f"/blob/{branch or default_branch}/{path}",
        }

    def create_branch(self, repo: str, branch_name: str, from_branch: Optional[str] = None) -> Dict[str, Any]:
        """ساخت شاخهٔ جدید از روی یک شاخهٔ مبنا (پیش‌فرض: شاخهٔ پیش‌فرض)."""
        full = self._repo_name(repo)
        base = from_branch or (self.client.get_repo(full).get("default_branch") or "main")
        sha = (self.client.get_branch(full, base).get("commit") or {}).get("sha")
        self.client.create_ref(full, f"refs/heads/{branch_name}", sha)
        return {"created": branch_name, "from": base, "base_sha": _sha(sha)}

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """ساخت Issue جدید در مخزن."""
        i = self.client.create_issue(self._repo_name(repo), title=title, body=body or None, labels=labels or None)
        return {"number": i.get("number"), "title": i.get("title"), "html_url": i.get("html_url")}

    def comment_on_issue(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        """ثبت کامنت روی یک Issue (کامنت PRها هم از همین مسیر است)."""
        full = self._repo_name(repo)
        c = self.client.add_issue_comment(full, int(number), body)
        return {"issue": int(number), "comment_html_url": c.get("html_url")}

    def update_issue_state(self, repo: str, number: int, state: str = "closed") -> Dict[str, Any]:
        """بستن یا بازگشایی یک Issue. state: open یا closed."""
        if state not in ("open", "closed"):
            return {"ok": False, "error": "state باید 'open' یا 'closed' باشد"}
        i = self.client.update_issue_state(self._repo_name(repo), int(number), state)
        return {"number": i.get("number"), "state": i.get("state")}

    def create_pull_request(
        self,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> Dict[str, Any]:
        """ساخت Pull Request از شاخهٔ head به شاخهٔ base."""
        pr = self.client.create_pull(self._repo_name(repo), title=title, body=body or None, head=head, base=base)
        return {"number": pr.get("number"), "title": pr.get("title"), "html_url": pr.get("html_url")}

    def comment_on_pr(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        """ثبت کامنت روی یک Pull Request (در API گیت‌هاب کامنت PR = کامنت Issue متناظر)."""
        full = self._repo_name(repo)
        c = self.client.add_issue_comment(full, int(number), body)
        return {"pr": int(number), "comment_html_url": c.get("html_url")}

    def merge_pull_request(
        self,
        repo: str,
        number: int,
        merge_method: str = "merge",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ادغام یک Pull Request. merge_method: merge / squash / rebase."""
        if merge_method not in ("merge", "squash", "rebase"):
            return {"ok": False, "error": "merge_method باید merge، squash یا rebase باشد"}
        res = self.client.merge_pull(
            self._repo_name(repo), int(number),
            merge_method=merge_method, commit_title=commit_title, commit_message=commit_message,
        )
        return {"number": int(number), "merged": bool(res.get("merged"))}

    def create_release(
        self,
        repo: str,
        tag: str,
        name: str,
        body: str = "",
        target_commitish: Optional[str] = None,
        draft: bool = False,
        prerelease: bool = False,
    ) -> Dict[str, Any]:
        """ساخت Release روی یک تگ."""
        rel = self.client.create_release(
            self._repo_name(repo), tag=tag, name=name, body=body,
            target_commitish=target_commitish, draft=draft, prerelease=prerelease,
        )
        return {"tag": rel.get("tag_name"), "name": rel.get("name"), "html_url": rel.get("html_url")}

    def trigger_workflow(
        self,
        repo: str,
        workflow: str,
        ref: str,
        inputs: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """اجرای دستی یک workflow با workflow dispatch.

        workflow نام فایل (مثل ci.yml) یا شناسهٔ عددی آن است؛ ref شاخهٔ هدف.
        برای اجرا توکن باید اسکوپ workflow داشته باشد.
        """
        full = self._repo_name(repo)
        self.client.dispatch_workflow(full, workflow, ref=ref, inputs=inputs or {})
        return {"workflow": workflow, "ref": ref, "dispatched": True}

    def fork_repo(self, repo: str) -> Dict[str, Any]:
        """فورک گرفتن از یک مخزن به حساب کاربر صاحب توکن."""
        data = self.client.fork_repo(self._repo_name(repo))
        return {"full_name": data.get("full_name"), "html_url": data.get("html_url")}


# ======================================================================
# ثبت ابزارها در رجیستری (لایهٔ پلاگین‌پذیر)
# ======================================================================

def _s(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    """ساخت سریع شِمای JSON Schema برای آرگومان‌های یک ابزار."""
    return {"type": "object", "properties": properties, "required": required or []}


def register_github_tools(registry: Any, gh_tools: GitHubTools) -> None:
    """ثبت همهٔ ابزارهای گیت‌هاب در یک ToolRegistry.

    این تابع همان نقطهٔ اتصال پلاگین است: ماژول‌های دیگر هم دقیقاً به همین
    شکل یک تابع register_xxx_tools(registry, ...) می‌گیرند و ابزارهای خود را
    ثبت می‌کنند (مثلاً web_tools، fs_tools و...).
    """
    R = registry.register_function

    R(
        name="get_authenticated_user",
        description="اطلاعات کاربر صاحب توکن گیت‌هاب (لاگین، اسکوپ‌های توکن و...)",
        parameters=_s({}),
        func=gh_tools.get_authenticated_user,
    )
    R(
        name="get_rate_limit",
        description="سهمیهٔ باقی‌ماندهٔ API گیت‌هاب (برای عیب‌یابی 403)",
        parameters=_s({}),
        func=gh_tools.get_rate_limit,
    )
    R(
        name="list_repos",
        description="فهرست مخازن کاربر. affiliation: owner یا collaborator یا organization_member یا ترکیب با کاما",
        parameters=_s(
            {
                "affiliation": {"type": "string", "default": "owner"},
                "limit": {"type": "integer", "default": 30},
            }
        ),
        func=gh_tools.list_repos,
    )
    R(
        name="get_repo",
        description="متادیتای کامل یک مخزن (repo با قالب owner/name یا فقط name برای مخازن خود کاربر)",
        parameters=_s({"repo": {"type": "string"}}, required=["repo"]),
        func=gh_tools.get_repo,
    )
    R(
        name="get_repo_status",
        description="وضعیت لحظه‌ای مخزن: شاخه‌ها، ۵ کامیت آخر، تعداد Issue باز و آخرین اجرای Actions",
        parameters=_s({"repo": {"type": "string"}}, required=["repo"]),
        func=gh_tools.get_repo_status,
    )
    R(
        name="create_repo",
        description="ساخت مخزن جدید برای کاربر صاحب توکن",
        parameters=_s(
            {
                "name": {"type": "string"},
                "description": {"type": "string", "default": ""},
                "private": {"type": "boolean", "default": True},
                "auto_init": {"type": "boolean", "default": True},
            }
        ),
        func=gh_tools.create_repo,
    )
    R(
        name="delete_repo",
        description="حذف دائمی مخزن — مخاطره‌آمیز؛ نیازمند confirm=true و اسکوپ delete_repo",
        parameters=_s(
            {"repo": {"type": "string"}, "confirm": {"type": "boolean"}},
            required=["repo", "confirm"],
        ),
        func=gh_tools.delete_repo,
        dangerous=True,
    )
    R(
        name="get_file_contents",
        description="محتوای یک فایل یا فهرست یک پوشه در مخزن",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
            }
        ),
        func=gh_tools.get_file_contents,
    )
    R(
        name="create_or_update_file",
        description="ساخت یا به‌روزرسانی یک فایل با یک کامیت",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "message": {"type": "string"},
                "branch": {"type": "string"},
            },
            required=["repo", "path", "content", "message"],
        ),
        func=gh_tools.create_or_update_file,
    )
    R(
        name="list_repo_files",
        description="فهرست فایل‌های مخزن از روی git tree، با فیلتر پیشوند مسیر",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "path_prefix": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 200},
            }
        ),
        func=gh_tools.list_repo_files,
    )
    R(
        name="list_branches",
        description="فهرست شاخه‌های مخزن",
        parameters=_s(
            {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 50}}
        ),
        func=gh_tools.list_branches,
    )
    R(
        name="create_branch",
        description="ساخت شاخهٔ جدید از روی شاخهٔ مبنا (پیش‌فرض شاخهٔ پیش‌فرض)",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "branch_name": {"type": "string"},
                "from_branch": {"type": "string"},
            },
            required=["repo", "branch_name"],
        ),
        func=gh_tools.create_branch,
    )
    R(
        name="create_issue",
        description="ساخت Issue جدید در مخزن",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            required=["repo", "title"],
        ),
        func=gh_tools.create_issue,
    )
    R(
        name="list_issues",
        description="فهرست Issues مخزن (بدون PRها). state: open/closed/all",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "state": {"type": "string", "default": "open"},
                "limit": {"type": "integer", "default": 30},
            }
        ),
        func=gh_tools.list_issues,
    )
    R(
        name="get_issue",
        description="جزئیات کامل یک Issue با شمارهٔ آن",
        parameters=_s(
            {"repo": {"type": "string"}, "number": {"type": "integer"}},
            required=["repo", "number"],
        ),
        func=gh_tools.get_issue,
    )
    R(
        name="comment_on_issue",
        description="ثبت کامنت روی یک Issue",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "body": {"type": "string"},
            },
            required=["repo", "number", "body"],
        ),
        func=gh_tools.comment_on_issue,
    )
    R(
        name="update_issue_state",
        description="بستن یا بازگشایی Issue. state: open یا closed",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "state": {"type": "string", "default": "closed"},
            },
            required=["repo", "number"],
        ),
        func=gh_tools.update_issue_state,
    )
    R(
        name="create_pull_request",
        description="ساخت Pull Request از شاخهٔ head به شاخهٔ base",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "head": {"type": "string"},
                "base": {"type": "string"},
                "body": {"type": "string", "default": ""},
            },
            required=["repo", "title", "head", "base"],
        ),
        func=gh_tools.create_pull_request,
    )
    R(
        name="list_pull_requests",
        description="فهرست Pull Requestها. state: open/closed/all",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "state": {"type": "string", "default": "open"},
                "limit": {"type": "integer", "default": 30},
            }
        ),
        func=gh_tools.list_pull_requests,
    )
    R(
        name="get_pull_request_status",
        description="وضعیت ادغام و CI یک PR (mergeable + check runs)",
        parameters=_s(
            {"repo": {"type": "string"}, "number": {"type": "integer"}},
            required=["repo", "number"],
        ),
        func=gh_tools.get_pull_request_status,
    )
    R(
        name="comment_on_pr",
        description="ثبت کامنت روی یک Pull Request",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "body": {"type": "string"},
            },
            required=["repo", "number", "body"],
        ),
        func=gh_tools.comment_on_pr,
    )
    R(
        name="merge_pull_request",
        description="ادغام یک Pull Request. merge_method: merge/squash/rebase",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "merge_method": {"type": "string", "default": "merge"},
                "commit_title": {"type": "string"},
                "commit_message": {"type": "string"},
            },
            required=["repo", "number"],
        ),
        func=gh_tools.merge_pull_request,
    )
    R(
        name="list_commits",
        description="کامیت‌های اخیر مخزن روی یک شاخهٔ اختیاری",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "branch": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            }
        ),
        func=gh_tools.list_commits,
    )
    R(
        name="create_release",
        description="ساخت Release روی یک تگ در مخزن",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "tag": {"type": "string"},
                "name": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "target_commitish": {"type": "string"},
                "draft": {"type": "boolean", "default": False},
                "prerelease": {"type": "boolean", "default": False},
            },
            required=["repo", "tag", "name"],
        ),
        func=gh_tools.create_release,
    )
    R(
        name="list_workflows",
        description="فهرست GitHub Actions workflows مخزن",
        parameters=_s(
            {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 30}}
        ),
        func=gh_tools.list_workflows,
    )
    R(
        name="trigger_workflow",
        description="اجرای دستی یک workflow (workflow_dispatch) روی شاخهٔ ref؛ نیازمند اسکوپ workflow",
        parameters=_s(
            {
                "repo": {"type": "string"},
                "workflow": {"type": "string"},
                "ref": {"type": "string"},
                "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            required=["repo", "workflow", "ref"],
        ),
        func=gh_tools.trigger_workflow,
    )
    R(
        name="get_workflow_runs",
        description="آخرین اجراهای GitHub Actions مخزن",
        parameters=_s(
            {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 10}}
        ),
        func=gh_tools.get_workflow_runs,
    )
    R(
        name="fork_repo",
        description="فورک گرفتن از یک مخزن به حساب کاربر صاحب توکن",
        parameters=_s({"repo": {"type": "string"}}, required=["repo"]),
        func=gh_tools.fork_repo,
    )
    R(
        name="search_repositories",
        description="جستجوی سراسری مخازن گیت‌هاب با نحو جستجوی سایت",
        parameters=_s(
            {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            required=["query"],
        ),
        func=gh_tools.search_repositories,
    )
