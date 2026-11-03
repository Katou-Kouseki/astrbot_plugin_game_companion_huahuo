from __future__ import annotations

import asyncio
import time

import pytest
from astrbot_plugin_game_companion_huahuo.blackjack import BlackjackCard, BlackjackGame
from astrbot_plugin_game_companion_huahuo.models import _visitor_uid
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.undercover import UndercoverGame


async def create_room(
    manager: RoomManager, *, source: str = "group", creator: str = "10001"
):
    return await manager.create_room(
        source=source,
        session_id=f"aiocqhttp:{source}:session",
        platform="aiocqhttp",
        group_id="20001" if source == "group" else "",
        creator_qq=creator,
        creator_name="创建者",
        admin_room=False,
        difficulty="normal",
    )


def fixed_blackjack_shoe(ranks: list[str]) -> list[BlackjackCard]:
    """Return a shoe dealt in the given rank order, oldest card first."""
    suits = ("♠", "♥", "♦", "♣")
    return [
        BlackjackCard(rank=rank, suit=suits[index % 4])
        for index, rank in enumerate(reversed(ranks))
    ]


async def create_blackjack_room(manager: RoomManager):
    return await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="blackjack",
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_source_wide_quota_is_atomic() -> None:
    manager = RoomManager(max_group_rooms=1, max_private_rooms=1)

    results = await asyncio.gather(
        create_room(manager, creator="10001"),
        create_room(manager, creator="10002"),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(manager.rooms) == 1


@pytest.mark.asyncio
async def test_zero_quota_means_unlimited() -> None:
    manager = RoomManager(max_group_rooms=0)

    for index in range(6):
        await create_room(manager, creator=str(10000 + index))

    assert len(manager.rooms) == 6


@pytest.mark.asyncio
async def test_disabled_games_reject_new_rooms_switches_and_rematches() -> None:
    manager = RoomManager(enabled_games={"xiangqi": False})

    with pytest.raises(PermissionError, match="暂未开放中国象棋"):
        await manager.create_room(
            source="private",
            session_id="aiocqhttp:private:10001",
            platform="aiocqhttp",
            group_id="",
            creator_qq="10001",
            creator_name="创建者",
            admin_room=False,
            game_type="xiangqi",
            difficulty="normal",
        )

    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    with pytest.raises(PermissionError, match="暂未开放中国象棋"):
        await manager.switch_game(room, "xiangqi", force=True)

    room.status = "finished"
    manager.enabled_games["gomoku"] = False
    with pytest.raises(PermissionError, match="暂未开放五子棋"):
        await manager.request_rematch(room, visitor.token)


@pytest.mark.asyncio
async def test_disabling_game_does_not_interrupt_an_active_round() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)

    manager.enabled_games["gomoku"] = False
    await manager.pause(room)
    await manager.resume(room)

    assert room.status == "active"
    assert room.game is not None


@pytest.mark.asyncio
async def test_disabling_game_prevents_a_waiting_room_from_starting() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)

    manager.enabled_games["gomoku"] = False
    with pytest.raises(PermissionError, match="暂未开放五子棋"):
        await manager.claim_and_start(room, visitor.token, "human_black")

    assert room.player_token == ""
    assert room.game is None
    assert room.status == "waiting"


@pytest.mark.asyncio
async def test_pig_dice_uses_configured_target_score_for_new_rounds() -> None:
    manager = RoomManager(pig_dice_target_score=80)
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="pig_dice",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "")

    assert room.game is not None
    assert room.game.target_score == 80


@pytest.mark.asyncio
async def test_visitors_receive_stable_non_reused_numbers() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)

    resumed = await manager.join(room, first.token)
    await manager.kick_visitor(room, second.number)
    third = await manager.join(room)

    assert resumed.number == 1
    assert third.number == 3


@pytest.mark.asyncio
async def test_admin_room_requires_dashboard_assignment() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    room.admin_room = True
    visitor = await manager.join(room)

    with pytest.raises(ValueError, match="管理员"):
        await manager.claim_and_start(room, visitor.token, "human_black")

    await manager.assign_player(room, visitor.number, "12345678")

    assert room.player_token == visitor.token
    assert room.player_qq == "12345678"
    assert room.player_identity_confirmed
    assert room.status == "setup"


@pytest.mark.asyncio
async def test_mobile_assignment_accepts_authenticated_non_qq_identity() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private", creator="mobile-owner")
    visitor = await manager.join(room)

    await manager.assign_player(
        room,
        visitor.number,
        "mobile-owner",
        allow_non_numeric=True,
    )

    assert room.player_token == visitor.token
    assert room.player_qq == "mobile-owner"
    assert visitor.identity_confirmed is True


@pytest.mark.asyncio
async def test_creator_correction_swaps_seat_and_resets_game() -> None:
    manager = RoomManager()
    room = await create_room(manager, creator="10001")
    thief = await manager.join(room)
    creator = await manager.join(room)
    await manager.claim_and_start(room, thief.token, "human_black")
    await manager.add_ai_seat(room, thief.token)
    assert room.game is not None

    await manager.correct_creator(room, "10001", creator.number)

    assert room.player_token == creator.token
    assert room.player_qq == "10001"
    assert room.player_identity_confirmed
    assert room.player_seat_locked
    assert room.game is None
    assert room.status == "setup"
    assert thief.token in room.visitors


@pytest.mark.asyncio
async def test_identity_confirmation_emits_only_after_real_qq_confirmation() -> None:
    events: list[str] = []

    async def callback(event: str, _room, _payload) -> None:
        events.append(event)

    manager = RoomManager(event_callback=callback)
    room = await create_room(manager, creator="10001")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")

    assert "player_confirmed" not in events

    await manager.confirm_creator(room, "10001")

    assert events[-1] == "player_confirmed"


@pytest.mark.asyncio
async def test_rematch_reuses_room_player_identity_score_and_rotates_side() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private", creator="10001")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)
    await manager.confirm_creator(room, "10001")
    original_room_id = room.room_id
    original_access_token = room.access_token
    original_player_token = room.player_token
    original_human_color = room.game.human_color
    room.status = "finished"
    room.completed_games = 2
    room.human_wins = 1
    room.bot_wins = 1

    await manager.restart_finished_game(room, difficulty="hard")
    await manager.wait_bot_turns()

    assert room.room_id == original_room_id
    assert room.access_token == original_access_token
    assert room.player_token == original_player_token
    assert room.player_identity_confirmed
    assert room.completed_games == 2
    assert room.human_wins == 1
    assert room.bot_wins == 1
    assert room.status == "active"
    assert room.difficulty == "hard"
    assert room.game is not None
    # 两人对弈再来一局会换先，玩家执色与上一局相反
    assert room.game.human_color != original_human_color
    # 玩家换到执白后，执黑的花火先落第一手
    assert room.game.history == [(7, 7, 1)]


@pytest.mark.asyncio
async def test_rematch_does_not_reset_an_unfinished_game() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)
    game = room.game

    with pytest.raises(ValueError, match="尚未结束"):
        await manager.restart_finished_game(room, difficulty="easy")

    assert room.game is game
    assert room.status == "active"


@pytest.mark.asyncio
async def test_stale_web_rematch_decision_cannot_override_a_qq_restart() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token)
    room.status = "rematch_pending"

    await manager.restart_finished_game(room, difficulty="hard")
    active_game = room.game
    applied = await manager.resolve_rematch(
        room,
        accepted=True,
        message="迟到的网页决议",
        difficulty="easy",
    )

    assert not applied
    assert room.status == "active"
    assert room.game is active_game
    assert room.difficulty == "hard"
    assert all(
        message["content"] != "迟到的网页决议" for message in room.messages
    )


@pytest.mark.asyncio
async def test_non_creator_cannot_correct_identity() -> None:
    manager = RoomManager()
    room = await create_room(manager, creator="10001")
    visitor = await manager.join(room)

    with pytest.raises(PermissionError):
        await manager.correct_creator(room, "99999", visitor.number)


@pytest.mark.asyncio
async def test_new_room_waits_for_first_player_without_empty_sweep() -> None:
    """新建房间（还没有玩家）不应被 empty_player_timeout 秒杀，只受 idle_timeout 兜底。

    否则 LLM 开房 / 管理台开房后，访客还没点开链接房间就被销毁（「管理台没显示」「链接进不去」）。
    """
    manager = RoomManager(empty_player_timeout=60, idle_timeout=300)
    room = await create_room(manager, source="private")
    assert room.player_empty_since is None
    # 超过空房超时（60s）仍存活：还没人入座，不该进入「玩家席无人」倒计时
    assert await manager.sweep_expired(now=room.created_at + 61) == []
    assert room.room_id in manager.rooms
    # 超过空闲超时（300s）才被兜底清理
    assert await manager.sweep_expired(now=room.created_at + 301) == [room.room_id]


@pytest.mark.asyncio
async def test_empty_sweep_still_fires_after_player_leaves() -> None:
    """玩家离席后，只要还有真人访客在线就不销毁；访客也离开后才由 empty_player_timeout 清理。

    一局结束后玩家「退出玩家席」仍留在房间当观众，页面持续心跳，此时不应被当作空房秒杀。
    """
    manager = RoomManager(empty_player_timeout=60, idle_timeout=0)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10001")
    assert room.player_empty_since is None
    await manager.remove_player(room, visitor.number)
    assert room.player_empty_since is not None
    # 访客仍在线（只是退出玩家席当观众），不销毁
    assert await manager.sweep_expired(now=room.player_empty_since + 61) == []
    assert room.room_id in manager.rooms
    # 访客也离开后，空房超时生效
    await manager.leave(room, visitor.token)
    assert await manager.sweep_expired(now=room.player_empty_since + 61) == [room.room_id]


@pytest.mark.asyncio
async def test_heartbeat_does_not_refresh_meaningful_activity() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=10)
    room = await create_room(manager)
    visitor = await manager.join(room)
    room.last_activity_at = 100.0

    await manager.heartbeat(room, visitor.token)

    assert room.last_activity_at == 100.0


@pytest.mark.asyncio
async def test_finished_room_expires_shortly_after_player_leaves() -> None:
    manager = RoomManager(empty_player_timeout=60, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    # 真实 leave 会把 connected 置为 False（玩家已退出房间）
    visitor.connected = False
    visitor.left_at = 100.0
    visitor.last_seen_at = 100.0

    assert await manager.sweep_expired(now=107.9) == []
    assert await manager.sweep_expired(now=108.0) == [room.room_id]
    assert manager.closed_reason_by_access_token(room.access_token) == (
        "本局结束后玩家已离开，房间已自动销毁"
    )


@pytest.mark.asyncio
async def test_finished_room_not_fast_recycled_when_empty_timeout_disabled() -> None:
    """「玩家席无人自动销毁秒数」设为 0 时，本局结束后玩家离开不再被 8 秒快速回收，
    只由「房间无有效操作」超时兜底销毁（LLM 开房后玩家走光也不该被秒杀）。"""
    manager = RoomManager(empty_player_timeout=0, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    room.last_activity_at = 100.0
    visitor.connected = False
    visitor.left_at = 100.0
    visitor.last_seen_at = 100.0

    # 超过 8 秒离开宽限，房间也不应被「人去房空」快速回收
    assert await manager.sweep_expired(now=108.0) == []
    assert room.room_id in manager.rooms
    # 直到「房间无有效操作」超时（300s）才被兜底销毁
    assert await manager.sweep_expired(now=399.9) == []
    assert await manager.sweep_expired(now=400.0) == [room.room_id]


@pytest.mark.asyncio
async def test_finished_room_survives_while_spectator_is_present() -> None:
    """一局结束后玩家离开，但只要还有观众在线，房间就不应被自动销毁。"""
    manager = RoomManager(empty_player_timeout=60, idle_timeout=300)
    room = await create_room(manager, source="private")
    player = await manager.join(room)
    spectator = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    room.status = "finished"

    # 玩家离开（connected=False），观众仍在线（connected=True、心跳新鲜）
    player.connected = False
    player.left_at = 100.0
    player.last_seen_at = 100.0
    spectator.connected = True
    spectator.last_seen_at = 200.0

    # 玩家早已超过离开宽限，但观众还在 → 不销毁
    assert await manager.sweep_expired(now=200.0) == []
    assert room.room_id in manager.rooms

    # 观众也离开后 → 房间才销毁
    spectator.connected = False
    spectator.left_at = 200.0
    spectator.last_seen_at = 200.0
    assert await manager.sweep_expired(now=208.0) == [room.room_id]


@pytest.mark.asyncio
async def test_finished_room_survives_when_both_timeouts_disabled() -> None:
    """两个自动销毁配置都设为 0 时，finished 房间也不得自动回收。"""
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager, source="private")
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    room.status = "finished"
    # 玩家离开且房间无任何在线访客
    player.connected = False
    player.left_at = 100.0
    player.last_seen_at = 100.0

    assert await manager.sweep_expired(now=1000.0) == []
    assert await manager.sweep_expired(now=86400.0) == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_rejoin_cancels_finished_room_leave_expiry() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"

    await manager.leave(room, visitor.token)
    assert visitor.left_at is not None
    await manager.join(room, visitor.token)

    assert visitor.left_at is None
    assert visitor.connected
    assert await manager.sweep_expired(now=visitor.last_seen_at + 8) == []


@pytest.mark.asyncio
async def test_active_game_is_not_destroyed_by_browser_leave_grace() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    visitor.left_at = 100.0
    visitor.last_seen_at = 100.0
    room.last_activity_at = 100.0

    assert await manager.sweep_expired(now=108.0) == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_finished_room_uses_heartbeat_loss_when_leave_signal_is_missing() -> None:
    manager = RoomManager(empty_player_timeout=60, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    visitor.left_at = None
    visitor.last_seen_at = 100.0
    # 崩溃/断网：connected 仍为 True，只能靠心跳超时判定「已离开」。
    # 观众在线判定（_has_present_visitors）用 90 秒心跳阈值，因此这里要到
    # last_seen_at + 90s 才真正销毁（比 finished 的 60s 判定多留 30s 缓冲）。

    assert await manager.sweep_expired(now=159.9) == []
    assert await manager.sweep_expired(now=189.9) == []
    assert await manager.sweep_expired(now=190.0) == [room.room_id]


@pytest.mark.asyncio
async def test_empty_and_idle_zero_disable_expiry() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager)
    room.created_at = room.last_activity_at = 1.0
    room.player_empty_since = 1.0

    expired = await manager.sweep_expired(now=10000.0)

    assert expired == []
    assert room.room_id in manager.rooms


@pytest.mark.asyncio
async def test_empty_room_expires_and_releases_quota() -> None:
    manager = RoomManager(max_group_rooms=1, empty_player_timeout=60, idle_timeout=0)
    room = await create_room(manager)
    room.player_empty_since = 10.0

    assert await manager.sweep_expired(now=70.0) == [room.room_id]
    assert not manager.rooms
    await create_room(manager, creator="10002")


@pytest.mark.asyncio
async def test_destroyed_room_keeps_only_a_short_lived_close_reason() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    access_token = room.access_token

    await manager.destroy(room.room_id, "玩家席长时间无人，房间已自动销毁")

    assert manager.by_access_token(access_token) is None
    assert manager.closed_reason_by_access_token(access_token) == (
        "玩家席长时间无人，房间已自动销毁"
    )
    assert room.room_id not in manager.rooms
    assert access_token not in manager._access_index

    reason, _expires_at = manager._closed_access[access_token]
    manager._closed_access[access_token] = (reason, 0.0)
    assert manager.closed_reason_by_access_token(access_token) == ""


@pytest.mark.asyncio
async def test_admin_snapshot_omits_board_and_access_tokens() -> None:
    manager = RoomManager()
    room = await create_room(manager)
    visitor = await manager.join(room)
    snapshot = room.admin_snapshot()

    assert "game" not in snapshot
    assert "access_token" not in snapshot
    assert "token" not in snapshot["visitors"][0]
    assert visitor.number == snapshot["visitors"][0]["number"]


@pytest.mark.asyncio
async def test_solo_blackjack_deals_one_hand_automatically(monkeypatch) -> None:
    manager = RoomManager(blackjack_max_players=1)
    room = await create_blackjack_room(manager)
    visitor = await manager.join(room)

    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, player_uids=None, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            player_uids=player_uids,
            shoe=fixed_blackjack_shoe(["10", "6", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "active"
    assert isinstance(room.game, BlackjackGame)
    assert room.game.phase == "player_turns"
    assert room.game.hand_for_uid(_visitor_uid(visitor.token)) is not None
    assert room.multiplayer.enabled is True
    assert room.multiplayer.capacity == 1
    assert room.multiplayer.turn_deadline > 0


@pytest.mark.asyncio
async def test_multiplayer_blackjack_waits_for_explicit_deal(monkeypatch) -> None:
    manager = RoomManager(blackjack_max_players=2)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")

    assert room.status == "setup"
    assert room.game is None

    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    assert len(room.multiplayer.seats) == 2

    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, player_uids=None, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            player_uids=player_uids,
            shoe=fixed_blackjack_shoe(["10", "6", "10", "8", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert isinstance(room.game, BlackjackGame)
    assert room.game.hand_for_uid(_visitor_uid(first.token)) is not None and room.game.hand_for_uid(_visitor_uid(second.token)) is not None
    assert room.status == "active"


@pytest.mark.asyncio
async def test_blackjack_actions_rotate_turns_and_settle_hands() -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number],
        player_uids=[_visitor_uid(first.token), _visitor_uid(second.token)],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    manager._reset_turn_deadline(room)

    assert room.multiplayer.current_token == first.token
    await manager.player_blackjack_action(room, first.token, "stand")
    assert room.multiplayer.current_token == second.token
    await manager.player_blackjack_action(room, second.token, "stand")
    await manager.wait_bot_turns()

    assert room.status == "finished"
    assert room.game.results_by_number() == {first.number: "win", second.number: "loss"}
    assert room.human_wins == 1
    assert room.bot_wins == 1
    assert room.completed_games == 1


@pytest.mark.asyncio
async def test_blackjack_turn_timeout_auto_stands_current_hand() -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=10)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number],
        player_uids=[_visitor_uid(first.token), _visitor_uid(second.token)],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    first.last_seen_at = 1000.0
    second.last_seen_at = 1000.0
    manager._reset_turn_deadline(room, now=1000.0)
    room.multiplayer.turn_deadline = 990.0

    await manager.sweep_expired(now=1000.0)

    assert room.game.hand_for_number(first.number).status == "stand"
    assert room.multiplayer.current_token == second.token


@pytest.mark.asyncio
async def test_blackjack_rejects_seat_join_during_an_active_round() -> None:
    manager = RoomManager(blackjack_max_players=2)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal", player_numbers=[first.number], player_uids=[_visitor_uid(first.token)], shoe=fixed_blackjack_shoe(["10", "10", "9", "8"])
    )
    room.status = "active"
    latecomer = await manager.join(room)

    with pytest.raises(ValueError, match="本局进行中"):
        await manager.claim_and_start(room, latecomer.token, "")


@pytest.mark.asyncio
async def test_blackjack_deadline_is_set_only_while_hands_are_pending() -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=30)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number],
        player_uids=[_visitor_uid(first.token), _visitor_uid(second.token)],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"

    manager._reset_turn_deadline(room, now=1000.0)
    assert room.multiplayer.turn_deadline == 1030.0

    room.game.stand(first.number)
    room.game.stand(second.number)
    manager._reset_turn_deadline(room, now=1000.0)
    assert room.multiplayer.turn_deadline == 0.0


@pytest.mark.asyncio
async def test_blackjack_deal_skips_natural_blackjack_hands(monkeypatch) -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")

    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, player_uids=None, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            player_uids=player_uids,
            shoe=fixed_blackjack_shoe(["A", "K", "10", "6", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert room.game.hand_for_number(first.number).blackjack is True
    assert room.multiplayer.current_token == second.token
    assert room.multiplayer.turn_deadline > 0


@pytest.mark.asyncio
async def test_blackjack_all_natural_hands_go_straight_to_settlement(monkeypatch) -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")

    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, player_uids=None, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            player_uids=player_uids,
            shoe=fixed_blackjack_shoe(["A", "K", "A", "Q", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")
    await manager.wait_bot_turns()

    assert room.status == "finished"
    assert room.game.results_by_number() == {
        first.number: "blackjack_win",
        second.number: "blackjack_win",
    }


@pytest.mark.asyncio
async def test_blackjack_offline_hand_is_auto_stood_and_turn_advances() -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number],
        player_uids=[_visitor_uid(first.token), _visitor_uid(second.token)],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    first.connected = False
    first.last_seen_at = 1000.0
    second.last_seen_at = 1000.0
    manager._reset_turn_deadline(room, now=1000.0)

    await manager.sweep_expired(now=1000.0)

    assert room.game.hand_for_number(first.number).status == "stand"
    assert room.multiplayer.current_token == second.token


@pytest.mark.asyncio
async def test_blackjack_first_seat_acts_first_on_a_normal_deal(monkeypatch) -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, player_uids=None, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            player_uids=player_uids,
            shoe=fixed_blackjack_shoe(["10", "6", "10", "8", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert room.game.hand_for_number(first.number).blackjack is False
    assert room.multiplayer.current_token == first.token
    assert room.multiplayer.turn_deadline > 0


@pytest.mark.asyncio
async def test_blackjack_action_resolves_at_once_when_remaining_players_are_offline() -> None:
    manager = RoomManager(blackjack_max_players=2, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.claim_and_start(room, second.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number],
        player_uids=[_visitor_uid(first.token), _visitor_uid(second.token)],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    second.connected = False
    manager._reset_turn_deadline(room)

    await manager.player_blackjack_action(room, first.token, "stand")
    await manager.wait_bot_turns()

    assert room.status == "finished"
    assert room.game.hand_for_number(second.number).status == "stand"
    assert room.game.results_by_number() == {first.number: "win", second.number: "loss"}


@pytest.mark.asyncio
async def test_board_game_leaderboard_credits_huahuo_with_winrate() -> None:
    """棋类全局榜包含花火，并返回局数与胜率。"""
    manager = RoomManager(max_private_rooms=0)
    room = await create_room(manager, source="private", creator="10001")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.assign_player(room, visitor.number, "10001")
    await manager.add_ai_seat(room, visitor.token, side="black")

    # 花火赢一局：花火 +1 胜 +1 局，玩家 +1 局（不计胜）
    manager._credit_board_game_global(room, winner_tokens=set(), ai_won=True)
    rows = {row["name"]: row for row in manager.global_leaderboard("gomoku")}
    assert rows["花火"]["wins"] == 1
    assert rows["花火"]["games"] == 1
    assert rows["花火"]["winrate"] == 100.0
    assert rows["创建者"]["wins"] == 0
    assert rows["创建者"]["games"] == 1
    assert rows["创建者"]["winrate"] == 0.0

    # 玩家赢一局：双方局数都到 2，胜率各 50%
    manager._credit_board_game_global(
        room, winner_tokens={visitor.token}, ai_won=False
    )
    rows = {row["name"]: row for row in manager.global_leaderboard("gomoku")}
    assert rows["创建者"]["wins"] == 1
    assert rows["创建者"]["games"] == 2
    assert rows["创建者"]["winrate"] == 50.0
    assert rows["花火"]["wins"] == 1
    assert rows["花火"]["games"] == 2
    assert rows["花火"]["winrate"] == 50.0


@pytest.mark.asyncio
async def test_player_can_pick_huahuo_difficulty_when_playing_ai() -> None:
    """玩家在玩家席且对手是花火时，可以自选棋力，并对当前对局立即生效。"""
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager, source="private")
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    await manager.add_ai_seat(room, player.token, side="black")

    await manager.player_set_difficulty(room, player.token, "easy")

    assert room.difficulty == "easy"
    assert room.game.difficulty == "easy"
    # 玩家手动选过之后，再战不再让模型把难度改回去
    assert room.difficulty_chosen_by_player is True


@pytest.mark.asyncio
async def test_player_cannot_pick_difficulty_against_human() -> None:
    """两位真人对局没有花火可调，改档应被拒绝。"""
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager, source="private")
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.claim_and_start(room, first.token, "human_black")
    await manager.claim_and_start(room, second.token, "human_white")

    with pytest.raises(PermissionError):
        await manager.player_set_difficulty(room, first.token, "hard")
    assert room.difficulty == "normal"


@pytest.mark.asyncio
async def test_spectator_cannot_pick_difficulty() -> None:
    """没落座的观众不能调花火棋力。"""
    manager = RoomManager(empty_player_timeout=0, idle_timeout=0)
    room = await create_room(manager, source="private")
    player = await manager.join(room)
    spectator = await manager.join(room)
    await manager.claim_and_start(room, player.token, "human_black")
    await manager.add_ai_seat(room, player.token, side="black")

    with pytest.raises(PermissionError):
        await manager.player_set_difficulty(room, spectator.token, "hard")


@pytest.mark.asyncio
async def test_leaving_seat_after_finished_clears_ai_and_waits_for_side_choice() -> None:
    """结算阶段退出玩家席：花火一起离座，下次入座停在 setup（可以选先后手）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
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
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token, side="white")
    assert room.status == "active"
    assert [seat.is_ai for seat in room.multiplayer.seats] == [False, True]

    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    assert room.status == "finished"

    await manager.leave_player_seat(room, visitor.token)

    # 花火不能留在玩家席上，否则下一个人入座就直接开局
    assert room.multiplayer.seats == []
    assert room.status == "waiting"
    assert room.game is None

    await manager.claim_and_start(room, visitor.token, "human_black")

    assert room.status == "setup"
    assert room.game is None


async def _duel_room_with_ai(manager: RoomManager):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:session",
        platform="aiocqhttp",
        group_id="20001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.add_ai_seat(room, visitor.token, side="white")
    return room, visitor


@pytest.mark.asyncio
async def test_duel_sides_alternate_every_rematch() -> None:
    """再来一局严格换先：黑 / 白 / 黑 / 白 …，不再「只换一次然后卡住」。"""
    manager = RoomManager(max_private_rooms=0)
    room, visitor = await _duel_room_with_ai(manager)
    colors = [room.game.human_color]

    for _ in range(4):
        room.game.winner = room.game.bot_color
        await manager._finish_game(room)
        await manager.start_game(room, visitor.token, "human_black")
        colors.append(room.game.human_color)

    assert colors == [1, 2, 1, 2, 1]
    sides = [seat.side for seat in room.multiplayer.seats]
    assert sides == ["black", "white"]


@pytest.mark.asyncio
async def test_duel_sides_fixed_by_seat_when_rotation_disabled() -> None:
    """关掉「再来一局换先」：固定按座位号执色，永远不换。"""
    manager = RoomManager(max_private_rooms=0, duel_side_rotation=False)
    room, visitor = await _duel_room_with_ai(manager)

    for _ in range(3):
        assert [seat.side for seat in room.multiplayer.seats] == ["black", "white"]
        room.game.winner = room.game.bot_color
        await manager._finish_game(room)
        await manager.start_game(room, visitor.token, "human_black")

    assert room.game.human_color == 1


@pytest.mark.asyncio
async def test_duel_sides_restart_sequence_when_roster_changes() -> None:
    """玩家席成员变化后重新起序列：按座位号定色，而不是接着上一局的交替。"""
    manager = RoomManager(max_private_rooms=0)
    room, visitor = await _duel_room_with_ai(manager)
    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    await manager.start_game(room, visitor.token, "human_black")
    assert room.game.human_color == 2  # 第 2 局玩家执白

    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    await manager.remove_ai_seat(room, visitor.token)
    rival = await manager.join(room)
    await manager.claim_and_start(room, rival.token, "human_black")

    assert [seat.side for seat in room.multiplayer.seats] == ["black", "white"]
    assert room.game.human_color == 1


def test_xiangqi_sides_swap_red_and_black() -> None:
    """象棋换先：红黑互换（不能和黑/白词表混用）。"""
    from astrbot_plugin_game_companion_huahuo.models import GameRoom, MultiplayerState, PlayerSeat

    manager = RoomManager.__new__(RoomManager)
    manager.duel_side_rotation = True
    room = GameRoom(
        room_id="r1",
        access_token="t",
        source="group",
        session_id="UMO:t:Group:1",
        platform="aiocqhttp",
        group_id="1",
        creator_qq="1",
        creator_name="创建者",
        admin_room=False,
        game_type="xiangqi",
        difficulty="normal",
        multiplayer=MultiplayerState(enabled=True),
    )
    room.multiplayer.seats = [
        PlayerSeat(visitor_token="a", side="red"),
        PlayerSeat(visitor_token="b", side="black"),
    ]

    manager._swap_duel_sides(room)

    assert [seat.side for seat in room.multiplayer.seats] == ["black", "red"]


@pytest.mark.asyncio
async def test_restart_finished_game_also_rotates_sides() -> None:
    """群里/LLM 触发的再来一局同样换先（不再固定 rotate=True 卡死）。"""
    manager = RoomManager(max_private_rooms=0)
    room, _visitor = await _duel_room_with_ai(manager)

    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    await manager.restart_finished_game(room, difficulty="normal")
    assert room.game.human_color == 2

    room.game.winner = room.game.bot_color
    await manager._finish_game(room)
    await manager.restart_finished_game(room, difficulty="normal")
    assert room.game.human_color == 1


@pytest.mark.asyncio
async def test_blackjack_hand_kept_by_uid_after_departed_seat_renumber() -> None:
    """掉线玩家被移出玩家席后，剩余玩家的手牌仍按稳定 uid 精确认领。

    旧实现用座位号做手牌键，掉线回收会重编座位号，导致剩下的玩家
    「认错手牌 / 找不到手牌」。回归保证改用 uid 后不再发生。
    """
    manager = RoomManager(blackjack_max_players=3, multiplayer_turn_timeout=60)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    third = await manager.join(room)
    for visitor in (first, second, third):
        await manager.claim_and_start(room, visitor.token, "")

    room.game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[first.number, second.number, third.number],
        player_uids=[
            _visitor_uid(first.token),
            _visitor_uid(second.token),
            _visitor_uid(third.token),
        ],
        shoe=fixed_blackjack_shoe(["10", "10", "10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    first.connected = True
    third.connected = True
    now = time.time()
    first.last_seen_at = now
    third.last_seen_at = now
    manager._reset_turn_deadline(room, now=now)

    # 管理台在对局进行中把 2 号移到观众席：座位会重编，但手牌必须仍按 uid 认领
    await manager.remove_player(room, second.number)

    # 掉线的 second 手牌按 uid 精确投降，third 手牌不受影响
    assert room.game.hand_for_uid(_visitor_uid(second.token)).status == "surrendered"
    assert room.game.hand_for_uid(_visitor_uid(third.token)).status == "playing"
    # 对局进行中座位编号冻结，third 的局内号码未被重编
    assert room.game.hand_for_uid(_visitor_uid(third.token)).number == third.number
    assert third.number == 3


@pytest.mark.asyncio
async def test_undercover_word_fetch_runs_outside_room_lock(monkeypatch) -> None:
    """卧底取词（可能调 LLM、最长数秒）必须发生在 room.lock 之外。

    否则会锁死本房间的其它操作，并拖慢串行的 sweep_expired 链路。
    """
    manager = RoomManager(
        undercover_min_players=3,
        undercover_ai_fill_enabled=False,
        undercover_max_players=3,
    )
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:uc",
        platform="aiocqhttp",
        group_id="20002",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )
    visitors = [await manager.join(room) for _index in range(3)]
    for visitor in visitors:
        await manager.claim_and_start(room, visitor.token, "")

    lock_states: list[bool] = []

    async def fake_fetch(r):
        lock_states.append(r.lock.locked())
        return ("牛奶", "豆浆")

    monkeypatch.setattr(manager, "_fetch_undercover_word_pair", fake_fetch)
    await manager.start_game(room, visitors[0].token, "")

    assert room.status == "active"
    assert isinstance(room.game, UndercoverGame)
    # 取词时房间锁必须是释放状态
    assert lock_states == [False]


@pytest.mark.asyncio
async def test_undercover_auto_start_does_not_block_sweep(monkeypatch) -> None:
    """满员触发的自动开局必须丢到后台，不阻塞 sweep 串行循环，且不会重复触发。"""
    manager = RoomManager(
        undercover_min_players=3,
        undercover_ai_fill_enabled=False,
        undercover_max_players=3,
    )
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:uc2",
        platform="aiocqhttp",
        group_id="20003",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )
    visitors = [await manager.join(room) for _index in range(3)]
    for visitor in visitors:
        await manager.claim_and_start(room, visitor.token, "")

    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    fetch_calls: list[int] = []

    async def fake_fetch(r):
        fetch_calls.append(1)
        fetch_started.set()
        await release_fetch.wait()
        return ("牛奶", "豆浆")

    monkeypatch.setattr(manager, "_fetch_undercover_word_pair", fake_fetch)

    now = time.time()
    # 第一轮清扫：满员触发自动开局，取词在后台挂起，sweep 应立即返回
    await manager._tick_multiplayer_room(room, now)
    await fetch_started.wait()
    assert room.undercover_auto_starting is True
    assert fetch_calls == [1]

    # 取词尚未返回时再来一轮清扫：防重入标记应阻止重复触发
    await manager._tick_multiplayer_room(room, now + 2)
    assert fetch_calls == [1]

    # 放行取词，自动开局完成
    release_fetch.set()
    await manager.wait_auto_starts()
    assert room.status == "active"
    assert isinstance(room.game, UndercoverGame)
    assert room.undercover_auto_starting is False


@pytest.mark.asyncio
async def test_spectator_claims_finished_duel_takes_over_ai_seat() -> None:
    """对局结束后，观众席玩家可以直接接替花火的座位，与在席玩家重新对弈。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
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
    first = await manager.join(room)
    await manager.assign_player(room, first.number, "10001")
    await manager.add_ai_seat(room, first.token, side="black")
    assert room.status == "active"
    assert manager._ai_seat(room) is not None

    # 本局结束，在席玩家还没申请再来一局
    room.status = "finished"
    room.game.winner = 1
    room.game.draw = False

    # 观众席玩家（已绑定身份）接替花火
    spectator = await manager.join(room)
    spectator.identity_confirmed = True
    spectator.qq = "10002"
    spectator.display_name = "观众乙"

    await manager.claim_and_start(room, spectator.token, "")

    assert manager._ai_seat(room) is None
    assert len(room.multiplayer.seats) == 2
    assert all(not seat.is_ai for seat in room.multiplayer.seats)
    assert room.status == "active"
    assert room.game is not None
