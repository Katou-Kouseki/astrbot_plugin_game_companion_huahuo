from __future__ import annotations

import base64

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE
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

