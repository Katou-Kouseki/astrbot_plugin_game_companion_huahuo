"""海龟汤玩家席：群里玩的人不该因为「没有网页心跳」被悄悄移出去。

现象：QQ 群里开的局，玩家一直在群里提问，过一会儿却被移出玩家席——
因为网页心跳那条路对他根本不存在。网页玩家关掉标签页再回来也一样，
链接还没失效，座位却没了。
"""

from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "海边的钟",
            "surface": "海边小镇的钟每天都会慢一分钟，镇上的人却毫不在意。为什么？",
            "solution": (
                "那口钟是给渔船看的时间，船长们按它出海，慢一分钟刚好避开暗流。"
            ),
            "key_facts": ["钟是给渔船看的", "慢一分钟是故意的", "为避开暗流"],
            "acceptable_variants": ["说时间被故意调慢即可"],
            "hints": ["钟的用途特别", "慢是人为的", "和出海有关"],
            "theme": "海边",
            "trick": "故意调慢的钟",
        },
        content_level="normal",
    )


async def make_active_soup(manager: RoomManager, *, status: str = "active"):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:GroupMessage:46001",
        platform="aiocqhttp",
        group_id="46001",
        creator_qq="30001",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "30001", allow_non_numeric=True)
    await manager.start_game(room, visitor.token, "")
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    if status != "active":
        room.status = status
    return room, visitor


@pytest.mark.asyncio
async def test_qq_player_keeps_the_seat_while_thinking() -> None:
    """对局中不说话 10 分钟也不摘座位：这是「想想再问」的玩法。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager)
    visitor.connected = False
    visitor.last_seen_at = time.time() - 600

    manager._vacate_departed_seats(room, time.time())

    assert room.multiplayer.seats, "对局中的海龟汤不该把玩家移出玩家席"


@pytest.mark.asyncio
async def test_closed_tab_can_come_back_mid_game() -> None:
    """网页关了（left_at 已写）再打开链接，座位还在。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager)
    await manager.leave(room, visitor.token)

    manager._vacate_departed_seats(room, time.time())

    assert room.multiplayer.seat_for_token(visitor.token) is not None


@pytest.mark.asyncio
async def test_group_player_has_a_much_longer_offline_window() -> None:
    """结算后群友还在群里聊复盘，别按浏览器那套 90 秒就回收。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager, status="finished")
    visitor.last_seen_at = time.time() - 300

    deadline = manager._visitor_offline_seconds(room, visitor)

    assert deadline >= manager.QQ_SEAT_OFFLINE_SECONDS
    manager._vacate_departed_seats(room, time.time())
    assert room.multiplayer.seat_for_token(visitor.token) is not None


@pytest.mark.asyncio
async def test_browser_player_is_still_released_when_waiting() -> None:
    """准备阶段人真的走了还是要让位：不然一个空座位能堵住整局。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager, status="waiting")
    visitor.connected = False
    visitor.last_seen_at = time.time() - 600
    # 这个房间是群房，但访客没绑 QQ（纯网页身份）
    visitor.qq = ""

    manager._vacate_departed_seats(room, time.time())

    assert room.multiplayer.seat_for_token(visitor.token) is None
