"""本轮全量审计的回归用例：只补真实行为，不靠源码字符串匹配。

先前的静态契约测试把「字符串还在」当成功能正常，结果 0.6.17 的揭晓去重、
0.6.19 的平局名字、0.6.22 的档案清理等都是"代码写了、常见路径却不生效"。
这里把最容易回退的几条链路钉成行为断言。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo import main as main_module
from astrbot_plugin_game_companion_huahuo import qq_bridge
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self, default=None):
        return dict(self.payload)


def make_manager(tmp_path: Path) -> RoomManager:
    return RoomManager(max_private_rooms=0, global_stats_path=tmp_path / "global.json")


def make_plugin(manager: RoomManager) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.manager = manager
    return plugin


def Number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------- 战报下发链路 ----------------


@pytest.mark.asyncio
async def test_public_snapshot_forwards_draw_and_human_names(tmp_path: Path) -> None:
    """退房/再来一局后座位可能已空，平局双方名字必须随房间快照下发。"""
    manager = make_manager(tmp_path)
    room = await manager.create_room(
        source="private",
        session_id="UMO:t:PM:audit",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="小明",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    room.last_board_result = {
        "game_type": "gomoku",
        "draw": True,
        "winner_name": "",
        "loser_name": "",
        "moves": 42,
        "draw_names": "小明（执黑） vs 花火",
        "human_name": "小明",
    }

    board = room.public_snapshot().get("board_result") or {}

    assert board["draw_names"] == "小明（执黑） vs 花火"
    assert board["human_name"] == "小明"
    assert board["draw"] is True


def test_solo_draw_names_fall_back_to_human_vs_ai(tmp_path: Path) -> None:
    """没启用玩家席的人机平局，服务端战报也要写出双方。"""
    manager = make_manager(tmp_path)
    room = SimpleNamespace(
        multiplayer=SimpleNamespace(seats=[]),
        player_token="",
        visitors={},
    )

    assert manager._board_draw_names(room) == "玩家 vs 花火"  # noqa: SLF001


@pytest.mark.asyncio
async def test_auto_join_keeps_a_running_solo_game_alive() -> None:
    """群里第一次说话不该把单人房正在玩的一局清掉。"""
    game = object()
    room = SimpleNamespace(
        status="active",
        multiplayer=SimpleNamespace(enabled=False, seats=[], capacity=0),
        player_qq="",
        player_token="",
        game=game,
        visitors={},
        add_message=lambda *_args, **_kwargs: None,
    )

    class Manager:
        async def join(self, current_room, _token=""):
            visitor = SimpleNamespace(
                token="v1", number=1, display_name="", identity_confirmed=False, qq=""
            )
            current_room.visitors["v1"] = visitor
            return visitor

        async def assign_player(self, current_room, _number, qq, **_kwargs):
            current_room.game = None
            current_room.status = "setup"
            current_room.player_qq = qq
            current_room.player_token = "v1"

    plugin = SimpleNamespace(manager=Manager(), turtle_soup_qq_auto_bind=True)

    ok, _ = await qq_bridge.ensure_qq_seat(plugin, room, "10001", "小明")

    assert ok is True
    assert room.game is game, "局中加入不能销毁 room.game"
    assert room.status == "active", "局中加入不能把房间打回 setup"


@pytest.mark.asyncio
async def test_failed_auto_join_does_not_leave_orphan_visitors() -> None:
    room = SimpleNamespace(
        status="active",
        multiplayer=SimpleNamespace(enabled=False, seats=[], capacity=0),
        player_qq="",
        player_token="",
        game=object(),
        visitors={},
        add_message=lambda *_args, **_kwargs: None,
    )

    class Manager:
        async def join(self, current_room, _token=""):
            visitor = SimpleNamespace(
                token="v1", number=1, display_name="", identity_confirmed=False, qq=""
            )
            current_room.visitors["v1"] = visitor
            return visitor

        async def assign_player(self, _room, _number, _qq, **_kwargs):
            raise ValueError("座位冲突")

    plugin = SimpleNamespace(manager=Manager(), turtle_soup_qq_auto_bind=True)

    for _ in range(3):
        ok, _ = await qq_bridge.ensure_qq_seat(plugin, room, "10001", "小明")
        assert ok is False

    assert room.visitors == {}, "每次失败都新建访客却不回滚会一直堆积"


# ---------------- 群指令揭晓不得双份 ----------------


@pytest.mark.asyncio
async def test_group_reveal_receipt_suppresses_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """群指令回执已发群 → 不再广播；只交给模型转述的路径 → 广播兜底。

    bridge 层 announce 表示「回执是否已发群」，manager 层 announce_in_group
    表示「是否广播」，两者语义相反，必须在 run_soup_action 里取反。
    """
    dispatch = AsyncMock(return_value="🥣 汤底揭晓")
    monkeypatch.setattr(qq_bridge, "_dispatch", dispatch)
    monkeypatch.setattr(qq_bridge, "ensure_qq_seat", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(qq_bridge, "_mark_active", lambda _room, _qq: None)
    room = SimpleNamespace(session_id="s")

    await qq_bridge.run_soup_action(None, room, "reveal", "", "10001")
    assert dispatch.call_args.kwargs["announce"] is False, "群指令回执已发群，不该再广播一条"

    dispatch.reset_mock()
    await qq_bridge.run_soup_action(None, room, "reveal", "", "10001", announce=False)
    assert dispatch.call_args.kwargs["announce"] is True, "模型转述路径必须由广播兜底"


# ---------------- 战绩脏数据不能让接口 500 ----------------


def test_leaderboard_and_profiles_tolerate_dirty_numbers(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    today = time.strftime("%Y-%m-%d")
    manager.global_player_wins = {
        "gomoku": {
            "10001": {
                "name": "小明",
                "wins": "abc",
                "games": -2,
                "draws": "99",
                "last_at": "bad",
                "daily": {today: {"w": "1.5", "g": 2, "d": None}},
            }
        }
    }

    all_rows = manager.global_leaderboard("gomoku")
    window_rows = manager.global_leaderboard("gomoku", days=7)
    profiles = manager.player_profiles()

    for row in all_rows + profiles:
        assert 0 <= Number(row["winrate"]) <= 100
        assert Number(row["losses"]) >= 0
    assert all_rows and profiles
    assert isinstance(window_rows, list)


def test_set_player_stats_keeps_draws_consistent(tmp_path: Path) -> None:
    """胜场 + 平局不能超过总局数，否则负场被 clamp 成 0 掩盖异常。"""
    manager = make_manager(tmp_path)
    manager.global_player_wins = {
        "gomoku": {
            "10001": {"name": "小明", "wins": 2, "games": 5, "draws": 3, "daily": {}}
        }
    }

    manager.set_player_stats("10001", "gomoku", wins=5, games=5)

    entry = manager.global_player_wins["gomoku"]["10001"]
    assert entry["wins"] + entry["draws"] <= entry["games"]


# ---------------- 管理台接口契约 ----------------


@pytest.mark.asyncio
async def test_clear_leaderboard_with_no_data_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「该玩法暂无数据」是正常结果，不能返回 status:error 让前端抛异常。"""
    plugin = make_plugin(make_manager(tmp_path))
    monkeypatch.setattr(main_module, "request", FakeRequest({"game_type": "gomoku"}))

    result = await plugin.page_clear_leaderboard()

    assert result["status"] == "ok"
    assert not result["data"].get("cleared")
    assert result["message"]


@pytest.mark.asyncio
async def test_page_leaderboard_bad_limit_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = make_plugin(make_manager(tmp_path))
    monkeypatch.setattr(main_module, "request", FakeRequest({"limit": "abc"}))

    result = await plugin.page_leaderboard()

    assert result["status"] == "ok"
    assert "leaderboard" in result["data"]


def test_operation_log_action_is_case_insensitive(tmp_path: Path) -> None:
    """历史日志混入大写动作时，下拉不该出现两个近似的筛选项。"""
    manager = make_manager(tmp_path)
    log = manager.operation_log_path
    assert log is not None
    now = time.time()
    log.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": now - index,
                    "time": "2026-11-12 10:00:00",
                    "action": action,
                    "detail": f"第 {index} 条",
                }
            )
            for index, action in enumerate(("ASSIGN", "assign"))
        )
        + "\n",
        encoding="utf-8",
    )

    result = manager.operation_log_page(10, action="assign")

    assert result["total"] == 2
    assert len(result["actions"]) == 1


# ---------------- 后台出题任务状态 ----------------


def test_stale_job_snapshot_does_not_mutate_the_live_job() -> None:
    """超时只影响展示副本；任务 finally 仍能把真实 completed 写回本体。"""
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    job = {
        "state": "running",
        "model": "model-x",
        "total": 1,
        "done": 1,
        "titles": ["已入库的题"],
        "error": "",
        "started_at": time.time() - 7200,
        "finished_at": 0.0,
    }
    plugin._soup_generate_job = job  # noqa: SLF001

    snapshot = plugin._soup_generate_job_snapshot()  # noqa: SLF001

    assert snapshot["state"] == "failed"
    assert job["state"] == "running", "GET 快照不能改写正在跑的任务本体"
    job["state"] = "completed"  # 模拟 _run_soup_generate_job 的 finally
    assert plugin._soup_generate_job_snapshot()["state"] == "completed"  # noqa: SLF001
