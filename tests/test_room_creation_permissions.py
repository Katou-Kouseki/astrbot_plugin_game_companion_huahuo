from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def make_event(qq: str = "10001", *, group_id: str = "20001") -> SimpleNamespace:
    return SimpleNamespace(
        unified_msg_origin=f"aiocqhttp:group:{group_id}",
        get_group_id=lambda: group_id,
        get_sender_id=lambda: qq,
        get_sender_name=lambda: "创建者",
        get_platform_id=lambda: "aiocqhttp",
    )


def make_plugin(*, allow_non_admin: bool) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.group_rooms_enabled = True
    plugin.private_rooms_enabled = True
    plugin.allow_non_admin_group_creation = allow_non_admin
    plugin.game_admin_ids = {"10001"}
    plugin.manager = RoomManager(max_group_rooms=0, max_private_rooms=0)
    plugin._ensure_public_access = AsyncMock()
    return plugin


@pytest.mark.asyncio
async def test_closed_public_creation_switch_forces_admin_room_for_admin() -> None:
    plugin = make_plugin(allow_non_admin=False)

    room = await plugin._create_room_from_event(
        make_event(), "normal", "gomoku", requested_admin_room=False
    )

    assert room.admin_room is True


@pytest.mark.asyncio
async def test_open_public_creation_switch_defaults_admin_to_normal_room() -> None:
    plugin = make_plugin(allow_non_admin=True)

    room = await plugin._create_room_from_event(
        make_event(), "normal", "gomoku", requested_admin_room=False
    )

    assert room.admin_room is False


@pytest.mark.asyncio
async def test_admin_can_explicitly_request_admin_room_when_switch_is_open() -> None:
    plugin = make_plugin(allow_non_admin=True)

    room = await plugin._create_room_from_event(
        make_event(), "normal", "gomoku", requested_admin_room=True
    )

    assert room.admin_room is True


@pytest.mark.asyncio
async def test_non_admin_cannot_request_admin_room() -> None:
    plugin = make_plugin(allow_non_admin=True)

    with pytest.raises(PermissionError, match="只有游戏管理员"):
        await plugin._create_room_from_event(
            make_event("10002"),
            "normal",
            "gomoku",
            requested_admin_room=True,
        )


@pytest.mark.asyncio
async def test_non_admin_gets_normal_room_when_creation_is_allowed() -> None:
    plugin = make_plugin(allow_non_admin=True)

    room = await plugin._create_room_from_event(
        make_event("10002"), "normal", "gomoku", requested_admin_room=False
    )

    assert room.admin_room is False


@pytest.mark.parametrize(
    ("message", "tool_value", "expected"),
    [
        ("创建一个管理员房间", False, True),
        ("开个需要我审核玩家的房间", False, True),
        ("创建普通房间", False, False),
        ("不要管理员房间", False, False),
        ("开个房间", True, True),
    ],
)
def test_explicit_admin_room_intent_has_deterministic_fallback(
    message: str, tool_value: bool, expected: bool
) -> None:
    assert GameCompanionPlugin._admin_room_requested(message, tool_value) is expected
