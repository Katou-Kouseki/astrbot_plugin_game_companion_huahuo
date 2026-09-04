from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

try:
    from .undercover import DEFAULT_SEED_WORDS
except ImportError:
    from undercover import DEFAULT_SEED_WORDS  # type: ignore[no-redef]


class UndercoverWordStore:
    """JSON 持久化的谁是卧底词库（不使用 SQL，与 AstrBot 数据目录风格一致）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 已用词对统计：跨局去重，避免 LLM/本地取词反复抽到同一对
        self._used_path = self.path.with_name(self.path.stem + "_used.json")
        self.ensure_defaults()

    # ---------------- file IO ----------------

    def _read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return data
            return []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write(self, items: list[dict[str, Any]]) -> None:
        text = json.dumps(items, ensure_ascii=False, indent=2)
        self.path.write_text(text, encoding="utf-8")

    def ensure_defaults(self) -> None:
        existing = self._read()
        if existing:
            return
        seed: list[dict[str, Any]] = []
        next_id = 1
        for word1, word2 in DEFAULT_SEED_WORDS:
            seed.append(
                {
                    "id": next_id,
                    "word1": str(word1),
                    "word2": str(word2),
                }
            )
            next_id += 1
        self._write(seed)

    # ---------------- public API ----------------

    def list_all(self) -> list[dict[str, Any]]:
        return self._read()

    def random_pair(self, exclude: set[tuple[str, str]] | None = None) -> tuple[str, str]:
        items = self._read()
        if not items:
            raise RuntimeError("谁是卧底词库为空，请先在游戏管理台添加词条")
        banned = {tuple(p) for p in (exclude or set())}
        # 优先从未用过的词对里挑；词库被用过一轮后允许复用，避免无词可用
        for _attempt in range(30):
            pick = random.choice(items)
            pair = (str(pick.get("word1") or ""), str(pick.get("word2") or ""))
            if pair not in banned:
                return pair
        pick = random.choice(items)
        return str(pick.get("word1") or ""), str(pick.get("word2") or "")

    # ---------------- used-pair stats (cross-game dedup) ----------------

    def _read_used(self) -> list[dict[str, Any]]:
        try:
            with self._used_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return data
            return []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_used(self, items: list[dict[str, Any]]) -> None:
        text = json.dumps(items, ensure_ascii=False, indent=2)
        self._used_path.write_text(text, encoding="utf-8")

    def mark_used(self, word1: str, word2: str) -> None:
        """记录一对词条已被选用（含次数与最近使用时间），供跨局去重使用。"""
        w1 = str(word1 or "").strip()
        w2 = str(word2 or "").strip()
        if not w1 or not w2:
            return
        items = self._read_used()
        found = None
        for item in items:
            a = str(item.get("word1") or "")
            b = str(item.get("word2") or "")
            if {a, b} == {w1, w2}:
                found = item
                break
        if found is None:
            items.append(
                {
                    "word1": w1,
                    "word2": w2,
                    "times": 1,
                    "last_used_at": time.time(),
                }
            )
        else:
            found["times"] = int(found.get("times") or 0) + 1
            found["last_used_at"] = time.time()
        # 只保留最近 500 条，避免文件无限膨胀
        del items[:-500]
        self._write_used(items)

    def used_pairs(self, limit: int = 40) -> list[tuple[str, str]]:
        """按最近使用时间倒序返回 limit 对已用词条。"""
        items = self._read_used()
        items.sort(key=lambda it: float(it.get("last_used_at") or 0), reverse=True)
        result: list[tuple[str, str]] = []
        for it in items[: max(1, int(limit))]:
            w1 = str(it.get("word1") or "").strip()
            w2 = str(it.get("word2") or "").strip()
            if w1 and w2:
                result.append((w1, w2))
        return result

    def _exists(self, word1: str, word2: str) -> bool:
        items = self._read()
        for item in items:
            w1 = str(item.get("word1") or "")
            w2 = str(item.get("word2") or "")
            if {w1, w2} == {word1, word2}:
                return True
        return False

    def add(self, word1: str, word2: str) -> dict[str, Any] | None:
        w1 = str(word1 or "").strip()
        w2 = str(word2 or "").strip()
        if not w1 or not w2:
            return None
        if len(w1) < 2 or len(w2) < 2:
            return None
        if len(w1) > 10 or len(w2) > 10:
            return None
        if w1 == w2:
            return None
        if self._exists(w1, w2):
            return None
        items = self._read()
        new_id = max((int(item.get("id") or 0) for item in items), default=0) + 1
        record = {"id": new_id, "word1": w1, "word2": w2}
        items.append(record)
        self._write(items)
        return record

    def delete(self, item_id: int) -> bool:
        target = int(item_id)
        items = self._read()
        filtered = [item for item in items if int(item.get("id") or 0) != target]
        if len(filtered) == len(items):
            return False
        self._write(filtered)
        return True
