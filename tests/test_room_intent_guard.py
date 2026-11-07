"""建房意图闸门：群里没人说要玩，模型不能自己开房发链接。

线上现象：群里只是唤醒花火、随口说了句话，她就开了一个游戏房间并把链接发到群里，
而群里根本没人提出要玩。根因是建房完全交给模型自由裁量——`game_companion_create_room`
是所有会话都能用的工具，模型想调就调，代码层没有任何校验（上游原版就是如此）。

这里锁定 `_room_request_intent` 的判定：只有消息里点了具体游戏名，或者带了明确的
开局请求（来一局 / 陪我玩 / 打开房间 / 来个 / 开个……），才允许真的建房；纯闲聊、
「开个玩笑」这类口语、以及提到了插件不支持的其它游戏，一律拦下。

闸门只作用于群聊：私聊里误开一局影响很小，不额外收紧。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin

GROUP = "aiocqhttp:group:13579"
PRIVATE = "aiocqhttp:FriendMessage:10001"


def make_event(text: str, *, group_id: str = "13579") -> SimpleNamespace:
    return SimpleNamespace(
        message_str=text,
        unified_msg_origin=GROUP if group_id else PRIVATE,
        get_group_id=lambda: group_id,
    )


def make_plugin(*, room: SimpleNamespace | None = None) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    room = room or SimpleNamespace(
        room_id="room-9",
        game_type="gomoku",
        difficulty="normal",
        admin_room=False,
        turtle_soup_mode="bot_host",
    )
    plugin.manager = SimpleNamespace(empty_player_timeout=60)
    plugin._create_or_reuse_room_from_event = AsyncMock(
        return_value=(room, False, False)
    )
    plugin._remember_bot_ctx = lambda *_args, **_kwargs: None
    plugin._room_url = lambda _room: "https://example.test/room/token"
    plugin._deliver_room_link = AsyncMock(return_value=True)
    return plugin


@pytest.mark.parametrize(
    "message",
    [
        "花火在吗",
        "在不在呀",
        "今天好累啊",
        "我刚玩完一局游戏回来",
        "你最近怎么样",
        "哈哈哈笑死",
        "开个玩笑",
        "来个笑话",
        "开个会吧",
        "来个段子",
        "",
        "   ",
    ],
)
def test_plain_chat_does_not_open_a_room(message: str) -> None:
    allowed, reason = GameCompanionPlugin._room_request_intent(message, "gomoku")
    assert allowed is False, reason


@pytest.mark.parametrize(
    "message,game_type",
    [
        ("花火来下五子棋", "gomoku"),
        ("五子棋", "gomoku"),
        ("陪我下盘围棋吧", "weiqi"),
        ("来一局象棋", "xiangqi"),
        ("想玩海龟汤", "turtle_soup"),
        ("给我开个海龟汤房间", "turtle_soup"),
        ("一起玩你画我猜", "draw_guess"),
        ("来一把二十一点", "blackjack"),
        ("开一局谁是卧底", "undercover"),
        ("来个五子棋", "gomoku"),
    ],
)
def test_explicit_play_request_is_allowed(message: str, game_type: str) -> None:
    allowed, reason = GameCompanionPlugin._room_request_intent(message, game_type)
    assert allowed is True, reason


@pytest.mark.parametrize("message", ["来一局吧", "来个", "开个", "整一局", "开个游戏"])
def test_colloquial_request_is_allowed(message: str) -> None:
    """口语说法「来个 / 开个」也算开局请求（除非后面接的是离题内容）。"""
    allowed, reason = GameCompanionPlugin._room_request_intent(message, "gomoku")
    assert allowed is True, reason


def test_mentioning_unsupported_game_is_rejected() -> None:
    """「来一把王者」不该蹦出一个五子棋房间。"""
    allowed, reason = GameCompanionPlugin._room_request_intent(
        "来一把王者吧", "gomoku"
    )
    assert allowed is False
    assert "不支持" in reason


def test_at_prefix_and_punctuation_are_stripped() -> None:
    allowed, _reason = GameCompanionPlugin._room_request_intent(
        "@花火？来局五子棋！", "gomoku"
    )
    assert allowed is True


def test_opening_existing_room_is_allowed() -> None:
    """已有房间时用户说「打开房间」也要能放行，否则链接永远发不出去。"""
    for message in ("打开房间", "把房间链接发我", "进房间"):
        allowed, _reason = GameCompanionPlugin._room_request_intent(message, "gomoku")
        assert allowed is True, message


@pytest.mark.asyncio
async def test_group_chitchat_is_blocked_without_touching_manager() -> None:
    """闸门拦下时必须直接返回错误，不能真的走到建房流程。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = SimpleNamespace()  # 一旦被调用就会 AttributeError，等于断言没走建房
    plugin._create_or_reuse_room_from_event = AsyncMock()

    result = await plugin.create_room_tool(
        make_event("花火在吗，今天好热"), game_type="gomoku", difficulty="normal"
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert "没有要求开一局" in payload["error"]
    plugin._create_or_reuse_room_from_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_chat_is_not_gated() -> None:
    """私聊不设闸门：一个人玩，误开一局的代价可以接受。"""
    plugin = make_plugin()

    result = await plugin.create_room_tool(
        make_event("花火在吗", group_id=""), game_type="gomoku", difficulty="normal"
    )

    assert json.loads(result)["ok"] is True
    plugin._create_or_reuse_room_from_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_request_passes_through() -> None:
    plugin = make_plugin()

    result = await plugin.create_room_tool(
        make_event("@花火 来一局五子棋"), game_type="gomoku", difficulty="normal"
    )

    assert json.loads(result)["ok"] is True
    plugin._create_or_reuse_room_from_event.assert_awaited_once()


def test_followup_yes_only_counts_after_a_block() -> None:
    """没被拦过时，「好」不能被当成开局请求（否则群里说句好就开房）。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    session = GROUP

    assert plugin._room_followup_confirmed(session, "好") is False

    plugin._remember_room_intent_block(session)
    assert plugin._room_followup_confirmed(session, "好呀！") is True
    # 同一条消息里夹着别的意思就不算肯定回应
    assert plugin._room_followup_confirmed(session, "今天好热") is False
    # 别的会话不受影响
    assert plugin._room_followup_confirmed("aiocqhttp:group:24680", "好") is False


def test_followup_yes_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    session = GROUP

    now = [1000.0]
    monkeypatch.setattr(
        "astrbot_plugin_game_companion_huahuo.main.time.monotonic",
        lambda: now[0],
    )
    plugin._remember_room_intent_block(session)
    assert plugin._room_followup_confirmed(session, "好") is True
    now[0] += plugin._ROOM_INTENT_BLOCK_TTL + 1
    assert plugin._room_followup_confirmed(session, "好") is False


@pytest.mark.asyncio
async def test_ask_then_confirm_flow_can_open_a_room() -> None:
    """被拦 → 花火追问 → 用户说「好」，第二次调用必须放行。"""
    plugin = make_plugin()

    first = json.loads(
        await plugin.create_room_tool(
            make_event("有点无聊"), game_type="gomoku", difficulty="normal"
        )
    )
    assert first["ok"] is False
    plugin._create_or_reuse_room_from_event.assert_not_awaited()

    second = json.loads(
        await plugin.create_room_tool(
            make_event("好呀"), game_type="gomoku", difficulty="normal"
        )
    )
    assert second["ok"] is True
    plugin._create_or_reuse_room_from_event.assert_awaited_once()
