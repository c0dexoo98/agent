# -*- coding: utf-8 -*-
"""شبیه‌سازی ترموکس: playwright و PyGithub و python-dotenv در دسترس نیستند.

گوشی واقعی: pip install PyGithub شکست می‌خورد چون cryptography/pynacl برای
پایتون اندروید ویل ندارند و build از سورس Rust می‌خواهد — نتیجه: هیچ‌کدام از
پکیج‌ها نصب نمی‌شوند. این تست دقیقاً همان حالت را با مسدودساز import می‌سازد:
سرور باید کامل بیاید، UI سرو شود، نصب گیت‌هاب (Device Flow، کلاینت REST خالص)
کار کند و /api/token حذف‌شده 404 بدهد.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["TERMUX"] = "1"   # شبیه‌سازی محیط ترموکس برای پیام خطای صادقانه

_BLOCKED = ("playwright", "github", "nacl", "dotenv")


class _NoTermuxPackages:
    """مسدودساز import — مثل دستگاهی که هیچ‌کدام از این پکیج‌ها را ندارد."""

    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in _BLOCKED):
            raise ImportError(f"No module named {name!r} (Termux simulation)")
        return None


sys.meta_path.insert(0, _NoTermuxPackages())
for m in list(sys.modules):
    if any(m == b or m.startswith(b + ".") for b in _BLOCKED):
        del sys.modules[m]

import deepseek_client as dc  # noqa: E402
import github_tools as gt  # noqa: E402

assert dc.PLAYWRIGHT_AVAILABLE is False, "پرچم playwright فعال نشد"
assert gt.GITHUB_LIB is False, "پرچم PyGithub فعال نشد"
from deepseek_client import DeepSeekClient, DeepSeekError  # noqa: E402

c = DeepSeekClient(user_data_dir=Path("/tmp/nope"))
try:
    c.start()
    raise SystemExit("start() باید خطای واضح می‌داد")
except DeepSeekError as e:
    assert "playwright نصب نیست" in str(e) and "pkg install chromium nodejs-lts" in str(e), str(e)
print("ok: بدون playwright — روی ترموکس پیام صادقانه + راهنمای نصب واقعی می‌دهد")

# ─── محیط درایور پلی‌رایت روی ترموکس (فیکس «Unsupported platform: android») ──
# نود ۲۳+ روی اندروید process.platform را «android» می‌دهد و درایور پلی‌رایت
# در computeDefaultCacheDirectory فقط linux/darwin/win32 می‌شناسد → موقع بالا
# آمدن می‌میرد («connection closed while reading from the driver»).
_PW_ENV_KEYS = ("PLAYWRIGHT_NODEJS_PATH", "PLAYWRIGHT_BROWSERS_PATH", "DISPLAY", "WAYLAND_DISPLAY")
_saved_env = {k: os.environ.get(k) for k in _PW_ENV_KEYS}
for k in ("PLAYWRIGHT_NODEJS_PATH", "PLAYWRIGHT_BROWSERS_PATH", "DISPLAY", "WAYLAND_DISPLAY"):
    os.environ.pop(k, None)
try:
    dc._ensure_pw_env()
    node_p = os.environ.get("PLAYWRIGHT_NODEJS_PATH", "")
    bw_p = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    assert Path(node_p).name in ("node", "nodejs"), f"نود سیستم معرفی نشد: {node_p!r}"
    assert "ms-playwright" in bw_p, f"مسیر cache ست نشد: {bw_p!r}"
    # مقدار دستی کاربر دست نمی‌خورد
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/custom/px"
    dc._ensure_pw_env()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/custom/px"
    print("ok: _ensure_pw_env روی ترموکس → نود سیستم + PLAYWRIGHT_BROWSERS_PATH (فیکس android)؛ مقدار دستی دست‌نخورده")
finally:
    os.environ.update({k: v for k, v in _saved_env.items() if v is not None})
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)

# ─── نمایشگر: پنجرهٔ مرئی روی ترموکس بدون X11 → راهنمای termux-x11 ─────────
_saved_disp = {k: os.environ.get(k) for k in ("DISPLAY", "WAYLAND_DISPLAY", "TERMUX")}
for k in ("DISPLAY", "WAYLAND_DISPLAY"):
    os.environ.pop(k, None)
try:
    msg = dc._visible_launch_error(headless=False)
    assert msg and "termux-x11" in msg and "DISPLAY" in msg, msg
    assert dc._visible_launch_error(headless=True) is None, "headless نباید خطای نمایشگر بدهد"
    assert dc.auto_headless_background() is True, "لینوکس بدون نمایشگر → پس‌زمینه headless"
    os.environ["DISPLAY"] = ":0"
    assert dc._visible_launch_error(headless=False) is None, "با نمایشگر مانعی نیست"
    assert dc.auto_headless_background() is False
    print("ok: گارد نمایشگر — پیام راهنمای termux-x11 به‌جای کرش مبهم؛ پس‌زمینه خودکار headless")
finally:
    os.environ.update({k: v for k, v in _saved_disp.items() if v is not None})
    for k, v in _saved_disp.items():
        if v is None:
            os.environ.pop(k, None)
os.environ["TERMUX"] = "1"   # شبیه‌سازی ترموکس برای ادامهٔ تست

# ─── خطاهای مرگ درایور → پیام فارسی قابل‌اقدام ─────────────────────────────
h1 = dc._pw_driver_help(RuntimeError("Connection closed while reading from the driver."))
assert h1 and "PLAYWRIGHT_BROWSERS_PATH" in h1 and "Unsupported platform" in h1, h1
h2 = dc._pw_driver_help(RuntimeError("This installation of Playwright does not include Node.js."))
assert h2 and "nodejs-lts" in h2, h2
assert dc._pw_driver_help(RuntimeError("some random launch error")) is None
print("ok: خطای «connection closed» → راهنمای فارسی دقیق (نود + BROWSERS_PATH)")

# ─── بدون PyGithub هم ابزارهای گیت‌هاب کار می‌کنند (کلاینت REST خالص) ───────
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class _FakeGH(BaseHTTPRequestHandler):
    def log_message(self, *a):  # بی‌صدا
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/user":
            body = b'{"login": "termux-user", "name": "Termux User", "html_url": "https://github.com/termux-user"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("x-oauth-scopes", "repo, workflow")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


_gh = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGH)
_gh.daemon_threads = True
threading.Thread(target=_gh.serve_forever, daemon=True).start()
GH_FAKE = f"http://127.0.0.1:{_gh.server_address[1]}"

_tools = gt.GitHubTools("ghp_anything", api_base=GH_FAKE)
_info = _tools.validate_token()
assert _info["login"] == "termux-user" and "repo" in (_info["token_scopes"] or ""), _info
print("ok: بدون PyGithub — ابزارهای گیت‌هاب با کلاینت REST خالص کار می‌کنند ✓")

import agent  # noqa: E402 — بدون PyGithub هم import می‌شود (import قدیمی حذف شد)
assert agent.config.MAX_ROUNDS > 0, "agent import کامل نشد"
assert gt.GITHUB_LIB is False, "پرچم GITHUB_LIB در github_tools دیده نشد"
import json  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import server as srv  # noqa: E402

srv.config.GH_API_BASE = GH_FAKE   # اعتبارسنجی گیت‌هاب هم به فیک می‌رود (بدون شبکهٔ واقعی)
srv.STATE.deepseek_ready = False
srv.STATE.deepseek_started = False
srv.STATE.deepseek_message = "playwright نصب نیست"
srv.STATE.engine.brain = None
s = srv.serve(open_browser=False)
threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.4)

with urllib.request.urlopen("http://127.0.0.1:8770/", timeout=10) as r:
    html = r.read().decode("utf-8")
assert "DevX" in html and len(html) > 5000, f"UI نیامد: {len(html)}"
assert "ورود با مرورگر" in html, "دکمهٔ ورود با مرورگر در UI نیست"
with urllib.request.urlopen("http://127.0.0.1:8770/api/projects", timeout=10) as r:
    d = json.loads(r.read().decode("utf-8"))
assert d.get("ok"), d

# نصب کلاینت گیت‌هاب روی ترموکسِ بدون PyGithub — همان مسیری که «ورود با مرورگر»
# می‌رود (github_login_poll → _install_github)؛ کلاینت REST خالص باید کار کند
res = srv.STATE._install_github("ghp_fakefaketoken123")
assert res.get("ok") is True and res.get("login") == "termux-user", f"پاسخ نصب: {res}"
assert srv.STATE.gh_tools is not None and srv.STATE.gh_tools.login == "termux-user"

# ورود توکنی حذف شده — /api/token باید 404 بدهد (ورود فقط با Device Flow)
req404 = urllib.request.Request(
    "http://127.0.0.1:8770/api/token",
    data=json.dumps({"token": "ghp_fakefaketoken123"}).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req404, timeout=10)
    raise SystemExit("/api/token باید 404 می‌داد")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code

# وضعیت deepseek از طریق STATE/SSE می‌آید (endpoint status جدا وجود ندارد)
assert srv.STATE.deepseek_message == "playwright نصب نیست"
print("ok: سرور بدون playwright/PyGithub/dotenv سرو می‌دهد — UI + API + گیت‌هاب (Device Flow) ✓")
s.shutdown()
s.server_close()
_gh.shutdown()
print("\nTERMUX SIMULATION PASSED ✅")
