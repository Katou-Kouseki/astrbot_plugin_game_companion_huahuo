"""模型出的题要存得下来、管得动、复用得上，并且能被玩家看见出处。

这一组盯三件事：

* 生成的题落盘后重启还在，停用/删除真的影响复用；
* 出题时「往期生成」排在 LLM 之前，模型出成功才入库；
* 网页上显示的「题目来源」对每种来源都说人话。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo import main as main_module
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.soup_generated import GeneratedSoupStore
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    SoupPuzzle,
    TurtleSoupGame,
    soup_source_label,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

PACKAGE = Path(__file__).resolve().parent.parent
BUILTIN = PACKAGE / "soup_library.json"


def make_puzzle(suffix: str = "甲") -> SoupPuzzle:
    return SoupPuzzle(
        title=f"测试生成的题{suffix}",
        surface=f"某个人每天都会在同一时间做一件奇怪的事（{suffix}），为什么？",
        solution=(
            "他在农场里养了一群需要定时投喂的动物，时间一到就必须去喂，"
            "否则会互相踩踏，所以看起来很奇怪。"
        ),
        key_facts=("他养了动物", "必须定时投喂", "否则会出事故"),
        acceptable_variants=("他必须按时喂动物",),
        hints=("和动物有关", "和时间有关", "不喂会出事"),
        content_level="normal",
        theme=f"养殖{suffix}",
        trick=f"定时投喂{suffix}",
    )


def make_store(tmp_path: Path) -> GeneratedSoupStore:
    return GeneratedSoupStore(tmp_path / "generated.json")


# ---------------- 存档本身 ----------------


def test_add_persists_and_reloads(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.add(make_puzzle()) is True

    again = make_store(tmp_path)
    assert len(again) == 1
    puzzles = again.puzzles()
    assert puzzles[0].title == "测试生成的题甲"
    assert again.stats()["total"] == 1


def test_same_signature_is_not_stored_twice(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add(make_puzzle())
    store.add(make_puzzle())
    assert len(store) == 1


def test_disabled_puzzle_is_not_reused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add(make_puzzle())
    store.set_disabled(1, True)

    assert store.puzzles() == []
    assert store.stats()["disabled"] == 1
    # 停用不等于删除：存档还在，随时能恢复
    store.set_disabled(1, False)
    assert len(store.puzzles()) == 1


def test_content_level_ceiling_is_respected(tmp_path: Path) -> None:
    """全年龄房不能抽到标成「不限制」的生成题。"""
    store = make_store(tmp_path)
    puzzle = make_puzzle()
    store.add(
        SoupPuzzle(
            title=puzzle.title,
            surface=puzzle.surface,
            solution=puzzle.solution,
            key_facts=puzzle.key_facts,
            acceptable_variants=puzzle.acceptable_variants,
            hints=puzzle.hints,
            content_level="unrestricted",
            theme=puzzle.theme,
            trick=puzzle.trick,
        )
    )
    assert store.puzzles("all_ages") == []
    assert len(store.puzzles("unrestricted")) == 1


def test_update_rejects_broken_puzzle_and_keeps_old_one(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add(make_puzzle())

    with pytest.raises(ValueError, match="至少 20 字"):
        store.update(1, surface="太短了")

    assert store.puzzles()[0].surface.startswith("某个人每天")


def test_update_changes_surface_and_level(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add(make_puzzle())
    store.update(1, surface="换成一个全新的汤面，长度也要够，讲的是另一个人遇到的事。", content_level="all_ages")

    puzzle = store.puzzles("all_ages")[0]
    assert puzzle.surface.startswith("换成一个全新的汤面")
    assert puzzle.content_level == "all_ages"


def test_remove_drops_the_entry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add(make_puzzle())
    store.remove(1)
    assert len(store) == 0


def test_corrupted_file_does_not_break_the_store(tmp_path: Path) -> None:
    path = tmp_path / "generated.json"
    path.write_text("{ not json", encoding="utf-8")
    assert len(GeneratedSoupStore(path)) == 0


# ---------------- 出处文案 ----------------


@pytest.mark.parametrize(
    "source,note,expected",
    [
        ("library", "", "内置题库"),
        ("online", "", "网络题库"),
        ("generated", "", "往期生成"),
        ("llm", "", "花火现编"),
        ("fallback", "", "内置兜底题"),
        ("custom", "", "玩家投稿"),
        ("custom", "123456", "玩家 123456 的投稿"),
        ("custom", "小明", "小明 的投稿"),
        ("player", "2 号（小明）", "2 号（小明） 出题"),
        ("", "", ""),
        ("unknown", "", ""),
    ],
)
def test_source_label_reads_like_a_person(source: str, note: str, expected: str) -> None:
    assert soup_source_label(source, note) == expected


def test_snapshot_carries_the_source_label() -> None:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    game.puzzle_source = "custom"
    game.puzzle_source_note = "7654321"

    data = game.snapshot()
    assert data["puzzle_source"] == "custom"
    assert data["puzzle_source_label"] == "玩家 7654321 的投稿"


# ---------------- 出题流程接入 ----------------


def make_plugin(tmp_path: Path, *, library_enabled: bool = False) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.data_dir = tmp_path
    plugin.turtle_soup_online_pool_url = ""
    plugin.turtle_soup_library_enabled = library_enabled
    plugin.turtle_soup_library_priority = "builtin_first"

    async def apply(_room, game, puzzle, *, source="", note=""):
        game.puzzle = puzzle
        game.puzzle_source = source
        game.puzzle_source_note = note
        return True

    plugin.manager = SimpleNamespace(
        complete_turtle_soup_generation=AsyncMock(side_effect=apply)
    )
    plugin._soup_deck = lambda: SoupDeck(tmp_path / "deck.json")  # noqa: SLF001
    plugin._soup_generated = lambda: GeneratedSoupStore(  # noqa: SLF001
        tmp_path / "generated.json"
    )
    plugin._soup_library = lambda: SoupLibrary(  # noqa: SLF001
        BUILTIN, tmp_path / "cache.json"
    )
    plugin._soup_library_enricher = lambda _room: None  # noqa: SLF001
    plugin._persona_prompt = AsyncMock(return_value="")  # noqa: SLF001
    plugin._soup_generation_provider = lambda _room: None  # noqa: SLF001
    return plugin


def make_room() -> SimpleNamespace:
    return SimpleNamespace(turtle_soup_recent_signatures=[], difficulty="normal")


@pytest.mark.asyncio
async def test_generated_puzzle_is_reused_before_calling_the_model(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plugin._soup_generated().add(make_puzzle())  # noqa: SLF001
    plugin._call_room_model = AsyncMock(  # noqa: SLF001
        side_effect=AssertionError("有往期生成题时不该再调用模型")
    )

    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    await plugin._prepare_turtle_soup(make_room(), game)

    assert game.puzzle is not None
    assert game.puzzle_source == "generated"
    assert game.puzzle_source_label == "往期生成"


@pytest.mark.asyncio
async def test_disabled_generated_puzzle_falls_through_to_llm(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    store = plugin._soup_generated()  # noqa: SLF001
    store.add(make_puzzle())
    store.set_disabled(1, True)

    plugin._call_room_model = AsyncMock(  # noqa: SLF001
        return_value=json.dumps(
            {
                "title": "模型新出的题",
                "surface": "一个男人每天都要把同一把椅子搬到门口再搬回去，为什么？",
                "solution": "那把椅子的位置决定他能否听见楼下独居母亲的动静，他每天检查两次。",
                "key_facts": ["椅子用于听声音", "楼下住着母亲", "每天检查两次"],
                "hints": ["和声音有关", "和楼下的家人有关", "搬动是为了确认"],
                "theme": "听声",
                "trick": "用椅子监听楼下",
            },
            ensure_ascii=False,
        )
    )
    plugin._soup_generation_provider = lambda _room: None  # noqa: SLF001
    original_generation_prompt = main_module.generation_prompt
    original_validation_prompt = main_module.validation_prompt
    original_validation_passed = main_module.validation_passed
    main_module.generation_prompt = lambda **_kwargs: ("sys", "prompt")
    main_module.validation_prompt = lambda _puzzle: ("sys", "check")
    main_module.validation_passed = lambda _text: True
    try:
        game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
        await plugin._prepare_turtle_soup(make_room(), game)
    finally:
        main_module.generation_prompt = original_generation_prompt
        main_module.validation_prompt = original_validation_prompt
        main_module.validation_passed = original_validation_passed

    assert game.puzzle_source == "llm"
    assert game.puzzle is not None and game.puzzle.title == "模型新出的题"


@pytest.mark.asyncio
async def test_llm_puzzle_is_saved_for_later_reuse(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plugin._call_room_model = AsyncMock(  # noqa: SLF001
        return_value=json.dumps(
            {
                "title": "会消失的脚印",
                "surface": "雪地上只有一串进来的脚印，却没有出去的，屋里的人却不见了。",
                "solution": "他从屋顶的烟囱爬上阁楼，再沿屋后的梯子离开，所以雪地只剩进来的脚印。",
                "key_facts": ["脚印只朝一个方向", "他走了屋顶", "屋后有梯子"],
                "hints": ["脚印不是全部", "他往上走了", "出口不在雪地上"],
                "theme": "雪地",
                "trick": "从屋顶离开",
            },
            ensure_ascii=False,
        )
    )
    original_generation_prompt = main_module.generation_prompt
    original_validation_prompt = main_module.validation_prompt
    original_validation_passed = main_module.validation_passed
    main_module.generation_prompt = lambda **_kwargs: ("sys", "prompt")
    main_module.validation_prompt = lambda _puzzle: ("sys", "check")
    main_module.validation_passed = lambda _text: True
    try:
        game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
        await plugin._prepare_turtle_soup(make_room(), game)
    finally:
        main_module.generation_prompt = original_generation_prompt
        main_module.validation_prompt = original_validation_prompt
        main_module.validation_passed = original_validation_passed

    store = GeneratedSoupStore(tmp_path / "generated.json")
    assert len(store) == 1
    assert store.listing()[0]["title"] == "会消失的脚印"
