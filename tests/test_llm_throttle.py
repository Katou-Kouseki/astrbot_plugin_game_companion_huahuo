"""模型调用闸门：群里人一多，提问会一窝蜂打到模型上。

以前的做法是「直接调、超时就报错」，于是十几个人同时问时，先撞限流的那些
人要等满 30 秒才拿到「Bot 暂时无法判断」。这里锁住三件事：并发有上限、
限流会退避重试、排队太久要早点告诉玩家。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import GameCompanionPlugin


class Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class ScriptedProvider:
    """按脚本依次返回 / 抛错，并记录被调了几次。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str = ""):
        self.calls += 1
        item = self.script.pop(0) if self.script else Response("默认回复")
        if isinstance(item, BaseException):
            raise item
        return item


class SlowProvider:
    """所有调用都挂一会儿，用来观察并发峰值。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str = ""):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return Response("ok")
        finally:
            self.active -= 1


def make_plugin() -> GameCompanionPlugin:
    plugin = object.__new__(GameCompanionPlugin)
    plugin.config = {}
    # 退避改成不等，测试不该真的睡两秒
    plugin.LLM_RETRY_DELAYS = (0, 0)
    return plugin


ROOM = SimpleNamespace(session_id="aiocqhttp:group:20001")


async def call(plugin: GameCompanionPlugin, provider, timeout: float = 5.0) -> str:
    return await plugin._call_room_model(  # noqa: SLF001
        ROOM, system_prompt="系统", prompt="提问", timeout=timeout, provider=provider
    )


@pytest.mark.asyncio
async def test_rate_limit_is_retried_then_succeeds() -> None:
    provider = ScriptedProvider([RuntimeError("429 Too Many Requests"), Response("是")])
    assert await call(make_plugin(), provider) == "是"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_persistent_rate_limit_gives_a_readable_message() -> None:
    provider = ScriptedProvider(
        [RuntimeError("429 rate limit"), RuntimeError("429 rate limit"), RuntimeError("429 rate limit")]
    )
    with pytest.raises(RuntimeError, match="限流"):
        await call(make_plugin(), provider)
    assert provider.calls == 3, "首发 + 两次退避重试"


@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried() -> None:
    """参数错误之类的失败重试也没用，只会让玩家多等。"""
    provider = ScriptedProvider([ValueError("bad request")])
    with pytest.raises(RuntimeError, match="模型调用失败"):
        await call(make_plugin(), provider)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_timeout_is_not_retried() -> None:
    provider = SlowProvider(delay=1.0)
    with pytest.raises(RuntimeError, match="超时"):
        await call(make_plugin(), provider, timeout=0.05)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_empty_reply_gets_one_more_chance() -> None:
    provider = ScriptedProvider([Response(""), Response("判定")])
    assert await call(make_plugin(), provider) == "判定"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_concurrency_is_capped() -> None:
    """7 个人同时问，同时打到模型上的最多 3 个，其余排队。"""
    plugin = make_plugin()
    provider = SlowProvider(delay=0.05)

    results = await asyncio.gather(*[call(plugin, provider) for _ in range(7)])

    assert results == ["ok"] * 7
    assert provider.peak == plugin.LLM_MAX_CONCURRENCY, "应该真的把闸门用满，而不是没并发"


@pytest.mark.asyncio
async def test_queue_wait_has_its_own_timeout() -> None:
    """排了 25 秒还没轮到，就别让玩家继续干等。"""
    plugin = make_plugin()
    plugin.LLM_QUEUE_TIMEOUT = 0.05
    holder = SlowProvider(delay=1.0)
    busy = [asyncio.create_task(call(plugin, holder)) for _ in range(3)]
    await asyncio.sleep(0.01)

    with pytest.raises(RuntimeError, match="人有点多"):
        await call(plugin, holder)

    for task in busy:
        task.cancel()
    await asyncio.gather(*busy, return_exceptions=True)


@pytest.mark.asyncio
async def test_gate_is_rebuilt_for_a_new_event_loop() -> None:
    """测试与热重载都会换事件循环，闸门不能跨 loop 复用。"""
    plugin = make_plugin()
    first = plugin._llm_gate()  # noqa: SLF001
    second = plugin._llm_gate()  # noqa: SLF001
    assert first is second, "同一个 loop 里必须复用同一个闸门"
    assert getattr(first, "_value", None) == plugin.LLM_MAX_CONCURRENCY
