# -*- coding: utf-8 -*-
"""Soak test حافظه/پایداری (پاس ۵) — ۹۰ ثانیه بار ترکیبی روی سرور واقعی.

برخلاف مرور ایستای پاس ۴، اینجا رشد حافظه را «اندازه‌گیری» می‌کنیم:
  * ~۱۲۰ اتصال/قطع SSE → مشترک‌های bus باید به baseline برگردند
    (راستی‌آزمایی runtime فیکس unsubscribe پاس ۴)
  * ~۳۰ تسک + ۶۰ اعلان + ۵۰ فکت + ۶۰ GET — همه از مسیر واقعی API
  * یک job زمان‌بند (every_s=30) باید وسط soak فایر شود (مسیر تولیدی next_run)
  * tracemalloc: مقایسهٔ snapshot قبل/بعد + gc — رشد باید محدود باشد
  * زنجیرهٔ ممیزی باید بعد از همهٔ این‌ها سالم بماند

اجرا:  python tests/test_soak.py   (~۹۰ ثانیه)
"""
from __future__ import annotations

import gc
import http.client
import json
import sys
import threading
import time
import traceback
import tracemalloc
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server as srv  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402

PORT = srv.config.DEVX_PORT
BASE = f"http://127.0.0.1:{PORT}"


def req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def sse_connect_close(port: int) -> None:
    """اتصال کوتاه به /api/stream: کمی بخوان و ناگهان قطع کن."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        r.read(64)
        c.close()
    except Exception:
        pass


def main() -> None:
    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "اتصال برقرار است (FakeBrain)"
    srv.STATE.engine.brain = FakeBrain([])

    server = srv.serve(open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    assert req("/api/state")[0] == 200, "سرور بالا نیامد"

    # یک پروژهٔ فعال برای job زمان‌بند
    st, body = req("/api/projects", "POST", {"name": "soak"})
    assert st == 200, body

    baseline_subs = len(srv.STATE.bus._subs)
    print(f"• baseline مشترک‌های bus: {baseline_subs}")

    # ── مشترک SSE ماندگار (کل مدت بار) — مثل یک تب باز واقعی ──
    # نکتهٔ ظریف: read روی استریم SSE مسدود می‌شود؛ نخ باید با تایم‌اوت کوتاه
    # خودش خارج شود و اتصال را ببندد (بستن fd از نخ دیگر به‌خاطر رفرکانسِ
    # makefile واقعاً socket را نمی‌بندد و اتصال زنده می‌ماند).
    stop_persist = threading.Event()
    holder: dict = {}

    def persistent_sse() -> None:
        import socket as _s
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=25)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        holder.update(c=c, r=r)
        if c.sock is not None:
            c.sock.settimeout(0.5)
        try:
            while not stop_persist.is_set():
                try:
                    if not r.read(256):
                        break
                except (_s.timeout, TimeoutError):
                    continue
                except OSError:
                    break
        finally:
            try:
                r.close()
            except Exception:
                pass
            c.close()

    persist_t = threading.Thread(target=persistent_sse, daemon=True)
    persist_t.start()
    time.sleep(0.5)

    # job زمان‌بند: هر ۳۰ ثانیه (باید حداقل یک‌بار وسط soak فایر شود)
    st, body = req("/api/schedule", "POST",
                   {"name": "soak-job", "task": "تسک زمان‌بندی soak", "kind": "interval", "every_s": 30})
    assert st == 200 and json.loads(body).get("ok"), body
    job_id = json.loads(body)["job"]["id"]

    tracemalloc.start()
    gc.collect()
    snap0 = tracemalloc.take_snapshot()

    # ── بار ترکیبی ──
    t0 = time.monotonic()
    tasks_ok = 0
    for i in range(120):
        sse_connect_close(PORT)                    # چرخهٔ اتصال/قطع
        if i % 4 == 0:                             # ~۳۰ تسک
            st, _ = req("/api/task", "POST", {"task": f"تسک soak شمارهٔ {i}"})
            if st == 200:
                tasks_ok += 1
            time.sleep(0.05)
        if i % 2 == 0:                             # ~۶۰ GET سبک
            req("/api/state")
            req("/api/history")
        if i % 2 == 1:                             # ~۶۰ اعلان
            srv.STATE.notifications.push("system", f"soak {i}", "بار آزمون")
        if i % 3 == 2:                             # ~۴۰ فکت حافظه
            srv.STATE.engine.memory.add_fact(f"فکت soak {i}")
        if i % 30 == 29:
            print(f"  … چرخهٔ {i + 1}/120 — {time.monotonic() - t0:.0f}s")
    workload_s = time.monotonic() - t0

    stop_persist.set()
    persist_t.join(timeout=6)
    if persist_t.is_alive():
        # دیاپ: کجا گیر کرده؟
        fr = sys._current_frames().get(persist_t.ident)
        if fr:
            print("  ⚠ نخ ماندگار زنده — استک:", [str(l).strip() for l in traceback.format_stack(fr)][-2:])
        # مسیر پشتیبان: بستن fp (مالک واقعی fd) از نخ اصلی
        try:
            holder["r"].fp.close()
        except Exception:
            pass
        try:
            holder["c"].close()
        except Exception:
            pass
        persist_t.join(timeout=6)
    assert not persist_t.is_alive(), "نخ کلاینت ماندگار باید بسته شود"

    # ── پایش: پاک‌سازی مشترک مرده کراندار است (تجربی: ~۳۲s = ۲ heartbeat
    # + تأخیر زمان‌بندی). اگر از این هم گذشت، دیاپ استک برای عیب‌یابی.
    print("• انتظار تخلیهٔ مشترک‌های مرده …")
    deadline = time.monotonic() + 50
    subs_now = -1
    while time.monotonic() < deadline:
        subs_now = len(srv.STATE.bus._subs)
        if subs_now <= baseline_subs:
            break
        time.sleep(2)
        print(f"  … subs={subs_now} (baseline {baseline_subs})")
    if subs_now > baseline_subs:
        frames = sys._current_frames()
        for t in threading.enumerate():
            fr = frames.get(t.ident)
            if fr and "server.py" in str(fr.f_code.co_filename):
                print("STUCK-HANDLER:", t.name, [str(l).strip() for l in traceback.format_stack(fr)][-3:])
    assert subs_now <= baseline_subs, (
        f"نشتی مشترک SSE: {subs_now} > baseline {baseline_subs} — unsubscribe کار نمی‌کند")

    # ── حافظه ──
    gc.collect()
    snap1 = tracemalloc.take_snapshot()
    cur0, peak0 = tracemalloc.get_traced_memory()  # فقط برای گزارش
    growth_mb = (sum(s.size_diff for s in snap1.compare_to(snap0, "filename") if s.size_diff > 0)) / 1e6
    total_mb = sum(s.size for s in snap1.statistics("filename")) / 1e6
    print(f"• رشد حافظهٔ تخصیص‌یافته (مثبت‌ها): +{growth_mb:.1f} MB | کل: {total_mb:.1f} MB | peak: {peak0 / 1e6:.1f} MB")

    # ── سلامت نهایی ──
    assert req("/api/state")[0] == 200, "سرور بعد از soak پاسخ نمی‌دهد"
    ver = srv.STATE.audit.verify()
    assert ver.get("ok") or ver.get("valid"), f"زنجیرهٔ ممیزی شکست: {ver}"
    st, body = req("/api/schedule")
    jobs = {j["id"]: j for j in json.loads(body)["jobs"]}
    fired = jobs[job_id]["run_count"]
    assert fired >= 1, f"job زمان‌بند در {workload_s:.0f}s+ فایر نشد (run_count=0)"
    facts = srv.STATE.engine.memory.counts()["facts"]
    assert facts >= 40, f"فکت‌ها ثبت نشدند: {facts}"

    print(f"• تسک‌های موفق: {tasks_ok}/30 | فایر job: {fired}× | فکت: {facts} | اعلان: {len(srv.STATE.notifications.list(limit=500))}")
    print("• بزرگ‌ترین رشد حافظه (top-3):")
    for s in snap1.compare_to(snap0, "lineno")[:3]:
        print(f"    {s.size_diff / 1e6:+.2f} MB  {s.traceback[0]}")

    # رشد ساختاری (نشتی) با آستانهٔ سخاوت‌مندانه — نشتی واقعی مشترک‌ها/صف‌ها
    # خودش را در subs_now نشان می‌داد؛ اینجا فقط سقف کلی.
    assert growth_mb < 25, f"رشد حافظه غیرعادی: +{growth_mb:.1f} MB"

    server.shutdown()
    print(f"\nSOAK TEST PASSED ✅  ({workload_s:.0f}s بار + تخلیه | subs: {baseline_subs}→{subs_now})")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
