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


# ---------------- 房间内实时改难度 ----------------


async def make_active_room(manager: RoomManager, difficulty: str = "normal"):
    """造一个正在进行中的海龟汤房间（含题目），供改难度用。"""
    import time

    from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping

    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:46001",
        platform="aiocqhttp",
        group_id="46001",
        creator_qq="31001",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty=difficulty,
    )
    visitor = await manager.join(room)
    visitor.connected = True
    visitor.last_seen_at = time.time()
    await manager.claim_and_start(room, visitor.token, "")
    await manager.start_game(room, visitor.token, "")
    await manager.complete_turtle_soup_generation(
        room,
        room.game,
        puzzle_from_mapping(
            {
                "title": "测试题",
                "surface": "一个人每天都在同一时间打开一扇没有上锁的门，但从不走进去。为什么？",
                "solution": "那是一间温室的通风门，他按时打开是为了给植物换气，入口在另一侧。",
                "key_facts": ["门用于温室通风", "他在照料植物", "入口在另一侧"],
                "hints": ["和通行无关", "和室内环境有关", "里面是植物"],
                "theme": "植物",
                "trick": "把通风门当入口",
            },
            content_level="normal",
        ),
    )
    return room, visitor


async def make_setup_room(manager: RoomManager, difficulty: str = "normal"):
    """造一个入座但还没开局的房间：难度只允许在这个阶段调。"""
    import time

    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:46002",
        platform="aiocqhttp",
        group_id="46002",
        creator_qq="31002",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty=difficulty,
    )
    visitor = await manager.join(room)
    visitor.connected = True
    visitor.last_seen_at = time.time()
    await manager.claim_and_start(room, visitor.token, "")
    return room, visitor


@pytest.mark.asyncio
async def test_player_can_change_difficulty_before_start() -> None:
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_setup_room(manager)
    assert room.status == "setup"

    await manager.player_set_soup_difficulty(room, visitor.token, "hard")

    assert room.difficulty == "hard"
    assert manager.soup_limits(room)["questions"] == 15
    assert room.difficulty_chosen_by_player is True


@pytest.mark.asyncio
async def test_difficulty_cannot_change_mid_game() -> None:
    """对局中改难度等于中途换规则：先问的人会被临时加严或放宽，必须拦住。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_room(manager)
    assert room.status == "active"

    with pytest.raises(ValueError, match="本局已经开始"):
        await manager.player_set_soup_difficulty(room, visitor.token, "hard")

    assert room.difficulty == "normal"
    assert room.game.max_hints == 5  # 普通档，没有被改掉


@pytest.mark.asyncio
async def test_spectator_cannot_change_difficulty() -> None:
    manager = RoomManager(turtle_soup_max_players=0)
    room, _visitor = await make_setup_room(manager)
    guest = await manager.join(room)

    with pytest.raises(PermissionError):
        await manager.player_set_soup_difficulty(room, guest.token, "entertainment")


@pytest.mark.asyncio
async def test_switching_to_entertainment_lifts_the_limits() -> None:
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_setup_room(manager, difficulty="hard")

    await manager.player_set_soup_difficulty(room, visitor.token, "entertainment")

    limits = manager.soup_limits(room)
    assert limits["questions"] == 0  # 不限
    assert limits["answers"] == 0
    assert limits["hints"] == 15
