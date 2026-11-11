"""汤底揭晓不该在群里出现两条一样的消息。

群里发「汤 附议」凑齐票数时，这条指令的回执已经把完整汤底写全了
（「🥣 票数够了，汤底揭晓：…」）；与此同时 manager 又 emit 了
`soup_revealed`，主程序照着播一遍，就成了群里连着两条相同的汤底。
发起揭晓、附议但票数还没够，这两种情况同理。

约定：动作入口自己已经把结果回给群里时传 `announce=False`，主程序不再广播；
网页、超时、Bot 自玩这些没有回执的路径保持默认 `True`，广播仍是唯一渠道。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import astrbot_plugin_game_companion_huahuo.main as main_module
from astrbot_plugin_game_companion_huahuo.qq_bridge import _dispatch
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping


def make_puzzle():  # noqa: ANN201
    return puzzle_from_mapping(
        {
            "title": "列车",
            "surface": "他在列车上收到一条短信后就崩溃了，为什么？",
            "solution": (
                "有人往他手机上发了一条恶作剧短信，说这趟列车即将脱轨、他逃不掉了。"
                "周围没人像在开玩笑，他无法确认真假，恐惧一下就把人压垮了。"
            ),
            "key_facts": ["收到短信", "说列车要脱轨", "他相信了"],
            "acceptable_variants": [],
            "hints": ["和手机有关", "和列车安全有关", "他没法确认"],
            "theme": "列车",
            "trick": "把恶作剧短信当真",
        },
        content_level="all_ages",
    )


async def build_room(manager: RoomManager, *, bind: str = ""):  # noqa: ANN201
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:30001",
        platform="aiocqhttp",
        group_id="30001",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
        turtle_soup_mode="bot_host",
    )
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "")
    if room.status == "setup":
        await manager.start_game(room, player.token, "")
    if room.game.puzzle is None:
        await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    if bind:
        await manager.assign_player(
            room, player.number, bind, allow_non_numeric=True
        )
    return room


class FakeManager:
    """只记录调用参数，用来确认 announce 有没有一路传下去。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _turtle_soup_game(self, _room):  # noqa: ANN001
        return None

    async def request_soup_reveal(self, _room, **kwargs):  # noqa: ANN001
        self.calls.append(("request", kwargs))
        return {"revealed": True}

    async def vote_soup_reveal(self, _room, **kwargs):  # noqa: ANN001
        self.calls.append(("vote", kwargs))
        return {"revealed": True}


def make_plugin() -> tuple[main_module.GameCompanionPlugin, list, list]:
    plugin = object.__new__(main_module.GameCompanionPlugin)
    decisions: list[dict[str, Any]] = []
    votes: list[str] = []

    plugin._announce_soup_decision = lambda _room, _payload, **kw: decisions.append(  # noqa: SLF001
        kw
    )
    plugin._announce_soup_reveal_vote = (  # noqa: SLF001
        lambda _room, _payload, *, kind: votes.append(kind)
    )
    plugin._spawn = lambda coro: coro  # noqa: SLF001
    return plugin, decisions, votes


@pytest.mark.asyncio
async def test_group_reveal_command_does_not_duplicate_the_solution() -> None:
    """群指令拿到的回执已经带汤底，事件广播就得让位。"""
    plugin, decisions, _votes = make_plugin()
    room = SimpleNamespace(game_type="turtle_soup")

    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {
            "source": "qq",
            "reason": "revealed",
            "qq_participated": True,
            "announce_in_group": False,
        },
    )
    assert decisions == [], "回执里已经有汤底了，不能再播一条一样的"

    # 网页 / 超时 / Bot 自玩没有回执，广播必须照旧，否则群里静默
    await plugin._on_room_event(  # noqa: SLF001
        "soup_revealed",
        room,
        {"source": "system", "reason": "timeout"},
    )
    assert decisions == [{"with_solution": False}]


@pytest.mark.asyncio
async def test_group_vote_receipts_do_not_duplicate_the_ballot_notice() -> None:
    """「已发起揭晓（1/2）」「已记录你的意见（1/2）」也不该再被广播一遍。"""
    plugin, _decisions, votes = make_plugin()
    room = SimpleNamespace(game_type="turtle_soup")

    for event_name in ("soup_reveal_requested", "soup_reveal_voted"):
        await plugin._on_room_event(  # noqa: SLF001
            event_name,
            room,
            {"votes": 1, "required": 2, "announce_in_group": False},
        )
    assert votes == []

    # 网页玩家发起时群里没有任何回执，广播这条就是唯一的信息来源
    await plugin._on_room_event(  # noqa: SLF001
        "soup_reveal_requested",
        room,
        {"votes": 1, "required": 2},
    )
    assert votes == ["requested"]


@pytest.mark.asyncio
async def test_bridge_forwards_announce_to_the_manager() -> None:
    """群里发指令时 announce 默认是 True，回执自己会发到群。"""
    manager = FakeManager()
    plugin = SimpleNamespace(manager=manager)
    room = SimpleNamespace()

    await _dispatch(plugin, room, "reveal", "", "10001")
    await _dispatch(plugin, room, "agree", "", "10001")
    assert [name for name, _ in manager.calls] == ["request", "vote"]
    for _name, kwargs in manager.calls:
        assert kwargs["announce"] is True

    # 只交给模型转述回执的路径，要让主程序去广播
    manager.calls.clear()
    await _dispatch(plugin, room, "reveal", "", "10001", announce=False)
    assert manager.calls[0][1]["announce"] is False


@pytest.mark.asyncio
async def test_manager_marks_payloads_so_main_knows_who_already_replied() -> None:
    """会触发揭晓的入口都要把「回执是否已发到群」带进事件负载。"""
    manager = RoomManager()
    room = await build_room(manager)
    await manager.assign_player(
        room, room.multiplayer.seats[0].number, "10001", allow_non_numeric=True
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name, _room, payload):  # noqa: ANN001
        events.append((name, dict(payload or {})))

    manager._emit = emit  # noqa: SLF001

    # 单人房：发起即揭晓，事件里必须标明「回执已发到群」
    await manager.request_soup_reveal(
        room, source="qq", actor_qq="10001", announce=False
    )
    revealed = [payload for name, payload in events if name == "soup_revealed"]
    assert len(revealed) == 1
    assert revealed[0]["announce_in_group"] is False

    # 两人房：发起后还要等附议，凑够票的那一票同样要标明
    events.clear()
    manager2 = RoomManager(turtle_soup_max_players=6)
    pair = await build_room(manager2, bind="10001")
    second = await manager2.join(pair)
    await manager2.assign_player(
        pair, second.number, "10002", allow_non_numeric=True
    )
    manager2._emit = emit  # noqa: SLF001

    await manager2.request_soup_reveal(
        pair, source="qq", actor_qq="10001", announce=False
    )
    await manager2.vote_soup_reveal(
        pair, agree=True, source="qq", actor_qq="10002", announce=False
    )

    ballot = [
        (name, payload)
        for name, payload in events
        if name in {"soup_reveal_requested", "soup_reveal_voted", "soup_revealed"}
    ]
    assert [name for name, _ in ballot] == [
        "soup_reveal_requested",
        "soup_revealed",
    ], "两人房要先发起、再由另一人附议凑票"
    for name, payload in ballot:
        assert payload["announce_in_group"] is False, name
