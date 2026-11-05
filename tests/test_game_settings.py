from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_game_companion_huahuo.blackjack import BlackjackCard, BlackjackGame
from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


def make_plugin() -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.config = {}
    plugin.manager = RoomManager()
    plugin.blackjack_max_players = 1
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
        "weiqi",
        "tictactoe",
        "othello",
        "turtle_soup",
        "pig_dice",
        "draw_guess",
        "blackjack",
        "undercover",
    }
    assert all(item["enabled"] is True for item in games.values())
    pig_fields = {item["key"]: item for item in games["pig_dice"]["fields"]}
    assert pig_fields["target_score"]["value"] == 50
    assert pig_fields["target_score"]["minimum"] == 20
    blackjack_fields = {
        item["key"]: item for item in games["blackjack"]["fields"]
    }
    assert blackjack_fields["max_players"]["value"] == 1
    assert blackjack_fields["max_players"]["maximum"] == 6
    weiqi_fields = {item["key"]: item for item in games["weiqi"]["fields"]}
    assert weiqi_fields["board_size"]["value"] == "13"
    assert {option["value"] for option in weiqi_fields["board_size"]["options"]} == {
        "9",
        "13",
        "19",
    }
    assert weiqi_fields["komi"]["value"] == "7.5"


@pytest.mark.asyncio
async def test_validated_settings_persist_and_apply_without_reloading_plugin() -> None:
    plugin = make_plugin()
    changes = plugin._validated_game_settings(
        {
            "games": {
                "gomoku": {"enabled": False},
                "othello": {"enabled": False},
                "pig_dice": {"enabled": True, "target_score": 80},
                "draw_guess": {
                    "enabled": True,
                    "duration_seconds": 180,
                    "max_guesses": 7,
                    "vision_provider_id": "vision-provider",
                },
                "blackjack": {"enabled": True, "max_players": 3},
            }
        }
    )

    await plugin._persist_game_settings(plugin._game_settings_config_patch(changes))
    plugin._apply_game_settings_runtime()

    assert plugin.config["gomoku"]["enabled"] is False
    assert plugin.manager.game_enabled("gomoku") is False
    assert plugin.config["othello"]["enabled"] is False
    assert plugin.manager.game_enabled("othello") is False
    assert plugin.manager.pig_dice_target_score == 80
    assert plugin.manager.draw_guess_duration_seconds == 180
    assert plugin.manager.draw_guess_max_guesses == 7
    assert plugin.draw_guess_vision_provider_id == "vision-provider"
    assert plugin.manager.blackjack_max_players == 3


@pytest.mark.asyncio
async def test_accepted_blackjack_rematch_starts_a_new_round_in_the_same_room(
    monkeypatch,
) -> None:
    plugin = make_plugin()
    plugin._generate_persona_text = AsyncMock(
        return_value='{"accept": true, "difficulty": "hard", "reply": "再来一局。"}'
    )
    original_deal = BlackjackGame.deal.__func__

    def fixed_deal(*, difficulty, player_numbers, **_kwargs):
        ranks = ["10", "6", "9", "8"]
        suits = ("♠", "♥", "♦", "♣")
        shoe = [
            BlackjackCard(rank=rank, suit=suits[index % 4])
            for index, rank in enumerate(reversed(ranks))
        ]
        return original_deal(
            BlackjackGame,
            difficulty=difficulty,
            player_numbers=player_numbers,
            shoe=shoe,
        )

    monkeypatch.setattr(BlackjackGame, "deal", staticmethod(fixed_deal))
    room = await plugin.manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type="blackjack",
        difficulty="normal",
    )
    visitor = await plugin.manager.join(room)
    await plugin.manager.claim_and_start(room, visitor.token, "")
    previous_game = room.game
    assert isinstance(previous_game, BlackjackGame)
    previous_game.finished = True
    room.status = "finished"

    await plugin.manager.request_rematch(room, visitor.token)
    await plugin._decide_rematch(room, visitor=visitor)

    assert room.status == "active"
    assert isinstance(room.game, BlackjackGame)
    assert room.game is not previous_game
    assert room.difficulty == "hard"


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


def test_game_catalog_has_no_duplicate_config_keys() -> None:
    """管理台按 config_key 合并提交，同一 key 不能出现在多个玩法面板里。

    棋类时间配置曾在五子棋/象棋/围棋/黑白棋四个面板各放一份，保存时前端提交
    全量字段，后遍历的玩法会用旧值覆盖前面的新值——表现就是「改哪个都不生效」。
    """
    from astrbot_plugin_game_companion_huahuo.main import GAME_CATALOG

    seen: dict[str, str] = {}
    for game in GAME_CATALOG:
        for field in game["fields"]:
            config_key = str(field["config_key"])
            assert config_key not in seen, (
                f"{config_key} 同时出现在 {seen[config_key]} 和 {game['game_type']}"
            )
            seen[config_key] = str(game["game_type"])


def test_undercover_settings_apply_without_reloading_plugin() -> None:
    """管理台里能改的卧底参数必须同步到 manager。

    以前 `_apply_game_settings_runtime` 整段漏了 undercover_*：保存后页面显示新值，
    实际对局仍用启动时的旧值（要重启插件才生效）。
    """
    plugin = make_plugin()
    plugin.config = {
        "undercover": {
            "max_players": 8,
            "min_players": 4,
            "camp_scales_default": "3 1 0",
            "allow_host_customize_camp_scales": False,
            "match_seconds": 120,
            "prepare_seconds": 5,
            "speaking_seconds": 90,
            "voting_seconds": 60,
            "first_round_non_voting": 5,
            "send_identity_in_card": False,
            "show_voters": True,
            "similarity": 66,
            "ai_fill_enabled": False,
            "ai_fill_min_players": 5,
        }
    }

    plugin._apply_game_settings_runtime()

    manager = plugin.manager
    assert manager.undercover_max_players == 8
    assert manager.undercover_min_players == 4
    assert manager.undercover_match_seconds == 120
    assert manager.undercover_prepare_seconds == 5
    assert manager.undercover_speaking_seconds == 90
    assert manager.undercover_voting_seconds == 60
    assert manager.undercover_first_round_non_voting == 5
    assert manager.undercover_send_identity_in_card is False
    assert manager.undercover_show_voters is True
    assert manager.undercover_similarity == 66
    assert manager.undercover_allow_host_customize_camp_scales is False
    assert manager.undercover_ai_fill_enabled is False
    assert manager.undercover_ai_fill_min_players == 5
    assert manager.undercover_camp_scales_default == (3, 1, 0)
