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
async def test_single_seat_soup_also_waits_for_the_start_button() -> None:
    """单人房也停在准备状态：先给人机会选「我来出题」，也防私聊链接被拉进群里。

    0.5.81 曾经把单人房改回「入座即开局」，理由是「等下去只会让人以为按钮坏了」。
    实际反馈是反过来的：一入座就开局，「我来出题」永远点不到，从私聊发到群里的
    链接别人也来不及坐进来。前端本来就有「开始出题」按钮和人数提示。
    """
    manager = RoomManager(turtle_soup_max_players=1)
    room, visitor = await make_soup_room(manager)

    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "setup"
    assert room.game is None

    await manager.start_game(room, visitor.token, "")
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


# ---------------- 6. 申请出题不能清掉上一局的对局对象 ----------------


async def two_seat_finished_room(manager: RoomManager):
    """两位玩家一起打完一局海龟汤，房间停在 finished。"""
    room, first = await make_soup_room(manager)
    second = await manager.join(room)
    second.connected = True
    second.last_seen_at = time.time()
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    await manager.start_game(room, first.token, "")
    puzzle = SoupPuzzle(
        title="门",
        surface="一个人每天打开同一扇门，却从来不从这扇门进入房间。",
        solution="这是温室的通风门，真正的入口在另一侧。",
        key_facts=("门用于通风", "房间是温室"),
        acceptable_variants=(),
        hints=("门不只用于通行", "和环境有关"),
        content_level="all_ages",
        theme="温室",
        trick="通风门",
    )
    await manager.complete_turtle_soup_generation(room, room.game, puzzle)
    room.game.record_answer(
        "这是温室的通风门，入口在另一侧。",
        solved=True,
        source="web",
        matched_facts={"门用于通风", "房间是温室"},
        player_number=1,
        reply="猜中了。",
        coverage=1.0,
    )
    await manager._finish_game(room)
    return room, first, second


@pytest.mark.asyncio
async def test_applying_to_host_keeps_previous_game_intact() -> None:
    """0.5.73 的坑：申请出题把 room.game 清空，导致表单不出现、再来一局必失败。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, first, _second = await two_seat_finished_room(manager)

    await manager.apply_soup_host(room, first.token)

    assert room.soup_host_number == 1
    assert room.game is not None, "申请出题只决定下一局谁出题，不能清掉上一局"
    assert room.status == "finished"
    snapshot = room.public_snapshot(first.token)
    assert snapshot["soup_host_number"] == 1
    assert snapshot["player_number"] == 1


@pytest.mark.asyncio
async def test_apply_then_submit_starts_player_hosted_game() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, first, _second = await two_seat_finished_room(manager)

    await manager.apply_soup_host(room, first.token)
    game = await manager.start_soup_host_game(
        room,
        first.token,
        title="雨夜的伞",
        surface="她每天带两把伞出门，明明一个人住，这是为什么？",
        solution="丈夫三年前在雨夜出车祸去世，两把伞是替他拿的。",
        hints=["和一起去上班有关"],
    )

    assert room.status == "active"
    assert game.host_number == 1
    assert game.puzzle is not None and game.puzzle.solution.startswith("丈夫三年前")


@pytest.mark.asyncio
async def test_rematch_request_is_refused_after_applying_to_host() -> None:
    """两套流程不能并存：有人要自己出题时，别再让花火开一局。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, first, second = await two_seat_finished_room(manager)

    await manager.apply_soup_host(room, first.token)
    with pytest.raises(ValueError) as excinfo:
        await manager.request_rematch(room, second.token)

    assert "申请出题" in str(excinfo.value)
    assert room.status == "finished"


# ---------------- 7. 多人揭晓表决：不算回合、谁都能附议 ----------------


@pytest.mark.asyncio
async def test_reveal_vote_does_not_consume_turn_and_others_can_vote() -> None:
    """「我发起揭晓，别人没点」时：我仍是当前玩家、能继续提问；别人也能投票。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, first, second = await two_seat_finished_room(manager)
    await manager.apply_soup_host(room, first.token)
    game = await manager.start_soup_host_game(
        room,
        first.token,
        title="雨夜的伞",
        surface="她每天带两把伞出门，明明一个人住，这是为什么？",
        solution="丈夫三年前在雨夜出车祸去世，两把伞是替他拿的。",
    )
    # 让 2 号成为当前回合玩家
    index = next(
        i for i, seat in enumerate(room.multiplayer.seats) if seat.number == 2
    )
    room.multiplayer.current_turn_index = index

    payload = await manager.request_soup_reveal(
        room, source="web", visitor_token=second.token
    )

    assert payload["revealed"] is False
    assert payload["votes"] == 1 and payload["required"] == 2
    snapshot = room.public_snapshot(second.token)
    assert snapshot["current_player_number"] == 2
    assert snapshot["is_current_player"] is True, "发起表决不能吃掉回合"
    assert "表决期间照常提问" in room.messages[-1]["content"]

    # 表决期间继续提问（不被 reveal 状态卡住）
    game_obj, _text = await manager.begin_turtle_soup_interaction(
        room, "她是不是在等人一起上班？", source="web", visitor_token=second.token, limit=200
    )
    await manager.cancel_turtle_soup_interaction(room, game_obj, "测试结束")

    # 1 号不是当前回合玩家，但依然能附议
    voted = await manager.vote_soup_reveal(
        room, agree=True, source="web", visitor_token=first.token
    )
    assert voted["revealed"] is True
    assert game.finished is True


@pytest.mark.asyncio
async def test_rematch_refusal_keeps_room_alive() -> None:
    """花火拒绝再来一局时房间要留在结算页，不能把房间整个销毁。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, first, _second = await two_seat_finished_room(manager)

    await manager.request_rematch(room, first.token)
    applied = await manager.resolve_rematch(
        room, accepted=False, message="这局就先到这儿吧。"
    )

    assert applied is True
    assert room.room_id in manager.rooms, "拒绝再来一局不该把房间销毁"
    assert room.status == "finished"


@pytest.mark.asyncio
async def test_private_room_can_still_apply_as_host_before_starting() -> None:
    """私聊房入座后也停在 setup：这时「我来出题」才有意义。

    以前私聊房入座即开局，`soup_host_number` 的申请窗口（只在开局前）直接消失，
    按钮看起来就是「点了没反应」。
    """
    manager = RoomManager(turtle_soup_max_players=0)
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "setup"
    await manager.apply_soup_host(room, visitor.token)
    assert room.soup_host_number == 1


@pytest.mark.asyncio
async def test_second_player_can_join_after_the_soup_has_started() -> None:
    """开题后仍然允许加入：群里随时有人进来，不该被挡。

    海龟汤不是对局类（不像棋类有固定回合归属），中途入座的人直接进轮转。
    """
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_soup_room(manager)
    await manager.claim_and_start(room, visitor.token, "")
    await manager.start_game(room, visitor.token, "")
    assert room.status == "active"

    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")

    assert len(room.multiplayer.seats) == 2
    assert room.status == "active"
