from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.server import GameRoomServer


def test_rev_ignores_volatile_fields() -> None:
    """server_time 每次都变，必须排除；内容一变指纹就要变。"""
    base = {"a": 1, "messages": [{"id": 1}], "server_time": 1.0}
    assert GameRoomServer._snapshot_rev(base) == GameRoomServer._snapshot_rev(
        {**base, "server_time": 99.0}
    )
    # rev 自己也要排除，否则会自己套自己
    assert GameRoomServer._snapshot_rev(base) == GameRoomServer._snapshot_rev(
        {**base, "rev": "whatever"}
    )
    assert GameRoomServer._snapshot_rev(base) != GameRoomServer._snapshot_rev(
        {**base, "a": 2}
    )
    assert GameRoomServer._snapshot_rev(base) != GameRoomServer._snapshot_rev(
        {**base, "messages": [{"id": 2}]}
    )


@pytest.mark.asyncio
async def test_room_payload_rev_is_stable_until_content_changes() -> None:
    """同一间房两次轮询指纹相同（走小包），有变化时才变（走全量）。"""
    manager = RoomManager()
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:42001",
        platform="aiocqhttp",
        group_id="42001",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    server = GameRoomServer(
        SimpleNamespace(),
        manager,
        host="127.0.0.1",
        port=6199,
        web_root=Path("web"),
    )

    first = server._room_payload(room, visitor.token)
    time.sleep(0.05)
    second = server._room_payload(room, visitor.token)
    assert first["rev"]
    assert first["rev"] == second["rev"]

    room.add_message("system", "来了一条新消息")
    third = server._room_payload(room, visitor.token)
    assert third["rev"] != first["rev"]
