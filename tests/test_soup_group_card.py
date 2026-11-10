"""群里的开局卡：题目一出好就把汤面、难度、次数发出来。

群里开的局以前只收到一条房间链接，不进网页根本不知道在玩什么；
t2i 渲染失败时必须退回纯文字，不能让这一局没有声音。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    TurtleSoupGame,
    puzzle_from_mapping,
)


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "灯塔与沉船",
            "surface": "灯塔管理员每天准时点灯，可那天船还是撞了礁。为什么？",
            "solution": (
                "那天他照常点了灯，但灯油被人换成了水，灯亮了几分钟就灭了，"
                "船在黑暗里撞上暗礁。"
            ),
            "key_facts": ["灯确实点过", "灯油被换成水", "船在黑暗里撞礁"],
            "acceptable_variants": ["说他点灯后灯很快熄灭即可"],
            "hints": ["灯亮过但不久", "问题出在灯油", "有人动过手脚"],
            "theme": "灯塔",
            "trick": "灯亮过却被换油",
        },
        content_level="normal",
    )


def make_room(*, seats: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        room_id="room-1",
        game_type="turtle_soup",
        source="group",
        session_id="aiocqhttp:GroupMessage:123",
        platform="aiocqhttp",
        group_id="123",
        difficulty="normal",
        multiplayer=SimpleNamespace(
            enabled=True,
            seats=[
                SimpleNamespace(number=index + 1, is_ai=False)
                for index in range(seats)
            ],
        ),
        visitors={},
    )


def make_game() -> TurtleSoupGame:
    game = TurtleSoupGame(
        difficulty="normal", max_hints=5, content_level="normal"
    )
    game.puzzle = make_puzzle()
    game.puzzle_source = "library"
    return game


def make_plugin() -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_bridge = True
    plugin.turtle_soup_qq_opening_card = True
    plugin.turtle_soup_max_questions = 35
    plugin.turtle_soup_max_hints = 5
    plugin.turtle_soup_max_answers = 4
    plugin._send_to_origin = AsyncMock(return_value=True)  # noqa: SLF001
    plugin._send_to_origin_image = AsyncMock(return_value=True)  # noqa: SLF001
    return plugin


@pytest.mark.asyncio
async def test_opening_card_falls_back_to_text_without_t2i() -> None:
    plugin = make_plugin()
    room = make_room()
    room.game = make_game()
    plugin._soup_t2i = AsyncMock(side_effect=RuntimeError("没有 t2i"))  # noqa: SLF001

    await plugin._announce_soup_opening(room)  # noqa: SLF001

    plugin._send_to_origin_image.assert_not_awaited()  # noqa: SLF001
    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "灯塔管理员每天准时点灯" in text, "群里必须能看到汤面"
    assert "普通" in text
    assert "提示 5 次" in text
    assert "汤 提示" in text


@pytest.mark.asyncio
async def test_opening_card_is_sent_as_an_image_first() -> None:
    plugin = make_plugin()
    room = make_room()
    room.game = make_game()
    plugin._soup_t2i = AsyncMock(return_value="BASE64")  # noqa: SLF001

    await plugin._announce_soup_opening(room)  # noqa: SLF001

    plugin._send_to_origin_image.assert_awaited_once()  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_opening_card_is_sent_only_once_per_round() -> None:
    plugin = make_plugin()
    room = make_room()
    room.game = make_game()
    plugin._soup_t2i = AsyncMock(return_value="BASE64")  # noqa: SLF001

    await plugin._announce_soup_opening(room)  # noqa: SLF001
    await plugin._announce_soup_opening(room)  # noqa: SLF001

    assert plugin._send_to_origin_image.await_count == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_opening_card_skips_rooms_without_a_group() -> None:
    plugin = make_plugin()
    room = make_room()
    room.group_id = ""
    room.session_id = "admin:console"
    room.platform = "admin"
    room.game = make_game()
    plugin._soup_t2i = AsyncMock(return_value="BASE64")  # noqa: SLF001

    await plugin._announce_soup_opening(room)  # noqa: SLF001

    plugin._send_to_origin_image.assert_not_awaited()  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


def test_opening_data_counts_seats_and_limits() -> None:
    plugin = make_plugin()
    room = make_room(seats=3)
    game = make_game()

    data = plugin._soup_opening_data(room, game)  # noqa: SLF001

    assert data is not None
    assert data["difficulty"] == "普通"
    assert data["players"] == "3 人"
    assert data["questions"] == "35 次"
    assert data["hints"] == "5 次"
    assert "灯塔管理员" in data["surface"]
