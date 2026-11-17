# -*- coding: utf-8 -*-
"""تست‌های پاس ۹: فاز آرگومان‌های خصمانهٔ ابزارها، انقضای تایید، جستجوی سراسری.

۱) لایهٔ ابزار (سطح LLM): فاز پاس ۶ endpoint های HTTP را کوبید ولی ابزارهایی که
   «مدل» صدا می‌زند هرگز با ورودی خراب آزموده نشده بودند. اینجا همهٔ ابزارهای
   رجیستری واقعی با آرگومان‌های خصمانه (None/نوع غلط/نامعلوم/بایت خام/پیمایش
   مسیر/رشتهٔ ۱۰KB/ایموجی) اجرا می‌شوند — هیچ استثنایی نباید از execute فرار کند
   و هیچ فایلی نباید از زندان workspace بیرون بیفتد.

۲) انقضای تایید انسانی: مسیر TTL (۶۰۰s در عمل، اینجا کوتاه) — درخواست بی‌تصمیم
   باید به «timeout» ختم شود، تاییدِ دیرهنگام باید رد شود، و وضعیت باید ماندگار باشد.

۳) جستجوی سراسری: بین پروژه‌های متعدد (بدون سوییچ پروژهٔ فعال) + فیلتر نوع/
   پروژه + تحمل فایل خراب.

اجرا:  python tests/test_toolfuzz.py
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import tool_registry  # noqa: E402
from approval import ApprovalManager  # noqa: E402
from globalsearch import search_all  # noqa: E402

MARKER = "fz-escape-marker-9q"


# ──────────────────────────────────────────────────────────────────
# ۱) فاز آرگومان‌های خصمانه روی کل رجیستری
# ──────────────────────────────────────────────────────────────────
def test_tool_args_fuzz() -> None:
    import server as srv
    from agent_loop import FakeBrain

    srv.STATE.deepseek_ready = True
    srv.STATE.deepseek_started = True
    srv.STATE.deepseek_message = "FakeBrain"
    srv.STATE.engine.brain = FakeBrain([])
    reg = srv.STATE._make_registry(None, srv.STATE.memory, srv.STATE.skills)
    names = reg.names()
    assert len(names) >= 15, f"رجیستری ناقص است؟ فقط {len(names)} ابزار: {names}"

    # ابزارهای خطرناک باید از دروازهٔ تایید رد شوند — برای سرعت تست، تایم‌اوت را ۱ ثانیه می‌کنیم
    real_timeout = tool_registry.APPROVAL_TIMEOUT
    tool_registry.APPROVAL_TIMEOUT = 1

    bad_args: list = [
        {},  # بدون هیچ آرگومانی
        {"path": None, "content": None, "name": None, "query": None, "text": None, "id": None},
        {"path": "../../" + MARKER + ".txt", "content": "x",
         "name": "../" + MARKER, "file": "/etc/passwd", "query": "q", "text": "t", "id": -999},
        {"zzz_unknown": b"\x00\xff\xfe", "n": [1, {"deep": None}]},   # بایت خام (غیر-JSON)
        {"query": "فارسی 💥🇮🇷", "text": "x" * 10_000, "name": "\u200f RTL", "path": "a\x00b"},
    ]
    calls = timeouts = errors = 0
    escaped: list = []
    try:
        for name in names:
            for args in bad_args:
                try:
                    r = reg.execute(name, dict(args))
                except Exception as e:  # استثنا از execute فرار کرد = باگ واقعی
                    escaped.append(f"{name}: {type(e).__name__}: {e}")
                    continue
                calls += 1
                if not isinstance(r, dict) or "ok" not in r:
                    escaped.append(f"{name}: خروجی ساخت‌یافته نیست: {r!r:.80}")
                if r.get("error") and "تایید انسانی" in str(r.get("error", "")):
                    timeouts += 1
                elif r.get("ok") is False:
                    errors += 1
        assert not escaped, "استثنا/خروجی خام از execute فرار کرد:\n" + "\n".join(escaped[:8])
    finally:
        tool_registry.APPROVAL_TIMEOUT = real_timeout

    # هیچ فایلی از زندان workspace بیرون نیفتاده باشد
    ws_root = PROJECT_ROOT / "workspace"
    for candidate in (PROJECT_ROOT / f"{MARKER}.txt", PROJECT_ROOT.parent / f"{MARKER}.txt",
                      PROJECT_ROOT / MARKER, Path(f"/tmp/{MARKER}.txt")):
        assert not candidate.exists(), f"فرار از زندان فایل‌سیستم: {candidate}"
    leaks = [str(p) for p in ws_root.rglob(f"*{MARKER}*")] if ws_root.exists() else []
    assert not leaks, f"نوشتهٔ خصمانه داخل workspace هم نباید جا شود: {leaks}"

    print(f"ok: toolfuzz — {len(names)} ابزار × {len(bad_args)} ورودی خصمانه = {calls} اجرا؛ "
          f"{errors} خطای ساخت‌یافته، {timeouts} عبور از دروازهٔ تایید؛ صفر استثنا/فرار")


# ──────────────────────────────────────────────────────────────────
# ۲) انقضای تایید انسانی (TTL)
# ──────────────────────────────────────────────────────────────────
def test_approval_ttl() -> None:
    tmp = Path("/tmp") / "devx-approval-ttl-test"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    store = ApprovalManager(path=tmp / "a.json", ttl_s=1)

    # بی‌تصمیم → timeout (مسیر ساعت‌محور؛ تا حالا تست نشده بود)
    req = store.request("delete_file", {"path": "x"}, project=3)
    r = store.wait(req["id"])          # ttl_s=1 از خود استور
    assert r["status"] == "timeout" and r["ok"] is False, f"انقضا کار نکرد: {r}"
    assert not store.pending(), "درخواست منقضی باید از pending خارج شود"

    # تاییدِ دیرهنگام روی درخواستِ منقضی → رد باید شود
    late = store.resolve(req["id"], approved=True)
    assert not late.get("ok"), f"تایید بعد از انقضا نباید پذیرفته شود: {late}"

    # مسیر خوش‌بختی: تصمیم همزمان با انتظار (نخ جدا)
    req2 = store.request("run_code", {"code": "x"}, project=3)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(store.wait(req2["id"])))
    t.start()
    time.sleep(0.2)
    assert store.resolve(req2["id"], approved=True)["ok"]
    t.join(timeout=5)
    assert out.get("status") == "approved" and out.get("ok"), f"تایید همزمان نرسید: {out}"

    # ماندگاری: بعد از reload، وضعیت‌ها (timeout/approved) حفظ شوند
    store2 = ApprovalManager(path=tmp / "a.json", ttl_s=60)
    hist = store2.history()
    statuses = {h.get("status") for h in hist}
    assert {"timeout", "approved"} <= statuses, f"وضعیت‌ها بعد از reload گم شد: {hist}"
    assert not store2.pending(), "pending بعد از reload باید خالی باشد"
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok: approval-ttl — انقضا، ردِ تایید دیرهنگام، تصمیم همزمان، ماندگاری ✓")


# ──────────────────────────────────────────────────────────────────
# ۳) جستجوی سراسری بین پروژه‌ها
# ──────────────────────────────────────────────────────────────────
def test_globalsearch() -> None:
    tmp = Path("/tmp") / "devx-gs-test"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "memory" / "projects").mkdir(parents=True)
    (tmp / "skills" / "projects").mkdir(parents=True)
    projects = [
        {"id": 1, "key": "k1", "name": "پروژهٔ الف"},
        {"id": 2, "key": "k2", "name": "پروژهٔ ب"},
        {"id": 3, "key": "k3", "name": "خراب"},  # حافظهٔ JSON خراب → باید تحمل شود
    ]
    for key, fact in (("k1", "یادداشت مهم دربارهٔ هوانوردی در پروژهٔ الف"),
                      ("k2", "یادداشت مهم دربارهٔ هوانوردی در پروژهٔ ب")):
        d = tmp / "memory" / "projects" / key
        d.mkdir()
        (d / "memory.json").write_text(
            json.dumps({"facts": [{"id": 1, "text": fact, "ts": "2026-11-17T00:00:00+00:00"}]},
                       ensure_ascii=False), encoding="utf-8")
    k3 = tmp / "memory" / "projects" / "k3"
    k3.mkdir()
    (k3 / "memory.json").write_text("{not json!!", encoding="utf-8")
    sdir = tmp / "skills" / "projects" / "k1"
    sdir.mkdir()
    (sdir / "hov-pilot.md").write_text("name: مهارت خلبانی\nتوضیح: یادداشت مهم دربارهٔ هوانوردی", encoding="utf-8")

    # جستجو در هر دو پروژه (بدون سوییچ پروژهٔ فعال)
    r = search_all(projects, tmp / "memory", tmp / "skills", "هوانوردی")
    assert r["ok"] and r["count"] >= 3, f"نتایج ناقص: {r}"
    got_projects = {x["project_id"] for x in r["results"]}
    assert got_projects == {1, 2}, f"جستجوی سراسری پروژه‌ای را جا انداخت: {got_projects}"
    kinds = {x["kind"] for x in r["results"]}
    assert "fact" in kinds and "skill" in kinds, f"نوع‌ها: {kinds}"

    # فیلتر نوع
    r = search_all(projects, tmp / "memory", tmp / "skills", "هوانوردی", kind="skill")
    assert r["ok"] and r["count"] == 1 and r["results"][0]["kind"] == "skill", f"فیلتر نوع: {r}"

    # فیلتر پروژه
    r = search_all(projects, tmp / "memory", tmp / "skills", "هوانوردی", project_id=2)
    assert r["ok"] and r["count"] >= 1 and all(x["project_id"] == 2 for x in r["results"]), f"فیلتر پروژه: {r}"

    # کوئری خالی → خطای ساخت‌یافته (نه کرش)
    r = search_all(projects, tmp / "memory", tmp / "skills", "   ")
    assert not r["ok"] and "error" in r, f"کوئری خالی: {r}"
    shutil.rmtree(tmp, ignore_errors=True)
    print("ok: globalsearch — بین‌پروژه‌ای، فیلتر نوع/پروژه، تحمل JSON خراب ✓")


def main() -> None:
    test_tool_args_fuzz()
    test_approval_ttl()
    test_globalsearch()
    print("\nALL TOOL-LAYER TESTS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
