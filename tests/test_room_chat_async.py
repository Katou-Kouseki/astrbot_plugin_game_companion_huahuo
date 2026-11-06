from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_room(manager: RoomManager):
    return await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:20002",
        platform="aiocqhttp",
        group_id="20002",
        creator_qq="10003",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_chat_returns_before_model_finishes() -> None:
    """聊天接口不能等模型：模型还在生成时 HTTP 就该返回，回复稍后由轮询带回来。

    这就是「聊天框里的文字提示超时」的回归用例——旧实现会一直 await 到模型返回
    （最长 35 秒），而前端 12 秒就 abort 了。
    """
    manager = RoomManager()
    room = await make_room(manager)
    visitor = await manager.join(room)

    gate = asyncio.Event()

    async def slow_text_chat(**_kwargs):
        await gate.wait()
        return SimpleNamespace(completion_text="我在呢，刚才在忙。")

    provider = SimpleNamespace(text_chat=slow_text_chat)
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(
        persona_manager=None,
        get_using_provider=lambda _session_id: provider,
    )

    result = await plugin.submit_room_chat(
        room, "在吗？", visitor_token=visitor.token
    )

    # 接口立刻返回：消息已落库，回复还在后台生成
    assert result["pending"] is True
    assert room.messages[-1]["role"] == "user"
    assert not any(item["role"] == "bot" for item in room.messages)
    # 「正在打字」占位与提速轮询靠这两个字段
    assert room.bot_chat_pending == 1
    assert room.public_snapshot(visitor.token)["bot_chat_typing"] is True

    gate.set()
    await plugin.wait_room_chat_turns()

    assert room.bot_chat_pending == 0
    assert room.public_snapshot(visitor.token)["bot_chat_typing"] is False
    assert room.messages[-1]["role"] == "bot"
    assert room.messages[-1]["content"] == "我在呢，刚才在忙。"


@pytest.mark.asyncio
async def test_two_messages_are_answered_in_order() -> None:
    """同一间房的聊天仍然串行：先发的那条先生成、先入库。"""
    manager = RoomManager()
    room = await make_room(manager)
    visitor = await manager.join(room)
    replies = iter(["第一句回复。", "第二句回复。"])

    async def text_chat(**_kwargs):
        await asyncio.sleep(0.02)
        return SimpleNamespace(completion_text=next(replies))

    provider = SimpleNamespace(text_chat=text_chat)
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.context = SimpleNamespace(
        persona_manager=None,
        get_using_provider=lambda _session_id: provider,
    )

    await plugin.submit_room_chat(room, "第一问", visitor_token=visitor.token)
    # 绕过「发送太快」的冷却（0.8 秒），只为验证回复顺序
    visitor.last_chat_at = 0.0
    await plugin.submit_room_chat(room, "第二问", visitor_token=visitor.token)
    await plugin.wait_room_chat_turns()

    texts = [item["content"] for item in room.messages if item["role"] == "bot"]
    assert texts == ["第一句回复。", "第二句回复。"]
