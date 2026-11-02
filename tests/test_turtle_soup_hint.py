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


def test_hints_are_counted_per_player_uid() -> None:
    game = _ready_game(max_hints=2)
    # 玩家 A 用满 2 次
    assert game.reveal_hint(source="web", player_uid="uid-A") == "门的用途不只是通行。"
    assert game.reveal_hint(source="web", player_uid="uid-A") == "这件事与室内环境有关。"
    with pytest.raises(ValueError, match="用完提示次数"):
        game.reveal_hint(source="web", player_uid="uid-A")

    # 玩家 B 独立计数，从第 1 条开始
    assert game.reveal_hint(source="web", player_uid="uid-B") == "门的用途不只是通行。"


def test_snapshot_reports_self_hint_usage() -> None:
    game = _ready_game(max_hints=3)
    game.reveal_hint(source="web", player_uid="uid-A")
    game.reveal_hint(source="web", player_uid="uid-A")

    snap_a = game.snapshot(viewer_uid="uid-A")
    snap_b = game.snapshot(viewer_uid="uid-B")

    assert snap_a["hint_used_self"] == 2
    assert snap_a["hint_limit"] == 3
    assert snap_b["hint_used_self"] == 0
    # hints_used 仍保留全局累计语义，供战绩统计使用
    assert game.hints_used == 2


def test_hint_usage_falls_back_to_player_number_without_uid() -> None:
    game = _ready_game(max_hints=1)
    game.reveal_hint(source="web", player_number=1)
    with pytest.raises(ValueError, match="用完提示次数"):
        game.reveal_hint(source="web", player_number=1)
