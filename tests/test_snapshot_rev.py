from __future__ import annotations

import json
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


def _as_payload(response):
    """兼容真实 aiohttp Response 与测试环境里的极简 shim（返回 dict）。"""
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return json.loads(body.decode("utf-8")), len(body)
    if isinstance(body, str):
        return json.loads(body), len(body.encode("utf-8"))
    # 测试环境的 aiohttp shim：json_response 直接把 dict 挂在 body 上
    if isinstance(body, (dict, list)):
        return body, len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    raise AssertionError(f"无法解析响应: {type(response)!r}")


class _FakeRequest:
    """只实现 _state 用到的那几个属性，避免依赖 aiohttp.test_utils。"""

    def __init__(self, token: str, query: dict[str, str]) -> None:
        self.match_info = {"access_token": token}
        self.query = query
        self.cookies: dict[str, str] = {}


@pytest.mark.asyncio
async def test_state_returns_tiny_payload_when_rev_matches() -> None:
    manager = RoomManager()
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:42002",
        platform="aiocqhttp",
        group_id="42002",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    for index in range(40):
        room.add_message("user", f"第 {index} 条聊天消息，稍微长一点用来撑体积。" * 2, visitor=visitor)
    server = GameRoomServer(
        SimpleNamespace(trusted_browser_enabled=False, trusted_identity_store=None),
        manager,
        host="127.0.0.1",
        port=6199,
        web_root=Path("web"),
    )

    full_payload, full_size = _as_payload(
        await server._state(
            _FakeRequest(room.access_token, {"visitor_token": visitor.token})
        )
    )
    rev = full_payload["data"]["room"]["rev"]
    assert rev

    tiny_payload, tiny_size = _as_payload(
        await server._state(
            _FakeRequest(
                room.access_token, {"visitor_token": visitor.token, "rev": rev}
            )
        )
    )
    assert tiny_payload["data"]["unchanged"] is True
    assert tiny_payload["data"]["rev"] == rev
    assert tiny_payload["data"]["server_time"] > 0
    assert tiny_size < 400
    assert tiny_size * 10 < full_size

    # 内容一变（新消息），旧指纹立刻失效 → 回到全量
    room.add_message("system", "新消息")
    changed_payload, _changed_size = _as_payload(
        await server._state(
            _FakeRequest(
                room.access_token, {"visitor_token": visitor.token, "rev": rev}
            )
        )
    )
    assert "unchanged" not in changed_payload["data"]
    assert changed_payload["data"]["room"]["rev"] != rev
