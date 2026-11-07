"""自定义海龟汤：私聊投稿的校验、去重、列表与删除。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.soup_custom import (
    CustomSoupStore,
    handle_custom_command,
)

OWNER = "10001"
OTHER = "10002"
SURFACE = "一个男人每天摆两副碗筷，后来他自杀了，为什么？"
SOLUTION = "他是灯塔管理员，某天忘了点灯导致船只失事，妻女因此遇难，他愧疚自杀，摆碗筷是在等她们回来。"


def make_plugin(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _soup_custom=lambda: CustomSoupStore(tmp_path / "custom.json")
    )


def test_add_stores_and_builds_playable_puzzle(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    reply = handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)

    assert "已收录" in reply
    store = plugin._soup_custom()
    assert len(store) == 1
    puzzles = store.puzzles()
    assert len(puzzles) == 1
    assert puzzles[0].surface == SURFACE
    assert puzzles[0].solution == SOLUTION
    # 规则补全要给出可用的七字段
    assert puzzles[0].title
    assert len(puzzles[0].key_facts) >= 3
    assert len(puzzles[0].hints) >= 1


def test_short_surface_is_rejected(tmp_path: Path) -> None:
    reply = handle_custom_command(make_plugin(tmp_path), f"添加海龟汤 太短了|{SOLUTION}", OWNER)
    assert "汤面太短" in reply
    assert len(CustomSoupStore(tmp_path / "custom.json")) == 0


def test_short_solution_is_rejected(tmp_path: Path) -> None:
    reply = handle_custom_command(make_plugin(tmp_path), f"添加海龟汤 {SURFACE}|没讲清楚", OWNER)
    assert "汤底太短" in reply


def test_missing_separator_shows_usage(tmp_path: Path) -> None:
    reply = handle_custom_command(make_plugin(tmp_path), "添加海龟汤 只有汤面没有汤底", OWNER)
    assert "用法" in reply
    assert "|" in reply


def test_duplicate_is_rejected(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    reply = handle_custom_command(plugin, f"添加海龟汤 {SURFACE} | {SOLUTION}", OWNER)
    assert "已经有这道题" in reply
    assert len(plugin._soup_custom()) == 1


def test_list_and_delete(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    listed = handle_custom_command(plugin, "我的海龟汤", OWNER)
    assert "1 道" in listed
    assert "1." in listed

    removed = handle_custom_command(plugin, "删除海龟汤 1", OWNER)
    assert "已删除" in removed
    assert len(plugin._soup_custom()) == 0

    missing = handle_custom_command(plugin, "删除海龟汤 9", OWNER)
    assert "没有这道题" in missing


def test_delete_needs_number(tmp_path: Path) -> None:
    reply = handle_custom_command(make_plugin(tmp_path), "删除海龟汤", OWNER)
    assert "序号" in reply


def test_entries_persist_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    store = CustomSoupStore(path)
    store.add(SURFACE, SOLUTION)
    reloaded = CustomSoupStore(path)
    assert len(reloaded) == 1
    assert reloaded.entries[0].surface == SURFACE


def test_content_level_filters_heavy_puzzles(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    store = CustomSoupStore(path)
    store.add(SURFACE, SOLUTION, content_level="unrestricted")
    assert store.puzzles("all_ages") == []
    assert len(store.puzzles("unrestricted")) == 1


def test_slash_prefix_is_accepted(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    reply = handle_custom_command(plugin, f"/添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    assert "已收录" in reply


@pytest.mark.parametrize("message", ["我的题库", "海龟汤题库"])
def test_other_list_aliases(tmp_path: Path, message: str) -> None:
    plugin = make_plugin(tmp_path)
    assert "还没有投稿" in handle_custom_command(plugin, message, OWNER)


# ---------------- 管理台题库页 ----------------

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

BUILTIN = Path(__file__).resolve().parent.parent / "soup_library.json"


def make_console_plugin(tmp_path: Path) -> SimpleNamespace:
    plugin = SimpleNamespace(
        _soup_library=lambda: SoupLibrary(BUILTIN, tmp_path / "cache.json"),
        _soup_deck=lambda: SoupDeck(tmp_path / "deck.json"),
        _soup_custom=lambda: CustomSoupStore(tmp_path / "custom.json"),
    )
    return plugin


@pytest.mark.asyncio
async def test_page_soup_library_reports_all_three_sources(tmp_path: Path) -> None:
    plugin = make_console_plugin(tmp_path)
    plugin._soup_custom().add(SURFACE, SOLUTION)
    result = await GameCompanionPlugin.page_soup_library(plugin)

    data = result["data"]
    assert data["builtin"]["total"] > 0
    assert data["builtin"]["by_level"]["normal"] >= 1
    assert data["used"]["total"] == 0
    assert data["custom"]["total"] == 1
    assert data["custom"]["items"][0]["surface"] == SURFACE


@pytest.mark.asyncio
async def test_page_reset_only_clears_usage_not_library(tmp_path: Path) -> None:
    from astrbot_plugin_game_companion_huahuo.soup_library import build_puzzle

    plugin = make_console_plugin(tmp_path)
    deck = plugin._soup_deck()
    puzzle = build_puzzle(SURFACE, SOLUTION)
    deck.remember(puzzle, source="custom")
    assert deck.stats()["total"] == 1

    result = await GameCompanionPlugin.page_soup_library_reset(plugin)

    assert result["data"]["cleared"] == 1
    assert plugin._soup_deck().stats()["total"] == 0
    # 题库文件与投稿都还在
    assert len(plugin._soup_library().entries) > 0


@pytest.mark.asyncio
async def test_page_delete_custom_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import astrbot_plugin_game_companion_huahuo.main as main_module

    plugin = make_console_plugin(tmp_path)
    plugin._soup_custom().add(SURFACE, SOLUTION)

    class FakeRequest:
        async def json(self, default=None):
            return {"index": 1}

    monkeypatch.setattr(main_module, "request", FakeRequest())
    result = await GameCompanionPlugin.page_soup_custom_delete(plugin)

    assert result["status"] == "ok"
    assert len(plugin._soup_custom()) == 0


@pytest.mark.asyncio
async def test_page_delete_rejects_bad_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import astrbot_plugin_game_companion_huahuo.main as main_module

    plugin = make_console_plugin(tmp_path)

    class FakeRequest:
        async def json(self, default=None):
            return {"index": 9}

    monkeypatch.setattr(main_module, "request", FakeRequest())
    result = await GameCompanionPlugin.page_soup_custom_delete(plugin)
    assert result["status"] == "error"


# ---------------- 归属：不能删别人的题 ----------------


def test_only_own_entries_are_listed(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    handle_custom_command(
        plugin,
        "添加海龟汤 一个人在密室里被水淹死，房间里却没有水，为什么？"
        "|他是潜水员，在深海作业时氧气耗尽，被队友放进减压舱后仍然没救回来，"
        "所谓密室其实是加压舱。",
        OTHER,
    )

    mine = handle_custom_command(plugin, "我的海龟汤", OWNER)
    assert "1 道" in mine
    assert SURFACE[:10] in mine

    theirs = handle_custom_command(plugin, "我的海龟汤", OTHER)
    assert "密室里被水淹死" in theirs
    assert SURFACE[:10] not in theirs


def test_cannot_delete_someone_elses_entry(tmp_path: Path) -> None:
    """题库是共用的：不校验归属时谁都能把别人投的题删掉。"""
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)

    reply = handle_custom_command(plugin, "删除海龟汤 1", OTHER)

    assert "没有这道题" in reply
    assert len(plugin._soup_custom()) == 1, "别人的题不能被删掉"


def test_can_delete_own_entry(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    reply = handle_custom_command(plugin, "删除海龟汤 1", OWNER)
    assert "已删除" in reply
    assert len(plugin._soup_custom()) == 0


def test_entry_records_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "custom.json"
    store = CustomSoupStore(path)
    store.add(SURFACE, SOLUTION, owner=OWNER)
    reloaded = CustomSoupStore(path)
    assert reloaded.entries[0].owner == OWNER
    assert [index for index, _ in reloaded.owned(OWNER)] == [1]
    assert reloaded.owned(OTHER) == []


# ---------------- 投稿时的内容等级 ----------------


def test_level_defaults_to_normal_when_omitted(tmp_path: Path) -> None:
    """不写等级就按普通入库，和加等级之前的行为一致。"""
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {SURFACE}|{SOLUTION}", OWNER)
    assert plugin._soup_custom().entries[0].content_level == "normal"


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("全年龄", "all_ages"),
        ("普通", "normal"),
        ("不限制", "unrestricted"),
        ("成人", "unrestricted"),
        ("重口", "unrestricted"),
        ("all_ages", "all_ages"),
        ("unrestricted", "unrestricted"),
    ],
)
def test_level_prefix_is_recognized(tmp_path: Path, prefix: str, expected: str) -> None:
    plugin = make_plugin(tmp_path)
    reply = handle_custom_command(
        plugin, f"添加海龟汤 {prefix} {SURFACE}|{SOLUTION}", OWNER
    )
    assert "已收录" in reply
    assert plugin._soup_custom().entries[0].content_level == expected


def test_bracketed_level_is_recognized(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 【全年龄】{SURFACE}|{SOLUTION}", OWNER)
    entry = plugin._soup_custom().entries[0]
    assert entry.content_level == "all_ages"
    assert entry.surface == SURFACE


def test_surface_starting_with_level_word_is_not_eaten(tmp_path: Path) -> None:
    """汤面本身以「普通」开头时不能被当成等级前缀吃掉。"""
    surface = "普通的一天早上，他照常出门，却再也没回来，为什么？"
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 {surface}|{SOLUTION}", OWNER)
    entry = plugin._soup_custom().entries[0]
    assert entry.content_level == "normal"
    assert entry.surface == surface


def test_all_ages_room_cannot_draw_unrestricted_entry(tmp_path: Path) -> None:
    """等级的意义就在这里：全年龄房抽不到投稿标成「不限制」的题。"""
    plugin = make_plugin(tmp_path)
    store = plugin._soup_custom()
    store.add(SURFACE, SOLUTION, content_level="unrestricted", owner=OWNER)
    assert store.puzzles(max_content_level="all_ages") == []
    assert len(store.puzzles(max_content_level="unrestricted")) == 1


def test_list_shows_the_level_of_each_entry(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    handle_custom_command(plugin, f"添加海龟汤 全年龄 {SURFACE}|{SOLUTION}", OWNER)
    reply = handle_custom_command(plugin, "我的海龟汤", OWNER)
    assert "[全年龄]" in reply
