"""管理台「战绩管理」的玩家档案能力：负场、改名、精确修正、合并、清理。

战绩榜回答「某个玩法谁最强」，玩家档案回答「这个人整体怎么样」——
所以档案必须按 QQ 汇总，并且同名不同人不能被并成一条。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import astrbot_plugin_game_companion_huahuo.main as main_module
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def make_manager(tmp_path: Path) -> RoomManager:
    return RoomManager(max_private_rooms=0, global_stats_path=tmp_path / "global.json")


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self, default=None):
        return dict(self.payload)


def make_plugin(manager: RoomManager) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    return plugin


def seed(
    manager: RoomManager,
    game_type: str,
    qq: str,
    *,
    name: str = "",
    wins: int = 0,
    games: int = 0,
    draws: int = 0,
    last_at: float = 0.0,
) -> None:
    bucket = manager.global_player_wins.setdefault(game_type, {})
    bucket[qq] = {
        "name": name or qq,
        "wins": wins,
        "games": games,
        "draws": draws,
        "last_at": last_at,
        "daily": {time.strftime("%Y-%m-%d"): {"w": wins, "g": games, "d": draws}},
    }


# ---------------- 负场 / 平局 ----------------


@pytest.mark.asyncio
async def test_draw_is_counted_separately_from_a_loss(tmp_path: Path) -> None:
    """平局只记局数和平局数，负场 = 局数 - 胜场 - 平局。"""
    manager = make_manager(tmp_path)
    room = SimpleNamespace(
        game_type="gomoku",
        multiplayer=SimpleNamespace(
            enabled=True,
            seats=[
                SimpleNamespace(
                    is_ai=False,
                    qq="10001",
                    identity_confirmed=True,
                    display_name="小明",
                    visitor_token="t1",
                )
            ],
        ),
        visitors={},
    )
    manager._credit_board_game_global(  # noqa: SLF001
        room, winner_tokens={"t1"}, ai_won=False
    )
    manager._credit_board_game_global(  # noqa: SLF001
        room, winner_tokens=set(), ai_won=False
    )
    manager._credit_board_game_global(  # noqa: SLF001
        room, winner_tokens=set(), ai_won=False, draw=True
    )

    entry = manager.global_player_wins["gomoku"]["10001"]
    assert (entry["wins"], entry["games"], entry["draws"]) == (1, 3, 1)
    row = manager.global_leaderboard("gomoku")[0]
    assert row["losses"] == 1, "1 胜 1 负 1 平，负场不能把平局算进去"
    assert row["draws"] == 1


@pytest.mark.asyncio
async def test_leaderboard_row_reports_losses_even_for_legacy_data(tmp_path: Path) -> None:
    """老存档没有 draws 字段：负场按「局数 - 胜场」兜底，平局数为 0。"""
    manager = make_manager(tmp_path)
    manager.global_player_wins["gomoku"] = {
        "10001": {"name": "小明", "wins": 3, "games": 10}
    }

    row = manager.global_leaderboard("gomoku")[0]
    assert (row["wins"], row["games"], row["draws"], row["losses"]) == (3, 10, 0, 7)


# ---------------- 玩家档案 ----------------


@pytest.mark.asyncio
async def test_profiles_aggregate_across_games_and_keep_names_apart(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=4, games=10, draws=1, last_at=1000.0)
    seed(manager, "xiangqi", "10001", name="小明", wins=1, games=2)
    # 同名不同 QQ：必须各算各的
    seed(manager, "gomoku", "10002", name="小明", wins=9, games=9)

    rows = {row["qq"]: row for row in manager.player_profiles()}
    assert rows["10001"]["wins"] == 5
    assert rows["10001"]["games"] == 12
    assert rows["10001"]["draws"] == 1
    assert rows["10001"]["losses"] == 6
    assert rows["10001"]["games_played"] == ["gomoku", "xiangqi"]
    assert rows["10001"]["last_at_text"], "最近对局时间要能显示到分钟"
    assert rows["10002"]["wins"] == 9

    # 榜单也不能把两个「小明」并成一条
    board = manager.global_leaderboard("gomoku")
    assert sorted(row["qq"] for row in board) == ["10001", "10002"]


def test_profiles_fall_back_to_placeholder_name(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10009", name="", wins=1, games=0)
    manager.global_player_wins["gomoku"]["10009"]["name"] = ""
    row = manager.player_profiles()[0]
    assert row["name"] == "玩家0009"


# ---------------- 改名 / 修战绩 / 清零 / 删除 ----------------


def test_rename_player_updates_every_game(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="旧名字", wins=1, games=2)
    seed(manager, "xiangqi", "10001", name="旧名字", wins=0, games=1)
    manager.undercover_camp_wins["qq:10001"] = {"name": "旧名字", "civilian": 2}

    changed = manager.rename_player("10001", "新名字")

    assert changed == 2
    assert manager.global_player_wins["gomoku"]["10001"]["name"] == "新名字"
    assert manager.global_player_wins["xiangqi"]["10001"]["name"] == "新名字"
    assert manager.undercover_camp_wins["qq:10001"]["name"] == "新名字"


def test_rename_rejects_empty_and_unknown(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with pytest.raises(ValueError, match="不能为空"):
        manager.rename_player("10001", "   ")
    seed(manager, "gomoku", "10001")
    with pytest.raises(ValueError, match="未找到"):
        manager.rename_player("99999", "名字")


def test_set_player_stats_is_precise_and_validated(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=1, games=2)
    seed(manager, "gomoku", "10002", name="小明", wins=5, games=8)

    manager.set_player_stats("10001", "gomoku", wins=6, games=9)

    assert manager.global_player_wins["gomoku"]["10001"]["wins"] == 6
    assert manager.global_player_wins["gomoku"]["10002"]["wins"] == 5, "同名玩家不该被连带修改"
    with pytest.raises(ValueError, match="不能大于"):
        manager.set_player_stats("10001", "gomoku", wins=9, games=3)
    with pytest.raises(ValueError, match="未找到"):
        manager.set_player_stats("10003", "gomoku", wins=1)


def test_reset_and_delete_player(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", wins=3, games=5, draws=1)
    seed(manager, "xiangqi", "10001", wins=1, games=2)

    manager.reset_player("10001", "gomoku")
    entry = manager.global_player_wins["gomoku"]["10001"]
    assert (entry["wins"], entry["games"], entry["draws"], entry["daily"]) == (0, 0, 0, {})
    assert manager.global_player_wins["xiangqi"]["10001"]["wins"] == 1

    manager.delete_player("10001")
    assert "10001" not in manager.global_player_wins["gomoku"]
    assert "10001" not in manager.global_player_wins["xiangqi"]


def test_merge_player_sums_and_removes_source(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=2, games=4, draws=1, last_at=100.0)
    seed(manager, "gomoku", "10002", name="小明小号", wins=3, games=5, last_at=200.0)
    seed(manager, "xiangqi", "10002", name="小明小号", wins=1, games=1)
    manager.undercover_camp_wins["qq:10002"] = {"name": "小明小号", "undercover": 2}

    result = manager.merge_player("10001", "10002")

    assert result["touched"] == 2
    entry = manager.global_player_wins["gomoku"]["10001"]
    assert (entry["wins"], entry["games"], entry["draws"]) == (5, 9, 1)
    assert entry["last_at"] == 200.0
    assert "10002" not in manager.global_player_wins["gomoku"]
    assert "10002" not in manager.global_player_wins["xiangqi"]
    assert manager.undercover_camp_wins["qq:10001"]["undercover"] == 2
    assert "qq:10002" not in manager.undercover_camp_wins


def test_prune_empty_players_keeps_real_records(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.global_player_wins["gomoku"] = {
        "10001": {"name": "空号", "wins": 0, "games": 0, "daily": {}},
        "10002": {"name": "小明", "wins": 1, "games": 2},
    }

    removed = manager.prune_empty_players()

    assert removed == 1
    assert list(manager.global_player_wins["gomoku"]) == ["10002"]


# ---------------- 回归：清理 / 清零 / 合并跨玩法的数据完整性 ----------------


def test_prune_keeps_player_who_has_games_in_another_mode(tmp_path: Path) -> None:
    """同一个 QQ 在某个玩法里是空档案，不代表整个人都是空档案。"""
    manager = make_manager(tmp_path)
    manager.global_player_wins = {
        "gomoku": {"10001": {"name": "小明", "wins": 0, "games": 0, "daily": {}}},
        "xiangqi": {"10001": {"name": "小明", "wins": 12, "games": 20, "draws": 1}},
    }

    assert manager.prune_empty_players() == 0
    assert "10001" in manager.global_player_wins["xiangqi"], "不能把活跃玩家一起删掉"
    assert manager.global_player_wins["xiangqi"]["10001"]["wins"] == 12


def test_prune_keeps_player_with_only_camp_wins(tmp_path: Path) -> None:
    """只有卧底阵营胜场、没有对局记录的老玩家，档案也不是空的。"""
    manager = make_manager(tmp_path)
    manager.global_player_wins = {
        "undercover": {"10002": {"name": "小红", "wins": 0, "games": 0, "daily": {}}}
    }
    manager.undercover_camp_wins = {"qq:10002": {"name": "小红", "civilian": 4}}

    assert manager.prune_empty_players() == 0
    assert "qq:10002" in manager.undercover_camp_wins


def test_reset_player_clears_camp_wins_for_undercover_or_full_reset(tmp_path: Path) -> None:
    """阵营胜场是卧底专属：清该玩法或全清都要一起清，清别的玩法则保留。"""
    manager = make_manager(tmp_path)
    seed(manager, "undercover", "10001", name="小明", wins=3, games=5)
    seed(manager, "gomoku", "10001", name="小明", wins=1, games=2)

    manager.undercover_camp_wins["qq:10001"] = {"name": "小明", "undercover": 2}
    manager.reset_player("10001", "gomoku")
    assert "qq:10001" in manager.undercover_camp_wins, "清五子棋不该动卧底徽章"

    manager.reset_player("10001", "undercover")
    assert "qq:10001" not in manager.undercover_camp_wins, "清卧底玩法要连阵营胜场一起清"

    manager.undercover_camp_wins["qq:10001"] = {"name": "小明", "undercover": 2}
    manager.reset_player("10001")
    assert "qq:10001" not in manager.undercover_camp_wins, "全清后档案里不该还留着阵营胜场"
    assert manager.player_profiles()[0]["camps"] == {}


def test_delete_player_clears_camp_wins_for_undercover(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "undercover", "10001", name="小明", wins=3, games=5)
    manager.undercover_camp_wins["qq:10001"] = {"name": "小明", "civilian": 4}

    manager.delete_player("10001", "undercover")

    assert "qq:10001" not in manager.undercover_camp_wins


def test_merge_player_keeps_daily_draws(tmp_path: Path) -> None:
    """合并小号时按天分桶的平局也要累加，否则时间窗口里负场会被多算。"""
    manager = make_manager(tmp_path)
    today = time.strftime("%Y-%m-%d")
    manager.global_player_wins["gomoku"] = {
        "10001": {
            "name": "A", "wins": 3, "games": 5, "draws": 0,
            "daily": {today: {"w": 3, "g": 5, "d": 0}},
        },
        "10002": {
            "name": "B", "wins": 1, "games": 4, "draws": 2,
            "daily": {today: {"w": 1, "g": 4, "d": 2}},
        },
    }

    manager.merge_player("10001", "10002")

    slot = manager.global_player_wins["gomoku"]["10001"]["daily"][today]
    assert slot["d"] == 2
    row = manager.global_leaderboard("gomoku", days=7)[0]
    assert row["draws"] == 2
    assert row["losses"] == 3, "4 胜 / 9 局 / 2 平 => 3 负，不能把平局算成负场"


# ---------------- 管理台接口 ----------------


@pytest.mark.asyncio
async def test_page_players_returns_rows_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=2, games=4)
    plugin = make_plugin(manager)
    monkeypatch.setattr(main_module, "request", FakeRequest({}))

    result = await plugin.page_players()

    assert result["status"] == "ok"
    assert result["data"]["summary"]["players"] == 1
    assert result["data"]["summary"]["games"] == 4
    assert result["data"]["players"][0]["qq"] == "10001"


@pytest.mark.asyncio
async def test_page_player_action_dispatches_each_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=2, games=4)
    plugin = make_plugin(manager)

    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"action": "rename", "qq": "10001", "name": "新名字"}),
    )
    assert (await plugin.page_player_action())["status"] == "ok"
    assert manager.global_player_wins["gomoku"]["10001"]["name"] == "新名字"

    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest(
            {"action": "set_stats", "qq": "10001", "game_type": "gomoku", "wins": 3, "games": 5}
        ),
    )
    assert (await plugin.page_player_action())["status"] == "ok"
    assert manager.global_player_wins["gomoku"]["10001"]["wins"] == 3

    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"action": "set_stats", "qq": "10001", "game_type": "gomoku", "wins": 9, "games": 1}),
    )
    failed = await plugin.page_player_action()
    assert failed["status"] == "error" and "不能大于" in failed["message"]

    monkeypatch.setattr(
        main_module, "request", FakeRequest({"action": "reset", "qq": "10001"})
    )
    assert (await plugin.page_player_action())["status"] == "ok"
    assert manager.global_player_wins["gomoku"]["10001"]["games"] == 0

    monkeypatch.setattr(
        main_module, "request", FakeRequest({"action": "unknown"})
    )
    assert (await plugin.page_player_action())["status"] == "error"


@pytest.mark.asyncio
async def test_page_leaderboard_accepts_time_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理台的「最近 7 天 / 30 天」下拉走的是排行榜接口的 days 参数。"""
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=2, games=4)
    plugin = make_plugin(manager)
    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"game_type": "gomoku", "days": 7}),
    )

    result = await plugin.page_leaderboard()

    assert result["status"] == "ok"
    row = result["data"]["leaderboard"]["gomoku"][0]
    assert row["wins"] == 2 and "losses" in row
