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


def make_plugin(*, sync: bool = True, bridge: bool = True) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_sync = sync
    plugin.turtle_soup_qq_bridge = bridge
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
    assert "3号问：和灯塔有关吗？" in text


@pytest.mark.asyncio
async def test_more_than_four_lines_are_summarized() -> None:
    plugin = make_plugin()
    room = make_room()
    for index in range(7):
        add_entry(room.game, kind="question", prompt=f"第{index}个问题", response="不是", number=index + 1)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert text.startswith("🥣 网页那边刚问了 7 条：")
    assert "…还有 3 条" in text
    assert text.count("· ") == plugin.SOUP_SYNC_MAX_LINES


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
async def test_prefers_forward_nodes_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """合并聊天记录可用时，走折叠消息而不是刷一屏纯文本。"""
    import astrbot.api.message_components as mc

    class FakeNode:
        def __init__(self, content=None, name="", uin="0"):
            self.content = content or []
            self.name = name
            self.uin = uin

    class FakeNodes:
        def __init__(self, nodes=None):
            self.nodes = list(nodes or [])

    monkeypatch.setattr(mc, "Node", FakeNode, raising=False)
    monkeypatch.setattr(mc, "Nodes", FakeNodes, raising=False)

    plugin = make_plugin()
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    room = make_room()
    for index, prompt in enumerate(["和灯塔有关吗？", "他是不是瞎了？"], start=3):
        add_entry(room.game, kind="question", prompt=prompt, response="不是", number=index)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin.context.send_message.assert_awaited_once()
    _session, chain = plugin.context.send_message.await_args.args
    nodes = chain.chain[0].nodes
    # 1 个标题节点 + 2 条问答
    assert len(nodes) == 3
    assert nodes[0].name == "花火·海龟汤"
    assert nodes[1].name == "3号提问·网页"
    # 合并成功就不该再补一条纯文本
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


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
    """条数少时优先发图：群里看着显眼，也更容易把人拉进房间。"""
    plugin = make_plugin()
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
async def test_large_batch_prefers_forward_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """条数多时优先合并聊天记录：图片塞不下，折叠记录才装得完整。"""
    import astrbot.api.message_components as mc

    monkeypatch.setattr(mc, "Node", lambda **kwargs: kwargs, raising=False)
    monkeypatch.setattr(mc, "Nodes", lambda nodes: {"nodes": nodes}, raising=False)

    plugin = make_plugin()
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin.html_render = AsyncMock(return_value="https://img.example/card.png")
    plugin._download_image_base64 = AsyncMock(return_value="BASE64DATA")
    plugin._send_to_origin_image = AsyncMock(return_value=True)

    room = make_room()
    for index in range(8):
        add_entry(room.game, kind="question", prompt=f"第{index}个问题", response="不是", number=index + 1)
        plugin._queue_soup_group_sync(room)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin.context.send_message.assert_awaited_once()
    plugin._send_to_origin_image.assert_not_awaited()  # noqa: SLF001


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
