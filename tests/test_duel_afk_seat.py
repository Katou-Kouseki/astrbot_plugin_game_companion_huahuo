from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_room(manager: RoomManager):
    return await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:39002",
        platform="aiocqhttp",
        group_id="39002",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )


def has_message(room, needle: str) -> bool:
    return any(needle in str(item.get("content") or "") for item in room.messages)


@pytest.mark.asyncio
async def test_release_afk_duel_seat_when_someone_is_waiting() -> None:
    manager = RoomManager(duel_afk_seat_timeout=120)
    room = await make_room(manager)
    first = await manager.join(room)
    standby_a = await manager.join(room)
    standby_b = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")

    now = time.time()
    seat = room.multiplayer.seats[0]
    seat.last_action_at = now - 200
    # 页面还开着、心跳一直新鲜：服务端认为他「在线」，只有操作时间暴露他不在
    first.connected = True
    first.last_seen_at = now
    for visitor in (standby_a, standby_b):
        visitor.connected = True
        visitor.last_seen_at = now

    manager._release_afk_duel_seats(room, now)

    assert room.multiplayer.seats == []
    assert has_message(room, "已让出座位")
    assert room.status == "waiting"


@pytest.mark.asyncio
async def test_afk_seat_kept_when_no_one_is_waiting() -> None:
    manager = RoomManager(duel_afk_seat_timeout=120)
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")

    now = time.time()
    room.multiplayer.seats[0].last_action_at = now - 200
    first.connected = True
    first.last_seen_at = now

    manager._release_afk_duel_seats(room, now)

    # 没有候补观众时不动：可能是真的在等朋友
    assert len(room.multiplayer.seats) == 1


@pytest.mark.asyncio
async def test_claim_asks_for_confirmation_when_opponent_is_afk() -> None:
    manager = RoomManager(duel_afk_seat_timeout=120)
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")

    now = time.time()
    room.multiplayer.seats[0].last_action_at = now - 200
    first.connected = True
    first.last_seen_at = now
    second.connected = True
    second.last_seen_at = now

    result = await manager.claim_and_start(room, second.token, "")

    assert result["need_confirm"] is True
    assert result["opponent_idle_seconds"] >= 120
    # 未确认前不能动座位，也不能开局
    assert len(room.multiplayer.seats) == 1
    assert room.multiplayer.seat_for_token(second.token) is None
    assert room.status == "setup"


@pytest.mark.asyncio
async def test_takeover_replaces_afk_seat_without_starting_game() -> None:
    manager = RoomManager(duel_afk_seat_timeout=120)
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")

    now = time.time()
    room.multiplayer.seats[0].last_action_at = now - 200
    first.connected = True
    first.last_seen_at = now
    second.connected = True
    second.last_seen_at = now

    result = await manager.claim_and_start(room, second.token, "", takeover=True)

    assert result.get("claimed") is True
    assert room.multiplayer.seat_for_token(first.token) is None
    assert room.multiplayer.seat_for_token(second.token) is not None
    # 顶替后只有一个人：停在 setup 等另一位玩家，不能直接开局对空气
    assert room.status == "setup"
    assert room.game is None
    assert has_message(room, "已让出座位")


@pytest.mark.asyncio
async def test_afk_release_skipped_during_active_game() -> None:
    manager = RoomManager(duel_afk_seat_timeout=120)
    room = await make_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    await manager.claim_and_start(room, second.token, "")
    assert room.status == "active"

    now = time.time()
    for seat in room.multiplayer.seats:
        seat.last_action_at = now - 600
    first.connected = True
    first.last_seen_at = now
    second.connected = True
    second.last_seen_at = now

    manager._release_afk_duel_seats(room, now)

    # 对局中走「掉线暂停 → 判负」，这里不能直接摘座位
    assert len(room.multiplayer.seats) == 2
