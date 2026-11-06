from __future__ import annotations

import asyncio
import threading

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE, GomokuGame
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_bot_room(manager: RoomManager):
    """走真实开局流程，拿到「花火坐 AI 座位、轮到她走」的房间。

    注意不能只塞一个 GomokuGame 进 room：`_is_bot_turn` 是房间级判定，
    没有 AI 座位时它会认为是「两位真人」，花火本来就不该落子。
    """
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:41001",
        platform="aiocqhttp",
        group_id="41001",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "human_black")
    await manager.start_game(room, visitor.token, "human_black")
    game = room.game
    assert isinstance(game, GomokuGame)
    # 玩家先落一子，把回合交给花火（白）
    game.place(7, 7, game.human_color)
    assert room.game.turn == game.bot_color
    return room, game


@pytest.mark.asyncio
async def test_bot_thinking_does_not_hold_room_lock(monkeypatch) -> None:
    """花火思考（围棋/象棋搜索）期间不能占着 room.lock。

    旧实现把 choose_bot_move 放在 `async with room.lock` 里，思考几百毫秒到几秒的
    整段时间网页心跳、落子、入座全排队，房间会明显发木。
    """
    manager = RoomManager()
    manager.bot_think_delay = lambda _room: 0.0  # type: ignore[method-assign]
    room, game = await make_bot_room(manager)

    started = threading.Event()
    release = threading.Event()

    def slow_choose(self, **_kwargs):
        started.set()
        release.wait(5.0)
        return (7, 8)

    monkeypatch.setattr(GomokuGame, "choose_bot_move", slow_choose)

    task = asyncio.create_task(manager._bot_turn(room))
    assert await asyncio.to_thread(started.wait, 5.0), "计算没有开始"

    # 计算还在进行：锁必须是自由状态，且能被别的请求立刻拿到
    assert not room.lock.locked()
    async with room.lock:
        pass

    release.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert len(game.history) == 2, "花火这一手应该落到棋盘上"
    assert game.history[-1][:2] == (7, 8)


@pytest.mark.asyncio
async def test_stale_bot_plan_is_discarded(monkeypatch) -> None:
    """思考期间局面被改（悔棋/换局/判负）时，算好的那一手必须丢弃。"""
    manager = RoomManager()
    manager.bot_think_delay = lambda _room: 0.0  # type: ignore[method-assign]
    room, game = await make_bot_room(manager)

    before = len(game.history)

    def choose_but_board_changes(self, **_kwargs):
        # 模拟「计算期间局面变了」：回合被交还给真人
        self.turn = self.human_color
        return (7, 8)

    monkeypatch.setattr(GomokuGame, "choose_bot_move", choose_but_board_changes)

    await asyncio.wait_for(manager._bot_turn(room), timeout=5.0)

    assert len(game.history) == before, "局面已经变了，花火这一手不该落下去"


@pytest.mark.asyncio
async def test_marker_tracks_turn_and_move_count() -> None:
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    game.turn = WHITE
    before = RoomManager._bot_plan_marker(game)
    game.place(7, 7, WHITE)
    after = RoomManager._bot_plan_marker(game)
    assert before != after


def test_xiangqi_apply_bot_move_still_checks_turn() -> None:
    """拆出来的 apply_bot_move 必须保留「不是花火回合就拒绝」的校验。"""
    from types import SimpleNamespace

    from astrbot_plugin_game_companion_huahuo.xiangqi import RED, XiangqiGame

    game = XiangqiGame(human_side=RED, difficulty="normal")
    assert game.turn == RED  # 轮到真人

    with pytest.raises(ValueError):
        asyncio.run(game.apply_bot_move(SimpleNamespace(), "h2e2"))
