from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE, GomokuGame
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def _plugin(manager: RoomManager) -> GameCompanionPlugin:
    """构造一个仅带 manager 的插件实例，用于测试纯函数 _board_announce_text。"""
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
        game_type=game_type,  # type: ignore[arg-type]
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10001")
    await manager.add_ai_seat(room, visitor.token, side="black")
    return room


def _auto_announce_plugin(manager: RoomManager, *, auto_all: bool, auto: bool):
    """构造一个仅用于验证自动播报触发条件的插件实例，mock 掉无关副作用。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.board_group_announce_enabled = True
    plugin.board_group_announce_auto_all = auto_all
    plugin.board_group_announce_auto = auto
    plugin.group_announce_enabled = False
    plugin.group_announce_auto = False
    plugin.companion_afterglow_enabled = False
    plugin.private_qq_game_context_enabled = False
    plugin.undercover_failed_mute_seconds = 0
    plugin._companion_round_event_tasks = {}
    plugin.spawned: list[tuple] = []
    plugin._spawn = lambda op: plugin.spawned.append(op)  # type: ignore[attr-defined]
    plugin._comment = lambda room, prompt, **kw: ("COMMENT",)  # type: ignore[attr-defined]
    plugin._auto_announce_board = lambda room: ("BOARD", room.room_id)  # type: ignore[attr-defined]
    plugin._auto_announce_undercover = lambda room: ("UC",)  # type: ignore[attr-defined]
    return plugin


def _spawned_board(plugin: GameCompanionPlugin) -> bool:
    return any(item and item[0] == "BOARD" for item in plugin.spawned)


@pytest.mark.asyncio
async def test_board_announce_text_ai_win_names_huahuo() -> None:
    """花火（AI 白）获胜时，棋类战报文案点名「花火」并带执方与手数。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE  # 玩家执黑、花火执白，白胜
    room.game.draw = False
    room.status = "finished"

    text = _plugin(manager)._board_announce_text(room)

    assert "五子棋" in text
    assert "花火" in text
    assert "获胜" in text
    assert "执白" in text
    assert "手" in text


@pytest.mark.asyncio
async def test_board_announce_text_human_win() -> None:
    """真人获胜时文案点名玩家昵称并带执黑。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = BLACK
    room.game.draw = False
    room.status = "finished"

    text = _plugin(manager)._board_announce_text(room)

    assert "五子棋" in text
    assert "小明" in text
    assert "获胜" in text
    assert "执黑" in text


@pytest.mark.asyncio
async def test_board_announce_text_draw() -> None:
    """和棋时文案落到「平局」。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = 0
    room.game.draw = True
    room.status = "finished"

    text = _plugin(manager)._board_announce_text(room)

    assert "五子棋" in text
    assert "平局" in text


def test_leaderboard_winrate_clamps_legacy_games() -> None:
    """旧数据只记胜场没记局数（如 12 胜 / 1 局）时，胜率不能超过 100%。"""
    manager = RoomManager.__new__(RoomManager)
    manager.global_player_wins = {
        "gomoku": {
            "10001": {"name": "小明", "wins": 12, "games": 1},
            "__huahuo_ai__": {"name": "花火", "wins": 0, "games": 12},
        }
    }

    rows = {row["name"]: row for row in manager.global_leaderboard("gomoku")}

    assert rows["小明"]["winrate"] == 100.0
    assert rows["小明"]["games"] == 12
    assert rows["花火"]["winrate"] == 0.0


@pytest.mark.asyncio
async def test_snapshot_reports_board_announced_flag() -> None:
    """通报到群后，快照用 board_announced 标记，供前端把按钮置灰。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE
    room.game.draw = False
    room.status = "finished"
    visitor = list(room.visitors.values())[0]

    snap = room.public_snapshot(visitor.token)
    assert snap["board_announced"] is False

    room.last_announced_uid = f"board:{int(room.completed_games or 0)}"
    snap = room.public_snapshot(visitor.token)
    assert snap["board_announced"] is True


@pytest.mark.asyncio
async def test_board_poster_data_ai_win() -> None:
    """花火（AI 白）获胜时，战报海报数据点名「花火」并带执白，且内嵌棋盘 SVG 非空。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.board[7][7] = BLACK
    room.game.board[7][8] = WHITE
    room.game.winner = WHITE
    room.game.draw = False
    room.game.history = [(7, 7, BLACK), (7, 8, WHITE)]
    room.status = "finished"

    data = _plugin(manager)._board_poster_data(room)

    assert data["title"] == "花火 获胜"
    assert "执白" in data["sub"]
    assert data["badge"] == "🏆"
    assert data["board_svg"].startswith("<svg")
    assert '<circle' in data["board_svg"]


@pytest.mark.asyncio
async def test_board_poster_data_includes_ai_difficulty() -> None:
    """与花火对局的战报海报副信息要带上花火棋力（难度），方便群友看清对手水平。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)  # difficulty="normal"
    room.game.winner = BLACK  # 玩家执黑胜
    room.game.draw = False
    room.game.history = [(7, 7, BLACK)]
    room.status = "finished"

    data = _plugin(manager)._board_poster_data(room)

    assert "花火（普通）" in data["sub"]


@pytest.mark.asyncio
async def test_board_poster_data_omits_difficulty_without_ai() -> None:
    """两位真人对弈（无 AI 座位）的战报海报不显示花火难度。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:3",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.assign_player(room, first.number, "10001")
    await manager.assign_player(room, second.number, "10002")
    room.multiplayer.seats[0].side = "black"
    room.multiplayer.seats[1].side = "white"
    room.game = SimpleNamespace(winner=BLACK, draw=False, history=[])
    room.status = "finished"

    data = _plugin(manager)._board_poster_data(room)

    assert "花火" not in data["sub"]


@pytest.mark.asyncio
async def test_board_poster_t2i_uses_html_render() -> None:
    """服务端海报优先走 AstrBot 的 html_render（t2i），并返回 base64 PNG。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE
    room.game.draw = False
    room.game.history = [(7, 7, BLACK)]
    room.status = "finished"

    plugin = _plugin(manager)
    captured: dict[str, object] = {}

    async def fake_render(tmpl: str, data: dict, options: object = None) -> str:
        captured["title"] = data["title"]
        captured["options"] = options
        return "https://example.com/board.png"

    plugin.html_render = fake_render  # type: ignore[attr-defined]

    async def fake_download(_url: str) -> str:
        return base64.b64encode(b"FAKEPNG").decode("ascii")

    GameCompanionPlugin._download_image_base64 = staticmethod(fake_download)  # type: ignore[method-assign]

    result = await plugin._board_poster_t2i(room)

    assert result == base64.b64encode(b"FAKEPNG").decode("ascii")
    assert captured["title"] == "花火 获胜"
    assert captured["options"]["type"] == "png"


@pytest.mark.asyncio
async def test_board_auto_announce_all_covers_human_vs_human() -> None:
    """group_announce_auto_all 开启时，玩家 vs 玩家对局结束也自动播报。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:2",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.assign_player(room, first.number, "10001")
    await manager.assign_player(room, second.number, "10002")
    room.multiplayer.seats[0].side = "black"
    room.multiplayer.seats[1].side = "white"
    room.game = SimpleNamespace(winner=BLACK, draw=False, history=[])  # type: ignore[assignment]
    room.status = "finished"

    plugin = _auto_announce_plugin(manager, auto_all=True, auto=False)
    await plugin._on_room_event("game_finished", room, {})

    assert _spawned_board(plugin)


@pytest.mark.asyncio
async def test_board_auto_announce_huahuo_only_on_ai_win() -> None:
    """花火获胜自动播报只对「花火赢」生效：玩家赢时不触发。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = BLACK  # 玩家执黑胜
    room.game.draw = False
    room.status = "finished"

    plugin = _auto_announce_plugin(manager, auto_all=False, auto=True)
    await plugin._on_room_event("game_finished", room, {})

    assert not _spawned_board(plugin)


@pytest.mark.asyncio
async def test_board_auto_announce_all_covers_human_win_vs_ai() -> None:
    """group_announce_auto_all 开启时，玩家赢花火也自动播报。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = BLACK  # 玩家执黑胜
    room.game.draw = False
    room.status = "finished"

    plugin = _auto_announce_plugin(manager, auto_all=True, auto=False)
    await plugin._on_room_event("game_finished", room, {})

    assert _spawned_board(plugin)



def _announce_plugin(
    manager: RoomManager, *, min_moves: int = 5, cooldown: int = 30
):
    """只测节流逻辑的插件：mock 掉真正的群发送，只记录是否发过。"""
    plugin = _plugin(manager)
    plugin.board_group_announce_style = "text"
    plugin.board_group_announce_min_moves = min_moves
    plugin.board_group_announce_cooldown = cooldown
    plugin.board_group_announce_recap = False
    # 默认退回「冷却一过就发」，需要测连战等待的用例自己覆盖这个值
    plugin.board_group_announce_series_idle = 0
    # 旧行为（时间驱动）：关闭「玩家在房间内就攒着」；新逻辑用例自行开启
    plugin.board_group_announce_wait_while_active = False
    plugin.sent: list[str] = []
    # 「连战 N 局」合并播报走 _send_to_origin，单独记录文案
    plugin.sent_texts: list[str] = []
    plugin._board_flush_tasks = {}

    async def fake_announce(room, image_data=""):
        plugin.sent.append("SENT")
        return "TEXT"

    async def fake_send(room, text):
        plugin.sent_texts.append(text)
        return True  # 真实 _send_to_origin 现在返回「是否发出去了」

    def spawn(operation):
        import asyncio

        return asyncio.create_task(operation)

    plugin.board_announce_result = fake_announce  # type: ignore[attr-defined]
    plugin._send_to_origin = fake_send  # type: ignore[attr-defined]
    plugin._spawn = spawn  # type: ignore[attr-defined]
    return plugin


def _cancel_flushes(plugin: GameCompanionPlugin) -> None:
    """取消排队的「连战合并播报」后台任务，让测试可以手动立即触发。"""
    tasks = getattr(plugin, "_board_flush_tasks", {})
    for task in list(tasks.values()):
        try:
            task.cancel()
        except Exception:
            pass
    tasks.clear()


@pytest.mark.asyncio
async def test_board_result_snapshot_survives_rematch() -> None:
    """终局快照：玩家立刻再战，播报仍读上一局的真实手数（不再是 0/1 手）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    assert room.last_board_result is not None
    assert room.last_board_result["moves"] == 12

    await manager.restart_finished_game(room, difficulty="normal")  # 新局 history 归零

    plugin = _plugin(manager)
    text = plugin._board_announce_text(room)
    assert "共 12 手" in text
    assert "共 1 手" not in text


@pytest.mark.asyncio
async def test_auto_announce_skips_below_min_moves() -> None:
    """手数少于「自动播报最少手数」时不发群（避免 0 手速战刷屏）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 3
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=5, cooldown=0)
    await plugin._auto_announce_board(room)

    assert plugin.sent == []


@pytest.mark.asyncio
async def test_auto_announce_respects_cooldown() -> None:
    """冷却期内不立刻发群（但会攒着等合并），冷却过后恢复单局播报。"""
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    assert plugin.sent == []
    assert len(room.pending_board_announces) == 1
    _cancel_flushes(plugin)

    # 窗口内没有积压（比如窗口刚好过去）：恢复逐局播报
    room.pending_board_announces = []
    room.last_board_announce_at = time.time() - 31
    await plugin._auto_announce_board(room)
    assert len(plugin.sent) == 1


@pytest.mark.asyncio
async def test_cooldown_games_merge_into_one_series_message() -> None:
    """冷却窗口内结束的连战不逐局刷群，窗口结束时合并成一条「连战 N 局」。"""
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = WHITE  # 花火（AI 白）先赢一局
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    _cancel_flushes(plugin)
    assert plugin.sent_texts == []
    assert len(room.pending_board_announces) == 1

    # 第二局：立刻再战（再来一局会换先，玩家改执白），玩家赢
    await manager.restart_finished_game(room, difficulty="normal")
    room.game.history = [(7, 7, WHITE)] * 9
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)
    await plugin._auto_announce_board(room)
    _cancel_flushes(plugin)
    assert plugin.sent == []  # 一局都没单独发
    assert len(room.pending_board_announces) == 2

    await plugin._flush_board_announces(room, 0)

    assert len(plugin.sent_texts) == 1
    text = plugin.sent_texts[0]
    assert "连战 2 局" in text
    assert "花火获胜" in text and "小明获胜" in text
    assert "12 手" in text and "9 手" in text
    assert "花火 1 胜" in text and "玩家 1 胜" in text
    assert room.pending_board_announces == []
    assert room.last_board_announce_at > 0


@pytest.mark.asyncio
async def test_backlog_drained_into_next_single_announce() -> None:
    """窗口刚过就来新对局：积压的连战和本局并成一条，不补发两条。"""
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, WHITE)] * 11
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    _cancel_flushes(plugin)

    await manager.restart_finished_game(room, difficulty="normal")
    room.game.history = [(7, 7, WHITE)] * 7
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)
    room.last_board_announce_at = time.time() - 31  # 冷却已过

    await plugin._auto_announce_board(room)

    assert plugin.sent == []  # 不再走单局播报
    assert len(plugin.sent_texts) == 1
    assert "连战 2 局" in plugin.sent_texts[0]


@pytest.mark.asyncio
async def test_short_games_not_queued_for_series() -> None:
    """手数不足的对局既不播报也不进合并队列（避免 0 手速战被攒成一条）。"""
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 3
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=5, cooldown=30)
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)

    assert room.pending_board_announces == []
    assert plugin.sent_texts == []


@pytest.mark.asyncio
async def test_scheduled_flush_fires_after_window() -> None:
    """排队任务会在冷却窗口结束时自动发出合并战报。"""
    import asyncio
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 10
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=0.2)
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    assert plugin.sent_texts == []

    await asyncio.sleep(0.8)

    assert len(plugin.sent_texts) == 1
    assert "连战 1 局" in plugin.sent_texts[0]


def test_board_series_text_tally() -> None:
    """连战文案：逐局胜负 + 手数 + 总比分。"""
    manager = RoomManager(max_private_rooms=0)
    plugin = _plugin(manager)
    room = type("R", (), {"game_type": "gomoku"})()
    entries = [
        {"winner_is_ai": True, "winner_side": "white", "moves": 12},
        {"winner_is_ai": False, "winner_name": "小明", "winner_side": "black", "moves": 8},
        {"draw": True, "moves": 21},
    ]

    text = plugin._board_series_text(room, entries)

    assert "连战 3 局" in text
    assert "花火获胜（执白）" in text
    assert "小明获胜（执黑）" in text
    assert "21 手" in text
    assert "花火 1 胜" in text and "玩家 1 胜" in text and "1 平" in text


@pytest.mark.asyncio
async def test_series_flushes_immediately_when_humans_leave_seat() -> None:
    """连战汇总：真人全离开玩家席就立刻发，不用等空闲计时。"""
    import asyncio
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = BLACK
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    plugin.board_group_announce_series_idle = 600  # 按理要等 10 分钟
    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    _cancel_flushes(plugin)
    assert plugin.sent_texts == []
    assert len(room.pending_board_announces) == 1

    await manager.leave_player_seat(room, room.multiplayer.seats[0].visitor_token)
    # 退席后花火也一起离座，房间回到 waiting（下次入座才能重新选先后手）
    assert room.multiplayer.seats == []
    assert room.status == "waiting"

    plugin._flush_board_series_when_seats_empty(room)
    await asyncio.sleep(0)

    assert len(plugin.sent_texts) == 1
    assert "连战 1 局" in plugin.sent_texts[0]


@pytest.mark.asyncio
async def test_series_flushes_when_human_goes_offline() -> None:
    """攒着模式：真人离线（无心跳）后，连战合并任务立即发出。"""
    import asyncio
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    plugin.board_group_announce_wait_while_active = True
    plugin.board_group_announce_series_idle = 60  # 兜底 60s，测试内不会到

    # 真人离线
    human = room.multiplayer.seats[0]
    visitor = room.visitors[human.visitor_token]
    visitor.connected = False
    visitor.last_seen_at = time.time() - 500

    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    assert len(room.pending_board_announces) == 1

    await asyncio.sleep(0.2)

    assert len(plugin.sent_texts) == 1
    assert "连战 1 局" in plugin.sent_texts[0]
    assert room.pending_board_announces == []


@pytest.mark.asyncio
async def test_series_does_not_flush_while_active_even_after_cooldown() -> None:
    """攒着模式是状态驱动：玩家在线时，即使过了冷却窗口也不发。"""
    import asyncio
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=0.2)
    plugin.board_group_announce_wait_while_active = True
    plugin.board_group_announce_series_idle = 60

    room.last_board_announce_at = time.time()
    await plugin._auto_announce_board(room)
    assert len(room.pending_board_announces) == 1

    await asyncio.sleep(0.5)  # 已超过冷却窗口 0.2s，但玩家在线，不应发

    assert plugin.sent_texts == []
    assert len(room.pending_board_announces) == 1
    _cancel_flushes(plugin)


@pytest.mark.asyncio
async def test_board_humans_all_gone() -> None:
    """_board_humans_all_gone：离席/离线返回 True，活跃在线返回 False。"""
    import time

    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.history = [(7, 7, BLACK)] * 12
    room.game.winner = WHITE
    room.game.draw = False
    await manager._finish_game(room)

    plugin = _announce_plugin(manager, min_moves=0, cooldown=30)
    human = room.multiplayer.seats[0]
    visitor = room.visitors[human.visitor_token]

    # 在线且心跳新鲜：不算离席
    visitor.connected = True
    visitor.last_seen_at = time.time()
    assert plugin._board_humans_all_gone(room) is False

    # 离线（断连）
    visitor.connected = False
    assert plugin._board_humans_all_gone(room) is True

    # 在线但心跳超时（后台标签节流/断网）
    visitor.connected = True
    visitor.last_seen_at = time.time() - 500
    assert plugin._board_humans_all_gone(room) is True

    # 全部离席
    await manager.leave_player_seat(room, human.visitor_token)
    assert room.multiplayer.seats == []
    assert plugin._board_humans_all_gone(room) is True


@pytest.mark.asyncio
async def test_board_svg_draws_gomoku_winning_line() -> None:
    """五子棋战报海报：连成五的一串棋子上要画一条贯穿的高亮线。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)  # 人类执黑、花火执白
    for col in range(3, 8):
        room.game.board[7][col] = BLACK
    room.game.winner = BLACK
    room.game.draw = False
    room.game.history = [(7, col, BLACK) for col in range(3, 8)]
    manager._snapshot_board_result(room)

    svg = _plugin(manager)._board_svg(room)

    assert 'stroke="#ffd166"' in svg


@pytest.mark.asyncio
async def test_board_poster_data_othello_shows_piece_count() -> None:
    """黑白棋战报海报副信息显示双方终局子数。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:4",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="othello",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.assign_player(room, visitor.number, "10001")
    await manager.add_ai_seat(room, visitor.token, side="black")
    room.game.winner = BLACK
    room.game.draw = False
    manager._snapshot_board_result(room)

    data = _plugin(manager)._board_poster_data(room)

    assert "黑 " in data["sub"]
    assert "白 " in data["sub"]


def _report_plugin(manager: RoomManager) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.board_report_mode = "daily"
    return plugin


@pytest.mark.asyncio
async def test_board_sample_room_produces_snapshot_with_highlights() -> None:
    """样例房间会冻结终局快照，带上三种终局高亮（五子棋连线/象棋最后一手/黑白棋落子+子数）。"""
    manager = RoomManager(max_private_rooms=0)
    plugin = _report_plugin(manager)

    gomoku = plugin._board_sample_room("五子棋")
    assert gomoku.last_board_result["winning_line"]
    assert gomoku.last_board_result.get("last_move") is None

    othello = plugin._board_sample_room("黑白棋")
    assert othello.last_board_result["last_move"] == {"to": [2, 4], "color": 1}
    assert othello.last_board_result["piece_count"]["black"] > othello.last_board_result["piece_count"]["white"]

    xiangqi = plugin._board_sample_room("中国象棋")
    assert xiangqi.last_board_result["last_move"]["to"] is not None
    assert xiangqi.last_board_result["last_move"]["from"] is not None

    assert plugin._board_sample_room("").game_type == "gomoku"


@pytest.mark.asyncio
async def test_board_sample_room_svg_highlights_each_game() -> None:
    """三种棋类的样例海报 SVG 都带金色高亮标记。"""
    manager = RoomManager(max_private_rooms=0)
    plugin = _report_plugin(manager)
    for key in ("五子棋", "黑白棋", "中国象棋"):
        room = plugin._board_sample_room(key)
        svg = plugin._board_svg(room)
        assert "#ffd166" in svg


def test_board_report_data_groups_four_games() -> None:
    """棋类日报数据按四种棋类分区，每区最多 limit 名。"""
    manager = RoomManager(max_private_rooms=0)
    manager.global_player_wins["gomoku"] = {
        "1": {"name": "小明", "wins": 5, "games": 8},
        "2": {"name": "小红", "wins": 3, "games": 5},
    }
    manager.global_player_wins["xiangqi"] = {"3": {"name": "王五", "wins": 4, "games": 9}}
    plugin = _report_plugin(manager)

    data = plugin._board_report_data(subtitle="日报")

    labels = [s["label"] for s in data["sections"]]
    assert labels == ["五子棋", "中国象棋"]
    gomoku = data["sections"][0]
    assert [r["name"] for r in gomoku["rows"]] == ["小明", "小红"]
    assert gomoku["rows"][0]["winrate"] == 62
    assert data["subtitle"] == "日报"


def test_board_report_data_empty_returns_no_sections() -> None:
    manager = RoomManager(max_private_rooms=0)
    plugin = _report_plugin(manager)
    data = plugin._board_report_data(subtitle="周报")
    assert data["sections"] == []
    assert data["subtitle"] == "周报"


def test_board_report_poster_pillow_renders_png() -> None:
    """棋类日报海报 Pillow 兜底能渲染出 PNG（有数据与空数据两种）。"""
    pytest.importorskip("PIL")
    manager = RoomManager(max_private_rooms=0)
    manager.global_player_wins["gomoku"] = {
        "1": {"name": "小明", "wins": 5, "games": 8},
    }
    plugin = _report_plugin(manager)

    data = plugin._board_report_data(subtitle="日报")
    png = GameCompanionPlugin._board_report_poster_pillow(data)
    assert png.startswith("iVBOR")  # base64 PNG 头

    empty = plugin._board_report_data(subtitle="周报")
    empty_png = GameCompanionPlugin._board_report_poster_pillow(empty)
    assert empty_png.startswith("iVBOR")


@pytest.mark.asyncio
async def test_snapshot_records_loser_for_vs_ai() -> None:
    """终局快照记录败者：单人 vs 花火，花火(白)胜 → 败者是玩家(黑)。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE
    room.game.draw = False
    room.game.history = [(7, 7, BLACK), (7, 8, WHITE)]
    manager._snapshot_board_result(room)

    snap = room.last_board_result
    assert snap["winner_name"] == "花火"
    assert snap["loser_name"] == "小明"
    assert snap["loser_side"] == "black"


@pytest.mark.asyncio
async def test_board_poster_data_includes_loser_player_wins() -> None:
    """玩家胜时，战报海报副信息带上败者（花火，含难度）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = BLACK
    room.game.draw = False
    room.game.history = [(7, 7, BLACK)]
    manager._snapshot_board_result(room)

    data = _plugin(manager)._board_poster_data(room)

    assert data["title"] == "小明 获胜"
    assert "胜 花火（普通）执白" in data["sub"]


@pytest.mark.asyncio
async def test_board_poster_data_includes_loser_ai_wins() -> None:
    """花火胜时，战报海报副信息带上败者（玩家）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE
    room.game.draw = False
    room.game.history = [(7, 7, BLACK), (7, 8, WHITE)]
    manager._snapshot_board_result(room)

    data = _plugin(manager)._board_poster_data(room)

    assert data["title"] == "花火 获胜"
    assert "胜 小明执黑" in data["sub"]


@pytest.mark.asyncio
async def test_board_poster_data_includes_loser_for_two_humans() -> None:
    """两位真人对弈时，战报海报副信息带上败者（不带花火难度）。"""
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:5",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    first = await manager.join(room)
    second = await manager.join(room)
    await manager.assign_player(room, first.number, "10001")
    await manager.assign_player(room, second.number, "10002")
    room.multiplayer.seats[0].side = "black"
    room.multiplayer.seats[1].side = "white"
    room.multiplayer.seats[0].display_name = "小明"
    room.multiplayer.seats[1].display_name = "小红"
    room.game = GomokuGame(human_color=BLACK)
    room.game.winner = BLACK
    room.game.draw = False
    room.game.history = [(7, 7, BLACK)]
    manager._snapshot_board_result(room)

    data = _plugin(manager)._board_poster_data(room)

    assert data["title"] == "小明 获胜"
    assert "胜 小红执白" in data["sub"]
    assert "花火" not in data["sub"]


@pytest.mark.asyncio
async def test_board_announce_text_includes_loser() -> None:
    """棋类纯文字战报带上败者。"""
    manager = RoomManager(max_private_rooms=0)
    room = await _duel_room(manager)
    room.game.winner = WHITE
    room.game.draw = False
    room.game.history = [(7, 7, BLACK), (7, 8, WHITE)]
    manager._snapshot_board_result(room)

    text = _plugin(manager)._board_announce_text(room)

    assert "花火获胜" in text
    assert "小明落败" in text
