# -*- coding: utf-8 -*-
"""شبیه‌سازی ترموکس: playwright در دسترس نیست — سرور باید بدون آن سالم بیاید."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


class _NoPlaywright:
    """مسدودساز import به‌شکل دقیق‌ترموکس (پکیج نصب نیست) — با find_spec مدرن."""

    def find_spec(self, name, path=None, target=None):
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError(f"No module named {name!r} (Termux simulation)")
        return None


sys.meta_path.insert(0, _NoPlaywright())
for m in list(sys.modules):
    if m == "playwright" or m.startswith("playwright."):
        del sys.modules[m]

import deepseek_client as dc  # noqa: E402

assert dc.PLAYWRIGHT_AVAILABLE is False, "پرچم اختیاری بودن فعال نشد"
from deepseek_client import DeepSeekClient, DeepSeekError  # noqa: E402

c = DeepSeekClient(user_data_dir=Path("/tmp/nope"))
try:
    c.start()
    raise SystemExit("start() باید خطای واضح می‌داد")
except DeepSeekError as e:
    assert "playwright نصب نیست" in str(e), str(e)
print("ok: بدون playwright — start() خطای واضح و ساخت‌یافته می‌دهد")

import threading  # noqa: E402
import json  # noqa: E402
import urllib.request  # noqa: E402

import server as srv  # noqa: E402

srv.STATE.deepseek_ready = False
srv.STATE.deepseek_started = False
srv.STATE.deepseek_message = "playwright نصب نیست"
srv.STATE.engine.brain = None
s = srv.serve(open_browser=False)
threading.Thread(target=s.serve_forever, daemon=True).start()
import time  # noqa: E402
time.sleep(0.4)

with urllib.request.urlopen("http://127.0.0.1:8770/", timeout=10) as r:
    html = r.read().decode("utf-8")
assert "DevX" in html and len(html) > 5000, f"UI نیامد: {len(html)}"
with urllib.request.urlopen("http://127.0.0.1:8770/api/projects", timeout=10) as r:
    d = json.loads(r.read().decode("utf-8"))
assert d.get("ok"), d
# وضعیت deepseek از طریق STATE/SSE می‌آید (endpoint status جدا وجود ندارد)
assert srv.STATE.deepseek_message == "playwright نصب نیست"
print("ok: سرور بدون playwright سرو می‌دهد — UI + API پروژه‌ها + پیام وضعیت ✓")
s.shutdown()
s.server_close()
print("\nTERMUX SIMULATION PASSED ✅")
