# -*- coding: utf-8 -*-
"""تست fuzz ورودی‌های خراب + کلاینت SSE کُند (پاس ۶).

قرارداد هر endpoint: هر ورودی‌ای که بفرستی، پاسخ باید JSON معتبر باشد
(نه HTML، نه اتصال بی‌پاسخ) و سرور باید زنده بماند. ورودی‌های خراب سه دسته‌اند:
بایت‌های غیر-JSON، دیکتی با نوع‌های غلط، و مقادیر حدی/غول‌پیکر.

route های deepseek/login و deepseek/reconnect مستثنی‌اند (مرورگر واقعی
راه می‌اندازند)؛ /api/stream هم چون استریم بلند است.

بخش دوم: مشترک SSE که هیچ نمی‌خواند → صف ۸۰۰تایی‌اش پر می‌شود؛
مسیر drop (put_nowait) نباید موتور را قفل کند — ۱۰۰۰ رویداد باید
سریع عبور کنند و سرور پاسخ‌گو بماند.

اجرا:  python tests/test_fuzz.py
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server as srv  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402

PORT = srv.config.DEVX_PORT
BASE = f"http://127.0.0.1:{PORT}"

GET_PATHS = [
    "/api/state", "/api/history", "/api/memory", "/api/skills",
    "/api/skills/versions", "/api/skills/diff", "/api/projects", "/api/sessions",
    "/api/notifications", "/api/approvals", "/api/audit", "/api/usage",
    "/api/schedule", "/api/watch", "/api/search", "/api/timeline",
    "/api/snapshots", "/api/checkpoints", "/api/exports", "/api/github/overview",
]
QUERY_FUZZ = [
    "?project=abc", "?project=-1", "?project=0", "?project=999999",
    "?project=1e99", "?project=%F0%9F%A4%96", "?project=", "?project=1&project=2",
    "?limit=abc", "?limit=-5", "?limit=1e99", "?kind=bogus", "?q=", "?q=%00",
    "?id=xyz", "?name=" + "x" * 500, "?file=../../etc/passwd",
]

POST_PATHS = [
    "/api/token", "/api/task", "/api/projects", "/api/projects/select",
    "/api/projects/delete", "/api/projects/rename", "/api/persona",
    "/api/project/settings", "/api/sessions", "/api/sessions/select",
    "/api/sessions/rename", "/api/sessions/delete", "/api/notifications/read",
    "/api/approvals/resolve", "/api/schedule", "/api/schedule/delete",
    "/api/schedule/toggle", "/api/watch", "/api/watch/delete",
    "/api/snapshots/create", "/api/snapshots/restore", "/api/export",
    "/api/import", "/api/resume", "/api/nonexistent",
]

RAW_BODIES = [b"{", b"[]", b"null", b'"str"', b"123", b"\xff\xfe\xfd", b"", b"a" * (1 << 20)]

TYPED_BODIES = [
    {"task": 123, "session_id": "abc"},
    {"name": {"x": 1}, "project_id": [1, 2]},
    {"token": {"a": 1}},
    {"id": None, "project_id": "من"},
    {"every_s": "soon", "kind": {"k": 1}},
    {"checkpoint_id": [], "at": 12345},
    {"session_id": True, "title": 3.14},
    {"approved": "yes", "read": "true"},
]
EXTREME_BODIES = [
    {"every_s": -999, "kind": "interval"},
    {"every_s": 10 ** 15, "kind": "interval"},
    {"project_id": 10 ** 12},
    {"name": "x" * 100000},
    {"task": "تسک\ufeff\x00\u202eمعکوس"},
    {"spec": {"kind": "daily", "at": "25:99"}},
    {"text": "🤖" * 5000},
]


def post_raw(path: str, raw: bytes):
    r = urllib.request.Request(BASE + path, data=raw, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def post_json(path: str, body):
    return post_raw(path, json.dumps(body, ensure_ascii=False).encode("utf-8"))


def get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def check_json(status: int, body: bytes, ctype: str, failures: list, tag: str) -> bool:
    """قرارداد: همیشه JSON معتبر — نه HTML، نه خالی. ۵۰۰ = استثنای فراری از route."""
    if "json" not in (ctype or ""):
        failures.append(f"{tag}: content-type={ctype!r}")
        return False
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as e:
        failures.append(f"{tag}: بدنه JSON نیست ({e}) — {body[:60]!r}")
        return False
    if not isinstance(parsed, dict):
        failures.append(f"{tag}: پاسخ dict نیست — {type(parsed).__name__}")
        return False
    if status == 500:  # گارد global خواسته — ورودی بد نباید به استثنای فراری برسد
        STATUS_500.append(f"{tag} → {str(parsed.get('error'))[:70]}")
    return True


def alive(failures: list) -> bool:
    st, body, ct = get("/api/state")
    ok = st == 200 and check_json(st, body, ct, failures, "alive-check")
    return ok


STATUS_500: list = []  # ورودی‌های بد که به استثنای فراری (۵۰۰) رسیدند — باید صفر شوند


def test_fuzz() -> None:
    failures: list = []
    stats = {"total": 0, "err": 0}
    t_all = time.monotonic()

    # ── GET ──
    for p in GET_PATHS:
        for q in QUERY_FUZZ:
            st, body, ct = get(p + q)
            stats["total"] += 1
            if not check_json(st, body, ct, failures, f"GET {p}{q}"):
                stats["err"] += 1
        if not alive(failures):
            failures.append(f"سرور بعد از GET {p} مردد")

    # ── POST: بایت‌های خراب ──
    for p in POST_PATHS:
        for raw in RAW_BODIES:
            st, body, ct = post_raw(p, raw)
            stats["total"] += 1
            if not check_json(st, body, ct, failures, f"POST {p} raw={raw[:12]!r}"):
                stats["err"] += 1
        if not alive(failures):
            failures.append(f"سرور بعد از POST-raw {p} مردد")

    # ── POST: نوع غلط ──
    for p in POST_PATHS:
        for body_obj in TYPED_BODIES + EXTREME_BODIES:
            st, body, ct = post_json(p, body_obj)
            stats["total"] += 1
            if not check_json(st, body, ct, failures, f"POST {p} body={str(body_obj)[:40]!r}"):
                stats["err"] += 1
        if not alive(failures):
            failures.append(f"سرور بعد از POST-typed {p} مردد")

    # ── سلامت state بعد از همه‌چیز ──
    for f in ("memory/projects.json", "jobs.json", "watches.json",
              "notifications.json", "usage.json", "approvals.json"):
        p = Path(PROJECT_ROOT) / f
        if p.exists():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                failures.append(f"فایل state خراب شد: {f}: {e}")

    dur = time.monotonic() - t_all
    assert not failures, "\n" + "\n".join(failures[:20])
    assert not STATUS_500, (
        f"{len(STATUS_500)} ورودی بد به ۵۰۰ رسید (باید ok:false بدهند):\n"
        + "\n".join(STATUS_500[:10]))
    print(f"ok: fuzz — {stats['total']} درخواست خراب، همه JSON معتبر بدون ۵۰۰، سرور زنده ({dur:.0f}s)")


def test_slow_sse_consumer() -> None:
    """مشترک SSE که هیچ نمی‌خواند: صف پر می‌شود؛ drop باید کار کند نه قفل."""
    failures: list = []
    # یک اتصال باز که هرگز نمی‌خواند
    stalled = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    stalled.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
    time.sleep(0.6)  # اشتراک ثبت شود

    base_subs = len(srv.STATE.bus._subs)
    assert base_subs >= 2, "مشترک stalled ثبت نشد"

    # ۱۰۰۰ رویداد سریع — نباید بلوک شود (صف ۸۰۰تایی → با put_nowait دور ریخته می‌شود)
    t0 = time.monotonic()
    for i in range(1000):
        srv.STATE.bus.emit("flood", n=i)
    flood_s = time.monotonic() - t0
    assert flood_s < 10, f"emit با مشترک کُند بلاک شد: {flood_s:.1f}s"

    # سرور هنوز پاسخ‌گوست + یک مشترک تازه رویداد زنده می‌گیرد
    assert alive(failures), "سرور بعد از سیل رویداد پاسخ نمی‌دهد"
    fresh = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    fresh.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
    time.sleep(0.4)
    srv.STATE.bus.emit("probe", mark="after-flood")
    fresh.settimeout(5)
    got = b""
    try:
        while b"probe" not in got and len(got) < (1 << 20):
            got += fresh.recv(4096)
    except socket.timeout:
        pass
    assert b"probe" in got, "مشترک تازه رویداد زنده نگرفت — سیل صف را خراب کرد"

    stalled.close()
    fresh.close()
    assert not failures, failures
    print(f"ok: slow-SSE — ۱۰۰۰ رویداد در {flood_s:.2f}s (بدون بلوک)، مشترک تازه زنده دریافت کرد")


def main() -> None:
    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "FakeBrain"
    srv.STATE.engine.brain = FakeBrain([])
    server = srv.serve(open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    try:
        test_fuzz()
        test_slow_sse_consumer()
    finally:
        server.shutdown()
    print("\nALL FUZZ TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
