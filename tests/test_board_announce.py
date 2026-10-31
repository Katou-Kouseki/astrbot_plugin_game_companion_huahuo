from __future__ import annotations

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
