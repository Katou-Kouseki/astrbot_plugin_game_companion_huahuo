from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from astrbot_plugin_game_companion_huahuo.server import GameRoomServer


class _FakePlugin:
    """只实现 _cfg_bool / _cfg_non_negative，模拟插件配置读取。"""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self.values.get(key, default))

    def _cfg_non_negative(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.values.get(key, default)))
        except (TypeError, ValueError):
            return default


def _snapshot(plugin: Any) -> dict[str, object]:
    # _activity_toast_snapshot 只用到 self.plugin，直接以简单对象调用
    return GameRoomServer._activity_toast_snapshot(SimpleNamespace(plugin=plugin))


def test_activity_toast_defaults_are_all_on() -> None:
    """默认：总开关开、观众进出也提醒、同一人冷却 15 秒。"""
    snapshot = _snapshot(_FakePlugin({}))

    assert snapshot == {"enabled": True, "spectators": True, "cooldown_seconds": 15}


def test_activity_toast_can_hide_spectator_events() -> None:
    """关掉「观众进出也提醒」后仍保留总开关与落座提醒能力。"""
    snapshot = _snapshot(_FakePlugin({"web_ui.activity_toast_spectators": False}))

    assert snapshot["enabled"] is True
    assert snapshot["spectators"] is False


def test_activity_toast_master_switch_and_zero_cooldown() -> None:
    """总开关关闭 / 冷却设为 0（不限制）都能原样下发。"""
    snapshot = _snapshot(
        _FakePlugin(
            {
                "web_ui.activity_toast_enabled": False,
                "web_ui.activity_toast_cooldown_seconds": 0,
            }
        )
    )

    assert snapshot["enabled"] is False
    assert snapshot["cooldown_seconds"] == 0


def test_activity_toast_survives_broken_config() -> None:
    """配置读取异常时回退默认值，不能把房间状态接口带崩。"""
    assert _snapshot(object()) == {
        "enabled": True,
        "spectators": True,
        "cooldown_seconds": 15,
    }

    class _Broken:
        def _cfg_bool(self, key: str, default: bool) -> bool:
            raise RuntimeError("boom")

        def _cfg_non_negative(self, key: str, default: int) -> int:
            return default

    assert _snapshot(_Broken())["enabled"] is True
