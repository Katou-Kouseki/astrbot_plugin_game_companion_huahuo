from __future__ import annotations

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE, GomokuGame


def _game(*, human_color: int = BLACK, difficulty: str = "hard") -> GomokuGame:
    game = GomokuGame(human_color=human_color, difficulty=difficulty)
    game.turn = WHITE if human_color == BLACK else BLACK
    return game


def _put(game: GomokuGame, stones: list[tuple[int, int]], color: int) -> None:
    for row, column in stones:
        game.board[row][column] = color
        game.history.append((row, column, color))


def test_hard_mode_extends_open_three_to_four() -> None:
    """困难档：自己有活三时必须做成四（双杀点），而不是随便应一手。"""
    game = _game()  # 花火执白
    _put(game, [(7, 7), (7, 8), (7, 9)], WHITE)
    _put(game, [(0, 0), (1, 1), (2, 2)], BLACK)

    move = game.choose_bot_move()

    assert move in {(7, 6), (7, 10)}, move


def test_hard_mode_blocks_opponent_open_three() -> None:
    """困难档：对手活三必须堵，不能只顾自己做棋。"""
    game = _game()  # 花火执白
    _put(game, [(5, 5)], WHITE)
    _put(game, [(7, 7), (7, 8), (7, 9)], BLACK)

    move = game.choose_bot_move()

    assert move in {(7, 6), (7, 10)}, move


def test_hard_mode_prefers_double_threat() -> None:
    """困难档：能一手做出两条四（双杀）时直接走这手。"""
    game = _game()
    # 白在横竖两个方向各有一个「三」，交叉点 (7, 7) 落子后形成双四
    _put(game, [(7, 5), (7, 6), (7, 8)], WHITE)
    _put(game, [(5, 7), (6, 7), (8, 7)], WHITE)
    _put(game, [(0, 0), (1, 1), (2, 2), (10, 10)], BLACK)

    move = game.choose_bot_move()

    assert move == (7, 7), move


def test_normal_mode_stays_greedy_and_legal() -> None:
    """普通档保持贪心：只要求落在合法点且能堵住活三。"""
    game = _game(difficulty="normal")
    _put(game, [(5, 5)], WHITE)
    _put(game, [(7, 7), (7, 8), (7, 9)], BLACK)

    move = game.choose_bot_move()

    assert move in {(7, 6), (7, 10)}, move
