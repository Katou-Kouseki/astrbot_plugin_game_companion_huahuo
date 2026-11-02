from __future__ import annotations

import pytest
from astrbot_plugin_game_companion_huahuo.othello import (
    BLACK,
    EMPTY,
    WHITE,
    OthelloGame,
)
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def test_initial_board_and_legal_moves_follow_standard_rules() -> None:
    game = OthelloGame()

    assert game.black_count == 2
    assert game.white_count == 2
    assert game.turn == BLACK
    assert game.legal_moves() == [(2, 3), (3, 2), (4, 5), (5, 4)]
    assert game.legal_moves(WHITE) == [(2, 4), (3, 5), (4, 2), (5, 3)]
    assert game.snapshot()["legal_moves"] == [[2, 3], [3, 2], [4, 5], [5, 4]]


def test_move_flips_every_bracketed_opponent_disk_and_rejects_illegal_moves() -> None:
    game = OthelloGame()

    with pytest.raises(ValueError, match="不能夹住"):
        game.place(0, 0, BLACK)
    with pytest.raises(ValueError, match="轮到"):
        game.place(2, 3, WHITE)

    game.place(2, 3, BLACK)

    assert game.board[2][3] == BLACK
    assert game.board[3][3] == BLACK
    assert (game.black_count, game.white_count) == (4, 1)
    assert game.turn == WHITE


def test_no_legal_move_is_skipped_and_game_ends_after_both_sides_pass() -> None:
    board = [
        [0, 2, 2, 1, 1, 1, 1, 1],
        [2, 2, 2, 1, 1, 2, 2, 0],
        [1, 1, 1, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 1, 2, 2, 2],
        [1, 1, 2, 2, 2, 1, 2, 2],
        [1, 1, 1, 2, 2, 2, 2, 1],
        [1, 1, 1, 0, 2, 2, 2, 1],
    ]
    game = OthelloGame(board=board, turn=BLACK, human_color=WHITE)

    assert not game.legal_moves(WHITE)
    assert game.legal_moves(BLACK)
    game.place(0, 0, BLACK)

    assert game.last_passed == WHITE
    assert game.turn == BLACK
    assert game.legal_moves(BLACK)

    board = [[BLACK for _ in range(8)] for _ in range(8)]
    board[7][6] = WHITE
    board[7][7] = EMPTY
    final = OthelloGame(board=board, turn=BLACK)
    final.place(7, 7, BLACK)

    assert final.finished
    assert final.winner == BLACK
    assert not final.draw
    assert final.black_count == 64
    assert final.white_count == 0


def test_undo_round_restores_flipped_disks_and_preserves_prior_rounds() -> None:
    game = OthelloGame(human_color=BLACK)
    game.place(2, 3, BLACK)
    bot_move = game.legal_moves(WHITE)[0]
    game.place(*bot_move, WHITE)
    board_before_human_move = [row[:] for row in game.board]
    human_move = game.legal_moves(BLACK)[0]
    game.place(*human_move, BLACK)

    game.undo_round()

    assert len(game.history) == 2
    assert game.turn == BLACK
    assert game.board == board_before_human_move
    assert game.winner == EMPTY
    assert not game.finished


@pytest.mark.parametrize("difficulty", ["easy", "normal", "hard"])
def test_bot_selects_a_legal_move_at_each_difficulty(difficulty: str) -> None:
    game = OthelloGame(human_color=WHITE, difficulty=difficulty)  # type: ignore[arg-type]

    move = game.choose_bot_move(seed=7, time_limit=0.12)

    assert move in game.legal_moves(BLACK)


@pytest.mark.asyncio
async def test_room_manager_starts_bot_when_human_chooses_white() -> None:
    manager = RoomManager()
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="othello",
        difficulty="easy",
    )
    visitor = await manager.join(room)

    # 两人对弈改为「先入座、再决定对手」：只有一个人时停在 setup，
    # 可以等朋友，也可以直接让花火补位开局；首局仍按选的先后手决定执色。
    await manager.claim_and_start(room, visitor.token, "bot_black")
    assert room.status == "setup"
    await manager.start_game(room, visitor.token, "bot_black")
    # 花火的回合改在后台跑：开局接口先返回空棋盘，AI 想好了才落子
    await manager.wait_bot_turns()

    assert isinstance(room.game, OthelloGame)
    assert room.game.human_color == WHITE
    assert len(room.game.history) == 1
    assert room.game.turn == WHITE
    assert room.status == "active"


def test_evaluate_reports_winrate_that_tracks_disk_lead_and_empty_cells() -> None:
    game = OthelloGame()

    opening = game.evaluate()
    # 开局 2:2 且黑先，先手只该带来极小偏移，不能出现一边倒
    assert 45.0 <= float(opening["black"]) <= 60.0
    assert opening["empty"] == 60
    assert opening["scored"] is False
    assert game.snapshot()["eval"] == opening

    # 黑子越多胜率越高；同一子数差下空格越少越确定
    game.board = [[BLACK] * 8 for _ in range(8)]
    for row in range(4):
        game.board[row][7] = WHITE
    leading = game.evaluate()
    assert float(leading["black"]) > 60.0
    assert float(leading["lead"]) > 0
    assert leading["empty"] == 0

    # 终局：胜负已定，胜方必须明确站在 50% 以上
    game.winner = BLACK
    assert float(game.evaluate()["black"]) > 50.0
    game.board = [[WHITE] * 8 for _ in range(8)]
    for row in range(4):
        game.board[row][7] = BLACK
    game.winner = WHITE
    assert float(game.evaluate()["black"]) < 50.0
    game.board = [[EMPTY] * 8 for _ in range(8)]
    game.winner = EMPTY
    game.draw = True
    assert float(game.evaluate()["black"]) == pytest.approx(50.0, abs=0.5)
