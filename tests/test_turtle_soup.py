from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.server import GameRoomServer
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    TurtleSoupGame,
    fallback_puzzle,
    puzzle_from_mapping,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import (
    parse_answer_judgment,
    parse_question_judgment,
)


def new_plugin() -> GameCompanionPlugin:
    """造一个最小插件实例，并给它独立的临时数据目录。

    `__new__` 跳过了 __init__，实例没有 data_dir；题库账本与补全缓存按
    `Path(getattr(self, "data_dir", Path(".")))` 会落到当前工作目录——跑一次测试
    就往仓库外面写一份《已经出过哪些题》，再跑一次就变成「题目重复」而失败。
    这里给每次调用一个空目录，测试之间互不影响。
    """
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.data_dir = Path(tempfile.mkdtemp(prefix="soup-test-"))
    return plugin


def puzzle_data() -> dict[str, object]:
    return {
        "title": "测试题",
        "surface": "一个人每天都在同一时间打开一扇没有上锁的门，但从不走进去。为什么？",
        "solution": "那是一间温室的通风门。他按时打开门是为了给植物换气，入口在另一侧，所以不从这里进入。",
        "key_facts": ["门用于温室通风", "他在照料植物", "真正入口位于另一侧"],
        "acceptable_variants": ["把温室描述为需要通风的种植房也可以"],
        "hints": [
            "门的用途不只是通行。",
            "这件事与室内环境有关。",
            "房间里主要是植物。",
        ],
        "theme": "植物照料",
        "trick": "把通风门误认为普通入口",
    }


def make_puzzle():
    return puzzle_from_mapping(puzzle_data(), content_level="all_ages")


async def make_soup_room(manager: RoomManager):
    room = await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitor = await manager.join(room)
    await manager.claim_and_start(room, visitor.token, "")
    if room.game is None:
        # 0.5.84 起海龟汤不再「入座即开局」：要房内玩家点「开始出题」才生成题目。
        # 这里模拟那一次点击，测试关注的是点击之后的行为。
        await manager.start_game(room, visitor.token, "")
    assert isinstance(room.game, TurtleSoupGame)
    return room, visitor, room.game


def test_active_snapshot_never_exposes_solution_or_key_facts() -> None:
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="all_ages")
    game.set_puzzle(make_puzzle())

    active = game.snapshot()
    assert active["puzzle"] == {
        "title": "测试题",
        "surface": puzzle_data()["surface"],
        "content_level": "all_ages",
    }

    game.give_up(source="web")
    finished = game.snapshot()
    assert finished["puzzle"]["solution"] == puzzle_data()["solution"]
    assert finished["puzzle"]["key_facts"] == puzzle_data()["key_facts"]


def test_judge_parsers_reject_unknown_verdict_and_overconfident_answer() -> None:
    verdict, facts, solved_flag = parse_question_judgment(
        '{"verdict":"maybe","matched_facts":[0,9,"1"]}', fact_count=3
    )
    # 「自信但没说到点子上」仍要拒绝；0.7 覆盖且命中全部事实的情况在
    # tests/test_turtle_soup_upgrade.py 里另有正向用例（判定已放宽，见 CHANGELOG）
    solved, coverage, answer_facts = parse_answer_judgment(
        '{"solved":true,"coverage":0.4,"matched_facts":[0]}', fact_count=3
    )

    assert verdict == "irrelevant"
    assert facts == {0, 1}
    assert not solved_flag
    assert not solved
    assert coverage == 0.4
    assert answer_facts == {0}

    _, _, solved_flag2 = parse_question_judgment(
        '{"verdict":"yes","solved":true}',
        fact_count=3,
    )
    _, _, solved_flag3 = parse_question_judgment(
        '{"verdict":"yes","solved":"true"}',
        fact_count=3,
    )
    assert solved_flag2 is True
    assert solved_flag3 is False


@pytest.mark.asyncio
async def test_soup_start_is_async_and_generation_is_not_idle_expired() -> None:
    events: list[str] = []

    async def callback(event, _room, _payload):
        events.append(event)

    manager = RoomManager(idle_timeout=1, event_callback=callback)
    room, _visitor, game = await make_soup_room(manager)
    room.last_activity_at = 1.0

    assert game.phase == "preparing"
    # 入座不再自动开局，会先来一条 seats_changed；这里只关心生成请求只发一次
    assert events.count("soup_generation_requested") == 1
    assert await manager.sweep_expired(now=100.0) == []


@pytest.mark.asyncio
async def test_only_current_player_can_ask_and_questions_are_shared() -> None:
    manager = RoomManager()
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())
    spectator = await manager.join(room)

    with pytest.raises(PermissionError, match="玩家席"):
        await manager.begin_turtle_soup_interaction(
            room,
            "这是温室吗？",
            source="web",
            visitor_token=spectator.token,
            limit=200,
        )

    active_game, question = await manager.begin_turtle_soup_interaction(
        room,
        "这是温室吗？",
        source="web",
        visitor_token=visitor.token,
        limit=200,
    )
    await manager.resolve_turtle_soup_question(
        room,
        active_game,
        question,
        "yes",
        source="web",
        matched_facts={0},
    )

    snapshot = room.public_snapshot(spectator.token)
    assert snapshot["game"]["question_count"] == 1
    assert snapshot["game"]["entries"][0]["response"] == "是"
    assert snapshot["game"]["discovered_fact_count"] == 1


@pytest.mark.asyncio
async def test_hints_are_progressive_and_honor_configured_limit() -> None:
    manager = RoomManager(turtle_soup_follow_difficulty=False, turtle_soup_max_hints=2)
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())

    first = await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )
    second = await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )

    assert first != second
    assert game.snapshot()["hint_limit"] == 2
    with pytest.raises(ValueError, match="提示次数已经用完"):
        await manager.request_turtle_soup_hint(
            room, source="web", visitor_token=visitor.token
        )


@pytest.mark.asyncio
async def test_solving_updates_only_turtle_soup_score_and_stats() -> None:
    manager = RoomManager()
    room, visitor, game = await make_soup_room(manager)
    await manager.complete_turtle_soup_generation(room, game, make_puzzle())
    await manager.request_turtle_soup_hint(
        room, source="web", visitor_token=visitor.token
    )
    active_game, answer = await manager.begin_turtle_soup_interaction(
        room,
        "这是温室的通风门，他在照料植物，真正入口在另一边。",
        source="web",
        visitor_token=visitor.token,
        limit=800,
    )
    await manager.resolve_turtle_soup_answer(
        room,
        active_game,
        answer,
        solved=True,
        source="web",
        matched_facts={0, 1, 2},
    )

    assert room.status == "finished"
    assert room.scores["turtle_soup"].completed == 1
    assert room.scores["turtle_soup"].human_wins == 1
    assert room.scores["gomoku"].completed == 0
    assert room.turtle_soup_stats.hints == 1
    assert room.turtle_soup_stats.answer_attempts == 1


@pytest.mark.asyncio
async def test_rematch_keeps_room_but_returns_to_fresh_generation() -> None:
    manager = RoomManager()
    room, _visitor, game = await make_soup_room(manager)
    puzzle = make_puzzle()
    await manager.complete_turtle_soup_generation(room, game, puzzle)
    game.give_up()
    await manager._finish_game(room)
    room_id = room.room_id
    access_token = room.access_token

    await manager.restart_finished_game(room, difficulty="hard")

    assert room.room_id == room_id
    assert room.access_token == access_token
    assert isinstance(room.game, TurtleSoupGame)
    assert room.game is not game
    assert room.game.phase == "preparing"
    assert room.difficulty == "hard"
    assert puzzle.signature in room.turtle_soup_recent_signatures


@pytest.mark.asyncio
async def test_plugin_uses_model_generation_then_hidden_validation() -> None:
    """题库都挑不出题时，仍然要「模型生成 + 独立校验」两段都走。

    默认配置下内置题库会先命中（题库里两百多道题），根本不会调用模型；
    这里显式关掉题库，测的是最后一层的 LLM 兜底路径。
    """
    generated = puzzle_data()
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    completion_text=json.dumps(generated, ensure_ascii=False)
                ),
                SimpleNamespace(completion_text='{"valid":true,"reason":"自洽"}'),
            ]
        )
    )
    plugin = new_plugin()
    plugin.context = SimpleNamespace(
        get_using_provider=lambda _session: provider,
        persona_manager=None,
    )
    plugin.manager = RoomManager()
    plugin.turtle_soup_library_enabled = False
    plugin.turtle_soup_online_pool_url = ""
    room, _visitor, game = await make_soup_room(plugin.manager)

    await plugin._prepare_turtle_soup(room, game)

    assert game.phase == "ready"
    assert game.puzzle is not None
    assert game.puzzle.title == "测试题"
    assert provider.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_plugin_falls_back_when_room_has_no_model() -> None:
    plugin = new_plugin()
    plugin.context = SimpleNamespace(
        get_using_provider=lambda _session: None,
        persona_manager=None,
    )
    plugin.manager = RoomManager(turtle_soup_content_level="all_ages")
    room, _visitor, game = await make_soup_room(plugin.manager)

    await plugin._prepare_turtle_soup(room, game)

    assert game.phase == "ready"
    assert game.puzzle is not None
    assert game.puzzle.content_level == "all_ages"


@pytest.mark.asyncio
async def test_failed_judgment_releases_busy_state_without_counting_question() -> None:
    provider = SimpleNamespace(text_chat=AsyncMock(side_effect=RuntimeError("offline")))
    plugin = new_plugin()
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())

    with pytest.raises(RuntimeError, match="模型调用失败"):
        await plugin.submit_turtle_soup_question(
            room,
            "这是温室吗？",
            source="web",
            visitor_token=visitor.token,
        )

    assert not game.processing
    assert game.question_count == 0
    assert game.entries == []


@pytest.mark.asyncio
async def test_web_question_endpoint_uses_same_hidden_judge_flow() -> None:
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='{"verdict":"yes","matched_facts":[0]}'
            )
        )
    )
    plugin = new_plugin()
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.public_base_url = ""
    plugin.quick_tunnel = SimpleNamespace(url="", running=False)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())
    server = GameRoomServer(
        plugin,
        plugin.manager,
        host="127.0.0.1",
        port=0,
        web_root=Path(__file__).resolve().parents[1] / "web",
    )

    async with TestClient(TestServer(server._build_app())) as client:
        response = await client.post(
            f"/api/room/{room.access_token}/soup/question",
            json={"visitor_token": visitor.token, "text": "这是温室吗？"},
            headers={"Origin": str(client.make_url("/")).rstrip("/")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["data"]["verdict"] == "yes"
    assert payload["data"]["room"]["game"]["entries"][0]["response"] == "是"
    assert "solution" not in payload["data"]["room"]["game"]["puzzle"]


def test_fallback_avoids_recent_signature_when_another_choice_exists() -> None:
    first = fallback_puzzle(content_level="all_ages")
    choices = {
        fallback_puzzle(
            content_level="all_ages", excluded_signatures={first.signature}
        ).signature
        for _index in range(10)
    }

    assert first.signature not in choices


# ---------------- 判定输出跑偏：重试，而不是当成「无关」 ----------------


THINKING_OUTPUT = (
    "让我想想。汤底里提到温室通风，所以「这是温室吗」应该回答是。"
    "但要注意不能把关键事实说出去……"
)


def test_question_judgment_usable_rejects_plain_prose() -> None:
    from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import (
        question_judgment_usable,
    )

    assert question_judgment_usable('{"verdict":"yes"}') is True
    assert question_judgment_usable('```json\n{"verdict":"no","solved":false}\n```') is True
    assert question_judgment_usable(THINKING_OUTPUT) is False
    assert question_judgment_usable('{"verdict":"大概是吧"}') is False
    assert question_judgment_usable("") is False


def test_answer_judgment_usable_rejects_plain_prose() -> None:
    from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import (
        answer_judgment_usable,
    )

    assert answer_judgment_usable('{"coverage":0.6,"verdict":"close"}') is True
    assert answer_judgment_usable('{"solved":false,"matched_facts":[0]}') is True
    assert answer_judgment_usable(THINKING_OUTPUT) is False
    assert answer_judgment_usable("这个推理差不多对了一半吧") is False


@pytest.mark.asyncio
async def test_thinking_output_is_retried_and_counted_when_retry_succeeds() -> None:
    """第一次吐出思考过程，重试拿到合法 JSON：这次提问应当正常计入。"""
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            side_effect=[
                SimpleNamespace(completion_text=THINKING_OUTPUT),
                SimpleNamespace(completion_text='{"verdict":"yes","matched_facts":[0]}'),
            ]
        )
    )
    plugin = new_plugin()
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())

    result = await plugin.submit_turtle_soup_question(
        room, "这是温室吗？", source="web", visitor_token=visitor.token
    )

    assert result["verdict"] == "yes"
    assert provider.text_chat.await_count == 2
    assert game.question_count == 1


@pytest.mark.asyncio
async def test_persistent_garbage_is_rejected_without_counting() -> None:
    """两次都跑偏：这句不该当成「无关」落库，也不该扣提问次数。"""
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text=THINKING_OUTPUT)
        )
    )
    plugin = new_plugin()
    plugin.context = SimpleNamespace(get_using_provider=lambda _session: provider)
    plugin.manager = RoomManager()
    room, visitor, game = await make_soup_room(plugin.manager)
    await plugin.manager.complete_turtle_soup_generation(room, game, make_puzzle())

    with pytest.raises(RuntimeError, match="不算次数"):
        await plugin.submit_turtle_soup_question(
            room, "这是温室吗？", source="web", visitor_token=visitor.token
        )

    assert provider.text_chat.await_count == 2
    assert game.question_count == 0
    assert game.entries == []
    assert not game.processing
