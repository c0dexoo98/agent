# -*- coding: utf-8 -*-
"""تست مغز API — کلاینت urllib خالص + اتصال سرور + اولویت انتخاب مغز.

سرور جعلی OpenAI-compatible می‌سازیم (GET /models + POST /chat/completions با
SSE استریم) و چک می‌کنیم:
  ۱) APIBrain: جمع‌کردن دلتاها + استریم زنده + تاریخچه + new_chat
  ۲) خطای 401 → پیام فارسی واضح
  ۳) POST /api/ai → مغز API فعال + خواندن وضعیت با GET /api/ai
  ۴) قرارداد موتور: parse_model_action روی خروجی مغز API اکشن می‌دهد
  ۵) اولویت: با AI_API_KEY، connect_deepseek بدون مرورگر مغز API می‌گذارد
"""
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

ACTION = {"final_answer": "پاسخ تست از مغز API"}
ACTION_RAW = json.dumps(ACTION, ensure_ascii=False)
_CHUNKS = [ACTION_RAW[i:i + 12] for i in range(0, len(ACTION_RAW), 12)]


class _FakeOpenAI(BaseHTTPRequestHandler):
    last_body: dict = {}

    def log_message(self, *a):  # بی‌صدا
        pass

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == "Bearer sk-test-valid"

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/models":
            if not self._authed():
                return self._json({"error": "bad key"}, status=401)
            return self._json({"data": [{"id": "fake-model"}, {"id": "other"}]})
        self._json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        if self.path == "/chat/completions":
            if not self._authed():
                return self._json({"error": "bad key"}, status=401)
            type(self).last_body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.close_connection = True   # بدون فریم‌بندی chunked — خواندن تا EOF
            self.end_headers()
            for ch in _CHUNKS:
                evt = json.dumps({"choices": [{"delta": {"content": ch}}]}, ensure_ascii=False)
                self.wfile.write(f"data: {evt}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json({"error": "not found"}, status=404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
srv.daemon_threads = True
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ─── ۱) SSE + دلتا + تاریخچه ───────────────────────────────────────────────
from api_client import APIBrain, APIError  # noqa: E402

brain = APIBrain(base_url=BASE, api_key="sk-test-valid", model="fake-model")
deltas: list = []
brain.set_on_delta(deltas.append)
brain.send("پرامپت کامل موتور — تسک تست")
raw = brain.wait_response()
assert raw == ACTION_RAW, f"متن ناقص: {raw!r}"
assert deltas and deltas[-1] == ACTION_RAW and len(deltas) > 1, "استریم دلتا (تجمعی، مثل مغز مرورگر) خراب است"
assert len(brain._messages) == 2 and brain._messages[1]["role"] == "assistant", "تاریخچه درست ساخته نشد"
assert brain._messages[0]["content"] == "پرامپت کامل موتور — تسک تست"
brain.new_chat()
assert brain._messages == [], "new_chat پاک نکرد"
print("✅ ۱) SSE استریم + دلتا + تاریخچه + new_chat")

# ─── ۲) کلید نامعتبر → پیام واضح ──────────────────────────────────────────
bad = APIBrain(base_url=BASE, api_key="sk-wrong", model="fake-model")
try:
    bad.validate()
    raise SystemExit("validate باید ۴۰۱ می‌داد")
except APIError as e:
    assert e.status == 401 and "کلید API نامعتبر" in str(e), str(e)
try:
    bad.send("x")
    bad.wait_response()
    raise SystemExit("wait_response باید ۴۰۱ می‌داد")
except APIError as e:
    assert e.status == 401, str(e)
print("✅ ۲) کلید نامعتبر → APIError با پیام فارسی")

# ─── ۳) سرور DevX: POST /api/ai → مغز فعال + قرارداد موتور ────────────────
import server as srvmod  # noqa: E402

srvmod.STATE.engine.brain = None
s = srvmod.serve(open_browser=False)
threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.4)

req = urllib.request.Request("http://127.0.0.1:8770/api/ai",
                             data=json.dumps({"key": "sk-test-valid", "base_url": BASE, "model": "fake-model"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=15) as r:
    j = json.loads(r.read().decode())
assert j.get("ok") and j.get("model") == "fake-model", f"connect_ai: {j}"
assert srvmod.STATE.brain_kind == "api" and srvmod.STATE.deepseek_ready, "مغز API فعال نشد"
assert "مغز API فعال" in srvmod.STATE.deepseek_message

with urllib.request.urlopen("http://127.0.0.1:8770/api/ai", timeout=10) as r:
    st = json.loads(r.read().decode())
assert st.get("kind") == "api" and st.get("ready") and st.get("key_masked", "").startswith("sk-te"), st

# قرارداد موتور: پرامپت سبک موتور → مغز API → اکشن JSON
from system_prompt import parse_model_action  # noqa: E402

eb = srvmod.STATE.engine.brain
eb.send("=== SYSTEM === (تست) === USER TASK ===\nسلام")
out = eb.wait_response()
act = parse_model_action(out)
assert act == ACTION, f"اکشن پارس نشد: {act}"
assert _FakeOpenAI.last_body.get("model") == "fake-model", f"مدل در payload اشتباه است: {_FakeOpenAI.last_body}"
print("✅ ۳) POST /api/ai → مغز فعال + وضعیت + قرارداد اکشن موتور")

# ─── ۴) اولویت: AI_API_KEY → connect_deepseek بدون مرورگر ─────────────────
srvmod.config.AI_API_KEY = "sk-test-valid"
srvmod.STATE._api_key_mem = None
srvmod.STATE.deepseek_ready = False
srvmod.STATE.connect_deepseek(visible_login=False)
for _ in range(50):
    if srvmod.STATE.deepseek_ready and srvmod.STATE.brain_kind == "api":
        break
    time.sleep(0.1)
assert srvmod.STATE.brain_kind == "api" and srvmod.STATE.deepseek_ready, "اولویت API در connect_deepseek خراب است"
assert _FakeOpenAI.last_body.get("model") == "fake-model"
srvmod.config.AI_API_KEY = ""
print("✅ ۴) با AI_API_KEY، connect_deepseek بدون مرورگر مغز API می‌گذارد")

s.shutdown()
s.server_close()
srv.shutdown()
print("\nAPI BRAIN TESTS PASSED ✅")
