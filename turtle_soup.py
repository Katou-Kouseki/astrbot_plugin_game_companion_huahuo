from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .gomoku import Difficulty

SoupContentLevel = Literal["all_ages", "normal", "unrestricted"]
SoupVerdict = Literal["yes", "no", "irrelevant", "partial", "compound"]
SoupMode = Literal["bot_host", "player_host"]
SoupEntryKind = Literal["question", "answer", "hint", "reverse"]
SoupBotAction = Literal["question", "guess"]


VERDICT_LABELS: dict[SoupVerdict, str] = {
    "yes": "是",
    "no": "否",
    "irrelevant": "无关",
    "partial": "部分正确",
    "compound": "请一次只问一个问题",
}


@dataclass(slots=True, frozen=True)
class SoupPuzzle:
    title: str
    surface: str
    solution: str
    key_facts: tuple[str, ...]
    acceptable_variants: tuple[str, ...]
    hints: tuple[str, ...]
    content_level: SoupContentLevel
    theme: str
    trick: str

    @property
    def signature(self) -> str:
        return f"{self.theme.strip().lower()}|{self.trick.strip().lower()}"[:180]

    def public_snapshot(self, *, reveal_solution: bool) -> dict[str, object]:
        data: dict[str, object] = {
            "title": self.title,
            "surface": self.surface,
            "content_level": self.content_level,
        }
        if reveal_solution:
            data["solution"] = self.solution
            data["key_facts"] = list(self.key_facts)
        return data


@dataclass(slots=True)
class SoupEntry:
    kind: SoupEntryKind
    prompt: str
    response: str
    source: Literal["web", "qq", "system"]
    verdict: SoupVerdict | None = None
    player_number: int | None = None
    bot_action: SoupBotAction | None = None
    at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "response": self.response,
            "source": self.source,
            "verdict": self.verdict,
            "player_number": self.player_number,
            "bot_action": self.bot_action,
            "at": self.at,
        }


@dataclass(slots=True)
class TurtleSoupGame:
    difficulty: Difficulty
    max_hints: int
    content_level: SoupContentLevel
    mode: SoupMode = "bot_host"
    puzzle: SoupPuzzle | None = None
    phase: Literal["preparing", "ready", "finished"] = "preparing"
    processing: bool = False
    processing_player_number: int | None = None
    entries: list[SoupEntry] = field(default_factory=list)
    discovered_facts: set[int] = field(default_factory=set)
    question_count: int = 0
    answer_attempts: int = 0
    hints_used: int = 0
    # 每位玩家领过几条（仅用于展示「谁问过提示」）
    hint_usage: dict[str, int] = field(default_factory=dict)
    # 本局已经发出去的提示索引：不同玩家拿到的提示保证不重复
    issued_hints: set[int] = field(default_factory=set)
    solved: bool = False
    gave_up: bool = False
    bot_solved: bool = False
    turn_count: int = 0
    last_bot_action: SoupBotAction | None = None
    last_bot_text: str = ""
    failure_reason: str = ""
    last_judge_at: float = 0.0
    # 结束原因：solved（解开）/ revealed（玩家投票揭晓）/ timeout（超时兜底揭晓）
    # / gave_up（旧版「放弃」入口，保留兼容）/ bot_solved（玩家出题、花火猜中）
    end_reason: str = ""
    # 揭晓汤底的投票：发起人 uid -> 投票 uid:是否同意。过半（单人房直接放行）即揭晓。
    reveal_requested_by: str = ""
    reveal_requested_at: float = 0.0
    reveal_votes: dict[str, bool] = field(default_factory=dict)
    # 每次提交推理的覆盖率历史：连续「很接近」却解不出时，给一条免费提示。
    coverage_history: list[float] = field(default_factory=list)
    last_missing: str = ""
    # 最后一次有实质推进的时间（提问/推理/提示都算）：超时兜底揭晓的计时基准。
    last_progress_at: float = field(default_factory=time.time)
    # 同一道题里已经因「连续接近」送过的提示数，避免每次都送
    auto_hints_sent: int = 0
    # 玩家出题人座位号（0 = 花火出题）。出题人不参与提问与回合轮转，只负责裁决：
    # 其他玩家和花火一起猜，他按「是/否/无关/部分正确」回答，并对推理判对错。
    host_number: int = 0
    # 等出题人裁决的一条提问/推理（同一时间只允许一条）
    # {"kind": "question"|"answer", "text": str, "asker_number": int,
    #  "asker_name": str, "for_bot": bool, "at": float}
    pending_judge: dict[str, object] | None = None
    # 猜中人：座位号；0 表示花火猜中（配合 bot_solved）
    solver_number: int = 0

    def __post_init__(self) -> None:
        if self.mode == "player_host" and self.phase == "preparing":
            self.phase = "ready"

    @property
    def finished(self) -> bool:
        return self.phase == "finished"

    @property
    def draw(self) -> bool:
        return False

    @property
    def winner(self) -> str:
        if not self.finished:
            return ""
        if self.mode == "player_host":
            return "bot"
        if self.host_number:
            # 玩家出题：谁猜中算谁赢（花火猜中时 bot_solved 为真、solved 保持假）
            if self.bot_solved:
                return "bot"
            return "human" if self.solved else "bot"
        return "human" if self.solved else "bot"

    @property
    def history(self) -> list[SoupEntry]:
        return self.entries

    def set_puzzle(self, puzzle: SoupPuzzle) -> None:
        if self.mode != "bot_host":
            raise ValueError("玩家出题模式不使用 Bot 题库")
        if self.phase != "preparing":
            raise ValueError("当前海龟汤已经完成出题")
        self.puzzle = puzzle
        self.phase = "ready"
        self.failure_reason = ""

    def begin_processing(self) -> None:
        if self.phase == "preparing":
            raise ValueError("Bot 还在准备题目")
        if self.phase == "finished":
            raise ValueError("本题已经结束")
        if self.processing:
            raise ValueError("Bot 正在判断上一条内容")
        if self.mode == "bot_host" and self.puzzle is None:
            raise ValueError("题目尚未准备完成")
        self.processing = True

    def cancel_processing(self, reason: str = "") -> None:
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = str(reason or "")[:160]

    def record_question(
        self,
        question: str,
        verdict: SoupVerdict,
        *,
        source: Literal["web", "qq"],
        matched_facts: set[int] | None = None,
        player_number: int | None = None,
    ) -> set[int]:
        if self.phase != "ready":
            raise ValueError("当前不能继续提问")
        newly_discovered = set(matched_facts or ()) - self.discovered_facts
        self.discovered_facts.update(matched_facts or ())
        self.question_count += 1
        self.entries.append(
            SoupEntry(
                kind="question",
                prompt=question,
                response=VERDICT_LABELS[verdict],
                source=source,
                verdict=verdict,
                player_number=player_number,
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""
        self.last_judge_at = time.time()
        self.last_progress_at = self.last_judge_at
        return newly_discovered

    def record_answer(
        self,
        answer: str,
        *,
        solved: bool,
        source: Literal["web", "qq"],
        matched_facts: set[int] | None = None,
        player_number: int | None = None,
        reply: str = "",
        coverage: float = 0.0,
        missing: str = "",
    ) -> set[int]:
        if self.phase != "ready":
            raise ValueError("当前不能提交答案")
        newly_discovered = set(matched_facts or ()) - self.discovered_facts
        self.discovered_facts.update(matched_facts or ())
        self.answer_attempts += 1
        self.coverage_history.append(max(0.0, min(float(coverage or 0.0), 1.0)))
        del self.coverage_history[:-12]
        self.last_missing = str(missing or "")[:24]
        response = reply or (
            "推理正确，汤底已经揭晓。" if solved else "已经接近了，但还缺少关键环节。"
        )
        self.entries.append(
            SoupEntry(
                kind="answer",
                prompt=answer,
                response=response,
                source=source,
                player_number=player_number,
                verdict="yes" if solved else "partial",
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""
        self.last_judge_at = time.time()
        self.last_progress_at = self.last_judge_at
        if solved:
            self.solved = True
            self.end_reason = "solved"
            self.phase = "finished"
        return newly_discovered

    def set_pending_judge(
        self,
        kind: Literal["question", "answer"],
        text: str,
        *,
        asker_number: int = 0,
        asker_name: str = "",
        for_bot: bool = False,
    ) -> dict[str, object]:
        """把一条提问/推理挂到出题人面前，等他裁决（同一时间只允许一条）。"""
        if self.phase != "ready":
            raise ValueError("当前没有正在进行的海龟汤")
        if self.pending_judge is not None:
            raise ValueError("出题人还没回答上一个问题")
        self.pending_judge = {
            "kind": kind,
            "text": str(text)[:600],
            "asker_number": int(asker_number or 0),
            "asker_name": str(asker_name or "")[:40],
            "for_bot": bool(for_bot),
            "at": time.time(),
        }
        self.last_progress_at = time.time()
        return dict(self.pending_judge)

    def clear_pending_judge(self) -> dict[str, object] | None:
        pending = self.pending_judge
        self.pending_judge = None
        return dict(pending) if pending else None

    def request_reveal(self, uid: str) -> dict[str, object]:
        """发起「揭晓汤底」：多人房需要过半同意，单人房直接生效。"""
        if self.phase != "ready":
            raise ValueError("当前没有可以揭晓的海龟汤")
        if self.mode == "player_host":
            raise ValueError("玩家出题模式没有固定汤底可揭晓")
        key = str(uid or "")
        if not self.reveal_requested_by:
            self.reveal_requested_by = key
            self.reveal_requested_at = time.time()
        self.reveal_votes[key] = True
        return {
            "requested_by": self.reveal_requested_by,
            "votes": sum(1 for agree in self.reveal_votes.values() if agree),
            "agreed": [token for token, agree in self.reveal_votes.items() if agree],
        }

    def cast_reveal_vote(self, uid: str, agree: bool) -> dict[str, object]:
        if self.phase != "ready":
            raise ValueError("当前没有可以揭晓的海龟汤")
        if not self.reveal_requested_by:
            raise ValueError("还没有人发起揭晓汤底")
        key = str(uid or "")
        self.reveal_votes[key] = bool(agree)
        return {
            "requested_by": self.reveal_requested_by,
            "votes": sum(1 for value in self.reveal_votes.values() if value),
            "agreed": [token for token, value in self.reveal_votes.items() if value],
        }

    def reveal(self, *, reason: str, source: Literal["web", "qq"] = "web") -> None:
        """结束本题并揭晓汤底（玩家投票或超时兜底）。

        语义与「投降」区分：这不是某一方认输，而是「这题到此为止，公布答案」，
        比分记为花火守住（与放弃一致），战报文案写「无人解开」。
        """
        if self.phase == "finished":
            return
        self.solved = False
        self.gave_up = False
        self.end_reason = str(reason or "revealed")
        self.phase = "finished"
        self.processing = False
        self.processing_player_number = None
        self.entries.append(
            SoupEntry(
                kind="answer",
                prompt=(
                    "超时揭晓汤底"
                    if self.end_reason == "timeout"
                    else "投票揭晓汤底"
                ),
                response="本题已揭晓，汤底见下方。",
                source=source,
            )
        )
        del self.entries[:-80]

    def reveal_hint(
        self,
        *,
        source: Literal["web", "qq"],
        player_number: int | None = None,
        player_uid: str = "",
    ) -> str:
        if self.phase != "ready" or self.puzzle is None:
            raise ValueError("当前不能申请提示")
        allowed = len(self.puzzle.hints)
        if self.max_hints:
            allowed = min(allowed, self.max_hints)
        # 总量限制：整局一共只给 allowed 条。以前是「每人各算一份」，
        # 结果 3 个玩家各领一次，同一句提示被念了三遍。
        if self.hints_used >= allowed:
            raise ValueError("本局的提示次数已经用完")
        # 按题目顺序取还没发过的那条：不同玩家拿到的提示一定不一样
        index = next(
            (
                i
                for i in range(len(self.puzzle.hints))
                if i not in self.issued_hints
            ),
            None,
        )
        if index is None:
            raise ValueError("本局的提示已经全部给出")
        hint = self.puzzle.hints[index]
        self.issued_hints.add(index)
        key = str(player_uid or "") or str(player_number or 0)
        self.hint_usage[key] = self.hint_usage.get(key, 0) + 1
        self.hints_used += 1
        self.entries.append(
            SoupEntry(
                kind="hint",
                prompt="申请提示",
                response=hint,
                source=source,
                player_number=player_number,
            )
        )
        del self.entries[:-80]
        self.last_progress_at = time.time()
        return hint

    def give_up(self, *, source: Literal["web", "qq"] = "qq") -> None:
        if self.phase != "ready" or (self.mode == "bot_host" and self.puzzle is None):
            raise ValueError("当前没有可以放弃的海龟汤")
        self.gave_up = self.mode == "bot_host"
        self.end_reason = "gave_up" if self.mode == "bot_host" else "host_ended"
        self.phase = "finished"
        self.processing = False
        self.processing_player_number = None
        self.entries.append(
            SoupEntry(
                kind="answer",
                prompt=(
                    "结束玩家出题" if self.mode == "player_host" else "放弃并查看汤底"
                ),
                response=(
                    "本题已结束。"
                    if self.mode == "player_host"
                    else "本题已结束，汤底已经揭晓。"
                ),
                source=source,
            )
        )
        del self.entries[:-80]

    def record_reverse_turn(
        self,
        player_text: str,
        *,
        bot_action: SoupBotAction,
        bot_text: str,
        source: Literal["web", "qq"],
        player_number: int | None = None,
    ) -> None:
        if self.mode != "player_host" or self.phase != "ready":
            raise ValueError("当前不是玩家出题回合")
        self.turn_count += 1
        if bot_action == "question":
            self.question_count += 1
        else:
            self.answer_attempts += 1
        self.last_bot_action = bot_action
        self.last_bot_text = bot_text
        self.entries.append(
            SoupEntry(
                kind="reverse",
                prompt=player_text,
                response=bot_text,
                source=source,
                player_number=player_number,
                bot_action=bot_action,
            )
        )
        del self.entries[:-80]
        self.processing = False
        self.processing_player_number = None
        self.failure_reason = ""

    def confirm_bot_guess(self, *, correct: bool) -> None:
        if self.mode != "player_host" or self.phase != "ready":
            raise ValueError("当前不能判定 Bot 的猜测")
        if self.last_bot_action != "guess":
            raise ValueError("Bot 目前还没有提交可判定的猜测")
        if correct:
            self.bot_solved = True
            self.solved = False
            self.end_reason = "bot_solved"
            self.phase = "finished"
        self.processing = False
        self.processing_player_number = None

    def snapshot(
        self, viewer_uid: str | None = None, *, reveal_required: int = 0
    ) -> dict[str, object]:
        reveal_solution = self.finished and self.puzzle is not None
        available_hints = len(self.puzzle.hints) if self.puzzle else 0
        hint_limit = (
            min(self.max_hints, available_hints) if self.max_hints else available_hints
        )
        viewer_key = str(viewer_uid or "")
        viewer_used = self.hint_usage.get(viewer_key, 0) if viewer_key else 0
        reveal_votes = sum(1 for agree in self.reveal_votes.values() if agree)
        return {
            "kind": "turtle_soup",
            "mode": self.mode,
            "phase": self.phase,
            "preparing": self.phase == "preparing",
            "processing": self.processing,
            "failure_reason": self.failure_reason,
            "puzzle": self.puzzle.public_snapshot(reveal_solution=reveal_solution)
            if self.puzzle
            else None,
            "entries": [entry.snapshot() for entry in self.entries[-60:]],
            "question_count": self.question_count,
            "answer_attempts": self.answer_attempts,
            "hints_used": self.hints_used,
            "hint_used_self": viewer_used,
            "hint_limit": hint_limit,
            "hint_left": max(0, hint_limit - self.hints_used),
            "discovered_fact_count": len(self.discovered_facts),
            "key_fact_count": len(self.puzzle.key_facts) if self.puzzle else 0,
            "solved": self.solved,
            "gave_up": self.gave_up,
            "bot_solved": self.bot_solved,
            "turn_count": self.turn_count,
            "last_bot_action": self.last_bot_action,
            "last_bot_text": self.last_bot_text,
            "finished": self.finished,
            "end_reason": self.end_reason,
            # 揭晓汤底的投票进度：前端据此显示「已有 1/2 人同意」并给投票按钮
            "reveal": {
                "requested": bool(self.reveal_requested_by),
                "votes": reveal_votes,
                "required": max(0, int(reveal_required or 0)),
                "requested_at": self.reveal_requested_at,
            },
            "coverage_last": self.coverage_history[-1] if self.coverage_history else 0.0,
            "missing_hint": self.last_missing,
            # 玩家出题人（0 = 花火出题）：前端据此显示「我在出题」与裁决按钮
            "host_number": self.host_number,
            "pending_judge": dict(self.pending_judge) if self.pending_judge else None,
            "solver_number": self.solver_number,
        }


def normalize_content_level(value: Any) -> SoupContentLevel:
    normalized = str(value or "normal").strip().lower()
    aliases = {
        "all_ages": "all_ages",
        "all-ages": "all_ages",
        "全年龄": "all_ages",
        "normal": "normal",
        "普通": "normal",
        "unrestricted": "unrestricted",
        "不限制": "unrestricted",
    }
    return aliases.get(normalized, "normal")  # type: ignore[return-value]


def puzzle_from_mapping(
    data: dict[str, Any], *, content_level: SoupContentLevel
) -> SoupPuzzle:
    title = _text(data.get("title"), 80)
    surface = _text(data.get("surface"), 800)
    solution = _text(data.get("solution"), 1600)
    key_facts = _text_items(data.get("key_facts"), maximum=8, item_limit=260)
    variants = _text_items(data.get("acceptable_variants"), maximum=8, item_limit=260)
    hints = _text_items(data.get("hints"), maximum=8, item_limit=260)
    theme = _text(data.get("theme"), 80)
    trick = _text(data.get("trick"), 160)
    if len(title) < 2:
        raise ValueError("题目标题过短")
    if len(surface) < 20:
        raise ValueError("汤面信息不足")
    if len(solution) < 30:
        raise ValueError("汤底信息不足")
    if len(key_facts) < 3:
        raise ValueError("关键事实不足")
    if len(hints) < 3:
        raise ValueError("渐进提示不足")
    if not theme or not trick:
        raise ValueError("题目缺少主题或核心诡计标记")
    return SoupPuzzle(
        title=title,
        surface=surface,
        solution=solution,
        key_facts=tuple(key_facts),
        acceptable_variants=tuple(variants),
        hints=tuple(hints),
        content_level=content_level,
        theme=theme,
        trick=trick,
    )


def _content_rank(level: str) -> int:
    return {"all_ages": 0, "normal": 1, "unrestricted": 2}.get(str(level), 1)


def all_fallback_puzzles(
    *, content_level: SoupContentLevel
) -> list[SoupPuzzle]:
    """内置题库按内容等级过滤后的全部题目（允许更轻的内容，不允许更重的）。"""
    ceiling = _content_rank(content_level)
    puzzles: list[SoupPuzzle] = []
    for item in _FALLBACK_PUZZLES:
        level = normalize_content_level(item.get("content_level"))
        if _content_rank(level) > ceiling:
            continue
        try:
            puzzles.append(puzzle_from_mapping(item, content_level=level))
        except ValueError:  # 题库写坏了不该拖垮出题
            continue
    return puzzles


def fallback_puzzle(
    *,
    content_level: SoupContentLevel,
    excluded_signatures: set[str] | None = None,
) -> SoupPuzzle:
    """从内置题库挑一道：优先没出过的，全都出过就挑「最久以前出的」。

    以前「全部排除后回退到全量随机」，会让刚玩过的题立刻再来一次；
    现在交给调用方传入按账本排好序的候选（`preferred`），没有就随机。
    """
    candidates = all_fallback_puzzles(content_level=content_level)
    if not candidates:
        # 极端情况：题库被改空，构造一道最小可用题目保证流程能继续
        return puzzle_from_mapping(
            {
                "title": "空白的一天",
                "surface": "有个人一整天什么都没做，却说自己完成了一件重要的事。为什么？",
                "solution": "他答应自己休息一天，整天没有工作、没有社交，只是安静地待着；对他来说「允许自己停下来」本身就是完成的事。",
                "key_facts": ["他原本有必须完成的工作", "他刻意选择休息", "休息对他而言是难得的事"],
                "hints": ["重点不在他做了什么。", "他平时很忙。", "他主动选择了停下。"],
                "theme": "休息与自我",
                "trick": "把「做事」当成唯一的价值",
            },
            content_level=content_level,
        )
    excluded = excluded_signatures or set()
    available = [item for item in candidates if item.signature not in excluded]
    return secrets.choice(available or candidates)


def clean_player_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError("内容不能为空")
    if len(text) > limit:
        raise ValueError(f"内容不能超过 {limit} 个字符")
    return text


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _text_items(value: Any, *, maximum: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


_FALLBACK_PUZZLES: tuple[dict[str, Any], ...] = (
    {
        "content_level": "all_ages",
        "title": "没有寄出的明信片",
        "surface": "小林旅行回来后，把一张写好的明信片放进抽屉。他没有寄出它，却因此确认朋友已经收到了自己的祝福。为什么？",
        "solution": "小林和朋友约定，各自在旅行地写一张明信片，但不邮寄，而是在回家后视频展示。朋友在视频中读出了小林明信片背面的内容，所以小林确认祝福已经通过视频传达；明信片本身仍留在抽屉里。",
        "key_facts": [
            "祝福并非依靠邮寄传达",
            "两人事先约定通过视频展示",
            "朋友已经在视频中读到明信片内容",
        ],
        "acceptable_variants": ["通过照片、直播或视频通话看到了明信片内容也可视为等价"],
        "hints": [
            "重点不是邮局出了问题。",
            "朋友看到了信息，但没有拿到那张纸。",
            "两人使用了实时影像交流。",
        ],
        "theme": "旅行与通讯",
        "trick": "收到祝福不等于收到实体明信片",
    },
    {
        "content_level": "all_ages",
        "title": "最后一名的奖牌",
        "surface": "阿远在比赛中最后一个到达终点，却获得了唯一一枚奖牌，而且所有参赛者都认为公平。为什么？",
        "solution": "这是一场接力式的公益徒步，奖牌交给最后一棒的代表，由他代全队领取。阿远是整支队伍最后通过终点的人，也是被事先指定的领奖代表；比赛并不按个人到达顺序排名。",
        "key_facts": [
            "比赛不是个人竞速排名",
            "阿远代表一个团队",
            "唯一奖牌属于全队并由最后一棒代领",
        ],
        "acceptable_variants": ["团体完赛纪念活动、非竞速接力等设定可视为等价"],
        "hints": [
            "最后到达不一定代表成绩最差。",
            "奖牌并不只属于阿远个人。",
            "这是团体完成的接力活动。",
        ],
        "theme": "体育与团队",
        "trick": "把团体完赛误认为个人竞速",
    },
    {
        "content_level": "normal",
        "title": "每天准时的空椅子",
        "surface": "餐馆每天打烊前都会摆出一把空椅子。某天店员忘了摆，老板立刻报警，警方也认为他的决定合理。为什么？",
        "solution": "餐馆与附近独居老人约定：老人每天散步时会把门口的折叠椅搬到指定位置，表示自己平安；店员打烊前再把椅子收回。那天椅子仍在店内，说明老人没有按约出现。老板联系不上老人后报警，警方上门发现老人突发疾病并及时救助。",
        "key_facts": [
            "空椅子是约定好的平安信号",
            "平时由附近老人移动椅子",
            "当天信号没有出现且老人失联",
            "报警目的是确认并救助老人",
        ],
        "acceptable_variants": ["其他通过日常物品确认独居者平安的约定可视为等价"],
        "hints": [
            "椅子不是为顾客准备的。",
            "它的位置代表某个人当天是否出现。",
            "这是餐馆与独居老人约定的平安信号。",
        ],
        "theme": "社区与平安确认",
        "trick": "空椅子是一种非语言信号",
    },
    {
        "content_level": "all_ages",
        "title": "窗边那块蛋糕",
        "surface": "糕点店每天下午四点会把一块小蛋糕摆在窗边，从不标价也从不卖。熟客都知道这个规矩。某天新来的店员把它卖给了客人，老板立刻追出去把蛋糕要了回来。为什么？",
        "solution": "那块蛋糕是给附近幼儿园一个孩子的路标。孩子放学独自回家，必须看到窗边的蛋糕才肯继续往前走——他的妈妈和店主约好，用这块蛋糕给他壮胆。新店员把它卖掉后，孩子停在门口不肯走，老板追出去用一个新蛋糕换回了它，重新摆回窗边，孩子才继续回家。",
        "key_facts": [
            "蛋糕不是商品，是给固定孩子的信号",
            "那个孩子看到它才肯继续回家",
            "孩子的妈妈与店主事先约定",
            "老板换回蛋糕是为了恢复这个约定",
        ],
        "acceptable_variants": ["把蛋糕换成面包、摆件等固定标志物可视为等价"],
        "hints": [
            "它不是拿来卖的。",
            "每天都有个人会经过这里。",
            "那个孩子需要看到它才敢继续走。",
            "老板追出去，是为了把信号放回原处。",
        ],
        "theme": "街道与约定",
        "trick": "把非卖品当成商品",
    },
    {
        "content_level": "all_ages",
        "title": "多出来的第三把钥匙",
        "surface": "房东交房时给了租客三把钥匙，说都是这扇门的。租客试了试，两把能开门，第三把怎么拧都不动，就把它挂在门口没再管。半年后房东来换锁，租客才知道那把钥匙一直是有用的。为什么？",
        "solution": "第三把是楼下信箱的钥匙，齿形和门钥匙很像，房东当初随口说成了「都是这扇门的」。租客从没用过信箱，所以一直以为它坏了。房东换锁时只换了入户门，信箱锁没换，才顺口提起这件事。",
        "key_facts": [
            "第三把钥匙对应的是信箱锁",
            "房东当初说错了用途",
            "租客从未使用过信箱",
            "换锁时只换了入户门",
        ],
        "acceptable_variants": ["把信箱换成储物柜、报箱等楼下附属锁具可视为等价"],
        "hints": [
            "它确实有用，只是不对这扇门。",
            "楼下还有一个带锁的东西。",
            "那东西平时没什么人打开。",
        ],
        "theme": "租房日常",
        "trick": "钥匙长得像，被误认为同一把锁",
    },
    {
        "content_level": "all_ages",
        "title": "每年寄给自己的包裹",
        "surface": "他每年生日都给自己寄一个包裹，收件地址写公司。今年包裹被退回了，他却说「这样更好」。为什么？",
        "solution": "他寄包裹是想让前台在生日当天叫他去签收，好让同事知道他今天生日——那声生日快乐是他自己安排的。今年他换了独立办公室，前台不再代收，包裹被退回；他索性不再演这场戏，主动约了朋友一起吃饭。",
        "key_facts": [
            "包裹是他自己寄给自己的",
            "目的是让前台在生日当天通知他",
            "换办公室后前台不再代收，包裹被退回",
            "他决定改用真实的方式过生日",
        ],
        "acceptable_variants": ["把公司换成学校、合租公寓等有代收点的场所可视为等价"],
        "hints": [
            "寄件人是谁，值得想一想。",
            "他想要的不是包裹本身。",
            "公司前台曾经帮了他一个忙。",
        ],
        "theme": "孤独与生日",
        "trick": "用物流制造被人记得的错觉",
    },
    {
        "content_level": "all_ages",
        "title": "绕远路的出租车",
        "surface": "司机明明知道一条更近的路，却每次都绕远。乘客从不投诉，还常常多给几块钱。为什么？",
        "solution": "近路要经过一段被重车压坏的碎石路，颠得厉害。那位乘客刚做完腰部手术，坐不了颠簸的路；两人第一次走错时乘客疼得脸色发白，从此司机宁可绕远。多给的钱是乘客主动补的油费。",
        "key_facts": [
            "近路路况很差、非常颠簸",
            "乘客身体不能承受颠簸",
            "绕远是双方默契",
            "多给的钱是乘客主动补偿",
        ],
        "acceptable_variants": ["把腰部手术换成其他不能受颠簸的情况可视为等价"],
        "hints": [
            "近路确实更近，但不是更好。",
            "问题不在于距离。",
            "乘客的身体对某件事很敏感。",
        ],
        "theme": "出行与体谅",
        "trick": "把最近当成最合适",
    },
    {
        "content_level": "normal",
        "title": "凌晨三点的洗衣机",
        "surface": "独居的她每天凌晨三点准时开洗衣机，邻居从不投诉，物业也管不了。为什么？",
        "solution": "她是夜班护士，凌晨三点是下班回家的时间；房子在临街二楼，楼下是 24 小时营业的商铺而不是住户，洗衣机又放在铺了隔音垫的阳台。她入住前逐一和楼下商户打过招呼，而商户本来就通宵营业。",
        "key_facts": [
            "她上夜班，三点是她回家的时间",
            "楼下不是住户而是商铺",
            "洗衣位置做过隔音处理",
            "她事先与楼下商户打过招呼",
        ],
        "acceptable_variants": ["夜班护士换成其他凌晨下班的职业可视为等价"],
        "hints": [
            "三点对她来说不是深夜。",
            "楼下待着的不是住户。",
            "她提前打过招呼。",
        ],
        "theme": "城市夜班",
        "trick": "用自己的作息去推断别人的作息",
    },
    {
        "content_level": "normal",
        "title": "不存在的十三号",
        "surface": "这条街的门牌从 12 号直接跳到 14 号，13 号不存在。但快递、外卖、挂号信都能准确送到 13 号那户人家。为什么？",
        "solution": "13 号院子的大门开在与主街垂直的另一条街上，按另一条街的门牌登记（例如「光明路 5 号」）。主街这一侧只有围墙，没有入口。快递按院内实际入口的地址系统走，所以一直送得到。",
        "key_facts": [
            "13 号的院门开在另一条街上",
            "该户按另一条街的门牌登记",
            "主街这侧只有围墙没有入口",
            "快递按实际入口地址投递",
        ],
        "acceptable_variants": ["把另一条街换成巷子、后街可视为等价"],
        "hints": [
            "13 号并不是不存在。",
            "它的门不在这条街上。",
            "换个地址系统就能找到它。",
        ],
        "theme": "城市地址",
        "trick": "把这条街的门牌当成唯一坐标",
    },
    {
        "content_level": "normal",
        "title": "打烊不锁的后门",
        "surface": "便利店老板每晚打烊都不锁后门，员工提醒他好几次，他说锁上反而更危险。为什么？",
        "solution": "后门连着整栋楼的消防疏散通道，也是店里配电箱和总闸的唯一检修入口；按规定这扇门必须能从内部随时推开。店里夜间有值守的保安，卷帘门和报警器才是真正的防线。",
        "key_facts": [
            "后门是消防疏散通道的一部分",
            "这扇门必须能从内部随时推开",
            "店内夜间有保安值守",
            "真正的防盗靠卷帘门与报警器",
        ],
        "acceptable_variants": ["把便利店换成小餐馆、药店可视为等价"],
        "hints": [
            "这扇门不是用来防贼的。",
            "有一条规定要求它不能锁。",
            "店里还有别人在。",
        ],
        "theme": "小店与安全",
        "trick": "把防盗当成唯一的安全目标",
    },
    {
        "content_level": "normal",
        "title": "共撑一把伞的两个人",
        "surface": "雨天，两个人共撑一把伞走进派出所，其中一个人被留下做笔录，另一个人撑着伞离开，两个人都很平静。为什么？",
        "solution": "拾金不昧的路人把捡到的钱包送到派出所，失主正好也在附近，两人在门口遇上就共撑一把伞进来。留下的失主核对物品、做领取笔录；拾到者做完登记先走。伞是失主的，所以由他带走。",
        "key_facts": [
            "两人不是一伙的，是门口遇上",
            "一个人来上交捡到的钱包",
            "另一个人是失主",
            "离开的人先做完了登记",
        ],
        "acceptable_variants": ["把钱包换成手机、证件等可辨识失物可视为等价"],
        "hints": [
            "被留下的不是嫌疑人。",
            "他们本来并不认识。",
            "伞是其中一个人的。",
        ],
        "theme": "雨天与失物",
        "trick": "把一起进门当成一伙人",
    },
    {
        "content_level": "all_ages",
        "title": "只坐一站却买全程票",
        "surface": "有位乘客每次只坐一站，却坚持买全程票。售票员提醒他可以少花钱，他说这样更划算。为什么？",
        "solution": "全程票是当日不限次数乘坐的通票。他一天要来回很多趟（送孩子、跑医院、买菜），一张张买单程票更贵，而且通票还能在中途改乘另一条线路。",
        "key_facts": [
            "全程票是当日通票、不限次数",
            "他一天要坐很多趟",
            "通票可以换乘另一条线路",
            "单次购票的总价更高",
        ],
        "acceptable_variants": ["把公交换成地铁、轮渡可视为等价"],
        "hints": [
            "他一天不只坐这一趟。",
            "那张票的用法不止一次。",
            "要算一整天的账。",
        ],
        "theme": "城市公交",
        "trick": "只看单程，不看一天",
    },
)
