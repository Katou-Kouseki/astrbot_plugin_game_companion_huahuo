from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.trusted_identity import TrustedIdentity

ADMIN_SESSION = "admin:console"


def _create(manager: RoomManager, *, bypass: bool, game_type: str = "gomoku"):
    return manager.create_room(
        source="private",
        session_id=ADMIN_SESSION,
        platform="admin",
        group_id="",
        creator_qq="",
        creator_name="游戏管理台",
        admin_room=True,
        game_type=game_type,  # type: ignore[arg-type]
        difficulty="normal",
        bypass_limit=bypass,
    )


@pytest.mark.asyncio
async def test_console_room_bypasses_private_quota() -> None:
    """管理台开房不受私聊并行上限约束，普通创建仍然受限。"""
    manager = RoomManager(max_private_rooms=1)

    first = await _create(manager, bypass=True)
    assert first.admin_room is True
    assert first.session_id == ADMIN_SESSION

    # 上限已经用满：走普通路径必须被拒
    with pytest.raises(ValueError, match="并行上限"):
        await _create(manager, bypass=False)

    # 管理台再开一个仍然成功
    second = await _create(manager, bypass=True, game_type="weiqi")
    assert second.room_id != first.room_id
    assert second.game_type == "weiqi"
    assert len(manager.rooms) == 2


@pytest.mark.asyncio
async def test_console_room_still_respects_disabled_games() -> None:
    """开房绕过的是配额，不是「玩法开关」：关掉的玩法依旧开不出来。"""
    manager = RoomManager(max_private_rooms=1)
    manager.enabled_games["weiqi"] = False

    with pytest.raises((ValueError, PermissionError)):
        await _create(manager, bypass=True, game_type="weiqi")
    assert not manager.rooms


@pytest.mark.asyncio
async def test_console_room_lets_confirmed_visitors_seat_themselves() -> None:
    """管理台房间：已带身份的访客自己入座，匿名访客仍然要管理员安排。"""
    manager = RoomManager(max_private_rooms=1)
    room = await _create(manager, bypass=True, game_type="gomoku")

    anonymous = await manager.join(room, "")
    with pytest.raises(ValueError, match="需要管理员"):
        await manager.claim_and_start(room, anonymous.token, "human_black")
    assert room.multiplayer.seats == []

    bound = await manager.join(
        room,
        "",
        trusted_identity=TrustedIdentity(
            qq="10001", display_name="小明", expires_at=10**12
        ),
    )
    assert bound.identity_confirmed is True
    await manager.claim_and_start(room, bound.token, "human_black")
    assert [seat.visitor_token for seat in room.multiplayer.seats] == [bound.token]
    assert room.multiplayer.seats[0].identity_confirmed is True
    assert room.multiplayer.seats[0].qq == "10001"


@pytest.mark.asyncio
async def test_console_room_survives_waiting_for_visitor() -> None:
    """管理台「链接房间」在访客到来前不被空房/空闲超时秒杀，链接保持可用。

    管理台开房后管理员要把网址发给朋友，对方可能过很久才点开；
    若仍套用 empty_player_timeout / idle_timeout 自动销毁，就会变成「链接进不去」。
    """
    manager = RoomManager(
        max_private_rooms=1, empty_player_timeout=60, idle_timeout=300
    )
    room = await _create(manager, bypass=True, game_type="gomoku")
    # 新建的管理台房间还没有玩家入座，不应一开始就进入「玩家席无人」倒计时
    assert room.player_empty_since is None
    # 超过空房超时（60s）后仍存活
    assert await manager.sweep_expired(now=room.created_at + 61) == []
    assert room.room_id in manager.rooms
    # 超过空闲超时（300s）后仍存活：管理台房间只由管理员显式关闭
    assert await manager.sweep_expired(now=room.created_at + 301) == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_console_assign_dialog_does_not_lock_up_without_visitors() -> None:
    """「安排玩家」弹窗里的下拉不能带 required：空列表时会把关闭按钮一起锁死。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    page = (root / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")
    script = (root / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert '<select id="assignVisitor" required>' not in page
    assert '<select id="assignVisitor"></select>' in page
    assert "房间序号" not in page
    assert "选择访客" in page
    # 没有访客时直接提示，不弹空弹窗
    assert "房间里还没有访客" in script
    assert "assign.disabled = !hasAssignable" in script
