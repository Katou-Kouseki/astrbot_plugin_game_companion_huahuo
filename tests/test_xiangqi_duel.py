from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


class DuelPikafish:
    """足够跑通双人象棋流程的假引擎：按手数轮换给出红/黑着法。"""

    def __init__(self) -> None:
        self.ready = False

    async def ensure_ready(self) -> None:
        self.ready = True

    async def legal_moves(self, moves: list[str]) -> list[str]:
        if len(moves) % 2 == 0:
            return ["a3a4", "b0c2"]
        return ["a6a5", "h9g7"]

    async def choose_move(self, moves: list[str], _difficulty: str) -> str:
        return (await self.legal_moves(moves))[0]


async def make_room(manager: RoomManager, game_type: str = "xiangqi"):
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


async def two_human_room():
    manager = RoomManager(xiangqi_engine=DuelPikafish(), duel_turn_timeout=180)
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_red")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    assert room.status == "active"
    return manager, room, first, second


def _go_online(room, now: float) -> None:
    for visitor in room.visitors.values():
        visitor.last_seen_at = now
        visitor.connected = True


@pytest.mark.asyncio
async def test_two_humans_get_red_and_black_and_move_in_turn() -> None:
    manager, room, first, second = await two_human_room()

    seats = room.multiplayer.seats
    assert [seat.side for seat in seats] == ["red", "black"]
    assert room.game.turn == "red"
    assert room.game.human_side == "red"

    # 红方（1 号座位）走兵，随后轮到黑方
    await manager.player_move(
        room, first.token, from_row=6, from_column=0, to_row=5, to_column=0
    )
    assert room.game.moves == ["a3a4"]
    assert room.game.turn == "black"

    # 黑方（2 号座位）卒
    await manager.player_move(
        room, second.token, from_row=3, from_column=0, to_row=4, to_column=0
    )
    assert room.game.moves == ["a3a4", "a6a5"]
    assert room.game.turn == "red"


@pytest.mark.asyncio
async def test_xiangqi_seat_cannot_move_for_the_other_side() -> None:
    manager, room, first, second = await two_human_room()

    # 轮到红方时黑方不能替对手走棋
    with pytest.raises(ValueError, match="还没轮到你走棋"):
        await manager.player_move(
            room, second.token, from_row=3, from_column=0, to_row=4, to_column=0
        )
    # 红方也不能走黑方的子
    with pytest.raises(ValueError, match="不能走到该位置"):
        await manager.player_move(
            room, first.token, from_row=3, from_column=0, to_row=4, to_column=0
        )


@pytest.mark.asyncio
async def test_snapshot_legal_moves_follow_the_viewing_seat() -> None:
    manager, room, first, second = await two_human_room()

    red_view = room.public_snapshot(first.token)["game"]
    black_view = room.public_snapshot(second.token)["game"]

    assert red_view["viewer_side"] == "red"
    assert black_view["viewer_side"] == "black"
    # 只有该走棋的一方能看到落点提示
    assert red_view["legal_moves"]
    assert black_view["legal_moves"] == []


@pytest.mark.asyncio
async def test_xiangqi_offline_pause_then_forfeit_to_other_side() -> None:
    manager, room, _first, second = await two_human_room()
    now = time.time()
    _go_online(room, now)
    manager._reset_turn_deadline(room, now=now)

    owner = manager._duel_turn_owner(room)
    assert owner is not None
    visitor = room.visitors[owner.visitor_token]
    visitor.connected = False
    visitor.last_seen_at = now - 60

    await manager.sweep_expired(now=now)
    assert room.multiplayer.duel_paused
    assert room.status == "active"

    # 重连宽限期结束后判负：赢家是另一方（对手保持在线）
    for other in room.visitors.values():
        if other.token != owner.visitor_token:
            other.connected = True
            other.last_seen_at = now + 91
    await manager.sweep_expired(now=now + 91)
    assert room.status == "finished"
    assert room.game.winner == "black"
    assert room.human_wins == 0 and room.bot_wins == 1


@pytest.mark.asyncio
async def test_xiangqi_play_with_bot_seat_puts_bot_on_the_other_side() -> None:
    manager = RoomManager(xiangqi_engine=DuelPikafish(), duel_turn_timeout=180)
    room = await make_room(manager)
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_red")

    # 我执黑：花火执红先走，走完轮到真人
    payload = await manager.add_ai_seat(room, player.token, side="human_black")
    assert payload["started"] is True
    assert room.status == "active"
    await manager.wait_bot_turns()

    seat = room.multiplayer.seat_for_token(player.token)
    assert seat is not None and seat.side == "black"
    assert room.game.human_side == "black"
    assert room.game.turn == "black"
    assert room.game.moves == ["a3a4"]
    # 只有一位真人：练习局不计时
    assert room.multiplayer.turn_deadline == 0


@pytest.mark.asyncio
async def test_undo_is_refused_between_two_humans() -> None:
    manager, room, _first, _second = await two_human_room()

    with pytest.raises(PermissionError, match="不能悔棋"):
        await manager.undo(room)


@pytest.mark.asyncio
async def test_xiangqi_resign_and_rematch_swap_colors() -> None:
    manager, room, _first, second = await two_human_room()

    # 黑方（2 号座位）认输，红方获胜
    await manager.resign(room, visitor_token=second.token)
    assert room.status == "finished"
    assert room.game.winner == "red"
    assert room.human_wins == 1 and room.bot_wins == 0

    # 再来一局：红黑互换，仍是红方先走
    await manager.restart_finished_game(room, difficulty="normal")
    assert room.status == "active"
    assert [seat.side for seat in room.multiplayer.seats] == ["black", "red"]
    assert room.game.turn == "red"
    assert room.game.human_side == "black"


@pytest.mark.asyncio
async def test_xiangqi_swap_request_is_blocked_during_a_duel() -> None:
    manager, room, _first, second = await two_human_room()
    spectator = await manager.join(room)

    with pytest.raises(ValueError, match="不能换座"):
        await manager.request_seat_swap(room, spectator.token, second.number)
