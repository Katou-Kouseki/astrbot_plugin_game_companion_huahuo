"""内置题库：两字段原始题目必须能变成可玩的海龟汤。

线上题库（以及绝大多数人自己收集的题库）只有「汤面 + 汤底」，而 `SoupPuzzle`
要七个字段，少了任何一个 `puzzle_from_mapping` 都会拒收。这里的测试锁住的就是
「导进来就能玩」，以及补全的三级回退：缓存 → 模型 → 规则。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from astrbot_plugin_game_companion_huahuo.soup_library import (
    SoupLibrary,
    _rule_title,
)

PACKAGE = Path(__file__).resolve().parent.parent
BUILTIN = PACKAGE / "soup_library.json"


def write_library(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "lib.json"
    path.write_text(
        json.dumps({"version": 1, "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


SAMPLE = {
    "surface": "一个男人走进酒吧，向酒保要了一杯水，酒保却掏出一把枪指着他。男人说了声谢谢就走了。为什么？",
    "solution": "男人在打嗝，想喝水止嗝。酒保看出他的意图，用掏枪吓他一跳治好打嗝，所以男人道谢离开。",
    "content_level": "all_ages",
}


# ---------------- 加载 ----------------


def test_builtin_library_is_usable() -> None:
    library = SoupLibrary(BUILTIN)
    assert len(library.entries) >= 200
    stats = library.stats()
    assert stats["by_level"]["all_ages"] > 0
    assert stats["by_level"]["normal"] > 0


def test_every_builtin_entry_can_be_built() -> None:
    """内置 226 条必须条条可用，不能出现「抽到才发现字段不够」。"""
    library = SoupLibrary(BUILTIN)
    broken = [
        entry.surface[:24]
        for entry in library.entries
        if library.rule_puzzle(entry) is None
    ]
    assert broken == [], f"有题目无法构造：{broken[:3]}"


def test_two_field_format_is_accepted(tmp_path: Path) -> None:
    """兼容外部题库的 puzzle/answer 键名。"""
    path = write_library(
        tmp_path,
        [{"puzzle": SAMPLE["surface"], "answer": SAMPLE["solution"]}],
    )
    library = SoupLibrary(path)
    assert len(library.entries) == 1
    puzzle = library.rule_puzzle(library.entries[0])
    assert puzzle is not None
    assert puzzle.surface == SAMPLE["surface"]
    assert puzzle.solution == SAMPLE["solution"]
    assert len(puzzle.key_facts) >= 3
    assert len(puzzle.hints) >= 3


def test_too_short_entries_are_dropped(tmp_path: Path) -> None:
    path = write_library(
        tmp_path,
        [
            {"surface": "短", "solution": "太短"},
            {"puzzle": SAMPLE["surface"], "answer": SAMPLE["solution"]},
        ],
    )
    library = SoupLibrary(path)
    assert len(library.entries) == 1


def test_missing_file_is_not_fatal(tmp_path: Path) -> None:
    library = SoupLibrary(tmp_path / "不存在.json")
    assert library.entries == []
    assert library.entries_for("normal") == []


# ---------------- 内容分级 ----------------


def test_content_level_filters_heavier_items(tmp_path: Path) -> None:
    path = write_library(
        tmp_path,
        [
            {**SAMPLE, "content_level": "all_ages"},
            {**SAMPLE, "surface": SAMPLE["surface"] + "（普通）", "content_level": "normal"},
            {
                **SAMPLE,
                "surface": SAMPLE["surface"] + "（重口）",
                "content_level": "unrestricted",
            },
        ],
    )
    library = SoupLibrary(path)
    assert len(library.entries_for("all_ages")) == 1
    assert len(library.entries_for("normal")) == 2
    assert len(library.entries_for("unrestricted")) == 3


# ---------------- 补全 ----------------


VALID_FIELDS = {
    "title": "酒吧里的枪",
    "key_facts": ["男人在打嗝", "他想喝水止嗝", "酒保用惊吓治好了他"],
    "hints": ["注意他为什么道谢", "枪不是威胁", "惊吓也有用处"],
    "theme": "误会与惊吓",
    "trick": "把「要水」误读成打劫",
    "content_level": "all_ages",
}


@pytest.mark.asyncio
async def test_enricher_result_is_used_and_cached(tmp_path: Path) -> None:
    path = write_library(tmp_path, [SAMPLE])
    cache = tmp_path / "cache.json"
    library = SoupLibrary(path, cache)

    async def enricher(system: str, prompt: str) -> str:
        assert "汤面" in system or "题库编辑" in system
        return json.dumps(VALID_FIELDS, ensure_ascii=False)

    puzzle = await library.puzzle_for(library.entries[0], enricher)
    assert puzzle is not None
    assert puzzle.title == "酒吧里的枪"
    assert list(puzzle.hints) == VALID_FIELDS["hints"]

    # 补全结果落盘：新实例读到缓存，不再调模型
    reloaded = SoupLibrary(path, cache)
    assert reloaded.stats()["enriched"] == 1
    calls = 0

    async def should_not_run(system: str, prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    again = await reloaded.puzzle_for(reloaded.entries[0], should_not_run)
    assert again is not None and again.title == "酒吧里的枪"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "不是 JSON", "{}", json.dumps({"title": "短"})])
async def test_bad_enricher_output_falls_back_to_rules(
    tmp_path: Path, raw: str
) -> None:
    path = write_library(tmp_path, [SAMPLE])
    library = SoupLibrary(path)

    async def enricher(system: str, prompt: str) -> str:
        return raw

    puzzle = await library.puzzle_for(library.entries[0], enricher)
    assert puzzle is not None
    assert puzzle.title  # 规则版标题
    assert len(puzzle.hints) >= 3
    # 失败不写缓存，下次还会再试模型
    assert library.stats()["enriched"] == 0


@pytest.mark.asyncio
async def test_enricher_exception_and_timeout_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_library(tmp_path, [SAMPLE])
    library = SoupLibrary(path)

    async def boom(system: str, prompt: str) -> str:
        raise RuntimeError("模型挂了")

    assert await library.puzzle_for(library.entries[0], boom) is not None

    monkeypatch.setattr(
        "astrbot_plugin_game_companion_huahuo.soup_library.ENRICH_TIMEOUT", 0.01
    )

    async def slow(system: str, prompt: str) -> str:
        await asyncio.sleep(0.2)
        return json.dumps(VALID_FIELDS, ensure_ascii=False)

    assert await library.puzzle_for(library.entries[0], slow) is not None


@pytest.mark.asyncio
async def test_enricher_cannot_downgrade_content_level(tmp_path: Path) -> None:
    """素材是「重口」，模型判成 all_ages 也不许降档。"""
    path = write_library(
        tmp_path, [{**SAMPLE, "content_level": "unrestricted"}]
    )
    library = SoupLibrary(path)
    fields = {**VALID_FIELDS, "content_level": "all_ages"}

    async def enricher(system: str, prompt: str) -> str:
        return json.dumps(fields, ensure_ascii=False)

    puzzle = await library.puzzle_for(library.entries[0], enricher)
    assert puzzle is not None
    assert puzzle.content_level == "unrestricted"


@pytest.mark.asyncio
async def test_enricher_can_upgrade_content_level(tmp_path: Path) -> None:
    path = write_library(tmp_path, [{**SAMPLE, "content_level": "all_ages"}])
    library = SoupLibrary(path)
    fields = {**VALID_FIELDS, "content_level": "unrestricted"}

    async def enricher(system: str, prompt: str) -> str:
        return json.dumps(fields, ensure_ascii=False)

    puzzle = await library.puzzle_for(library.entries[0], enricher)
    assert puzzle is not None
    assert puzzle.content_level == "unrestricted"


# ---------------- 标题规则 ----------------


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("一个男人推开门，看到眼前的景象后立即跳楼。为什么？", "一个男人推开门"),
        ("她喝了一口汤就哭了。", "她喝了一口汤就哭了"),
    ],
)
def test_rule_title_cuts_at_first_comma(surface: str, expected: str) -> None:
    assert _rule_title(surface) == expected
