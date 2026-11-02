"""一致性回归：播报幂等/失败回填、清扫规则、报表时间窗口、悔棋与恢复。"""

from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def _plugin(manager: RoomManager) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager  # type: ignore[attr-defined]
    return plugin


async def _duel_room(manager: RoomManager, game_type: str = "gomoku"):
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:1",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10001")
    await manager.add_ai_seat(room, visitor.token, side="black")
    await manager.wait_bot_turns()
    return room


@pytest.mark.asyncio
async def test_auto_announce_skips_game_already_announced_manually() -> None:
    """手动点过「通知到群」之后，自动播报不能把同一局再发一遍。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, 1)] * 12
    room.game.winner = 2
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _plugin(manager)
    plugin.board_group_announce_enabled = True
    plugin.board_group_announce_style = "text"
    plugin.board_group_announce_recap = False
    plugin.board_group_announce_min_moves = 0
    plugin.board_group_announce_cooldown = 0
    plugin.board_group_announce_series_idle = 0
    sent: list[str] = []

    async def fake_announce(r, image_data=""):
        sent.append("SENT")
        return "TEXT"

    plugin.board_announce_result = fake_announce  # type: ignore[attr-defined]
    room.last_announced_uid = f"board:{int(room.completed_games)}"

    await plugin._auto_announce_board(room)

    assert sent == []


@pytest.mark.asyncio
async def test_send_failure_does_not_mark_as_announced() -> None:
    """群发失败（被踢/禁言）不能写幂等标记，否则这一局之后再也播不出去。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, 1)] * 12
    room.game.winner = 2
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _plugin(manager)
    plugin.board_group_announce_enabled = True
    plugin.board_group_announce_style = "text"
    plugin.board_group_announce_recap = False

    async def failing_send(r, text):
        return False

    plugin._send_to_origin = failing_send  # type: ignore[attr-defined]

    text = await plugin.board_announce_result(room)

    assert text == ""
    assert room.last_announced_uid != f"board:{int(room.completed_games)}"


@pytest.mark.asyncio
async def test_series_requeues_entries_when_group_send_fails() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    plugin = _plugin(manager)
    plugin.board_group_announce_recap = False

    async def failing_send(r, text):
        return False

    plugin._send_to_origin = failing_send  # type: ignore[attr-defined]
    plugin._queue_board_announce(
        room,
        {
            "game_type": "gomoku",
            "game_uid": "g1",
            "completed_games": 1,
            "moves": 12,
            "draw": False,
            "winner_name": "小明",
            "winner_side": "black",
            "winner_is_ai": False,
            "human_won": True,
        },
    )

    await plugin._flush_board_announces(room, 0.0)

    # 没有发出去 → 条目必须回到队列里，不能凭空消失
    assert len(room.pending_board_announces) == 1
    assert room.pending_board_announces[0]["game_uid"] == "g1"


@pytest.mark.asyncio
async def test_series_keeps_entries_when_task_is_cancelled() -> None:
    import asyncio

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    plugin = _plugin(manager)
    plugin.board_group_announce_recap = False
    plugin._queue_board_announce(
        room,
        {
            "game_type": "gomoku",
            "game_uid": "g2",
            "completed_games": 3,
            "moves": 20,
            "draw": False,
            "winner_name": "花火",
            "winner_side": "white",
            "winner_is_ai": True,
            "human_won": False,
        },
    )

    task = asyncio.create_task(plugin._flush_board_announces(room, 30.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert len(room.pending_board_announces) == 1


@pytest.mark.asyncio
async def test_idle_sweep_keeps_active_room_while_player_is_present() -> None:
    """玩家还连着（心跳新鲜）时，长时间没操作也不能把正在对局的房间销毁。"""
    manager = RoomManager(max_private_rooms=0, idle_timeout=300)
    room = await _duel_room(manager)
    assert room.status == "active"
    now = time.time()
    for visitor in room.visitors.values():
        visitor.connected = True
        visitor.last_seen_at = now
        visitor.left_at = None
    room.last_activity_at = now - 1000

    assert await manager.sweep_expired(now=now) == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_idle_sweep_still_cleans_abandoned_active_room() -> None:
    """但一个人都不在线时，闲置的房间仍要被回收（否则永久占配额）。"""
    manager = RoomManager(max_private_rooms=0, idle_timeout=300)
    room = await _duel_room(manager)
    now = time.time()
    for visitor in room.visitors.values():
        visitor.connected = False
        visitor.last_seen_at = now - 1000
    room.last_activity_at = now - 1000

    expired = await manager.sweep_expired(now=now)

    assert room.room_id in expired


@pytest.mark.asyncio
async def test_turtle_soup_preparing_still_expires_by_idle() -> None:
    """出题阶段只豁免「玩家席无人」，不能连 idle 一起豁免——否则房间永远不回收。"""
    from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame

    manager = RoomManager(max_private_rooms=0, idle_timeout=120)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:1",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    room.game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    room.game.phase = "preparing"
    room.last_activity_at = 100.0

    expired = await manager.sweep_expired(now=100.0 + 121)

    assert room.room_id in expired


def test_leaderboard_supports_time_window() -> None:
    """日报/周报按天分桶统计，而不是都读同一份累计榜。"""
    manager = RoomManager(max_private_rooms=0)
    now = time.time()
    bucket = manager.global_player_wins.setdefault("gomoku", {})
    entry = bucket.setdefault("10001", {"name": "小明", "wins": 0, "games": 0})
    entry["wins"] = 9
    entry["games"] = 10
    manager._bump_daily(entry, wins=1, games=1, now=now)
    old = time.time() - 30 * 86400
    manager._bump_daily(entry, wins=8, games=9, now=old)

    cumulative = manager.global_leaderboard("gomoku")
    windowed = manager.global_leaderboard("gomoku", days=1, now=now)

    assert cumulative[0]["wins"] == 9
    assert windowed[0]["wins"] == 1
    assert windowed[0]["games"] == 1


def test_daily_buckets_are_pruned() -> None:
    manager = RoomManager(max_private_rooms=0)
    entry: dict[str, object] = {}
    now = time.time()
    for day in range(70):
        manager._bump_daily(entry, wins=1, now=now - day * 86400)

    assert len(entry["daily"]) <= 60


@pytest.mark.asyncio
async def test_ui_undo_defaults_to_reject_when_model_output_is_unusable() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    plugin = _plugin(manager)

    async def garbage(*args, **kwargs):
        return "抱歉我现在说不出 JSON"

    plugin._generate_persona_text = garbage  # type: ignore[attr-defined]

    accepted, reply = await plugin._decide_ui_undo(room, room.player)
    assert accepted is False
    assert reply


@pytest.mark.asyncio
async def test_resume_two_humans_does_not_play_for_the_opponent() -> None:
    """双真人局恢复时不能把「对手那一手」当花火的回合自动落子。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:1",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "human_black")
    if room.status != "active":
        await manager.start_game(room, first.token, "human_black")
    assert not any(seat.is_ai for seat in room.multiplayer.seats)
    moves_before = len(room.game.history)

    await manager.pause(room)
    await manager.resume(room)
    await manager.wait_bot_turns()

    assert len(room.game.history) == moves_before


@pytest.mark.asyncio
async def test_destroy_releases_room_level_caches() -> None:
    """房间销毁后要放掉房间级缓存，避免长期运行按「开过多少房」线性增长。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.recap_cache["g1"] = "很长的复盘文本"
    room.pending_board_announces = [{"game_uid": "g1", "completed_games": 1}]
    room.last_board_result = {"moves": 12}

    await manager.destroy(room.room_id, "测试销毁")

    assert room.recap_cache == {}
    assert room.pending_board_announces == []
    assert room.last_board_result is None
    assert room.room_id not in manager.rooms


@pytest.mark.asyncio
async def test_idle_grace_is_bounded_so_quota_is_released() -> None:
    """有人守着可以宽限，但不能永久宽限：群聊只有 1 个房间配额。

    一局结束后把结算页挂着不管，以前（宽限期无限）会让同一个群的第二个人
    开新房时报「群聊房间已达到并行上限」。
    """
    manager = RoomManager(
        max_group_rooms=1, max_private_rooms=0, idle_timeout=300, empty_player_timeout=60
    )
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:1",
        platform="aiocqhttp",
        group_id="1",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    await manager.add_ai_seat(room, player.token, side="white")
    await manager.wait_bot_turns()
    room.game.winner = room.game.bot_color
    await manager._finish_game(room)

    now = time.time()
    player.connected = True
    player.last_seen_at = now
    room.last_activity_at = now - 360  # 6 分钟：超过 idle 但在宽限内

    assert await manager.sweep_expired(now=now) == []
    assert room.room_id in manager.rooms

    # 挂一个小时后必须回收，否则群聊房间配额永远被占
    room.last_activity_at = now - 3600
    player.last_seen_at = now
    expired = await manager.sweep_expired(now=now)

    assert room.room_id in expired
    assert room.room_id not in manager.rooms
