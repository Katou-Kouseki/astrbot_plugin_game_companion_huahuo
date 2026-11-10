"""0.5.81 这一批修复的回归测试。

都是「跑通全量测试之后才暴露出来」的实际问题：
A. 管理台题库页三个接口只写了 handler，忘了注册 → 整个页签 404；
B. `max_hints=0`（不限）时，群里显示「提示剩 0」而网页还有提示；
C. 玩家出题模式不走模型判定，问答一条都同步不到群里；裁决还会统一记成 web；
D. 问答速报队列延迟 12 秒，房间关掉后还会往群里发，换题后标题会串。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import astrbot_plugin_game_companion_huahuo.main as main_module
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    SoupEntry,
    TurtleSoupGame,
    fallback_puzzle,
)


# ---------------- A. 管理台题库接口必须注册 ----------------


def test_console_library_endpoints_are_registered() -> None:
    """只定义 handler 不注册，前端请求就是 404 —— 这正是 0.5.80 的疏漏。"""
    registered: list[tuple] = []
    plugin = object.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        register_web_api=lambda *args, **kwargs: registered.append(args)
    )

    GameCompanionPlugin._register_page_api(plugin)  # noqa: SLF001

    paths = {args[0] for args in registered}
    for name in ("soup_library", "soup_library_reset", "soup_custom_delete"):
        assert any(path.endswith(f"/{name}") for path in paths), f"{name} 没有注册"


def test_console_library_methods_get_the_right_http_verbs() -> None:
    registered: dict[str, list[str]] = {}
    plugin = object.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        register_web_api=lambda path, _handler, methods, _desc: registered.update(
            {path: list(methods)}
        )
    )

    GameCompanionPlugin._register_page_api(plugin)  # noqa: SLF001

    def verbs(name: str) -> list[str]:
        return next(value for key, value in registered.items() if key.endswith(f"/{name}"))

    assert verbs("soup_library") == ["GET"]
    assert verbs("soup_library_reset") == ["POST"]
    assert verbs("soup_custom_delete") == ["POST"]


# ---------------- B. 提示额度单一来源 ----------------


def make_ready_game(*, max_hints: int) -> TurtleSoupGame:
    game = TurtleSoupGame(
        difficulty="normal", max_hints=max_hints, content_level="normal"
    )
    game.puzzle = fallback_puzzle(content_level="normal")
    game.phase = "ready"
    return game


def test_unlimited_hints_report_the_real_budget() -> None:
    """`max_hints=0` 表示不限，上限就是题库给的条数，不能拿 0 去减。"""
    game = make_ready_game(max_hints=0)
    available = len(game.puzzle.hints)

    assert game.hint_limit == available
    assert game.hint_left == available
    assert game.snapshot()["hint_left"] == available

    game.hints_used = 1
    assert game.hint_left == available - 1
    assert game.snapshot()["hint_left"] == available - 1


def test_capped_hints_still_respect_the_cap() -> None:
    game = make_ready_game(max_hints=2)
    assert game.hint_limit == 2
    game.hints_used = 2
    assert game.hint_left == 0
    # 用超了也不会变成负数
    game.hints_used = 5
    assert game.hint_left == 0


def test_progress_helper_and_snapshot_agree() -> None:
    """群里回的「提示剩 N」与网页快照必须同源。"""
    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_max_questions = 15

    for max_hints in (0, 3):
        game = make_ready_game(max_hints=max_hints)
        game.hints_used = 1
        progress = plugin._soup_progress(game)  # noqa: SLF001
        assert progress["hint_left"] == game.snapshot()["hint_left"]
        assert progress["hint_left"] == game.hint_left


def test_progress_reports_question_budget() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_max_questions = 15
    game = make_ready_game(max_hints=3)
    game.question_count = 4

    progress = plugin._soup_progress(game)  # noqa: SLF001
    assert progress["question_limit"] == 15
    assert progress["question_count"] == 4
    assert progress["question_left"] == 11

    plugin.turtle_soup_max_questions = 0
    assert plugin._soup_progress(game)["question_left"] is None  # noqa: SLF001


# ---------------- C. 玩家出题模式：来源与群同步 ----------------


def test_pending_judge_remembers_the_real_source() -> None:
    game = make_ready_game(max_hints=3)
    game.set_pending_judge("question", "是灯塔吗", asker_number=2, source="qq")
    assert game.pending_judge["source"] == "qq"

    game.clear_pending_judge()
    game.set_pending_judge("question", "是灯塔吗", asker_number=2)
    # 未知来源一律按网页，不要凭空造第三种
    assert game.pending_judge["source"] == "web"


@pytest.mark.asyncio
async def test_host_mode_judgement_is_synced_to_group() -> None:
    """玩家出题模式没有模型判定事件，裁决才是「有结果」的时刻。"""
    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_sync = True
    plugin.turtle_soup_qq_bridge = True
    plugin._queue_soup_group_sync = Mock()  # noqa: SLF001
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None  # noqa: SLF001

    game = make_ready_game(max_hints=3)
    game.host_number = 1
    room = SimpleNamespace(
        room_id="room-host",
        game_type="turtle_soup",
        source="group",
        session_id="aiocqhttp:group:1",
        game=game,
        status="active",
    )

    await plugin._on_room_event(  # noqa: SLF001
        "soup_host_judged", room, {"entry_seq": 42}
    )

    plugin._queue_soup_group_sync.assert_called_once()  # noqa: SLF001
    assert plugin._queue_soup_group_sync.call_args.args == (room,)  # noqa: SLF001
    kwargs = plugin._queue_soup_group_sync.call_args.kwargs  # noqa: SLF001
    # 裁决结果即使源自群里提问也要发到群：群里当时只看到「等出题人回答」
    assert kwargs.get("host_verdict") is True
    # 序号要一路带过去，末尾追加「揭晓」合成条目时才不会指错
    assert kwargs.get("entry_seq") == 42


@pytest.mark.asyncio
async def test_host_mode_judged_entry_uses_qq_source() -> None:
    """群里问的问题经出题人裁决后，记录要标成 qq（速报与角标都看它）。"""
    manager = RoomManager()
    emitted: list[tuple[str, dict]] = []

    async def capture(event: str, _room: object, payload: dict) -> None:
        emitted.append((event, dict(payload)))

    manager.event_callback = capture
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:1",
        platform="aiocqhttp",
        group_id="1",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    host = await manager.join(room)
    host.qq = "10001"
    host.identity_confirmed = True
    asker = await manager.join(room)
    asker.qq = "10002"
    asker.identity_confirmed = True
    for visitor in (host, asker):
        visitor.connected = True
        await manager.claim_and_start(room, visitor.token, "")
    await manager.start_game(room, host.token, "")

    game = manager._turtle_soup_game(room)  # noqa: SLF001
    await manager.complete_turtle_soup_generation(
        room, game, fallback_puzzle(content_level="normal")
    )
    # 切到玩家出题模式：第一个座位当出题人
    game.mode = "player_host"
    game.host_number = 1

    await manager.begin_soup_host_judgement(
        room, "是不是在下雨？", kind="question", source="qq", actor_qq="10002"
    )
    # 裁决只能由出题人（1 号位）做
    await manager.judge_soup_pending(room, host.token, verdict="yes")

    entries = [entry for entry in game.entries if entry.kind == "question"]
    assert entries, "裁决后必须留下一条问答记录"
    assert entries[-1].source == "qq"
    judged = next(p for name, p in emitted if name == "soup_host_judged")
    assert judged["entry_seq"] == entries[-1].seq > 0


# ---------------- D. 速报队列的清理与串题 ----------------


def make_sync_plugin() -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_sync = True
    plugin.turtle_soup_qq_bridge = True
    plugin.SOUP_SYNC_WINDOW = 0
    plugin._send_to_origin = AsyncMock(return_value=True)  # noqa: SLF001
    return plugin


def make_group_room(game: TurtleSoupGame, *, status: str = "active"):
    return SimpleNamespace(
        room_id="room-sync",
        source="group",
        session_id="aiocqhttp:group:1",
        status=status,
        game=game,
    )


def add_question(game: TurtleSoupGame, prompt: str) -> None:
    game.entries.append(
        SoupEntry(kind="question", prompt=prompt, response="不是", source="web")
    )


@pytest.mark.asyncio
async def test_closed_room_does_not_get_a_late_summary() -> None:
    """队列延迟 12 秒，这期间玩家完全可能已经关房走人。"""
    plugin = make_sync_plugin()
    game = make_ready_game(max_hints=3)
    room = make_group_room(game)
    add_question(game, "和灯塔有关吗？")
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    room.status = "closed"
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001
    assert room.room_id not in plugin.__dict__.get("_soup_sync_state", {})


@pytest.mark.asyncio
async def test_summary_keeps_the_puzzle_title_it_started_with() -> None:
    """12 秒内点「再来一道」时，标题要跟问答一起冻结，不能读新题。"""
    plugin = make_sync_plugin()
    # 这个用例测的是横条图渲染参数，默认纯文字模式不会走图
    plugin.turtle_soup_qq_sync_style = "image"
    game = make_ready_game(max_hints=3)
    old_title = game.puzzle.title
    room = make_group_room(game)
    add_question(game, "和灯塔有关吗？")
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    # 换题：直接改标题，避免题库随机挑到同名题导致断言不稳
    game.puzzle = replace(game.puzzle, title="换了一道完全不同的题")

    plugin._soup_sync_t2i = AsyncMock(return_value="BASE64")  # noqa: SLF001
    plugin._send_to_origin_image = AsyncMock(return_value=False)  # noqa: SLF001
    plugin._soup_sync_chain = lambda *args, **kwargs: None  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    data = plugin._soup_sync_t2i.await_args.args[0]  # noqa: SLF001
    assert data["puzzle"] == old_title[:32]


def test_plugin_imports_stay_light() -> None:
    """顺手确认改了这么多之后模块还能正常导入（没有循环引用）。"""
    assert main_module.GameCompanionPlugin is GameCompanionPlugin


# ---------------- E. 花火自己的提问不吃玩家额度 ----------------


def test_bot_questions_do_not_eat_the_player_budget() -> None:
    """每局上限是给玩家的：花火问得越多，玩家能问的不该变少。"""
    game = make_ready_game(max_hints=3)
    game.record_question("玩家问的？", "yes", source="web", matched_facts=set(), player_number=2)
    game.record_question("花火问的？", "no", source="web", matched_facts=set(), player_number=None)
    game.record_question("花火又问？", "no", source="web", matched_facts=set(), player_number=None)

    assert game.question_count == 3
    assert game.bot_question_count == 2
    assert game.player_question_count == 1

    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_max_questions = 15
    progress = plugin._soup_progress(game)  # noqa: SLF001
    assert progress["question_count"] == 1
    assert progress["question_left"] == 14


@pytest.mark.asyncio
async def test_question_limit_ignores_bot_questions() -> None:
    """花火问到上限了，玩家仍然能继续问。"""
    plugin = object.__new__(GameCompanionPlugin)
    plugin.turtle_soup_max_questions = 2
    plugin.manager = SimpleNamespace(
        _turtle_soup_game=lambda _room: None,
    )

    game = make_ready_game(max_hints=3)
    room = SimpleNamespace(game_type="turtle_soup", game=game, status="active")
    plugin.manager._turtle_soup_game = lambda _room: game  # noqa: SLF001

    # 花火自己问了 5 条，玩家一条都没问
    for _ in range(5):
        game.record_question("花火问的？", "no", source="web", matched_facts=set())

    plugin.manager.begin_turtle_soup_interaction = AsyncMock(
        return_value=(game, "玩家问的？")
    )
    plugin._call_room_model = AsyncMock(  # noqa: SLF001
        return_value='{"verdict":"no","matched_facts":[]}'
    )
    plugin._soup_judge_provider = lambda _room: None  # noqa: SLF001
    plugin.manager.resolve_turtle_soup_question = AsyncMock(return_value=True)
    plugin.manager.cancel_turtle_soup_interaction = AsyncMock(return_value=None)

    result = await plugin.submit_turtle_soup_question(
        room, "玩家问的？", source="web", visitor_token="tk"
    )

    assert not result.get("limited"), "花火问满不该连带玩家也问不了"
