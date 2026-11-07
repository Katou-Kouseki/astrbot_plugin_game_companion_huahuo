"""0.5.73 回归：入座不自动开局、提示去重、出题/判断模型、解说身份。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import (
    GameCompanionPlugin,
    _commentary_duel_room,
)
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import SoupPuzzle, TurtleSoupGame


async def make_soup_room(manager: RoomManager):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:44001",
        platform="aiocqhttp",
        group_id="44001",
        creator_qq="30001",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    visitor.connected = True
    visitor.last_seen_at = time.time()
    return room, visitor


# ---------------- 1. 多人海龟汤第一个入座不要自动开局 ----------------


@pytest.mark.asyncio
async def test_first_seat_does_not_start_multiplayer_soup() -> None:
    """4 人房：第一个人入座只进 setup，后面的人才来得及坐进来。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitor = await make_soup_room(manager)

    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "setup"
    assert room.game is None
    assert len(room.multiplayer.seats) == 1


@pytest.mark.asyncio
async def test_single_seat_soup_still_starts_immediately() -> None:
    """单人房没有「等别人」的意义，保持入座即开局。"""
    manager = RoomManager(turtle_soup_max_players=1)
    room, visitor = await make_soup_room(manager)

    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "active"
    assert isinstance(room.game, TurtleSoupGame)


# ---------------- 2. 提示：不同玩家不重复，总量受限 ----------------


def make_soup_game(*, max_hints: int = 2, hints: tuple[str, ...] = ("弱提示", "中提示", "强提示")):
    puzzle = SoupPuzzle(
        title="测试汤",
        surface="一个人每天做两件奇怪的事，为什么？",
        solution="真正的原因藏在最后。",
        key_facts=(),
        acceptable_variants=(),
        hints=hints,
        content_level="normal",
        theme="测试",
        trick="测试",
    )
    game = TurtleSoupGame(difficulty="normal", max_hints=max_hints, content_level="normal")
    game.set_puzzle(puzzle)
    return game


def test_hints_are_unique_across_players_and_total_is_limited() -> None:
    game = make_soup_game(max_hints=2)

    first = game.reveal_hint(source="web", player_number=1, player_uid="uid-a")
    second = game.reveal_hint(source="web", player_number=2, player_uid="uid-b")

    assert first == "弱提示"
    assert second == "中提示", "第二个玩家不能再拿到同一句提示"
    assert game.hint_usage == {"uid-a": 1, "uid-b": 1}

    # 总量 2 次已经用完，第三个人（哪怕是第一个玩家再来）都要被拒
    with pytest.raises(ValueError):
        game.reveal_hint(source="web", player_number=1, player_uid="uid-a")
    assert game.hints_used == 2


def test_hint_snapshot_reports_remaining_total() -> None:
    game = make_soup_game(max_hints=3)
    game.reveal_hint(source="web", player_number=1, player_uid="uid-a")

    snap = game.snapshot("uid-a")

    assert snap["hint_limit"] == 3
    assert snap["hints_used"] == 1
    assert snap["hint_left"] == 2
    assert snap["hint_used_self"] == 1


def test_unlimited_hints_stop_when_library_runs_out() -> None:
    """max_hints=0 表示不限制次数，但提示库发完就不能再编。"""
    game = make_soup_game(max_hints=0, hints=("A", "B"))
    assert game.reveal_hint(source="web", player_uid="a") == "A"
    assert game.reveal_hint(source="web", player_uid="b") == "B"
    with pytest.raises(ValueError):
        game.reveal_hint(source="web", player_uid="c")


# ---------------- 3. 出题 / 判断各用各的模型 ----------------


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeContext:
    def __init__(self, by_id: dict[str, object], session: object, default: object) -> None:
        self.by_id = by_id
        self.session = session
        self.default = default

    def get_provider_by_id(self, provider_id: str):
        return self.by_id.get(provider_id)

    def get_using_provider(self, session_id: str | None = None):
        return self.session if session_id else self.default


def test_soup_generation_and_judge_use_their_own_providers() -> None:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    judge, generation, session, default = (
        FakeProvider("judge"),
        FakeProvider("generation"),
        FakeProvider("session"),
        FakeProvider("default"),
    )
    plugin.context = FakeContext(
        {"judge-id": judge, "gen-id": generation}, session, default
    )
    plugin.turtle_soup_judge_provider_id = "judge-id"
    plugin.turtle_soup_generation_provider_id = "gen-id"
    room = SimpleNamespace(session_id="session-1")

    assert plugin._soup_judge_provider(room) is judge
    assert plugin._soup_generation_provider(room) is generation
    assert plugin._commentary_provider(room) is session


def test_missing_provider_falls_back_to_session_then_default() -> None:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    session, default = FakeProvider("session"), FakeProvider("default")
    plugin.context = FakeContext({}, session, default)
    plugin.turtle_soup_judge_provider_id = "不存在的-provider"
    plugin.turtle_soup_generation_provider_id = ""
    room = SimpleNamespace(session_id="session-1")

    assert plugin._soup_judge_provider(room) is session
    assert plugin._soup_generation_provider(room) is session


# ---------------- 4. 解说身份：海龟汤不能被当成棋类双人对局 ----------------


def _seat(number: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        number=number, display_name=name, side="", is_ai=False, visitor_token=f"t{number}"
    )


def test_commentary_duel_only_applies_to_board_games() -> None:
    soup_room = SimpleNamespace(
        game_type="turtle_soup",
        multiplayer=SimpleNamespace(enabled=True, seats=[_seat(1, "甲"), _seat(2, "乙")]),
    )
    board_room = SimpleNamespace(
        game_type="gomoku",
        multiplayer=SimpleNamespace(enabled=True, seats=[_seat(1, "甲"), _seat(2, "乙")]),
    )

    assert _commentary_duel_room(soup_room) is False, "海龟汤不是双人对弈，别说「执黑的一方」"
    assert _commentary_duel_room(board_room) is True


# ---------------- 5. 申请出题期间不允许再来一局抢跑 ----------------


@pytest.mark.asyncio
async def test_rematch_refused_while_someone_is_applying_to_host() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitor = await make_soup_room(manager)
    await manager.claim_and_start(room, visitor.token, "")
    await manager.start_game(room, visitor.token, "")
    room.game.phase = "finished"
    room.game.end_reason = "timeout"
    room.status = "finished"
    room.soup_host_number = 1

    with pytest.raises(ValueError) as excinfo:
        await manager.restart_finished_game(room, difficulty="normal")

    assert "申请出题" in str(excinfo.value)
