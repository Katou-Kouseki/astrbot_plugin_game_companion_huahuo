"""两人对弈的回合倒计时：掉线暂停、超时判负。

只对「两位真人」的对局计时；和花火练习局不设倒计时，避免挂机直接判输。
"""

from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import GomokuGame
from astrbot_plugin_game_companion_huahuo.othello import OthelloGame
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_duel_room(manager: RoomManager, game_type: str = "gomoku"):
    return await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )


async def two_human_duel(game_type: str = "gomoku"):
    """两个人各占一席并开局。"""
    manager = RoomManager(duel_turn_timeout=180, duel_offline_grace=90)
    room = await make_duel_room(manager, game_type)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    if room.status != "active":
        await manager.start_game(room, first.token, "")
    assert room.status == "active"
    assert isinstance(room.game, (GomokuGame, OthelloGame))
    return manager, room, first, second


def keep_others_online(room, current_token: str, now: float) -> None:
    """除当前回合玩家外，其他座位保持在线（模拟持续心跳）。"""
    for visitor in room.visitors.values():
        if visitor.token != current_token:
            visitor.last_seen_at = now
            visitor.connected = True


@pytest.mark.asyncio
async def test_duel_offline_player_pauses_then_forfeits() -> None:
    manager, room, _first, _second = await two_human_duel()
    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now
        visitor.connected = True
    manager._reset_turn_deadline(room, now=now)
    assert room.multiplayer.turn_deadline == pytest.approx(now + 180)

    seat = manager._duel_turn_owner(room)
    assert seat is not None and not seat.is_ai
    loser = room.visitors[seat.visitor_token]
    loser.connected = False
    loser.last_seen_at = now - 30

    await manager.sweep_expired(now=now)
    # 掉线立刻冻结棋盘，而不是等到每步超时
    assert room.multiplayer.duel_paused is True
    assert room.status == "active"
    assert room.game.winner == 0

    keep_others_online(room, loser.token, now + 91)
    loser.last_seen_at = now - 30
    await manager.sweep_expired(now=now + 91)
    assert room.status == "finished"
    assert room.game.winner != 0
    assert room.multiplayer.duel_paused is False


@pytest.mark.asyncio
async def test_duel_online_player_forfeits_on_turn_timeout() -> None:
    manager, room, _first, _second = await two_human_duel()
    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now
        visitor.connected = True
    manager._reset_turn_deadline(room, now=now)

    seat = manager._duel_turn_owner(room)
    assert seat is not None
    # 心跳一直在，只是不落子
    for visitor in room.visitors.values():
        visitor.last_seen_at = now + 181
    await manager.sweep_expired(now=now + 181)

    assert room.status == "finished"
    assert room.game.winner != 0
    assert room.multiplayer.duel_paused is False


@pytest.mark.asyncio
async def test_duel_reconnecting_player_resumes_clock() -> None:
    manager, room, _first, _second = await two_human_duel()
    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now
        visitor.connected = True
    manager._reset_turn_deadline(room, now=now)

    seat = manager._duel_turn_owner(room)
    assert seat is not None
    loser = room.visitors[seat.visitor_token]
    loser.last_seen_at = now - 30

    await manager.sweep_expired(now=now)
    assert room.multiplayer.duel_paused is True

    loser.last_seen_at = now + 5
    keep_others_online(room, loser.token, now + 6)
    await manager.sweep_expired(now=now + 6)

    assert room.multiplayer.duel_paused is False
    assert room.status == "active"
    assert room.multiplayer.turn_deadline == pytest.approx(now + 6 + 180)


@pytest.mark.asyncio
async def test_duel_timeout_disabled_when_zero() -> None:
    manager = RoomManager(duel_turn_timeout=0, idle_timeout=0)
    room = await make_duel_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    if room.status != "active":
        await manager.start_game(room, first.token, "")
    assert room.multiplayer.turn_timeout_seconds == 0

    manager._reset_turn_deadline(room)
    assert room.multiplayer.turn_deadline == 0.0

    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now
        visitor.connected = True
    await manager.sweep_expired(now=now + 600)
    assert room.status == "active"


@pytest.mark.asyncio
async def test_othello_uses_the_same_turn_clock() -> None:
    manager, room, _first, _second = await two_human_duel("othello")
    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now + 181
    manager._reset_turn_deadline(room, now=now)
    await manager.sweep_expired(now=now + 181)

    assert room.status == "finished"
    assert room.game.winner != 0


@pytest.mark.asyncio
async def test_practice_game_against_ai_has_no_turn_clock() -> None:
    manager = RoomManager(duel_turn_timeout=180, duel_offline_grace=90)
    room = await make_duel_room(manager)
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "")
    await manager.start_game(room, player.token, "")

    assert room.status == "active"
    assert any(seat.is_ai for seat in room.multiplayer.seats)
    assert room.multiplayer.turn_deadline == 0.0

    now = time.time()
    for visitor in room.visitors.values():
        visitor.last_seen_at = now - 600
        visitor.connected = False
    await manager.sweep_expired(now=now)

    # 和花火练习：掉线不暂停、不判负
    assert room.status == "active"
    assert room.multiplayer.duel_paused is False
