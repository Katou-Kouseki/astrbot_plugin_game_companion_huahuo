from __future__ import annotations

from astrbot_plugin_game_companion_huahuo.undercover import UndercoverGame


def test_attach_players_assigns_stable_uids() -> None:
    game = UndercoverGame()
    game.attach_players(
        [(1, "10001", "甲"), (2, "10002", "乙"), (3, "10003", "丙")],
        uids=["u-a", "u-b", "u-c"],
    )

    assert [p.uid for p in game.players] == ["u-a", "u-b", "u-c"]
    assert game.player_for_uid("u-c").number == 3
    assert game.player_for_uid("u-c").display_name == "丙"
    assert game.player_for_uid("missing") is None


def test_attach_players_defaults_uid_to_number_string() -> None:
    game = UndercoverGame()
    game.attach_players([(2, "", "乙"), (5, "", "戊")])

    assert game.player_for_uid("5").number == 5
    assert game.player_for_uid("2").number == 2


def test_player_for_number_still_resolves_by_frozen_number() -> None:
    game = UndercoverGame()
    game.attach_players([(1, "a", "甲"), (2, "b", "乙")], uids=["x", "y"])

    assert game.player_for_number(2).uid == "y"
    assert game.player_for_number(9) is None


def test_uid_mismatch_is_not_resolved_by_number() -> None:
    """按 uid 反查不能回退成按号码猜：号码会被重编，uid 才是唯一稳定标识。"""
    game = UndercoverGame()
    game.attach_players([(1, "a", "甲"), (2, "b", "乙")], uids=["x", "y"])

    # 座位号 2 的玩家 uid 是 "y"，用「重编后的号码」直接查不会命中错的 uid
    assert game.player_for_uid("2") is None
    assert game.player_for_uid("y").number == 2
