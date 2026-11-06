"""玩家上座出题人：申请 → 出题 → 他人提问 → 出题人裁决 → 花火参与。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from astrbot_plugin_game_companion_huahuo.models import PlayerSeat
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_room(manager: RoomManager, *, seats: int = 2):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:43001",
        platform="aiocqhttp",
        group_id="43001",
        creator_qq="20001",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitors = []
    for index in range(seats):
        visitor = await manager.join(room)
        visitor.connected = True
        visitor.last_seen_at = time.time()
        visitors.append(visitor)
    room.multiplayer.enabled = True
    room.multiplayer.capacity = seats
    room.multiplayer.seats = [
        PlayerSeat(
            number=index + 1,
            visitor_token=visitor.token,
            display_name=f"玩家{index + 1}",
            identity_confirmed=True,
        )
        for index, visitor in enumerate(visitors)
    ]
    room.status = "setup"
    return room, visitors


async def start_host_game(manager: RoomManager, room, host_token: str):
    await manager.apply_soup_host(room, host_token)
    return await manager.start_soup_host_game(
        room,
        host_token,
        title="雨夜的伞",
        surface="她每天都带两把伞出门，明明一个人住。邻居问她，她只说「习惯了」。为什么？",
        solution="她丈夫三年前在雨夜出车祸去世，两人一直同撑一把伞上班，她带两把是替他拿的。",
        hints=["和「一起去上班」有关", "伞不是给活人准备的"],
    )


@pytest.mark.asyncio
async def test_host_applies_and_starts_puzzle_without_llm() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)

    applied = await manager.apply_soup_host(room, visitors[0].token)
    assert applied["host_number"] == 1
    assert room.soup_host_number == 1

    game = await start_host_game(manager, room, visitors[0].token)

    assert game.host_number == 1
    assert game.puzzle is not None
    assert game.puzzle.solution.startswith("她丈夫三年前")
    assert game.puzzle.hints == ("和「一起去上班」有关", "伞不是给活人准备的")
    assert room.status == "active"
    assert room.soup_host_number == 0, "题目提交后申请人转成局内出题人"
    # 轮转落到不是出题人的那一席
    current = room.multiplayer.seats[room.multiplayer.current_turn_index]
    assert current.number == 2


@pytest.mark.asyncio
async def test_host_cannot_ask_and_others_wait_for_human_judge() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)

    with pytest.raises(ValueError):
        await manager.begin_soup_host_judgement(
            room, "她是不是在等人？", kind="question", visitor_token=visitors[0].token
        )

    await manager.begin_soup_host_judgement(
        room, "她是不是在等人？", kind="question", visitor_token=visitors[1].token
    )
    assert game.pending_judge is not None
    assert game.pending_judge["asker_name"] == "玩家2"

    # 只有出题人能裁决
    with pytest.raises(PermissionError):
        await manager.judge_soup_pending(room, visitors[1].token, verdict="yes")

    result = await manager.judge_soup_pending(room, visitors[0].token, verdict="yes")
    assert result["finished"] is False
    assert game.pending_judge is None
    assert game.entries[-1].prompt == "她是不是在等人？"
    assert game.entries[-1].verdict == "yes"
    assert game.entries[-1].player_number == 2


@pytest.mark.asyncio
async def test_host_judges_correct_answer_and_finishes_with_solver() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)

    await manager.begin_soup_host_judgement(
        room,
        "她丈夫去世了，另一把伞是替他拿的。",
        kind="answer",
        visitor_token=visitors[1].token,
    )
    result = await manager.judge_soup_pending(room, visitors[0].token, verdict="correct")

    assert result["finished"] is True
    assert game.finished is True
    assert game.solved is True
    assert game.solver_number == 2
    assert game.winner == "human"
    assert room.status == "finished"


@pytest.mark.asyncio
async def test_bot_guess_correct_marks_bot_win() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)

    await manager.begin_soup_host_judgement(
        room, "她替去世的丈夫带伞。", kind="answer", for_bot=True
    )
    assert game.pending_judge["for_bot"] is True
    await manager.judge_soup_pending(room, visitors[0].token, verdict="correct")

    assert game.finished is True
    assert game.bot_solved is True
    assert game.solved is False
    assert game.winner == "bot"
    assert game.end_reason == "bot_solved"


@pytest.mark.asyncio
async def test_turn_rotation_skips_the_host_seat() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager, seats=3)
    game = await start_host_game(manager, room, visitors[1].token)

    assert game.host_number == 2
    room.multiplayer.current_turn_index = 0  # 1 号
    manager._advance_multiplayer_turn(room)
    assert room.multiplayer.seats[room.multiplayer.current_turn_index].number == 3

    # 从 3 号继续推进时要跳过 2 号出题人
    manager._advance_multiplayer_turn(room)
    assert room.multiplayer.seats[room.multiplayer.current_turn_index].number == 1


@pytest.mark.asyncio
async def test_host_reveal_is_immediate_without_votes() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)

    payload = await manager.request_soup_reveal(
        room, source="web", visitor_token=visitors[0].token
    )

    assert payload["revealed"] is True
    assert game.finished is True
    assert game.end_reason == "revealed"


def test_host_mode_routes_and_ui_are_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    server = (root / "server.py").read_text(encoding="utf-8")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    page = (root / "web" / "index.html").read_text(encoding="utf-8")

    assert '"/api/room/{access_token}/soup/host"' in server
    assert '"/api/room/{access_token}/soup/judge"' in server
    for element in ("soupHostPanel", "soupJudgePanel", "soupHostSubmit", "soupJudgeActions"):
        assert f'id="{element}"' in page
    assert 'request("POST", "soup/host"' in script
    assert 'request("POST", "soup/judge"' in script
    assert "drawSoupAvatar" in script


# ---------------- 花火参与的开关与节奏 ----------------


def make_bot_plugin(manager: RoomManager, *, join: bool, every: int = 1):
    """构造一个只跑「花火回合」的插件实例，模型调用换成可控桩。"""
    from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin

    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.turtle_soup_bot_join = join
    plugin.turtle_soup_bot_join_every = every
    calls: list[str] = []
    reply = {"value": '{"action":"question","text":"这件事和一场意外有关吗？"}'}

    async def fake_model(_room, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append("called")
        return reply["value"]

    plugin._call_room_model = fake_model  # type: ignore[assignment]
    return plugin, calls, reply


async def ask_and_judge(manager: RoomManager, room, visitor, host_token: str, text: str):
    """让某位玩家提一个问题，出题人答「是」，模拟一个完整的玩家行动。"""
    for index, seat in enumerate(room.multiplayer.seats):
        if seat.visitor_token == visitor.token:
            room.multiplayer.current_turn_index = index
            break
    await manager.begin_soup_host_judgement(
        room, text, kind="question", visitor_token=visitor.token
    )
    await manager.judge_soup_pending(room, host_token, verdict="yes")


@pytest.mark.asyncio
async def test_bot_join_switch_off_never_calls_model() -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_bot_join=False)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)
    plugin, calls, _ = make_bot_plugin(manager, join=False)

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "他是不是在等人？")
    await plugin._soup_bot_turn(room, game)

    assert calls == [], "关掉之后不该再花一次模型调用"
    assert game.pending_judge is None
    assert room.soup_bot_join is False, "房间快照要带上开关，前端才知道怎么写提示"


@pytest.mark.asyncio
async def test_bot_join_rhythm_is_one_for_one() -> None:
    """默认 every=1：玩家问一句，花火跟一句，交替进行。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)
    plugin, calls, _ = make_bot_plugin(manager, join=True, every=1)

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "他是不是在等人？")
    await plugin._soup_bot_turn(room, game)
    assert calls == ["called"]
    assert game.pending_judge is not None
    assert game.pending_judge["for_bot"] is True
    assert game.pending_judge["kind"] == "question"

    # 花火自己的提问被裁决后，还没等到新的玩家行动，不能连着再动一次
    await manager.judge_soup_pending(room, visitors[0].token, verdict="no")
    await plugin._soup_bot_turn(room, game)
    assert calls == ["called"], "花火不能自己接自己"

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "和钱有关吗？")
    await plugin._soup_bot_turn(room, game)
    assert calls == ["called", "called"], "玩家再问一句，花火才跟下一句"


@pytest.mark.asyncio
async def test_bot_join_every_two_waits_for_second_player_move() -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_bot_join_every=2)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)
    plugin, calls, _ = make_bot_plugin(manager, join=True, every=2)

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "他是不是在等人？")
    await plugin._soup_bot_turn(room, game)
    assert calls == [], "every=2 时第一个玩家行动还不够"

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "和钱有关吗？")
    await plugin._soup_bot_turn(room, game)
    assert calls == ["called"]
    assert game.pending_judge is not None


@pytest.mark.asyncio
async def test_bot_cannot_guess_before_three_public_questions() -> None:
    """线索太少时模型想猜也不许，让它继续提问——避免开局就蒙一个。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room, visitors = await make_room(manager)
    game = await start_host_game(manager, room, visitors[0].token)
    plugin, calls, reply = make_bot_plugin(manager, join=True)
    reply["value"] = '{"action":"guess","answer":"她替去世的丈夫带伞。"}'

    await ask_and_judge(manager, room, visitors[1], visitors[0].token, "她是不是在等人？")
    await plugin._soup_bot_turn(room, game)

    assert calls == ["called"]
    assert game.pending_judge is None, "少于 3 条公开问答时不该提交推理"
    assert game.finished is False


def test_bot_join_settings_are_exposed_in_schema() -> None:
    import json

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["turtle_soup"]["items"]

    assert items["bot_join_player_host"]["default"] is True
    assert items["bot_join_every"]["default"] == 1
    source = (root / "main.py").read_text(encoding="utf-8")
    assert '"turtle_soup.bot_join_player_host"' in source
    assert '"turtle_soup.bot_join_every"' in source
