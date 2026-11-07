"""自定义海龟汤题库：玩家私聊投稿的题目，存在插件数据目录。

为什么要单独一份、并且只在私聊里加：

* 群聊里发「汤面|汤底」等于当众剧透，而且很容易被当成聊天内容刷掉；
  私聊投稿是一条安静、可控的入口。
* 存到 `data_dir/turtle_soup_custom.json` 而不是内置题库文件：内置题库随
  插件分发、升级时会被覆盖，玩家投的题必须独立保存。
* 出题时自定义题排在最前面——人家特意投稿的题，如果总被 226 道内置题挤掉，
  投了也白投。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .soup_library import build_puzzle
from .turtle_soup import SoupPuzzle, normalize_content_level
from .turtle_soup_deck import normalize_text

# 与内置题库一致的下限：太短的汤面/汤底补全后也过不了 puzzle_from_mapping
MIN_SURFACE = 20
MIN_SOLUTION = 30
# 单份题库上限：够玩很久，也不会把 data_dir 撑大
MAX_ENTRIES = 300

_SEPARATORS = ("|", "｜")
_ADD_PREFIXES = ("添加海龟汤", "添加汤", "投稿海龟汤", "投稿汤")
_LIST_PREFIXES = ("我的海龟汤", "我的题库", "海龟汤题库")
_DELETE_PREFIXES = ("删除海龟汤", "删除汤")

_PREFIX_RE = re.compile(r"^\s*[/!！]\s*")


@dataclass(slots=True)
class CustomEntry:
    surface: str
    solution: str
    content_level: str
    created_at: float

    def snapshot(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "solution": self.solution,
            "content_level": self.content_level,
            "created_at": self.created_at,
        }


class CustomSoupStore:
    """玩家投稿的海龟汤（JSON 持久化，进程内缓存）。"""

    def __init__(self, path: Path, *, limit: int = MAX_ENTRIES) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self.entries: list[CustomEntry] = []
        self.load()

    # ---------------- IO ----------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.entries = []
            return
        items = raw.get("items") if isinstance(raw, dict) else raw
        entries: list[CustomEntry] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            surface = str(item.get("surface") or "").strip()
            solution = str(item.get("solution") or "").strip()
            if len(surface) < MIN_SURFACE or len(solution) < MIN_SOLUTION:
                continue
            entries.append(
                CustomEntry(
                    surface=surface,
                    solution=solution,
                    content_level=normalize_content_level(item.get("content_level")),
                    created_at=float(item.get("created_at") or 0.0),
                )
            )
        self.entries = entries[: self.limit]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [entry.snapshot() for entry in self.entries],
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:  # 磁盘只读不该让投稿直接报错崩掉
            pass

    # ---------------- 增删查 ----------------

    def add(self, surface: str, solution: str, *, content_level: str = "normal") -> CustomEntry:
        entry = CustomEntry(
            surface=surface.strip(),
            solution=solution.strip(),
            content_level=normalize_content_level(content_level),
            created_at=time.time(),
        )
        self.entries.append(entry)
        # 超上限时丢最旧的：新投的题更值得留下
        self.entries = self.entries[-self.limit :]
        self.save()
        return entry

    def remove(self, index: int) -> CustomEntry | None:
        """按 1 起的序号删除；越界返回 None。"""
        if not 1 <= index <= len(self.entries):
            return None
        entry = self.entries.pop(index - 1)
        self.save()
        return entry

    def find_duplicate(self, surface: str) -> CustomEntry | None:
        target = normalize_text(surface)
        for entry in self.entries:
            if normalize_text(entry.surface) == target:
                return entry
        return None

    # ---------------- 出题 ----------------

    def puzzles(self, max_content_level: str = "unrestricted") -> list[SoupPuzzle]:
        """按内容等级上限给出可玩的题目（太重的题不会出现在全年龄房）。"""
        order = {"all_ages": 0, "normal": 1, "unrestricted": 2}
        ceiling = order.get(normalize_content_level(max_content_level), 2)
        result: list[SoupPuzzle] = []
        for entry in self.entries:
            if order.get(entry.content_level, 1) > ceiling:
                continue
            puzzle = build_puzzle(
                entry.surface, entry.solution, content_level=entry.content_level
            )
            if puzzle is not None:
                result.append(puzzle)
        return result

    def __len__(self) -> int:
        return len(self.entries)


def _strip_prefix(message: str) -> str:
    text = _PREFIX_RE.sub("", str(message or "")).strip()
    for prefix in (*_ADD_PREFIXES, *_LIST_PREFIXES, *_DELETE_PREFIXES):
        if text.startswith(prefix):
            return text[len(prefix) :].strip(" ：:，,")
    return text


def _split_surface_solution(body: str) -> tuple[str, str] | None:
    for sep in _SEPARATORS:
        if sep in body:
            left, _, right = body.partition(sep)
            left = left.strip()
            right = right.strip()
            if left and right:
                return left, right
    return None


def format_list(store: CustomSoupStore) -> str:
    if not len(store):
        return "你还没有投稿过海龟汤。用法：添加海龟汤 汤面|汤底"
    lines = [f"🥣 你投稿的海龟汤（{len(store)} 道）："]
    for index, entry in enumerate(store.entries, start=1):
        head = re.sub(r"\s+", " ", entry.surface)[:24]
        lines.append(f"{index}. {head}…")
    lines.append("删除用：删除海龟汤 序号")
    return "\n".join(lines)


def handle_custom_command(plugin: Any, message: str) -> str:
    """处理私聊里的投稿指令，返回要回给用户的话。"""
    body = _strip_prefix(message)
    raw = _PREFIX_RE.sub("", str(message or "")).strip()

    if raw.startswith(_LIST_PREFIXES):
        return format_list(plugin._soup_custom())

    if raw.startswith(_DELETE_PREFIXES):
        index_text = body.strip()
        if not index_text.isdigit():
            return "要给个序号，例如：删除海龟汤 2（先发「我的海龟汤」看序号）"
        removed = plugin._soup_custom().remove(int(index_text))
        if removed is None:
            return "没有这个序号。先发「我的海龟汤」看看现在有哪些。"
        return f"已删除：{removed.surface[:24]}…"

    pair = _split_surface_solution(body)
    if pair is None:
        return (
            "用法：添加海龟汤 汤面|汤底\n"
            "汤面和汤底之间用竖线「|」隔开，例如：\n"
            "添加海龟汤 一个人每天摆两副碗筷，最后自杀了，为什么？|他是灯塔管理员，"
            "某天忘了点灯导致船只失事，妻女因此遇难，他愧疚自杀。"
        )
    surface, solution = pair
    if len(surface) < MIN_SURFACE:
        return f"汤面太短了（至少要 {MIN_SURFACE} 个字），写清楚场景和那个反常的地方。"
    if len(solution) < MIN_SOLUTION:
        return f"汤底太短了（至少要 {MIN_SOLUTION} 个字），把「为什么会这样」讲完整。"
    store = plugin._soup_custom()
    if store.find_duplicate(surface) is not None:
        return "这道题你已经投稿过了，换个说法或者换一道吧。"
    if len(store) >= store.limit:
        return f"投稿已经到上限（{store.limit} 道），先删几道再加。"
    entry = store.add(surface, solution)
    puzzle = build_puzzle(entry.surface, entry.solution, content_level=entry.content_level)
    if puzzle is None:
        # 已经存进去了，但补全失败：告诉用户会被跳过，而不是假装成功
        return "题目已保存，但内容不完整、暂时没法出题；可以删掉重投一个有完整因果的汤底。"
    return (
        f"已收录（第 {len(store)} 道）。\n"
        f"汤面：{entry.surface}\n"
        f"汤底：{entry.solution}\n"
        "下次开海龟汤会优先抽到自己投稿的题。"
    )
