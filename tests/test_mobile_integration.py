from __future__ import annotations

import types

import pytest

from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


@pytest.mark.asyncio
async def test_mobile_room_uses_lan_server_and_prebinds_phone_user() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = ""
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(
        running=True,
        local_base_url="http://127.0.0.1:6331",
    )
    plugin.quick_tunnel = types.SimpleNamespace(running=False, ready=False, url="")

    result = await plugin.mobile_create_room("mobile-owner", "gomoku")

    assert result["game_type"] == "gomoku"
    assert result["url"].startswith("http://127.0.0.1:6331/room/")
    assert "visitor_token=" in result["url"]
    room = plugin.manager.rooms[result["room_id"]]
    assert room.player_qq == "mobile-owner"
    assert room.player_identity_confirmed is True
    assert room.status == "active"


@pytest.mark.asyncio
async def test_mobile_room_reuses_the_phone_users_active_room() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.server_enabled = True
    plugin.private_rooms_enabled = True
    plugin.server_host = "0.0.0.0"
    plugin.auto_quick_tunnel = False
    plugin.public_base_url = "https://games.example.com"
    plugin.manager = RoomManager(max_private_rooms=1)
    plugin.room_server = types.SimpleNamespace(running=True, local_base_url="http://127.0.0.1:6331")
    plugin.quick_tunnel = types.SimpleNamespace(running=False, ready=False, url="")

    first = await plugin.mobile_create_room("mobile-owner", "tictactoe")
    second = await plugin.mobile_create_room("mobile-owner", "gomoku")

    assert second["room_id"] == first["room_id"]
    assert second["game_type"] == "tictactoe"
    assert second["reused_room"] is True


def test_room_url_rejects_a_running_but_unhealthy_tunnel() -> None:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.public_base_url = ""
    plugin.quick_tunnel = types.SimpleNamespace(
        running=True,
        ready=False,
        url="https://stale.trycloudflare.com",
    )
    room = types.SimpleNamespace(access_token="room-token")

    with pytest.raises(RuntimeError, match="尚未就绪"):
        plugin._room_url(room)
