"""往期生成的题目：模型现编、通过校验的题，存一份给以后复用。

为什么要单独存：

* 模型每出一题要花一次生成 + 一次自检调用，同一道题下回再编一次纯属浪费；
* 管理台得能看见「模型到底出了些什么」，不合适的题要能删掉或改掉；
* 内置题库随插件分发、升级会被覆盖，生成的题必须落在数据目录里。

只存能通过 `puzzle_from_mapping` 的完整题目（含关键事实与提示），所以复用时
不需要再让模型补全，出题是瞬时的。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .turtle_soup import SoupPuzzle, normalize_content_level, puzzle_from_mapping

# 单份存档上限：够玩很久，也不至于把 data_dir 撑大
MAX_ENTRIES = 300
_LEVEL_ORDER = {"all_ages": 0, "normal": 1, "unrestricted": 2}


def _puzzle_mapping(puzzle: SoupPuzzle) -> dict[str, Any]:
    return {
        "title": puzzle.title,
        "surface": puzzle.surface,
        "solution": puzzle.solution,
        "key_facts": list(puzzle.key_facts),
        "acceptable_variants": list(puzzle.acceptable_variants),
        "hints": list(puzzle.hints),
        "content_level": puzzle.content_level,
        "theme": puzzle.theme,
        "trick": puzzle.trick,
    }


class GeneratedSoupStore:
    """模型出过的题目（JSON 持久化，进程内缓存）。"""

    def __init__(self, path: Path, *, limit: int = MAX_ENTRIES) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self.items: list[dict[str, Any]] = []
        self.load()

    # ---------------- IO ----------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.items = []
            return
        rows = raw.get("items") if isinstance(raw, dict) else raw
        items: list[dict[str, Any]] = []
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            mapping = row.get("puzzle") if isinstance(row.get("puzzle"), dict) else row
            try:
                puzzle_from_mapping(
                    mapping, content_level=normalize_content_level(mapping.get("content_level"))
                )
            except (ValueError, TypeError):
                # 存坏了、或旧版本字段不全：跳过，别让整份存档作废
                continue
            items.append(
                {
                    "puzzle": dict(mapping),
                    "created_at": float(row.get("created_at") or 0.0),
                    "last_used_at": float(row.get("last_used_at") or 0.0),
                    "disabled": bool(row.get("disabled")),
                    # 老存档没有这个字段，读成空串即可，不要求迁移
                    "model": str(row.get("model") or "")[:120],
                }
            )
        self.items = items[-self.limit :]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"version": 1, "items": self.items},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:  # 磁盘只读不该让出题直接崩掉
            pass

    # ---------------- 查询 ----------------

    def __len__(self) -> int:
        return len(self.items)

    def _build(self, row: dict[str, Any]) -> SoupPuzzle | None:
        mapping = row.get("puzzle") or {}
        try:
            return puzzle_from_mapping(
                mapping, content_level=normalize_content_level(mapping.get("content_level"))
            )
        except (ValueError, TypeError):
            return None

    def puzzles(self, max_content_level: str = "unrestricted") -> list[SoupPuzzle]:
        """可复用的题目：停用的、超出内容等级的不给。"""
        ceiling = _LEVEL_ORDER.get(normalize_content_level(max_content_level), 2)
        result: list[SoupPuzzle] = []
        for row in self.items:
            if row.get("disabled"):
                continue
            mapping = row.get("puzzle") or {}
            level = normalize_content_level(mapping.get("content_level"))
            if _LEVEL_ORDER.get(level, 1) > ceiling:
                continue
            puzzle = self._build(row)
            if puzzle is not None:
                result.append(puzzle)
        return result

    def all_puzzles(self) -> list[SoupPuzzle]:
        """判重专用：包含停用题在内的全部生成题。

        `puzzles()` 是「可以上桌的题」，会跳过停用的；但管理员停用一道题
        往往正因为它重复或质量差，停用不等于允许模型照搬同一个故事再存一份。
        """
        result: list[SoupPuzzle] = []
        for row in self.items:
            puzzle = self._build(row)
            if puzzle is not None:
                result.append(puzzle)
        return result

    def unused(self, max_content_level: str = "unrestricted") -> list[SoupPuzzle]:
        """还没出过的题（预生成的目标就是让这些保持够用）。

        `last_used_at > 0` 说明至少已经上过一次桌：账本还会按近 12 道去重，
        所以它们基本用不上，计入「备用」只会让预生成误以为池子还满。
        """
        ceiling = _LEVEL_ORDER.get(normalize_content_level(max_content_level), 2)
        result: list[SoupPuzzle] = []
        for row in self.items:
            if row.get("disabled") or float(row.get("last_used_at") or 0.0) > 0:
                continue
            mapping = row.get("puzzle") or {}
            level = normalize_content_level(mapping.get("content_level"))
            if _LEVEL_ORDER.get(level, 1) > ceiling:
                continue
            puzzle = self._build(row)
            if puzzle is not None:
                result.append(puzzle)
        return result

    def signatures(self) -> set[str]:
        return {
            puzzle.signature
            for puzzle in (self._build(row) for row in self.items)
            if puzzle is not None
        }

    def get(self, index: int) -> dict[str, Any] | None:
        position = int(index) - 1
        if 0 <= position < len(self.items):
            return self.items[position]
        return None

    def listing(self) -> list[dict[str, Any]]:
        """管理台列表用：序号从 1 开始，按入库顺序。"""
        rows: list[dict[str, Any]] = []
        for position, row in enumerate(self.items, start=1):
            mapping = row.get("puzzle") or {}
            rows.append(
                {
                    "index": position,
                    "title": str(mapping.get("title") or ""),
                    "surface": str(mapping.get("surface") or ""),
                    "solution": str(mapping.get("solution") or ""),
                    "content_level": normalize_content_level(mapping.get("content_level")),
                    "created_at": float(row.get("created_at") or 0.0),
                    "last_used_at": float(row.get("last_used_at") or 0.0),
                    "disabled": bool(row.get("disabled")),
                    "model": str(row.get("model") or "")[:120],
                }
            )
        return rows

    def stats(self) -> dict[str, Any]:
        counts = {"all_ages": 0, "normal": 0, "unrestricted": 0}
        by_model: dict[str, int] = {}
        disabled = 0
        unused = 0
        for row in self.items:
            level = normalize_content_level((row.get("puzzle") or {}).get("content_level"))
            counts[level] = counts.get(level, 0) + 1
            model = str(row.get("model") or "").strip() or "未记录"
            by_model[model] = by_model.get(model, 0) + 1
            if row.get("disabled"):
                disabled += 1
            elif float(row.get("last_used_at") or 0.0) <= 0:
                unused += 1
        return {
            "total": len(self.items),
            "available": len(self.items) - disabled,
            "disabled": disabled,
            "unused": unused,
            "by_level": counts,
            "by_model": by_model,
            "limit": self.limit,
        }

    # ---------------- 增删改 ----------------

    def add(self, puzzle: SoupPuzzle, *, model: str = "") -> bool:
        """存一道模型出的题；同一道题（主题+诡计）只留一份。

        `model` 记下到底是哪家哪个模型出的，管理台按模型展示，方便对比质量。
        旧调用不传也能用（老题显示「未记录」）。
        """
        if puzzle.signature in self.signatures():
            return False
        self.items.append(
            {
                "puzzle": _puzzle_mapping(puzzle),
                "created_at": time.time(),
                "last_used_at": 0.0,
                "disabled": False,
                "model": str(model or "").strip()[:120],
            }
        )
        # 超上限时丢最旧的：新题更值得留
        self.items = self.items[-self.limit :]
        self.save()
        return True

    def mark_used(self, signature: str) -> None:
        for row in self.items:
            puzzle = self._build(row)
            if puzzle is not None and puzzle.signature == signature:
                row["last_used_at"] = time.time()
                self.save()
                return

    def reset_used(self, index: int) -> bool:
        """单独把一道题恢复成「待用」：只清这一条的出场标记，不动别的题。"""
        row = self.get(index)
        if row is None:
            raise ValueError("没有这个序号")
        row["last_used_at"] = 0.0
        self.save()
        return True

    def set_disabled(self, index: int, disabled: bool) -> bool:
        row = self.get(index)
        if row is None:
            raise ValueError("没有这个序号")
        row["disabled"] = bool(disabled)
        self.save()
        return True

    def remove(self, index: int) -> bool:
        row = self.get(index)
        if row is None:
            raise ValueError("没有这个序号")
        self.items.pop(int(index) - 1)
        self.save()
        return True

    def update(
        self,
        index: int,
        *,
        title: str | None = None,
        surface: str | None = None,
        solution: str | None = None,
        content_level: str | None = None,
    ) -> bool:
        """改一道生成的题。改完必须仍能通过校验，否则不让保存。"""
        row = self.get(index)
        if row is None:
            raise ValueError("没有这个序号")
        mapping = dict(row.get("puzzle") or {})
        if title is not None:
            text = str(title).strip()
            if len(text) < 2:
                raise ValueError("标题至少 2 个字")
            mapping["title"] = text[:80]
        if surface is not None:
            text = str(surface).strip()
            if len(text) < 20:
                raise ValueError("汤面至少 20 字")
            mapping["surface"] = text[:800]
        if solution is not None:
            text = str(solution).strip()
            if len(solution) < 30:
                raise ValueError("汤底至少 30 字")
            mapping["solution"] = text[:1600]
        if content_level is not None:
            mapping["content_level"] = normalize_content_level(content_level)
        try:
            puzzle_from_mapping(
                mapping,
                content_level=normalize_content_level(mapping.get("content_level")),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"改完这道题不能直接出题：{exc}") from exc
        row["puzzle"] = mapping
        self.save()
        return True
