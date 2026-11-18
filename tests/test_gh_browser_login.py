# -*- coding: utf-8 -*-
"""تست ورود با مرورگر (Device Flow) + کلاینت REST خالص گیت‌هاب.

سرور جعلی گیت‌هاب (oauth + api) می‌سازیم و چک می‌کنیم:
  ۱) Device Flow: start → کد؛ poll → pending → authorized → توکن
  ۲) GitHubTools روی REST خالص: validate_token + چرخهٔ کامل ابزارها
     (فایل، Issues، PR، merge، Actions، Release، فورک، جستجو، …)
  ۳) سرور DevX: github_login_start/poll → نصب ابزارها + /api/token با gho_
تست‌ها همه روی سرور جعلی لوکال‌اند — بدون شبکهٔ واقعی.
"""
import base64
import json
import sys
import threading
import time
import urllib.error  # noqa: F401 — HTTPError برای چک 404 مسیر حذف‌شده
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

TOKEN = "gho_test_token"
USER = {"login": "octocat", "name": "The Octocat", "html_url": "https://github.com/octocat",
        "email": None, "bio": "hi", "public_repos": 2, "created_at": "2020-01-01T00:00:00Z"}
REPO = {"full_name": "octocat/hello", "private": False, "description": "demo",
        "html_url": "https://github.com/octocat/hello", "default_branch": "main",
        "language": "Python", "size": 12, "stargazers_count": 3, "forks_count": 1,
        "open_issues_count": 2, "topics": ["web"], "license": None,
        "created_at": "2026-01-01T00:00:00Z", "pushed_at": "2026-11-01T00:00:00Z"}

state = {"polls": 0, "files": {}, "puts": []}


class _FakeGH(BaseHTTPRequestHandler):
    def log_message(self, *a):  # بی‌صدا
        pass

    def _json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _body(self) -> dict:
        """بدنهٔ درخواست: JSON برای API و فرم‌encoded برای oauth — هر دو پذیرفته."""
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            from urllib.parse import parse_qs
            return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):  # noqa: N802
        if not self._authed():
            return self._json({"message": "Bad credentials"}, status=401)
        p = self.path.split("?")[0]
        if p == "/user":
            return self._json(USER, headers={"x-oauth-scopes": "repo, workflow, read:org, gist"})
        if p == "/rate_limit":
            return self._json({"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": "2030-01-01T00:00:00Z"}}})
        if p == "/user/repos":
            return self._json([REPO])
        if p == "/repos/octocat/hello":
            return self._json(REPO)
        if p == "/repos/octocat/hello/branches/main":
            return self._json({"name": "main", "commit": {"sha": "base123full"}, "protected": False})
        if p == "/repos/octocat/hello/branches":
            return self._json([{"name": "main", "commit": {"sha": "base123full"}, "protected": False}])
        if p.startswith("/repos/octocat/hello/contents/"):
            path = p[len("/repos/octocat/hello/contents/"):]
            if path in state["files"]:
                content, sha = state["files"][path]
                return self._json({"name": path.rsplit("/", 1)[-1], "path": path, "size": len(content),
                                   "encoding": "base64", "content": base64.b64encode(content.encode()).decode(),
                                   "sha": sha, "type": "file"})
            if path == "README.md" and path not in state["files"]:
                content = "سلام گیت‌هاب"
                return self._json({"name": "README.md", "path": path, "size": len(content),
                                   "encoding": "base64", "content": base64.b64encode(content.encode()).decode(),
                                   "sha": "f1full", "type": "file"})
            if path == "src":
                return self._json([{"name": "main.py", "type": "file", "path": "src/main.py", "size": 10}])
            return self._json({"message": "Not Found"}, status=404)
        if p == "/repos/octocat/hello/git/trees/main":
            return self._json({"tree": [{"type": "blob", "path": "README.md"}, {"type": "blob", "path": "src/main.py"}]})
        if p == "/repos/octocat/hello/issues":
            return self._json([
                {"number": 1, "title": "PR اشیو", "state": "open", "user": {"login": "u"},
                 "labels": [], "comments": 0, "html_url": "u1", "created_at": "2026-01-01T00:00:00Z",
                 "pull_request": {"url": "x"}},
                {"number": 2, "title": "باگ واقعی", "state": "open", "user": {"login": "v"},
                 "labels": [{"name": "bug"}], "comments": 1, "html_url": "u2",
                 "created_at": "2026-01-02T00:00:00Z", "closed_at": None},
            ])
        if p == "/repos/octocat/hello/issues/2":
            return self._json({"number": 2, "title": "باگ واقعی", "state": "open", "user": {"login": "v"},
                               "body": "جزئیات", "labels": [{"name": "bug"}], "assignees": [{"login": "me"}],
                               "comments": 1, "html_url": "u2", "created_at": "2026-01-02T00:00:00Z", "closed_at": None})
        if p == "/repos/octocat/hello/pulls":
            return self._json([{
                "number": 1, "title": "PR1", "state": "open", "draft": False, "user": {"login": "u"},
                "head": {"ref": "feat", "sha": "headsha123full"}, "base": {"ref": "main"},
                "html_url": "pr1", "created_at": "2026-02-01T00:00:00Z",
                "merged": False, "mergeable": True, "mergeable_state": "clean"}])
        if p == "/repos/octocat/hello/pulls/1":
            return self._json({
                "number": 1, "title": "PR1", "state": "open", "user": {"login": "u"},
                "head": {"ref": "feat", "sha": "headsha123full"}, "base": {"ref": "main"},
                "html_url": "pr1", "created_at": "2026-02-01T00:00:00Z",
                "merged": False, "mergeable": True, "mergeable_state": "clean"})
        if p == "/repos/octocat/hello/commits":
            return self._json([{"sha": "c1fullsha", "html_url": "c1",
                                "commit": {"message": "کامیت اول", "author": {"name": "Ali", "date": "2026-03-01T00:00:00Z"}}}])
        if p.endswith("/check-runs"):
            return self._json({"total_count": 1, "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]})
        if p.endswith("/status"):
            return self._json({"state": "success"})
        if p == "/repos/octocat/hello/actions/workflows":
            return self._json({"workflows": [{"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]})
        if p == "/repos/octocat/hello/actions/runs":
            return self._json({"workflow_runs": [{"id": 9, "name": "CI", "status": "completed", "conclusion": "success",
                                                  "event": "push", "head_branch": "main", "head_sha": "c1fullsha",
                                                  "run_number": 4, "html_url": "run9", "created_at": "2026-03-02T00:00:00Z"}]})
        if p == "/search/repositories":
            return self._json({"items": [{"full_name": "x/y", "description": "d", "stargazers_count": 5, "html_url": "xy"}]})
        return self._json({"message": "Not Found"}, status=404)

    def do_POST(self):  # noqa: N802
        body = self._body()
        p = self.path.split("?")[0]
        if p == "/login/device/code":
            state["polls"] = 0
            return self._json({"device_code": "dev1", "user_code": "ABCD-1234",
                               "verification_uri": "http://127.0.0.1:1/login/device",
                               "expires_in": 900, "interval": 1})
        if p == "/login/oauth/access_token":
            if body.get("device_code") != "dev1":
                return self._json({"error": "incorrect_device_code",
                                   "error_description": "device_code ناشناخته"})
            state["polls"] += 1
            if state["polls"] < 3:
                return self._json({"error": "authorization_pending"})
            return self._json({"access_token": TOKEN, "token_type": "bearer", "scope": "repo, workflow, read:org, gist"})
        if not self._authed():
            return self._json({"message": "Bad credentials"}, status=401)
        if p == "/repos/octocat/hello/issues":
            return self._json({"number": 3, "title": body.get("title"), "html_url": "i3"})
        if p.startswith("/repos/octocat/hello/issues/") and p.endswith("/comments"):
            return self._json({"html_url": "comment-url"})
        if p == "/repos/octocat/hello/git/refs":
            return self._json({"ref": body.get("ref")}, status=201)
        if p == "/repos/octocat/hello/pulls":
            return self._json({"number": 4, "title": body.get("title"), "html_url": "pr4"})
        if p == "/repos/octocat/hello/pulls/1/merge":
            return self._json({"merged": True})
        if p.endswith("/dispatches"):
            return self._json({}, status=204)
        if p == "/repos/octocat/hello/releases":
            return self._json({"tag_name": body.get("tag_name"), "name": body.get("name"), "html_url": "rel"})
        if p == "/repos/octocat/hello/forks":
            return self._json({"full_name": "octocat/hello-fork", "html_url": "fork"})
        if p == "/user/repos":
            return self._json({"full_name": "octocat/newrepo", "private": body.get("private"), "html_url": "nr"})
        return self._json({"message": "Not Found"}, status=404)

    def do_PUT(self):  # noqa: N802
        body = self._body()
        if not self._authed():
            return self._json({"message": "Bad credentials"}, status=401)
        p = self.path.split("?")[0]
        if p.startswith("/repos/octocat/hello/pulls/1/merge"):
            return self._json({"merged": True})
        if p.startswith("/repos/octocat/hello/contents/"):
            path = p[len("/repos/octocat/hello/contents/"):]
            state["puts"].append(body)
            content = base64.b64decode(body.get("content") or "").decode("utf-8")
            state["files"][path] = (content, "newsha1full")
            return self._json({"commit": {"sha": "commit456full"}})
        return self._json({"message": "Not Found"}, status=404)

    def do_PATCH(self):  # noqa: N802
        body = self._body()
        if not self._authed():
            return self._json({"message": "Bad credentials"}, status=401)
        if self.path.startswith("/repos/octocat/hello/issues/"):
            return self._json({"number": 2, "state": body.get("state")})
        return self._json({"message": "Not Found"}, status=404)

    def do_DELETE(self):  # noqa: N802
        if not self._authed():
            return self._json({"message": "Bad credentials"}, status=401)
        if self.path == "/repos/octocat/hello":
            return self._json({}, status=204)
        return self._json({"message": "Not Found"}, status=404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGH)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# ─── ۱) Device Flow ────────────────────────────────────────────────────────
from gh_oauth import DeviceFlow, DeviceFlowError  # noqa: E402

flow = DeviceFlow(oauth_base=BASE, timeout=10)
info = flow.start()
assert info["user_code"] == "ABCD-1234" and info["verification_uri"].endswith("/login/device"), info
assert flow.poll()["status"] == "pending", "اول باید pending باشد"
flow.interval = 0.05   # تسریع تست
token = flow.wait(sleep_fn=lambda s: time.sleep(min(s, 0.05)), max_s=30)
assert token == TOKEN, token
print("✅ ۱) Device Flow: کد → pending → تأیید مرورگر → توکن")

try:
    bad = DeviceFlow(oauth_base=BASE)
    bad.start()
    bad.device_code = "wrong"
    res = bad.poll()
    assert res["status"] == "error" and "خطا" in res["error"], res
    print("✅ ۲) Device Flow با device_code غلط → خطای ساخت‌یافته")
except DeviceFlowError as e:
    raise SystemExit(f"poll نباید exception می‌داد، dict ساخت‌یافته: {e}")

# ─── ۳) GitHubTools روی REST خالص ─────────────────────────────────────────
from github_tools import GitHubTools, TokenInvalidError  # noqa: E402

t = GitHubTools(TOKEN, api_base=BASE)
v = t.validate_token()
assert v["login"] == "octocat" and "repo" in (v["token_scopes"] or ""), v
assert t.get_authenticated_user()["public_repos"] == 2
rl = t.get_rate_limit()
assert rl["remaining"] == 4999 and rl["limit"] == 5000
print("✅ ۳) validate_token + کاربر + rate limit (بدون PyGithub)")

repo = t.get_repo("octocat/hello")
assert repo["full_name"] == "octocat/hello" and repo["topics"] == ["web"], repo
assert t.get_repo("hello")["full_name"] == "octocat/hello", "نام کوتاه با مالک کامل نشد"
br = t.list_branches("octocat/hello")
assert br["count"] == 1 and br["branches"][0]["sha"] == "base123", br
fc = t.get_file_contents("octocat/hello", "README.md")
assert fc["type"] == "file" and fc["content"] == "سلام گیت‌هاب", fc
d = t.get_file_contents("octocat/hello", "src")
assert d["type"] == "directory" and d["entries"][0]["path"] == "src/main.py", d
files = t.list_repo_files("octocat/hello")
assert files["files"] == ["README.md", "src/main.py"], files
print("✅ ۴) repo/branches/contents(dir+file)/tree")

res = t.create_or_update_file("octocat/hello", "docs/new.md", "محتوای جدید", "add docs")
assert res["commit_sha"] == "commit4" and res["html_url"].endswith("/docs/new.md"), res
upd = t.create_or_update_file("octocat/hello", "README.md", "نسخهٔ ۲", "update")
assert upd["commit_sha"] == "commit4"
assert state["puts"][-1].get("sha") == "f1full", "sha فایل موجود برای update گرفته نشد"
assert state["puts"][0].get("sha") is None, "فایل جدید نباید sha می‌گرفت"
print("✅ ۵) create/update فایل با تشخیص درست sha")

iss = t.list_issues("octocat/hello")
assert iss["count"] == 1 and iss["issues"][0]["number"] == 2, "فیلتر PR خراب است"
gi = t.get_issue("octocat/hello", 2)
assert gi["author"] == "v" and gi["labels"] == ["bug"], gi
ci = t.create_issue("octocat/hello", "تسک جدید", body="بدنه")
assert ci["number"] == 3
cm = t.comment_on_issue("octocat/hello", 2, "بررسی شد")
assert cm["comment_html_url"] == "comment-url"
st = t.update_issue_state("octocat/hello", 2, "closed")
assert st["state"] == "closed"
bad_state = t.update_issue_state("octocat/hello", 2, "weird")
assert bad_state.get("ok") is False
print("✅ ۶) Issues: فهرست با فیلتر PR + ساخت + کامنت + تغییر وضعیت")

prs = t.list_pull_requests("octocat/hello")
assert prs["count"] == 1 and prs["pull_requests"][0]["head"] == "feat", prs
ps = t.get_pull_request_status("octocat/hello", 1)
assert ps["head_sha"] == "headsha" and ps["combined_status"] == "success", ps
assert ps["check_runs"][0]["conclusion"] == "success", ps
mg = t.merge_pull_request("octocat/hello", 1, merge_method="squash")
assert mg["merged"] is True
bad_mg = t.merge_pull_request("octocat/hello", 1, merge_method="weird")
assert bad_mg.get("ok") is False
print("✅ ۷) PR: فهرست + وضعیت CI + ادغام + اعتبارسنجی روش ادغام")

cms = t.list_commits("octocat/hello")
assert cms["commits"][0]["author"] == "Ali" and cms["commits"][0]["sha"] == "c1fulls", cms
wfs = t.list_workflows("octocat/hello")
assert wfs["workflows"][0]["path"] == ".github/workflows/ci.yml"
runs = t.get_workflow_runs("octocat/hello")
assert runs["workflow_runs"][0]["run_number"] == 4
tw = t.trigger_workflow("octocat/hello", "ci.yml", ref="main")
assert tw["dispatched"] is True
print("✅ ۸) کامیت‌ها + Actions (فهرست/اجرا/dispatch)")

sr = t.search_repositories("devx")
assert sr["repositories"][0]["full_name"] == "x/y"
nr = t.create_repo("newrepo", description="د", private=True)
assert nr["full_name"] == "octocat/newrepo"
fk = t.fork_repo("octocat/hello")
assert fk["full_name"] == "octocat/hello-fork"
rel = t.create_release("octocat/hello", tag="v1.0", name="انتشار")
assert rel["tag"] == "v1.0"
cb = t.create_branch("octocat/hello", "feat-x")
assert cb["base_sha"] == "base123"
assert t.delete_repo("octocat/hello").get("ok") is False, "بدون confirm باید رد شود"
try:
    GitHubTools("gho_wrong", api_base=BASE).validate_token()
    raise SystemExit("توکن غلط باید ۴۰۱ می‌داد")
except TokenInvalidError:
    pass
print("✅ ۹) search/create/fork/release/branch + گیت confirm + 401")

# ─── ۱۰) سرور DevX: ورود با مرورگر از طریق UI ─────────────────────────────
import server as srvmod  # noqa: E402

srvmod.config.GH_API_BASE = BASE
srvmod.config.GH_OAUTH_BASE = BASE
srvmod.STATE.engine.brain = None
s = srvmod.serve(open_browser=False)
threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.4)

start = srvmod.STATE.github_login_start()
assert start["ok"] and start["user_code"] == "ABCD-1234", start
p1 = srvmod.STATE.github_login_poll()
assert p1["status"] == "pending", p1
p2 = srvmod.STATE.github_login_poll()
assert p2["ok"] and p2["status"] == "pending", p2
p3 = srvmod.STATE.github_login_poll()
assert p3["ok"] and p3["status"] == "authorized" and p3["login"] == "octocat", p3
assert srvmod.STATE.gh_tools.login == "octocat"
# ابزار نصب‌شده (harden‌شده) واقعاً کار می‌کند
assert srvmod.STATE.gh_tools.get_rate_limit()["remaining"] == 4999

# ورود توکنی حذف شده — /api/token باید 404 بدهد (تنها مسیر ورود: Device Flow)
req404 = urllib.request.Request(
    "http://127.0.0.1:8770/api/token",
    data=json.dumps({"token": TOKEN}).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req404, timeout=15)
    raise SystemExit("/api/token باید 404 می‌داد")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code

# poll بدون جلسهٔ فعال → خطای ساخت‌یافته
none_flow = srvmod.STATE.github_login_poll()
assert none_flow.get("ok") is False and none_flow.get("status") == "none", none_flow
print("✅ ۱۰) github_login_start/poll → نصب ابزارها + /api/token حذف‌شده (404) + poll بدون جلسه")

# ─── ۱۱) تاب‌آوری جریان: خطای گذرای شبکه جریان را نمی‌کشد ───────────────────
# سناریوی واقعی موبایل: کاربر در تب گیت‌هاب است، شبکه/سرور لحظه‌ای می‌پرد؛
# device_code در گیت‌هاب معتبر مانده و پول بعدی باید جریان را ادامه بدهد.
from gh_oauth import DeviceFlow as _DF  # noqa: E402

dead = _DF(oauth_base="http://127.0.0.1:9", timeout=2)  # پورت ۹ → رد فوری اتصال
dead.device_code, dead.user_code, dead.verification_uri, dead.interval = "x", "X-1", "http://x", 5
srvmod.STATE._gh_flow = dead
r = srvmod.STATE.github_login_poll()
assert r.get("ok") is False and r.get("status") == "network_error", r
assert srvmod.STATE._gh_flow is dead, "خطای گذرا نباید جریان ورود را بکشد"
assert r.get("interval", 0) >= 5, r
srvmod.STATE._gh_flow = None
print("✅ ۱۱) خطای گذرای شبکه → network_error و جریان زنده می‌ماند")

# ─── ۱۲) پاسخ start/poll دارای interval برای ضرب‌آهنگ صحیح UI ───────────────
start2 = srvmod.STATE.github_login_start()
assert start2["ok"] and start2["user_code"] == "ABCD-1234", start2
assert start2.get("interval", 0) >= 1, f"start باید interval گیت‌هاب را بدهد: {start2}"
p = srvmod.STATE.github_login_poll()
assert p["ok"] and p["status"] == "pending" and p.get("interval", 0) >= 1, p
p2 = srvmod.STATE.github_login_poll()
assert p2["ok"] and p2["status"] in ("pending", "authorized"), p2
srvmod.STATE._gh_flow = None
print("✅ ۱۲) start/poll دارای interval (UI با ضرب‌آهنگ مجاز گیت‌هاب پول می‌زند)")

# ─── ۱۳) سشن ماندگار: ذخیره → خروج → بازیابی خودکار بعد از ری‌استارت ────────
import os as _os  # noqa: E402

sess = srvmod.config.GH_SESSION_FILE
assert sess.exists(), "بعد از ورود موفق، فایل سشن باید ذخیره شود"
saved = json.loads(sess.read_text(encoding="utf-8"))
assert saved.get("token") == TOKEN and saved.get("login") == "octocat", saved
assert (_os.stat(sess).st_mode & 0o777) == 0o600, "فایل سشن باید دسترسی ۶۰۰ داشته باشد"
print("✅ ۱۳-الف) سشن با دسترسی ۶۰۰ در فایل محلی ذخیره شد")

# خروج: توکن از حافظه + فایل پاک؛ snapshotهم قطع را نشان بدهد
out = srvmod.STATE.github_logout()
assert out["ok"] and not sess.exists(), out
assert srvmod.STATE.gh_tools is None and srvmod.STATE.token_info is None
assert srvmod.STATE.snapshot()["token"]["set"] is False
print("✅ ۱۳-ب) خروج: حافظه + فایل سشن کامل پاک شد")

# بازیابی (مثل ری‌استارت سرور): فایل سشن معتبر → لاگین خودکار بدون ورود دوباره
sess.write_text(json.dumps({"token": TOKEN, "login": "octocat"}), encoding="utf-8")
srvmod.STATE.restore_github_session()
assert srvmod.STATE.token_info and srvmod.STATE.token_info["login"] == "octocat"
assert srvmod.STATE.gh_tools.get_rate_limit()["remaining"] == 4999, "ابزارهای بازیابی‌شده باید کار کنند"
print("✅ ۱۳-ج) بازیابی خودکار از فایل سشن — بدون ورود دوباره")

# توکن منقضی‌شده (۴۰۱): فایل سشن بی‌فایده است → پاک شود و بیرون بمانیم
srvmod.STATE.github_logout()
sess.write_text(json.dumps({"token": "gho_wrong"}), encoding="utf-8")
srvmod.STATE.restore_github_session()
assert srvmod.STATE.token_info is None and not sess.exists(), "توکن باطل باید فایل را پاک کند"
print("✅ ۱۳-د) توکن منقضی (401) → فایل سشن پاک شد و لاگین باقی نماند")

s.shutdown()
s.server_close()
srv.shutdown()
print("\nBROWSER LOGIN (DEVICE FLOW) TESTS PASSED ✅")
