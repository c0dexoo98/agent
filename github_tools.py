# -*- coding: utf-8 -*-
"""ابزارهای GitHub برای ایجنت — پیاده‌سازی با PyGithub نسخهٔ 2.x.

نکات امنیتی مهم (بخش توضیحات الزامی طبق مشخصات پروژه):

۱) توکن کلاسیک باید از صفحهٔ https://github.com/settings/tokens ساخته شود
   (Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)).
   حداقل اسکوپ‌های موردنیاز:
       - repo    → دسترسی کامل به مخازن عمومی/پرایوت (کد، Issues، PR، Releases)
       - workflow→ ساخت/ویرایش فایل‌های .github/workflows و اجرای Actions (در صورت نیاز)
   (برای ابزار delete_repo به‌تنهایی اسکوپ delete_repo لازم است که پیش‌فرض نیست.)

۲) توکن فقط در حافظهٔ همین اجرا نگه داشته می‌شود (آبجکت GitHubTools)؛ هرگز در
   فایل، لاگ یا کد ذخیره/چاپ نمی‌شود. ورود توکن با getpass انجام می‌شود تا در
   ترمینال هم نمایش داده نشود.

۳) توکن کلاسیک به‌طور پیش‌فرض به منابع «سازمان‌ها» هم دسترسی دارد (به‌جز
   سازمان‌هایی که SSO/محدودسازی توکن فعال کرده‌اند)؛ بنابراین باید با احتیاط
   ساخته و استفاده شود و در پایان کار حذف یا محدود شود.

۴) در PyGithub 2.x باید آبجکت Auth ساخته شود:
       auth = Auth.Token(token)
       gh = Github(auth=auth)
   (سبک قدیمی Github(token) در نسخهٔ 2.x منسوخ شده است.)

هر ابزار یک متد مستقل است که ورودی‌های مشخص دارد و خروجی را به‌صورت dict
(JSON-سازگار) برمی‌گرداند تا مدل بتواند از آن استفاده کند.
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

# PyGithub فقط برای ابزارهای گیت‌هاب لازم است؛ روی دستگاه‌هایی که نصب‌شدنش سخت است
# (مثلاً ترموکس اندروید — cryptography/pynacl ویل ندارند و از سورس با Rust build می‌شوند)
# بقیهٔ برنامه باید بدون آن هم بالا بیاید — باگ قابل‌حمل بودن پاس ۱۰
try:
    from github import Auth, Github, GithubException
    from github.Repository import Repository
    GITHUB_LIB = True
except ImportError:
    GITHUB_LIB = False
    Github = Auth = Repository = None

    class GithubException(Exception):
        """جایگزین امن برای except-clause ها وقتی کتابخانهٔ گیت‌هاب نصب نیست."""
        status: int = 0


class GitHubUnavailableError(Exception):
    """عملیات گیت‌هاب خواسته شده ولی PyGithub روی دستگاه نصب نیست."""


class TokenInvalidError(Exception):
    """وقتی توکن ۴۰۱ بدهد (نامعتبر یا باطل‌شده)."""


def _iso(dt: Any) -> Optional[str]:
    """تبدیل امن datetime به رشتهٔ ISO؛ None-سازگار."""
    try:
        return dt.isoformat() if dt else None
    except Exception:
        return None


def _sha(sha: Optional[str]) -> Optional[str]:
    return sha[:7] if sha else None


class GitHubTools:
    """نگهدارندهٔ کلاینت PyGithub و همهٔ عملیات گیت‌هاب به‌صورت ابزار.

    توکن تنها در self._token (حافظهٔ این آبجکت) زندگی می‌کند و از آن
    فقط برای ساخت کلاینت Github استفاده می‌شود.
    """

    def __init__(self, token: Optional[str]) -> None:
        self._token = token          # فقط در حافظه — هرگز serialize/لاگ نمی‌شود
        self._gh: Optional[Github] = None
        self._user: Any = None       # کاربر احرازهویت‌شده (پس از validate_token)
        self._login: Optional[str] = None
        self._scopes: Optional[str] = None  # مقدار هدر X-OAuth-Scopes از پاسخ /user

    # ------------------------------------------------------------------
    # هسته
    # ------------------------------------------------------------------

    @property
    def gh(self) -> Github:
        """ساخت تنبل کلاینت PyGithub 2.x با الگوی رسمی Auth.Token."""
        if not GITHUB_LIB:
            raise GitHubUnavailableError(
                "PyGithub نصب نیست — ابزارهای گیت‌هاب در دسترس نیستند. "
                "روی ترموکس: pkg install python-cryptography python-pynacl و بعد pip install PyGithub"
            )
        if self._gh is None:
            if not self._token:
                raise TokenInvalidError("توکنی در دسترس نیست — ابتدا validate_token اجرا شود")
            auth = Auth.Token(self._token)   # الگوی رسمی نسخهٔ 2.x
            self._gh = Github(auth=auth, base_url="https://api.github.com", per_page=30)
        return self._gh

    def validate_token(self) -> Dict[str, Any]:
        """اعتبارسنجی توکن با GET https://api.github.com/user.

        اگر نام کاربری برگردد، توکن معتبر است؛ اگر 401 بدهد TokenInvalidError
        پرتاب می‌شود تا agent.py پیام واضح بدهد و از کاربر بخواهد دوباره وارد کند.
        """
        try:
            # درخواست خام تا هدر X-OAuth-Scopes هم خوانده شود
            # (در PyGithub 2.x خروجی requestJsonAndCheck تاپل (headers, data) است)
            headers, data = self.gh.requester.requestJsonAndCheck("GET", "/user")
        except GithubException as e:
            if e.status in (401, 403):
                raise TokenInvalidError(
                    f"توکن نامعتبر است یا اسکوپ کافی ندارد (HTTP {e.status})"
                ) from e
            raise
        self._login = data.get("login")
        self._scopes = headers.get("x-oauth-scopes", "") or None
        self._user = self.gh.get_user()
        return {
            "login": self._login,
            "name": data.get("name"),
            "html_url": data.get("html_url"),
            "token_scopes": self._scopes,
        }

    @property
    def login(self) -> Optional[str]:
        return self._login

    def _repo(self, repo: str) -> Repository:
        """دریافت ریپو با «owner/name» یا فقط «name» (مالک = کاربر خودش)."""
        repo = str(repo).strip().strip("/")
        if "/" in repo:
            return self.gh.get_repo(repo)
        if not self._user:
            self._user = self.gh.get_user()
        return self._user.get_repo(repo)

    # ==================================================================
    # ابزارهای فقط‌خواندنی (Read-only)
    # ==================================================================

    def get_authenticated_user(self) -> Dict[str, Any]:
        """اطلاعات کاربرِ صاحب توکن + اسکوپ‌های توکن."""
        if not self._user:
            self.validate_token()
        return {
            "login": self._user.login,
            "name": self._user.name,
            "email": self._user.email,
            "bio": self._user.bio,
            "html_url": self._user.html_url,
            "public_repos": self._user.public_repos,
            "created_at": _iso(self._user.created_at),
            "token_scopes": self._scopes,
        }

    def get_rate_limit(self) -> Dict[str, Any]:
        """سهمیهٔ فعلی API گیت‌هاب (برای عیب‌یابی خطاهای 403 rate-limit).

        در PyGithub 2.x خروجی get_rate_limit ساختار RateLimitOverview دارد:
        هستهٔ REST در .resources.core قرار می‌گیرد (با سازگاری .rate).
        """
        overview = self.gh.get_rate_limit()
        core = getattr(overview, "resources", None).core if getattr(overview, "resources", None) else overview.rate
        return {
            "limit": core.limit,
            "remaining": core.remaining,
            "reset_utc": _iso(core.reset),
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
        repos_iter = self.gh.get_user().get_repos(affiliation=affiliation)
        items = []
        for r in itertools.islice(repos_iter, max(1, min(int(limit), 100))):
            items.append(
                {
                    "full_name": r.full_name,
                    "private": r.private,
                    "description": r.description,
                    "language": r.language,
                    "stars": r.stargazers_count,
                    "pushed_at": _iso(r.pushed_at),
                    "html_url": r.html_url,
                }
            )
        return {"count": len(items), "repos": items}

    def get_repo(self, repo: str) -> Dict[str, Any]:
        """متادیتای کامل یک مخزن («owner/name» یا «name» برای مخازن خود کاربر)."""
        r = self._repo(repo)
        license_name = None
        try:
            license_name = r.license.name if r.license else None
        except Exception:
            pass
        return {
            "full_name": r.full_name,
            "private": r.private,
            "description": r.description,
            "html_url": r.html_url,
            "default_branch": r.default_branch,
            "language": r.language,
            "size_kb": r.size,
            "stars": r.stargazers_count,
            "forks": r.forks_count,
            "open_issues": r.open_issues_count,
            "topics": list(r.get_topics() or []),
            "license": license_name,
            "created_at": _iso(r.created_at),
            "pushed_at": _iso(r.pushed_at),
        }

    def get_repo_status(self, repo: str) -> Dict[str, Any]:
        """وضعیت لحظه‌ای مخزن: شاخه‌ها، آخرین کامیت‌ها، تعداد Issues/PR باز و آخرین اجرای Actions."""
        r = self._repo(repo)
        branches = list(itertools.islice(r.get_branches(), 20))
        commits = []
        for c in itertools.islice(r.get_commits(), 5):
            commits.append(
                {
                    "sha": _sha(c.sha),
                    "message": (c.commit.message or "")[:120],
                    "author": (c.commit.author.name if c.commit.author else None),
                    "date": _iso(c.commit.author.date if c.commit.author else None),
                }
            )
        last_run = None
        try:
            for run in itertools.islice(r.get_workflow_runs(), 1):
                last_run = {
                    "name": run.name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "created_at": _iso(run.created_at),
                }
        except Exception:
            last_run = None
        return {
            "full_name": r.full_name,
            "default_branch": r.default_branch,
            "branch_count": len(branches),
            "branches": [b.name for b in branches],
            "recent_commits": commits,
            "open_issues": r.open_issues_count,
            "last_workflow_run": last_run,
            "pushed_at": _iso(r.pushed_at),
        }

    def list_branches(self, repo: str, limit: int = 50) -> Dict[str, Any]:
        """فهرست شاخه‌های مخزن."""
        r = self._repo(repo)
        items = []
        for b in itertools.islice(r.get_branches(), max(1, min(int(limit), 100))):
            items.append({"name": b.name, "sha": _sha(b.commit.sha), "protected": b.protected})
        return {"count": len(items), "branches": items}

    def get_file_contents(self, repo: str, path: str, ref: Optional[str] = None) -> Dict[str, Any]:
        """محتوای یک فایل (یا فهرست محتویات یک پوشه) از مخزن."""
        r = self._repo(repo)
        contents = r.get_contents(path, ref=ref) if ref else r.get_contents(path)
        if isinstance(contents, list):
            return {
                "type": "directory",
                "path": path,
                "entries": [
                    {"name": c.name, "type": c.type, "path": c.path, "size": c.size}
                    for c in contents
                ],
            }
        decoded = ""
        try:
            decoded = (contents.decoded_content or b"").decode("utf-8", errors="replace")
        except Exception as e:
            decoded = f"[محتوای دودویی یا ناخوانا: {e}]"
        return {
            "type": "file",
            "path": contents.path,
            "size": contents.size,
            "encoding": contents.encoding,
            "content": decoded,
        }

    def list_repo_files(
        self,
        repo: str,
        path_prefix: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """فهرست همهٔ فایل‌های مخزن از روی git tree (فیلتر پیشوند مسیر اختیاری)."""
        r = self._repo(repo)
        tree = r.get_git_tree(r.default_branch, recursive=True)
        prefix = (path_prefix or "").strip().strip("/")
        paths = [
            el.path
            for el in tree.tree
            if el.type == "blob" and (not prefix or el.path.startswith(prefix))
        ]
        truncated = len(paths) > int(limit)
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
        r = self._repo(repo)
        items = []
        for i in itertools.islice(r.get_issues(state=state), max(1, min(int(limit), 100))):
            if getattr(i, "pull_request", None):
                continue  # PRها در ابزار جداگانهٔ list_pull_requests هستند
            items.append(
                {
                    "number": i.number,
                    "title": i.title,
                    "state": i.state,
                    "user": i.user.login if i.user else None,
                    "labels": [lb.name for lb in i.labels],
                    "comments": i.comments,
                    "html_url": i.html_url,
                    "created_at": _iso(i.created_at),
                }
            )
        return {"count": len(items), "issues": items}

    def get_issue(self, repo: str, number: int) -> Dict[str, Any]:
        """جزئیات کامل یک Issue."""
        i = self._repo(repo).get_issue(int(number))
        return {
            "number": i.number,
            "title": i.title,
            "state": i.state,
            "author": i.user.login if i.user else None,
            "body": (i.body or "")[:4000],
            "labels": [lb.name for lb in i.labels],
            "assignees": [a.login for a in i.assignees],
            "comments": i.comments,
            "html_url": i.html_url,
            "created_at": _iso(i.created_at),
            "closed_at": _iso(i.closed_at),
        }

    def list_pull_requests(
        self,
        repo: str,
        state: str = "open",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """فهرست Pull Requestها. state: open/closed/all."""
        r = self._repo(repo)
        items = []
        for pr in itertools.islice(r.get_pulls(state=state), max(1, min(int(limit), 100))):
            items.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "draft": pr.draft,
                    "user": pr.user.login if pr.user else None,
                    "head": pr.head.ref,
                    "base": pr.base.ref,
                    "html_url": pr.html_url,
                    "created_at": _iso(pr.created_at),
                }
            )
        return {"count": len(items), "pull_requests": items}

    def get_pull_request_status(self, repo: str, number: int) -> Dict[str, Any]:
        """وضعیت ادغام و CI یک Pull Request (mergeable + نتیجهٔ check runs)."""
        r = self._repo(repo)
        pr = r.get_pull(int(number))
        checks = []
        try:
            commit = r.get_commit(pr.head.sha)
            for cr in itertools.islice(commit.get_check_runs(), 20):
                checks.append({"name": cr.name, "status": cr.status, "conclusion": cr.conclusion})
            combined = commit.get_combined_status().state
        except Exception as e:
            combined = f"unavailable: {e}"
        return {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "merged": pr.merged,
            "mergeable": pr.mergeable,
            "mergeable_state": pr.mergeable_state,
            "head_sha": _sha(pr.head.sha),
            "combined_status": combined,
            "check_runs": checks,
            "html_url": pr.html_url,
        }

    def list_commits(self, repo: str, branch: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
        """کامیت‌های اخیر مخزن (روی یک شاخهٔ اختیاری)."""
        r = self._repo(repo)
        it = r.get_commits(sha=branch) if branch else r.get_commits()
        items = []
        for c in itertools.islice(it, max(1, min(int(limit), 100))):
            items.append(
                {
                    "sha": _sha(c.sha),
                    "message": (c.commit.message or "")[:200],
                    "author": c.commit.author.name if c.commit.author else None,
                    "date": _iso(c.commit.author.date if c.commit.author else None),
                    "html_url": c.html_url,
                }
            )
        return {"count": len(items), "commits": items}

    def list_workflows(self, repo: str, limit: int = 30) -> Dict[str, Any]:
        """فهرست GitHub Actions workflows مخزن."""
        r = self._repo(repo)
        items = []
        for w in itertools.islice(r.get_workflows(), max(1, min(int(limit), 100))):
            items.append({"id": w.id, "name": w.name, "path": w.path, "state": w.state})
        return {"count": len(items), "workflows": items}

    def get_workflow_runs(self, repo: str, limit: int = 10) -> Dict[str, Any]:
        """آخرین اجراهای GitHub Actions مخزن."""
        r = self._repo(repo)
        items = []
        for run in itertools.islice(r.get_workflow_runs(), max(1, min(int(limit), 50))):
            items.append(
                {
                    "id": run.id,
                    "name": run.name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "event": run.event,
                    "branch": run.head_branch,
                    "head_sha": _sha(run.head_sha),
                    "run_number": run.run_number,
                    "html_url": run.html_url,
                    "created_at": _iso(run.created_at),
                }
            )
        return {"count": len(items), "workflow_runs": items}

    def search_repositories(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """جستجوی سراسری مخازن در گیت‌هاب (همان نحو جستجوی سایت)."""
        items = []
        for r in itertools.islice(self.gh.search_repositories(query=query), max(1, min(int(limit), 50))):
            items.append(
                {
                    "full_name": r.full_name,
                    "description": r.description,
                    "stars": r.stargazers_count,
                    "html_url": r.html_url,
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
        u = self._user or self.gh.get_user()
        r = u.create_repo(name=name, description=description or None, private=private, auto_init=auto_init)
        return {"full_name": r.full_name, "private": r.private, "html_url": r.html_url}

    def delete_repo(self, repo: str, confirm: bool = False) -> Dict[str, Any]:
        """حذف مخزن (غیرقابل‌بازگشت!) — نیازمند پارامتر confirm=True و اسکوپ delete_repo."""
        if confirm is not True:
            return {
                "ok": False,
                "error": "برای حذف مخزن باید confirm=true ارسال شود. این عملیات غیرقابل‌بازگشت است.",
            }
        r = self._repo(repo)
        full_name = r.full_name
        r.delete()
        return {"deleted": full_name}

    def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ساخت یا به‌روزرسانی یک فایل در مخزن با یک کامیت."""
        r = self._repo(repo)
        kwargs: Dict[str, Any] = {}
        if branch:
            kwargs["branch"] = branch
        res = r.create_file(path=path, message=message, content=content.encode("utf-8"), **kwargs)
        commit = res.get("commit") if isinstance(res, dict) else None
        return {
            "path": path,
            "branch": branch or r.default_branch,
            "commit_sha": _sha(commit.sha) if commit else None,
            "html_url": r.html_url + f"/blob/{branch or r.default_branch}/{path}",
        }

    def create_branch(self, repo: str, branch_name: str, from_branch: Optional[str] = None) -> Dict[str, Any]:
        """ساخت شاخهٔ جدید از روی یک شاخهٔ مبنا (پیش‌فرض: شاخهٔ پیش‌فرض)."""
        r = self._repo(repo)
        base = from_branch or r.default_branch
        sha = r.get_branch(base).commit.sha
        r.create_git_ref(ref=f"refs/heads/{branch_name}", sha=sha)
        return {"created": branch_name, "from": base, "base_sha": _sha(sha)}

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """ساخت Issue جدید در مخزن."""
        r = self._repo(repo)
        i = r.create_issue(title=title, body=body or None, labels=labels or [])
        return {"number": i.number, "title": i.title, "html_url": i.html_url}

    def comment_on_issue(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        """ثبت کامنت روی یک Issue (کامنت PRها هم از همین مسیر است)."""
        i = self._repo(repo).get_issue(int(number))
        c = i.create_comment(body)
        return {"issue": i.number, "comment_html_url": c.html_url}

    def update_issue_state(self, repo: str, number: int, state: str = "closed") -> Dict[str, Any]:
        """بستن یا بازگشایی یک Issue. state: open یا closed."""
        if state not in ("open", "closed"):
            return {"ok": False, "error": "state باید 'open' یا 'closed' باشد"}
        i = self._repo(repo).get_issue(int(number))
        i.edit(state=state)
        return {"number": i.number, "state": i.state}

    def create_pull_request(
        self,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> Dict[str, Any]:
        """ساخت Pull Request از شاخهٔ head به شاخهٔ base."""
        r = self._repo(repo)
        pr = r.create_pull(title=title, body=body or None, head=head, base=base)
        return {"number": pr.number, "title": pr.title, "html_url": pr.html_url}

    def comment_on_pr(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        """ثبت کامنت روی یک Pull Request (در API گیت‌هاب کامنت PR = کامنت Issue متناظر)."""
        r = self._repo(repo)
        pr = r.get_pull(int(number))
        i = r.get_issue(pr.number)
        c = i.create_comment(body)
        return {"pr": pr.number, "comment_html_url": c.html_url}

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
        pr = self._repo(repo).get_pull(int(number))
        kwargs: Dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            kwargs["commit_title"] = commit_title
        if commit_message:
            kwargs["commit_message"] = commit_message
        result = pr.merge(**kwargs)
        merged = result.merged if hasattr(result, "merged") else bool(result)
        return {"number": pr.number, "merged": merged}

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
        """ساخت Release روی یک تگ (در PyGithub 2.x: create_git_release با پارامتر message)."""
        r = self._repo(repo)
        kwargs: Dict[str, Any] = {"name": name, "message": body, "draft": draft, "prerelease": prerelease}
        if target_commitish:
            kwargs["target_commitish"] = target_commitish
        rel = r.create_git_release(tag=tag, **kwargs)
        return {"tag": rel.tag_name, "name": rel.name, "html_url": rel.html_url}

    def trigger_workflow(
        self,
        repo: str,
        workflow: str,
        ref: str,
        inputs: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """اجرای دستی یک workflow با repository_dispatch/create_dispatch.

        workflow نام فایل (مثل ci.yml) یا شناسهٔ عددی آن است؛ ref شاخهٔ هدف.
        برای ساخت/اجرای workflow توکن باید اسکوپ workflow داشته باشد.
        """
        r = self._repo(repo)
        wf = r.get_workflow(workflow)
        ok = wf.create_dispatch(ref=ref, inputs=inputs or {})
        return {"workflow": workflow, "ref": ref, "dispatched": bool(ok)}

    def fork_repo(self, repo: str) -> Dict[str, Any]:
        """فورک گرفتن از یک مخزن به حساب کاربر صاحب توکن."""
        r = self._repo(repo)
        # در PyGithub 2.x خروجی requestJsonAndCheck تاپل (headers, data) است
        _headers, data = self.gh.requester.requestJsonAndCheck("POST", f"{r.url}/forks")
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
