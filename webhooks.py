# -*- coding: utf-8 -*-
"""نگهبان مخزن (Webhook Watcher) — نظرسنجی رویدادهای گیت‌هاب و واکنش خودکار.

هر watch: {id, project_id, repo, events:[issues,prs,commits], interval_s,
last_state, auto_task؟}. نخ پس‌زمینه هر(interval_s) مخزن را می‌سنجد:
  * issues  → تعداد/آخرین شمارهٔ issueهای باز
  * prs     → آخرین شمارهٔ PRهای باز
  * commits → SHA آخرین کامیت شاخهٔ پیش‌فرض
تغییر نسبت به last_state → اعلان + (اختیاری) اجرای خودکار `auto_task` در پروژهٔ
مربوط با متن رویداد — مثلاً «ایسو #۱۲ جدید باز شده، خلاصه‌ش کن و برچسب بزن».
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class WebhookWatcher:
    """نظرسنجی سبک رویدادهای مخزن‌ها (بدون وب‌هوک عمومی — همه‌چیز محلی)."""

    def __init__(
        self,
        path: Optional[Path] = None,
        gh_provider: Optional[Callable[[], Any]] = None,
        on_change: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
    ) -> None:
        self.path = Path(path)
        self.gh_provider = gh_provider          # تابعی که GitHubTools زنده برمی‌گرداند یا None
        self.on_change = on_change              # (watch, change_dict)
        self._lock = threading.RLock()
        self._watches: List[Dict[str, Any]] = []
        self._next_id = 1
        self._next_poll: Dict[int, float] = {}  # سرِ موعدِ per-watch (monotonic) — باگ قبلی: همه هر سیکل پول می‌شدند
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._watches = raw.get("watches", [])
            self._next_id = int(raw.get("next_id", len(self._watches) + 1))
        except Exception:
            self._watches, self._next_id = [], 1

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self._next_id, "watches": self._watches}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------------

    def add(self, project_id: int, repo: str, events: List[str], interval_s: int = 300, auto_task: str = "") -> Dict[str, Any]:
        repo = (repo or "").strip().strip("/")
        if "/" not in repo:
            return {"ok": False, "error": "repo باید owner/name باشد"}
        events = [e for e in (events or ["issues", "prs", "commits"]) if e in ("issues", "prs", "commits")]
        with self._lock:
            w = {
                "id": self._next_id, "project_id": int(project_id), "repo": repo,
                "events": events, "interval_s": max(60, int(interval_s)),
                "auto_task": (auto_task or "")[:300], "last_state": {}, "last_change": None,
            }
            self._next_id += 1
            self._watches.append(w)
            self.save()
        return {"ok": True, "watch": w}

    def remove(self, watch_id: int) -> bool:
        with self._lock:
            before = len(self._watches)
            self._watches = [w for w in self._watches if w["id"] != int(watch_id)]
            self._next_poll.pop(int(watch_id), None)
            self.save()
            return len(self._watches) < before

    def list(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(w) for w in self._watches]
        if project_id is not None:
            items = [w for w in items if w["project_id"] == int(project_id)]
        return items

    # ------------------------------------------------------------------

    def poll_once(self, watch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """یک بار وضعیت مخزن را بخوان و تغییرها را برگردان (None = بدون تغییر/خطا).

        نکته: به‌روزرسانی last_state باید روی رکورد واقعیِ ذخیره‌شده انجام شود
        نه روی کپی — وگرنه هر بار «اولین سنجش» تفسیر می‌شود و هیچ‌وقت اعلانی
        نمی‌آید (باگ نسخهٔ قبل). متدهای gh مستقیم صدا زده می‌شوند (نه از طریق
        رجیستری)، پس خروجی کلید ok ندارد و مستقیماً کلید داده خوانده می‌شود.
        """
        gh = self.gh_provider() if self.gh_provider else None
        if gh is None:
            return None
        state: Dict[str, Any] = {}
        try:
            if "issues" in watch["events"]:
                res = gh.list_issues(watch["repo"], state="open", limit=30)
                issues = res.get("issues", [])
                state["issues_open"] = len(issues)
                state["issues_latest"] = issues[0].get("number") if issues else None
            if "prs" in watch["events"]:
                res = gh.list_pull_requests(watch["repo"], state="open", limit=30)
                prs = res.get("pull_requests", [])
                state["prs_open"] = len(prs)
                state["prs_latest"] = prs[0].get("number") if prs else None
            if "commits" in watch["events"]:
                res = gh.list_commits(watch["repo"], limit=1)
                cs = res.get("commits", [])
                state["commit_latest"] = (cs[0].get("sha") or "")[:10] if cs else None
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

        # رکورد واقعی در self._watches پیدا و به‌روزرسانی شود (کپی ورودی کافی نیست)
        with self._lock:
            real = next((w for w in self._watches if w.get("id") == watch.get("id")), None)
            if real is None:
                return None  # بین کپی و پول حذف شده
            change = {k: v for k, v in state.items() if real.get("last_state", {}).get(k) != v}
            if not real.get("last_state"):
                # اولین سنجش → فقط ذخیره، بدون اعلان
                real["last_state"] = state
                self.save()
                return None
            if change:
                change_obj = {"from": real.get("last_state"), "to": state}
                real["last_state"] = state
                real["last_change"] = change_obj
                self.save()
                return change_obj
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                watches = [dict(w) for w in self._watches]
            now = time.monotonic()
            for w in watches:
                if self._stop.is_set():
                    break
                # سرِ موعد per-watch: هر watch فقط با interval_s خودش پول می‌شود
                if now < self._next_poll.get(w["id"], 0.0):
                    continue
                self._next_poll[w["id"]] = now + max(60, int(w.get("interval_s", 300)))
                try:
                    change = self.poll_once(w)
                except Exception:
                    change = None
                if change and not change.get("error") and self.on_change:
                    try:
                        self.on_change(w, change)
                    except Exception:
                        pass
            # بیدار شدن حداکثر تا نزدیک‌ترین سرِ موعد (با سقف ۳۰۰ ثانیه)
            try:  # remove() همزمان از request thread → RuntimeError وسط تکرار
                pending = [t - time.monotonic() for t in self._next_poll.values()]
            except RuntimeError:
                pending = []
            wait_s = min(pending) if pending else 300.0
            self._stop.wait(max(5.0, min(wait_s, 300.0)))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="devx-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
