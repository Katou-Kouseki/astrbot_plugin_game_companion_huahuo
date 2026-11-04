"""房间内「投票换游戏」：门槛、单人放行、冷却、过期、撤回与对局中开关。"""

from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def _room(manager: RoomManager, game_type: str = "gomoku"):
    return await manager.create_room(
        source="private",
        session_id="GAMEVOTE:priv:1",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )


@pytest.mark.asyncio
async def test_two_online_players_need_both_votes() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    first = await manager.join(room)
    second = await manager.join(room)

    await manager.propose_game_vote(room, first.token, "othello")

    vote = room.game_vote
    assert vote is not None
    # 两人在线 → 门槛 2，发起人自带 1 票但还差一票
    assert vote.voter_base == 2
    assert vote.required == 2
    assert vote.counts() == (1, 0)
    assert room.game_type == "gomoku"

    passed = await manager.cast_game_vote(room, second.token, True)

    assert passed is True
    assert room.game_vote is None
    assert room.game_type == "othello"


@pytest.mark.asyncio
async def test_single_online_player_passes_immediately() -> None:
    """只有自己在线时不该被门槛卡死：发起即通过。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    only = await manager.join(room)

    await manager.propose_game_vote(room, only.token, "othello")

    assert room.game_type == "othello"
    assert room.game_vote is None


@pytest.mark.asyncio
async def test_against_vote_does_not_block_the_threshold() -> None:
    """反对票只做展示，不参与分母；赞成票够了就切换。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    first = await manager.join(room)
    second = await manager.join(room)
    third = await manager.join(room)

    await manager.propose_game_vote(room, first.token, "othello")
    # 三人在线 → 需要 2 票；先投一张反对，再补一张赞成
    assert await manager.cast_game_vote(room, second.token, False) is False
    assert room.game_vote is not None
    assert room.game_vote.counts() == (1, 1)

    assert await manager.cast_game_vote(room, third.token, True) is True
    assert room.game_type == "othello"


@pytest.mark.asyncio
async def test_cannot_propose_while_playing_by_default() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    await manager.join(room)
    room.status = "active"

    with pytest.raises(ValueError, match="对局进行中"):
        await manager.propose_game_vote(room, next(iter(room.visitors)), "othello")


@pytest.mark.asyncio
async def test_play_while_active_allowed_by_switch() -> None:
    manager = RoomManager(max_private_rooms=0, game_vote_allow_while_playing=True)
    room = await _room(manager)
    only = await manager.join(room)
    room.status = "active"

    await manager.propose_game_vote(room, only.token, "othello")

    assert room.game_type == "othello"


@pytest.mark.asyncio
async def test_cooldown_blocks_the_next_proposal() -> None:
    manager = RoomManager(max_private_rooms=0, game_vote_cooldown=30)
    room = await _room(manager)
    only = await manager.join(room)

    await manager.propose_game_vote(room, only.token, "othello")
    assert room.game_type == "othello"

    with pytest.raises(ValueError, match="冷却"):
        await manager.propose_game_vote(room, only.token, "gomoku")


@pytest.mark.asyncio
async def test_expired_proposal_is_pruned() -> None:
    manager = RoomManager(max_private_rooms=0, game_vote_cooldown=0)
    room = await _room(manager)
    first = await manager.join(room)
    await manager.join(room)

    await manager.propose_game_vote(room, first.token, "othello")
    vote = room.game_vote
    assert vote is not None

    # 未到期不清
    assert manager.prune_game_vote(room, now=vote.expires_at - 1) is False
    assert room.game_vote is not None
    # 到期清掉，并且不改玩法
    assert manager.prune_game_vote(room, now=vote.expires_at + 1) is True
    assert room.game_vote is None
    assert room.game_type == "gomoku"


@pytest.mark.asyncio
async def test_only_proposer_can_cancel() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    first = await manager.join(room)
    second = await manager.join(room)

    await manager.propose_game_vote(room, first.token, "othello")

    with pytest.raises(PermissionError):
        await manager.cancel_game_vote(room, second.token)

    await manager.cancel_game_vote(room, first.token)
    assert room.game_vote is None
    assert room.game_type == "gomoku"


@pytest.mark.asyncio
async def test_switchable_games_excludes_current_and_disabled() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    manager.enabled_games["othello"] = False

    keys = [item["key"] for item in manager.switchable_games(room)]

    assert "gomoku" not in keys  # 当前玩法
    assert "othello" not in keys  # 管理员已关闭
    assert "blackjack" in keys
    assert all(item["label"] for item in manager.switchable_games(room))


@pytest.mark.asyncio
async def test_proposal_snapshot_shape() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await _room(manager)
    first = await manager.join(room)
    await manager.join(room)

    await manager.propose_game_vote(room, first.token, "othello")
    snapshot = room.public_snapshot(first.token, switchable_games=manager.switchable_games(room))

    vote = snapshot["game_vote"]
    assert vote["target_game"] == "othello"
    assert vote["required"] == 2
    assert vote["votes_for"] == 1
    assert vote["my_vote"] == "for"
    assert vote["is_proposer"] is True
    # 可选玩法下发到前端
    assert snapshot["switchable_games"]


@pytest.mark.asyncio
async def test_vote_disabled_blocks_everything() -> None:
    manager = RoomManager(max_private_rooms=0, game_vote_enabled=False)
    room = await _room(manager)
    only = await manager.join(room)

    with pytest.raises(PermissionError):
        await manager.propose_game_vote(room, only.token, "othello")
