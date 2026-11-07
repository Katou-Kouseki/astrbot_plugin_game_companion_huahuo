"""群聊指令入口：解析要准，回发要说人话，没房间不许乱响应。

最容易出错的两处：`汤` 是单字，会把「汤圆好吃」也认成指令；以及没绑定座位的
玩家一提问就抛 `PermissionError`，群里会看到一串异常原文。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.qq_bridge import (
    HELP_TEXT,
    handle_soup_command,
    parse_soup_command,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame


# ---------------- 解析 ----------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("汤 提问 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("汤提问 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("海龟汤提问：是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("/汤 推理 他是灯塔管理员", ("answer", "他是灯塔管理员")),
        ("汤 提示", ("hint", "")),
        ("汤提示", ("hint", "")),
        ("汤 揭晓", ("reveal", "")),
        ("汤 附议", ("agree", "")),
        ("汤 反对", ("disagree", "")),
        ("汤 状态", ("status", "")),
        ("汤 查看", ("history", "")),
        ("汤", ("help", "")),
        ("海龟汤", ("help", "")),
        # 没说动作词的整句按提问处理——群里最常用的就是直接问
        ("汤 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
    ],
)
def test_parse_commands(message: str, expected: tuple[str, str]) -> None:
    assert parse_soup_command(message) == expected


@pytest.mark.parametrize(
    "message",
    ["汤圆好吃", "汤面来了", "今天喝汤", "你画的汤", "", "海龟", "汤勺在哪"],
)
def test_non_commands_are_ignored(message: str) -> None:
    assert parse_soup_command(message) is None


# ---------------- 无房间 ----------------


def make_event(message: str, *, qq: str = "10001") -> SimpleNamespace:
    return SimpleNamespace(
        message_str=message,
        unified_msg_origin="aiocqhttp:group:1",
        get_sender_id=lambda: qq,
    )


def make_room(*, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        game_type="turtle_soup",
        status=status,
        multiplayer=SimpleNamespace(enabled=False),
        visitors={},
    )


def make_plugin(room: SimpleNamespace | None = None) -> SimpleNamespace:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    plugin = SimpleNamespace(
        manager=SimpleNamespace(
            for_session=lambda _sid: [room] if room is not None else [],
            _turtle_soup_game=lambda _room: game,
            request_turtle_soup_hint=AsyncMock(return_value="灯不是他关的。"),
            request_soup_reveal=AsyncMock(
                return_value={"votes": 1, "required": 2, "revealed": False}
            ),
            vote_soup_reveal=AsyncMock(
                return_value={"votes": 2, "required": 2, "revealed": True}
            ),
        ),
        submit_turtle_soup_question=AsyncMock(
            return_value={"verdict": "yes", "reply": "是", "question_count": 3}
        ),
        submit_turtle_soup_answer=AsyncMock(
            return_value={"solved": False, "coverage": 0.5, "reply": "还差一点"}
        ),
    )
    plugin.manager.game = game  # 便于测试里直接改状态
    return plugin


@pytest.mark.asyncio
async def test_help_works_without_a_room() -> None:
    plugin = make_plugin()
    assert await handle_soup_command(plugin, make_event("汤")) == HELP_TEXT


@pytest.mark.asyncio
async def test_action_without_room_explains_what_to_do() -> None:
    plugin = make_plugin()
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "还没有进行中的海龟汤" in reply
    plugin.submit_turtle_soup_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_chitchat_gets_no_reply() -> None:
    plugin = make_plugin()
    assert await handle_soup_command(plugin, make_event("汤圆好吃")) == ""


# ---------------- 有房间 ----------------


@pytest.mark.asyncio
async def test_question_goes_through_qq_source() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))

    plugin.submit_turtle_soup_question.assert_awaited_once()
    _room, text = plugin.submit_turtle_soup_question.await_args.args
    assert text == "是灯塔吗"
    kwargs = plugin.submit_turtle_soup_question.await_args.kwargs
    assert kwargs["source"] == "qq"
    assert kwargs["actor_qq"] == "10001"
    assert "是" in reply
    assert "第 3 问" in reply


@pytest.mark.asyncio
async def test_answer_reports_coverage() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 推理 他是灯塔管理员"))
    assert "还差一点" in reply
    assert "50%" in reply


@pytest.mark.asyncio
async def test_hint_is_returned() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 提示"))
    assert "灯不是他关的" in reply


@pytest.mark.asyncio
async def test_reveal_request_states_remaining_votes() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 揭晓"))
    assert "1/2" in reply
    assert "还需要 1 人同意" in reply


@pytest.mark.asyncio
async def test_permission_error_becomes_binding_guide() -> None:
    plugin = make_plugin(make_room())
    plugin.submit_turtle_soup_question.side_effect = PermissionError(
        "你的 QQ 尚未绑定到玩家席，请在 WebUI 操作"
    )

    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "绑定玩家席" in reply
    assert "绑定玩家" in reply
    assert "Traceback" not in reply


@pytest.mark.asyncio
async def test_room_must_be_active() -> None:
    """房间只是 finished/rematch_pending 时不该响应提问。"""
    plugin = make_plugin(make_room(status="finished"))
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "还没有进行中的海龟汤" in reply


@pytest.mark.asyncio
async def test_qq_activity_marks_player_online() -> None:
    """群里说话说明人在，在线人数节奏不能被算成缺席。"""
    from astrbot_plugin_game_companion_huahuo.qq_bridge import _mark_active

    visitor = SimpleNamespace(last_seen_at=0.0, connected=False)
    room = SimpleNamespace(
        multiplayer=SimpleNamespace(
            enabled=True,
            seat_for_qq=lambda qq: SimpleNamespace(visitor_token="tk") if qq == "10001" else None,
        ),
        visitors={"tk": visitor},
    )
    _mark_active(room, "10001")
    assert visitor.connected is True
    assert visitor.last_seen_at > 0
