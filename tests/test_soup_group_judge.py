"""玩家出题模式：群里也能裁决，但只有出题人本人可以。"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.models import PlayerSeat
from astrbot_plugin_game_companion_huahuo.qq_bridge import (
    _answer_reply,
    _pending_label,
    _question_reply,
    handle_soup_at_message,
    handle_soup_command,
    judge_soup_from_group,
    parse_soup_command,
    parse_soup_judgement,
    soup_judge_text,
    soup_quote_present,
)
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame

HOST_QQ = "20001"
ASKER_QQ = "20002"
SESSION = "aiocqhttp:group:43001"


async def make_host_room(
    *, pending_kind: str = "question", bind_qq: bool = True
) -> tuple[RoomManager, GameCompanionPlugin, object]:
    """一局玩家出题、且挂着一条待裁决内容的真实房间。"""
    manager = RoomManager(turtle_soup_max_players=4)
    room = await manager.create_room(
        source="group",
        session_id=SESSION,
        platform="aiocqhttp",
        group_id="43001",
        creator_qq=HOST_QQ,
        creator_name="房主",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitors = []
    for _ in range(2):
        visitor = await manager.join(room)
        visitor.connected = True
        visitor.last_seen_at = time.time()
        visitors.append(visitor)
    room.multiplayer.enabled = True
    room.multiplayer.capacity = 2
    room.multiplayer.seats = [
        PlayerSeat(
            number=index + 1,
            visitor_token=visitor.token,
            display_name=f"玩家{index + 1}",
            identity_confirmed=True,
            qq=(HOST_QQ if index == 0 else ASKER_QQ) if bind_qq else "",
        )
        for index, visitor in enumerate(visitors)
    ]
    room.status = "setup"
    await manager.apply_soup_host(room, visitors[0].token)
    await manager.start_soup_host_game(
        room,
        visitors[0].token,
        title="雨夜的伞",
        surface="她每天都带两把伞出门，明明一个人住。邻居问她，她只说习惯了。为什么？",
        solution="她丈夫三年前在雨夜出车祸去世，她一直替他多拿一把。",
        hints=["和上班有关"],
    )
    await manager.begin_soup_host_judgement(
        room,
        "是不是在下雨？" if pending_kind == "question" else "我猜他是守塔人忘了点灯",
        kind=pending_kind,
        source="qq",
        actor_qq=ASKER_QQ,
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    return manager, plugin, room


def make_event(text: str, qq: str = HOST_QQ) -> SimpleNamespace:
    return SimpleNamespace(
        unified_msg_origin=SESSION,
        message_str=text,
        get_sender_id=lambda: qq,
        get_sender_name=lambda: "测试玩家",
        is_at_or_wake_command=False,
    )


@pytest.mark.parametrize(
    "text,kind,expected",
    [
        ("是", "question", "yes"),
        ("是的。", "question", "yes"),
        ("否", "question", "no"),
        ("不是", "question", "no"),
        ("无关", "question", "irrelevant"),
        ("部分正确", "question", "partial"),
        ("是也不是", "question", "compound"),
        ("对", "answer", "correct"),
        ("猜对了！", "answer", "correct"),
        ("不对", "answer", "wrong"),
        # 两类词表互不串用，也不认自由发挥
        ("对", "question", None),
        ("是", "answer", None),
        ("大概是吧", "question", None),
        ("", "question", None),
    ],
)
def test_judgement_words_are_split_by_pending_kind(text, kind, expected) -> None:
    """问题用是/否/无关/部分正确，推理用对/不对，不混用。"""
    assert parse_soup_judgement(text, kind) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/汤裁决 是", ("judge", "是")),
        ("汤 裁决 部分正确", ("judge", "部分正确")),
        ("汤裁决 不对", ("judge", "不对")),
        ("/汤裁决 对", ("judge", "对")),
    ],
)
def test_explicit_command_parses(text, expected) -> None:
    """明确指令走指令表，不参与 @Bot 的整句意图猜测。"""
    assert parse_soup_command(text) == expected


class _Reply:
    pass


class _Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text


@pytest.mark.parametrize(
    "event,quoted,clean",
    [
        (SimpleNamespace(message_str="[CQ:reply,id=12]是"), True, "是"),
        (SimpleNamespace(message_str="[回复 玩家2 的消息]是"), True, "是"),
        (
            SimpleNamespace(message_str="[CQ:at,qq=123] 不对"),
            False,
            "不对",
        ),
        (
            SimpleNamespace(
                message_str="部分正确",
                message_obj=SimpleNamespace(message=[_Reply(), _Plain("部分正确")]),
            ),
            True,
            "部分正确",
        ),
        (
            SimpleNamespace(
                message_str="不对",
                message_obj=SimpleNamespace(message=[], extra={"reply_id": 9}),
            ),
            True,
            "不对",
        ),
        (SimpleNamespace(message_str="是"), False, "是"),
        (SimpleNamespace(message_str="@bot 无关"), False, "无关"),
    ],
)
def test_quote_detection_and_text_cleanup(event, quoted, clean) -> None:
    """引用形态按鸭子类型识别；普通短句不认作引用，CQ 码与 @ 前缀清干净。"""
    assert soup_quote_present(event) is quoted
    assert soup_judge_text(event) == clean


@pytest.mark.asyncio
async def test_only_host_can_judge() -> None:
    """出题人能裁；其他群员、未绑定座位的人一律不放行。"""
    manager, plugin, room = await make_host_room()

    # 引用快捷路径：不是出题人 / 没绑座位都静默放行，不打扰普通聊天
    assert await judge_soup_from_group(plugin, room, ASKER_QQ, "是") is None
    assert await judge_soup_from_group(plugin, room, "99999", "是") is None
    # 明确指令要说清原因
    other = await judge_soup_from_group(
        plugin, room, ASKER_QQ, "是", explicit=True
    )
    assert "只有出题人" in str(other)
    assert "绑定" in str(
        await judge_soup_from_group(plugin, room, "99999", "是", explicit=True)
    )
    assert manager._turtle_soup_game(room).pending_judge  # noqa: SLF001

    reply = await judge_soup_from_group(plugin, room, HOST_QQ, "是")
    assert str(reply).startswith("✅ 出题人裁决：是")
    # 回执要写清裁的是哪条，出题人引用错消息时当场能看出来
    assert "本条裁决对象" in str(reply)
    assert "是不是在下雨？" in str(reply)
    game = manager._turtle_soup_game(room)  # noqa: SLF001
    assert game.pending_judge is None
    assert game.entries[-1].verdict == "yes"
    assert game.entries[-1].source == "qq"


@pytest.mark.asyncio
async def test_no_pending_never_takes_over_normal_chat() -> None:
    """没有待裁决内容时，群里说「是」「对」不能被游戏吞掉。"""
    manager, plugin, room = await make_host_room()
    game = manager._turtle_soup_game(room)  # noqa: SLF001
    game.clear_pending_judge()

    assert await judge_soup_from_group(plugin, room, HOST_QQ, "是") is None
    assert await judge_soup_from_group(plugin, room, HOST_QQ, "对") is None
    assert "没有等待" in str(
        await judge_soup_from_group(plugin, room, HOST_QQ, "是", explicit=True)
    )


@pytest.mark.asyncio
async def test_answer_judgement_uses_correct_and_wrong() -> None:
    """推理裁决用「对 / 不对」，不能用问题那套词。"""
    manager, plugin, room = await make_host_room(pending_kind="answer")
    assert await judge_soup_from_group(plugin, room, HOST_QQ, "是") is None

    reply = await judge_soup_from_group(plugin, room, HOST_QQ, "对")
    assert "推理成立" in str(reply)
    game = manager._turtle_soup_game(room)  # noqa: SLF001
    assert game.solved is True

    manager2, plugin2, room2 = await make_host_room(pending_kind="answer")
    reply2 = await judge_soup_from_group(plugin2, room2, HOST_QQ, "不对")
    assert "还不对" in str(reply2)
    assert manager2._turtle_soup_game(room2).solved is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_wrong_word_gets_usage_hint_on_explicit_command() -> None:
    """明确指令写错词要提示用法，引用快捷路径则静默。"""
    manager, plugin, room = await make_host_room(pending_kind="answer")
    hint = await judge_soup_from_group(
        plugin, room, HOST_QQ, "大概是吧", explicit=True
    )
    assert "裁决要用" in str(hint)
    assert await judge_soup_from_group(plugin, room, HOST_QQ, "大概是吧") is None


@pytest.mark.asyncio
async def test_at_bot_short_verdict_is_a_judgement() -> None:
    """@Bot 直接回一句「是」也能裁决；非出题人的 @Bot 仍是普通聊天。"""
    manager, plugin, room = await make_host_room()
    event = make_event("@bot 是")
    event.is_at_or_wake_command = True

    judged_reply = await handle_soup_at_message(plugin, event)
    assert judged_reply.startswith("✅ 出题人裁决：是")
    assert "是不是在下雨？" in judged_reply
    assert manager._turtle_soup_game(room).pending_judge is None  # noqa: SLF001

    manager2, plugin2, _room2 = await make_host_room()
    other_event = make_event("@bot 是", qq=ASKER_QQ)
    other_event.is_at_or_wake_command = True
    assert await handle_soup_at_message(plugin2, other_event) == ""


@pytest.mark.asyncio
async def test_explicit_command_is_left_to_command_handler() -> None:
    """/汤裁决 是 由指令入口独占，@ 入口不能抢着回第二条。"""
    _manager, plugin, room = await make_host_room()
    event = make_event("/汤裁决 是")
    event.is_at_or_wake_command = True

    assert await handle_soup_at_message(plugin, event) == ""
    # 待裁决内容还在，等指令入口处理
    assert plugin.manager._turtle_soup_game(room).pending_judge  # noqa: SLF001


@pytest.mark.asyncio
async def test_group_judgement_emits_origin_and_group_flag() -> None:
    """群内裁决的事件带来源标记，供局末群通知去重。"""
    manager, plugin, room = await make_host_room()
    emitted: list[tuple[str, dict]] = []

    async def capture(name: str, _room, payload: dict) -> None:
        emitted.append((name, dict(payload)))

    manager.event_callback = capture
    await judge_soup_from_group(plugin, room, HOST_QQ, "否")

    judged = next(payload for name, payload in emitted if name == "soup_host_judged")
    assert judged["origin"] == "qq"
    assert judged["judged_in_group"] is True
    assert judged["entry_seq"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,via_group,expected_calls",
    [
        ("qq", True, 0),  # 群里出题人当场裁决：已有回执，不重复同步
        ("web", True, 1),  # 网页玩家提问，群里需要看到原文
        ("qq", False, 1),  # 出题人在网页裁决，群里没见过结果
        ("", False, 1),  # 旧 payload 也照常同步
    ],
)
async def test_group_judgement_suppresses_duplicate_sync(
    origin, via_group, expected_calls
) -> None:
    """只在「群内裁决 + 群里提问」时省掉那条重复的问答同步。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_bridge = True
    plugin._queue_soup_group_sync = MagicMock()  # noqa: SLF001
    plugin._spawn = lambda coro: coro.close() if hasattr(coro, "close") else None  # noqa: SLF001
    plugin._game_label = lambda _gt: "海龟汤"  # noqa: SLF001
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    room = SimpleNamespace(
        room_id="r",
        game=game,
        game_type="turtle_soup",
        source="group",
        session_id=SESSION,
        status="active",
    )

    await plugin._on_room_event(  # noqa: SLF001
        "soup_host_judged",
        room,
        {"entry_seq": 7, "origin": origin, "judged_in_group": via_group},
    )

    assert plugin._queue_soup_group_sync.call_count == expected_calls  # noqa: SLF001


@pytest.mark.asyncio
async def test_explicit_command_end_to_end() -> None:
    """/汤裁决 否：从指令解析一路走到裁决落库。"""
    manager, plugin, room = await make_host_room()

    reply = await handle_soup_command(plugin, make_event("/汤裁决 否"))

    assert "裁决" in reply
    game = manager._turtle_soup_game(room)  # noqa: SLF001
    assert game.pending_judge is None
    assert game.entries[-1].verdict == "no"


@pytest.mark.asyncio
async def test_quote_shortcut_works_without_mentioning_bot() -> None:
    """引用 + 判定词，不 @Bot 也能裁决；没引用或不是出题人则静默放行。"""
    manager, plugin, room = await make_host_room()
    plugin.turtle_soup_qq_bridge = True

    async def run(text: str, qq: str = HOST_QQ) -> list[str]:
        event = make_event(text, qq=qq)
        event.plain_result = lambda value: value
        event.stop_event = lambda: None
        return [
            result
            async for result in GameCompanionPlugin.soup_qq_quote_judge(plugin, event)
        ]

    shortcut = await run("[CQ:reply,id=12]是")
    assert len(shortcut) == 1
    assert shortcut[0].startswith("✅ 出题人裁决：是")
    assert "是不是在下雨？" in shortcut[0]
    assert manager._turtle_soup_game(room).pending_judge is None  # noqa: SLF001

    manager2, plugin2, _room2 = await make_host_room()
    plugin2.turtle_soup_qq_bridge = True

    async def run2(text: str, qq: str = HOST_QQ) -> list[str]:
        event = make_event(text, qq=qq)
        event.plain_result = lambda value: value
        event.stop_event = lambda: None
        return [
            result
            async for result in GameCompanionPlugin.soup_qq_quote_judge(plugin2, event)
        ]

    assert await run2("是") == []  # 没引用：普通聊天，不接管
    assert await run2("[CQ:reply,id=12]是", qq=ASKER_QQ) == []  # 非出题人
    assert plugin2.manager._turtle_soup_game(_room2).pending_judge  # noqa: SLF001


def test_quote_shortcut_regex_is_strict() -> None:
    """粗筛正则只吃整句判定词，长句子和不相关的内容不会被吞。"""
    import re

    from astrbot_plugin_game_companion_huahuo.qq_bridge import (
        SOUP_JUDGE_WORD_PATTERN,
    )

    pattern = re.compile(
        r"^\s*(?:\[[^\]]*\]\s*)*(?:@[^\s]{1,24}\s+)?\s*"
        r"(?:" + SOUP_JUDGE_WORD_PATTERN + r")"
        r"[。\.！!？?~～、,，\s]*$"
    )
    for text in (
        "是",
        "[CQ:reply,id=12]是",
        "[回复 玩家2 的消息]是",
        "@bot 部分正确",
        "不对。",
        "@bot 猜对了！",
    ):
        assert pattern.match(text), text
    for text in ("是不是和灯塔有关", "是的吧", "我觉得对", "汤 裁决 是", "对啊"):
        assert not pattern.match(text), text


def test_pending_receipts_carry_question_and_guidance() -> None:
    """玩家出题：待裁决回执带原文、提问者和两种引用方式的引导。"""
    question = _question_reply(
        None,
        "和灯塔有关吗？",
        {
            "action": "awaiting_host",
            "pending_text": "和灯塔有关吗？",
            "pending_asker": "玩家2",
            "pending_number": 2,
            "pending_for_bot": False,
            "pending_kind": "question",
            "question_count": 3,
            "question_limit": 35,
            "hint_left": 2,
            "answer_left": 4,
        },
    )
    assert "📋 待裁决" in question
    assert "玩家2（2 号）问「和灯塔有关吗？」" in question
    assert "引用本条" in question and "引用提问人那条消息" in question
    assert "是 / 否 / 无关 / 部分正确" in question
    assert "已问 3/35" in question

    answer = _answer_reply(
        {
            "action": "awaiting_host",
            "pending_text": "我猜他是守塔人忘了点灯",
            "pending_number": 3,
            "pending_for_bot": False,
            "pending_kind": "answer",
        }
    )
    assert "交推理「我猜他是守塔人忘了点灯」" in answer
    assert "对 / 不对" in answer
    assert "是 / 否" not in answer

    # 普通（Bot 出题）模式不该冒出裁决引导
    normal = _question_reply(
        None, "和灯塔有关吗？", {"verdict": "yes", "reply": "是"}
    )
    assert "引用本条" not in normal
    assert "待裁决" not in normal


def test_pending_label_keeps_full_text_and_marks_bot_asker() -> None:
    """原文不按字数砍半句；Bot 自己提问时标注 Bot，别写成某个玩家。"""
    long_text = "是不是因为他忘了点灯导致船只失事？" * 12
    label = _pending_label(
        {
            "pending_text": long_text,
            "pending_asker": "玩家2",
            "pending_number": 2,
            "pending_kind": "question",
        }
    )
    assert long_text in label and "…" not in label

    bot = _pending_label(
        {
            "pending_text": "灯塔当天有人值班吗？",
            "pending_asker": "花火",
            "pending_for_bot": True,
            "pending_kind": "question",
        }
    )
    assert bot.startswith("Bot问「")


@pytest.mark.asyncio
async def test_bot_pending_is_announced_to_group() -> None:
    """网页 / Bot 挂起待裁决时群里补一条可引用的提示。"""
    from unittest.mock import AsyncMock

    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_bridge = True
    plugin.announce_target = lambda _room: "aiocqhttp:GroupMessage:1"  # noqa: SLF001
    plugin._send_to_origin = AsyncMock(return_value=True)  # noqa: SLF001
    payload = {
        "kind": "question",
        "text": "灯塔当天有人值班吗？",
        "asker_name": "花火",
        "asker_number": 0,
        "for_bot": True,
        "source": "web",
    }

    assert await plugin._announce_soup_pending_to_group(  # noqa: SLF001
        SimpleNamespace(), payload
    ) is True
    _room, text = plugin._send_to_origin.await_args.args  # noqa: SLF001
    assert "Bot问「灯塔当天有人值班吗？」" in text
    assert "引用本条" in text and "引用提问人那条消息" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,bridge,expected",
    [("qq", True, 0), ("web", True, 1), ("web", False, 0)],
)
async def test_host_question_event_only_announces_non_group(
    source, bridge, expected
) -> None:
    """群里自己提问的回执已带原文，不重复发；桥关掉时也不发。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.turtle_soup_qq_bridge = bridge
    plugin._game_label = lambda _gt: "海龟汤"  # noqa: SLF001
    spawned: list = []
    plugin._spawn = lambda coro: spawned.append(coro) or coro.close()  # noqa: SLF001
    room = SimpleNamespace(game_type="turtle_soup")

    await plugin._on_room_event(  # noqa: SLF001
        "soup_host_question",
        room,
        {"kind": "question", "text": "和灯塔有关吗？", "source": source},
    )

    assert len(spawned) == expected


@pytest.mark.asyncio
async def test_host_question_event_carries_identity() -> None:
    """事件带上提问者与来源，群里才能分辨该补哪一条提示。"""
    manager, _plugin, room = await make_host_room()
    emitted: list[tuple[str, dict]] = []

    async def capture(name: str, _room, payload: dict) -> None:
        emitted.append((name, dict(payload)))

    manager.event_callback = capture
    manager._turtle_soup_game(room).clear_pending_judge()  # noqa: SLF001
    await manager.begin_soup_host_judgement(
        room, "和灯塔有关吗？", kind="question", source="qq", actor_qq=ASKER_QQ
    )

    payload = next(p for name, p in emitted if name == "soup_host_question")
    assert payload["source"] == "qq"
    assert payload["asker_number"] == 2
    assert payload["text"] == "和灯塔有关吗？"
