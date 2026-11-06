from __future__ import annotations

import asyncio
import threading

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE, GomokuGame
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


async def make_bot_room(manager: RoomManager):
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
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    game.turn = WHITE  # 轮到花火
    room.game = game
    room.status = "active"
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
        return (7, 7)

    monkeypatch.setattr(GomokuGame, "choose_bot_move", slow_choose)

    task = asyncio.create_task(manager._bot_turn(room))
    assert await asyncio.to_thread(started.wait, 5.0), "计算没有开始"

    # 计算还在进行：锁必须是自由状态，且能被别的请求立刻拿到
    assert not room.lock.locked()
    async with room.lock:
        pass

    release.set()
    await asyncio.wait_for(task, timeout=5.0)

    assert len(game.history) == 1
    assert game.history[0][:2] == (7, 7)


@pytest.mark.asyncio
async def test_stale_bot_plan_is_discarded(monkeypatch) -> None:
    """思考期间局面被改（悔棋/换局/判负）时，算好的那一手必须丢弃。"""
    manager = RoomManager()
    manager.bot_think_delay = lambda _room: 0.0  # type: ignore[method-assign]
    room, game = await make_bot_room(manager)

    def choose_but_board_changes(self, **_kwargs):
        # 模拟「计算期间局面变了」：回合被交还给真人
        self.turn = self.human_color
        return (7, 7)

    monkeypatch.setattr(GomokuGame, "choose_bot_move", choose_but_board_changes)

    await asyncio.wait_for(manager._bot_turn(room), timeout=5.0)

    assert game.history == [], "局面已经变了，花火这一手不该落下去"


@pytest.mark.asyncio
async def test_marker_tracks_turn_and_move_count() -> None:
    game = GomokuGame(human_color=BLACK, difficulty="normal")
    game.turn = WHITE
    before = RoomManager._bot_plan_marker(game)
    game.place(7, 7, WHITE)
    after = RoomManager._bot_plan_marker(game)
    assert before != after
