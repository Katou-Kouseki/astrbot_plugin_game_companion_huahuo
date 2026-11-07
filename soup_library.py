"""内置海龟汤题库：两字段原始条目 → 可以直接开局的 SoupPuzzle。

为什么需要这一层：

* 外部题库（以及很多自己收集的题库）只给「汤面 + 汤底」，而 :class:`SoupPuzzle`
  要七个字段——标题、关键事实、渐进提示、题材、诡计。少任何一个，
  :func:`puzzle_from_mapping` 都会直接拒收，所以「把 json 拷进来就能用」不成立。
* 补全需要模型，但两百多条一次性补完既慢又贵，而且大部分题目短期内根本抽不到。
  所以分成两步：先用规则补一份「能玩但不精致」的，真被抽中时再让模型精修，
  结果落到 data_dir 的缓存里。第一局就能玩，也不会为没人抽到的题白烧调用。

缓存与题库文件分离（`data_dir/soup_library_cache.json`）：内置题库是只读的，
插件目录在部分部署里不可写；把补全结果单独存一份，既能缓存在重启后保留，
也不会把内置文件改脏。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .turtle_soup import (
    SoupContentLevel,
    SoupPuzzle,
    normalize_content_level,
    puzzle_from_mapping,
)
from .turtle_soup_ai import extract_json_object
from .turtle_soup_deck import normalize_text

# content_level 的上限比较：允许更轻的内容，不允许更重的。
_CONTENT_ORDER = {"all_ages": 0, "normal": 1, "unrestricted": 2}

# 缓存里最多留多少条：一条不到 1KB，两千条足够覆盖长期使用。
_CACHE_LIMIT = 2000

# 单次补全的超时。补全只是「锦上添花」，等太久不如先用规则版开局。
ENRICH_TIMEOUT = 25.0

_ENRICH_SYSTEM = (
    "你是海龟汤题库编辑。给定汤面与汤底，补全成一份可以直接主持的题目。"
    "只输出一个 JSON 对象，不要解释、不要代码块标记。"
)

_ENRICH_PROMPT = """汤面：{surface}

汤底：{solution}

请输出如下 JSON：
{{
  "title": "8 字以内的标题，能记住又不剧透",
  "key_facts": ["解开谜底必须知道的 3~6 条事实，每条一句话"],
  "hints": ["由模糊到具体的 3~5 条提示，每条都不许直接说出答案"],
  "theme": "题材标签，4 字以内，例如：灯塔与失职",
  "trick": "核心诡计一句话，不超过 30 字",
  "content_level": "all_ages 或 normal 或 unrestricted"
}}

要求：key_facts 只写事实，不写推理过程；hints 必须让人想到新方向而不是复述汤底；
不要引入汤面和汤底里没有的人物或情节。"""


@dataclass(slots=True)
class LibraryEntry:
    """题库里的一条原始记录（只有汤面与汤底）。"""

    surface: str
    solution: str
    content_level: SoupContentLevel

    @property
    def cache_key(self) -> str:
        """缓存键：只用汤面的归一化文本算哈希。

        不用 title 是因为这时还没有 title；用整段汤底会让「同一题微调一个标点」
        就变成两条缓存。汤面已经足够区分。
        """
        return hashlib.sha1(
            normalize_text(self.surface)[:400].encode("utf-8")
        ).hexdigest()[:20]


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？；\n]+", str(text or ""))
    result = [part.strip(" ，,、") for part in parts]
    return [part for part in result if len(part) >= 4]


def _rule_title(surface: str) -> str:
    """规则版标题：取汤面第一小句（逗号/句号前），再截 12 字。

    整句直接截断会得到「一个男人推开门，看到眼前」这种残句，很难看；
    先在第一个标点前断开，标题就是一个完整的短句。
    """
    first = re.split(r"[。！？；\n]+", str(surface or ""))[0].strip()
    head = re.split(r"[，,]", first)[0].strip()
    candidate = head if len(head) >= 4 else first
    cleaned = re.sub(r"(为什么|怎么回事|发生了什么|请问|答案是啥)[？?！!。]*$", "", candidate)
    cleaned = cleaned.strip(" ，,、？?！!。")
    if len(cleaned) < 2:
        cleaned = str(surface or "").strip()
    return cleaned[:12] or "海龟汤"


def _rule_fields(surface: str, solution: str) -> dict[str, Any]:
    """不调模型时的兜底补全：能开局，但提示是「方向性」的通用话术。

    这里刻意不从汤底摘句子当提示——那样等于把答案念给玩家。规则版只给
    「往哪想」，具体线索等模型补全（:meth:`SoupLibrary.puzzle_for` 会优先走它）。
    """
    sentences = _split_sentences(solution)
    if len(sentences) < 3:
        # 一句写到底的汤底：退到逗号层面再拆一次
        extra = [
            part.strip()
            for part in re.split(r"[，,]", str(solution or ""))
            if len(part.strip()) >= 4
        ]
        sentences = sentences + [part for part in extra if part not in sentences]
    key_facts = sentences[:6] or [str(solution or "").strip()[:60] or "汤底缺失"]
    while len(key_facts) < 3:
        key_facts.append(f"第 {len(key_facts) + 1} 条关键事实")

    hints = [
        "先把汤面里最反常的那一处圈出来。",
        "再想想：这个反常对当事人意味着什么？",
        "最后一步：如果他当时有别的选择，故事会怎么变？",
    ]

    title = _rule_title(surface)
    return {
        "title": title,
        "key_facts": key_facts,
        "hints": hints,
        "theme": title,
        "trick": (sentences[0] if sentences else str(solution or ""))[:60],
        "content_level": None,  # 由原始条目决定，不让规则版改判
    }


class SoupLibrary:
    """内置题库：加载、按内容等级过滤、补全并缓存。"""

    def __init__(
        self,
        path: Path,
        cache_path: Path | None = None,
        *,
        cache_limit: int = _CACHE_LIMIT,
    ) -> None:
        self.path = Path(path)
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache_limit = max(1, int(cache_limit))
        self.entries: list[LibraryEntry] = []
        self._cache: dict[str, dict[str, Any]] = {}
        self.load()

    # ---------------- 加载 ----------------

    def load(self) -> None:
        self.entries = self._load_entries()
        self._cache = self._load_cache()

    def _load_entries(self) -> list[LibraryEntry]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("items") or raw.get("puzzles") or ()
        if not isinstance(raw, list):
            return []
        entries: list[LibraryEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            surface = str(item.get("surface") or item.get("puzzle") or "").strip()
            solution = str(item.get("solution") or item.get("answer") or "").strip()
            if len(surface) < 20 or len(solution) < 30:
                # 过不了 puzzle_from_mapping 的下限，提前剔掉，别等到出题时才报错
                continue
            entries.append(
                LibraryEntry(
                    surface=surface,
                    solution=solution,
                    content_level=normalize_content_level(
                        item.get("content_level")
                    ),
                )
            )
        return entries

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if self.cache_path is None:
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return {}
        return {
            str(key): value
            for key, value in items.items()
            if isinstance(value, dict)
        }

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        if len(self._cache) > self.cache_limit:
            # 按写入时间留最近的，避免缓存无限膨胀
            ordered = sorted(
                self._cache.items(),
                key=lambda pair: float(pair[1].get("at") or 0.0),
            )
            self._cache = dict(ordered[-self.cache_limit :])
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {"version": 1, "items": self._cache},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:  # 磁盘只读不该拖垮出题
            pass

    # ---------------- 查询 ----------------

    def entries_for(self, content_level: str) -> list[LibraryEntry]:
        """按内容等级上限筛出可用题目（允许更轻，不允许更重）。"""
        ceiling = _CONTENT_ORDER.get(normalize_content_level(content_level), 1)
        return [
            entry
            for entry in self.entries
            if _CONTENT_ORDER.get(entry.content_level, 1) <= ceiling
        ]

    def cached_fields(self, entry: LibraryEntry) -> dict[str, Any] | None:
        return self._cache.get(entry.cache_key)

    # ---------------- 补全 ----------------

    def _build(self, entry: LibraryEntry, fields: dict[str, Any]) -> SoupPuzzle | None:
        """把原始条目和补全字段合成 SoupPuzzle；不合格返回 None。"""
        payload: dict[str, Any] = {
            "surface": entry.surface,
            "solution": entry.solution,
            "title": fields.get("title") or "",
            "key_facts": fields.get("key_facts") or [],
            "hints": fields.get("hints") or [],
            "theme": fields.get("theme") or "",
            "trick": fields.get("trick") or "",
            "acceptable_variants": fields.get("acceptable_variants") or [],
        }
        # 等级取两者中更严的：模型认为这题更重就升档，绝不因为补全而放松限制
        llm_level = normalize_content_level(
            fields.get("content_level") or entry.content_level
        )
        level = max(
            (entry.content_level, llm_level),
            key=lambda name: _CONTENT_ORDER.get(name, 1),
        )
        try:
            return puzzle_from_mapping(payload, content_level=level)
        except (ValueError, TypeError):
            return None

    def rule_puzzle(self, entry: LibraryEntry) -> SoupPuzzle | None:
        """规则版补全：不联网、不调模型，永远可用。"""
        return self._build(entry, _rule_fields(entry.surface, entry.solution))

    async def puzzle_for(
        self,
        entry: LibraryEntry,
        enricher: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> SoupPuzzle | None:
        """取一道可玩的题：缓存 → 模型精修 → 规则兜底。

        `enricher` 是 `async (system_prompt, prompt) -> str`，由调用方注入
        （它知道该用哪个 Provider）。补全失败绝不抛异常——出题流程不能因为
        一次补全失败就卡住。
        """
        cached = self.cached_fields(entry)
        if cached:
            puzzle = self._build(entry, cached)
            if puzzle is not None:
                return puzzle

        if enricher is not None:
            try:
                raw = await asyncio.wait_for(
                    enricher(
                        _ENRICH_SYSTEM,
                        _ENRICH_PROMPT.format(
                            surface=entry.surface, solution=entry.solution
                        ),
                    ),
                    timeout=ENRICH_TIMEOUT,
                )
            except (asyncio.TimeoutError, RuntimeError, ValueError, OSError):
                raw = ""
            data = extract_json_object(raw) if raw else None
            if isinstance(data, dict):
                puzzle = self._build(entry, data)
                if puzzle is not None:
                    self._remember(entry, data, puzzle)
                    return puzzle

        return self.rule_puzzle(entry)

    def _remember(
        self, entry: LibraryEntry, fields: dict[str, Any], puzzle: SoupPuzzle
    ) -> None:
        """把补全结果写进缓存（只存字段，题面题底仍以题库文件为准）。"""
        self._cache[entry.cache_key] = {
            "title": puzzle.title,
            "key_facts": list(puzzle.key_facts),
            "hints": list(puzzle.hints),
            "acceptable_variants": list(puzzle.acceptable_variants),
            "theme": puzzle.theme,
            "trick": puzzle.trick,
            "content_level": puzzle.content_level,
            "at": time.time(),
        }
        self._save_cache()

    # ---------------- 统计 ----------------

    def stats(self) -> dict[str, Any]:
        counts = {"all_ages": 0, "normal": 0, "unrestricted": 0}
        for entry in self.entries:
            counts[entry.content_level] = counts.get(entry.content_level, 0) + 1
        return {
            "total": len(self.entries),
            "by_level": counts,
            "enriched": len(self._cache),
        }
