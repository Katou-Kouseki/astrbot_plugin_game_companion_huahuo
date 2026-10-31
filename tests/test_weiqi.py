from __future__ import annotations

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, EMPTY, WHITE
from astrbot_plugin_game_companion_huahuo.weiqi import WeiqiGame


def make_game(size: int = 9, **kwargs) -> WeiqiGame:
    return WeiqiGame(human_color=BLACK, size=size, **kwargs)


def test_initial_state_and_winrate_is_sane() -> None:
    game = make_game(13)
    snapshot = game.snapshot()
    assert snapshot["size"] == 13
    assert snapshot["turn"] == BLACK
    assert len(snapshot["board"]) == 13
    assert all(len(row) == 13 for row in snapshot["board"])
    evaluation = game.evaluate()
    assert 0 <= evaluation["black"] <= 100
    assert round(evaluation["black"] + evaluation["white"], 1) == 100
    # 开局贴目被先手部分对冲，不应该一边倒
    assert 35 <= evaluation["black"] <= 65


def test_capture_removes_surrounded_stone() -> None:
    game = make_game()
    game.board[0][1] = WHITE
    game.board[0][0] = BLACK
    game.board[1][0] = BLACK
    game.board[1][1] = BLACK
    game.turn = BLACK
    captured = game.place(0, 2, BLACK)
    assert captured == 1
    assert game.board[0][1] == EMPTY
    assert game.captures[BLACK] == 1
    assert game.last_captured == 1
    assert game.turn == WHITE


def test_ko_forbids_immediate_recapture() -> None:
    game = make_game()
    game.board[0][1] = BLACK
    game.board[0][2] = WHITE
    game.board[1][0] = BLACK
    game.board[1][1] = WHITE
    game.board[1][3] = WHITE
    game.board[2][1] = BLACK
    game.board[2][2] = WHITE
    game.turn = BLACK
    assert game.place(1, 2, BLACK) == 1
    assert game.ko_point == (1, 1)
    assert game.is_legal(1, 1, WHITE) is False
    assert game.is_legal(4, 4, WHITE) is True


def test_suicide_is_rejected() -> None:
    game = make_game()
    game.board[0][0] = BLACK
    game.board[0][2] = BLACK
    game.board[1][1] = BLACK
    game.turn = WHITE
    assert game.is_legal(0, 1, WHITE) is False


def test_two_passes_settle_by_area_score() -> None:
    game = make_game()
    assert game.pass_turn(BLACK) is False
    assert game.pass_turn(WHITE) is True
    assert game.finished is True
    assert game.winner == WHITE  # 空盘 + 黑贴 7.5，白方领先
    assert game.result["margin"] == -7.5


def test_territories_follow_surrounding_color() -> None:
    game = make_game()
    for row in range(9):
        game.board[row][4] = BLACK
        game.board[row][6] = WHITE
    black_territory, white_territory, neutral = game.territories()
    assert black_territory == 36
    assert white_territory == 18
    assert neutral == 9


def test_bot_move_is_legal_and_only_on_its_turn() -> None:
    game = make_game(13, difficulty="normal")
    assert game.choose_bot_move() is None  # 开局轮到真人执黑
    game.turn = WHITE
    move = game.choose_bot_move(seed=3)
    if move is not None:
        assert game.is_legal(move[0], move[1], WHITE)
        game.place(move[0], move[1], WHITE)
        assert game.turn == BLACK


def test_snapshot_exposes_winrate_and_legal_moves() -> None:
    game = make_game(9)
    game.place(4, 4, BLACK)
    snapshot = game.snapshot()
    assert snapshot["kind"] == "weiqi"
    assert snapshot["move_count"] == 1
    assert snapshot["last_move"] == [4, 4]
    assert snapshot["captures"] == {"black": 0, "white": 0}
    assert isinstance(snapshot["eval"], dict)
    assert snapshot["eval"]["black"] > 0
    assert all(game.is_legal(row, column, WHITE) for row, column in snapshot["legal_moves"])
    assert len(snapshot["legal_moves"]) == 80


def test_undo_round_restores_previous_position() -> None:
    game = make_game(9)
    game.place(4, 4, BLACK)
    game.place(4, 5, WHITE)
    removed = game.undo_round()
    assert removed == 2
    assert game.history == []
    assert all(cell == EMPTY for row in game.board for cell in row)
    assert game.turn == BLACK
