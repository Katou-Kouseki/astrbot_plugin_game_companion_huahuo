"""海龟汤难度档位：次数与「猜中条件」随难度变化，题库优先级可配。"""

from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    SOUP_DIFFICULTY_PROFILES,
    soup_limits_for_difficulty,
    soup_solve_policy,
)


def test_profiles_match_the_documented_table() -> None:
    """数值就是产品表：改了就要同步改配置项的提示文案。"""
    assert soup_limits_for_difficulty("entertainment") == {
        "questions": 0, "hints": 15, "answers": 0,
    }
    assert soup_limits_for_difficulty("easy") == {
        "questions": 90, "hints": 10, "answers": 8,
    }
    assert soup_limits_for_difficulty("normal") == {
        "questions": 35, "hints": 5, "answers": 4,
    }
    assert soup_limits_for_difficulty("hard") == {
        "questions": 15, "hints": 1, "answers": 2,
    }


def test_unknown_difficulty_falls_back_to_normal() -> None:
    assert soup_limits_for_difficulty("???") == soup_limits_for_difficulty("normal")
    assert soup_solve_policy("") == "core"
    assert set(SOUP_DIFFICULTY_PROFILES) == {
        "entertainment", "easy", "normal", "hard",
    }


def test_solve_policy_per_difficulty() -> None:
    assert soup_solve_policy("entertainment") == "partial"
    assert soup_solve_policy("easy") == "core"
    assert soup_solve_policy("normal") == "core"
    assert soup_solve_policy("hard") == "exact"


# ---------------- 房间按难度取次数 ----------------


def make_room(difficulty: str):
    from types import SimpleNamespace

    return SimpleNamespace(difficulty=difficulty)


def test_manager_uses_difficulty_when_enabled() -> None:
    manager = RoomManager(turtle_soup_follow_difficulty=True)
    assert manager.soup_limits(make_room("entertainment"))["questions"] == 0
    assert manager.soup_limits(make_room("hard"))["answers"] == 2


def test_manager_uses_custom_numbers_when_disabled() -> None:
    manager = RoomManager(
        turtle_soup_follow_difficulty=False,
        turtle_soup_max_questions=7,
        turtle_soup_max_hints=2,
        turtle_soup_max_answers=3,
    )
    assert manager.soup_limits(make_room("hard")) == {
        "questions": 7, "hints": 2, "answers": 3,
    }


# ---------------- 猜中条件：按难度放宽或收紧 ----------------


def judgment(solved: bool, coverage: float):
    from types import SimpleNamespace

    return SimpleNamespace(solved=solved, coverage=coverage)


def test_entertainment_accepts_partial_answers() -> None:
    """娱乐档：部分正确也算过，避免新手卡死。"""
    plugin = object.__new__(GameCompanionPlugin)
    assert plugin._soup_answer_solved(make_room("entertainment"), judgment(False, 0.5))
    assert plugin._soup_answer_solved(make_room("entertainment"), judgment(False, 0.2)) is False


def test_normal_requires_core_understanding() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    assert plugin._soup_answer_solved(make_room("normal"), judgment(False, 0.75))
    assert plugin._soup_answer_solved(make_room("normal"), judgment(False, 0.5)) is False


def test_hard_requires_full_recall() -> None:
    """困难档：只有模型判「完全还原」且覆盖度足够高才算解开。"""
    plugin = object.__new__(GameCompanionPlugin)
    assert plugin._soup_answer_solved(make_room("hard"), judgment(True, 0.95))
    assert plugin._soup_answer_solved(make_room("hard"), judgment(True, 0.7)) is False
    assert plugin._soup_answer_solved(make_room("hard"), judgment(False, 0.95)) is False


# ---------------- 难度解析 ----------------


def test_soup_difficulty_accepts_chinese_aliases() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    assert plugin._soup_difficulty("娱乐") == "entertainment"
    assert plugin._soup_difficulty("休闲") == "entertainment"
    assert plugin._soup_difficulty("困难") == "hard"
    assert plugin._soup_difficulty("乱写") == "normal"


def test_empty_request_uses_configured_default() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_difficulty = "hard"
    assert plugin._soup_difficulty("") == "hard"


def test_entertainment_is_rejected_for_board_games() -> None:
    """棋类只有三档，别让「娱乐」漏进去导致引擎 KeyError。"""
    plugin = object.__new__(GameCompanionPlugin)
    assert plugin._difficulty_for_game("turtle_soup", "娱乐") == "entertainment"
    assert plugin._difficulty_for_game("gomoku", "娱乐") == "normal"


# ---------------- 题库优先级 ----------------


@pytest.mark.asyncio
async def test_library_priority_decides_which_source_is_tried_first() -> None:
    from types import SimpleNamespace

    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_online_pool_url = "https://example.com/pool.json"
    plugin.turtle_soup_library_enabled = True
    plugin._soup_custom = lambda: SimpleNamespace(puzzles=lambda level: [])
    plugin._soup_deck = lambda: SimpleNamespace(
        recent_signatures=lambda count: [],
        known_surfaces=lambda: set(),
        has=lambda puzzle: False,
    )

    calls: list[str] = []

    async def fake_builtin(room, game, deck, excluded):
        calls.append("builtin")
        return True

    async def fake_online(room, game, deck, excluded):
        calls.append("online")
        return False

    plugin._builtin_soup_puzzle = fake_builtin
    plugin._online_soup_puzzle = fake_online

    room = SimpleNamespace(turtle_soup_recent_signatures=[], difficulty="normal")
    game = SimpleNamespace(mode="bot_host", content_level="normal")

    plugin.turtle_soup_library_priority = "builtin_first"
    await plugin._prepare_turtle_soup(room, game)
    assert calls == ["builtin"]

    calls.clear()
    plugin.turtle_soup_library_priority = "online_first"
    await plugin._prepare_turtle_soup(room, game)
    assert calls == ["online", "builtin"]
