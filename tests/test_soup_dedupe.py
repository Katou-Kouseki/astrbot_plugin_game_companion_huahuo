"""模型出题的整体去重：账本、模型存档、内置题库、玩家投稿四处一起比。

旧逻辑只跟「出题账本」比，而账本只在题目上过桌时才记一笔：管理台刚出的题
还没人玩过，就不在账本里，于是同一道故事换个标题/主题还能再存一份；内置
题库和玩家投稿更是完全没参与比对。这里把这些漏洞逐一钉住。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_game_companion_huahuo.main import (
    GameCompanionPlugin,
    _soup_looks_alike,
)
from astrbot_plugin_game_companion_huahuo.soup_custom import CustomSoupStore
from astrbot_plugin_game_companion_huahuo.soup_generated import GeneratedSoupStore
from astrbot_plugin_game_companion_huahuo.soup_library import SoupLibrary
from astrbot_plugin_game_companion_huahuo.turtle_soup import puzzle_from_mapping
from astrbot_plugin_game_companion_huahuo.turtle_soup_deck import SoupDeck

BUILTIN = Path(__file__).resolve().parent.parent / "soup_library.json"
STORY_B_SURFACE = "她每天把同一把蓝色雨伞放在公司前台，从不撑开，同事问起也只是笑笑，为什么？"
STORY_B_SOLUTION = "那把伞是父亲生前落下的，父亲走后她怕家里人看见难过，便寄放在前台；每天看一眼当成来看过他，伞骨早就坏了撑不开。"
PASS_JSON = '{"valid":true,"reason":"","fix":""}'

STORY_A_SURFACE = "他每天凌晨都要把楼道的灯全部关掉，邻居劝了很多次他也不听，为什么？"
STORY_A_SOLUTION = "楼道灯的光会照进楼下独居老人家里，让他彻夜睡不着；老人怕麻烦别人一直没说，他偶然发现后才每天去关。"


def payload(
    title: str,
    *,
    surface: str,
    solution: str,
    theme: str = "楼道灯",
    trick: str = "关灯是体贴",
) -> str:
    return json.dumps(
        {
            "title": title,
            "surface": surface,
            "solution": solution,
            "key_facts": ["灯光照进楼下", "老人失眠", "关灯是为了体贴"],
            "acceptable_variants": ["他怕影响楼下"],
            "hints": ["和灯光有关", "和楼下有关", "是为了别人"],
            "theme": theme,
            "trick": trick,
        },
        ensure_ascii=False,
    )


class ScriptedProvider:
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
    plugin.LLM_RETRY_DELAYS = (0, 0)
    plugin.SOUP_RETRY_DELAYS = (0, 0, 0)
    plugin.SOUP_QUICK_RETRY_DELAYS = (0, 0)
    deck = SoupDeck(tmp_path / "deck.json")
    store = GeneratedSoupStore(tmp_path / "generated.json")
    library = SoupLibrary(BUILTIN, tmp_path / "cache.json")
    custom = CustomSoupStore(tmp_path / "custom.json")
    plugin._soup_deck = lambda: deck  # noqa: SLF001
    plugin._soup_generated = lambda: store  # noqa: SLF001
    plugin._soup_library = lambda: library  # noqa: SLF001
    plugin._soup_custom = lambda: custom  # noqa: SLF001
    plugin._test_deck = deck  # noqa: SLF001
    return plugin


ROOM = SimpleNamespace(session_id="admin:console", difficulty="normal")


async def generate(plugin: GameCompanionPlugin, provider: ScriptedProvider, *, attempts: int = 1):
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
async def test_generated_store_is_checked_even_before_it_is_played(
    tmp_path: Path,
) -> None:
    """管理台出的题没上过桌、不在账本里，也不能换个标题再存一份。"""
    plugin = make_plugin(tmp_path)
    store = plugin._soup_generated()  # noqa: SLF001
    first = puzzle_from_mapping(
        json.loads(payload("关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION)),
        content_level="normal",
    )
    assert store.add(first, model="m") is True

    provider = ScriptedProvider(
        [
            payload(
                "熄灯的人",
                surface=STORY_A_SURFACE.replace("每天凌晨", "每晚深夜"),
                solution=STORY_A_SOLUTION,
                theme="走廊照明",
                trick="为了楼下安睡",
            )
        ]
    )

    with pytest.raises(ValueError, match="高度近似"):
        await generate(plugin, provider)

    assert len(store) == 1, "近似题不该被写进存档"


@pytest.mark.asyncio
async def test_builtin_library_participates_in_dedupe(tmp_path: Path) -> None:
    """内置题库有两百多条，模型不能贴着其中一条再编一道。"""
    plugin = make_plugin(tmp_path)
    entry = plugin._soup_library().entries[0]  # noqa: SLF001
    provider = ScriptedProvider(
        [
            payload(
                "换汤不换药",
                surface=entry.surface,
                solution=entry.solution,
                theme="内置改写",
                trick="照搬内置题",
            )
        ]
    )

    with pytest.raises(ValueError, match="内置题库"):
        await generate(plugin, provider)


@pytest.mark.asyncio
async def test_player_submissions_participate_in_dedupe(tmp_path: Path) -> None:
    """玩家投稿过的故事，模型再编一遍同样算重复。"""
    plugin = make_plugin(tmp_path)
    plugin._soup_custom().add(STORY_A_SURFACE, STORY_A_SOLUTION)  # noqa: SLF001
    provider = ScriptedProvider(
        [payload("关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION)]
    )

    with pytest.raises(ValueError, match="玩家投稿"):
        await generate(plugin, provider)


@pytest.mark.asyncio
async def test_a_genuinely_different_puzzle_still_passes(tmp_path: Path) -> None:
    """判重不能误伤：换一个完全不同的故事必须放行。"""
    plugin = make_plugin(tmp_path)
    provider = ScriptedProvider(
        [
            payload(
                "雪地的脚印",
                surface="雪地上只有一串走进屋子的脚印，却没有出去的，屋里的人也不见了。",
                solution="他从屋顶的烟囱爬进阁楼，再沿屋后的梯子离开，雪地上只留下进来的脚印。",
                theme="雪地",
                trick="出口在屋顶",
            ),
            PASS_JSON,
        ]
    )

    puzzle = await generate(plugin, provider)

    assert puzzle.title == "雪地的脚印"


@pytest.mark.parametrize("kind", ["builtin", "custom", "generated"])
@pytest.mark.asyncio
async def test_duplicate_note_names_the_pool(tmp_path: Path, kind: str) -> None:
    """日志/报错要说清撞了谁，方便判断是题库问题还是模型偷懒。"""
    plugin = make_plugin(tmp_path)
    library = plugin._soup_library()  # noqa: SLF001
    if kind == "generated":
        plugin._soup_generated().add(  # noqa: SLF001
            puzzle_from_mapping(
                json.loads(
                    payload("关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION)
                ),
                content_level="normal",
            ),
            model="m",
        )
        target = SimpleNamespace(
            title="关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION
        )
    elif kind == "custom":
        plugin._soup_custom().add(STORY_A_SURFACE, STORY_A_SOLUTION)  # noqa: SLF001
        target = SimpleNamespace(
            title="关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION
        )
    else:
        entry = library.entries[0]
        target = SimpleNamespace(
            title=str(getattr(entry, "title", "") or "内置题"),
            surface=entry.surface,
            solution=entry.solution,
        )

    note = plugin._soup_duplicate_note(target, plugin._test_deck)  # noqa: SLF001

    assert note, "应该判定为重复"
    assert {"generated": "模型出题存档", "custom": "玩家投稿", "builtin": "内置题库"}[
        kind
    ] in note


# ---------------- 回归：判重误杀 / 漏判 ----------------


def _short_title_puzzles() -> tuple:
    first = puzzle_from_mapping(
        json.loads(payload("无题", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION)),
        content_level="normal",
    )
    second = puzzle_from_mapping(
        json.loads(
            payload(
                "无题",
                surface=STORY_B_SURFACE,
                solution=STORY_B_SOLUTION,
                theme="雨伞",
                trick="寄放遗物",
            )
        ),
        content_level="normal",
    )
    return first, second


def test_generic_short_title_does_not_kill_a_different_story(tmp_path: Path) -> None:
    """「无题 / 海龟汤」这类通用短标题相似度恒为 1，不能只凭它判重复。"""
    plugin = make_plugin(tmp_path)
    first, second = _short_title_puzzles()

    assert _soup_looks_alike(first, second) is False
    plugin._test_deck.remember(first)  # noqa: SLF001
    assert plugin._test_deck.find_duplicate(second) is None  # noqa: SLF001
    assert plugin._soup_duplicate_note(second, plugin._test_deck) == ""  # noqa: SLF001


def test_disabled_generated_puzzle_still_blocks_duplicates(tmp_path: Path) -> None:
    """停用只是不上桌，不代表同一个故事可以换个说法重新入库。"""
    plugin = make_plugin(tmp_path)
    puzzle = puzzle_from_mapping(
        json.loads(payload("关灯的人", surface=STORY_A_SURFACE, solution=STORY_A_SOLUTION)),
        content_level="normal",
    )
    store = plugin._soup_generated()  # noqa: SLF001
    assert store.add(puzzle, model="m") is True
    store.set_disabled(1, True)

    note = plugin._soup_duplicate_note(puzzle, plugin._test_deck)  # noqa: SLF001

    assert "模型出题存档" in note
