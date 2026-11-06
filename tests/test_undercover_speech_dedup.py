from __future__ import annotations

import pytest

from astrbot_plugin_game_companion_huahuo.main import (
    GameCompanionPlugin,
    _uc_ai_fallback,
    _uc_speech_is_repeat,
)
from astrbot_plugin_game_companion_huahuo.models import PlayerSeat, _visitor_uid
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.undercover import UndercoverGame

SAME_LINE = "说起来昨天我还用到它了，当时就觉得挺顺手的"


def test_repeat_detects_identical_and_near_identical() -> None:
    history = ["说起来昨天我还用到它了，当时就觉得挺顺手的"]
    assert _uc_speech_is_repeat("说起来昨天我还用到它了，当时就觉得挺顺手的", history)
    # 只改一两个字也算复读
    assert _uc_speech_is_repeat("说起来昨天我还用到它了，当时觉得挺顺手的", history)
    # 完全换一个角度则不算
    assert not _uc_speech_is_repeat("我妈那边也备了一个，用起来差不多", history)


def test_fallback_skips_lines_already_used() -> None:
    """连续多轮兜底发言不能退化成同一句（PK 加赛后复读的直接原因）。"""
    used: list[str] = []
    for round_no in range(1, 9):
        line = _uc_ai_fallback("civilian", round_no, 1, used=used)
        assert not _uc_speech_is_repeat(line, used), f"第{round_no}轮兜底与自己历史重复：{line}"
        used.append(line)
    assert len(set(used)) == 8


async def _make_room(manager: RoomManager):
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:39002",
        platform="aiocqhttp",
        group_id="39002",
        creator_qq="10002",
        creator_name="乙",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )
    visitors = []
    for _ in range(2):
        visitor = await manager.join(room)
        room.multiplayer.seats.append(
            PlayerSeat(
                visitor_token=visitor.token,
                display_name=visitor.display_name,
            )
        )
        visitors.append(visitor)
    game = UndercoverGame()
    game.attach_players(
        [(index + 1, visitor.qq, visitor.display_name) for index, visitor in enumerate(visitors)],
        uids=[_visitor_uid(visitor.token) for visitor in visitors],
    )
    game.civilian_word = "牙刷"
    game.undercover_word = "牙膏"
    game.players[0].camp = "civilian"
    game.players[0].word = "牙刷"
    game.players[1].camp = "undercover"
    game.players[1].word = "牙膏"
    game._start_new_round()
    game.speaking_order = [1, 2]
    game.current_speaker_index = 0
    room.game = game
    room.status = "active"
    return room, visitors, game


def _make_plugin(manager: RoomManager, reply: str) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager

    async def fake_generate(*_args, **_kwargs) -> str:
        return reply

    plugin._generate_persona_text = fake_generate  # type: ignore[method-assign]
    return plugin


@pytest.mark.asyncio
async def test_pk_round_speech_never_repeats_previous_round() -> None:
    """PK 加赛轮不能把上一轮那句原样再说一遍——模型复读时必须被本地拦下。"""
    manager = RoomManager()
    room, _visitors, game = await _make_room(manager)
    plugin = _make_plugin(manager, SAME_LINE)
    seat = room.multiplayer.seats[0]

    await plugin._undercover_ai_do_speech(room, seat)
    first = game.rounds[-1].speeches[0].content
    assert first

    # 同票进入 PK 子轮，又轮到同一个 AI 发言，模型依旧返回同一句
    game.continue_pk([1, 2])
    game.current_speaker_index = game.speaking_order.index(1)
    await plugin._undercover_ai_do_speech(room, seat)
    second = game.rounds[-1].speeches[0].content

    assert second != first
    assert not _uc_speech_is_repeat(second, [first])
    assert second in [line for line in _pool_lines()]


def _pool_lines() -> list[str]:
    return [_uc_ai_fallback("civilian", 1, index) for index in range(6)]


@pytest.mark.asyncio
async def test_distinct_generation_is_kept_as_is() -> None:
    manager = RoomManager()
    room, _visitors, game = await _make_room(manager)
    plugin = _make_plugin(manager, "我妈那边也备了一个，用起来差不多")
    seat = room.multiplayer.seats[0]

    await plugin._undercover_ai_do_speech(room, seat)

    assert game.rounds[-1].speeches[0].content == "我妈那边也备了一个，用起来差不多"
