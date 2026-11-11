"""出题失败率的两个来源：上游限流与自检打回。

以前的做法是「一撞限流就整道重来」「一审不过就整道丢掉重出」，结果是额度被
反复烧掉、失败率还特别高。这里锁住新策略：

* 限流只等不消耗「重出」次数，并且退避窗口按出题/后台场景放宽；
* 自检打回先带着审查意见修一版，改完只再审一次；
* 审查员自己输出格式跑偏算「没跑成」，重问一次，而不是判题目不合格。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping
from astrbot_plugin_game_companion_huahuo.turtle_soup_ai import (
    repair_prompt,
    validation_detail,
    validation_passed,
    validation_prompt,
)
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck


def puzzle_json(title: str) -> str:
    return json.dumps(
        {
            "title": title,
            "surface": f"{title}：一个人每天固定时间做一件怪事，为什么？",
            "solution": "他要在固定时间给楼下的家人发信号，确认彼此都平安，所以看起来很奇怪。",
            "key_facts": ["动作是为了报平安", "楼下住着家人", "必须固定时间"],
            "acceptable_variants": ["他在给家人报平安"],
            "hints": ["和声音有关", "和楼下的家人有关", "是为了确认"],
            "theme": f"主题{title}",
            "trick": "把报平安当成怪癖",
        },
        ensure_ascii=False,
    )


PASS_JSON = '{"valid":true,"reason":"","fix":""}'
FAIL_JSON = '{"valid":false,"reason":"汤面已经透露了关键事实","fix":"把汤面里的原因去掉"}'
NOISE = "让我想想……这道题看起来还行，但我先说下思路。"


class ScriptedProvider:
    """按脚本依次返回 / 抛错，并记录被调了几次。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str = ""):
        self.calls += 1
        item = self.script.pop(0) if self.script else SimpleNamespace(completion_text="")
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(completion_text=item)


def make_plugin(tmp_path: Path) -> GameCompanionPlugin:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.config = {}
    # 测试里不真的等退避，但也别把重试次数改掉
    plugin.LLM_RETRY_DELAYS = (0, 0)
    plugin.SOUP_RETRY_DELAYS = (0, 0, 0)
    plugin.SOUP_QUICK_RETRY_DELAYS = (0, 0)
    plugin._test_deck = SoupDeck(tmp_path / "deck.json")  # noqa: SLF001
    return plugin


ROOM = SimpleNamespace(session_id="admin:console", difficulty="normal")


async def generate(
    plugin: GameCompanionPlugin, provider: ScriptedProvider, *, attempts: int = 1
):
    return await plugin._generate_soup_puzzle(  # noqa: SLF001
        ROOM,
        content_level="normal",
        difficulty="normal",
        excluded=set(),
        deck=plugin._test_deck,  # noqa: SLF001
        attempts=attempts,
        provider=provider,
        use_persona=False,
    )


@pytest.mark.asyncio
async def test_rate_limit_does_not_burn_a_generation_attempt(tmp_path: Path) -> None:
    """撞限流先等再要，不该把这次「出一道」的额度算掉。"""
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider(
        [
            RuntimeError("Error code: 429 - rate limit exceeded"),
            RuntimeError("429 rate limit exceeded"),
            puzzle_json("限流后的题"),
            PASS_JSON,
        ]
    )

    puzzle = await generate(plugin, provider, attempts=1)

    assert puzzle.title == "限流后的题"
    assert provider.calls == 4, "两次限流重试 + 出题 + 自检"


@pytest.mark.asyncio
async def test_rate_limit_leaves_a_global_cooldown(tmp_path: Path) -> None:
    """撞过限流后留冷却：同批其它调用先等一下，别立刻再撞一次。"""
    plugin = make_plugin(tmp_path)
    plugin._set_llm_cooldown(5.0)  # noqa: SLF001

    assert plugin._llm_cooldown_until() > time.monotonic()  # noqa: SLF001
    # 冷却时间被上限截断，不会因为上游 Retry-After 给得离谱而卡死
    plugin._set_llm_cooldown(3600)  # noqa: SLF001
    assert (
        plugin._llm_cooldown_until() - time.monotonic()
        <= plugin.LLM_COOLDOWN_CAP + 0.5
    )


@pytest.mark.asyncio
async def test_failed_validation_is_repaired_instead_of_regenerated(
    tmp_path: Path,
) -> None:
    """自检打回：先按审查意见修一版，修好比重新编便宜得多。"""
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider(
        [
            puzzle_json("初版"),
            FAIL_JSON,
            puzzle_json("修复版"),
            PASS_JSON,
        ]
    )

    puzzle = await generate(plugin, provider, attempts=1)

    assert puzzle.title == "修复版"
    assert provider.calls == 4, "出题 + 自检 + 修复 + 复审"


@pytest.mark.asyncio
async def test_repair_reports_reason_and_fix_from_the_checker() -> None:
    detail = validation_detail(FAIL_JSON)
    assert detail == (False, "汤面已经透露了关键事实", "把汤面里的原因去掉")

    puzzle = puzzle_from_mapping(json.loads(puzzle_json("初版")), content_level="normal")
    system, prompt = repair_prompt(
        puzzle,
        reason="汤面已经透露了关键事实",
        fix="把汤面里的原因去掉",
    )
    assert "修" in system
    assert "汤面已经透露了关键事实" in prompt and "把汤面里的原因去掉" in prompt


@pytest.mark.asyncio
async def test_checker_noise_is_retried_not_treated_as_failure(
    tmp_path: Path,
) -> None:
    """审查员吐了一段解释而不是 JSON：重问一次，不能因此丢掉一道好题。"""
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider([puzzle_json("审查员跑偏的题"), NOISE, PASS_JSON])

    puzzle = await generate(plugin, provider, attempts=1)

    assert puzzle.title == "审查员跑偏的题"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_checker_noise_twice_keeps_the_puzzle(tmp_path: Path) -> None:
    """两次都没按格式回答就按通过处理：宁可少一道审查，也不要白丢题。"""
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider([puzzle_json("审查员完全不配合的题"), NOISE, NOISE])

    puzzle = await generate(plugin, provider, attempts=1)

    assert puzzle.title == "审查员完全不配合的题"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_validation_prompt_only_rejects_concrete_problems() -> None:
    """审查口径收窄：文笔、难度、题材普通都不算不合格。"""
    puzzle = puzzle_from_mapping(json.loads(puzzle_json("审查口径")), content_level="normal")
    system, prompt = validation_prompt(puzzle)
    assert "自相矛盾" in prompt and "唯一解释" in prompt
    assert "不够精巧" in prompt and "valid=true" in prompt
    assert '"fix"' in prompt
    assert "只输出 JSON" in system
    assert validation_passed('{"valid":true}') is True


@pytest.mark.asyncio
async def test_transient_connection_error_is_retried_once(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider(
        [RuntimeError("Connection reset by peer"), puzzle_json("抖动后的题"), PASS_JSON]
    )

    puzzle = await generate(plugin, provider, attempts=1)

    assert puzzle.title == "抖动后的题"
    assert provider.calls == 3
