from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager

ROOT = Path(__file__).resolve().parents[1]


def _manager(tmp_path: Path) -> RoomManager:
    manager = RoomManager(max_private_rooms=0)
    manager.operation_log_path = tmp_path / "operation_log.jsonl"
    return manager


def test_record_operation_writes_room_and_game_fields(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    manager.record_operation(
        "room_claim",
        "房间 A1：落座玩家席 · 小明（1号）",
        operator="小明（1号）",
        room_id="A1",
        game_type="gomoku",
    )
    manager.record_operation("clear_leaderboard", "清空战绩排行榜", operator="游戏管理台")

    rows = manager.operation_log(10)

    # 倒序：最新一条在前
    assert [row["action"] for row in rows] == ["clear_leaderboard", "room_claim"]
    claim = rows[1]
    assert claim["room_id"] == "A1"
    assert claim["game_type"] == "gomoku"
    assert claim["operator"] == "小明（1号）"
    # 未提供房间/玩法时写空串，前端按「—」展示
    assert rows[0]["room_id"] == ""
    assert rows[0]["game_type"] == ""


def test_operation_log_reads_legacy_rows_without_new_fields(tmp_path: Path) -> None:
    """旧日志（无 room_id/game_type）仍能正常读取，不因缺字段报错。"""
    manager = _manager(tmp_path)
    legacy = {
        "ts": 1.0,
        "time": "2026-11-01 10:00:00",
        "action": "close_room",
        "detail": "关闭房间 A0",
        "operator": "",
    }
    manager.operation_log_path.write_text(
        json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rows = manager.operation_log(10)

    assert len(rows) == 1
    assert rows[0]["action"] == "close_room"
    assert rows[0].get("room_id", "") == ""


def test_operation_log_is_newest_first_and_honours_limit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    for index in range(5):
        manager.record_operation("assign", f"第 {index} 条")

    rows = manager.operation_log(3)

    assert len(rows) == 3
    assert [row["detail"] for row in rows] == ["第 4 条", "第 3 条", "第 2 条"]


def test_operation_log_ignores_malformed_lines(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.record_operation("assign", "正常一条")
    with manager.operation_log_path.open("a", encoding="utf-8") as handle:
        handle.write("{不是 JSON}\n")

    rows = manager.operation_log(10)

    assert len(rows) == 1
    assert rows[0]["detail"] == "正常一条"


def test_room_server_logs_key_operations() -> None:
    """房间内关键操作必须走统一的 _log_room_op 落日志（高频动作不记）。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    assert "def _log_room_op(" in server
    assert "def _visitor_label(" in server
    # 只有封装内部直接调 record_operation，避免各处散落
    assert server.count(".record_operation(") == 1

    for action in (
        "room_claim",
        "room_start",
        "room_seat_leave",
        "room_leave",
        "room_seat_swap",
        "room_ai_seat",
        "room_rematch",
        "room_resign",
        "room_difficulty",
        "room_side_rotation",
        "room_uc_ai",
        "room_uc_scales",
        "room_uc_reveal_identity",
        "room_uc_words",
        "room_announce",
    ):
        assert f'"{action}"' in server, f"缺少操作日志动作 {action}"


def test_operation_log_table_shows_operator_and_room() -> None:
    """管理台操作日志表新增「操作者 / 房间」两列。"""
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")

    assert "<th>操作者</th>" in page
    assert "<th>房间</th>" in page
    assert 'createText("td", row.operator || "—")' in script
    assert 'createText("td", row.room_id || "—")' in script
    # 房间内动作要有可读中文标签
    assert "room_claim: \"落座\"" in script
    assert "room_resign: \"认输\"" in script
