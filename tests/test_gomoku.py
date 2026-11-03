from __future__ import annotations

import pytest
from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, EMPTY, WHITE, GomokuGame
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.models import GameRoom


def make_room(game: GomokuGame) -> GameRoom:
    room = GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    room.game = game
    return room


def test_freestyle_five_or_more_wins() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4):
        game.board[7][column] = BLACK
        game.history.append((7, column, BLACK))
    game.turn = BLACK

    game.place(7, 4, BLACK)

    assert game.winner == BLACK
    assert game.finished


def test_move_rejects_wrong_turn_and_occupied_cell() -> None:
    game = GomokuGame(human_color=BLACK)
    with pytest.raises(ValueError, match="轮到"):
        game.place(7, 7, WHITE)

    game.place(7, 7, BLACK)
    with pytest.raises(ValueError, match="有棋子"):
        game.place(7, 7, WHITE)


@pytest.mark.parametrize("difficulty", ["normal", "hard"])
def test_ai_blocks_immediate_human_win(difficulty: str) -> None:
    game = GomokuGame(human_color=BLACK, difficulty=difficulty)
    for column in range(4, 8):
        game.board[8][column] = BLACK
        game.history.append((8, column, BLACK))
    game.turn = WHITE

    row, column = game.choose_bot_move(seed=2, time_limit=0.1)

    assert row == 8
    assert column in {3, 8}


def test_ai_takes_immediate_win_before_blocking() -> None:
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    for column in range(4):
        game.board[6][column] = WHITE
        game.history.append((6, column, WHITE))
        game.board[9][column] = BLACK
        game.history.append((9, column, BLACK))
    game.turn = WHITE

    assert game.choose_bot_move(seed=1) == (6, 4)


def test_undo_round_removes_player_and_following_bot_stone() -> None:
    game = GomokuGame(human_color=BLACK)
    game.place(7, 7, BLACK)
    game.place(7, 8, WHITE)
    game.place(6, 7, BLACK)
    game.place(6, 8, WHITE)

    removed = game.undo_round()

    assert removed == 2
    assert game.board[6][7] == EMPTY
    assert game.board[6][8] == EMPTY
    assert game.turn == BLACK
    assert len(game.history) == 2


def test_snapshot_does_not_expose_mutation_helpers() -> None:
    snapshot = GomokuGame().snapshot()

    assert snapshot["move_count"] == 0
    assert "history" not in snapshot


def test_last_move_threat_finds_two_immediate_winning_points() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 7, BLACK)

    assert threat.kind == "multiple"
    assert threat.winning_points == ((7, 3), (7, 8))


def test_last_move_threat_distinguishes_one_blockable_point() -> None:
    game = GomokuGame(human_color=BLACK)
    game.board[7][3] = WHITE
    for column in range(4, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 7, BLACK)

    assert threat.kind == "single"
    assert threat.winning_points == ((7, 8),)


def test_last_move_threat_finds_internal_winning_gap() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in (4, 5, 7, 8):
        game.board[7][column] = BLACK

    threat = game.move_threat(7, 8, BLACK)

    assert threat.kind == "single"
    assert threat.winning_points == ((7, 6),)


def test_last_move_does_not_report_an_unrelated_existing_threat() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK
    game.board[10][10] = BLACK

    threat = game.move_threat(10, 10, BLACK)

    assert threat.kind == ""
    assert threat.winning_points == ()


def test_gomoku_commentary_names_actor_color_coordinate_and_consequence() -> None:
    game = GomokuGame(human_color=BLACK)
    for column in range(4, 8):
        game.board[7][column] = BLACK
    game.turn = WHITE

    prompt = GameCompanionPlugin._gomoku_commentary_prompt(
        make_room(game),
        {"actor": "human", "row": 7, "column": 7, "color": BLACK},
    )

    assert "黑方（执黑）在第 8 行第 8 列落子" in prompt
    assert "对手是白方（执白）" in prompt
    assert "当前黑方 4 颗、白方 0 颗" in prompt
    assert "现在轮到白方走" in prompt
    assert "2 个下一手即可连成五子的空位" in prompt
    assert "白方下一手无法全部封住" in prompt
    assert all(term not in prompt for term in ("活四", "冲四", "死四"))


def test_gomoku_commentary_explains_single_bot_threat_without_jargon() -> None:
    game = GomokuGame(human_color=BLACK)
    game.board[6][3] = BLACK
    for column in range(4, 8):
        game.board[6][column] = WHITE
    game.turn = BLACK

    prompt = GameCompanionPlugin._gomoku_commentary_prompt(
        make_room(game),
        {"actor": "bot", "row": 6, "column": 7, "color": WHITE},
    )

    assert "白方（执白）在第 7 行第 8 列落子" in prompt
    assert "对手是黑方（执黑）" in prompt
    assert "当前黑方 1 颗、白方 4 颗" in prompt
    assert "1 个下一手即可连成五子的空位" in prompt
    assert "黑方下一手仍可封住" in prompt
    assert "专业棋型名称" in prompt


def test_run_score_ranks_live_three_above_rush_four() -> None:
    """活三威胁高于冲四，死四分值归零——避免 AI 往被堵的线里凑第 4 颗。"""
    assert GomokuGame._run_score(3, 2) > GomokuGame._run_score(4, 1)  # 活三 > 冲四
    assert GomokuGame._run_score(4, 0) == 0.0  # 死四：两端都被堵
    assert GomokuGame._run_score(4, 2) > GomokuGame._run_score(3, 2)  # 活四 > 活三


def test_ai_prefers_live_three_over_rush_four_extension() -> None:
    """玩家预堵了三连的一端后，AI 不应再往同一条线凑第 4 颗（冲四），
    而应优先去别处做活三。"""
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    # 白棋三连 (7,4)(7,5)(7,6)，黑棋预堵左端 (7,3)
    for column in (4, 5, 6):
        game.board[7][column] = WHITE
        game.history.append((7, column, WHITE))
    game.board[7][3] = BLACK
    game.history.append((7, 3, BLACK))
    # 别处白棋两连 (5,5)(5,6)，(5,4) / (5,7) 都能做成活三
    game.board[5][5] = WHITE
    game.history.append((5, 5, WHITE))
    game.board[5][6] = WHITE
    game.history.append((5, 6, WHITE))
    game.turn = WHITE

    row, column = game.choose_bot_move(seed=1)

    # 关键：不再往被预堵的那条线凑冲四 (7,7)。
    # 实际最优是 (6,5)——它同时连通 (5,5)(7,5) 与 (5,6)(7,4) 两条活三，
    # 属于双活三杀着；即便退一步也只该是 (5,4)/(5,7) 的活三，绝不该是冲四。
    assert (row, column) != (7, 7)
    assert (row, column) != (7, 2)
