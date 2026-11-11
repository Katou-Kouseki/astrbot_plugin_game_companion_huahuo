"""海龟汤题库去重：跨局账本 + 文本相似度 + 网络题库拉取。

为什么需要单独的账本：

* 房间里的 `turtle_soup_recent_signatures` 只记本房间最近 8 条，且只比
  `theme|trick` 两个短字段——模型把主题换个说法就绕过去了，玩家体感就是
  「怎么又是这一题」；
* 房间销毁、插件重启后这份内存记录直接丢失，同一群连着玩三天必然重复；
* 兜底题库原先只有 3 道，且「全部用过」时会退回全量随机，重复感更明显。

这里的账本落在插件数据目录（`turtle_soup_deck.json`），记录每道题的面/底
摘要，用「归一化文本 + 字符 3-gram Jaccard」判断是否实质重复，兼顾：
换个标题但故事一样（判重）、同题材不同故事（放行）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiohttp

from .turtle_soup import (
    SoupContentLevel,
    SoupPuzzle,
    normalize_content_level,
    puzzle_from_mapping,
)

# 相似度阈值：3-gram Jaccard。同一道题改写标题/微调措辞通常 0.7+；
# 同题材但不同故事一般低于 0.3，取 0.55 留出「换个说法重出」的判重余量。
DEDUP_SIMILARITY_THRESHOLD = 0.55
# 账本上限：一条约 0.5~1KB，240 条约 200KB，足够覆盖一个群几个月的出题量。
DECK_LIMIT = 240
# 汤面/汤底存入账本时的截断长度：判重只需要前半段，省磁盘也够用。
_TEXT_STORE_LIMIT = 320

_CONTENT_ORDER = {"all_ages": 0, "normal": 1, "unrestricted": 2}


def normalize_text(value: Any, *, limit: int = 0) -> str:
    """归一化文本：全角转半角、小写、去空白与标点，只留文字与数字。

    海龟汤的重复往往只差标点、空格和全半角；先归一化再比对，
    避免「一模一样但差一个逗号」被判成新题。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text[:limit] if limit else text


def _ngrams(text: str, size: int = 3) -> set[str]:
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def text_similarity(left: Any, right: Any) -> float:
    """两段中文文本的字符 3-gram Jaccard 相似度（0~1）。"""
    a = _ngrams(normalize_text(left))
    b = _ngrams(normalize_text(right))
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def puzzle_fingerprint(puzzle: SoupPuzzle) -> str:
    """题目的强指纹：标题 + 汤面归一化后的哈希。

    标题或汤面任何实质改动都会换指纹，所以它只能挡住「原样重复」；
    语义级判重靠 :func:`text_similarity`。
    """
    raw = f"{normalize_text(puzzle.title)}|{normalize_text(puzzle.surface)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _record_from_puzzle(puzzle: SoupPuzzle, source: str) -> dict[str, Any]:
    return {
        "fp": puzzle_fingerprint(puzzle),
        "sig": puzzle.signature,
        "title": puzzle.title[:80],
        "surface": puzzle.surface[:_TEXT_STORE_LIMIT],
        "solution": puzzle.solution[:_TEXT_STORE_LIMIT],
        "theme": puzzle.theme[:80],
        "trick": puzzle.trick[:160],
        "source": str(source or "llm")[:16],
        "at": time.time(),
    }


def spoiler_terms(puzzle: SoupPuzzle) -> set[str]:
    """汤底里出现过的字词片段（2-gram），用来识别「已经点破答案」的提问。

    群通知会摘录玩家的提问——大部分提问是安全的，但「和幼儿园有关吗」这种
    一句话就点中了汤底里的独特物件，直接贴出来等于剧透。这里不追求语义理解，
    只用「与汤底共享的 3-gram 比例」做保守过滤：宁可少贴一条，也不漏底。

    为什么是 3-gram 而不是 2-gram：「蛋糕」「信号」这类常用词会让几乎所有提问
    都命中，而「幼儿园」这种独特片段才是真正的泄底信号。
    """
    # 只取汤底本体（key_facts 是它的复述，加进来会让「蛋糕是……」这类
    # 正常提问也命中，反而把有效提问全滤掉）
    text = normalize_text(puzzle.solution + puzzle.trick)
    return _ngrams(text, 3)


def _is_word_char(char: str) -> bool:
    return char.isalnum() or "\u4e00" <= char <= "\u9fff"


def redact_spoiler(text: str, puzzle: SoupPuzzle, *, window: int = 3) -> str:
    """把提问里「已经贴着汤底」的片段替换成「……」而不是整条丢掉。

    为什么是脱敏而不是过滤：玩家问「和幼儿园有关吗？」这种句子，形状本身很适合
    放进群通知（别人一看就想知道幼儿园怎么了），但原文贴出去等于给答案。
    遮掉关键片段后既保留悬念，也不至于把有价值的提问全删光。
    """
    raw = str(text or "")
    chars = list(raw)
    if len(chars) < window:
        return raw
    terms = spoiler_terms(puzzle)
    masked = [False] * len(chars)
    for start in range(len(chars) - window + 1):
        chunk = "".join(chars[start : start + window])
        if not all(_is_word_char(char) for char in chunk):
            continue
        if normalize_text(chunk) in terms:
            for index in range(start, start + window):
                masked[index] = True
    if not any(masked):
        return raw
    out: list[str] = []
    in_mask = False
    for char, hide in zip(chars, masked):
        if hide:
            if not in_mask:
                out.append("……")
                in_mask = True
        else:
            in_mask = False
            out.append(char)
    return "".join(out).strip()


class SoupDeck:
    """已出过的海龟汤账本（JSON 持久化，进程内缓存）。"""

    def __init__(
        self,
        path: Path,
        *,
        limit: int = DECK_LIMIT,
        threshold: float = DEDUP_SIMILARITY_THRESHOLD,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = max(1, int(limit))
        self.threshold = float(threshold)
        self._records: list[dict[str, Any]] = []
        self.load()

    # ---------------- 文件 IO ----------------

    def load(self) -> None:
        records: list[dict[str, Any]] = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            raw = raw.get("puzzles")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                surface = str(item.get("surface") or "")
                title = str(item.get("title") or "")
                if not surface and not title:
                    continue
                records.append(
                    {
                        "fp": str(item.get("fp") or ""),
                        "sig": str(item.get("sig") or ""),
                        "title": title[:80],
                        "surface": surface[:_TEXT_STORE_LIMIT],
                        "solution": str(item.get("solution") or "")[:_TEXT_STORE_LIMIT],
                        "theme": str(item.get("theme") or "")[:80],
                        "trick": str(item.get("trick") or "")[:160],
                        "source": str(item.get("source") or "")[:16],
                        "at": float(item.get("at") or 0.0),
                    }
                )
        records.sort(key=lambda entry: float(entry.get("at") or 0.0))
        self._records = records[-self.limit :]

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(
                    {"version": 1, "puzzles": self._records},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:  # 磁盘只读/权限异常不该让出题流程崩掉
            pass

    # ---------------- 判重 ----------------

    def signature_hit(self, puzzle: SoupPuzzle) -> bool:
        return any(
            record.get("sig") and record["sig"] == puzzle.signature
            for record in self._records
        )

    def find_duplicate(self, puzzle: SoupPuzzle) -> dict[str, Any] | None:
        """返回判定为重复的那条记录（没有则 None）。"""
        fingerprint = puzzle_fingerprint(puzzle)
        for record in reversed(self._records):
            if record.get("fp") and record["fp"] == fingerprint:
                return record
        if self.signature_hit(puzzle):
            return next(
                record
                for record in reversed(self._records)
                if record.get("sig") == puzzle.signature
            )
        for record in reversed(self._records):
            if (
                text_similarity(puzzle.title, record.get("title")) >= 0.82
                or text_similarity(puzzle.surface, record.get("surface"))
                >= self.threshold
                or text_similarity(puzzle.solution, record.get("solution"))
                >= self.threshold
            ):
                return record
        return None

    def has(self, puzzle: SoupPuzzle) -> bool:
        return self.find_duplicate(puzzle) is not None

    def would_repeat(
        self, puzzles: Sequence[SoupPuzzle], *, skip: SoupPuzzle | None = None
    ) -> list[SoupPuzzle]:
        """把一批候选按账本与批内互斥过滤，返回可用的题目。"""
        kept: list[SoupPuzzle] = []
        for puzzle in puzzles:
            if skip is not None and puzzle is skip:
                continue
            if self.has(puzzle):
                continue
            if any(
                puzzle_fingerprint(puzzle) == puzzle_fingerprint(existing)
                or text_similarity(puzzle.surface, existing.surface) >= self.threshold
                for existing in kept
            ):
                continue
            kept.append(puzzle)
        return kept

    # ---------------- 记账 ----------------

    def remember(self, puzzle: SoupPuzzle, *, source: str = "llm") -> None:
        record = _record_from_puzzle(puzzle, source)
        self._records = [
            entry
            for entry in self._records
            if not (
                entry.get("fp") == record["fp"]
                or (entry.get("sig") and entry["sig"] == record["sig"] and record["sig"])
            )
        ]
        self._records.append(record)
        self._records = self._records[-self.limit :]
        self.save()

    def forget(self, surface: str, *, puzzle: SoupPuzzle | None = None) -> int:
        """把一道题从账本里抹掉（管理台「单独重置出题」），返回删掉的条数。

        按汤面为主键，另外允许带上题目对象补指纹 / 签名。只认精确命中，
        不做相似度扩散：管理员点的是「这一道」，不该把同题材的其它题也放出来。
        """
        target = normalize_text(str(surface or "")[:_TEXT_STORE_LIMIT])
        fingerprint = puzzle_fingerprint(puzzle) if puzzle is not None else ""
        signature = puzzle.signature if puzzle is not None else ""
        before = len(self._records)

        def matches(entry: dict[str, Any]) -> bool:
            if fingerprint and entry.get("fp") == fingerprint:
                return True
            if signature and entry.get("sig") == signature:
                return True
            return bool(target) and normalize_text(entry.get("surface")) == target

        self._records = [entry for entry in self._records if not matches(entry)]
        removed = before - len(self._records)
        if removed:
            self.save()
        return removed

    # ---------------- 查询 ----------------

    def known_surfaces(self) -> set[str]:
        """账本里出现过的汤面（归一化）。

        给内置题库用：它有两百多条，逐条做相似度判重太慢，先用「汤面完全相同」
        快速筛掉，再把剩下的交给 :meth:`has` 做语义级确认。
        """
        return {
            normalize_text(record.get("surface"))
            for record in self._records
            if record.get("surface")
        }

    def recent_signatures(self, limit: int = 8) -> list[str]:
        result: list[str] = []
        for record in reversed(self._records):
            signature = str(record.get("sig") or "")
            if signature and signature not in result:
                result.append(signature)
            if len(result) >= max(1, limit):
                break
        return list(reversed(result))

    def recent_titles(self, limit: int = 5) -> list[str]:
        return [
            str(record.get("title") or "").strip()
            for record in reversed(self._records[-max(1, limit) :])
            if str(record.get("title") or "").strip()
        ]

    def recent_surfaces(self, limit: int = 3) -> list[str]:
        return [
            str(record.get("surface") or "").strip()
            for record in reversed(self._records[-max(1, limit) :])
            if str(record.get("surface") or "").strip()
        ]

    def oldest_records(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._records[: max(1, limit)]

    def stats(self) -> dict[str, Any]:
        """账本概况：一共出过多少题、各来源各多少（管理台题库页用）。"""
        by_source: dict[str, int] = {}
        for record in self._records:
            key = str(record.get("source") or "llm")[:16]
            by_source[key] = by_source.get(key, 0) + 1
        return {"total": len(self._records), "by_source": by_source}

    def clear(self) -> int:
        """清空出题账本（重置「哪些题已经出过」），返回清掉的条数。"""
        removed = len(self._records)
        self._records = []
        self.save()
        return removed


async def fetch_online_puzzles(
    url: str,
    *,
    content_level: SoupContentLevel,
    limit: int = 8,
    timeout: float = 12.0,
) -> list[SoupPuzzle]:
    """从自建/第三方题库地址拉取候选题目（JSON）。

    约定：返回 JSON 数组，或 `{"puzzles": [...]}`；每项字段与 LLM 出题一致
    （title/surface/solution/key_facts/hints/theme/trick，可选 content_level）。
    任何网络或格式问题都返回空列表，由调用方继续走 LLM / 本地题库——
    网络题库是「锦上添花」，绝不能因为它挂掉而出不了题。
    """
    target = str(url or "").strip()
    if not target:
        return []
    if not re.match(r"^https?://", target, re.IGNORECASE):
        raise ValueError("题库地址必须以 http:// 或 https:// 开头")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(
                target, headers={"User-Agent": "AstrBot-GameCompanion"}
            ) as response:
                if response.status != 200:
                    return []
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
        return []
    raw_items: Iterable[Any]
    if isinstance(payload, dict):
        raw_items = payload.get("puzzles") or payload.get("items") or ()
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = ()
    ceiling = _CONTENT_ORDER.get(content_level, 1)
    puzzles: list[SoupPuzzle] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        level = normalize_content_level(
            item.get("content_level") or content_level
        )
        if _CONTENT_ORDER.get(level, 1) > ceiling:
            continue
        try:
            puzzles.append(puzzle_from_mapping(item, content_level=level))
        except (ValueError, TypeError):
            continue
        if len(puzzles) >= max(1, limit):
            break
    return puzzles
