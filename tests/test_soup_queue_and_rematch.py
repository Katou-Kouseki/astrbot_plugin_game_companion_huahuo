"""海龟汤：判定排队（不再「上一条还在判定中」）+ 再来一局回到准备状态。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping


def make_puzzle():
    return puzzle_from_mapping(
        {
            "title": "测试题",
            "surface": "一个人每天都在同一时间打开一扇没有上锁的门，但从不走进去。为什么？",
            "solution": (
                "那是一间温室的通风门。他按时打开门是为了给植物换气，"
                "入口在另一侧，所以不从这里进入。"
            ),
            "key_facts": ["门用于温室通风", "他在照料植物", "真正入口位于另一侧"],
            "acceptable_variants": ["把温室描述为需要通风的种植房也可以"],
            "hints": ["门的用途不只是通行。", "这件事与室内环境有关。", "房间里主要是植物。"],
            "theme": "植物照料",
            "trick": "把通风门误认为普通入口",
        },
        content_level="normal",
    )


async def make_active_soup(manager: RoomManager):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:45001",
        platform="aiocqhttp",
        group_id="45001",
        creator_qq="30001",
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    visitor.connected = True
    visitor.last_seen_at = time.time()
    await manager.claim_and_start(room, visitor.token, "")
    await manager.start_game(room, visitor.token, "")
    await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    return room, visitor


# ---------------- 判定排队 ----------------


@pytest.mark.asyncio
async def test_cooldown_queues_instead_of_dropping_the_question() -> None:
    """冷却期内不再直接报错：这句话会被排队判出来，而不是让玩家重发。"""
    manager = RoomManager(
        turtle_soup_max_players=0, turtle_soup_free_question_cooldown=1
    )
    room, visitor = await make_active_soup(manager)
    # 上一条刚判完 0.7 秒，还剩 0.3 秒冷却
    room.game.last_judge_at = time.time() - 0.7

    started = time.monotonic()
    game, text = await manager.begin_turtle_soup_interaction(
        room, "这是温室吗？", source="web", visitor_token=visitor.token, limit=200
    )
    elapsed = time.monotonic() - started

    assert elapsed >= 0.2, "冷却没结束就开判，等于没有节流"
    assert game.processing is True
    assert text == "这是温室吗？"


@pytest.mark.asyncio
async def test_second_question_waits_for_the_running_judgement() -> None:
    """同一条判定还没结束时，后来的人排队等，而不是被告知「等一两秒」。"""
    manager = RoomManager(
        turtle_soup_max_players=0, turtle_soup_free_question_cooldown=0
    )
    room, visitor = await make_active_soup(manager)
    game, _first = await manager.begin_turtle_soup_interaction(
        room, "第一个问题？", source="web", visitor_token=visitor.token, limit=200
    )

    waiter = asyncio.create_task(
        manager.begin_turtle_soup_interaction(
            room, "第二个问题？", source="web", visitor_token=visitor.token, limit=200
        )
    )
    await asyncio.sleep(0.2)
    assert not waiter.done(), "上一条还在判定，第二个问题不该直接开判"

    await manager.cancel_turtle_soup_interaction(room, game, "测试：上一条判完了")
    second_game, second_text = await asyncio.wait_for(waiter, timeout=3)

    assert second_game is game
    assert second_text == "第二个问题？"
    assert game.processing is True


@pytest.mark.asyncio
async def test_queue_gives_up_with_readable_message(monkeypatch) -> None:
    """排队太久要给一句人话，而不是永远转圈。"""
    monkeypatch.setattr(RoomManager, "SOUP_JUDGE_QUEUE_TIMEOUT", 0.3)
    manager = RoomManager(
        turtle_soup_max_players=0, turtle_soup_free_question_cooldown=0
    )
    room, visitor = await make_active_soup(manager)
    await manager.begin_turtle_soup_interaction(
        room, "第一个问题？", source="web", visitor_token=visitor.token, limit=200
    )

    with pytest.raises(ValueError, match="排队"):
        await manager.begin_turtle_soup_interaction(
            room, "第二个问题？", source="web", visitor_token=visitor.token, limit=200
        )


# ---------------- 再来一局回到准备状态 ----------------


@pytest.mark.asyncio
async def test_rematch_returns_to_setup_and_allows_hosting() -> None:
    """点「再来一局」不该直接开新题：要留出「我来出题」的窗口。"""
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager)
    room.game.phase = "finished"
    room.status = "finished"

    await manager.prepare_soup_next_round(room, visitor.token)

    assert room.status == "setup"
    assert room.game is None
    assert len(room.multiplayer.seats) == 1
    # 座位还在，所以现在能申请当出题人
    await manager.apply_soup_host(room, visitor.token)
    assert room.soup_host_number == 1


@pytest.mark.asyncio
async def test_only_seated_player_can_start_next_round() -> None:
    manager = RoomManager(turtle_soup_max_players=4)
    room, _visitor = await make_active_soup(manager)
    room.game.phase = "finished"
    room.status = "finished"
    guest = await manager.join(room)

    with pytest.raises(PermissionError):
        await manager.prepare_soup_next_round(room, guest.token)


@pytest.mark.asyncio
async def test_next_round_is_rejected_while_the_game_is_running() -> None:
    manager = RoomManager(turtle_soup_max_players=0)
    room, visitor = await make_active_soup(manager)

    with pytest.raises(ValueError, match="还没有结束"):
        await manager.prepare_soup_next_round(room, visitor.token)


# ---------------- 播报群绑定 ----------------


def test_console_room_announces_to_the_bound_group() -> None:
    """管理台链接房没有 QQ 会话，绑了群号才发得出去。

    第二段必须是 MessageType（GroupMessage），写成小写 `group` 会被
    AstrBot 在解析会话时直接判为「不合法的 session 字符串」。
    """
    console = SimpleNamespace(
        session_id="admin:console", platform="admin", group_id="123456"
    )
    assert GameCompanionPlugin.announce_target(console) == "aiocqhttp:GroupMessage:123456"

    console.group_id = ""
    assert GameCompanionPlugin.announce_target(console) == ""


def test_console_room_without_platform_defaults_to_aiocqhttp() -> None:
    console = SimpleNamespace(session_id="admin:console", platform="", group_id="998877")
    assert GameCompanionPlugin.announce_target(console) == "aiocqhttp:GroupMessage:998877"


def test_group_room_keeps_its_own_session() -> None:
    group = SimpleNamespace(
        session_id="aiocqhttp:GroupMessage:777", platform="aiocqhttp", group_id="777"
    )
    assert GameCompanionPlugin.announce_target(group) == "aiocqhttp:GroupMessage:777"


def test_legacy_lowercase_session_is_normalized() -> None:
    """老数据里的 `平台:group:群号` 也要能发出去，别让它静默失败。"""
    group = SimpleNamespace(
        session_id="aiocqhttp:group:777", platform="aiocqhttp", group_id="777"
    )
    assert GameCompanionPlugin.announce_target(group) == "aiocqhttp:GroupMessage:777"

    private = SimpleNamespace(
        session_id="aiocqhttp:private:10001", platform="aiocqhttp", group_id=""
    )
    assert GameCompanionPlugin.announce_target(private) == "aiocqhttp:FriendMessage:10001"


def test_group_id_input_is_validated() -> None:
    assert GameCompanionPlugin._normalize_group_id(" 123456 ") == "123456"
    assert GameCompanionPlugin._normalize_group_id("#123456") == "123456"
    assert GameCompanionPlugin._normalize_group_id("") == ""
    with pytest.raises(ValueError):
        GameCompanionPlugin._normalize_group_id("abc")
    with pytest.raises(ValueError):
        GameCompanionPlugin._normalize_group_id("12")


def _platform(pid: str, name: str, type_name: str = "") -> SimpleNamespace:
    meta = SimpleNamespace(id=pid, name=name, type=type_name)
    return SimpleNamespace(meta=lambda: meta)


def _plugin_with_platforms(*platforms: SimpleNamespace) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=list(platforms))
    )
    return plugin


def test_platform_id_is_resolved_from_the_adapter_type() -> None:
    """适配器 id 被改过（如 `huahuo`）时，`aiocqhttp` 只是类型名，不能当 id 用。

    AstrBot 按 `platform.meta().id` 找适配器，找不到就只打一行 warning 并返回
    False：`cannot find platform for session aiocqhttp:GroupMessage:xxx`。
    """
    plugin = _plugin_with_platforms(_platform("huahuo", "aiocqhttp"))

    assert plugin._resolve_platform_id("aiocqhttp") == "huahuo"
    assert (
        plugin._session_with_platform("aiocqhttp:GroupMessage:576554510")
        == "huahuo:GroupMessage:576554510"
    )


def test_platform_id_keeps_a_matching_id() -> None:
    plugin = _plugin_with_platforms(
        _platform("aiocqhttp", "aiocqhttp"), _platform("qq2", "aiocqhttp")
    )

    assert plugin._resolve_platform_id("aiocqhttp") == "aiocqhttp"
    assert plugin._resolve_platform_id("qq2") == "qq2"


def test_platform_id_is_left_alone_without_a_registry() -> None:
    """拿不到平台注册表（老框架 / 最小实例）时保持原样，交给框架处理。"""
    plugin = _plugin_with_platforms()

    assert plugin._resolve_platform_id("aiocqhttp") == "aiocqhttp"
    assert (
        plugin._session_with_platform("aiocqhttp:GroupMessage:123")
        == "aiocqhttp:GroupMessage:123"
    )


@pytest.mark.asyncio
async def test_deliver_skips_sending_when_the_platform_is_missing() -> None:
    """平台不在本机就别发，也别让调用方以为发成功了（幂等标记会卡死播报）。"""
    plugin = _plugin_with_platforms(_platform("huahuo", "aiocqhttp"))
    plugin.context.send_message = AsyncMock()
    room = SimpleNamespace(
        session_id="admin:console", platform="admin", group_id="576554510"
    )

    ok = await plugin._deliver(
        GameCompanionPlugin.announce_target(room),
        MessageChain([Plain("x")]),
        what="测试",
    )

    assert ok is True
    plugin.context.send_message.assert_awaited_once()
    sent_session = plugin.context.send_message.await_args.args[0]
    assert sent_session == "huahuo:GroupMessage:576554510"


@pytest.mark.asyncio
async def test_deliver_reports_false_for_an_unloaded_platform() -> None:
    plugin = _plugin_with_platforms(_platform("telegram", "telegram"))
    plugin.context.send_message = AsyncMock()
    room = SimpleNamespace(
        session_id="admin:console", platform="admin", group_id="576554510"
    )

    ok = await plugin._deliver(
        GameCompanionPlugin.announce_target(room),
        MessageChain([Plain("x")]),
        what="测试",
    )

    assert ok is False
    plugin.context.send_message.assert_not_called()


def test_announce_problem_distinguishes_missing_group_from_missing_adapter() -> None:
    bound = SimpleNamespace(
        session_id="admin:console", platform="admin", group_id="576554510"
    )
    unbound = SimpleNamespace(session_id="admin:console", platform="admin", group_id="")

    ready = _plugin_with_platforms(_platform("huahuo", "aiocqhttp"))
    assert ready.announce_problem(bound) == ""
    assert "没有绑定 QQ 群" in ready.announce_problem(unbound)

    no_qq = _plugin_with_platforms(_platform("telegram", "telegram"))
    assert "QQ 适配器" in no_qq.announce_problem(bound)
