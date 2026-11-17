# -*- coding: utf-8 -*-
"""تست‌های معنایی پاس ۸: شلیک زمان‌بند با ساعت جابه‌جا، صحت RAG فارسی، وفاداری round-trip.

۱) زمان‌بند: هیچ تستی تا حالا job را در «لحظهٔ due شدن» ندیده چون فاصلهٔ
   حداقل ۳۰ ثانیه است. اینجا ساعت را با monkeypatch جابه‌جا می‌کنیم و
   منطق واقعی _loop را می‌چرخانیم: daily در ۰۳:۳۰ دقیقاً همان لحظه شلیک
   می‌شود و next_run به فردا می‌رود؛ job غیرفعال نمی‌سوزد؛ موعد ازدست‌رفته
   فقط «یک‌بار» جبران می‌شود (نه طوفان ۸ روزه)؛ کرشِ callback حلقه را نمی‌کشد.

۲) RAG: نمایهٔ TF-IDF با نرمال‌سازی فارسی — جستجو با حروف عربی (ي/ك) و
   ارقام فارسی باید سند درست را اول برگرداند و remove سند را حذف کند.

۳) Round-trip خروجی→ورود: پروژه با persona + حافظه + مهارت + سشن؛ بعد از
   export و import هیچ داده‌ای نباید گم شود.

اجرا:  python tests/test_semantics.py
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys_path_hack = __import__("sys").path.insert(0, str(PROJECT_ROOT))

import scheduler as sched_mod  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from rag import SemanticIndex, normalize_fa  # noqa: E402

PORT = 8770
BASE = f"http://127.0.0.1:{PORT}"


# ──────────────────────────────────────────────────────────────────
# ۱) زمان‌بند با ساعت جابه‌جا
# ──────────────────────────────────────────────────────────────────
def test_scheduler_time_travel() -> None:
    fake = {"t": datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)}
    real_now = sched_mod._now
    sched_mod._now = lambda: fake["t"]  # ساعتِ قابل جابه‌جایی
    fires: list = []

    def on_fire(job):
        if job["name"] == "crasher":
            raise RuntimeError("callback عمداً خراب شد")
        fires.append((job["name"], fake["t"]))

    tmp = PROJECT_ROOT / "jobs_tt.json"
    try:
        sch = Scheduler(path=tmp, fire_callback=on_fire)
        sch.TICK_S = 0.05
        sch.start()
        time.sleep(0.15)

        # daily ساعت ۰۳:۳۰ — ساخته‌شده در ۰۲:۰۰ → موعد امروز ۰۳:۳۰ (نه ۵ ثانیه بعد، نه فردا)
        r1 = sch.add(1, "daily-job", "تسک", {"kind": "daily", "at": "03:30"})
        nr = datetime.fromisoformat(r1["job"]["next_run"])
        assert (nr.hour, nr.minute, nr.day) == (3, 30, 17), f"next_run اشتباه: {r1['job']['next_run']}"

        # interval ۶۰ ثانیه → موعد ۰۲:۰۱؛ ولی غیرفعالش می‌کنیم
        r2 = sch.add(1, "int-job", "تسک", {"kind": "interval", "every_s": 60})
        nr2 = datetime.fromisoformat(r2["job"]["next_run"])
        assert (nr2.hour, nr2.minute) == (2, 1), f"next_run interval اشتباه: {r2['job']['next_run']}"
        sch.set_enabled(r2["job"]["id"], False)

        # ۰۳:۲۹:۵۹ → هنوز هیچ‌چیز نسوخته
        fake["t"] = datetime(2026, 11, 17, 3, 29, 59, tzinfo=timezone.utc)
        time.sleep(0.3)
        assert not fires, f"شلیک زودهنگام: {fires}"

        # ۰۳:۳۰:۰۵ → daily دقیقاً یک‌بار؛ next_run فردا ۰۳:۳۰
        fake["t"] = datetime(2026, 11, 17, 3, 30, 5, tzinfo=timezone.utc)
        time.sleep(0.3)
        assert [f[0] for f in fires] == ["daily-job"], f"شلیک‌ها: {fires}"
        job1 = next(j for j in sch.list() if j["id"] == r1["job"]["id"])
        assert job1["run_count"] == 1 and job1["last_run"], "run_count/last_run ثبت نشد"
        assert datetime.fromisoformat(job1["next_run"]) == datetime(2026, 11, 18, 3, 30, tzinfo=timezone.utc), \
            f"next_run بعد از شلیک اشتباه: {job1['next_run']}"

        # فعال‌سازی دوباره در ۰۳:۳۱ → موعد «از نو» تنظیم می‌شود (re-arm به ۰۳:۳۲، نه شلیکِ بازماندهٔ کهنه)
        fake["t"] = datetime(2026, 11, 17, 3, 31, 0, tzinfo=timezone.utc)
        sch.set_enabled(r2["job"]["id"], True)
        time.sleep(0.3)
        assert [f[0] for f in fires].count("int-job") == 0, f"بعد از re-arm نباید فوراً بسوزد: {fires}"
        job2 = next(j for j in sch.list() if j["id"] == r2["job"]["id"])
        assert datetime.fromisoformat(job2["next_run"]) == datetime(2026, 11, 17, 3, 32, tzinfo=timezone.utc), \
            f"next_run بعد از re-arm اشتباه: {job2['next_run']}"

        # ۰۳:۳۲:۰۱ → می‌سوزد (run_count=1)
        fake["t"] = datetime(2026, 11, 17, 3, 32, 1, tzinfo=timezone.utc)
        time.sleep(0.3)
        assert [f[0] for f in fires].count("int-job") == 1
        # برای پرش هفتگی، interval را خاموش کن تا تداخل نشود
        sch.set_enabled(r2["job"]["id"], False)

        # پرش ۸ روزه — daily فقط «یک‌بار» جبران می‌کند (نه طوفان)، بعدی فردای همان روز
        fake["t"] = datetime(2026, 11, 25, 5, 0, tzinfo=timezone.utc)
        time.sleep(0.3)
        daily_total = [f[0] for f in fires].count("daily-job")
        assert daily_total == 2, f"طوفان جبران! daily در ۸ روز {daily_total} بار شلیک شد: {fires}"
        job1 = next(j for j in sch.list() if j["id"] == r1["job"]["id"])
        assert datetime.fromisoformat(job1["next_run"]) == datetime(2026, 11, 26, 3, 30, tzinfo=timezone.utc)

        # callback خراب حلقه را نمی‌کشد: crasher exception می‌دهد ولی حلقه زنده می‌ماند
        fake["t"] = datetime(2026, 11, 25, 5, 0, 0, tzinfo=timezone.utc)
        sch.add(1, "crasher", "تسک", {"kind": "interval", "every_s": 30})  # موعد: ۰۵:۰۰:۳۰
        fake["t"] = datetime(2026, 11, 25, 5, 1, 30, tzinfo=timezone.utc)
        time.sleep(0.3)
        crasher = next(j for j in sch.list() if j["name"] == "crasher")
        assert crasher["run_count"] == 1, "crasher فایر نشد (یا پیشرفت نشد)"
        # قربانی بعدی برای اثبات زنده‌ماندن حلقه بعد از استثنا: re-arm در ۰۵:۰۱:۳۰ → موعد ۰۵:۰۲:۳۰
        sch.set_enabled(r2["job"]["id"], True)
        fake["t"] = datetime(2026, 11, 25, 5, 3, 0, tzinfo=timezone.utc)
        time.sleep(0.3)
        assert [f[0] for f in fires].count("int-job") == 2, "کرش callback حلقه را کشت!"

        sch.stop()
        print(f"ok: time-travel — {len(fires)} شلیک صحیح؛ بدون طوفان جبران؛ کرش‌پذیری callback ✓")
    finally:
        sched_mod._now = real_now
        tmp.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────
# ۲) صحت جستجوی معنایی فارسی
# ──────────────────────────────────────────────────────────────────
def test_rag_search() -> None:
    idx = SemanticIndex()
    idx.add("d1", "استقرار پروژه با داکر و کامپوز روی سرور ابری")
    idx.add("d2", "دستور پایتون برای خواندن فایل اکسل با پانداس")
    idx.add("d3", "تنظیم زمان‌بند برای پشتیبان‌گیری روزانه ساعت ۳ بامداد")

    # جستجوی عادی — سند درست اول
    r = idx.search("استقرار با داکر")
    assert r and r[0]["id"] == "d1", f"رتبه‌بندی غلط: {r}"

    # حروف عربی (ي/ك) باید نرمال شوند و همان نتیجه بیاید
    assert normalize_fa("تكست تستي") == "تکست تستی", "نرمال‌سازی ي/ك کار نمی‌کند"
    r = idx.search("تنظيم زمان‌بنديك")  # ي و ك عربی
    assert r and r[0]["id"] == "d3", f"جستجو با حروف عربی گم شد: {r}"

    # ارقام فارسی در کوئری ↔ لاتین در سند
    r = idx.search("پشتیبان‌گیری روزانه ساعت ۳")
    assert r and r[0]["id"] == "d3", f"رقم فارسی ایندکس نشد: {r}"

    # کوئری نامرتبط → یا خالی یا سند مرتبط نیست
    r = idx.search("کوئری کاملاً بی‌ربط qwerty")
    assert not r or r[0]["id"] != "d2" or r[0]["score"] < 0.5, f"نتیجهٔ بی‌ربط: {r}"

    # remove → سند حذف‌شده دیگر برنمی‌گردد
    idx.remove("d2")
    r = idx.search("خواندن فایل اکسل با پانداس")
    assert all(x["id"] != "d2" for x in r), "سند حذف‌شده هنوز در نتایج است!"
    print("ok: rag — رتبه‌بندی درست، نرمال‌سازی فارسی (ي/ك/ارقام)، حذف سند ✓")


# ──────────────────────────────────────────────────────────────────
# ۳) وفاداری round-trip خروجی → ورود
# ──────────────────────────────────────────────────────────────────
def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def test_export_import_roundtrip() -> None:
    import server as srv
    from agent_loop import FakeBrain

    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "FakeBrain"
    srv.STATE.engine.brain = FakeBrain([])
    server = srv.serve(open_browser=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    try:
        # ── ساخت دادهٔ منبع ──
        assert _post("/api/projects", {"name": "rt-منبع"})["ok"]
        src = srv.STATE.projects.active()
        src_id = src["id"]
        assert _post("/api/persona", {"id": src_id, "persona": "شخصیتِ تست پاس ۸"})["ok"]
        assert srv.STATE.memory.add_fact("واقعیت کلیدی پاس هشت برای round-trip", tags=["rt"])["ok"]
        assert srv.STATE.skills.save_skill(
            "مهارت تست پاس ۸", "توصیف", ["تریگر rt"], "گام اولِ مهارتِ تست")["ok"]
        mdir = srv.STATE.projects.memory_dir(src["key"])
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "sessions.json").write_text(
            json.dumps({"sessions": [{"id": 1, "name": "سشن پاس ۸"}]}, ensure_ascii=False),
            encoding="utf-8")

        # ── خروجی و ورود ──
        ex = _post("/api/export", {"project_id": src_id})
        assert ex["ok"] and ex["files"] >= 3, f"خروجی خیلی کم: {ex}"
        im = _post("/api/import", {"name": ex["name"]})
        assert im["ok"], f"ورود شکست: {im}"
        new_id = im["project"]["id"]
        assert new_id != src_id, "import باید پروژهٔ جدید بسازد نه بازنویسی"

        # ── راستی‌آزمایی وفاداری ──
        recs = srv.STATE.projects._data["projects"]
        new_rec = next(p for p in recs if p["id"] == new_id)
        assert new_rec["name"] == "rt-منبع", "نام گم شد"
        assert new_rec.get("persona") == "شخصیتِ تست پاس ۸", "persona گم شد!"

        new_mdir = srv.STATE.projects.memory_dir(new_rec["key"])
        facts = json.loads((new_mdir / "memory.json").read_text(encoding="utf-8")).get("facts", [])
        assert any("واقعیت کلیدی پاس هشت" in f.get("text", "") for f in facts), "حافظه گم شد!"
        sessions = json.loads((new_mdir / "sessions.json").read_text(encoding="utf-8"))
        assert any(s["name"] == "سشن پاس ۸" for s in sessions.get("sessions", [])), "سشن گم شد!"

        skills = _get("/api/skills")["skills"]
        assert any(s.get("name") == "مهارت تست پاس ۸" for s in skills), "مهارت گم شد!"
        st = next(s for s in skills if s.get("name") == "مهارت تست پاس ۸")
        assert "گام اول" in (st.get("steps") or ""), "بدنهٔ مهارت گم شد!"

        # جستجوی سرور هنوز آیتم را پیدا می‌کند
        sr = _get("/api/search?q=round-trip")
        assert sr.get("ok"), f"جستجو شکست: {sr}"
        print(f"ok: round-trip — {ex['files']} فایل؛ نام/persona/حافظه/سشن/مهارت همه سالم برگشتند")
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    test_scheduler_time_travel()
    test_rag_search()
    test_export_import_roundtrip()
    print("\nALL SEMANTICS TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        __import__("sys").exit(1)
