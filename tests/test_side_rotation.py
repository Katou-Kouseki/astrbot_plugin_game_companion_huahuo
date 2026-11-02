"""房间内「再来一局」换边方式 + 花火整体思考延迟。"""

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def _solo_room(manager: RoomManager, side: str = "human_black"):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:session",
        platform="aiocqhttp",
        group_id="20001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, side)
    await manager.add_ai_seat(room, visitor.token, side=side)
    await manager.wait_bot_turns()
    return room, visitor


async def _next_game(manager: RoomManager, room, visitor) -> None:
    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    await manager.start_game(room, visitor.token, "human_black")
    await manager.wait_bot_turns()


@pytest.mark.asyncio
async def test_fixed_mode_keeps_player_color_across_rematches() -> None:
    """选「保持执色」后连开三局，玩家执色不再互换。"""
    manager = RoomManager(max_private_rooms=0)
    room, visitor = await _solo_room(manager)
    await manager.player_set_side_rotation(room, visitor.token, "fixed")
    colors = [room.game.human_color]

    for _ in range(3):
        await _next_game(manager, room, visitor)
        colors.append(room.game.human_color)

    assert colors == [1, 1, 1, 1]
    assert [seat.side for seat in room.multiplayer.seats] == ["black", "white"]


@pytest.mark.asyncio
async def test_auto_mode_alternates_every_game() -> None:
    """选「每局换边」时严格交替，和全局配置开启时一致。"""
    manager = RoomManager(max_private_rooms=0)
    room, visitor = await _solo_room(manager)
    await manager.player_set_side_rotation(room, visitor.token, "auto")
    colors = [room.game.human_color]

    for _ in range(3):
        await _next_game(manager, room, visitor)
        colors.append(room.game.human_color)

    assert colors == [1, 2, 1, 2]


@pytest.mark.asyncio
async def test_side_rotation_is_refused_between_two_humans() -> None:
    """两位真人对局不允许任何一方在页面上改换边规则。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:session",
        platform="aiocqhttp",
        group_id="20002",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    await manager.claim_and_start(room, second.token, "human_black")

    state = manager.side_rotation_state(room, first.token)
    assert state["configurable"] is False

    with pytest.raises(PermissionError):
        await manager.player_set_side_rotation(room, first.token, "fixed")


@pytest.mark.asyncio
async def test_side_rotation_requires_a_seat() -> None:
    """观众不能改；未入座时前端也不该显示这一行。"""
    manager = RoomManager(max_private_rooms=0)
    room, visitor = await _solo_room(manager)
    spectator = await manager.join(room)

    assert manager.side_rotation_state(room, spectator.token)["configurable"] is False
    assert manager.side_rotation_state(room, visitor.token)["configurable"] is True

    with pytest.raises(PermissionError):
        await manager.player_set_side_rotation(room, spectator.token, "fixed")


def test_bot_think_delay_follows_config_and_difficulty() -> None:
    """整体思考延迟是基础值，难度只做小幅增减。"""
    from astrbot_plugin_game_companion_huahuo.models import GameRoom

    room = GameRoom(
        room_id="r1",
        access_token="t",
        source="group",
        session_id="UMO:t:Group:1",
        platform="aiocqhttp",
        group_id="1",
        creator_qq="1",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    manager = RoomManager(max_private_rooms=0, ai_think_delay=0.8)

    delays = {}
    for difficulty in ("easy", "normal", "hard"):
        room.difficulty = difficulty
        delays[difficulty] = manager.bot_think_delay(room)
    assert delays["easy"] < delays["normal"] < delays["hard"]
    assert abs(delays["normal"] - 0.85) < 1e-6

    manager.ai_think_delay = 2.0
    room.difficulty = "normal"
    assert abs(manager.bot_think_delay(room) - 2.05) < 1e-6

    # 设成 0 也不会「毫无停顿」，仍然留一个最短思考时间
    manager.ai_think_delay = 0.0
    room.difficulty = "easy"
    assert manager.bot_think_delay(room) >= 0.15
