from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.undercover_words import UndercoverWordStore


def make_event(
    qq: str = "10001",
    *,
    private: bool = True,
    text: str = "",
) -> SimpleNamespace:
    origin = (
        f"aiocqhttp:FriendMessage:{qq}"
        if private
        else "aiocqhttp:GroupMessage:20001"
    )
    state = {"stopped": False}
    event = SimpleNamespace(
        unified_msg_origin=origin,
        get_sender_id=lambda: qq,
        message_str=text,
        plain_result=lambda content: SimpleNamespace(text=content),
        stop_event=lambda: state.update(stopped=True),
    )
    event._state = state
    return event


def make_plugin(tmp_path: Path, admins: tuple[str, ...] = ("10001",)) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.game_admin_ids = set(admins)
    plugin.undercover_word_store = UndercoverWordStore(
        tmp_path / "undercover_words.json"
    )
    plugin._uc_word_pending = {}
    return plugin


# ---------------- 解析纯函数 ----------------


def test_parse_uc_word_lines_accepts_space_comma_colon_and_dunhao() -> None:
    pairs, malformed = GameCompanionPlugin._parse_uc_word_lines(
        "测试甲 测试乙\n测试丙,测试丁\n测试戊：测试己\n测试庚、测试辛\n\n"
    )

    assert malformed == []
    assert pairs == [
        ("测试甲", "测试乙"),
        ("测试丙", "测试丁"),
        ("测试戊", "测试己"),
        ("测试庚", "测试辛"),
    ]


def test_parse_uc_word_lines_marks_malformed_lines() -> None:
    pairs, malformed = GameCompanionPlugin._parse_uc_word_lines(
        "只有一列\n测试甲 测试乙 测试丙\n正常甲 正常乙"
    )

    assert pairs == [("正常甲", "正常乙")]
    assert malformed == ["只有一列", "测试甲 测试乙 测试丙"]


# ---------------- 权限与私聊限制 ----------------


@pytest.mark.asyncio
async def test_start_rejects_non_admin(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path, admins=("10001",))
    event = make_event("10002", text="新增词条")

    results = [r async for r in plugin.undercover_words_add_start(event)]

    assert any("只有游戏管理员" in r.text for r in results)
    assert "10002" not in plugin._uc_word_pending


@pytest.mark.asyncio
async def test_start_rejects_group_chat(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    event = make_event("10001", private=False, text="新增词条")

    results = [r async for r in plugin.undercover_words_add_start(event)]

    assert any("私聊" in r.text for r in results)
    assert "10001" not in plugin._uc_word_pending


@pytest.mark.asyncio
async def test_start_sets_pending_and_returns_format_hint(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    event = make_event("10001", text="新增词条")

    results = [r async for r in plugin.undercover_words_add_start(event)]

    assert "10001" in plugin._uc_word_pending
    joined = "".join(r.text for r in results)
    assert "词条1 词条2" in joined
    assert "取消" in joined


# ---------------- 批量录入流程 ----------------


@pytest.mark.asyncio
async def test_receive_imports_pairs_and_reports_counts(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    await _run(plugin.undercover_words_add_start(make_event(text="新增词条")))

    event = make_event(text="测试甲 测试乙\n测试丙,测试丁\n坏行只有一列")
    results = [r async for r in plugin.undercover_words_add_receive(event)]
    joined = "".join(r.text for r in results)

    assert "成功 2 对" in joined
    assert "跳过 1 对" in joined
    assert event._state["stopped"] is True
    assert "10001" not in plugin._uc_word_pending

    stored = {
        (item["word1"], item["word2"])
        for item in plugin.undercover_word_store.list_all()
    }
    assert ("测试甲", "测试乙") in stored
    assert ("测试丙", "测试丁") in stored


@pytest.mark.asyncio
async def test_receive_cancel_clears_pending(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    await _run(plugin.undercover_words_add_start(make_event(text="新增词条")))

    event = make_event(text="取消")
    results = [r async for r in plugin.undercover_words_add_receive(event)]

    assert any("已退出" in r.text for r in results)
    assert "10001" not in plugin._uc_word_pending


@pytest.mark.asyncio
async def test_receive_ignores_messages_without_pending(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    event = make_event(text="测试甲 测试乙")

    results = [r async for r in plugin.undercover_words_add_receive(event)]

    assert results == []
    assert event._state["stopped"] is False


@pytest.mark.asyncio
async def test_receive_ignores_other_command_text_while_pending(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    await _run(plugin.undercover_words_add_start(make_event(text="新增词条")))

    event = make_event(text="添加词条")
    results = [r async for r in plugin.undercover_words_add_receive(event)]

    assert results == []
    # 指令文本不被当成词条，待录入状态保留
    assert "10001" in plugin._uc_word_pending


async def _run(agen) -> list[object]:
    return [item async for item in agen]


# ---------------- LLM 词对解析（AI 批量生成） ----------------


def test_extract_uc_word_pairs_accepts_bare_array() -> None:
    """LLM 常返回裸 JSON 数组，必须能全部取出（旧实现只认对象，会漏成 0 条）。"""
    text = '[{"word1":"可乐","word2":"雪碧"},{"word1":"牛奶","word2":"豆浆"}]'

    pairs = GameCompanionPlugin._extract_uc_word_pairs(text)

    assert [(p["word1"], p["word2"]) for p in pairs] == [
        ("可乐", "雪碧"),
        ("牛奶", "豆浆"),
    ]


def test_extract_uc_word_pairs_accepts_wrapped_and_markdown() -> None:
    wrapped = '{"items":[{"word1":"猫","word2":"狗"}]}'
    marked = '```json\n[{"word1":"咖啡","word2":"奶茶"}]\n```'

    assert GameCompanionPlugin._extract_uc_word_pairs(wrapped) == [
        {"word1": "猫", "word2": "狗"}
    ]
    assert GameCompanionPlugin._extract_uc_word_pairs(marked) == [
        {"word1": "咖啡", "word2": "奶茶"}
    ]


def test_extract_uc_word_pairs_accepts_single_object() -> None:
    assert GameCompanionPlugin._extract_uc_word_pairs(
        '{"word1":"口红","word2":"唇釉"}'
    ) == [{"word1": "口红", "word2": "唇釉"}]


def test_extract_uc_word_pairs_returns_empty_for_garbage() -> None:
    assert GameCompanionPlugin._extract_uc_word_pairs("抱歉，我无法生成") == []
    assert GameCompanionPlugin._extract_uc_word_pairs("") == []
