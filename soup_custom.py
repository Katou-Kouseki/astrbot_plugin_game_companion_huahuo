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

# 投稿时可选的等级：写不写都行，不写按「普通」入库（与 0.5.80 之前的行为一致）。
# 「成人 / 重口」不是正式等级名，只是让人不用记 unrestricted 这种英文词。
_LEVEL_WORDS: tuple[tuple[str, str], ...] = (
    ("全年龄", "all_ages"),
    ("all_ages", "all_ages"),
    ("all-ages", "all_ages"),
    ("普通", "normal"),
    ("normal", "normal"),
    ("不限制", "unrestricted"),
    ("成人", "unrestricted"),
    ("重口", "unrestricted"),
    ("unrestricted", "unrestricted"),
)
# 括号包起来的等级也认：`[全年龄]` / `【普通】`
_LEVEL_BRACKETS = {"[": "]", "【": "】", "(": ")", "（": "）"}
LEVEL_LABELS = {"all_ages": "全年龄", "normal": "普通", "unrestricted": "不限制"}

_PREFIX_RE = re.compile(r"^\s*[/!！]\s*")


@dataclass(slots=True)
class CustomEntry:
    surface: str
    solution: str
    content_level: str
    created_at: float
    # 投稿人 QQ。空表示「老数据 / 无主」，任何人不许删，只有管理台能清。
    owner: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "solution": self.solution,
            "content_level": self.content_level,
            "created_at": self.created_at,
            "owner": self.owner,
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
                    owner=str(item.get("owner") or "").strip(),
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

    def add(
        self,
        surface: str,
        solution: str,
        *,
        content_level: str = "normal",
        owner: str = "",
    ) -> CustomEntry:
        entry = CustomEntry(
            surface=surface.strip(),
            solution=solution.strip(),
            content_level=normalize_content_level(content_level),
            created_at=time.time(),
            owner=str(owner or "").strip(),
        )
        self.entries.append(entry)
        # 超上限时丢最旧的：新投的题更值得留下
        self.entries = self.entries[-self.limit :]
        self.save()
        return entry

    def remove(self, index: int, *, owner: str | None = None) -> CustomEntry | None:
        """按 1 起的序号删除；越界返回 None。

        给了 `owner` 就只能删自己投的：题库是大家共用的，不校验归属时任何人在
        私聊里发「删除海龟汤 1」都能把别人投的题删掉。无主的老数据也不许删，
        交给管理台清理。
        """
        if not 1 <= index <= len(self.entries):
            return None
        entry = self.entries[index - 1]
        if owner is not None:
            mine = str(owner or "").strip()
            if not mine or str(entry.owner or "") != mine:
                return None
        self.entries.pop(index - 1)
        self.save()
        return entry

    def update(
        self,
        index: int,
        *,
        surface: str | None = None,
        solution: str | None = None,
        content_level: str | None = None,
    ) -> CustomEntry | None:
        """管理台改投稿：只改传进来的字段，序号按全库算。

        管理台不属于玩家，不做归属校验——归属只管「私聊删别人的题」。
        """
        if not 1 <= index <= len(self.entries):
            return None
        entry = self.entries[index - 1]
        if surface is not None:
            text = str(surface).strip()
            if len(text) < MIN_SURFACE:
                raise ValueError(f"汤面至少 {MIN_SURFACE} 字")
            entry.surface = text
        if solution is not None:
            text = str(solution).strip()
            if len(text) < MIN_SOLUTION:
                raise ValueError(f"汤底至少 {MIN_SOLUTION} 字")
            entry.solution = text
        if content_level is not None:
            entry.content_level = normalize_content_level(content_level)
        self.save()
        return entry

    def owned(self, owner: str) -> list[tuple[int, CustomEntry]]:
        """某个人自己投的题（全局序号 + 条目），用于「我的海龟汤」。"""
        target = str(owner or "").strip()
        if not target:
            return []
        return [
            (index, entry)
            for index, entry in enumerate(self.entries, start=1)
            if str(entry.owner or "") == target
        ]

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

    def owner_for_surface(self, surface: str) -> str:
        """按汤面反查投稿人，出题时用来显示「题目来源：玩家 123 的投稿」。"""
        target = normalize_text(surface)
        if not target:
            return ""
        for entry in self.entries:
            if normalize_text(entry.surface) == target:
                return str(entry.owner or "").strip()
        return ""

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


def _split_level(body: str) -> tuple[str | None, str]:
    """把「全年龄 汤面|汤底」里的等级前缀摘出来；没写就返回 (None, 原文)。

    只在「摘掉之后还剩一份合法的汤面|汤底」时才认，否则原样返回——汤面本身
    以「普通」开头的句子（「普通的一天，他却……」）不能被当成等级吃掉。
    """
    text = str(body or "").lstrip()
    if text and text[0] in _LEVEL_BRACKETS:
        close = _LEVEL_BRACKETS[text[0]]
        end = text.find(close)
        if end > 0:
            level = dict(_LEVEL_WORDS).get(text[1:end].strip().lower())
            rest = text[end + 1 :].strip(" ：:，,")
            if level and _split_surface_solution(rest) is not None:
                return level, rest
    lowered = text.lower()
    for word, level in _LEVEL_WORDS:
        if not lowered.startswith(word):
            continue
        tail = text[len(word) :]
        # 等级词后面必须跟分隔（空格 / 冒号 / 逗号），不能直接贴正文：
        # 「普通的一天早上……」里的「普通」是汤面的一部分，不是等级。
        if tail and not (tail[0].isspace() or tail[0] in "：:，,"):
            continue
        rest = tail.strip(" ：:，,")
        if rest and _split_surface_solution(rest) is not None:
            return level, rest
    return None, text


def format_list(store: CustomSoupStore, owner: str = "") -> str:
    items = store.owned(owner)
    if not items:
        return (
            "你还没有投稿过海龟汤。用法：添加海龟汤 汤面|汤底\n"
            "想指定内容等级就把等级写在最前面：添加海龟汤 全年龄 汤面|汤底"
        )
    lines = [f"🥣 你投稿的海龟汤（{len(items)} 道）："]
    for index, entry in items:
        head = re.sub(r"\s+", " ", entry.surface)[:24]
        level = LEVEL_LABELS.get(entry.content_level, "普通")
        lines.append(f"{index}. [{level}] {head}…")
    lines.append("删除用：删除海龟汤 序号（序号按全库算，只列你自己投的）")
    return "\n".join(lines)


def handle_custom_command(plugin: Any, message: str, owner: str = "") -> str:
    """处理私聊里的投稿指令，返回要回给用户的话。

    `owner` 是投稿人的 QQ：列表只列自己的，删除只许删自己的——
    题库大家共用，不校验归属时谁都能把别人的题删掉。
    """
    body = _strip_prefix(message)
    raw = _PREFIX_RE.sub("", str(message or "")).strip()

    if raw.startswith(_LIST_PREFIXES):
        return format_list(plugin._soup_custom(), owner)

    if raw.startswith(_DELETE_PREFIXES):
        index_text = body.strip()
        if not index_text.isdigit():
            return "要给个序号，例如：删除海龟汤 2（先发「我的海龟汤」看序号）"
        removed = plugin._soup_custom().remove(int(index_text), owner=owner)
        if removed is None:
            return "没有这道题，或者不是你投的。先发「我的海龟汤」看看自己能删哪些。"
        return f"已删除：{removed.surface[:24]}…"

    level, rest = _split_level(body)
    pair = _split_surface_solution(rest)
    if pair is None:
        return (
            "用法：添加海龟汤 汤面|汤底\n"
            "汤面和汤底之间用竖线「|」隔开，例如：\n"
            "添加海龟汤 一个人每天摆两副碗筷，最后自杀了，为什么？|他是灯塔管理员，"
            "某天忘了点灯导致船只失事，妻女因此遇难，他愧疚自杀。\n"
            "想指定内容等级就在最前面加：全年龄 / 普通 / 不限制，"
            "例如：添加海龟汤 全年龄 汤面|汤底（不写默认普通）"
        )
    surface, solution = pair
    if len(surface) < MIN_SURFACE:
        return f"汤面太短了（至少要 {MIN_SURFACE} 个字），写清楚场景和那个反常的地方。"
    if len(solution) < MIN_SOLUTION:
        return f"汤底太短了（至少要 {MIN_SOLUTION} 个字），把「为什么会这样」讲完整。"
    store = plugin._soup_custom()
    if store.find_duplicate(surface) is not None:
        return "题库里已经有这道题了（可能是别人投的），换个说法或者换一道吧。"
    if len(store) >= store.limit:
        return f"投稿已经到上限（{store.limit} 道），先删几道再加。"
    entry = store.add(
        surface, solution, content_level=level or "normal", owner=owner
    )
    puzzle = build_puzzle(entry.surface, entry.solution, content_level=entry.content_level)
    if puzzle is None:
        # 已经存进去了，但补全失败：告诉用户会被跳过，而不是假装成功
        return "题目已保存，但内容不完整、暂时没法出题；可以删掉重投一个有完整因果的汤底。"
    label = LEVEL_LABELS.get(entry.content_level, "普通")
    return (
        f"已收录（第 {len(store)} 道 · {label}）。\n"
        f"汤面：{entry.surface}\n"
        f"汤底：{entry.solution}\n"
        "下次开海龟汤会优先抽到自己投稿的题。"
        + (
            "内容等级按房间设定筛：全年龄房不会抽到「不限制」的题。"
            if entry.content_level != "normal"
            else ""
        )
    )
