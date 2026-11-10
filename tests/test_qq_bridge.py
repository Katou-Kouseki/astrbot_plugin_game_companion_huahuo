"""群聊指令入口：解析要准，回发要说人话，没房间不许乱响应。

最容易出错的两处：`汤` 是单字，会把「汤圆好吃」也认成指令；以及没绑定座位的
玩家一提问就抛 `PermissionError`，群里会看到一串异常原文。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo.qq_bridge import (
    HELP_TEXT,
    handle_soup_command,
    parse_soup_command,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup import TurtleSoupGame


# ---------------- 解析 ----------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("汤 提问 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("汤提问 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("海龟汤提问：是不是和灯塔有关", ("question", "是不是和灯塔有关")),
        ("/汤 推理 他是灯塔管理员", ("answer", "他是灯塔管理员")),
        ("汤 提示", ("hint", "")),
        ("汤提示", ("hint", "")),
        ("汤 揭晓", ("reveal", "")),
        ("汤 附议", ("agree", "")),
        ("汤 反对", ("disagree", "")),
        ("汤 状态", ("status", "")),
        ("汤 查看", ("history", "")),
        ("汤 复盘", ("recap", "")),
        ("汤复盘", ("recap", "")),
        ("汤 回顾", ("recap", "")),
        ("汤 战报", ("recap", "")),
        ("汤", ("start", "")),
        ("海龟汤", ("start", "")),
        ("海龟汤 提示", ("hint", "")),
        # 全角斜杠：AstrBot 指令前缀只认半角 /，这种写法由群聊兜底收下
        ("／汤状态", ("status", "")),
        ("／汤提示", ("hint", "")),
        # 没说动作词的整句按提问处理——群里最常用的就是直接问
        ("汤 是不是和灯塔有关", ("question", "是不是和灯塔有关")),
    ],
)
def test_parse_commands(message: str, expected: tuple[str, str]) -> None:
    assert parse_soup_command(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "汤圆好吃", "汤面来了", "今天喝汤", "你画的汤", "", "海龟", "汤勺在哪",
        # 「海龟汤」开头但只是闲聊，不该被当成一次提问白扣额度
        "海龟汤真好玩", "海龟汤面", "海龟汤底到底是什么",
    ],
)
def test_non_commands_are_ignored(message: str) -> None:
    assert parse_soup_command(message) is None


# ---------------- 无房间 ----------------


def make_event(message: str, *, qq: str = "10001") -> SimpleNamespace:
    return SimpleNamespace(
        message_str=message,
        unified_msg_origin="aiocqhttp:group:1",
        get_sender_id=lambda: qq,
        get_sender_name=lambda: "测试用户",
    )


def make_room(*, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        game_type="turtle_soup",
        status=status,
        player_qq="",
        multiplayer=SimpleNamespace(enabled=False),
        visitors={},
        add_message=lambda *_args, **_kwargs: None,
    )


def make_at_room(
    *,
    seated: bool = True,
    capacity: int = 6,
    seats: int = 1,
    status: str = "active",
) -> SimpleNamespace:
    """多人房：10001 已绑座位（seated=False 时未绑）。"""
    return SimpleNamespace(
        game_type="turtle_soup",
        status=status,
        player_qq="",
        multiplayer=SimpleNamespace(
            enabled=True,
            capacity=capacity,
            seats=[SimpleNamespace(visitor_token=f"t{i}") for i in range(seats)],
            seat_for_qq=lambda qq: (
                SimpleNamespace(visitor_token="tk")
                if seated and qq == "10001"
                else None
            ),
        ),
        visitors={"tk": SimpleNamespace(last_seen_at=0.0, connected=False)},
        add_message=lambda *_args, **_kwargs: None,
    )


def make_plugin(room: SimpleNamespace | None = None) -> SimpleNamespace:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    plugin = SimpleNamespace(
        manager=SimpleNamespace(
            for_session=lambda _sid: [room] if room is not None else [],
            _turtle_soup_game=lambda _room: game,
            request_turtle_soup_hint=AsyncMock(return_value="灯不是他关的。"),
            request_soup_reveal=AsyncMock(
                return_value={"votes": 1, "required": 2, "revealed": False}
            ),
            vote_soup_reveal=AsyncMock(
                return_value={"votes": 2, "required": 2, "revealed": True}
            ),
            # 自动入座用：真实现里这两个会建访客、占座位
            join=AsyncMock(
                return_value=SimpleNamespace(number=7, display_name="", qq="")
            ),
            assign_player=AsyncMock(return_value=None),
        ),
        submit_turtle_soup_question=AsyncMock(
            return_value={"verdict": "yes", "reply": "是", "question_count": 3}
        ),
        submit_turtle_soup_answer=AsyncMock(
            return_value={"solved": False, "coverage": 0.5, "reply": "还差一点"}
        ),
    )
    plugin.manager.game = game  # 便于测试里直接改状态
    return plugin


@pytest.mark.asyncio
async def test_help_works_without_a_room() -> None:
    plugin = make_plugin()
    assert await handle_soup_command(plugin, make_event("汤 帮助")) == HELP_TEXT


@pytest.mark.asyncio
async def test_bare_soup_command_starts_a_room() -> None:
    """单独一句「汤」直接开局，不用先背「汤 开局」这个写法。"""
    plugin = make_plugin(None)
    plugin.manager.start_game = AsyncMock(return_value=True)
    plugin._soup_difficulty = lambda _text: "normal"
    created = SimpleNamespace(
        status="setup",
        player_qq="",
        player_token="pt",
        multiplayer=SimpleNamespace(
            enabled=False, seat_for_qq=lambda _qq: None, seats=[], capacity=0
        ),
        visitors={},
        add_message=lambda *_args, **_kwargs: None,
    )
    plugin._create_or_reuse_room_from_event = AsyncMock(
        return_value=(created, False, False, False)
    )
    plugin._room_url = lambda _room: "https://example.com/room/x"

    reply = await handle_soup_command(plugin, make_event("汤"))

    assert "海龟汤开好了" in reply
    plugin._create_or_reuse_room_from_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_without_room_explains_what_to_do() -> None:
    plugin = make_plugin()
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "还没有进行中的海龟汤" in reply
    plugin.submit_turtle_soup_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_chitchat_gets_no_reply() -> None:
    plugin = make_plugin()
    assert await handle_soup_command(plugin, make_event("汤圆好吃")) == ""


# ---------------- 有房间 ----------------


@pytest.mark.asyncio
async def test_question_goes_through_qq_source() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))

    plugin.submit_turtle_soup_question.assert_awaited_once()
    _room, text = plugin.submit_turtle_soup_question.await_args.args
    assert text == "是灯塔吗"
    kwargs = plugin.submit_turtle_soup_question.await_args.kwargs
    assert kwargs["source"] == "qq"
    assert kwargs["actor_qq"] == "10001"
    assert "是" in reply
    assert "已问 3 次" in reply


@pytest.mark.asyncio
async def test_answer_reports_coverage() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 推理 他是灯塔管理员"))
    assert "还差一点" in reply
    assert "50%" in reply


@pytest.mark.asyncio
async def test_hint_is_returned() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 提示"))
    assert "灯不是他关的" in reply


@pytest.mark.asyncio
async def test_reveal_request_states_remaining_votes() -> None:
    plugin = make_plugin(make_room())
    reply = await handle_soup_command(plugin, make_event("汤 揭晓"))
    assert "1/2" in reply
    assert "还需要 1 人同意" in reply


@pytest.mark.asyncio
async def test_permission_error_becomes_binding_guide() -> None:
    plugin = make_plugin(make_room())
    plugin.submit_turtle_soup_question.side_effect = PermissionError(
        "你的 QQ 尚未绑定到玩家席，请在 WebUI 操作"
    )

    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "绑定玩家席" in reply
    assert "绑定玩家" in reply
    assert "Traceback" not in reply


@pytest.mark.asyncio
async def test_room_must_be_active() -> None:
    """房间只是 finished/rematch_pending 时不该响应提问。"""
    plugin = make_plugin(make_room(status="finished"))
    reply = await handle_soup_command(plugin, make_event("汤 提问 是灯塔吗"))
    assert "还没有进行中的海龟汤" in reply


@pytest.mark.asyncio
async def test_qq_activity_marks_player_online() -> None:
    """群里说话说明人在，在线人数节奏不能被算成缺席。

    但群里发言不等于网页还开着：不能把已关闭的浏览器复活成 connected，
    否则结算后的房间会被误判「还有真人在线」而迟迟不回收。
    """
    from astrbot_plugin_game_companion_huahuo.qq_bridge import _mark_active

    visitor = SimpleNamespace(last_seen_at=0.0, connected=False, left_at=123.0)
    room = SimpleNamespace(
        multiplayer=SimpleNamespace(
            enabled=True,
            seat_for_qq=lambda qq: SimpleNamespace(visitor_token="tk") if qq == "10001" else None,
        ),
        visitors={"tk": visitor},
    )
    _mark_active(room, "10001")
    assert visitor.connected is False
    assert visitor.left_at is None
    assert visitor.last_seen_at > 0


# ---------------- @ 花火 直接说话 ----------------


class TestAtMessage:
    """@ 一句就当提问：判得准，且不能把别人的搭话也吃进去。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("和灯塔有关吗？", "question"),
            ("是不是跟他的工作有关", "question"),
            ("他是不是瞎了", "question"),
            ("他为什么自杀", "question"),
            ("我觉得他是灯塔管理员，因为灯灭了导致船只失事，他愧疚自杀", "answer"),
            ("我的推理是：他其实是守塔人，那天喝多了忘了点灯", "answer"),
            # 不带「我觉得」这类措辞，但明显是一整段陈述 → 也算推理
            ("他是灯塔管理员，灯灭了他才发现，所以自责跳楼", "answer"),
            ("就是灯塔管理员忘了点灯，船撞礁沉了", "answer"),
            # 太短的陈述、以及状态 / 复盘这类词，都不该被当成提问或指令
            ("灯灭了", None),
            ("状态", None),
            ("复盘", None),
            ("帮助", None),
            ("答案", None),
            # 寒暄不该变成提问
            ("你好", None),
            ("在吗", None),
            ("谢谢花火", None),
            ("哈哈哈哈", None),
            ("好的", None),
            ("", None),
            ("。。。", None),
        ],
    )
    def test_classify(self, text: str, expected: str | None) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            classify_at_message,
        )

        assert classify_at_message(text) == expected

    @pytest.mark.asyncio
    async def test_at_message_becomes_question(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )

        assert "已问 3 次" in reply
        kwargs = plugin.submit_turtle_soup_question.await_args.kwargs
        assert kwargs["source"] == "qq"
        assert kwargs["actor_qq"] == "10001"
        # @ 前缀必须剥掉，否则问的内容里会带着「@花火」
        _room, text = plugin.submit_turtle_soup_question.await_args.args
        assert text == "和灯塔有关吗？"

    @pytest.mark.asyncio
    async def test_unbound_player_auto_joins_seat(self) -> None:
        """没绑座位的人 @ 花火提问：自动安排一个空位，然后把问题问出去。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room(seated=False))
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )

        plugin.manager.join.assert_awaited_once()
        plugin.manager.assign_player.assert_awaited_once()
        _room, number, qq = plugin.manager.assign_player.await_args.args
        assert number == 7
        assert qq == "10001"
        assert "已把你加入玩家席" in reply
        plugin.submit_turtle_soup_question.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_full_seat_falls_back_to_binding_guide(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room(seated=False, capacity=2, seats=2))
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )

        assert "坐满" in reply
        plugin.manager.join.assert_not_awaited()
        plugin.submit_turtle_soup_question.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_bind_can_be_disabled(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room(seated=False))
        plugin.turtle_soup_qq_auto_bind = False
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )

        assert "绑定玩家" in reply
        plugin.manager.join.assert_not_awaited()
        plugin.submit_turtle_soup_question.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_message_without_room_is_ignored(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(None)
        assert (
            await handle_soup_at_message(plugin, make_event("@花火 和灯塔有关吗？"))
            == ""
        )

    @pytest.mark.asyncio
    async def test_at_command_is_not_handled_twice(self) -> None:
        """@花火 汤 提问 xxx 交给指令入口，不能两个入口各回一条。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        assert (
            await handle_soup_at_message(plugin, make_event("@花火 汤 提问 是灯塔吗"))
            == ""
        )
        plugin.submit_turtle_soup_question.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_greeting_is_not_sent_as_question(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        assert await handle_soup_at_message(plugin, make_event("@花火 在吗")) == ""
        plugin.submit_turtle_soup_question.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_statement_becomes_answer(self) -> None:
        """@花火 直接讲一段推理（不写「汤 推理」）也要能提交。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 他是灯塔管理员，灯灭了他才发现，所以自责跳楼")
        )

        plugin.submit_turtle_soup_answer.assert_awaited_once()
        plugin.submit_turtle_soup_question.assert_not_awaited()
        kwargs = plugin.submit_turtle_soup_answer.await_args.kwargs
        assert kwargs["source"] == "qq"
        _room, text = plugin.submit_turtle_soup_answer.await_args.args
        assert text.startswith("他是灯塔管理员")
        assert "50%" in reply

    @pytest.mark.asyncio
    async def test_at_question_still_goes_to_question(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        await handle_soup_at_message(plugin, make_event("@花火 是不是和灯塔有关"))
        plugin.submit_turtle_soup_question.assert_awaited_once()
        plugin.submit_turtle_soup_answer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["waiting", "setup", "finished", "rematch_pending", "closed"]
    )
    async def test_at_message_only_works_while_game_is_running(
        self, status: str
    ) -> None:
        """@ 提问/作答只在游戏进行中（active/paused）生效。

        房间还没开局、已经结束、或者停在再来一局确认里时，群里 @ 花火
        应该照常聊天，不能凭空冒出一条「已记录你的提问」。
        """
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room(status=status))
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )
        assert reply == ""
        plugin.submit_turtle_soup_question.assert_not_awaited()
        plugin.submit_turtle_soup_answer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["active", "paused"])
    async def test_at_message_works_when_running_or_paused(self, status: str) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room(status=status))
        reply = await handle_soup_at_message(
            plugin, make_event("@花火 和灯塔有关吗？")
        )
        assert reply != ""
        plugin.submit_turtle_soup_question.assert_awaited_once()


# ---------------- 汤 开局 ----------------

def make_start_plugin() -> SimpleNamespace:
    room = SimpleNamespace(
        room_id="room-new",
        game_type="turtle_soup",
        source="group",
        session_id="aiocqhttp:group:1",
        status="waiting",
        player_qq="",
        player_token="",
        visitors={},
        multiplayer=SimpleNamespace(enabled=False),
        add_message=lambda *args, **kwargs: None,
        touch=lambda: None,
    )

    async def join(_room):
        return SimpleNamespace(number=1, token="tk", qq="", display_name="")

    async def assign(_room, _number, qq, allow_non_numeric=True):
        _room.player_qq = qq
        _room.player_token = "tk"
        return {"seated": True}

    plugin = SimpleNamespace(
        turtle_soup_qq_auto_bind=True,
        _room_url=lambda _room: "https://example.test/room/token",
        _difficulty=lambda value: "normal",
        manager=SimpleNamespace(
            for_session=lambda _sid: [],
            join=AsyncMock(side_effect=join),
            assign_player=AsyncMock(side_effect=assign),
            start_game=AsyncMock(return_value=None),
        ),
    )
    plugin._create_or_reuse_room_from_event = AsyncMock(
        return_value=(room, False, False, False)
    )
    return plugin


def make_group_event(message: str, *, qq: str = "10001") -> SimpleNamespace:
    return SimpleNamespace(
        message_str=message,
        unified_msg_origin="aiocqhttp:group:1",
        get_sender_id=lambda: qq,
        get_sender_name=lambda: "小明",
        get_group_id=lambda: "1",
    )


@pytest.mark.asyncio
async def test_start_command_creates_room_seats_and_starts() -> None:
    plugin = make_start_plugin()
    reply = await handle_soup_command(plugin, make_group_event("汤 开局"))

    plugin._create_or_reuse_room_from_event.assert_awaited_once()
    plugin.manager.join.assert_awaited_once()
    plugin.manager.start_game.assert_awaited_once()
    assert "海龟汤开好了" in reply
    assert "https://example.test/room/token" in reply
    # 标题、链接、玩法各自成段，长链接不再和标题挤在一行
    lines = reply.splitlines()
    assert lines[0] == "🥣 海龟汤开好了"
    assert lines[2] == "🔗 https://example.test/room/token"
    assert "· 汤 提问 你的问题" in reply
    assert "· @我 问一句" in reply
    assert "· @我 交一段推理" in reply


@pytest.mark.asyncio
async def test_start_command_parses_difficulty() -> None:
    plugin = make_start_plugin()
    await handle_soup_command(plugin, make_group_event("汤开局 困难"))
    args = plugin._create_or_reuse_room_from_event.await_args
    assert args.args[1] == "hard"


@pytest.mark.asyncio
async def test_start_command_refuses_when_room_already_running() -> None:
    plugin = make_start_plugin()
    room = SimpleNamespace(game_type="turtle_soup", status="active")
    plugin.manager.for_session = lambda _sid: [room]

    reply = await handle_soup_command(plugin, make_group_event("汤 开局"))

    plugin._create_or_reuse_room_from_event.assert_not_awaited()
    assert "已经有一局" in reply


@pytest.mark.asyncio
async def test_start_command_reports_permission_error() -> None:
    plugin = make_start_plugin()
    plugin._create_or_reuse_room_from_event = AsyncMock(
        side_effect=PermissionError("当前只允许插件配置中的游戏管理员创建群聊房间")
    )
    reply = await handle_soup_command(plugin, make_group_event("汤 开局"))
    assert "开不了房" in reply
    assert "游戏管理员" in reply


# ---------------- 汤 复盘 ----------------


def make_recap_room(*, status: str = "active", finished: bool = False) -> SimpleNamespace:
    """一局已经出好题的海龟汤；问答各留一条。"""
    from astrbot_plugin_game_companion_huahuo.turtle_soup import (
        TurtleSoupGame,
        fallback_puzzle,
    )

    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
    game.puzzle = fallback_puzzle(content_level="normal")
    game.phase = "ready"
    game.record_question("是不是在下雨？", "yes", source="web", matched_facts={0}, player_number=1)
    game.record_question("和伞有关吗？", "no", source="qq", matched_facts=set(), player_number=1)
    if finished:
        game.phase = "finished"
        game.end_reason = "revealed"
    return SimpleNamespace(
        room_id="room-recap",
        game_type="turtle_soup",
        status=status,
        created_at=1.0,
        game=game,
        multiplayer=SimpleNamespace(enabled=False),
        visitors={},
        add_message=lambda *args, **kwargs: None,
        touch=lambda: None,
    )


def plugin_with_recap_room(room: SimpleNamespace) -> SimpleNamespace:
    """把桩插件的 `_turtle_soup_game` 指到这条房间的题上（默认桩是自己的空题）。"""
    plugin = make_plugin(room)
    plugin.manager._turtle_soup_game = lambda _room: room.game
    return plugin


@pytest.mark.asyncio
async def test_recap_does_not_consume_a_question() -> None:
    """「汤 复盘」以前会被兜底成一次提问，白白消耗一次额度。"""
    room = make_recap_room()
    plugin = plugin_with_recap_room(room)
    before = room.game.question_count
    reply = await handle_soup_command(plugin, make_event("汤 复盘", qq="10001"))
    assert "复盘" in reply
    assert room.game.question_count == before
    plugin.submit_turtle_soup_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_recap_hides_solution_until_finished() -> None:
    plugin = plugin_with_recap_room(make_recap_room())
    running = await handle_soup_command(plugin, make_event("汤 复盘", qq="10001"))
    assert "汤底先不给" in running
    assert plugin.manager  # 只读不写

    finished_room = make_recap_room(status="finished", finished=True)
    plugin = plugin_with_recap_room(finished_room)
    done = await handle_soup_command(plugin, make_event("汤 复盘", qq="10001"))
    assert "汤底：" in done
    assert finished_room.game.puzzle.solution[:10] in done


@pytest.mark.asyncio
async def test_recap_finds_finished_room() -> None:
    """刚打完（房间已结束）也要能复盘——这是复盘最常用的时机。"""
    plugin = plugin_with_recap_room(make_recap_room(status="finished", finished=True))
    reply = await handle_soup_command(plugin, make_event("汤 复盘", qq="10001"))
    assert "汤底：" in reply


@pytest.mark.asyncio
async def test_recap_without_any_room_says_so() -> None:
    plugin = make_plugin(None)
    reply = await handle_soup_command(plugin, make_event("汤 复盘", qq="10001"))
    assert "还没有" in reply


# ---------------- @ 花火 说纯动作（提示 / 状态 / 开局） ----------------


class TestAtIntent:
    """@Bot 的整句动作只剩「提示」；其余词改走明确指令，也不能没对局就开局。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("提示", ("hint", "")),
            ("给个提示", ("hint", "")),
            ("提示一下", ("hint", "")),
            ("来点提示", ("hint", "")),
            # 状态 / 复盘 / 揭晓 / 开局 / 帮助 改走 /汤xxx 明确指令
            ("状态", None),
            ("还剩几次", None),
            ("复盘", None),
            ("揭晓", None),
            ("开局", None),
            ("帮助", None),
            ("来局海龟汤", None),
            ("来一局海龟汤", None),
            ("提示是什么意思", None),
            ("和灯塔有关吗", None),
            ("", None),
        ],
    )
    def test_classify_intent(
        self, text: str, expected: tuple[str, str] | None
    ) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            classify_at_intent,
        )

        assert classify_at_intent(text) == expected

    @pytest.mark.asyncio
    async def test_hint_intent_does_not_become_a_question(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        reply = await handle_soup_at_message(plugin, make_event("@花火 提示"))

        assert "灯不是他关的" in reply
        plugin.manager.request_turtle_soup_hint.assert_awaited_once()
        plugin.submit_turtle_soup_question.assert_not_awaited()
        # 不能被当成问题去判定，也不该写进问答记录
        assert "已问" not in reply

    @pytest.mark.asyncio
    async def test_intent_without_room_is_left_to_normal_chat(self) -> None:
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(None)
        assert await handle_soup_at_message(plugin, make_event("@花火 提示")) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "来局海龟汤",
            "开局",
            "帮助",
            "状态",
            "复盘",
            "和灯塔有关吗？",
            "我觉得他是灯塔管理员，灯灭了导致沉船",
        ],
    )
    async def test_no_room_never_takes_over_at_message(self, text: str) -> None:
        """没有进行中的海龟汤时，@Bot 一律是普通聊天：不开局、不弹菜单、不提问。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(None)
        plugin._create_or_reuse_room_from_event = AsyncMock(
            return_value=(make_room(), False, False, False)
        )

        assert await handle_soup_at_message(plugin, make_event(f"@花火 {text}")) == ""
        plugin._create_or_reuse_room_from_event.assert_not_awaited()
        plugin.submit_turtle_soup_question.assert_not_awaited()
        plugin.submit_turtle_soup_answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_action_words_are_left_to_chat_while_playing(self) -> None:
        """有对局时，状态 / 开局 / 复盘这类词也不再靠整句关键词变成指令。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        for text in ("开局", "状态", "复盘", "答案", "灯灭了"):
            plugin = make_plugin(make_at_room())
            assert await handle_soup_at_message(
                plugin, make_event(f"@花火 {text}")
            ) == ""
            plugin.submit_turtle_soup_question.assert_not_awaited()
            plugin.submit_turtle_soup_answer.assert_not_awaited()
            plugin.manager.request_turtle_soup_hint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_answer_still_works_while_playing(self) -> None:
        """有对局时，@Bot 明确像作答的内容依然要提交成推理。"""
        from astrbot_plugin_game_companion_huahuo.qq_bridge import (
            handle_soup_at_message,
        )

        plugin = make_plugin(make_at_room())
        await handle_soup_at_message(
            plugin, make_event("@花火 我觉得他是灯塔管理员，灯灭了导致船只失事")
        )
        plugin.submit_turtle_soup_answer.assert_awaited_once()
        plugin.submit_turtle_soup_question.assert_not_awaited()


def test_help_text_mentions_the_quick_ways_in() -> None:
    """帮助里必须写明怎么开局、怎么 @ 提问，否则没人知道有这些入口。"""
    assert "汤 开局" in HELP_TEXT
    assert "@我" in HELP_TEXT
    assert "@花火" in HELP_TEXT or "@我" in HELP_TEXT
    assert "汤 提示" in HELP_TEXT
