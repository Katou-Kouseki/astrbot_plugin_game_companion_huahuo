from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def test_ai_opponent_credit_records_win_and_loss() -> None:
    manager = RoomManager()

    # 花火赢一局黑杰克：胜场 + 局数各 +1
    manager._credit_ai_opponent_game(SimpleNamespace(game_type="blackjack"), ai_won=True)
    row = manager.global_leaderboard("blackjack")[0]
    assert (row["name"], row["wins"], row["games"], row["losses"]) == ("花火", 1, 1, 0)
    assert row["winrate"] == 100.0
    # 花火不是真人，管理台据此禁掉改名 / 合并等操作
    assert row["is_ai"] is True

    # 花火输一局贪心骰子：只记局数，不记胜场
    manager._credit_ai_opponent_game(SimpleNamespace(game_type="pig_dice"), ai_won=False)
    row = manager.global_leaderboard("pig_dice")[0]
    assert (row["wins"], row["games"], row["losses"]) == (0, 1, 1)
    assert row["winrate"] == 0.0

    # 棋类榜不受影响
    assert manager.global_leaderboard("gomoku") == []


def test_ai_opponent_credit_ignores_non_opponent_games() -> None:
    manager = RoomManager()
    manager._credit_ai_opponent_game(SimpleNamespace(game_type="turtle_soup"), ai_won=True)
    manager._credit_ai_opponent_game(SimpleNamespace(game_type="undercover"), ai_won=True)
    assert manager.global_leaderboard("turtle_soup") == []
    assert manager.global_leaderboard("undercover") == []
