"""管理台手动出题 + 单条重置出题记录。

盯住三件事：

* 点「立即出题」时选中的模型真的被调用，出的题带模型名入库；
* 「模型出题」列表能看出是哪家的哪个模型出的，旧存档没有这个字段也不炸；
* 单条重置只把这一道题从「出过」里放出来，不清空整本账、不动别的题。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion_huahuo import main as main_module
from astrbot_plugin_game_companion_huahuo.main import (
    GameCompanionPlugin,
    _provider_label,
    _text_provider_options,
)
from astrbot_plugin_game_companion_huahuo.soup_custom import CustomSoupStore
from astrbot_plugin_game_companion_huahuo.soup_generated import GeneratedSoupStore
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary, build_puzzle
from astrbot_plugin_game_companion_huahuo.turtle_soup import SoupPuzzle
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

BUILTIN = Path(__file__).resolve().parent.parent / "soup_library.json"

SURFACE_A = "她每天都要买两份早餐，一份自己吃，一份放在窗台上，为什么？"
SOLUTION_A = "窗台对着邻居家的猫，独居老人住院前拜托她帮忙喂猫，老人去世后她一直喂，害怕猫饿死。"
SURFACE_B = "停电的夜里，他点了一根蜡烛，然后从十楼跳了下去，为什么？"
SOLUTION_B = "他是个盲人，停电时误以为天亮了，习惯性打开窗去阳台浇花，脚下踏空摔了下去。"


def make_puzzle(suffix: str = "甲") -> SoupPuzzle:
    return SoupPuzzle(
        title=f"测试生成的题{suffix}",
        surface=f"某个人每天都会在同一时间做一件奇怪的事（{suffix}），为什么？",
        solution=(
            "他在农场里养了一群需要定时投喂的动物，时间一到就必须去喂，"
            "否则会互相踩踏，所以看起来很奇怪。"
        ),
        key_facts=("他养了动物", "必须定时投喂", "否则会出事故"),
        acceptable_variants=("他必须按时喂动物",),
        hints=("和动物有关", "和时间有关", "不喂会出事"),
        content_level="normal",
        theme=f"养殖{suffix}",
        trick=f"定时投喂{suffix}",
    )


def generated_payload(
    title: str, theme: str, *, surface: str = "", solution: str = ""
) -> str:
    """默认模板；需要「两道德才真是两道」的用例可以覆盖汤面/汤底。"""
    return json.dumps(
        {
            "title": title,
            "surface": surface
            or f"{title}：一个人每天都在重复做同一件看起来很蠢的事，为什么？",
            "solution": solution
            or (
                "他这么做是为了让楼下的家人听见动静，确认彼此都还平安，"
                "这件事本身并不蠢，只是外人不知道原因。"
            ),
            "key_facts": ["动作是为了发出声音", "楼下住着家人", "确认平安"],
            "hints": ["和声音有关", "和楼下的家人有关", "是为了确认"],
            "theme": theme,
            "trick": "把报平安当成怪癖",
        },
        ensure_ascii=False,
    )


class FakeProvider:
    def __init__(self, pid: str, model: str, replies: list[str] | None = None) -> None:
        self.pid = pid
        self.model = model
        self.calls: list[str] = []
        self.replies = list(replies or [])
        self.text_chat = AsyncMock(side_effect=self._reply)

    def meta(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.pid, model=self.model)

    def _reply(self, **kwargs) -> SimpleNamespace:
        self.calls.append(str(kwargs.get("prompt") or ""))
        text = self.replies.pop(0) if self.replies else generated_payload("备用题", "备用主题")
        return SimpleNamespace(completion_text=text)


class FakeContext:
    def __init__(self, providers: list[FakeProvider], *, default: FakeProvider | None = None) -> None:
        self.providers = providers
        self.default = default
        self.looked_up: list[str] = []

    def get_all_providers(self) -> list[FakeProvider]:
        return list(self.providers)

    def get_provider_by_id(self, provider_id: str) -> FakeProvider | None:
        self.looked_up.append(provider_id)
        return next((p for p in self.providers if p.pid == provider_id), None)

    def get_using_provider(self, _session_id: str | None = None) -> FakeProvider | None:
        return self.default


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self, default=None):
        return dict(self.payload)


def make_console_plugin(
    tmp_path: Path, *, context: FakeContext | None = None
) -> GameCompanionPlugin:
    plugin = GameCompanionPlugin.__new__(GameCompanionPlugin)
    plugin.data_dir = tmp_path
    plugin.context = context or FakeContext([])
    plugin.turtle_soup_generation_provider_id = ""
    plugin.manager = SimpleNamespace(
        record_operation=lambda *_args, **_kwargs: None,
        rooms={},
    )
    # 真实插件是懒初始化后一直复用同一份，测试桩也照做，避免各读各的内存
    deck = SoupDeck(tmp_path / "deck.json")
    generated = GeneratedSoupStore(tmp_path / "generated.json")
    library = SoupLibrary(BUILTIN, tmp_path / "cache.json")
    custom = CustomSoupStore(tmp_path / "custom.json")
    plugin._soup_deck = lambda: deck  # noqa: SLF001
    plugin._soup_generated = lambda: generated  # noqa: SLF001
    plugin._soup_library = lambda: library  # noqa: SLF001
    plugin._soup_custom = lambda: custom  # noqa: SLF001
    return plugin


def patch_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "generation_prompt", lambda **_kw: ("sys", "出一道题"))
    monkeypatch.setattr(main_module, "validation_prompt", lambda _p: ("sys", "自检"))
    monkeypatch.setattr(main_module, "repair_prompt", lambda _p, **_kw: ("sys", "修一版"))
    monkeypatch.setattr(
        main_module, "validation_detail", lambda _text: (True, "", "")
    )


# ---------------- 存档：模型名与单条重置 ----------------


def test_model_is_stored_listed_and_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "generated.json"
    store = GeneratedSoupStore(path)
    store.add(make_puzzle(), model="deepseek · deepseek-chat")

    row = store.listing()[0]
    assert row["model"] == "deepseek · deepseek-chat"
    assert store.stats()["by_model"] == {"deepseek · deepseek-chat": 1}
    assert GeneratedSoupStore(path).listing()[0]["model"] == "deepseek · deepseek-chat"


def test_legacy_rows_without_model_are_readable(tmp_path: Path) -> None:
    path = tmp_path / "generated.json"
    store = GeneratedSoupStore(path)
    store.add(make_puzzle())
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["items"][0].pop("model", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    again = GeneratedSoupStore(path)
    assert again.listing()[0]["model"] == ""
    assert again.stats()["by_model"] == {"未记录": 1}


def test_reset_used_only_clears_one_row(tmp_path: Path) -> None:
    store = GeneratedSoupStore(tmp_path / "generated.json")
    first, second = make_puzzle("甲"), make_puzzle("乙")
    store.add(first, model="模型甲")
    store.add(second, model="模型乙")
    store.mark_used(first.signature)
    store.mark_used(second.signature)

    store.reset_used(1)

    rows = store.listing()
    assert rows[0]["last_used_at"] == 0
    assert rows[1]["last_used_at"] > 0
    assert len(store.unused()) == 1
    with pytest.raises(ValueError, match="没有这个序号"):
        store.reset_used(99)


def test_deck_forget_removes_only_the_target(tmp_path: Path) -> None:
    deck = SoupDeck(tmp_path / "deck.json")
    puzzle_a = build_puzzle(SURFACE_A, SOLUTION_A)
    puzzle_b = build_puzzle(SURFACE_B, SOLUTION_B)
    deck.remember(puzzle_a, source="custom")
    deck.remember(puzzle_b, source="library")

    assert deck.forget(SURFACE_A, puzzle=puzzle_a) == 1
    assert deck.stats()["total"] == 1
    assert deck.has(puzzle_b) is True
    assert deck.has(puzzle_a) is False
    # 再点一次不会误伤别的题
    assert deck.forget(SURFACE_A, puzzle=puzzle_a) == 0
    assert deck.stats()["total"] == 1


# ---------------- 模型枚举与展示 ----------------


def test_provider_options_and_label() -> None:
    context = FakeContext(
        [
            FakeProvider("deepseek", "deepseek-chat"),
            FakeProvider("qwen", "qwen-plus"),
        ]
    )
    assert _text_provider_options(context) == [
        {"id": "deepseek", "label": "deepseek · deepseek-chat"},
        {"id": "qwen", "label": "qwen · qwen-plus"},
    ]
    assert _provider_label(context.providers[0]) == "deepseek · deepseek-chat"


def test_provider_label_survives_odd_providers() -> None:
    class Bare:
        id = "bare"

        def text_chat(self, **_kwargs):  # noqa: ANN003
            return None

    class DictMeta:
        def meta(self):
            return {"id": "dict", "model": "dict-model"}

        def text_chat(self, **_kwargs):  # noqa: ANN003
            return None

    assert _provider_label(Bare()) == "bare"
    assert _provider_label(DictMeta()) == "dict · dict-model"
    assert _provider_label(None) == ""
    assert _text_provider_options(SimpleNamespace()) == []


def test_snapshot_carries_models_used_flags_and_model_column(tmp_path: Path) -> None:
    context = FakeContext([FakeProvider("picked", "model-x")], default=None)
    plugin = make_console_plugin(tmp_path, context=context)
    plugin.turtle_soup_generation_provider_id = "picked"
    plugin._soup_custom().add(SURFACE_A, SOLUTION_A)  # noqa: SLF001
    plugin._soup_generated().add(make_puzzle(), model="picked · model-x")  # noqa: SLF001
    plugin._soup_deck().remember(  # noqa: SLF001
        build_puzzle(SURFACE_A, SOLUTION_A), source="custom"
    )

    data = plugin._soup_library_snapshot()

    assert data["models"] == [{"id": "picked", "label": "picked · model-x"}]
    assert data["default_model"] == "picked"
    assert data["custom"]["items"][0]["used"] is True
    generated = data["generated"]["items"][0]
    assert generated["model"] == "picked · model-x"
    assert "solution" not in generated and "solution_preview" in generated


# ---------------- 管理台手动出题（后台任务 + 轮询） ----------------


def capture_spawn(plugin: GameCompanionPlugin) -> list:
    """把后台任务收进列表，测试里手动 await，不依赖真实事件循环调度。"""
    spawned: list = []
    plugin._spawn = lambda coro: spawned.append(coro) or SimpleNamespace(  # noqa: SLF001
        done=lambda: False
    )
    return spawned


class LogCapture:
    """替掉插件 logger，验证后台出题真的写进了 AstrBot 日志。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, message: object, args: tuple) -> None:
        self.lines.append(str(message) % args if args else str(message))

    def info(self, message: object, *args: object) -> None:
        self._record(message, args)

    def warning(self, message: object, *args: object) -> None:
        self._record(message, args)

    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.mark.asyncio
async def test_generate_status_starts_idle(tmp_path: Path) -> None:
    plugin = make_console_plugin(tmp_path)

    result = await plugin.page_soup_generate_status()

    assert result["data"]["job"]["state"] == "idle"
    assert result["data"]["job"]["running"] is False


@pytest.mark.asyncio
async def test_admin_generate_returns_before_the_model_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点一下就立刻返回：请求里不跑模型，网关不会再 504。"""
    patch_prompts(monkeypatch)
    chosen = FakeProvider(
        "chosen",
        "strong-model",
        [generated_payload("手动出的题", "手动主题"), json.dumps({"ok": True})],
    )
    other = FakeProvider("default", "weak-model")
    context = FakeContext([chosen, other], default=other)
    monkeypatch.setattr(
        main_module, "request", FakeRequest({"provider_id": "chosen", "count": 1})
    )
    plugin = make_console_plugin(tmp_path, context=context)
    spawned = capture_spawn(plugin)

    result = await plugin.page_soup_generate()

    assert result["status"] == "ok", result["message"]
    assert "可以先去忙别的" in result["message"]
    assert context.looked_up == ["chosen"]
    job = result["data"]["job"]
    assert job["running"] is True and job["total"] == 1 and job["done"] == 0
    # 请求返回时还没开始入库，模型也没被调用
    assert plugin._soup_generated().listing() == []  # noqa: SLF001
    assert other.text_chat.await_count == 0
    assert len(spawned) == 1

    await spawned[0]

    job = (await plugin.page_soup_generate_status())["data"]["job"]
    assert job["state"] == "completed" and job["done"] == 1
    rows = plugin._soup_generated().listing()  # noqa: SLF001
    assert rows[0]["title"] == "手动出的题"
    assert rows[0]["model"] == "chosen · strong-model"
    assert rows[0]["last_used_at"] == 0


@pytest.mark.asyncio
async def test_admin_generate_falls_back_to_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prompts(monkeypatch)
    configured = FakeProvider(
        "configured",
        "cfg-model",
        [generated_payload("默认模型的题", "默认主题"), json.dumps({"ok": True})],
    )
    context = FakeContext([configured], default=configured)
    monkeypatch.setattr(main_module, "request", FakeRequest({}))
    plugin = make_console_plugin(tmp_path, context=context)
    plugin.turtle_soup_generation_provider_id = "configured"
    spawned = capture_spawn(plugin)

    result = await plugin.page_soup_generate()
    await spawned[0]

    assert result["status"] == "ok", result["message"]
    assert context.looked_up == ["configured"]
    assert plugin._soup_generated().listing()[0]["model"] == (  # noqa: SLF001
        "configured · cfg-model"
    )


@pytest.mark.asyncio
async def test_admin_generate_reports_unknown_and_missing_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeContext([], default=None)
    plugin = make_console_plugin(tmp_path, context=context)
    spawned = capture_spawn(plugin)

    monkeypatch.setattr(
        main_module, "request", FakeRequest({"provider_id": "不存在的模型"})
    )
    unknown = await plugin.page_soup_generate()
    assert unknown["status"] == "error" and "找不到模型" in unknown["message"]

    monkeypatch.setattr(main_module, "request", FakeRequest({}))
    missing = await plugin.page_soup_generate()
    assert missing["status"] == "error" and "出题模型" in missing["message"]
    assert len(plugin._soup_generated()) == 0  # noqa: SLF001
    assert spawned == []


@pytest.mark.asyncio
async def test_admin_generate_serializes_clicks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = make_console_plugin(tmp_path)
    plugin._soup_generate_job = {  # noqa: SLF001
        "state": "running",
        "model": "busy-model",
        "total": 1,
        "done": 0,
        "titles": [],
        "error": "",
        "started_at": 1.0,
        "finished_at": 0.0,
    }
    spawned = capture_spawn(plugin)
    monkeypatch.setattr(main_module, "request", FakeRequest({}))

    result = await plugin.page_soup_generate()

    assert result["status"] == "ok"
    assert "已经有一批题在生成" in result["message"]
    assert result["data"]["job"]["running"] is True
    assert spawned == []


@pytest.mark.asyncio
async def test_admin_generate_survives_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prompts(monkeypatch)
    broken = FakeProvider("broken", "b-model")
    broken.text_chat = AsyncMock(side_effect=RuntimeError("上游返回了错误"))
    context = FakeContext([broken], default=broken)
    monkeypatch.setattr(main_module, "request", FakeRequest({"provider_id": "broken"}))
    plugin = make_console_plugin(tmp_path, context=context)
    spawned = capture_spawn(plugin)

    result = await plugin.page_soup_generate()
    assert result["status"] == "ok", "启动任务本身不该失败，失败要写进任务状态"

    await spawned[0]

    job = (await plugin.page_soup_generate_status())["data"]["job"]
    assert job["state"] == "failed" and job["error"]
    assert len(plugin._soup_generated()) == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_admin_generate_can_make_several_puzzles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prompts(monkeypatch)
    provider = FakeProvider(
        "multi",
        "m-model",
        [
            generated_payload("第一道", "主题一"),
            json.dumps({"ok": True}),
            # 两道必须是两个不同的故事，否则会被新增的近似判重挡下来
            generated_payload(
                "第二道",
                "主题二",
                surface="暴雨那天，整条街的人都听见了钟声，可教堂的钟十年前就被拆走了，为什么？",
                solution="钟声来自新装的消防警报，老居民一直按旧称叫它「钟」，所以听成了教堂的钟。",
            ),
            json.dumps({"ok": True}),
        ],
    )
    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"provider_id": "multi", "count": 2}),
    )
    plugin = make_console_plugin(tmp_path, context=FakeContext([provider]))
    spawned = capture_spawn(plugin)

    result = await plugin.page_soup_generate()
    assert result["data"]["job"]["total"] == 2
    await spawned[0]

    job = (await plugin.page_soup_generate_status())["data"]["job"]
    assert job["state"] == "completed" and job["done"] == 2
    titles = {row["title"] for row in plugin._soup_generated().listing()}  # noqa: SLF001
    assert titles == {"第一道", "第二道"}


@pytest.mark.asyncio
async def test_library_snapshot_carries_generate_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重进管理台时靠题库快照恢复「还在生成」：按钮不会先闪回可点。"""
    patch_prompts(monkeypatch)
    provider = FakeProvider(
        "chosen",
        "strong-model",
        [generated_payload("快照里的题", "快照主题"), json.dumps({"ok": True})],
    )
    monkeypatch.setattr(
        main_module, "request", FakeRequest({"provider_id": "chosen", "count": 1})
    )
    plugin = make_console_plugin(tmp_path, context=FakeContext([provider]))
    spawned = capture_spawn(plugin)

    await plugin.page_soup_generate()
    running = (await plugin.page_soup_library())["data"]["generate_job"]
    assert running["running"] is True
    assert running["model"] == "chosen · strong-model"
    assert running["total"] == 1

    await spawned[0]
    done = (await plugin.page_soup_library())["data"]["generate_job"]
    assert done["running"] is False
    assert done["state"] == "completed" and done["done"] == 1
    assert done["titles"] == ["快照里的题"]


@pytest.mark.asyncio
async def test_admin_generate_writes_progress_to_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台任务不能只在内存里跑：开始 / 入库 / 结束都要进 AstrBot 日志。"""
    patch_prompts(monkeypatch)
    logs = LogCapture()
    monkeypatch.setattr(main_module, "logger", logs)
    provider = FakeProvider(
        "logger",
        "log-model",
        [generated_payload("有日志的题", "日志主题"), json.dumps({"ok": True})],
    )
    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"provider_id": "logger", "count": 1}),
    )
    plugin = make_console_plugin(tmp_path, context=FakeContext([provider]))
    spawned = capture_spawn(plugin)

    await plugin.page_soup_generate()
    await spawned[0]

    emitted = logs.text()
    assert "管理台开始出题" in emitted
    assert "logger · log-model" in emitted
    assert "管理台出题已入库《有日志的题》" in emitted
    assert "管理台出题结束" in emitted


@pytest.mark.asyncio
async def test_admin_generate_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prompts(monkeypatch)
    logs = LogCapture()
    monkeypatch.setattr(main_module, "logger", logs)
    broken = FakeProvider("broken", "b-model")
    broken.text_chat = AsyncMock(side_effect=RuntimeError("上游返回了错误"))
    monkeypatch.setattr(main_module, "request", FakeRequest({"provider_id": "broken"}))
    plugin = make_console_plugin(tmp_path, context=FakeContext([broken]))
    spawned = capture_spawn(plugin)

    await plugin.page_soup_generate()
    await spawned[0]

    emitted = logs.text()
    assert "管理台出题失败" in emitted
    assert "没有结果" in emitted


# ---------------- 单条重置出题记录 ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["builtin", "custom", "generated"])
async def test_reset_used_action_only_clears_one_puzzle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    plugin = make_console_plugin(tmp_path)
    deck = plugin._soup_deck()  # noqa: SLF001
    store = plugin._soup_generated()  # noqa: SLF001
    custom = plugin._soup_custom()  # noqa: SLF001

    if kind == "generated":
        puzzle = make_puzzle()
        store.add(puzzle, model="some-model")
        store.mark_used(puzzle.signature)
        deck.remember(puzzle, source="generated")
        index = 1
    elif kind == "custom":
        custom.add(SURFACE_A, SOLUTION_A)
        puzzle = build_puzzle(SURFACE_A, SOLUTION_A)
        assert puzzle is not None
        deck.remember(puzzle, source="custom")
        index = 1
    else:
        entry = plugin._soup_library().find(1)  # noqa: SLF001
        assert entry is not None
        puzzle = build_puzzle(entry.surface, entry.solution)
        assert puzzle is not None
        deck.remember(puzzle, source="library")
        index = 1
    # 另一道题也在账本里：单条重置不该把它一起清掉
    other = build_puzzle(SURFACE_B, SOLUTION_B)
    assert other is not None
    deck.remember(other, source="library")
    assert deck.stats()["total"] == 2

    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"kind": kind, "action": "reset_used", "index": index}),
    )
    result = await GameCompanionPlugin.page_soup_library_action(plugin)

    assert result["status"] == "ok", result["message"]
    assert "单独重置" in result["message"]
    assert deck.stats()["total"] == 1
    assert deck.has(other) is True
    assert deck.has(puzzle) is False
    if kind == "generated":
        assert store.listing()[0]["last_used_at"] == 0
        assert len(store.unused()) == 1
    assert result["data"]["used"]["total"] == 1


@pytest.mark.asyncio
async def test_reset_used_action_rejects_bad_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = make_console_plugin(tmp_path)
    monkeypatch.setattr(
        main_module,
        "request",
        FakeRequest({"kind": "generated", "action": "reset_used", "index": 9}),
    )

    result = await GameCompanionPlugin.page_soup_library_action(plugin)

    assert result["status"] == "error"
    assert "没有这个序号" in result["message"]
