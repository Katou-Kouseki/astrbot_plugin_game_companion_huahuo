"""网页问答同步到群：攒批合并，不能一条一条往群里灌。

背景：网页房间才是主战场，但在群里看的人也想跟得上进度。直接每条都发，
五个人轮流问一分钟就是十几条，群里直接被顶掉；完全不发又像「群里没人玩」。
这里的取舍是「攒 12 秒 + 一条最多 4 行」。
"""

from __future__ import annotations

from pathlib import Path
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
async def test_question_text_is_never_redacted() -> None:
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

    # 群里的人全靠这条同步才知道网页玩家问了什么：原文照发，不遮关键词
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "是不是因为他忘了点灯导致船只失事？" in text
    assert "……" not in text
    assert "→ 是" in text


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


@pytest.mark.asyncio
async def test_long_question_is_sent_in_full() -> None:
    """长问题整句发到群，不按字数砍成半句。"""
    plugin = make_plugin()
    room = make_room()
    long_prompt = (
        "如果他当时其实已经知道灯塔那天会出事，"
        + "却还是照着原来的时刻表去点了灯，" * 6
        + "这到底算不算故意的？"
    )
    add_entry(room.game, kind="question", prompt=long_prompt, response="是", number=3)
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert long_prompt in text
    assert "…" not in text


@pytest.mark.asyncio
async def test_large_batch_lists_every_single_question() -> None:
    """一批 15 条逐条列出，不再补一句「还有 N 条没列出来」。"""
    plugin = make_plugin()
    room = make_room()
    for index in range(15):
        add_entry(
            room.game,
            kind="question",
            prompt=f"第{index}条独立问题？",
            response="不是",
            number=index + 1,
        )
        plugin._queue_soup_group_sync(room)  # noqa: SLF001

    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    for index in range(15):
        assert f"第{index}条独立问题？" in text
    assert "没列出来" not in text
    assert text.startswith("🥣 网页那边刚问了 15 条：")


@pytest.mark.asyncio
async def test_hint_entry_uses_its_own_tone_and_keeps_bulb() -> None:
    """提示保留灯泡，但配色独立，不再和「部分正确」共用橙色。"""
    plugin = make_plugin()
    room = make_room()
    add_entry(
        room.game, kind="hint", prompt="申请提示", response="和职业有关", number=3
    )
    plugin._queue_soup_group_sync(room)  # noqa: SLF001

    item = plugin.__dict__["_soup_sync_state"][room.room_id]["items"][0]
    assert item["tone"] == "hint"
    assert item["result"] == "💡"
    assert item["kind"] == "hint"


def test_three_kinds_get_distinct_strip_styles() -> None:
    """横条图：提问 / 推理 / 提示各用一套 strip 配色。"""
    plugin = make_plugin()

    def item(kind: str, tone: str) -> dict:
        return {
            "kind": kind,
            "kind_cn": {"question": "提问", "answer": "推理", "hint": "提示"}[kind],
            "tone": tone,
            "prompt": "内容",
            "result": "是",
            "number": "1号",
            "number_value": 1,
            "source": "网页",
        }

    pairs = [
        (plugin._soup_sync_image_data([item("question", "yes")], "题")["items"][0], "question", "yes"),
        (plugin._soup_sync_image_data([item("answer", "near")], "题")["items"][0], "answer", "near"),
        (plugin._soup_sync_image_data([item("hint", "hint")], "题")["items"][0], "hint", "hint"),
    ]
    assert [(p["strip_class"], p["tone"]) for p, _, _ in pairs] == [
        (cls, tone) for _, cls, tone in pairs
    ]
    assert len({p["strip_class"] for p, _, _ in pairs}) == 3


def test_history_text_lists_every_question_without_redaction() -> None:
    """群里 /汤查看 列出全部问答原文，不遮关键词、不只给最近 5 条。"""
    from astrbot_plugin_game_companion_huahuo.qq_bridge import _history_text

    game = make_game()
    game.puzzle = SimpleNamespace(title="灯塔", surface="汤面")
    for index in range(8):
        add_entry(
            game,
            kind="question",
            prompt=f"第{index}问：是不是因为他忘了点灯导致船只失事？",
            response="是",
            number=index + 1,
        )
    text = _history_text(SimpleNamespace(), game)

    assert "本局全部问答（8 条）" in text
    assert text.count("忘了点灯导致船只失事") == 8
    assert "……" not in text


@pytest.mark.asyncio
async def test_host_verdict_from_group_question_is_synced() -> None:
    """玩家出题：群里问的问题经出题人裁决，判定结果必须发到群。"""
    plugin = make_plugin()
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None
    room = make_room()
    add_entry(
        room.game,
        kind="question",
        prompt="是不是和灯塔有关？",
        response="是",
        number=2,
        source="qq",
    )
    room.game.entries[-1].verdict = "yes"
    seq = room.game.entries[-1].seq

    plugin._queue_soup_group_sync(room, host_verdict=True, entry_seq=seq)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "群里玩家2号提问" in text
    assert "是不是和灯塔有关？" in text
    assert "出题人裁决：是" in text


@pytest.mark.asyncio
async def test_host_verdict_survives_reveal_end_entry() -> None:
    """额度用尽那局末尾多一条合成「揭晓」，不能把最后一条裁决结果挡掉。"""
    plugin = make_plugin()
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None
    room = make_room()
    add_entry(
        room.game,
        kind="question",
        prompt="最后一次提问？",
        response="是",
        number=2,
        source="qq",
    )
    room.game.entries[-1].verdict = "yes"
    seq = room.game.entries[-1].seq
    room.game.reveal(reason="answer_limit", source="qq")

    plugin._queue_soup_group_sync(room, host_verdict=True, entry_seq=seq)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001

    plugin._send_to_origin.assert_awaited_once()  # noqa: SLF001
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "最后一次提问？" in text
    assert "出题人裁决：是" in text
    assert "汤底见下方" not in text


@pytest.mark.asyncio
async def test_normal_mode_still_skips_group_own_questions() -> None:
    """普通模式没放宽：群里自己问、当场就有判定的，不再重复发一遍。"""
    plugin = make_plugin()
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None
    room = make_room()
    add_entry(
        room.game,
        kind="question",
        prompt="和灯塔有关吗？",
        response="不是",
        number=2,
        source="qq",
    )
    room.game.entries[-1].verdict = "no"

    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    await plugin._flush_soup_group_sync(room.room_id)  # noqa: SLF001
    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


def test_awaiting_host_progress_counts_the_pending_entry() -> None:
    """玩家出题：挂起中的提问/推理还没落库，群回复进度要算上它。"""
    plugin = make_plugin()
    game = make_game()
    game.question_count = 2
    game.bot_question_count = 1
    before = plugin._soup_progress(game, None)  # noqa: SLF001

    question = plugin._awaiting_host_progress(game, None, "question")  # noqa: SLF001
    assert question["question_count"] == before["question_count"] + 1
    assert question["question_left"] == before["question_left"] - 1
    assert question["answer_count"] == before["answer_count"]

    answer = plugin._awaiting_host_progress(game, None, "answer")  # noqa: SLF001
    assert answer["answer_count"] == before["answer_count"] + 1
    assert answer["answer_left"] == before["answer_left"] - 1
    assert answer["question_count"] == before["question_count"]


def test_sync_strip_carries_frozen_progress() -> None:
    """横条图带上入队时冻结的「已问 / 提示剩 / 推理剩」。"""
    plugin = make_plugin()
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None
    room = make_room()
    add_entry(room.game, kind="question", prompt="和灯塔有关吗？", response="是", number=2)
    room.game.entries[-1].verdict = "yes"

    plugin._queue_soup_group_sync(room)  # noqa: SLF001
    item = plugin.__dict__["_soup_sync_state"][room.room_id]["items"][0]
    assert "已问" in item["progress"]
    assert "提示剩" in item["progress"]
    assert "推理剩" in item["progress"]

    data = plugin._soup_sync_image_data([item], "灯塔")  # noqa: SLF001
    assert data["items"][0]["progress"] == item["progress"]


def _reveal_room():
    room = make_room()
    room.game.puzzle = SimpleNamespace(
        title="灯塔", surface="汤面", solution="守塔人忘了点灯", hints=[]
    )
    return room


def _reveal_plugin(*, enabled: bool, auto: bool):
    plugin = make_plugin()
    plugin.turtle_soup_qq_bridge = True
    plugin.turtle_soup_group_announce_enabled = enabled
    plugin.turtle_soup_group_announce_auto = auto
    plugin._room_url = lambda _room: "https://example.com/room/x"  # noqa: SLF001
    return plugin


@pytest.mark.asyncio
async def test_web_reveal_is_skipped_when_auto_report_covers_it() -> None:
    """结算自动播报会说明结果并给链接，网页揭晓不再补一条重复通知。"""
    plugin = _reveal_plugin(enabled=True, auto=True)

    await plugin._announce_soup_reveal(_reveal_room(), with_solution=False)  # noqa: SLF001

    plugin._send_to_origin.assert_not_awaited()  # noqa: SLF001


@pytest.mark.asyncio
async def test_web_reveal_keeps_link_when_report_is_off() -> None:
    """播报关掉时仍要兜底，告诉群友去哪看完整汤底。"""
    plugin = _reveal_plugin(enabled=False, auto=False)

    await plugin._announce_soup_reveal(_reveal_room(), with_solution=False)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "本局汤底已揭晓" in text
    assert "想看完整汤底" in text


@pytest.mark.asyncio
async def test_group_seconded_reveal_sends_solution_without_link() -> None:
    """群里附议才给完整汤底；答案都给了就不再挂链接引导。"""
    plugin = _reveal_plugin(enabled=True, auto=True)

    await plugin._announce_soup_reveal(_reveal_room(), with_solution=True)  # noqa: SLF001

    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "守塔人忘了点灯" in text
    assert "想看完整汤底" not in text


def _host_game(*, title: str = "1号的汤", hints=()):
    from dataclasses import replace as _replace

    from astrbot_plugin_game_companion_huahuo.turtle_soup import fallback_puzzle

    game = make_game()
    game.puzzle = _replace(
        fallback_puzzle(content_level="normal"), title=title, hints=tuple(hints)
    )
    return game


def _host_room(game):
    room = make_room(game=game)
    room.game_type = "turtle_soup"
    room.touch = lambda: None
    return room


def _host_payload(game, *, need_title: bool = True, need_hints: bool = True) -> dict:
    return {
        "surface": game.puzzle.surface,
        "solution": game.puzzle.solution,
        "content_level": "normal",
        "owner": "20001",
        "need_title": need_title,
        "need_hints": need_hints,
    }


@pytest.mark.asyncio
async def test_host_puzzle_is_recorded_as_custom_submission(tmp_path: Path) -> None:
    """玩家出题默认进投稿库，管理台题库管理能看能改能删。"""
    from astrbot_plugin_game_companion_huahuo.soup_custom import CustomSoupStore

    plugin = make_plugin()
    plugin.soup_custom_store = CustomSoupStore(tmp_path / "custom.json")
    spawned: list = []
    plugin._spawn = lambda coro: spawned.append(coro)  # noqa: SLF001
    game = _host_game()
    payload = _host_payload(game)

    await plugin._on_room_event(  # noqa: SLF001
        "soup_host_puzzle_submitted", _host_room(game), payload
    )

    store = plugin._soup_custom()  # noqa: SLF001
    assert len(store.entries) == 1
    assert store.entries[0].surface == game.puzzle.surface
    assert store.entries[0].solution == game.puzzle.solution
    assert store.entries[0].owner == "20001"
    assert len(spawned) == 1
    spawned[0].close()

    # 同一道题重复开局不重复投稿
    await plugin._on_room_event(  # noqa: SLF001
        "soup_host_puzzle_submitted", _host_room(game), payload
    )
    assert len(store.entries) == 1
    spawned[1].close()


@pytest.mark.asyncio
async def test_host_puzzle_enrichment_fills_only_blank_fields() -> None:
    """后台模型只补留空的标题和提示，玩家填过的不覆盖。"""
    plugin = make_plugin()
    game = _host_game()
    plugin._llm_gen_neutral = AsyncMock(  # noqa: SLF001
        return_value='{"title":"雨夜的第二把伞","hints":["和她丈夫有关","不是给活人","是替他拿的"]}'
    )

    await plugin._enrich_soup_host_puzzle(  # noqa: SLF001
        _host_room(game), _host_payload(game)
    )

    assert game.puzzle.title == "雨夜的第二把伞"
    assert len(game.puzzle.hints) == 3
    assert game.puzzle.hints[0] == "和她丈夫有关"

    # 标题已填：模型返回的标题不能覆盖
    filled = _host_game(title="玩家自己起的名", hints=())
    plugin._llm_gen_neutral = AsyncMock(  # noqa: SLF001
        return_value='{"title":"不该覆盖","hints":["提示一"]}'
    )
    await plugin._enrich_soup_host_puzzle(  # noqa: SLF001
        _host_room(filled), _host_payload(filled, need_title=False)
    )
    assert filled.puzzle.title == "玩家自己起的名"
    assert filled.puzzle.hints == ("提示一",)


@pytest.mark.asyncio
async def test_host_puzzle_enrichment_skips_swapped_or_bad_results() -> None:
    """换题后不回填；模型结果不可用时保留占位值。"""
    plugin = make_plugin()
    game = _host_game()
    old_puzzle = game.puzzle
    plugin._llm_gen_neutral = AsyncMock(return_value="不是 JSON")  # noqa: SLF001
    await plugin._enrich_soup_host_puzzle(  # noqa: SLF001
        _host_room(game), _host_payload(game)
    )
    assert game.puzzle is old_puzzle

    # 模型跑完时题面已经换了：不能把上一题的标题贴到新题上
    from dataclasses import replace as _replace

    async def swap_then_answer(_prompt):
        game.puzzle = _replace(game.puzzle, surface="另一道题的汤面")
        return '{"title":"旧题标题","hints":["旧提示"]}'

    plugin._llm_gen_neutral = swap_then_answer  # noqa: SLF001
    await plugin._enrich_soup_host_puzzle(  # noqa: SLF001
        _host_room(game), _host_payload(game)
    )
    assert game.puzzle.title == old_puzzle.title


def test_soup_poster_questions_fill_up_from_all_entries() -> None:
    """只有一条「是」时，服务端战报「他们问过」也要补齐，不能孤零零一条。"""
    entries = [
        {"kind": "question", "prompt": "第一问？", "verdict": "no"},
        {"kind": "question", "prompt": "第二问？", "verdict": "yes"},
        {"kind": "question", "prompt": "第三问？", "verdict": "irrelevant"},
        {"kind": "question", "prompt": "第四问？", "verdict": "no"},
        {"kind": "question", "prompt": "第五问？", "verdict": "no"},
    ]

    picked = GameCompanionPlugin._soup_pick_questions(  # noqa: SLF001
        {"entries": entries}, limit=4
    )
    assert len(picked) == 4
    assert picked[0]["prompt"] == "第二问？", "问出关键事实的排最前"
    assert len({item["prompt"] for item in picked}) == 4

    # 重复提问仍只留一条，且不影响补齐到 4 条
    dup = entries + [{"kind": "question", "prompt": "第一问", "verdict": "yes"}]
    picked2 = GameCompanionPlugin._soup_pick_questions(  # noqa: SLF001
        {"entries": dup}, limit=4
    )
    assert len(picked2) == 4
    assert sum(1 for item in picked2 if item["prompt"].rstrip("？?") == "第一问") == 1
