"""全局战绩榜的胜率必须把「输了的局」也算进去。

历史问题：只有棋类（含井字棋 / 二十一点 / 贪心骰子）在结算时累加 `games`，
海龟汤、你画我猜、谁是卧底只给赢家 `wins + 1`、从不记局数；而排行榜又把局数
兜底成「至少等于胜场」，于是这些玩法不管输多少局，胜率永远是 100%。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping
from astrbot_plugin_game_companion_huahuo.undercover import UCPlayer, UndercoverGame

SOLUTION = (
    "他是列车上的乘客，收到一条说列车即将脱轨的短信，周围没人像在恶作剧，"
    "他无法确认真假，恐惧把他压垮了。"
)


def make_puzzle():  # noqa: ANN201
    return puzzle_from_mapping(
        {
            "title": "列车",
            "surface": "他在列车上收到一条短信，看完之后整个人就崩溃了，为什么？",
            "solution": SOLUTION,
            "key_facts": ["收到短信", "说列车要脱轨", "他相信了"],
            "acceptable_variants": [],
            "hints": ["和手机有关", "和列车安全有关", "他没法确认"],
            "theme": "列车",
            "trick": "把恶作剧短信当真",
        },
        content_level="all_ages",
    )


async def build_soup_room(manager: RoomManager, qq: str = "10001"):  # noqa: ANN201
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:40001",
        platform="aiocqhttp",
        group_id="40001",
        creator_qq=qq,
        creator_name="甲",
        admin_room=False,
        game_type="turtle_soup",
        difficulty="normal",
        turtle_soup_mode="bot_host",
    )
    player = await manager.join(room)
    await manager.claim_and_start(room, player.token, "")
    if room.status == "setup":
        await manager.start_game(room, player.token, "")
    if room.game.puzzle is None:
        await manager.complete_turtle_soup_generation(room, room.game, make_puzzle())
    await manager.assign_player(room, player.number, qq, allow_non_numeric=True)
    return room


def rows_by_name(manager: RoomManager, game_type: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["name"]): row for row in manager.global_leaderboard(game_type, limit=50)
    }


def test_human_global_wins_counts_losses_too() -> None:
    """核心回归：输了要记局数，胜率才会低于 100%。"""
    manager = RoomManager()
    room = SimpleNamespace(
        game_type="turtle_soup",
        multiplayer=SimpleNamespace(
            enabled=False,
            seats=[],
        ),
        player_qq="10001",
        player_token="tk",
        player_identity_confirmed=True,
        visitors={},
    )

    manager._credit_human_global_wins(room, won=False)  # noqa: SLF001
    manager._credit_human_global_wins(room, won=False)  # noqa: SLF001
    manager._credit_human_global_wins(room, won=True)  # noqa: SLF001

    entry = manager.global_player_wins["turtle_soup"]["10001"]
    assert entry["wins"] == 1
    assert entry["games"] == 3
    row = manager.global_leaderboard("turtle_soup")[0]
    assert row["winrate"] == pytest.approx(33.3, abs=0.1)
    # 按天分桶也要记局数，否则日报 / 周报窗口里的胜率同样是 100%
    day = next(iter(entry["daily"].values()))
    assert day == {"w": 1, "g": 3}


@pytest.mark.asyncio
async def test_unsolved_soup_is_recorded_as_a_loss() -> None:
    """海龟汤揭晓汤底（没人解开）也要进账，否则胜率永远 100%。"""
    manager = RoomManager()
    room = await build_soup_room(manager)
    game = room.game
    game.phase = "finished"
    game.solved = False
    game.end_reason = "revealed"
    room.status = "active"

    await manager._finish_game(room)  # noqa: SLF001

    entry = manager.global_player_wins["turtle_soup"]["10001"]
    assert entry["games"] == 1
    assert entry["wins"] == 0
    row = rows_by_name(manager, "turtle_soup")["甲"]
    assert row["games"] == 1 and row["wins"] == 0 and row["winrate"] == 0.0


@pytest.mark.asyncio
async def test_solved_soup_counts_a_win_and_a_game() -> None:
    """解开的那一局当然是「1 胜 / 1 局」，不能被算成 2 局。"""
    manager = RoomManager()
    room = await build_soup_room(manager)
    game = room.game
    game.phase = "finished"
    game.solved = True
    game.end_reason = "solved"
    room.status = "active"

    await manager._finish_game(room)  # noqa: SLF001

    entry = manager.global_player_wins["turtle_soup"]["10001"]
    assert entry["wins"] == 1 and entry["games"] == 1
    assert manager.global_leaderboard("turtle_soup")[0]["winrate"] == 100.0


@pytest.mark.asyncio
async def test_player_host_rewards_the_surviving_author() -> None:
    """玩家出题没人猜中：守住题目的出题人记胜，而不是跟着 winner="bot" 记负。"""
    manager = RoomManager()
    room = await build_soup_room(manager)
    game = room.game
    game.mode = "player_host"
    game.host_number = room.multiplayer.seats[0].number
    game.phase = "finished"
    game.solved = False
    game.bot_solved = False
    game.end_reason = "revealed"
    room.status = "active"

    await manager._finish_game(room)  # noqa: SLF001

    entry = manager.global_player_wins["turtle_soup"]["10001"]
    assert entry["wins"] == 1 and entry["games"] == 1


@pytest.mark.asyncio
async def test_undercover_losers_still_get_a_game() -> None:
    """卧底：输的阵营也记一局，赢的阵营额外记胜。"""
    manager = RoomManager()
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:40002",
        platform="aiocqhttp",
        group_id="40002",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )
    game = UndercoverGame()
    game.players = [
        UCPlayer(number=1, qq="10001", display_name="甲", camp="civilian", uid="u1"),
        UCPlayer(number=2, qq="10002", display_name="乙", camp="undercover", uid="u2"),
    ]
    game.winner = {"camp": "civilian"}
    game.finished = True
    room.game = game
    room.status = "active"
    for seat in list(room.multiplayer.seats):
        room.multiplayer.seats.remove(seat)
    from astrbot_plugin_game_companion_huahuo.models import PlayerSeat

    room.multiplayer.seats.extend(
        [
            PlayerSeat(
                number=1,
                visitor_token="t1",
                qq="10001",
                display_name="甲",
                identity_confirmed=True,
            ),
            PlayerSeat(
                number=2,
                visitor_token="t2",
                qq="10002",
                display_name="乙",
                identity_confirmed=True,
            ),
        ]
    )

    await manager._finish_game(room)  # noqa: SLF001

    winner = manager.global_player_wins["undercover"]["10001"]
    loser = manager.global_player_wins["undercover"]["10002"]
    assert winner["wins"] == 1 and winner["games"] == 1
    assert loser["wins"] == 0 and loser["games"] == 1, "输家也要记局数，否则胜率虚高"
    rows = rows_by_name(manager, "undercover")
    assert rows["乙"]["winrate"] == 0.0
    # 房间内「本房战绩」仍只记赢家，不能因为要记局数就冒出 0 胜的行
    assert "t2" not in room.player_win_counts


@pytest.mark.asyncio
async def test_undercover_camp_badges_only_for_the_winning_camp() -> None:
    """记局数不能顺手把输家也算进阵营胜场（徽章靠这份数据解锁）。"""
    manager = RoomManager()
    room = await manager.create_room(
        source="group",
        session_id="aiocqhttp:group:40003",
        platform="aiocqhttp",
        group_id="40003",
        creator_qq="10001",
        creator_name="甲",
        admin_room=False,
        game_type="undercover",
        difficulty="normal",
    )
    game = UndercoverGame()
    game.players = [
        UCPlayer(number=1, qq="10001", display_name="甲", camp="civilian", uid="u1"),
        UCPlayer(number=2, qq="10002", display_name="乙", camp="undercover", uid="u2"),
    ]
    game.winner = {"camp": "undercover"}
    game.finished = True
    room.game = game
    room.status = "active"
    from astrbot_plugin_game_companion_huahuo.models import PlayerSeat

    room.multiplayer.seats.clear()
    room.multiplayer.seats.extend(
        [
            PlayerSeat(
                number=1,
                visitor_token="t1",
                qq="10001",
                display_name="甲",
                identity_confirmed=True,
            ),
            PlayerSeat(
                number=2,
                visitor_token="t2",
                qq="10002",
                display_name="乙",
                identity_confirmed=True,
            ),
        ]
    )

    await manager._finish_game(room)  # noqa: SLF001

    assert manager.undercover_camp_wins["qq:10002"]["undercover"] == 1
    assert "civilian" not in manager.undercover_camp_wins.get("qq:10001", {})
