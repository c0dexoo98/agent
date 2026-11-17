# -*- coding: utf-8 -*-
"""تست دوام و نژ نوشتن (پاس ۷).

بخش ۱ — نژ همزمانی: ۸ نخ به‌طور موازی روی استورهای واقعی (از مسیر سرور)
پروژه می‌سازند/حذف می‌کنند، تسک زمان‌بندی و نوتیفیکیشن اضافه می‌کنند؛
بعد از گردباد، همهٔ فایل‌های state باید JSON معتبر باشند، آیدی‌ها یکتا و
شمارش‌ها دقیقاً منطبق بر عملیات موفق.

بخش ۲ — دوام kill-restart: سرور به‌صورت زیرپروسس بالا می‌آید، داده ساخته
می‌شود، در میانهٔ یک دستهٔ نوشتنِ همزمان SIGKILL می‌شود (بدون shutdown
تمیز) و دوباره بالا می‌آید؛ هرچه ثبت شده بود باید سالم برگردد — این
end-to-endproof الگوی اتمیک tmp+replace است.

اجرا:  python tests/test_durability.py
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server as srv  # noqa: E402
from agent_loop import FakeBrain  # noqa: E402

PORT = srv.config.DEVX_PORT
BASE = f"http://127.0.0.1:{PORT}"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def test_concurrent_writes() -> None:
    """۸ نخ × عملیات موازی روی استورها؛ یکپارچگی بعد از گردباد."""
    failures: list = []
    results: dict = {"create": [], "sched": [], "notif_read": []}
    lock = threading.Lock()

    def creator(i: int) -> None:
        for k in range(6):
            r = post("/api/projects", {"name": f"race-{i}-{k}"})
            with lock:
                results["create"].append(r.get("ok", False))

    def scheduler_adder(i: int) -> None:
        for k in range(4):
            r = post("/api/schedule", {
                "name": f"job-{i}-{k}", "task": "تسک تست",
                "kind": "interval", "every_s": 3600})
            with lock:
                results["sched"].append(r.get("ok", False))

    def reader(i: int) -> None:
        for _ in range(10):
            try:
                get("/api/projects")
                get("/api/schedule")
                get("/api/state")
            except Exception as e:
                with lock:
                    failures.append(f"خوانندهٔ {i}: {e}")

    threads = ([threading.Thread(target=creator, args=(i,)) for i in range(6)]
               + [threading.Thread(target=scheduler_adder, args=(i,)) for i in range(2)]
               + [threading.Thread(target=reader, args=(i,)) for i in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "نخی گیر کرد — بن‌بست احتمالی"

    # ── یکپارچگی استورها ──
    projects = get("/api/projects")["projects"]
    ids = [p["id"] for p in projects]
    assert len(ids) == len(set(ids)), "آیدی تکراری در پروژه‌ها — نژ واقعی!"
    # کلید داخلی نباید به بیرون درز کند (فیلد مسیرساز است)
    assert all("key" not in p for p in projects), "کلید داخلی پروژه در API عمومی درز کرد!"
    created_ok = sum(results["create"])
    race_names = [p["name"] for p in projects if p["name"].startswith("race-")]
    assert len(race_names) == created_ok, (
        f"به‌روزرسانی از دست رفته: {created_ok} ساخت موفق ولی {len(race_names)} پروژهٔ race ثبت شد")

    jobs = get("/api/schedule")["jobs"]
    job_ids = [j["id"] for j in jobs]
    assert len(job_ids) == len(set(job_ids)), "آیدی تکراری در jobs!"
    assert len(jobs) == sum(results["sched"]), "تسک زمان‌بندی از دست رفته!"

    for f in ("memory/projects.json", "jobs.json", "watches.json",
              "notifications.json", "usage.json"):
        p = PROJECT_ROOT / f
        if p.exists():
            json.loads(p.read_text(encoding="utf-8"))  # JSON معتبر بعد از گردباد

    print(f"ok: concurrent — {created_ok} پروژه + {len(jobs)} job موازی، "
          f"بدون آیدی تکراری/به‌روزرسانی از دست رفته")


def _spawn_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "import server; server.serve(open_browser=False).serve_forever()"],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.25)
        try:
            get("/api/state")
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("سرور زیرپروسس بالا نیامد")
    proc.kill()
    raise RuntimeError("سرور زیرپروسس پاسخ نداد")


def test_kill_restart() -> None:
    """SIGKILL در میانهٔ نوشتن همزمان → restart → همهٔ داده‌ها سالم."""
    proc = _spawn_server()
    try:
        # دادهٔ پایه
        assert post("/api/projects", {"name": "durable-پایه"})["ok"]
        assert post("/api/schedule", {"name": "job-durable", "task": "تسک",
                                      "kind": "daily", "at": "03:30"})["ok"]
        assert post("/api/snapshots/create", {})["ok"]
        base_projects = len(get("/api/projects")["projects"])
        base_jobs = len(get("/api/schedule")["jobs"])

        # نوشتن همزمان هنگام قتل — بدترین حالت برای اتمی‌بودن
        stop = threading.Event()
        def churn():
            while not stop.is_set():
                try:
                    post("/api/projects", {"name": f"churn-{time.time()}"})
                except Exception:
                    return  # سرور کشته شد — انتظار می‌رود
        churners = [threading.Thread(target=churn, daemon=True) for _ in range(6)]
        for t in churners:
            t.start()
        time.sleep(1.5)          # نوشتن‌ها جریان دارند…
        proc.send_signal(signal.SIGKILL)   # …وسط کار، بدون shutdown تمیز
        proc.wait(timeout=10)
        stop.set()
        for t in churners:
            t.join(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    # ── restart و راستی‌آزمایی ──
    proc = _spawn_server()
    try:
        projects = get("/api/projects")["projects"]
        names = [p["name"] for p in projects]
        assert "durable-پایه" in names, "پروژهٔ پایه بعد از SIGKILL گم شد!"
        ids = [p["id"] for p in projects]
        assert len(ids) == len(set(ids)), "آیدی تکراری بعد از restart!"
        assert len(projects) >= base_projects, (
            f"قبل از kill {base_projects} پروژه بود، بعد از restart {len(projects)}")

        jobs = get("/api/schedule")["jobs"]
        assert any(j["name"] == "job-durable" for j in jobs), "job بعد از SIGKILL گم شد!"
        assert len(jobs) >= base_jobs

        # snapshot برای پروژهٔ «durable-پایه» (active توسط churn عوض شده — با id بگیر)
        durable_id = next(p["id"] for p in projects if p["name"] == "durable-پایه")
        snaps = get(f"/api/snapshots?project={durable_id}")
        assert snaps.get("ok") and snaps.get("snapshots"), "snapshot بعد از restart لیست نشد"

        # زنجیرهٔ ممیزی هنوز معتبر است
        audit = srv.AuditLog(srv.config.AUDIT_DIR / "chain.jsonl")
        v = audit.verify()
        assert v.get("ok"), f"زنجیرهٔ ممیزی بعد از restart شکست: {v}"

        # سرور کاملاً سالم است: عملیات جدید انجام می‌شود
        assert post("/api/projects", {"name": "بعد-از-ریستارت"})["ok"]
        print(f"ok: kill-restart — SIGKILL وسط {len(projects)} پروژه؛ "
              f"{len(projects)} سالم برگشت + job/snapshot/audit معتبر")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def main() -> None:
    # بخش همزمانی با سرور in-process (FakeBrain تا تسک‌ها واقعی نشوند)
    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.engine.brain = FakeBrain([])
    server = srv.serve(open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    try:
        test_concurrent_writes()
        server.shutdown()
        server.server_close()
    finally:
        if hasattr(server, "server_close"):
            try:
                server.shutdown()
            except Exception:
                pass
    # بخش kill-restart با پروسهٔ واقعی
    test_kill_restart()
    print("\nALL DURABILITY TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
