"""出题优先级：内置题库必须排在 LLM 之前，且不能覆盖账本去重。

这两点一旦错了很直观：要么又回到「每次都让模型现编、慢且贵」，
要么连着两局抽到同一道题。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo import main as main_module
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary
from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

PACKAGE = Path(__file__).resolve().parent.parent
BUILTIN = PACKAGE / "soup_library.json"


def make_plugin(tmp_path: Path, *, enabled: bool = True) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_online_pool_url = ""
    plugin.turtle_soup_library_enabled = enabled
    plugin.data_dir = tmp_path

    async def apply(_room, game, puzzle):
        """模拟 RoomManager：真正把题装进对局并返回成功。"""
        game.puzzle = puzzle
        return True

    plugin.manager = SimpleNamespace(
        complete_turtle_soup_generation=AsyncMock(side_effect=apply)
    )
    plugin._soup_deck = lambda: SoupDeck(tmp_path / "deck.json")  # noqa: SLF001
    plugin._soup_library = lambda: SoupLibrary(  # noqa: SLF001
        BUILTIN, tmp_path / "cache.json"
    )
    # 不注入模型：走规则版补全，测试不依赖网络
    plugin._soup_library_enricher = lambda _room: None  # noqa: SLF001
    return plugin


def make_room() -> SimpleNamespace:
    return SimpleNamespace(
        turtle_soup_recent_signatures=[],
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_library_is_used_before_llm(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plugin._call_room_model = AsyncMock(  # 一旦走到 LLM 段就会失败
        side_effect=AssertionError("本地题库命中时不该调用模型现编")
    )

    room = make_room()
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    await plugin._prepare_turtle_soup(room, game)

    assert game.puzzle is not None
    plugin._call_room_model.assert_not_called()
    plugin.manager.complete_turtle_soup_generation.assert_awaited_once()

    # 题目确实来自内置题库
    library = SoupLibrary(BUILTIN)
    surfaces = {entry.surface for entry in library.entries}
    assert game.puzzle.surface in surfaces

    # 账本记的是 library 来源：下一局才会被去重
    deck = plugin._soup_deck()
    assert any(record.get("source") == "library" for record in deck._records)  # noqa: SLF001


@pytest.mark.asyncio
async def test_disabled_library_is_skipped(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path, enabled=False)
    called = {"library": False}

    def spy():
        called["library"] = True
        return SoupLibrary(BUILTIN)

    plugin._soup_library = spy  # noqa: SLF001
    # 关掉题库后应走 LLM 段；这里让它失败，最终落到内置兜底
    plugin._call_room_model = AsyncMock(side_effect=RuntimeError("模型不可用"))
    plugin._persona_prompt = AsyncMock(return_value="")

    room = make_room()
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    await plugin._prepare_turtle_soup(room, game)

    assert called["library"] is False
    assert game.puzzle is not None  # 兜底题接手，出题流程没断


@pytest.mark.asyncio
async def test_full_deck_falls_through_to_other_sources(tmp_path: Path) -> None:
    """题库里所有题都出过时，不能重复抽，应该让后面的环节接手。"""
    plugin = make_plugin(tmp_path)
    plugin._call_room_model = AsyncMock(side_effect=RuntimeError("模型不可用"))
    plugin._persona_prompt = AsyncMock(return_value="")

    deck = plugin._soup_deck()
    library = SoupLibrary(BUILTIN)
    for entry in library.entries:
        puzzle = library.rule_puzzle(entry)
        assert puzzle is not None
        deck.remember(puzzle, source="library")

    room = make_room()
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    await plugin._prepare_turtle_soup(room, game)

    # 走的是内置兜底 12 条，而不是又抽一次题库
    assert game.puzzle is not None
    assert game.puzzle.surface not in {e.surface for e in library.entries}
    assert main_module is not None
