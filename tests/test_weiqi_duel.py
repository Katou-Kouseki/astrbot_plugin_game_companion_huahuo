from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.weiqi import WeiqiGame


async def make_room(manager: RoomManager, game_type: str = "weiqi"):
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
    manager = RoomManager(
        max_private_rooms=5, duel_turn_timeout=180, weiqi_board_size=9, weiqi_komi=7.5
    )
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    assert room.status == "active"
    return manager, room, first, second


@pytest.mark.asyncio
async def test_two_humans_get_black_and_white_and_move_in_turn() -> None:
    manager, room, first, second = await two_human_room()

    assert isinstance(room.game, WeiqiGame)
    assert room.game.size == 9
    assert [seat.side for seat in room.multiplayer.seats] == ["black", "white"]
    assert room.game.turn == BLACK

    await manager.player_move(room, first.token, row=4, column=4)
    assert room.game.board[4][4] == BLACK
    assert room.game.turn == WHITE

    with pytest.raises(ValueError):
        await manager.player_move(room, first.token, row=2, column=2)

    await manager.player_move(room, second.token, row=4, column=5)
    assert room.game.board[4][5] == WHITE
    assert room.game.turn == BLACK
    snapshot = room.public_snapshot(first.token)["game"]
    assert snapshot["kind"] == "weiqi"
    assert snapshot["eval"]["black"] > 0


@pytest.mark.asyncio
async def test_double_pass_finishes_the_game() -> None:
    manager, room, first, second = await two_human_room()

    await manager.player_move(room, first.token, row=4, column=4)
    await manager.player_pass(room, second.token)
    assert room.status == "active"
    assert room.game.consecutive_passes == 1
    await manager.player_pass(room, first.token)
    assert room.game.finished is True
    assert room.status == "finished"
    assert room.game.result["margin"] != 0
    assert room.completed_games == 1


@pytest.mark.asyncio
async def test_pass_is_rejected_out_of_turn() -> None:
    _manager, room, _first, second = await two_human_room()
    with pytest.raises(ValueError):
        await _manager.player_pass(room, second.token)


@pytest.mark.asyncio
async def test_ai_seat_answers_after_human_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "astrbot_plugin_game_companion_huahuo.room_manager.asyncio.sleep", no_delay
    )
    manager = RoomManager(max_private_rooms=5, weiqi_board_size=9, weiqi_komi=7.5)
    room = await make_room(manager)
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)
    assert room.status == "active"
    assert len(room.multiplayer.seats) == 2
    assert room.multiplayer.seats[1].is_ai

    await manager.player_move(room, visitor.token, row=4, column=4)
    await manager.wait_bot_turns()
    # 花火用本地启发式应一手，棋盘上应出现白子
    assert len(room.game.history) >= 2
    assert any(cell == WHITE for row in room.game.board for cell in row)


@pytest.mark.asyncio
async def test_admin_can_switch_difficulty_for_adjustable_games() -> None:
    manager = RoomManager(max_private_rooms=5, weiqi_board_size=9)
    room = await make_room(manager)
    assert room.admin_snapshot()["difficulty_adjustable"] is True

    await manager.set_difficulty(room, "hard")
    assert room.difficulty == "hard"

    # 贪心骰子/二十一点也是强度类玩法，可调难度
    pascal = await make_room(manager, game_type="pig_dice")
    assert pascal.admin_snapshot()["difficulty_adjustable"] is True
    await manager.set_difficulty(pascal, "hard")
    assert pascal.difficulty == "hard"

    # 海龟汤等非强度类玩法不可调
    soup = await make_room(manager, game_type="turtle_soup")
    assert soup.admin_snapshot()["difficulty_adjustable"] is False
    with pytest.raises(ValueError):
        await manager.set_difficulty(soup, "hard")


@pytest.mark.asyncio
async def test_difficulty_switch_reaches_the_live_game() -> None:
    manager = RoomManager(max_private_rooms=5, weiqi_board_size=9)
    room = await make_room(manager)
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)
    assert room.game.difficulty == "normal"
    await manager.set_difficulty(room, "easy")
    assert room.game.difficulty == "easy"
    assert room.difficulty == "easy"



@pytest.mark.asyncio
async def test_first_seated_player_can_change_board_size_before_moving() -> None:
    """盘路归最先入座的真人：另一位真人、观众、已落子的对局都不行。"""
    manager = RoomManager(max_private_rooms=5, weiqi_board_size=13, weiqi_komi=7.5)
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    spectator = await manager.join(room)

    # 首位入座、还没落子：可以换成 19 路
    assert room.public_snapshot(first.token)["weiqi"]["adjustable"] is True
    assert room.public_snapshot(spectator.token)["weiqi"]["adjustable"] is False
    await manager.set_weiqi_board(room, first.token, 19)
    assert room.weiqi_size == 19
    assert room.public_snapshot(first.token)["weiqi"]["size"] == 19

    # 观众不能改
    with pytest.raises(PermissionError):
        await manager.set_weiqi_board(room, spectator.token, 9)

    # 第二位真人入座开局：新棋盘按 19 路铺开
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    assert room.status == "active"
    assert room.game.size == 19
    assert len(room.game.board) == 19

    # 第二位真人不是「最先落座」的那位，也改不了
    with pytest.raises(PermissionError):
        await manager.set_weiqi_board(room, second.token, 9)

    # 已经落过子就不能再改盘路（否则棋谱失去意义）
    await manager.player_move(room, first.token, row=3, column=3)
    with pytest.raises(ValueError):
        await manager.set_weiqi_board(room, first.token, 9)
    assert room.game.size == 19


@pytest.mark.asyncio
async def test_set_weiqi_board_rejects_other_games_and_bad_sizes() -> None:
    manager = RoomManager(max_private_rooms=5)
    room = await make_room(manager, game_type="gomoku")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")

    with pytest.raises(ValueError):
        await manager.set_weiqi_board(room, visitor.token, 13)

    weiqi_room = await make_room(manager)
    player = await manager.join(weiqi_room)
    await manager.claim_and_start(weiqi_room, player.token, "human_black")
    with pytest.raises(ValueError):
        await manager.set_weiqi_board(weiqi_room, player.token, 15)


@pytest.mark.asyncio
async def test_capture_points_are_reported_for_the_replay_animation() -> None:
    """提子坐标随快照下发，前端才能在被提位置画残影。"""
    manager = RoomManager(max_private_rooms=5, weiqi_board_size=9, weiqi_komi=7.5)
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    game = room.game
    assert isinstance(game, WeiqiGame)

    # 白子 (1,1) 三面被黑围住，黑再下 (1,2) 即可提子
    for row, column in ((0, 1), (2, 1), (1, 0)):
        game.board[row][column] = BLACK
    game.board[1][1] = WHITE
    game.last_captured_points = []

    await manager.player_move(room, first.token, row=1, column=2)

    assert game.last_captured == 1
    assert game.last_captured_points == [(1, 1)]
    snapshot = room.public_snapshot(first.token)["game"]
    assert snapshot["last_captured_points"] == [[1, 1]]
    assert snapshot["captures"]["black"] == 1
    # 停一手后不应该残留上一手的提子坐标
    game.turn = WHITE
    await manager.player_pass(room, second.token)
    assert room.game.last_captured_points == []


@pytest.mark.asyncio
async def test_weiqi_resign_attributes_win_to_opponent() -> None:
    """围棋认输：认输的一方判负，胜利判给对面。"""
    manager, room, first, second = await two_human_room()

    await manager.player_move(room, first.token, row=4, column=4)
    # 黑方（first）认输 → 白方获胜
    await manager.resign(room, visitor_token=first.token)

    assert room.status == "finished"
    assert room.game.winner == WHITE
    assert room.game.finished is True

