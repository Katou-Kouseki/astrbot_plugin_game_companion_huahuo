"""网页问答同步到群：攒批合并，不能一条一条往群里灌。

背景：网页房间才是主战场，但在群里看的人也想跟得上进度。直接每条都发，
五个人轮流问一分钟就是十几条，群里直接被顶掉；完全不发又像「群里没人玩」。
这里的取舍是「攒 12 秒 + 一条最多 4 行」。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    SoupEntry,
    SoupPuzzle,
    TurtleSoupGame,
)


def make_game() -> TurtleSoupGame:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    game.phase = "ready"
    return game


def make_room(*, source: str = "group", game: TurtleSoupGame | None = None):
    return SimpleNamespace(
        room_id="room-1",
        source=source,
        session_id="aiocqhttp:group:1",
        game=game if game is not None else make_game(),
        # 0.5.93 起速报按座位号反查 QQ 头像，会读 room.multiplayer.enabled
        multiplayer=SimpleNamespace(enabled=False),
    )


def add_entry(game: TurtleSoupGame, *, kind: str, prompt: str, response: str, number: int, source: str = "web") -> None:
    game.entries.append(
        SoupEntry(
            kind=kind,  # type: ignore[arg-type]
            prompt=prompt,
            response=response,
            source=source,  # type: ignore[arg-type]
            player_number=number,
        )
    )


def make_plugin(
    *, sync: bool = True, bridge: bool = True, style: str = "text"
) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_sync = sync
    plugin.turtle_soup_qq_bridge = bridge
    plugin.turtle_soup_qq_sync_style = style
    plugin._send_to_origin = AsyncMock(return_value=True)  # noqa: SLF001
    # 真实窗口是 12 秒；测试里不许真等
    plugin.SOUP_SYNC_WINDOW = 0
    return plugin


@pytest.mark.asyncio
async def test_three_web_questions_merge_into_one_message() -> None:
    plugin = make_plugin()
    room = make_room()
    for index, prompt in enumerate(["和灯塔有关吗？", "他是不是瞎了？", "灯灭了才出的事吗？"], start=3):
        add_entry(room.game, kind="question", prompt=prompt, response="不是", number=index)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert text.startswith("🥣 网页那边刚问了 3 条：")
    assert text.count("\n") == 3  # 标题 + 三行
    assert "3号提问：和灯塔有关吗？" in text


@pytest.mark.asyncio
async def test_same_millisecond_same_text_entries_are_not_deduped() -> None:
    """同一毫秒里连着问两句一样的话，两句都要同步；同一条被两个事件触发才去重。"""
    plugin = make_plugin()
    room = make_room()
    game = room.game
    add_entry(game, kind="question", prompt="和灯塔有关吗？", response="不是", number=3)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    add_entry(game, kind="question", prompt="和灯塔有关吗？", response="不是", number=4)
    # 强制同一时间戳：旧的「毫秒 + 类型 + 正文」去重键会把两句当成一句
    for entry in game.entries:
        entry.at = 1700000000.123
    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    # 同一条记录被第二个事件再触发一次，不该重复进队
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert text.startswith("🥣 网页那边刚问了 2 条：")
    assert text.count("和灯塔有关吗") == 2


@pytest.mark.asyncio
async def test_admin_room_bound_to_group_still_syncs_questions() -> None:
    """管理台开房但绑了群号：问答同步不该按开房来源一刀切拦掉。"""
    plugin = make_plugin()
    room = make_room(source="admin")
    room.session_id = "admin:console"
    room.group_id = "20001"
    room.platform = "admin"
    add_entry(room.game, kind="question", prompt="和灯塔有关吗？", response="不是", number=3)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "和灯塔有关吗" in text


@pytest.mark.asyncio
async def test_text_mode_keeps_every_line() -> None:
    """默认纯文字：不截断也不省略，问了 7 条就老老实实列 7 条。"""
    plugin = make_plugin()
    room = make_room()
    for index in range(7):
        add_entry(room.game, kind="question", prompt=f"第{index}个问题", response="不是", number=index + 1)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert text.startswith("🥣 网页那边刚问了 7 条：")
    assert "第0个问题" in text
    assert "第6个问题" in text
    assert "还有 3 条" not in text


@pytest.mark.asyncio
async def test_mixed_kinds_use_neutral_title() -> None:
    plugin = make_plugin()
    room = make_room()
    add_entry(room.game, kind="question", prompt="是灯塔吗？", response="是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    add_entry(room.game, kind="answer", prompt="他是守塔人，忘了点灯", response="接近了", number=2)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert text.startswith("🥣 网页那边刚有 2 条动态：")


@pytest.mark.asyncio
async def test_qq_entries_are_not_echoed_back_to_group() -> None:
    """群里问的已经在群里了，再同步一遍就是重复。"""
    plugin = make_plugin()
    room = make_room()
    add_entry(room.game, kind="question", prompt="是灯塔吗？", response="是", number=1, source="qq")
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_private_room_has_no_group_to_sync_to() -> None:
    plugin = make_plugin()
    room = make_room(source="private")
    room.session_id = "aiocqhttp:private:10001"
    assert plugin._group_announce_session(room) == ""  # noqa: SLF001
    add_entry(room.game, kind="question", prompt="是灯塔吗？", response="是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_switch_off_disables_sync() -> None:
    plugin = make_plugin(sync=False)
    room = make_room()
    add_entry(room.game, kind="question", prompt="是灯塔吗？", response="是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_bridge_master_switch_disables_sync() -> None:
    plugin = make_plugin(bridge=False)
    room = make_room()
    add_entry(room.game, kind="question", prompt="是灯塔吗？", response="是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_spoiler_fragments_are_redacted() -> None:
    puzzle = SoupPuzzle(
        title="灯塔",
        surface="一个男人每天两副碗筷，最后自杀了，为什么？",
        solution="他是灯塔管理员，某天忘了点灯导致船只失事，愧疚自杀，留下碗筷是给死去的妻女。",
        key_facts=["他是灯塔管理员", "忘了点灯导致船只失事", "愧疚自杀"],
        hints=["和职业有关", "和他忘了做的一件事有关", "和一家人有关"],
        acceptable_variants=["他忘了点灯"],
        theme="灯塔",
        trick="失职导致事故",
        content_level="normal",
    )
    game = make_game()
    game.puzzle = puzzle
    plugin = make_plugin()
    room = make_room(game=game)
    add_entry(
        game,
        kind="question",
        prompt="是不是因为他忘了点灯导致船只失事？",
        response="是",
        number=2,
    )
    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "……" in text
    assert "忘了点灯导致船只失事" not in text


@pytest.mark.asyncio
async def test_text_mode_does_not_use_forward_nodes() -> None:
    """默认纯文字：合并转发能力即使可用也不再走，一批只发一条文字。"""
    plugin = make_plugin()
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    room = make_room()
    for index, prompt in enumerate(["和灯塔有关吗？", "他是不是瞎了？"], start=3):
        add_entry(room.game, kind="question", prompt=prompt, response="不是", number=index)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin.context.send_message.assert_not_awaited()
    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_falls_back_to_text_when_forward_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配器不支持合并转发（send_message 抛异常）时，退回纯文本，不能静默丢掉。"""
    import astrbot.api.message_components as mc

    monkeypatch.setattr(mc, "Node", lambda **kwargs: kwargs, raising=False)
    monkeypatch.setattr(mc, "Nodes", lambda nodes: {"nodes": nodes}, raising=False)

    plugin = make_plugin()
    plugin.context = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("not supported"))
    )
    room = make_room()
    add_entry(room.game, kind="question", prompt="和灯塔有关吗？", response="不是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_uses_t2i_image_for_small_batches() -> None:
    """切到横条图模式时，条数少就逐条发图。"""
    plugin = make_plugin(style="image")
    plugin.html_render = AsyncMock(return_value="https://img.example/card.png")
    plugin._download_image_base64 = AsyncMock(return_value="BASE64DATA")
    plugin._send_to_origin_image = AsyncMock(return_value=True)

    room = make_room()
    add_entry(room.game, kind="question", prompt="和灯塔有关吗？", response="不是", number=1)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin_image.assert_awaited_once()  # noqa: SLF001
    # 图上不该出现「网页那边刚问了」这种纯文本标题（那是文字兜底用的）
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_image_mode_falls_back_to_text_for_overflow() -> None:
    """横条图只发前几条，超出上限的不能静默丢掉：用纯文字补齐。"""
    plugin = make_plugin(style="image")
    plugin.html_render = AsyncMock(return_value="https://img.example/card.png")
    plugin._download_image_base64 = AsyncMock(return_value="BASE64DATA")
    plugin._send_to_origin_image = AsyncMock(return_value=True)

    room = make_room()
    for index in range(8):
        add_entry(room.game, kind="question", prompt=f"第{index}个问题", response="不是", number=index + 1)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    assert plugin._send_to_origin_image.await_count == plugin.SOUP_SYNC_MAX_LINES  # noqa: SLF001
    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "第7个问题" in text


def test_image_data_limits_rows_and_keeps_overflow() -> None:
    plugin = make_plugin()
    items = [
        {
            "number": f"{index}号",
            "source": "网页",
            "kind_cn": "提问",
            "prompt": f"问题{index}",
            "result": "不是",
            "tone": "no",
        }
        for index in range(9)
    ]
    data = plugin._soup_sync_image_data(items, "灯塔")  # noqa: SLF001
    assert data["count"] == 9
    assert len(data["items"]) == plugin.SOUP_SYNC_IMAGE_ROWS
    assert data["overflow"] == 3
    assert data["puzzle"] == "灯塔"


@pytest.mark.asyncio
async def test_web_hint_syncs_to_group() -> None:
    """网页玩家申请的提示也同步到群，走「问答同步到群的形态」（默认纯文字）。"""
    plugin = make_plugin()
    room = make_room()
    add_entry(
        room.game,
        kind="hint",
        prompt="申请提示",
        response="重点不在他做了什么",
        number=2,
    )
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "2号" in text
    assert "申请了提示" in text
    assert "重点不在他做了什么" in text
    # 提示没有「是/否」判定，不出现箭头判定
    assert "→" not in text


@pytest.mark.asyncio
async def test_synthetic_reveal_entry_is_not_synced() -> None:
    """揭晓/放弃的合成「结束」条目不再同步成「花火推理」。"""
    plugin = make_plugin()
    room = make_room()
    room.game.entries.append(
        SoupEntry(
            kind="answer",
            prompt="投票揭晓汤底",
            response="本题已揭晓，汤底见下方。",
            source="web",
        )
    )
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001
