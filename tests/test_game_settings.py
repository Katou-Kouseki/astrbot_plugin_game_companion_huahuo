from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.room_manager import RoomManager


def make_plugin() -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.config = {}
    plugin.manager = RoomManager()
    plugin.xiangqi_engine = SimpleNamespace(
        allow_download=True,
        auto_download=False,
    )
    plugin._settings_lock = asyncio.Lock()
    return plugin


def test_settings_snapshot_contains_every_game_and_editable_values() -> None:
    plugin = make_plugin()

    snapshot = plugin._game_settings_snapshot()

    games = {item["game_type"]: item for item in snapshot["games"]}
    assert set(games) == {
        "gomoku",
        "xiangqi",
        "tictactoe",
        "turtle_soup",
        "pig_dice",
        "draw_guess",
    }
    assert all(item["enabled"] is True for item in games.values())
    pig_fields = {item["key"]: item for item in games["pig_dice"]["fields"]}
    assert pig_fields["target_score"]["value"] == 50
    assert pig_fields["target_score"]["minimum"] == 20


@pytest.mark.asyncio
async def test_validated_settings_persist_and_apply_without_reloading_plugin() -> None:
    plugin = make_plugin()
    changes = plugin._validated_game_settings(
        {
            "games": {
                "gomoku": {"enabled": False},
                "pig_dice": {"enabled": True, "target_score": 80},
                "draw_guess": {
                    "enabled": True,
                    "duration_seconds": 180,
                    "max_guesses": 7,
                    "vision_provider_id": "vision-provider",
                },
            }
        }
    )

    await plugin._persist_game_settings(plugin._game_settings_config_patch(changes))
    plugin._apply_game_settings_runtime()

    assert plugin.config["gomoku"]["enabled"] is False
    assert plugin.manager.game_enabled("gomoku") is False
    assert plugin.manager.pig_dice_target_score == 80
    assert plugin.manager.draw_guess_duration_seconds == 180
    assert plugin.manager.draw_guess_max_guesses == 7
    assert plugin.draw_guess_vision_provider_id == "vision-provider"


@pytest.mark.parametrize(
    "payload",
    [
        {"games": {"pig_dice": {"target_score": 19}}},
        {"games": {"draw_guess": {"max_guesses": 11}}},
        {"games": {"turtle_soup": {"content_level": "invalid"}}},
        {"games": {"gomoku": {"enabled": "yes"}}},
        {"games": {"unknown": {"enabled": True}}},
    ],
)
def test_invalid_game_settings_are_rejected_atomically(payload: dict) -> None:
    plugin = make_plugin()

    with pytest.raises(ValueError):
        plugin._validated_game_settings(payload)

    assert plugin.config == {}
