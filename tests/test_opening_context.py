from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_game_companion.draw_guess import DrawGuessGame, DrawWord
from astrbot_plugin_game_companion.gomoku import WHITE, GomokuGame
from astrbot_plugin_game_companion.main import GameCompanionPlugin
from astrbot_plugin_game_companion.models import GameRoom
from astrbot_plugin_game_companion.pig_dice import PigDiceGame
from astrbot_plugin_game_companion.tictactoe import NOUGHT, TicTacToeGame
from astrbot_plugin_game_companion.xiangqi import BLACK, XiangqiGame


def make_room(game_type: str, game: object) -> GameRoom:
    room = GameRoom(
        room_id="room-1",
        access_token="access-token",
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,  # type: ignore[arg-type]
        difficulty="normal",
    )
    room.game = game  # type: ignore[assignment]
    return room


@pytest.mark.parametrize(
    ("room", "expected"),
    [
        (
            make_room("gomoku", GomokuGame(human_color=WHITE)),
            ("玩家执白，Bot 执黑", "黑方先手", "由Bot先行"),
        ),
        (
            make_room("xiangqi", XiangqiGame(human_side=BLACK)),
            ("玩家执黑，Bot 执红", "红方先手", "由Bot先行"),
        ),
        (
            make_room("tictactoe", TicTacToeGame(human_mark=NOUGHT)),
            ("玩家执 O，Bot 执 X", "X 先手", "由Bot先行"),
        ),
        (
            make_room("pig_dice", PigDiceGame(turn="human")),
            ("随机先手结果已经确定", "由玩家先掷", "目标是先得到 50 分"),
        ),
    ],
)
def test_opening_commentary_uses_authoritative_side_and_first_turn(
    room: GameRoom, expected: tuple[str, ...]
) -> None:
    prompt = GameCompanionPlugin._opening_commentary_prompt(room)

    for fact in expected:
        assert fact in prompt
    assert "不得说反双方身份、颜色、标记或先后手" in prompt


def test_draw_guess_prompts_keep_drawer_and_guesser_roles_explicit() -> None:
    game = DrawGuessGame(target=DrawWord("苹果"))
    room = make_room("draw_guess", game)

    opening = GameCompanionPlugin._opening_commentary_prompt(room)
    unsolved = GameCompanionPlugin._round_result_text(
        room, {"result": "cooperative_unsolved"}, reveal_answer=True
    )
    game.record_guess("苹果", correct=True)
    solved = GameCompanionPlugin._round_result_text(
        room, {"result": "cooperative_success"}, reveal_answer=True
    )

    assert "用户始终作画，Bot 始终猜图，Bot 不参与绘画" in opening
    assert unsolved == "用户负责作画，Bot 本轮未能猜中，答案是“苹果”"
    assert solved == "用户负责作画，Bot 在第 1 次猜中了“苹果”"


@pytest.mark.asyncio
async def test_draw_guess_persona_prompt_forbids_bot_from_claiming_the_drawing() -> None:
    room = make_room("draw_guess", DrawGuessGame(target=DrawWord("苹果")))
    provider = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="下次我会猜中。"))
    )
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.context = SimpleNamespace(get_using_provider=lambda _session_id: provider)
    plugin._persona_prompt = AsyncMock(return_value="")
    plugin._memory_context = AsyncMock(return_value="")
    plugin._companion_scene_prompt = lambda _room: ""

    await plugin._generate_persona_text(room, "本轮结束")

    system_prompt = provider.text_chat.await_args.kwargs["system_prompt"]
    assert "用户始终负责作画" in system_prompt
    assert "你（Bot）始终负责看图猜答案" in system_prompt
    assert "不得声称自己画得好或不好" in system_prompt
