"""棋类「连战」的归属：一条连战只能属于同一批坐在玩家席上的人。

玩家席换人、有人退回观众席、过一会又坐回来，都会把上一段连战结账，
新一轮从第一局重新计数——否则群里会看到「连战 5 局」，其实中间换了人。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def make_plugin(manager: RoomManager) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.board_group_announce_style = "text"
    plugin.board_group_announce_recap = False
    plugin.board_group_announce_cooldown = 30
    plugin.board_group_announce_series_idle = 0
    plugin.board_group_announce_wait_while_active = False
    plugin.sent_texts: list[str] = []
    plugin._board_flush_tasks = {}
    plugin.spawned: list = []

    async def fake_send(_room, text):
        plugin.sent_texts.append(text)
        return True

    def spawn(operation):
        plugin.spawned.append(operation)
        return SimpleNamespace(done=lambda: False, cancel=lambda: None)

    plugin._send_to_origin = fake_send  # type: ignore[attr-defined]
    plugin._spawn = spawn  # type: ignore[attr-defined]
    return plugin


async def run_spawned(plugin: GameCompanionPlugin) -> None:
    """把 _handle_board_lineup_change 排下的任务手动跑掉（测试里不靠事件循环）。"""
    while plugin.spawned:  # type: ignore[attr-defined]
        await plugin.spawned.pop(0)  # type: ignore[attr-defined]


async def make_room(manager: RoomManager, players: int = 2):
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:9",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    tokens = []
    for index in range(players):
        visitor = await manager.join(room)
        await manager.assign_player(
            room, visitor.number, f"1000{index + 1}", allow_non_numeric=True
        )
        room.visitors[visitor.token].display_name = ["小明", "小红", "小刚"][index]
        tokens.append(visitor.token)
    return room, tokens


def entry(
    completed: int,
    lineup,
    *,
    winner: str = "小明",
    loser: str = "小红",
    moves: int = 12,
):
    return {
        "game_type": "gomoku",
        "game_uid": f"g{completed}",
        "completed_games": completed,
        "moves": moves,
        "draw": False,
        "winner_name": winner,
        "winner_side": "black",
        "loser_name": loser,
        "loser_side": "white",
        "winner_is_ai": False,
        "human_won": None,
        "human_name": "",
        "draw_names": "",
        "lineup": tuple(lineup),
    }


# ---------------- 阵容签名 ----------------


@pytest.mark.asyncio
async def test_lineup_follows_the_seats() -> None:
    """阵容按座位上的真人算：换人、离席都会变，换边不会变。"""
    manager = RoomManager(max_private_rooms=0)
    room, tokens = await make_room(manager)
    plugin = make_plugin(manager)

    first = plugin._board_series_lineup(room)  # noqa: SLF001
    assert first == ("qq:10001", "qq:10002")

    # 换个先后手 / 准备状态：人没变，阵容就不该变
    room.multiplayer.seats[0].side, room.multiplayer.seats[1].side = "white", "black"
    assert plugin._board_series_lineup(room) == first  # noqa: SLF001

    # 小红退回观众席：阵容少一个人
    await manager.leave_player_seat(room, tokens[1])
    left = plugin._board_series_lineup(room)  # noqa: SLF001
    assert left == ("qq:10001",)

    # 又坐回来：仍然是同一批人，但这是一段新的连战（靠事件切断，不靠签名相等）
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10002", allow_non_numeric=True)
    assert plugin._board_series_lineup(room) == first  # noqa: SLF001


@pytest.mark.asyncio
async def test_single_player_room_uses_the_primary_player() -> None:
    """没启用玩家席的单人对局：阵容退化成「主玩家是谁」。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:11",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    room.player_token = visitor.token
    plugin = make_plugin(manager)

    assert plugin._board_series_lineup(room) == (f"tk:{visitor.token}",)  # noqa: SLF001


# ---------------- 换人 / 回来都要结账 ----------------


@pytest.mark.asyncio
async def test_leaving_to_spectator_flushes_the_series() -> None:
    """有人退回观众席：这一串连战立刻发掉，并让接下来的第一局重新单局通报。"""
    manager = RoomManager(max_private_rooms=0)
    room, tokens = await make_room(manager)
    plugin = make_plugin(manager)
    lineup = plugin._board_series_lineup(room)  # noqa: SLF001
    room.board_series_lineup = lineup
    room.pending_board_announces = [entry(1, lineup), entry(2, lineup)]
    room.last_board_announce_at = 1000.0

    await manager.leave_player_seat(room, tokens[1])
    plugin._handle_board_lineup_change(room)  # noqa: SLF001
    await run_spawned(plugin)

    assert len(plugin.sent_texts) == 1
    assert "连战 2 局" in plugin.sent_texts[0]
    assert room.pending_board_announces == []
    # 冷却窗口一起清掉：新阵容的第一局不能继续「攒着」
    assert room.last_board_announce_at == 0.0


@pytest.mark.asyncio
async def test_returning_player_does_not_join_the_old_streak() -> None:
    """退出去当观众、过一会又回来：之前的连战已经结账，回来后的第一局是新的。"""
    manager = RoomManager(max_private_rooms=0)
    room, tokens = await make_room(manager)
    plugin = make_plugin(manager)
    lineup = plugin._board_series_lineup(room)  # noqa: SLF001
    room.board_series_lineup = lineup
    room.pending_board_announces = [entry(1, lineup)]
    room.last_board_announce_at = 1000.0

    # 小红退席：手上的那一局先发掉
    await manager.leave_player_seat(room, tokens[1])
    plugin._handle_board_lineup_change(room)  # noqa: SLF001
    await run_spawned(plugin)
    assert len(plugin.sent_texts) == 1
    assert "连战" not in plugin.sent_texts[0]

    # 过一会她又坐回来：这段时间没有新对局，不该凭空补发任何东西
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10002", allow_non_numeric=True)
    plugin._handle_board_lineup_change(room)  # noqa: SLF001
    await run_spawned(plugin)
    assert len(plugin.sent_texts) == 1, "回来不该再次播报"

    # 回来后的第一局：独立成篇，不会和退席前那一局拼成「连战 2 局」
    room.pending_board_announces = [entry(2, plugin._board_series_lineup(room))]  # noqa: SLF001
    await plugin._flush_board_announces(room, 0)
    assert len(plugin.sent_texts) == 2
    assert "连战" not in plugin.sent_texts[1]
    assert "12 手" in plugin.sent_texts[1]


@pytest.mark.asyncio
async def test_admin_swapping_a_seat_resets_the_series() -> None:
    """管理台把座位上的人换掉：上一批人的连战先结账，新玩家从第一局算起。"""
    manager = RoomManager(max_private_rooms=0)
    room, tokens = await make_room(manager)
    plugin = make_plugin(manager)
    lineup = plugin._board_series_lineup(room)  # noqa: SLF001
    room.board_series_lineup = lineup
    room.pending_board_announces = [entry(1, lineup), entry(2, lineup)]
    room.last_board_announce_at = 1000.0

    # 把 2 号座位换给「小刚」
    await manager.assign_player(
        room, room.multiplayer.seats[1].number, "10003", allow_non_numeric=True
    )
    plugin._handle_board_lineup_change(room)  # noqa: SLF001
    await run_spawned(plugin)

    assert len(plugin.sent_texts) == 1
    assert "连战 2 局" in plugin.sent_texts[0]
    assert plugin._board_series_lineup(room) == ("qq:10001", "qq:10003")  # noqa: SLF001


@pytest.mark.asyncio
async def test_same_seats_keep_the_streak_running() -> None:
    """只是换先 / 准备状态变化（人没变）时，连战继续累计，不要误切。"""
    manager = RoomManager(max_private_rooms=0)
    room, _tokens = await make_room(manager)
    plugin = make_plugin(manager)
    lineup = plugin._board_series_lineup(room)  # noqa: SLF001
    room.board_series_lineup = lineup
    room.pending_board_announces = [entry(1, lineup)]
    room.last_board_announce_at = 1000.0

    room.multiplayer.seats[0].side = "white"
    plugin._handle_board_lineup_change(room)  # noqa: SLF001
    await run_spawned(plugin)

    assert plugin.sent_texts == []
    assert len(room.pending_board_announces) == 1


# ---------------- 兜底：混进来的对局分开播 ----------------


def test_mixed_lineups_are_grouped_apart() -> None:
    """队列里混了两批玩家的对局时按阵容分组，不硬凑成一条连战。"""
    entries = [
        entry(1, ("qq:10001", "qq:10002")),
        entry(2, ("qq:10001", "qq:10002")),
        entry(3, ("qq:10001", "qq:10003")),
    ]
    groups = GameCompanionPlugin._board_series_groups(entries)

    assert [len(group) for group in groups] == [2, 1]
    assert [group[0]["completed_games"] for group in groups] == [1, 3]


@pytest.mark.asyncio
async def test_flush_sends_each_lineup_as_its_own_series() -> None:
    """真发出去时：两局一段连战 + 另一批人的一局单报，各自成条。"""
    manager = RoomManager(max_private_rooms=0)
    room, _tokens = await make_room(manager)
    plugin = make_plugin(manager)
    a = ("qq:10001", "qq:10002")
    b = ("qq:10001", "qq:10003")
    room.pending_board_announces = [entry(1, a), entry(2, a), entry(3, b)]

    await plugin._flush_board_announces(room, 0)

    assert len(plugin.sent_texts) == 2
    assert "连战 2 局" in plugin.sent_texts[0]
    assert "连战" not in plugin.sent_texts[1]
    assert room.pending_board_announces == []
