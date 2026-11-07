from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    TurtleSoupGame,
    puzzle_from_mapping,
)


def _puzzle():
    return puzzle_from_mapping(
        {
            "title": "测试题",
            "surface": "一个人每天都在同一时间打开一扇没有上锁的门，但从不走进去。为什么？",
            "solution": "那是一间温室的通风门。他按时打开门是为了给植物换气，入口在另一侧，所以不从这里进入。",
            "key_facts": ["门用于温室通风", "他在照料植物", "真正入口位于另一侧"],
            "hints": ["门的用途不只是通行。", "这件事与室内环境有关。", "房间里主要是植物。"],
            "theme": "植物照料",
            "trick": "把通风门误认为普通入口",
        },
        content_level="all_ages",
    )


def _ready_game(max_hints: int) -> TurtleSoupGame:
    game = TurtleSoupGame(difficulty="normal", max_hints=max_hints, content_level="all_ages")
    game.set_puzzle(_puzzle())
    return game


def test_hints_are_global_and_never_repeat_between_players() -> None:
    """提示是「本局总量」，且两个玩家不会拿到同一句。"""
    game = _ready_game(max_hints=2)
    assert game.reveal_hint(source="web", player_uid="uid-A") == "门的用途不只是通行。"
    # 玩家 B 不会从第 1 条重新开始，而是拿还没发过的第 2 条
    assert game.reveal_hint(source="web", player_uid="uid-B") == "这件事与室内环境有关。"

    with pytest.raises(ValueError, match="用完"):
        game.reveal_hint(source="web", player_uid="uid-A")

    assert game.hints_used == 2
    assert game.hint_usage == {"uid-A": 1, "uid-B": 1}


def test_snapshot_reports_self_usage_and_remaining_total() -> None:
    game = _ready_game(max_hints=3)
    game.reveal_hint(source="web", player_uid="uid-A")
    game.reveal_hint(source="web", player_uid="uid-A")

    snap_a = game.snapshot(viewer_uid="uid-A")
    snap_b = game.snapshot(viewer_uid="uid-B")

    assert snap_a["hint_used_self"] == 2
    assert snap_a["hint_limit"] == 3
    assert snap_a["hints_used"] == 2
    assert snap_a["hint_left"] == 1
    assert snap_b["hint_used_self"] == 0
    # hints_used 是全局累计，供战绩统计与「本局还剩几次」使用
    assert game.hints_used == 2


def test_hint_usage_falls_back_to_player_number_without_uid() -> None:
    game = _ready_game(max_hints=1)
    game.reveal_hint(source="web", player_number=1)
    with pytest.raises(ValueError, match="用完"):
        game.reveal_hint(source="web", player_number=1)
    # 没有 uid 时仍能记录到座位号上
    assert game.hint_usage == {"1": 1}


def test_hint_library_exhausted_stops_even_without_max_hints() -> None:
    game = _ready_game(max_hints=0)
    for _index in range(3):
        game.reveal_hint(source="web", player_uid=f"uid-{_index}")
    with pytest.raises(ValueError, match="提示"):
        game.reveal_hint(source="web", player_uid="uid-x")
