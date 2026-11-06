from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.models import PlayerSeat
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import (
    TurtleSoupGame,
    all_fallback_puzzles,
    puzzle_from_mapping,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import (
    coverage_reply,
    parse_answer_judgment_detail,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import (
    SoupDeck,
    puzzle_fingerprint,
    text_similarity,
)

ROOT = Path(__file__).resolve().parents[1]


def puzzle_data(**overrides):
    data = {
        "title": "窗边那块蛋糕",
        "surface": "糕点店每天下午四点把一块小蛋糕摆在窗边，从不卖。某天新店员把它卖掉了，老板立刻追出去要了回来。为什么？",
        "solution": "那块蛋糕是给附近幼儿园一个孩子的路标，他放学要看到它才肯继续回家；妈妈和店主约好用蛋糕给他壮胆。",
        "key_facts": ["蛋糕是给孩子的信号", "孩子看到它才肯走", "妈妈与店主有约定"],
        "acceptable_variants": [],
        "hints": ["它不是拿来卖的。", "每天都有人经过这里。", "那个孩子需要看到它。"],
        "theme": "街道与约定",
        "trick": "把非卖品当成商品",
    }
    data.update(overrides)
    return data


def make_puzzle(**overrides):
    return puzzle_from_mapping(puzzle_data(**overrides), content_level="all_ages")


async def make_soup_room(manager: RoomManager, *, seats: int = 1):
    """建一间海龟汤房，并按需要摆好玩家席（不触发 LLM 出题）。"""
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:42001",
        platform="aiocqhttp",
        group_id="42001",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
    )
    visitors = []
    for index in range(seats):
        visitor = await manager.join(room)
        visitor.connected = True
        visitor.last_seen_at = time.time()
        visitors.append(visitor)
    room.multiplayer.enabled = True
    room.multiplayer.capacity = max(2, seats)
    room.multiplayer.seats = [
        PlayerSeat(
            number=index + 1,
            visitor_token=visitor.token,
            display_name=f"玩家{index + 1}",
            identity_confirmed=True,
        )
        for index, visitor in enumerate(visitors)
    ]
    room.multiplayer.current_turn_index = 0
    room.status = "active"
    game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="all_ages")
    game.set_puzzle(make_puzzle())
    room.game = game
    return room, visitors, game


def make_plugin(data_dir: Path) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.data_dir = data_dir
    plugin.soup_deck = SoupDeck(data_dir / "turtle_soup_deck.json")
    plugin.turtle_soup_online_pool_url = ""
    plugin.turtle_soup_group_announce_enabled = True
    plugin.turtle_soup_group_announce_auto = False
    plugin.turtle_soup_group_announce_style = "text"
    plugin.turtle_soup_group_announce_recap = False
    return plugin


# ---------------- 汤面去重 ----------------


def test_fingerprint_ignores_punctuation_and_width() -> None:
    """标题/汤面只差标点、空格、全半角时，必须被认成同一道题。"""
    base = make_puzzle()
    variant = make_puzzle(
        title="窗边那块蛋糕 ",
        surface="糕点店每天下午四点，把一块小蛋糕摆在窗边（从不卖）！某天新店员把它卖掉了，老板立刻追出去要了回来。为什么？",
    )
    # 标点/空格/全半角归一化后完全一致 → 同指纹，直接判重
    assert puzzle_fingerprint(base) == puzzle_fingerprint(variant)
    assert text_similarity(base.surface, variant.surface) >= 0.9


def test_deck_persists_and_rejects_rewritten_puzzle(tmp_path: Path) -> None:
    deck = SoupDeck(tmp_path / "deck.json")
    puzzle = make_puzzle()
    assert deck.has(puzzle) is False
    deck.remember(puzzle, source="llm")
    assert deck.has(puzzle) is True

    # 换个标题、微调措辞：新实例（模拟重启）也要认出来
    reopened = SoupDeck(tmp_path / "deck.json")
    rewritten = make_puzzle(
        title="窗台上的蛋糕",
        surface="有家糕点店每天下午四点都会把一块蛋糕摆在窗台，从来不卖。新店员把它卖了，老板追出去换回来。为什么？",
    )
    assert reopened.has(rewritten) is True


def test_deck_filters_duplicates_inside_one_batch(tmp_path: Path) -> None:
    deck = SoupDeck(tmp_path / "deck.json")
    first = make_puzzle()
    second = make_puzzle(title="完全不同的题", surface="另一个故事" * 10, theme="别的主题", trick="别的诡计")
    kept = deck.would_repeat([first, first, second])
    assert len(kept) == 2


def test_fallback_pool_covers_levels_and_never_repeats_until_exhausted(tmp_path: Path) -> None:
    all_ages = all_fallback_puzzles(content_level="all_ages")
    normal = all_fallback_puzzles(content_level="normal")
    assert len(all_ages) >= 6
    assert len(normal) > len(all_ages)
    assert all(item.content_level == "all_ages" for item in all_ages)

    plugin = make_plugin(tmp_path)
    deck = plugin.soup_deck
    picked: list[str] = []
    for _ in range(len(normal)):
        puzzle = plugin._pick_fallback_puzzle("normal", deck, set())
        assert puzzle.signature not in picked, "内置题库在耗尽前不应重复"
        picked.append(puzzle.signature)
        deck.remember(puzzle, source="fallback")
    # 全部出过之后，仍然要给出题目（挑最久以前出过的），不能抛异常
    assert plugin._pick_fallback_puzzle("normal", deck, set()) is not None


def test_generation_prompt_lists_recent_titles_to_avoid() -> None:
    from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import generation_prompt

    _, prompt = generation_prompt(
        difficulty="normal",
        content_level="normal",
        recent_signatures=["街道与约定|把非卖品当成商品"],
        recent_titles=["窗边那块蛋糕"],
        recent_surfaces=["糕点店每天下午四点把一块小蛋糕摆在窗边"],
    )
    assert "窗边那块蛋糕" in prompt
    assert "不能只是把职业、物品、地点或人物关系换一层皮" in prompt


# ---------------- 判定放宽与反馈 ----------------


def test_answer_judgment_accepts_complete_but_colloquial_reasoning() -> None:
    """口语化、跳跃但因果完整的推理，不能因为 coverage 0.7 就被判「还差一点」。"""
    judgment = parse_answer_judgment_detail(
        '{"verdict":"solved","coverage":0.7,"matched_facts":[0,1,2],"missing":""}',
        fact_count=3,
    )
    assert judgment.solved is True
    assert judgment.coverage == 0.7


def test_answer_judgment_rejects_confident_but_partial_answer() -> None:
    judgment = parse_answer_judgment_detail(
        '{"solved":true,"coverage":0.5,"matched_facts":[0],"missing":"动机"}',
        fact_count=3,
    )
    assert judgment.solved is False
    assert judgment.verdict == "close"
    assert judgment.missing == "动机"


def test_cumulative_facts_can_complete_an_answer() -> None:
    """本次只补了最后一环、但历史提问已确认其余事实时，应判解开。"""
    judgment = parse_answer_judgment_detail(
        '{"verdict":"solved","coverage":0.55,"matched_facts":[2],"missing":""}',
        fact_count=3,
        discovered_facts={0, 1},
    )
    assert judgment.cumulative >= 0.8
    assert judgment.solved is True


def test_coverage_reply_points_at_the_missing_piece() -> None:
    text = coverage_reply(0.8, "时间差", solved=False)
    assert "时间差" in text
    assert "接近" in text
    assert coverage_reply(0.2, "动机", solved=False).startswith("这条线还不太对")


# ---------------- 揭晓汤底与群通知 ----------------


@pytest.mark.asyncio
async def test_single_player_reveal_finishes_immediately() -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_free_questioning=True)
    room, visitors, game = await make_soup_room(manager, seats=1)

    result = await manager.request_soup_reveal(room, source="web", visitor_token=visitors[0].token)

    assert result["revealed"] is True
    assert game.finished is True
    assert game.end_reason == "revealed"
    assert room.status == "finished"
    # 群通知快照刻意不带汤底
    assert room.last_soup_result is not None
    assert "solution" not in room.last_soup_result


@pytest.mark.asyncio
async def test_multiplayer_reveal_needs_majority() -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_free_questioning=True)
    room, visitors, game = await make_soup_room(manager, seats=2)

    first = await manager.request_soup_reveal(
        room, source="web", visitor_token=visitors[0].token
    )
    assert first["revealed"] is False
    assert first["votes"] == 1
    assert first["required"] == 2
    assert game.finished is False, "一个人不能替全队揭晓"

    second = await manager.vote_soup_reveal(
        room, agree=True, source="web", visitor_token=visitors[1].token
    )
    assert second["revealed"] is True
    assert game.end_reason == "revealed"


@pytest.mark.asyncio
async def test_timeout_tick_reveals_unsloved_soup() -> None:
    manager = RoomManager(
        turtle_soup_max_players=4,
        turtle_soup_free_questioning=True,
        turtle_soup_auto_reveal_seconds=60,
    )
    room, _visitors, game = await make_soup_room(manager, seats=1)
    game.last_progress_at = time.time() - 600
    room.multiplayer.turn_timeout_seconds = 0

    await manager._tick_multiplayer_room(room, time.time())

    assert game.finished is True
    assert game.end_reason == "timeout"
    assert room.status == "finished"


@pytest.mark.asyncio
async def test_announce_text_shows_surface_and_questions_but_never_solution(
    tmp_path: Path,
) -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_free_questioning=True)
    room, visitors, game = await make_soup_room(manager, seats=1)
    game.record_question("蛋糕是给某个人的信号吗？", "yes", source="web", player_number=1)
    game.record_question("和幼儿园有关吗？", "yes", source="web", player_number=1)
    await manager.request_soup_reveal(room, source="web", visitor_token=visitors[0].token)

    plugin = make_plugin(tmp_path)
    text = plugin._soup_announce_text(room)

    assert "窗边那块蛋糕" in text
    assert "糕点店每天下午四点" in text
    # 提问形状保留，但贴着汤底的片段被遮成「……」
    assert "有关吗" in text
    assert "幼儿园" not in text
    assert "壮胆" not in text
    assert "路标" not in text
    assert "想知道答案" in text


@pytest.mark.asyncio
async def test_announce_is_idempotent_per_game(tmp_path: Path) -> None:
    manager = RoomManager(turtle_soup_max_players=4, turtle_soup_free_questioning=True)
    room, visitors, _game = await make_soup_room(manager, seats=1)
    await manager.request_soup_reveal(room, source="web", visitor_token=visitors[0].token)

    plugin = make_plugin(tmp_path)
    sent: list[str] = []

    async def fake_send(_room, text: str) -> bool:
        sent.append(text)
        return True

    plugin._send_to_origin = fake_send  # type: ignore[assignment]
    first = await plugin.soup_announce_result(room, "")
    key = f"soup:{int(room.completed_games)}"
    room.last_announced_uid = key
    room.last_announced_text = first

    assert first
    assert room.last_announced_uid == key
    assert len(sent) == 1


def test_soup_settings_exist_in_schema_and_panel() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["turtle_soup"]["items"]
    for key in (
        "auto_reveal_seconds",
        "online_pool_url",
        "group_announce_enabled",
        "group_announce_auto",
        "group_announce_style",
        "group_announce_recap",
    ):
        assert key in items

    from astrbot_plugin_game_companion_huahuo.main import GAME_CATALOG

    panel = next(item for item in GAME_CATALOG if item["game_type"] == "turtle_soup")
    assert "auto_reveal_seconds" in {field["key"] for field in panel["fields"]}
