# -*- coding: utf-8 -*-
"""تست دود سرور DevX: سرور واقعی + FakeBrain — بدون دیپ‌سیک و بدون گیت‌هاب.

اجرا:  python tests/test_server_smoke.py
بررسی: صفحهٔ UI، snapshot وضعیت، استریم SSE، رد شدن تسک قبل از اتصال دیپ‌سیک،
تاریخچهٔ رویدادها و اعتبارسنجی رد توکن بدشکل.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server as srv  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402


def req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"http://127.0.0.1:{srv.config.DEVX_PORT}{path}",
        data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read()


def main() -> None:
    # مغز آمادهٔ دیپ‌سیک را شبیه‌سازی می‌کنیم تا تسک اجرا شود
    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "اتصال برقرار است (FakeBrain)"
    srv.STATE.engine.brain = FakeBrain([])

    server = srv.serve(open_browser=False)
    port = srv.config.DEVX_PORT
    # حلقهٔ accept باید در نخ جدا اجرا شود وگرنه هیچ درخواستی پاسخ نمی‌گیرد
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # ۱) صفحهٔ UI
        status, html = req("/")
        text = html.decode("utf-8")
        assert status == 200 and "DEVX" in text and "/api/stream" in text
        print("ok: GET / → DevX UI served", flush=True)

        # ۲) snapshot
        status, raw = req("/api/state")
        s = json.loads(raw)
        assert s["token"]["set"] is False and s["tools"] > 0 and s["max_rounds"] >= 1
        print(f"ok: /api/state → tools={s['tools']} memory={s['memory']['counts']}")

        # ۳) توکن بدشکل باید رد شود
        status, raw = req("/api/token", "POST", {"token": "not-a-token"})
        assert json.loads(raw)["ok"] is False
        print("ok: /api/token rejects malformed token")

        # ۴) تسک بدون مغزِ پاسخ‌ده → FakeBrain خالی → خطای مهربان task_end
        status, raw = req("/api/task", "POST", {"task": "تست دود سرور"})
        assert json.loads(raw)["ok"] is True
        # کمی صبر تا worker تمام شود (FakeBrain خالی → RuntimeError → task_end err)
        time.sleep(0.8)
        status, raw = req("/api/history")
        events = json.loads(raw)["events"]
        assert any(e["event"] == "task_start" for e in events)
        assert any(e["event"] == "task_end" and not e.get("ok") for e in events)
        print("ok: /api/task + /api/history (brain error handled → task_end)")

        # ۵) تسک موفق با سناریوی کامل: ابزار → پاسخ نهایی → مهارت
        from agent_loop import FakeBrain as FB

        srv.STATE.engine.brain = FB([
            json.dumps({"tool": "echo", "arguments": {"text": "hi"}}),
            json.dumps({"final_answer": "تمام شد"}),
            json.dumps({"name": "t-smoke", "description": "د", "triggers": ["t"], "steps": "1. echo"}),
        ])
        status, raw = req("/api/task", "POST", {"task": "تست موفق"})
        assert json.loads(raw)["ok"] is True
        time.sleep(1.0)
        status, raw = req("/api/history")
        events = json.loads(raw)["events"]
        kinds = [e["event"] for e in events]
        for expected in ("tool_call", "tool_result", "final", "skill_learned", "task_end"):
            assert expected in kinds, f"رویداد {expected} در تاریخچه نیست: {kinds}"
        status, raw = req("/api/skills")
        assert any(s["name"] == "t-smoke" for s in json.loads(raw)["skills"])
        print("ok: full task flow via HTTP (tool → final → skill persisted)")

        # ۶) SSE: اتصال زنده و دریافت snapshot (خط‌به‌خط بخوان — استریم بسته نمی‌شود)
        sse = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=15)
        first = b""
        for _ in range(40):
            line = sse.readline()
            if not line:
                break
            first += line
            if b"snapshot" in first and line.strip() in (b"", b"\n"):
                break
        sse.close()
        assert b"data:" in first and b"snapshot" in first
        print("ok: /api/stream SSE live (history + snapshot)", flush=True)

        print("\nSERVER SMOKE TEST PASSED ✅", flush=True)
    finally:
        server.shutdown()   # با serve_forever فعال مجاز است
        server.server_close()
        t.join(timeout=5)

    # ۷) پرچم آماده بودن دیپ‌سیک پایین → تسک باید رد شود
    srv.STATE.deepseek_ready = False
    srv2 = srv.serve(open_browser=False)
    t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
    t2.start()
    try:
        status, raw = req("/api/task", "POST", {"task": "x"})
        assert json.loads(raw)["ok"] is False
        print("ok: task rejected while deepseek not ready", flush=True)
    finally:
        srv2.shutdown()
        srv2.server_close()
        t2.join(timeout=5)


if __name__ == "__main__":
    main()
