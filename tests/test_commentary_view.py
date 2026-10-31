"""花火解说的视角与输出清洗。

两位真人同房时花火只是旁观解说，不能把自己算成对局一方；
解说使用 commentary.provider_id 指定的 Provider，且生成后要洗掉「角色名：」这类标签。
"""

from __future__ import annotations

import types

import pytest

from astrbot_plugin_game_companion_huahuo.main import (
    GameCompanionPlugin,
    _commentary_duel_room,
    _commentary_sides_text,
    _strip_role_prefix,
)
from astrbot_plugin_game_companion_huahuo.models import PlayerSeat
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager


class _Provider:
    """记录调用参数的假 Provider。"""

    def __init__(self, *, reply: str = "测试解说") -> None:
        self.calls: list[dict[str, object]] = []
        self.reply = reply

    async def text_chat(self, prompt, system_prompt=None):  # noqa: ANN001
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return types.SimpleNamespace(completion_text=self.reply)


class _Context:
    def __init__(self, session_provider, by_id=None) -> None:  # noqa: ANN001
        self._session = session_provider
        self._by_id = by_id or {}

    def get_using_provider(self, _session_id):  # noqa: ANN001
        return self._session

    def get_provider_by_id(self, provider_id):  # noqa: ANN001
        return self._by_id.get(provider_id)


async def make_room(manager: RoomManager, game_type: str = "gomoku"):
    return await manager.create_room(
        source="private",
        session_id="aiocqhttp:private:10001",
        platform="aiocqhttp",
        group_id="",
        creator_qq="10001",
        creator_name="创建者",
        admin_room=False,
        game_type=game_type,
        difficulty="normal",
    )


async def two_human_room(game_type: str = "gomoku"):
    manager = RoomManager()
    room = await make_room(manager, game_type)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    second = await manager.join(room)
    await manager.claim_and_start(room, second.token, "")
    if room.status != "active":
        await manager.start_game(room, first.token, "")
    room.multiplayer.seats[0].display_name = "小明"
    room.multiplayer.seats[1].display_name = "阿花"
    return manager, room


async def solo_room():
    manager = RoomManager()
    room = await make_room(manager)
    first = await manager.join(room)
    await manager.claim_and_start(room, first.token, "")
    await manager.start_game(room, first.token, "")
    return manager, room


def plugin_for(manager: RoomManager, *, provider_id: str = ""):
    plugin = object.__new__(GameCompanionPlugin)
    plugin.manager = manager
    plugin.commentary_provider_id = provider_id
    return plugin


def stub_persona(plugin) -> None:  # noqa: ANN001
    async def persona(_room):
        return "人格：花火"

    async def memory(_room, _query):
        return ""

    def scene(_room):
        return ""

    plugin._persona_prompt = persona
    plugin._memory_context = memory
    plugin._companion_scene_prompt = scene


@pytest.mark.asyncio
async def test_duel_room_is_narrated_from_the_spectator_side() -> None:
    manager, room = await two_human_room()
    plugin = plugin_for(manager)

    assert _commentary_duel_room(room) is True

    opening = plugin._opening_commentary_prompt(room)
    assert "小明执黑" in opening
    assert "阿花执白" in opening
    assert "你不在棋盘上" in opening
    assert "Bot 执" not in opening


@pytest.mark.asyncio
async def test_tactical_prompt_names_the_player_who_moved() -> None:
    manager, room = await two_human_room()
    plugin = plugin_for(manager)
    # 黑方（小明）先落一子，轮到白方（阿花）
    black_seat = room.multiplayer.seats[0]
    await manager.player_move(room, black_seat.visitor_token, row=7, column=7)

    prompt = plugin._board_commentary_prompt(
        room, {"side": "black", "actor": "human"}, "刚发生了一次较大的翻子"
    )

    assert prompt is not None
    assert "小明执黑" in prompt
    assert "刚走完这一步" in prompt
    assert "现在轮到阿花走" in prompt
    assert "玩家" not in prompt


@pytest.mark.asyncio
async def test_solo_practice_room_keeps_first_person_narration() -> None:
    manager, room = await solo_room()
    plugin = plugin_for(manager)

    assert _commentary_duel_room(room) is False
    assert "花火执" in _commentary_sides_text(room)

    opening = plugin._opening_commentary_prompt(room)
    assert "Bot 执" in opening


@pytest.mark.asyncio
async def test_duel_system_prompt_forbids_first_person_play() -> None:
    manager, room = await two_human_room()
    configured = _Provider()
    plugin = plugin_for(manager)
    plugin.context = _Context(_Provider(), {"cheap": configured})
    stub_persona(plugin)
    plugin.commentary_provider_id = "cheap"

    await plugin._generate_persona_text(room, "说点什么")

    system = str(configured.calls[0]["system_prompt"])
    assert "旁观两位真人" in system
    assert "没有执任何一方" in system
    assert "不要说自己下了哪一步" in system
    assert "陪你玩" not in system


@pytest.mark.asyncio
async def test_commentary_uses_configured_provider() -> None:
    manager, room = await two_human_room()
    session_provider = _Provider()
    configured = _Provider()
    plugin = plugin_for(manager, provider_id="cheap")
    plugin.context = _Context(session_provider, {"cheap": configured})
    stub_persona(plugin)

    text = await plugin._generate_persona_text(room, "说点什么")

    assert text == "测试解说"
    assert configured.calls and not session_provider.calls


@pytest.mark.asyncio
async def test_missing_provider_falls_back_to_session_model() -> None:
    manager, room = await two_human_room()
    session_provider = _Provider()
    plugin = plugin_for(manager, provider_id="missing")
    plugin.context = _Context(session_provider, {})
    stub_persona(plugin)

    await plugin._generate_persona_text(room, "说点什么")

    assert session_provider.calls


@pytest.mark.asyncio
async def test_commentary_reply_drops_role_name_prefix() -> None:
    """模型偶尔会把 system 里的「角色名：花火」当成台词复述，生成后必须洗掉。"""
    manager, room = await two_human_room()
    provider = _Provider(reply="【角色名：火花】这手棋下得很稳。")
    plugin = plugin_for(manager, provider_id="cheap")
    plugin.context = _Context(_Provider(), {"cheap": provider})
    stub_persona(plugin)

    text = await plugin._generate_persona_text(room, "说点什么")
    assert text == "这手棋下得很稳。"
    # 只认 commentary.provider_id，不再有单独的模型名配置
    assert "model" not in provider.calls[0]


def test_role_prefix_helper_only_touches_leading_labels() -> None:
    assert _strip_role_prefix("角色名：火花\n开局了") == "开局了"
    assert _strip_role_prefix("**角色名：花火**：这步有点意思") == "这步有点意思"
    assert _strip_role_prefix("【姓名：花火】你好呀") == "你好呀"
    assert _strip_role_prefix("这步棋不错") == "这步棋不错"


def test_xiangqi_duel_result_names_the_seat() -> None:
    room = types.SimpleNamespace(
        game=types.SimpleNamespace(winner="red"),
        game_type="xiangqi",
        multiplayer=types.SimpleNamespace(
            enabled=True,
            seats=[
                PlayerSeat(number=1, display_name="小明", side="red"),
                PlayerSeat(number=2, display_name="阿花", side="black"),
            ],
        ),
    )

    text = GameCompanionPlugin._round_result_text(
        room, {"result": "human_win"}, reveal_answer=False
    )

    assert text == "小明执红获胜"
