from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.models import PlayerSeat, _visitor_uid
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.undercover import UndercoverGame


async def make_room(manager: RoomManager):
    return await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:39001",
        platform="aiocqhttp",
        group_id="39001",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )


async def seat_players(manager: RoomManager, room, count: int = 2):
    visitors = []
    for _ in range(count):
        visitor = await manager.join(room)
        room.multiplayer.seats.append(
            PlayerSeat(
                visitor_token=visitor.token,
                display_name=visitor.display_name,
            )
        )
        visitors.append(visitor)
    game = UndercoverGame()
    game.attach_players(
        [(index + 1, visitor.qq, visitor.display_name) for index, visitor in enumerate(visitors)],
        uids=[_visitor_uid(visitor.token) for visitor in visitors],
    )
    game.phase = "speech"
    game.speaking_order = list(range(1, count + 1))
    game.current_speaker_index = 0
    room.game = game
    room.status = "active"
    return visitors, game


def has_message(room, needle: str) -> bool:
    return any(needle in str(item.get("content") or "") for item in room.messages)


@pytest.mark.asyncio
async def test_undercover_offline_during_match_keeps_seat_and_enables_autopilot() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()
    visitors[0].connected = False
    visitors[0].left_at = now - manager.DEPARTED_LEAVE_GRACE_SECONDS - 1

    manager._vacate_departed_seats(room, now)

    # 座位必须保留：摘掉座位会让投票名单里的这个人永远补不上票
    assert len(room.multiplayer.seats) == 2
    assert game.players[0].autopilot is True
    assert has_message(room, "已切换托管")


@pytest.mark.asyncio
async def test_undercover_reconnect_clears_autopilot() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()
    visitors[0].connected = False
    visitors[0].left_at = now - manager.DEPARTED_LEAVE_GRACE_SECONDS - 1
    manager._vacate_departed_seats(room, now)
    assert game.players[0].autopilot is True

    visitors[0].connected = True
    visitors[0].left_at = None
    visitors[0].last_seen_at = now
    manager._restore_undercover_autopilot(room, now)

    assert game.players[0].autopilot is False
    assert has_message(room, "托管已解除")


@pytest.mark.asyncio
async def test_undercover_offline_before_match_releases_seat() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    room.status = "setup"
    game.phase = "idle"
    now = time.time()
    visitors[0].connected = False
    visitors[0].left_at = now - manager.DEPARTED_LEAVE_GRACE_SECONDS - 1

    manager._vacate_departed_seats(room, now)

    assert len(room.multiplayer.seats) == 1
    assert has_message(room, "已离线，已移出玩家席")


@pytest.mark.asyncio
async def test_autopilot_due_when_current_speaker_is_offline() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()

    visitors[0].connected = True
    visitors[0].last_seen_at = now
    assert manager._undercover_autopilot_due(room, now) is False

    visitors[0].connected = False
    assert manager._undercover_autopilot_due(room, now) is True


@pytest.mark.asyncio
async def test_afk_autopilot_has_explicit_restore_path() -> None:
    """在线挂机托管要留标记；心跳恢复不解除，真实操作才解除。"""
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()
    for visitor in visitors:
        visitor.connected = True
        visitor.last_seen_at = now

    # 先让行动点建立起来，再把计时起点往前拨，模拟「轮到他之后一直没动」
    assert manager._undercover_autopilot_due(room, now) is False
    stale = now - manager.UNDERCOVER_AFK_SECONDS - 1
    room.undercover_action_at = stale
    room.multiplayer.seats[0].last_action_at = stale
    assert manager._undercover_autopilot_due(room, now) is True

    player = game.players[0]
    assert player.autopilot is True
    assert player.autopilot_reason == "afk"
    assert has_message(room, "已临时托管")

    # 心跳一直新鲜，重连流程不能把 AFK 托管秒解除
    manager._restore_undercover_autopilot(room, now)
    assert player.autopilot is True

    # 玩家真的操作了一次才算回来
    manager._touch_seat_action(room, visitors[0].token, now=now)
    assert player.autopilot is False
    assert player.autopilot_reason == ""
    assert has_message(room, "托管解除")


@pytest.mark.asyncio
async def test_offline_autopilot_is_cleared_on_reconnect() -> None:
    """掉线托管仍按老规则：心跳恢复即解除，不受新增 AFK 分支影响。"""
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()
    visitors[0].connected = False
    visitors[0].left_at = now - manager.DEPARTED_LEAVE_GRACE_SECONDS - 1
    manager._vacate_departed_seats(room, now)
    assert game.players[0].autopilot_reason == "offline"

    visitors[0].connected = True
    visitors[0].last_seen_at = now
    manager._restore_undercover_autopilot(room, now)
    assert game.players[0].autopilot is False
    assert game.players[0].autopilot_reason == ""


@pytest.mark.asyncio
async def test_autopilot_due_ignored_for_online_players() -> None:
    manager = RoomManager()
    room = await make_room(manager)
    visitors, game = await seat_players(manager, room)
    now = time.time()
    for visitor in visitors:
        visitor.connected = True
        visitor.last_seen_at = now

    assert manager._undercover_autopilot_due(room, now) is False
