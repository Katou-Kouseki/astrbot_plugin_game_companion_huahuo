"""LLM 工具入口：只在「已有进行中的海龟汤 + 发送者已在玩家席」时动手。

这个工具最容易出的问题不是功能，而是**误触**：群里随便聊天，模型也调用它。
所以没有房间时必须直接拒绝，并且明确告诉模型不要因此开新房间。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame


def make_event(message: str = "", *, qq: str = "10001") -> SimpleNamespace:
    return SimpleNamespace(
        message_str=message,
        unified_msg_origin="aiocqhttp:group:1",
        get_sender_id=lambda: qq,
    )


def make_plugin(*, room: SimpleNamespace | None) -> GameCompanionPlugin:
    fallback_game = TurtleSoupGame(
        difficulty="normal", max_hints=3, content_level="normal"
    )
    # 房间自带的对局才是当前对局；没有房间时给一个占位对象避免 None 判空
    active_game = room.game if room is not None else fallback_game
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace(
        for_session=lambda _sid: [room] if room is not None else [],
        _turtle_soup_game=lambda _room: active_game,
        request_turtle_soup_hint=AsyncMock(return_value="灯不是他关的。"),
        join=AsyncMock(
            return_value=SimpleNamespace(number=7, display_name="", qq="")
        ),
        assign_player=AsyncMock(return_value=None),
    )
    plugin.submit_turtle_soup_question = AsyncMock(
        return_value={"verdict": "no", "reply": "否", "question_count": 2}
    )
    plugin.submit_turtle_soup_answer = AsyncMock(
        return_value={"solved": True, "solution": "他其实是灯塔管理员。"}
    )
    plugin.submit_reverse_turtle_soup_turn = AsyncMock(
        return_value={"reply": "我猜和灯塔有关？"}
    )
    return plugin


def make_room(*, mode: str = "bot_host", host_number: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        game_type="turtle_soup",
        status="active",
        player_qq="",
        multiplayer=SimpleNamespace(enabled=False),
        visitors={},
        add_message=lambda *_args, **_kwargs: None,
        game=TurtleSoupGame(
            difficulty="normal",
            max_hints=3,
            content_level="normal",
            mode=mode,
            host_number=host_number,
        ),
    )


@pytest.mark.asyncio
async def test_no_room_is_rejected_and_says_do_not_create() -> None:
    plugin = make_plugin(room=None)
    payload = json.loads(
        await plugin.turtle_soup_tool(make_event("和灯塔有关吗"), action="ask", text="和灯塔有关吗")
    )
    assert payload["ok"] is False
    assert "没有进行中的海龟汤" in payload["error"]
    plugin.submit_turtle_soup_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_goes_through_qq_source() -> None:
    plugin = make_plugin(room=make_room())
    payload = json.loads(
        await plugin.turtle_soup_tool(make_event(), action="ask", text="是灯塔吗")
    )
    assert payload["ok"] is True
    kwargs = plugin.submit_turtle_soup_question.await_args.kwargs
    assert kwargs["source"] == "qq"
    assert kwargs["actor_qq"] == "10001"
    assert "否" in payload["reply"]


@pytest.mark.asyncio
async def test_answer_returns_solution_when_solved() -> None:
    plugin = make_plugin(room=make_room())
    payload = json.loads(
        await plugin.turtle_soup_tool(
            make_event(), action="answer", text="他是灯塔管理员"
        )
    )
    assert payload["ok"] is True
    assert "灯塔管理员" in payload["reply"]


@pytest.mark.asyncio
async def test_hint_needs_no_text() -> None:
    plugin = make_plugin(room=make_room())
    payload = json.loads(await plugin.turtle_soup_tool(make_event(), action="hint"))
    assert payload["ok"] is True
    assert "灯不是他关的" in payload["reply"]


@pytest.mark.asyncio
async def test_player_host_mode_routes_to_reverse_turn() -> None:
    """玩家出题、花火猜：群里那句「提问」其实是给花火的线索。"""
    plugin = make_plugin(room=make_room(mode="player_host"))
    payload = json.loads(
        await plugin.turtle_soup_tool(make_event(), action="ask", text="和灯塔有关")
    )
    assert payload["ok"] is True
    plugin.submit_reverse_turtle_soup_turn.assert_awaited_once()
    plugin.submit_turtle_soup_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_action_and_empty_text_are_rejected() -> None:
    plugin = make_plugin(room=make_room())
    bad_action = json.loads(
        await plugin.turtle_soup_tool(make_event(), action="delete_room")
    )
    assert bad_action["ok"] is False

    empty = json.loads(
        await plugin.turtle_soup_tool(make_event(), action="ask", text="  ")
    )
    assert empty["ok"] is False
    assert "text 不能为空" in empty["error"]
