from __future__ import annotations

import asyncio

import pytest
from astrbot_plugin_game_companion.blackjack import BlackjackCard, BlackjackGame
from astrbot_plugin_game_companion.room_manager import RoomManager


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
async def test_creator_correction_swaps_seat_and_resets_game() -> None:
    manager = RoomManager()
    room = await create_room(manager, creator="10001")
    thief = await manager.join(room)
    creator = await manager.join(room)
    await manager.claim_and_start(room, thief.token, "human_black")
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
async def test_rematch_reuses_room_player_identity_score_and_side() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private", creator="10001")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
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
    assert room.game.human_color == original_human_color
    assert room.game.history == []


@pytest.mark.asyncio
async def test_rematch_does_not_reset_an_unfinished_game() -> None:
    manager = RoomManager()
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
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
async def test_heartbeat_does_not_refresh_meaningful_activity() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=10)
    room = await create_room(manager)
    visitor = await manager.join(room)
    room.last_activity_at = 100.0

    await manager.heartbeat(room, visitor.token)

    assert room.last_activity_at == 100.0


@pytest.mark.asyncio
async def test_finished_room_expires_shortly_after_player_leaves() -> None:
    manager = RoomManager(empty_player_timeout=0, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    visitor.left_at = 100.0
    visitor.last_seen_at = 100.0

    assert await manager.sweep_expired(now=107.9) == []
    assert await manager.sweep_expired(now=108.0) == [room.room_id]
    assert manager.closed_reason_by_access_token(room.access_token) == (
        "本局结束后玩家已离开，房间已自动销毁"
    )


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
    manager = RoomManager(empty_player_timeout=0, idle_timeout=300)
    room = await create_room(manager, source="private")
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    room.status = "finished"
    visitor.left_at = None
    visitor.last_seen_at = 100.0

    assert await manager.sweep_expired(now=159.9) == []
    assert await manager.sweep_expired(now=160.0) == [room.room_id]


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

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=fixed_blackjack_shoe(["10", "6", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.claim_and_start(room, visitor.token, "")

    assert room.status == "active"
    assert isinstance(room.game, BlackjackGame)
    assert room.game.phase == "player_turns"
    assert list(room.game.hands) == [visitor.number]
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

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=fixed_blackjack_shoe(["10", "6", "10", "8", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert isinstance(room.game, BlackjackGame)
    assert set(room.game.hands) == {first.number, second.number}
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
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    manager._reset_turn_deadline(room)

    assert room.multiplayer.current_token == first.token
    await manager.player_blackjack_action(room, first.token, "stand")
    assert room.multiplayer.current_token == second.token
    await manager.player_blackjack_action(room, second.token, "stand")

    assert room.status == "finished"
    assert room.game.results == {first.number: "win", second.number: "loss"}
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
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    first.last_seen_at = 1000.0
    second.last_seen_at = 1000.0
    manager._reset_turn_deadline(room, now=1000.0)
    room.multiplayer.turn_deadline = 990.0

    await manager.sweep_expired(now=1000.0)

    assert room.game.hands[first.number].status == "stand"
    assert room.multiplayer.current_token == second.token


@pytest.mark.asyncio
async def test_blackjack_rejects_seat_join_during_an_active_round() -> None:
    manager = RoomManager(blackjack_max_players=2)
    room = await create_blackjack_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    room.game = BlackjackGame.deal(
        difficulty="normal", player_numbers=[first.number], shoe=fixed_blackjack_shoe(["10", "10", "9", "8"])
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

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=fixed_blackjack_shoe(["A", "K", "10", "6", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert room.game.hands[first.number].blackjack is True
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

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=fixed_blackjack_shoe(["A", "K", "A", "Q", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert room.status == "finished"
    assert room.game.results == {
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
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    first.connected = False
    first.last_seen_at = 1000.0
    second.last_seen_at = 1000.0
    manager._reset_turn_deadline(room, now=1000.0)

    await manager.sweep_expired(now=1000.0)

    assert room.game.hands[first.number].status == "stand"
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

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=fixed_blackjack_shoe(["10", "6", "10", "8", "9", "8"]),
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    await manager.start_game(room, first.token, "")

    assert room.game.hands[first.number].blackjack is False
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
        shoe=fixed_blackjack_shoe(["10", "10", "10", "8", "9", "10"]),
    )
    room.status = "active"
    second.connected = False
    manager._reset_turn_deadline(room)

    await manager.player_blackjack_action(room, first.token, "stand")

    assert room.status == "finished"
    assert room.game.hands[second.number].status == "stand"
    assert room.game.results == {first.number: "win", second.number: "loss"}
