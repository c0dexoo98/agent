# -*- coding: utf-8 -*-
"""تست بار همزمان سرور DevX — راستی‌آزمایی thread-safety زیر بار واقعی.

۸ نخ همزمان، ده‌ها درخواست خواندن/نوشتن روی اندپوینت‌های اصلی +
اعتبارسنجی زنجیرهٔ ممیزی بعد از appends همزمان (باید سالم بماند).

اجرا:  python tests/test_stress.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server as srv  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402

PORT = srv.config.DEVX_PORT
GETS = [
    "/api/state",
    "/api/projects",
    "/api/sessions",
    "/api/notifications",
    "/api/audit?limit=5",
    "/api/usage?days=7",
    "/api/search?q=test",
    "/api/timeline",
    "/api/skills",
]
failures: list = []
lock = threading.Lock()


def req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 20):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def worker_gets(n: int) -> None:
    for i in range(n):
        path = GETS[(i + threading.get_ident()) % len(GETS)]
        try:
            status, payload = req(path)
            assert status == 200 and payload.get("ok") is True, f"{path}: {status} {str(payload)[:120]}"
        except Exception as e:  # noqa: BLE001
            with lock:
                failures.append(f"GET {path}: {e}")


def worker_creates(kind: str, n: int, created: list) -> None:
    for i in range(n):
        name = f"stresstest-{kind}-{threading.get_ident() % 1000}-{i}"
        try:
            path = "/api/projects" if kind == "project" else "/api/sessions"
            body = {"name": name} if kind == "project" else {"title": name}
            status, payload = req(path, "POST", body)
            assert status == 200 and payload.get("ok"), f"create {kind}: {str(payload)[:120]}"
            if kind == "project":
                created.append(payload["project"]["id"])
            else:
                created.append(payload["session"]["id"])
        except Exception as e:  # noqa: BLE001
            with lock:
                failures.append(f"create {kind} #{i}: {e}")


def main() -> None:
    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "اتصال برقرار است (FakeBrain)"
    srv.STATE.engine.brain = FakeBrain([])

    server = srv.serve(open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    t0 = time.time()
    try:
        # یک پروژهٔ پایه (مثل جریان واقعی کاربر) تا اندپوینت‌های پروژه‌محور کار کنند
        status, payload = req("/api/projects", "POST", {"name": "stress-base"})
        assert status == 200 and payload.get("ok"), str(payload)[:120]
        base_id = payload["project"]["id"]

        # موج A: خواندن همزمان (۸ نخ × ۱۲ درخواست)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(worker_gets, [12] * 8))

        # موج B: ساخت همزمان پروژه (۸ نخ × ۴)
        proj_ids: list = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(worker_creates, "project", 4, proj_ids) for _ in range(8)]
            [f.result() for f in futs]
        assert len(proj_ids) == 32, f"۳۲ پروژه باید ساخته شود: {len(proj_ids)}"

        # موج C: ساخت همزمان سشن (۴ نخ × ۵)
        sess_ids: list = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(worker_creates, "session", 5, sess_ids) for _ in range(4)]
            [f.result() for f in futs]
        assert len(sess_ids) == 20, f"۲۰ سشن باید ساخته شود: {len(sess_ids)}"

        # موج D: حذف همزمان نیمی از پروژه‌ها (بازچینش آیدی زیر بار)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(req, "/api/projects/delete", "POST", {"id": pid}) for pid in proj_ids[:16]]
            for f in futs:
                status, payload = f.result()
                assert payload.get("ok"), str(payload)[:120]

        # نهایی: سرور زنده + زنجیرهٔ ممیزی سالم بعد از ده‌ها append همزمان
        status, st = req("/api/state")
        assert status == 200 and st.get("tools", 0) > 0, str(st)[:120]
        status, av = req("/api/audit?verify=1")
        assert status == 200 and av.get("ok") is True and av.get("broken_at") is None, str(av)[:200]
        elapsed = time.time() - t0
        print(f"ok: stress — 96 GET + 52 create + 16 delete همزمان در {elapsed:.1f}s بدون خطا")
        print(f"ok: audit chain verified بعد از بار همزمان ({av.get('entries', '?')} رکورد)")

        # پاک‌سازی: پروژهٔ پایه + بقیهٔ ساختارهای تست
        req("/api/projects/delete", "POST", {"id": base_id}, timeout=30)
        for pid in proj_ids[16:]:
            req("/api/projects/delete", "POST", {"id": pid}, timeout=30)
        for sid in sess_ids:
            req("/api/sessions/delete", "POST", {"id": sid}, timeout=30)
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures[:10]:
            print(" -", f)
        sys.exit(1)
    print("\nSTRESS TEST PASSED ✅")


if __name__ == "__main__":
    main()
