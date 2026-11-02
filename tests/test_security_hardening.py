"""安全加固回归：座位令牌不外泄、播报/复盘/认输需身份、清扫与看门狗容错。"""

from __future__ import annotations

import base64
import time

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.server import GameRoomServer


async def _duel_room(manager: RoomManager, game_type: str = "gomoku"):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:10001",
        platform="aiocqhttp",
        group_id="10001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    await manager.add_ai_seat(room, player.token, side="white")
    await manager.wait_bot_turns()
    spectator = await manager.join(room)
    return room, player, spectator


def test_seat_snapshot_never_leaks_visitor_token() -> None:
    """座位快照对全房间可见，绝不能带原始 token（那是接口的身份凭证）。"""
    import asyncio

    async def run() -> dict:
        manager = RoomManager(max_private_rooms=0)
        room, player, _spectator = await _duel_room(manager)
        return room.public_snapshot(player.token)

    snapshot = asyncio.run(run())
    seats = snapshot["player_seats"]
    assert seats, "应当有玩家席"
    for seat in seats:
        assert "visitor_token" not in seat
        assert "uid" in seat
    # 每个座位都有不可逆 uid，且互不相同（AI 座位另有自己的随机 token）
    uids = [seat["uid"] for seat in seats]
    assert all(uids) and len(set(uids)) == len(uids)
    assert all(len(uid) == 10 and "-" not in uid for uid in uids)


@pytest.mark.asyncio
async def test_require_seated_player_rejects_spectator_and_outsider() -> None:
    manager = RoomManager(max_private_rooms=0)
    room, player, spectator = await _duel_room(manager)

    assert manager.require_seated_player(room, player.token).token == player.token

    with pytest.raises(PermissionError):
        manager.require_seated_player(room, spectator.token)
    with pytest.raises(PermissionError):
        manager.require_seated_player(room, "")
    with pytest.raises(PermissionError):
        manager.require_seated_player(room, "不存在的令牌")


@pytest.mark.asyncio
async def test_resign_requires_the_seated_player() -> None:
    """以前任何人都能替玩家认输（还会回退到 player_token 的座位）。"""
    manager = RoomManager(max_private_rooms=0)
    room, player, spectator = await _duel_room(manager)
    assert room.status == "active"

    with pytest.raises(PermissionError):
        await manager.resign(room, visitor_token=spectator.token)
    with pytest.raises(PermissionError):
        await manager.resign(room, visitor_token="")
    assert room.status == "active", "越权的认输不能结束对局"

    await manager.resign(room, visitor_token=player.token)
    assert room.status == "finished"


def test_image_payload_accepts_only_real_images() -> None:
    png = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"0" * 64
    ).decode()
    jpeg = "data:image/jpeg;base64," + base64.b64encode(
        b"\xff\xd8\xff\xe0" + b"0" * 64
    ).decode()

    assert GameRoomServer._image_payload({"image": png}) == png
    assert GameRoomServer._image_payload({"image": jpeg}) == jpeg
    # 非图片 / 非 data URL / MIME 与内容不符 / 缺字段，一律丢弃
    assert GameRoomServer._image_payload({"image": "data:text/html;base64,PHNjcmlwdD4="}) == ""
    assert GameRoomServer._image_payload({"image": "data:image/gif;base64,R0lGODlh"}) == ""
    assert GameRoomServer._image_payload(
        {"image": "data:image/png;base64," + base64.b64encode(b"<script>").decode()}
    ) == ""
    assert GameRoomServer._image_payload({"image": base64.b64encode(b"\x89PNG").decode()}) == ""
    assert GameRoomServer._image_payload({"image": None}) == ""
    assert GameRoomServer._image_payload({}) == ""
    # 超限（> 2.5MB 解码后）直接拒绝
    huge = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"0" * (3_600_000)
    ).decode()
    assert GameRoomServer._image_payload({"image": huge}) == ""


@pytest.mark.asyncio
async def test_sweep_survives_one_broken_room() -> None:
    """一个房间的 tick 抛异常不能中断整轮清扫（后面的房间仍要判超时）。"""
    manager = RoomManager(max_private_rooms=0, idle_timeout=60)
    broken = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    healthy = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10002",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10002",
        creator_name="另一个人",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    healthy.created_at = broken.created_at
    healthy.last_activity_at = broken.last_activity_at

    original = manager._tick_multiplayer_room

    async def flaky(room, current):
        if room.room_id == broken.room_id:
            raise RuntimeError("模拟单房间 tick 崩溃")
        return await original(room, current)

    manager._tick_multiplayer_room = flaky  # type: ignore[method-assign]

    expired = await manager.sweep_expired(now=broken.created_at + 120)

    # 坏房间本轮被跳过（保留，下轮再试），健康房间照常判超时销毁——
    # 关键是整轮清扫没有因为异常中断
    assert healthy.room_id in expired
    assert healthy.room_id not in manager.rooms
    assert broken.room_id in manager.rooms


@pytest.mark.asyncio
async def test_visitor_uid_is_stable_and_never_empty_for_humans() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:10001",
        platform="aiocqhttp",
        group_id="10001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    snapshot = room.public_snapshot(first.token)
    assert snapshot["visitor_uid"]
    assert snapshot["visitor_uid"] == next(
        item["uid"] for item in snapshot["visitors"] if item["number"] == first.number
    )
    # 同一个 token 多次取快照必须给出同一个 uid（前端靠它做名册差量）
    again = room.public_snapshot(first.token)
    assert again["visitor_uid"] == snapshot["visitor_uid"]


def test_watchdog_keeps_running_after_one_failed_sweep() -> None:
    """看门狗的 try 必须在 while 里，否则一次异常就永久停摆。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / "main.py").read_text(encoding="utf-8")
    start = source.index("async def _watchdog(")
    body = source[start : start + 1200]
    loop = body.index("while True:")
    guard = body.index("try:")
    assert loop < guard, "try 必须在 while 循环体内"
    assert "except Exception" in body[: body.index("def _schedule_tunnel_recovery")]


def test_sweep_isolates_room_failures() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = (root / "room_manager.py").read_text(encoding="utf-8")
    start = source.index("async def sweep_expired(")
    body = source[start : source.index("async def _sweep_room(")]
    assert "_sweep_room(room, current)" in body
    assert "except Exception" in body
