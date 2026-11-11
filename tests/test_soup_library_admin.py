"""题库管理：停用/删除/编辑内置题、改投稿、汤底打码。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.soup_custom import CustomSoupStore
from astrbot_plugin_game_companion_huahuo.soup_generated import GeneratedSoupStore
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary
from astrbot_plugin_game_companion_huahuo.soup_library import build_puzzle
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

ENTRIES = [
    {
        "surface": "一名男子走进餐厅点了一碗海龟汤，喝了一口就哭着离开了，为什么？",
        "solution": "他小时候遇上海难，同伴把自己的肉煮给他吃，说是海龟汤。他今天尝到真味道，才知道当年吃的是同伴。",
        "content_level": "normal",
    },
    {
        "surface": "她每天都要买两份早餐，一份自己吃，一份放在窗台上，为什么？",
        "solution": "窗台对着邻居家的猫，那位独居老人拜托她帮忙喂猫，老人住院后她害怕猫饿死，即使后来老人去世了她也一直喂。",
        "content_level": "all_ages",
    },
    {
        "surface": "停电的夜里，他点了一根蜡烛，然后从十楼跳了下去，为什么？",
        "solution": "他是个盲人，停电时误以为天亮了，习惯性地打开窗去阳台浇花，脚下踏空。",
        "content_level": "unrestricted",
    },
]


def make_library(tmp_path: Path) -> SoupLibrary:
    path = tmp_path / "library.json"
    path.write_text(
        json.dumps({"items": ENTRIES}, ensure_ascii=False), encoding="utf-8"
    )
    return SoupLibrary(
        path,
        tmp_path / "cache.json",
        overrides_path=tmp_path / "overrides.json",
    )


def test_solution_is_masked_for_the_list() -> None:
    """列表里的汤底要打码：一次铺开两百多条答案，管理员自己就被剧透了。"""
    masked = SoupLibrary.mask_solution(ENTRIES[0]["solution"])
    assert masked.startswith(ENTRIES[0]["solution"][:12])
    assert masked.endswith(f"（共 {len(ENTRIES[0]['solution'])} 字）")
    assert ENTRIES[0]["solution"] not in masked


def test_disable_keeps_the_entry_but_stops_drawing(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    # 内容等级上限：unrestricted 才看得到全部三档
    assert len(library.entries_for("unrestricted")) == 3

    library.set_disabled(1, True)
    assert len(library.entries_for("unrestricted")) == 2
    assert library.find(1) is not None
    assert library.find(1).disabled is True
    assert library.stats()["disabled"] == 1

    library.set_disabled(1, False)
    assert len(library.entries_for("unrestricted")) == 3


def test_remove_hides_entry_and_restore_all_brings_it_back(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    library.remove_entry(2)
    assert library.find(2) is None
    assert len(library.entries) == 2
    assert library.stats()["overrides"]["removed"] == 1

    # 改动写进了覆盖层，题库文件本身没被改
    raw = json.loads(library.path.read_text(encoding="utf-8"))
    assert len(raw["items"]) == 3

    library.restore_all()
    assert library.find(2) is not None
    assert len(library.entries) == 3


def test_edit_writes_override_not_the_shipped_file(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    solution = "她其实是在喂邻居家的猫。老人住院后托付她照看，老人去世后她仍然每天去喂那只猫，怕它饿死所以一直买两份早餐。"
    library.edit_entry(2, solution=solution, content_level="unrestricted")

    assert library.find(2).solution == solution
    assert library.find(2).content_level == "unrestricted"
    assert library.override_of(2)["solution"] == solution
    raw = json.loads(library.path.read_text(encoding="utf-8"))
    assert raw["items"][1]["solution"] != solution


def test_edit_rejects_too_short_text(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    with pytest.raises(ValueError):
        library.edit_entry(1, surface="太短了")

    assert library.find(1).surface == ENTRIES[0]["surface"]


def test_overrides_survive_reload(tmp_path: Path) -> None:
    """管理台的改动要落盘：重启插件后不该全部复原。"""
    library = make_library(tmp_path)
    library.set_disabled(3, True)
    library.edit_entry(1, content_level="all_ages")

    again = SoupLibrary(
        library.path,
        tmp_path / "cache.json",
        overrides_path=tmp_path / "overrides.json",
    )
    assert again.find(3).disabled is True
    assert again.find(1).content_level == "all_ages"


def test_custom_store_can_be_edited_from_console(tmp_path: Path) -> None:
    store = CustomSoupStore(tmp_path / "custom.json")
    store.add(ENTRIES[1]["surface"], ENTRIES[1]["solution"], content_level="normal")

    solution = "她每天买两份早餐，一份放在窗台上喂邻居家的猫；老人住院后托付给她，她一直喂到猫被接走为止。"
    updated = store.update(1, solution=solution, content_level="all_ages")

    assert updated is not None
    assert store.entries[0].solution == solution
    assert store.entries[0].content_level == "all_ages"
    # 越界返回 None，交给接口层报错
    assert store.update(99, surface="随便") is None


def test_console_snapshot_masks_solutions(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    custom = CustomSoupStore(tmp_path / "custom.json")
    custom.add(ENTRIES[0]["surface"], ENTRIES[0]["solution"])
    plugin = SimpleNamespace(
        _soup_library=lambda: library,
        _soup_deck=lambda: SoupDeck(tmp_path / "deck.json"),
        _soup_custom=lambda: custom,
        _soup_generated=lambda: GeneratedSoupStore(tmp_path / "generated.json"),
        # 题库快照会带上后台出题任务状态，桩对象也要能算出来
        _soup_generate_job_snapshot=lambda: GameCompanionPlugin._soup_generate_job_snapshot(
            plugin
        ),
    )

    data = GameCompanionPlugin._soup_library_snapshot(plugin)

    assert data["builtin"]["total"] == 3
    assert len(data["builtin"]["items"]) == 3
    for item in data["builtin"]["items"]:
        assert "solution_preview" in item
        assert item["solution_preview"].endswith("字）")
    assert data["custom"]["items"][0]["solution_preview"].endswith("字）")
    assert data["generate_job"]["state"] == "idle"


def test_used_flag_marks_puzzles_already_drawn(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    deck = SoupDeck(tmp_path / "deck.json")
    deck.remember(build_puzzle(ENTRIES[1]["surface"], ENTRIES[1]["solution"]), source="library")
    plugin = SimpleNamespace(
        _soup_library=lambda: library,
        _soup_deck=lambda: deck,
        _soup_custom=lambda: CustomSoupStore(tmp_path / "custom.json"),
        _soup_generated=lambda: GeneratedSoupStore(tmp_path / "generated.json"),
        _soup_generate_job_snapshot=lambda: GameCompanionPlugin._soup_generate_job_snapshot(
            plugin
        ),
    )

    data = GameCompanionPlugin._soup_library_snapshot(plugin)
    used = [item for item in data["builtin"]["items"] if item["used"]]
    assert [item["index"] for item in used] == [2]
