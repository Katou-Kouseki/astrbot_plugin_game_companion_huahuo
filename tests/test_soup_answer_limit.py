"""每局推理次数上限：用完了还没人答对，就直接揭晓汤底。

额度是整局共享的，不是每人一份——群里二十个人各猜一次就是二十次模型判定，
按人头给额度等于没有上限。这里锁住「到限即收题」这条，以及额度为 0 时不限。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.qq_bridge import _answer_reply
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping

SOLUTION = "他是灯塔管理员，某天忘了点灯导致船只失事，妻女因此遇难，他愧疚自杀。"


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "灯塔",
            "surface": "一个人每天摆两副碗筷，后来他自杀了，为什么？",
            "solution": SOLUTION,
            "key_facts": ["在等人", "有人遇难", "他自责"],
            "acceptable_variants": [],
            "hints": ["碗筷是给谁的", "和一次事故有关", "与他的职业有关"],
            "theme": "灯塔",
            "trick": "摆碗筷是在等不会再回来的人",
        },
        content_level="all_ages",
    )


async def build_room(manager: RoomManager):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:20001",
        platform="aiocqhttp",
        group_id="20001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
        turtle_soup_mode="bot_host",
    )
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "")
    if room.status == "setup":
        await manager.start_game(room, player.token, "")
    if room.game.puzzle is None:
        await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    return room


async def guess(manager: RoomManager, room, text: str, *, solved: bool = False) -> bool:
    return await manager.resolve_turtle_soup_answer(
        room,
        room.game,
        text,
        solved=solved,
        source="web",
        matched_facts=set(),
        coverage=0.2,
        missing="还没说到职业",
    )


@pytest.mark.asyncio
async def test_reveals_when_answer_budget_runs_out() -> None:
    manager = RoomManager(turtle_soup_max_players=1, turtle_soup_follow_difficulty=False, turtle_soup_max_answers=2)
    room = await build_room(manager)

    assert await guess(manager, room, "第一次瞎猜")
    assert room.game.answer_attempts == 1
    assert room.status == "active", "额度没用完之前不能收题"
    assert not room.game.finished

    assert await guess(manager, room, "第二次瞎猜")
    assert room.game.answer_attempts == 2
    assert room.game.end_reason == "answer_limit"
    assert room.game.finished, "用完还猜错就该直接揭晓"
    assert room.status == "finished"


@pytest.mark.asyncio
async def test_reveal_is_announced_as_an_event() -> None:
    """群播报与复盘挂在这个事件上，漏发的话群里看不到「为什么结束了」。"""
    events: list[tuple[str, dict]] = []

    async def callback(name, _room, payload):
        events.append((name, dict(payload)))

    manager = RoomManager(
        turtle_soup_max_players=1,
        turtle_soup_follow_difficulty=False, turtle_soup_max_answers=1,
        event_callback=callback,
    )
    room = await build_room(manager)
    await guess(manager, room, "唯一一次机会，猜错了")

    revealed = [payload for name, payload in events if name == "soup_revealed"]
    assert revealed and revealed[0]["reason"] == "answer_limit"


@pytest.mark.asyncio
async def test_correct_guess_before_budget_ends_keeps_its_own_path() -> None:
    manager = RoomManager(turtle_soup_max_players=1, turtle_soup_follow_difficulty=False, turtle_soup_max_answers=3)
    room = await build_room(manager)

    await guess(manager, room, "先错一次")
    await guess(manager, room, "对了", solved=True)

    assert room.game.solved, "答对走的是 solved 分支"
    assert room.game.end_reason != "answer_limit"
    assert room.status == "finished"


@pytest.mark.asyncio
async def test_zero_means_unlimited() -> None:
    manager = RoomManager(turtle_soup_max_players=1, turtle_soup_follow_difficulty=False, turtle_soup_max_answers=0)
    room = await build_room(manager)

    for index in range(6):
        assert await guess(manager, room, f"第 {index} 次瞎猜")
        assert room.status == "active"
    assert not room.game.finished
    assert room.game.end_reason != "answer_limit"


@pytest.mark.asyncio
async def test_budget_is_shared_by_the_whole_room() -> None:
    """额度不按人头算：两个人入座，仍然是整局共享一份。"""
    manager = RoomManager(turtle_soup_max_players=0, turtle_soup_follow_difficulty=False, turtle_soup_max_answers=3)
    room = await build_room(manager)
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")

    assert room.game.answer_attempts == 0
    for index in range(3):
        await guess(manager, room, f"第 {index} 次")
    assert room.game.finished


def test_answer_reply_shows_solution_when_budget_is_gone() -> None:
    reply = _answer_reply(
        {
            "solved": False,
            "answer_limit_reached": True,
            "solution": SOLUTION,
            "coverage": 0.4,
        }
    )
    assert "推理机会已经用完" in reply
    assert SOLUTION in reply


def test_answer_reply_keeps_coverage_when_budget_remains() -> None:
    reply = _answer_reply({"solved": False, "coverage": 0.4, "reply": "接近了"})
    assert "接近了" in reply and "40%" in reply
    assert "推理机会已经用完" not in reply


# ---------------- 玩家出题模式同样受额度约束 ----------------


async def make_host_room(manager: RoomManager, *, seats: int = 2):
    import time

    from astrbot_plugin_game_companion_huahuo.models import PlayerSeat

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
    for _ in range(seats):
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


@pytest.mark.asyncio
async def test_player_host_mode_also_stops_at_the_budget() -> None:
    """出题人判「不对」也算一次推理：用完就收题，别让一局无限拖下去。"""
    manager = RoomManager(turtle_soup_max_players=0, turtle_soup_follow_difficulty=False, turtle_soup_max_answers=2)
    room, visitors = await make_host_room(manager)
    host, solver = visitors[0], visitors[1]
    await manager.apply_soup_host(room, host.token)
    await manager.start_soup_host_game(
        room,
        host.token,
        title="雨夜的伞",
        surface="她每天都带两把伞出门，明明一个人住。邻居问她，她只说「习惯了」。为什么？",
        solution="她丈夫三年前在雨夜出车祸去世，两人一直同撑一把伞上班，她带两把是替他拿的。",
        hints=["和「一起去上班」有关", "伞不是给活人准备的"],
    )

    for attempt in range(2):
        await manager.begin_soup_host_judgement(
            room,
            f"第 {attempt + 1} 次猜：她是给丈夫带伞",
            kind="answer",
            source="web",
            visitor_token=solver.token,
            limit=800,
        )
        await manager.judge_soup_pending(room, host.token, verdict="wrong")

    assert room.game.answer_attempts == 2
    assert room.game.end_reason == "answer_limit"
    assert room.game.finished


@pytest.mark.asyncio
async def test_reveal_event_gates_solution_on_group_vote() -> None:
    """汤底只在本局群内有人参与揭晓时发到群；其余路径也要发一条「已揭晓」提示。

    以前 reveal 事件会把合成「投票揭晓汤底」条目塞进群同步，群里看到的是
    「花火推理：投票揭晓汤底 → 本题已揭晓，汤底见下方」，却看不到汤底；
    后来又矫枉过正，超时和猜中这两条路径群里干脆一点声音都没有。
    """
    from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin

    plugin = object.__new__(GameCompanionPlugin)
    calls: list = []
    plugin._announce_soup_reveal = lambda _room, **kw: calls.append(kw)  # noqa: SLF001
    plugin._spawn = lambda coro: coro

    room = SimpleNamespace(game_type="turtle_soup", session_id="aiocqhttp:group:1")

    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "qq", "reason": "revealed", "qq_participated": True},
    )
    assert calls == [{"with_solution": True}]

    calls.clear()
    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "web", "reason": "revealed", "qq_participated": False},
    )
    # 只有网页玩家投票揭晓：群里知道已揭晓，但不给汤底
    assert calls == [{"with_solution": False}]

    calls.clear()
    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "web", "reason": "answer_limit", "qq_participated": False},
    )
    # 网页玩家的推理额度用完自动揭晓：群里只说「已揭晓」，不给汤底
    assert calls == [{"with_solution": False}]

    calls.clear()
    # 群里发起、网页玩家投最后一票：按「本局群内参与过」而不是最后一票来源判定
    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "web", "reason": "revealed", "qq_participated": True},
    )
    assert calls == [{"with_solution": True}]

    calls.clear()
    # 超时自动揭晓也不能让群里静默
    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "system", "reason": "timeout", "qq_participated": False},
    )
    assert calls == [{"with_solution": False}]
