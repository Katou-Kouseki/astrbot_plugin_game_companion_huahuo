"""严格轮次海龟汤：换人时要在群里播报「轮到谁」，能 @ 就 @。

这项功能的坑集中在「什么时候不该播报」上：自由提问没有轮次、一个人玩没有
对象、等出题人裁决时还没轮到任何人。播报多了群里会反感，所以每一条排除
条件都单独锁住。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    TurtleSoupGame,
    puzzle_from_mapping,
)


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "测试题",
            "surface": "一个人每天打开同一扇门，却从来不从这扇门进入房间，这是为什么？",
            "solution": "这是温室的通风门。他负责照料植物，真正供人出入的门位于温室另一侧。",
            "key_facts": ["门用于通风", "房间是温室", "入口位于另一侧"],
            "acceptable_variants": [],
            "hints": ["门不只用于通行", "与环境有关", "房内有植物"],
            "theme": "温室",
            "trick": "把通风门当成入口",
        },
        content_level="all_ages",
    )


async def build_room(
    manager: RoomManager, *, source: str = "group", players: int = 2
):
    room = await manager.create_room(
        source=source,  # type: ignore[arg-type]
        session_id="aiocqhttp:group:20001",
        platform="aiocqhttp",
        group_id="20001" if source == "group" else "",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
        turtle_soup_mode="bot_host",
    )
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    # 单人房在 claim_and_start 里就已经开局，多人房要等坐满/手动开始
    if room.status == "setup":
        await manager.start_game(room, first.token, "")
    if isinstance(room.game, TurtleSoupGame) and room.game.puzzle is None:
        await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    for index in range(players - 1):
        extra = await manager.join(room)
        await manager.claim_and_start(room, extra.token, "")
        seat = room.multiplayer.seat_for_token(extra.token)
        assert seat is not None
        seat.qq = f"2000{index + 2}"
        seat.identity_confirmed = True
    return room


def collector():
    events: list[tuple[str, dict]] = []

    async def callback(name, _room, payload):
        # 房间还有 seats_changed / game_started 等噪声事件，这里只看轮次播报
        if name == "soup_turn_changed":
            events.append((name, dict(payload)))

    return events, callback


@pytest.mark.asyncio
async def test_strict_turn_announces_the_next_seat_with_its_qq() -> None:
    """2 人严格轮次：第一次推进就该告诉群里「轮到 2 号」，并带上他的 QQ。"""
    events, callback = collector()
    manager = RoomManager(
        turtle_soup_max_players=0,
        turtle_soup_free_questioning=False,
        event_callback=callback,
    )
    room = await build_room(manager)

    manager._advance_multiplayer_turn(room)  # noqa: SLF001
    await asyncio.sleep(0)

    assert [name for name, _ in events] == ["soup_turn_changed"]
    payload = events[0][1]
    assert payload["seat_number"] == 2
    assert payload["qq"] == "20002"
    assert payload["seconds"] > 0


@pytest.mark.asyncio
async def test_free_questioning_never_announces() -> None:
    """自由提问没有轮次概念，播报只会变成噪音。"""
    events, callback = collector()
    manager = RoomManager(
        turtle_soup_max_players=0,
        turtle_soup_free_questioning=True,
        event_callback=callback,
    )
    room = await build_room(manager)

    manager._advance_multiplayer_turn(room)  # noqa: SLF001
    await asyncio.sleep(0)

    assert events == []


@pytest.mark.asyncio
async def test_single_player_room_never_announces() -> None:
    """一个人的房间永远是自己，每问一句都播报一遍没有意义。"""
    events, callback = collector()
    manager = RoomManager(
        turtle_soup_max_players=1,
        turtle_soup_free_questioning=False,
        event_callback=callback,
    )
    room = await build_room(manager, players=1)

    manager._advance_multiplayer_turn(room)  # noqa: SLF001
    await asyncio.sleep(0)

    assert events == []


@pytest.mark.asyncio
async def test_same_seat_is_announced_only_once() -> None:
    """同一轮可能被提问/提示/裁决多条路径各推进一次，播报要幂等。"""
    events, callback = collector()
    manager = RoomManager(
        turtle_soup_max_players=0,
        turtle_soup_free_questioning=False,
        event_callback=callback,
    )
    room = await build_room(manager)

    manager._maybe_announce_soup_turn(room)  # noqa: SLF001
    manager._maybe_announce_soup_turn(room)  # noqa: SLF001
    await asyncio.sleep(0)

    assert len(events) == 1


@pytest.mark.asyncio
async def test_waiting_for_host_verdict_does_not_announce() -> None:
    """等出题人裁决时还没轮到任何人提问，说「轮到 X」是误导。"""
    events, callback = collector()
    manager = RoomManager(
        turtle_soup_max_players=0,
        turtle_soup_free_questioning=False,
        event_callback=callback,
    )
    room = await build_room(manager)
    room.game.processing = True

    manager._maybe_announce_soup_turn(room)  # noqa: SLF001
    await asyncio.sleep(0)

    assert events == []


def make_plugin(**overrides):
    plugin = object.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin.turtle_soup_qq_bridge = True
    plugin.turtle_soup_qq_turn_announce = True
    for key, value in overrides.items():
        setattr(plugin, key, value)
    return plugin


@pytest.mark.asyncio
async def test_announce_ats_the_bound_player(monkeypatch) -> None:
    from astrbot.api import message_components as components

    class FakeAt:
        def __init__(self, qq: str = "", name: str = "") -> None:
            self.qq = qq
            self.name = name

    monkeypatch.setattr(components, "At", FakeAt, raising=False)
    plugin = make_plugin()
    room = SimpleNamespace(source="group", session_id="aiocqhttp:group:20001")

    await plugin._announce_soup_turn(  # noqa: SLF001
        room, {"seat_number": 2, "qq": "20002", "display_name": "小明", "seconds": 60}
    )

    plugin.context.send_message.assert_awaited_once()
    chain = plugin.context.send_message.await_args.args[1].chain
    ats = [part for part in chain if isinstance(part, FakeAt)]
    assert [at.qq for at in ats] == ["20002"]
    text = "".join(getattr(part, "text", "") for part in chain)
    assert "轮到" in text and "60 秒" in text


@pytest.mark.asyncio
async def test_unbound_seat_falls_back_to_seat_number() -> None:
    """网页玩家多半没绑 QQ，@ 一个空 QQ 会把整条消息吞掉，只能退成文字。"""
    plugin = make_plugin()
    room = SimpleNamespace(source="group", session_id="aiocqhttp:group:20001")

    await plugin._announce_soup_turn(  # noqa: SLF001
        room, {"seat_number": 3, "qq": "", "display_name": "小明", "seconds": 0}
    )

    chain = plugin.context.send_message.await_args.args[1].chain
    text = "".join(getattr(part, "text", "") for part in chain)
    assert "3 号玩家" in text and "小明" in text
    assert all(type(part).__name__ == "Plain" for part in chain)


@pytest.mark.asyncio
async def test_private_room_is_not_announced() -> None:
    """私聊房里「轮到 3 号」没有任何意义，只会在私聊里冒出一条莫名其妙的消息。"""
    plugin = make_plugin()
    room = SimpleNamespace(source="private", session_id="aiocqhttp:FriendMessage:1")

    await plugin._announce_soup_turn(  # noqa: SLF001
        room, {"seat_number": 1, "qq": "10001", "display_name": "", "seconds": 60}
    )

    plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag", ["turtle_soup_qq_bridge", "turtle_soup_qq_turn_announce"]
)
async def test_switch_off_silences_announce(flag: str) -> None:
    plugin = make_plugin(**{flag: False})
    room = SimpleNamespace(source="group", session_id="aiocqhttp:group:20001")

    await plugin._announce_soup_turn(  # noqa: SLF001
        room, {"seat_number": 2, "qq": "20002", "display_name": "", "seconds": 60}
    )

    plugin.context.send_message.assert_not_awaited()
