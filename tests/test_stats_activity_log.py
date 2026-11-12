"""战绩管理的三块增量：玩家档案跟随玩法筛选、近 N 天活跃趋势、操作日志筛选。

以前档案永远是全玩法汇总，选着「五子棋」却显示包含海龟汤的总局数；
日志也只有一张最近 200 条的表，翻旧记录只能自己数。这里把三块都补上。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import astrbot_plugin_game_companion_huahuo.main as main_module
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import (
    AI_LEADERBOARD_KEY,
    RoomManager,
)

ROOT = Path(__file__).resolve().parents[1]


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
    daily: dict | None = None,
) -> None:
    bucket = manager.global_player_wins.setdefault(game_type, {})
    bucket[qq] = {
        "name": name or qq,
        "wins": wins,
        "games": games,
        "draws": draws,
        "daily": daily or {},
    }


def day_text(offset: int, *, now: float | None = None) -> str:
    base = now if now is not None else time.time()
    return time.strftime("%Y-%m-%d", time.localtime(base - offset * 86400))


# ---------------- 玩家档案跟随玩法筛选 ----------------


def test_player_profiles_can_be_scoped_to_one_game(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=4, games=10)
    seed(manager, "xiangqi", "10001", name="小明", wins=1, games=2)
    seed(manager, "xiangqi", "10002", name="小红", wins=3, games=3)

    rows = manager.player_profiles(game_type="gomoku")

    assert [row["qq"] for row in rows] == ["10001"], "筛五子棋时不该带出只玩过象棋的人"
    assert rows[0]["games"] == 10
    assert rows[0]["games_played"] == ["gomoku"]
    # 全部玩法汇总仍然完整
    all_rows = {row["qq"]: row for row in manager.player_profiles()}
    assert all_rows["10001"]["games"] == 12


def test_player_profiles_drop_undercover_camps_for_other_games(tmp_path: Path) -> None:
    """阵营胜场只属于卧底：筛别的玩法时不展示，避免看着像这个玩法也统计了阵营。"""
    manager = make_manager(tmp_path)
    seed(manager, "gomoku", "10001", name="小明", wins=1, games=2)
    seed(manager, "undercover", "10001", name="小明", wins=2, games=3)
    manager.undercover_camp_wins["qq:10001"] = {"name": "小明", "undercover": 2}

    gomoku = manager.player_profiles(game_type="gomoku")[0]
    undercover = manager.player_profiles(game_type="undercover")[0]

    assert gomoku["camps"] == {}
    assert undercover["camps"]["undercover"] == 2


# ---------------- 活跃趋势 ----------------


def test_activity_series_covers_every_day_and_counts_humans(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    now = time.time()
    today = day_text(0, now=now)
    yesterday = day_text(1, now=now)
    seed(
        manager,
        "gomoku",
        "10001",
        name="小明",
        daily={
            today: {"w": 1, "g": 2, "d": 0},
            yesterday: {"w": 0, "g": 1, "d": 0},
        },
    )
    seed(manager, "gomoku", "10002", name="小红", daily={today: {"w": 0, "g": 1, "d": 0}})
    # 花火是 AI：对局要算，但「活跃玩家数」不该把它算成一个人
    seed(manager, "gomoku", AI_LEADERBOARD_KEY, name="花火", daily={today: {"w": 0, "g": 1, "d": 0}})

    series = manager.activity_series(days=7, now=now)

    assert len(series) == 7, "没有对局的日子也要返回 0，前端才能画出连续的柱子"
    assert series[-1]["date"] == today
    assert series[-1]["games"] == 4
    assert series[-1]["players"] == 2
    assert series[-2]["games"] == 1
    assert series[-2]["players"] == 1
    assert series[0]["games"] == 0


def test_activity_series_is_scoped_to_game_type(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    now = time.time()
    today = day_text(0, now=now)
    seed(manager, "gomoku", "10001", daily={today: {"w": 1, "g": 2, "d": 0}})
    seed(manager, "xiangqi", "10001", daily={today: {"w": 0, "g": 5, "d": 0}})

    gomoku = manager.activity_series(game_type="gomoku", days=7, now=now)
    everything = manager.activity_series(days=7, now=now)

    assert gomoku[-1]["games"] == 2
    assert everything[-1]["games"] == 7


def test_activity_series_clamps_window(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    # 不传 / 传 0 按默认 7 天；上限 60 天，和按天分桶的保留天数一致
    assert len(manager.activity_series(days=0)) == 7
    assert len(manager.activity_series(days=999)) == 60


# ---------------- 操作日志筛选 ----------------


def _write_log(manager: RoomManager, entries: list[dict]) -> None:
    path = manager.operation_log_path
    assert path is not None
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def test_operation_log_page_filters_by_action_keyword_and_days(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    now = time.time()
    _write_log(
        manager,
        [
            {
                "ts": now - 10 * 86400,
                "time": "2026-11-01 10:00:00",
                "action": "assign",
                "detail": "房间 A1：安排玩家",
                "operator": "游戏管理台",
                "room_id": "A1",
            },
            {
                "ts": now - 60,
                "time": "2026-11-12 10:00:00",
                "action": "assign",
                "detail": "房间 B2：安排玩家",
                "operator": "游戏管理台",
                "room_id": "B2",
            },
            {
                "ts": now - 30,
                "time": "2026-11-12 10:01:00",
                "action": "close_room",
                "detail": "关闭房间 B2",
                "operator": "游戏管理台",
                "room_id": "B2",
            },
        ],
    )

    filtered = manager.operation_log_page(limit=10, action="assign", now=now)
    assert filtered["total"] == 2
    assert set(filtered["actions"]) == {"assign", "close_room"}, "动作下拉要列出全部动作"
    assert [row["room_id"] for row in filtered["rows"]] == ["B2", "A1"], "仍然是最新在前"

    by_keyword = manager.operation_log_page(limit=10, keyword="a1", now=now)
    assert by_keyword["total"] == 1
    assert by_keyword["rows"][0]["room_id"] == "A1"

    by_days = manager.operation_log_page(limit=10, days=7, now=now)
    assert by_days["total"] == 2

    limited = manager.operation_log_page(limit=1, now=now)
    assert len(limited["rows"]) == 1
    assert limited["total"] == 3, "total 是命中总数，不受 limit 截断影响"


def test_operation_log_keeps_backwards_compatible_signature(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.record_operation("assign", "第一条")
    manager.record_operation("assign", "第二条")

    rows = manager.operation_log(1)

    assert len(rows) == 1 and rows[0]["detail"] == "第二条"


# ---------------- 管理台接口 ----------------


@pytest.mark.asyncio
async def test_page_players_scopes_and_returns_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    now = time.time()
    today = day_text(0, now=now)
    seed(manager, "gomoku", "10001", name="小明", wins=2, games=4, daily={today: {"w": 2, "g": 4, "d": 0}})
    seed(manager, "xiangqi", "10002", name="小红", wins=1, games=1)
    plugin = make_plugin(manager)
    monkeypatch.setattr(
        main_module, "request", FakeRequest({"game_type": "gomoku", "activity_days": 7})
    )

    result = await plugin.page_players()

    data = result["data"]
    assert data["game_type"] == "gomoku"
    assert [row["qq"] for row in data["players"]] == ["10001"]
    assert data["summary"]["games"] == 4
    assert data["activity"]["days"] == 7
    assert len(data["activity"]["series"]) == 7
    assert data["activity"]["series"][-1]["games"] == 4


@pytest.mark.asyncio
async def test_page_operation_log_passes_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    manager.record_operation("assign", "房间 A1：安排玩家", room_id="A1")
    manager.record_operation("close_room", "关闭房间 A1", room_id="A1")
    plugin = make_plugin(manager)
    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"limit": 50, "action": "assign", "keyword": "A1"}),
    )

    result = await plugin.page_operation_log()

    data = result["data"]
    assert [row["action"] for row in data["rows"]] == ["assign"]
    assert data["total"] == 1
    assert "close_room" in data["actions"]


# ---------------- 前端静态契约 ----------------


def test_stats_panel_exposes_activity_chart_and_log_filters() -> None:
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    style = (ROOT / "pages" / "游戏管理台" / "manager.css").read_text(encoding="utf-8")

    assert 'id="activityChart"' in page
    assert 'data-activity-days="7"' in page and 'data-activity-days="30"' in page
    assert 'id="logActionSelect"' in page
    assert 'id="logWindowSelect"' in page
    assert 'id="logSearch"' in page

    assert "async function loadPlayers(" in script
    assert "async function loadOperationLog(" in script
    assert "function renderActivity(" in script
    assert "activity_days: activityDays" in script
    assert "payload.game_type = statsFilter" in script
    # 玩法下拉变化时三块一起刷新，档案不会停留在旧玩法
    assert 'addEventListener("change", loadStats)' in script or "loadStats()" in script
    assert "logAction =" in script and "logKeyword =" in script

    assert ".activity-chart" in style and ".log-toolbar" in style
