"""防作弊回归：隐藏信息不得出现在进行中的快照里。

对应 `docs/对局防作弊评估书.md` 的三个 P0：
A. 你画我猜：玩家席成员能直接读到谜底；
B. 二十一点：快照返回所有人的手牌；
C. 围棋：开局未落子时能改贴目。
"""

from __future__ import annotations

import time

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_room(manager: RoomManager, game_type: str, *, seats: int = 1):
    room = await manager.create_room(
        source="group",
        session_id=f"aiocqhttp:group:{52000 + seats}",
        platform="aiocqhttp",
        group_id=str(52000 + seats),
        creator_qq="62001",
        creator_name="房主",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )
    visitors = []
    for _ in range(seats):
        visitor = await manager.join(room)
        visitor.connected = True
        visitor.last_seen_at = time.time()
        visitors.append(visitor)
    await manager.claim_and_start(room, visitors[0].token, "")
    for visitor in visitors[1:]:
        await manager.claim_and_start(room, visitor.token, "")
    if room.status != "active":
        await manager.start_game(room, visitors[0].token, "")
    return room, visitors


def snapshot_of(room, visitor) -> dict:
    return room.public_snapshot(visitor.token)


# ---------------- A. 你画我猜 ----------------


@pytest.mark.asyncio
async def test_draw_guess_hides_answer_until_finished() -> None:
    manager = RoomManager()
    room, visitors = await make_room(manager, "draw_guess")

    game = snapshot_of(room, visitors[0])["game"]
    assert "answer" not in game, "进行中的快照绝不能带谜底"
    assert room.game.answer not in str(game)

    room.game.finished = True
    room.status = "finished"
    finished = snapshot_of(room, visitors[0])["game"]
    assert finished.get("answer") == room.game.answer


# ---------------- B. 二十一点 ----------------


@pytest.mark.asyncio
async def test_blackjack_only_exposes_own_hand_before_showdown() -> None:
    manager = RoomManager(blackjack_max_players=2)
    room, visitors = await make_room(manager, "blackjack", seats=2)
    # 发牌是随机的：起手 21 点会直接摊牌结算，那种局面本来就该公开。
    # 这里固定回玩家回合，测的才是「进行中不泄露别人手牌」。
    room.game.phase = "player_turns"
    room.game.finished = False

    hands = snapshot_of(room, visitors[0])["game"]["hands"]
    assert len(hands) == 2, "条目数要保留，前端才知道有几位玩家"
    own = [hand for hand in hands.values() if "cards" in hand]
    others = [hand for hand in hands.values() if "cards" not in hand]
    assert len(own) == 1 and len(others) == 1, "只有自己那一手带牌面"
    assert others[0]["number"] and "status" in others[0] and "result" in others[0]
    assert "value" not in others[0], "点数也是暗牌的一部分"

    # 摊牌（庄家回合）后全部公开
    room.game.phase = "dealer_turn"
    after = snapshot_of(room, visitors[0])["game"]["hands"]
    assert all("cards" in hand for hand in after.values())


# ---------------- C. 围棋 ----------------


@pytest.mark.asyncio
async def test_weiqi_board_and_komi_locked_after_start() -> None:
    manager = RoomManager()
    room, visitors = await make_room(manager, "weiqi", seats=2)
    before = room.game.komi

    with pytest.raises(ValueError):
        await manager.set_weiqi_board(room, visitors[0].token, 13, 30)
    with pytest.raises(ValueError):
        await manager.set_weiqi_board(room, visitors[0].token, 19)
    assert room.game.komi == before
    assert room.game.size == 13


@pytest.mark.asyncio
async def test_weiqi_komi_is_admin_only_even_before_start() -> None:
    manager = RoomManager()
    room, visitors = await make_room(manager, "weiqi", seats=2)
    # 回到开局前，模拟「还没开始就改贴目」
    room.status = "setup"
    room.game = None

    with pytest.raises(PermissionError):
        await manager.set_weiqi_board(room, visitors[0].token, 13, 30)

    # 管理员房间仍可调，方便办比赛
    room.admin_room = True
    await manager.set_weiqi_board(room, visitors[0].token, 13, 6.5)
    assert room.weiqi_komi == 6.5


@pytest.mark.asyncio
async def test_weiqi_board_size_still_changeable_before_start() -> None:
    manager = RoomManager()
    room, visitors = await make_room(manager, "weiqi", seats=2)
    room.status = "setup"
    room.game = None

    await manager.set_weiqi_board(room, visitors[0].token, 19)
    assert room.weiqi_size == 19
