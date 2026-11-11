from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin


def make_plugin(*, delivered: bool) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=delivered))
    plugin.manager = SimpleNamespace(empty_player_timeout=60)
    return plugin


def make_room() -> SimpleNamespace:
    return SimpleNamespace(
        room_id="room-1",
        session_id="aiocqhttp:FriendMessage:10001",
        game_type="xiangqi",
        player=None,
    )


@pytest.mark.asyncio
async def test_room_link_is_sent_as_an_independent_plain_message() -> None:
    plugin = make_plugin(delivered=True)
    room = make_room()
    url = "https://example.test/room/access-token"

    delivered = await plugin._deliver_room_link(
        room,
        url,
        reused=False,
        restarted=False,
    )

    assert delivered is True
    session_id, chain = plugin.context.send_message.await_args.args
    assert session_id == room.session_id
    assert len(chain.chain) == 1
    assert chain.chain[0].text == (
        "中国象棋房间已准备好：\n"
        f"{url}\n"
        "请在 60 秒内进入玩家席，否则房间会自动销毁。"
    )


@pytest.mark.asyncio
async def test_room_link_delivery_reports_platform_failure_for_model_fallback() -> None:
    plugin = make_plugin(delivered=False)

    delivered = await plugin._deliver_room_link(
        make_room(),
        "https://example.test/room/access-token",
        reused=True,
        restarted=False,
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_new_soup_room_uses_the_full_opening_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群里让模型建海龟汤房时，回的是「开好了 + 已入席 + 怎么玩」，不是裸链接。"""
    import astrbot_plugin_game_companion_huahuo.main as main_module

    seat = AsyncMock(
        return_value="已经把你排进玩家席并开局，选一种方式就能玩：\n· 汤 提问 你的问题"
    )
    monkeypatch.setattr(main_module, "_seat_and_start", seat)
    plugin = make_plugin(delivered=True)
    room = make_room()
    room.game_type = "turtle_soup"
    room.admin_room = False
    url = "https://example.test/room/access-token"

    delivered = await plugin._deliver_room_link(
        room,
        url,
        reused=False,
        restarted=False,
        requester_qq="10001",
        requester_name="小明",
    )

    assert delivered is True
    seat.assert_awaited_once()
    _session_id, chain = plugin.context.send_message.await_args.args
    text = chain.chain[0].text
    assert text.startswith("🥣 海龟汤开好了")
    assert url in text
    assert "汤 提问 你的问题" in text
    assert "房间已准备好" not in text


@pytest.mark.asyncio
async def test_reused_soup_room_keeps_the_plain_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复用已有房间时不该假装「开好了」，也不重复安排座位。"""
    import astrbot_plugin_game_companion_huahuo.main as main_module

    seat = AsyncMock()
    monkeypatch.setattr(main_module, "_seat_and_start", seat)
    plugin = make_plugin(delivered=True)
    room = make_room()
    room.game_type = "turtle_soup"
    room.admin_room = False

    await plugin._deliver_room_link(
        room,
        "https://example.test/room/access-token",
        reused=True,
        restarted=False,
        requester_qq="10001",
    )

    seat.assert_not_awaited()
    _session_id, chain = plugin.context.send_message.await_args.args
    assert chain.chain[0].text.startswith("继续使用当前游戏房间：")
