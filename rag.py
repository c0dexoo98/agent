# -*- coding: utf-8 -*-
"""جستجوی برداری سبک (RAG Lexical-Vector) — بازیابی معنایی بدون نیاز به embedding خارجی.

ایده: هر آیتم حافظه یک «سند» است؛ برای هر سند بردار TF-IDF (زیرخطی) ساخته می‌شود و
جستجو با شباهت کسینوسی انجام می‌شود. برای فارسی:
  * نرمال‌سازی: ی/ک عربی→فارسی، حذف اِعراب، نیم‌فاصله→فاصله، یکدست‌سازی ارقام
  * ریشه‌سازی سبک: حذف پسوندهای پرتکرار (های، ترین، ...) تا «مهارت‌ها» با «مهارت» بخواند
مزیت نسبت به جستجوی کلیدواژه‌ای ساده: وزن‌دهی IDF (کلمات نادرِ مهم‌تر) + نرمال‌سازی
طول سند + هم‌پوشانی جزئی چندکلمه‌ای. همه‌چیز آفلاین، بدون وابستگی، ماندگار در JSON.
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

_SUFFIXES = ("های", "ترین", "هایی", "ها", "تر", "مانند", "وار")


def normalize_fa(text: str) -> str:
    """نرمال‌سازی متن فارسی/انگلیسی برای نمایه‌سازی."""
    t = (text or "").lower()
    t = t.replace("\u200c", " ").replace("\u064a", "ی").replace("\u0643", "ک")
    t = re.sub(r"[\u064B-\u0652\u0670]", "", t)          # اِعراب
    t = re.sub(r"[\u06F0-\u06F9]", lambda m: str("۰۱۲۳۴۵۶۷۸۹".index(m.group(0))), t)   # ارقام فارسی
    t = re.sub(r"[\u0660-\u0669]", lambda m: str("٠١٢٣٤٥٦٧٨٩".index(m.group(0))), t)   # ارقام عربی
    return t


def _terms(text: str) -> List[str]:
    words = re.findall(r"[\w\u0600-\u06FF]{2,}", normalize_fa(text))
    out: List[str] = []
    for w in words:
        out.append(w)
        for suf in _SUFFIXES:                            # ریشه‌سازی سبک — اولین پسوندِ چسبیده
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                out.append(w[: len(w) - len(suf)])
                break
    return out


class SemanticIndex:
    """نمایهٔ برداری ماندگار اسناد حافظه (TF-IDF + کسینوس)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._docs: Dict[str, str] = {}
        self._tf: Dict[str, Counter] = {}
        self._df: Counter = Counter()
        self._n = 0
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._docs = raw.get("docs", {})
            self._tf = {k: Counter(v) for k, v in raw.get("tf", {}).items()}
            self._df = Counter(raw.get("df", {}))
            self._n = int(raw.get("n", 0))
        except Exception:
            self._docs, self._tf, self._df, self._n = {}, {}, Counter(), 0

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"docs": self._docs, "tf": {k: dict(v) for k, v in self._tf.items()},
                     "df": dict(self._df), "n": self._n},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # ------------------------------------------------------------------

    @staticmethod
    def _idf(df: int, n: int) -> float:
        return math.log((n + 1) / (df + 1)) + 1.0

    def add(self, doc_id: str, text: str) -> None:
        with self._lock:
            old = self._tf.get(str(doc_id))
            if old:
                for t in old:
                    self._df[t] -= 1
                    if self._df[t] <= 0:
                        del self._df[t]
                self._n -= 1
            tf = Counter(_terms(text))
            self._docs[str(doc_id)] = (text or "")[:500]
            self._tf[str(doc_id)] = tf
            for t in tf:
                self._df[t] += 1
            self._n += 1

    def remove(self, doc_id: str) -> None:
        with self._lock:
            doc_id = str(doc_id)
            tf = self._tf.pop(doc_id, None)
            if tf:
                for t in tf:
                    self._df[t] -= 1
                    if self._df[t] <= 0:
                        del self._df[t]
                self._docs.pop(doc_id, None)
                self._n -= 1

    def search(self, query: str, k: int = 5, min_score: float = 0.06) -> List[Dict[str, Any]]:
        """بازگشت بهترین اسناد بر اساس شباهت کسینوسی: [{id, text, score}]"""
        qtf = Counter(_terms(query))
        if not qtf or self._n == 0:
            return []
        qvec = {t: (1 + math.log(c)) * self._idf(self._df.get(t, 0), self._n) for t, c in qtf.items()}
        qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
        scored: List[Dict[str, Any]] = []
        with self._lock:
            for doc_id, tf in self._tf.items():
                dot = 0.0
                dnorm = 0.0
                for t, c in tf.items():
                    w = (1 + math.log(c)) * self._idf(self._df.get(t, 0), self._n)
                    dnorm += w * w
                    if t in qvec:
                        dot += w * qvec[t]
                if dot <= 0 or dnorm <= 0:
                    continue
                score = dot / (math.sqrt(dnorm) * qnorm)
                if score >= min_score:
                    scored.append({"id": doc_id, "text": self._docs.get(doc_id, ""), "score": round(score, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[: max(1, int(k))]
