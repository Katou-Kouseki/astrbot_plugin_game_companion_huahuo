"""全局战绩存档的加载清洗：重启不能把新版本写进磁盘的字段抹掉。

0.6.18 起每条战绩多了 `daily`（按天分桶）、`draws`（平局）、`last_at`（最近对局），
0.6.22 的玩家档案、时间窗口排行、0.6.23 的活跃趋势都依赖它们。加载时若只保留
name/wins/games，插件一重启这些能力就集体失效，而且下一次保存会把磁盘上的
原始记录也覆盖掉（不可逆）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def manager_at(path: Path) -> RoomManager:
    return RoomManager(max_private_rooms=0, global_stats_path=path)


def test_reload_preserves_daily_draws_and_last_active(tmp_path: Path) -> None:
    path = tmp_path / "global.json"
    today = time.strftime("%Y-%m-%d")
    first = manager_at(path)
    first.global_player_wins = {
        "gomoku": {
            "10001": {
                "name": "小明",
                "wins": 2,
                "games": 5,
                "draws": 1,
                "last_at": 1_700_000_000.0,
                "daily": {today: {"w": 2, "g": 5, "d": 1}},
            }
        }
    }
    first._save_global_stats()

    second = manager_at(path)

    entry = second.global_player_wins["gomoku"]["10001"]
    assert entry["draws"] == 1
    assert entry["last_at"] == 1_700_000_000.0
    assert entry["daily"][today]["g"] == 5
    # 三个依赖这些字段的功能都要还活着
    assert second.global_leaderboard("gomoku", days=7)[0]["draws"] == 1
    assert second.player_profiles()[0]["last_at_text"]
    assert second.activity_series(days=7)[-1]["games"] == 5
    # 再存一次不能把刚读进来的字段又抹掉
    second._save_global_stats()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gomoku"]["10001"]["daily"] == {today: {"w": 2, "g": 5, "d": 1}}
    assert raw["gomoku"]["10001"]["draws"] == 1


def test_reload_sanitizes_broken_values(tmp_path: Path) -> None:
    """存档被写坏时加载清洗，别让排行榜 / 档案接口一读就崩。"""
    path = tmp_path / "global.json"
    path.write_text(
        json.dumps(
            {
                "gomoku": {
                    "10001": {
                        "name": "小明",
                        "wins": "abc",
                        "games": -3,
                        "draws": "99",
                        "last_at": "bad",
                        "daily": {
                            "2026-11-12": {"w": "x", "g": 4, "d": None},
                            "broken": "not a dict",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    manager = manager_at(path)

    entry = manager.global_player_wins["gomoku"]["10001"]
    assert entry["wins"] == 0
    assert entry["games"] == 0
    assert entry["draws"] == 0
    assert entry["last_at"] == 0.0
    assert manager.activity_series(days=7)[-1]["games"] == 4
    assert manager.global_leaderboard("gomoku")[0]["losses"] == 0
    assert manager.player_profiles()[0]["losses"] == 0


def test_reload_keeps_legacy_flat_stats_readable(tmp_path: Path) -> None:
    """0.5.20 之前的扁平存档仍能迁移，不会因新字段校验整个作废。"""
    path = tmp_path / "global.json"
    path.write_text(
        json.dumps({"10001": {"name": "老玩家", "wins": 12}}), encoding="utf-8"
    )

    manager = manager_at(path)

    assert manager.global_leaderboard("undercover")[0]["wins"] == 12
    assert manager.player_profiles()[0]["qq"] == "10001"
