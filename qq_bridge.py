"""群聊入口：把 QQ 群里的指令接到当前房间的海龟汤上。

为什么要单独一个文件：

* `main.py` 已经一万多行，再往里面塞群聊解析只会更难维护；
* 这一层只调用领域方法（`submit_turtle_soup_question` /
  `request_turtle_soup_hint` / `request_soup_reveal`），不碰房间内部状态——
  以后把海龟汤拆成独立插件时，这层跟着搬走即可。

设计上的三个取舍：

1. **不做会话接管**。参考项目开局后会接管群里所有消息（`session_waiter`），
   自然但会吞掉群聊、和别的插件抢消息。我们的房间以网页为主，群里是"顺手
   问一句"的补充通道，所以用显式指令，只在有房间时响应。
2. **@Bot 说话也算提问，但门控很严**：只有当前群确实有进行中的海龟汤时才接管，
   而且只认明确像提问 / 推理的句式和「提示」这个动作；模糊短句交回模型聊天。
   状态、复盘、揭晓等一律走 `/汤xxx` 明确指令，不靠整句关键词猜意图。
3. **指令失败要说人话**。没绑座位是最高频的失败，直接给绑定引导，
   而不是把 `PermissionError` 原文丢到群里。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .models import GameRoom
from .turtle_soup import VERDICT_LABELS, TurtleSoupGame

HELP_TEXT = (
    "🥣 海龟汤群内指令（/ 是 AstrBot 的指令前缀，带不带都行）\n"
    "• /汤 —— 开一局（也可写 汤 开局 困难：娱乐 / 简单 / 普通 / 困难）\n"
    "• /汤提问 你的问题 —— 问一个是/否问题\n"
    "• /汤推理 你的推理 —— 提交对汤底的完整推断（/汤验证 同义）\n"
    "• /汤提示 —— 要一条提示，消耗一次机会（写「汤 提示」也行）\n"
    "• /汤状态 —— 看当前进度（已问次数、提示与推理剩余）\n"
    "• /汤查看 —— 看最近几条问答\n"
    "• /汤揭晓 —— 发起揭晓汤底；/汤附议、/汤反对 参与表决\n"
    "• /汤复盘 —— 结束后看完整回顾（汤面 + 汤底 + 关键问答）\n"
    "• /汤帮助 —— 再看这份菜单\n"
    "对局进行中也可以直接 @我 问一句、交一段推理，或者说「提示」。\n"
    "群里没有进行中的海龟汤时，@我 就是普通聊天，不会被游戏接管。"
)

# 动作词按长度倒序匹配，避免「看答案」被「答案」抢先切开。
_ACTIONS: dict[str, str] = {
    "开局": "start",
    "开房": "start",
    "开始": "start",
    "提问": "question",
    "问一下": "question",
    "推理": "answer",
    "验证": "answer",
    "答题": "answer",
    "答案": "answer",
    "看答案": "reveal",
    "揭晓": "reveal",
    "附议": "agree",
    "同意": "agree",
    "反对": "disagree",
    "提示": "hint",
    "复盘": "recap",
    "回顾": "recap",
    "战报": "recap",
    "状态": "status",
    "查看": "history",
    "帮助": "help",
    "菜单": "help",
}
_ACTION_WORDS = sorted(_ACTIONS, key=len, reverse=True)

# 直接注册给 AstrBot 的明确指令词（和「棋类日报」一样用 @filter.command）。
# `/` 是 AstrBot 的默认指令前缀，不是指令名的一部分：注册「汤状态」后发 `/汤状态` 即可。
# 这些词只由指令系统精确匹配，不参与 @Bot 的整句意图猜测，不会吃掉普通聊天。
SOUP_EXPLICIT_COMMANDS: tuple[str, ...] = (
    "汤开局",
    "汤状态",
    "汤帮助",
    "汤复盘",
    "汤提问",
    "汤推理",
    "汤验证",
    "汤提示",
    "汤查看",
    "汤揭晓",
    "汤附议",
    "汤反对",
)

_PREFIX_RE = re.compile(r"^\s*[/／!！]\s*")
_NO_ROOM_ACTIONS = frozenset(
    {
        "question",
        "answer",
        "hint",
        "reveal",
        "agree",
        "disagree",
        "status",
        "history",
        "recap",
    }
)
# 看进度、看历史、看复盘、看帮助不需要座位，观众也能用
_NO_SEAT_ACTIONS = frozenset({"status", "history", "help", "recap"})


def parse_soup_command(message: str) -> tuple[str, str] | None:
    """解析「汤 xxx」指令，返回 (动作, 正文)；不是海龟汤指令返回 None。

    「汤」是单字，群里说「汤圆好吃」也会撞上前缀，所以要求「汤」后面必须是
    空白或已知动作词——否则整句当普通聊天放过。
    """
    text = _PREFIX_RE.sub("", _strip_at_prefix(str(message or ""))).strip()
    if text.startswith("海龟汤"):
        rest = text[len("海龟汤") :]
        # 和「汤」一样要求后面是空白或已知动作词，否则「海龟汤真好玩」会被
        # 当成一次提问，白扣一轮额度
        if rest and not rest[0].isspace() and not any(
            rest.startswith(word) for word in _ACTION_WORDS
        ):
            return None
    elif text.startswith("汤"):
        rest = text[1:]
        if rest and not rest[0].isspace() and not any(
            rest.startswith(word) for word in _ACTION_WORDS
        ):
            return None
    else:
        return None
    rest = rest.lstrip(" ：:，,")
    if not rest:
        # 一句「汤」直接开局，不再让玩家先背「汤 开局」这个写法
        return ("start", "")
    for word in _ACTION_WORDS:
        if rest.startswith(word):
            return (_ACTIONS[word], rest[len(word) :].lstrip(" ：:，,"))
    # 「汤 是灯塔吗」这种没说动作的，按提问处理：群里最常用的就是直接问
    return ("question", rest)


def soup_room_for_session(plugin: Any, session_id: str) -> GameRoom | None:
    """当前会话里进行中的海龟汤房间（没有就是 None）。"""
    for room in plugin.manager.for_session(session_id):
        if room.game_type != "turtle_soup":
            continue
        if room.status in {"active", "paused"}:
            return room
    return None


def soup_latest_room_for_session(plugin: Any, session_id: str) -> GameRoom | None:
    """当前会话里最近的一局海龟汤，含已结束的房间。

    只有「复盘」用它——刚打完想问一句「刚才汤底是什么」，那时候房间已经结束，
    `soup_room_for_session` 会返回 None。
    """
    rooms = [
        room
        for room in plugin.manager.for_session(session_id)
        if room.game_type == "turtle_soup"
    ]
    if not rooms:
        return None
    active = [room for room in rooms if room.status in {"active", "paused"}]
    pool = active or rooms
    return max(pool, key=lambda room: float(getattr(room, "created_at", 0) or 0))


def _mark_active(room: GameRoom, qq: str) -> None:
    """群里说话说明人在，更新活跃时间，免得被「在线人数」节奏算成缺席。

    同时清掉 `left_at`：群里玩的人可能刚才顺手关了网页，人其实还在群里，
    不清这个标记就会被当成「关页面走人」移出玩家席。

    但**不能**把 `connected` 改回 True：在群里发言只证明人还在群里，不证明
    网页还开着。以前这里会把早已关页的访客一直「复活」，房间回收时误判
    「还有真人在线」，结算后挂着的房间就被拖到 30 分钟宽限才销毁。
    """
    if not qq or not room.multiplayer.enabled:
        return
    seat = room.multiplayer.seat_for_qq(qq)
    if seat is None:
        return
    visitor = room.visitors.get(seat.visitor_token)
    if visitor is None:
        return
    visitor.last_seen_at = time.time()
    visitor.left_at = None


def _progress_tail(result: dict[str, Any]) -> str:
    """把「已问 N/M · 提示剩 K · 推理剩 J」拼成一句尾巴。

    群里以前只回一句判定，玩家不知道自己问到第几条、还剩几次，人一多就会
    一直问下去。数字由插件统一算好（`_soup_progress`），网页和群里永远一致。
    推理额度只在快用完时才显得重要，所以一直显示（条数很少，不占地方）。
    """
    asked = result.get("question_count")
    limit = result.get("question_limit")
    hints = result.get("hint_left")
    answer_left = result.get("answer_left")
    parts: list[str] = []
    if isinstance(asked, int):
        if isinstance(limit, int) and limit > 0:
            parts.append(f"已问 {asked}/{limit}")
        else:
            parts.append(f"已问 {asked} 次")
    if isinstance(hints, int):
        parts.append(f"提示剩 {hints}")
    if isinstance(answer_left, int):
        parts.append(f"推理剩 {answer_left}")
    return f"（{' · '.join(parts)}）" if parts else ""


def _status_text(plugin: Any, room: GameRoom, game: TurtleSoupGame | None) -> str:
    if game is None or game.puzzle is None:
        return "🥣 海龟汤正在准备题目，等小会儿再试。"
    title = game.puzzle.title or "本局题目"
    hints_left = int(getattr(game, "hint_left", 0))
    # 次数跟难度走：同一进程里不同房间难度不同，必须按房间取，不能用插件级配置
    if hasattr(plugin, "_soup_limits"):
        limits = plugin._soup_limits(room)
    else:  # 兼容极简桩对象（测试里只给了部分属性）
        limits = {
            "questions": int(getattr(plugin, "turtle_soup_max_questions", 15) or 0),
            "answers": int(getattr(plugin, "turtle_soup_max_answers", 0) or 0),
        }
    limit = int(limits.get("questions", 0) or 0)
    # 只算玩家问的（花火自己的提问不吃玩家额度）
    mine = int(getattr(game, "player_question_count", game.question_count))
    asked = f"{mine} 次"
    if limit:
        asked = f"{mine}/{limit} 次（剩 {max(0, limit - mine)}）"
    answer_limit = int(limits.get("answers", 0) or 0)
    used_answers = int(getattr(game, "answer_attempts", 0) or 0)
    answer_text = f"推理已交 {used_answers} 次"
    if answer_limit:
        answer_text = (
            f"推理已交 {used_answers}/{answer_limit} 次（剩 {max(0, answer_limit - used_answers)}）"
        )
    lines = [
        f"🥣 海龟汤 · {title}",
        f"难度：{_difficulty_cn(room.difficulty)} · 已提问 {asked} · 提示剩 {hints_left} 次",
        answer_text,
    ]
    if room.status == "paused":
        lines.append("当前已暂停，继续后才能在群里提问。")
    if room.multiplayer.enabled:
        free = bool(getattr(plugin.manager, "turtle_soup_free_questioning", False))
        current = room.multiplayer.current_token
        seat = room.multiplayer.seat_for_token(current) if current else None
        if free:
            lines.append("自由提问：玩家席谁都可以问，不用等轮次。")
        elif seat is not None:
            lines.append(f"现在轮到 {seat.number} 号玩家。")
    else:
        lines.append("这是单人房间，你随时可以提问。")
    return "\n".join(lines)


def _recap_text(room: GameRoom, game: TurtleSoupGame | None) -> str:
    """群内复盘：结束后给汤面 + 汤底 + 关键问答；进行中只给进度，不剧透。

    以前群里没有这个入口，`汤 复盘` 会被兜底成一次提问，白白消耗一次次数。
    """
    if game is None or game.puzzle is None:
        return "🥣 这一局还没出题，等题目准备好再试。"
    puzzle = game.puzzle
    ended = bool(game.finished) or str(room.status) in {"finished", "rematch_pending"}
    lines = [f"🥣 复盘 · {puzzle.title}", f"汤面：{puzzle.surface}"]
    if ended:
        lines.append(f"汤底：{puzzle.solution}")
    else:
        lines.append("（本局还没结束，汤底先不给）")
    lines.append(f"已问 {game.question_count} 次 · 提示剩 {game.hint_left} 次")

    entries = [
        entry for entry in game.entries if entry.kind in {"question", "answer"}
    ]
    if entries:
        # 群里回看问答就是为了知道别人问了啥：原文完整列出，不脱敏也不按字数砍
        lines.append(f"本局问答（{len(entries)} 条）：")
        for entry in entries:
            who = f"{entry.player_number} 号" if entry.player_number else "玩家"
            mark = "问" if entry.kind == "question" else "推理"
            prompt = re.sub(r"\s+", " ", str(entry.prompt or "")).strip()
            reply = re.sub(r"\s+", " ", str(entry.response or "")).strip()
            lines.append(f"· {who}{mark}：{prompt} → {reply or '已记录'}")
    if not ended:
        lines.append("（本局结束后再发一次「汤 复盘」可以看完整汤底）")
    return "\n".join(lines)


def _history_text(room: GameRoom, game: TurtleSoupGame | None) -> str:
    if game is None or game.puzzle is None:
        return "还没有可查看的问答。"
    items = [
        entry
        for entry in game.entries
        if entry.kind in {"question", "answer"} and entry.response
    ]
    if not items:
        return "还没有人提问。"
    # 全部列出、原文照发：以前只给最近 5 条还把贴着汤底的片段遮成「……」，
    # 群里根本拼不出网页玩家到底问了什么。
    lines = [f"🥣 本局全部问答（{len(items)} 条）："]
    for entry in items:
        prompt = re.sub(r"\s+", " ", str(entry.prompt or "")).strip()
        reply = re.sub(r"\s+", " ", str(entry.response or "")).strip()
        who = f"{entry.player_number} 号" if entry.player_number else "玩家"
        mark = "问" if entry.kind == "question" else "推理"
        lines.append(f"· {who}{mark}：{prompt} —— {reply}")
    return "\n".join(lines)


def _question_reply(game: TurtleSoupGame | None, body: str, result: dict[str, Any]) -> str:
    if result.get("solved"):
        solution = str(result.get("solution") or "").strip()
        return f"🎉 推理正确，汤底揭晓：\n{solution}" if solution else "🎉 推理正确，汤底已揭晓。"
    if result.get("limited"):
        return f"⏳ {result.get('reply') or '本局提问次数已经用完'}{_progress_tail(result)}"
    if result.get("action") == "awaiting_host":
        return f"问题已提交，等出题人回答。{_progress_tail(result)}"
    verdict = str(result.get("verdict") or "")
    return f"🙋 {body}\n💡 {VERDICT_LABELS.get(verdict, result.get('reply') or '已记录')}{_progress_tail(result)}"


def _answer_reply(result: dict[str, Any]) -> str:
    if result.get("solved"):
        solution = str(result.get("solution") or "").strip()
        return f"🎉 推理正确，汤底揭晓：\n{solution}" if solution else "🎉 推理正确，汤底已揭晓。"
    if result.get("answer_limit_reached"):
        # 这局推理机会用完、也没人答对：manager 那边已经揭晓，这里直接把汤底给出来
        solution = str(result.get("solution") or "").strip()
        head = "⏳ 本局推理机会已经用完，没人答对。"
        return f"{head}\n汤底揭晓：\n{solution}" if solution else head + "汤底已揭晓。"
    if result.get("action") == "awaiting_host":
        return f"推理已提交，等出题人判断。{_progress_tail(result)}"
    coverage = result.get("coverage")
    reply = str(result.get("reply") or "还没有完全对上")
    if isinstance(coverage, (int, float)):
        return f"{reply}（覆盖度 {round(float(coverage) * 100)}%）{_progress_tail(result)}"
    return f"{reply}{_progress_tail(result)}"


async def _dispatch(
    plugin: Any, room: GameRoom, action: str, body: str, qq: str
) -> str:
    game = plugin.manager._turtle_soup_game(room)
    if action == "help":
        return HELP_TEXT
    if action == "status":
        return _status_text(plugin, room, game)
    if action == "history":
        return _history_text(room, game)
    if action == "recap":
        return _recap_text(room, game)
    if action == "hint":
        hint = await plugin.manager.request_turtle_soup_hint(
            room, source="qq", actor_qq=qq
        )
        return f"💡 {hint}"
    if action == "reveal":
        result = await plugin.manager.request_soup_reveal(
            room, source="qq", actor_qq=qq
        )
        if result.get("revealed"):
            solution = (game.puzzle.solution if game and game.puzzle else "").strip()
            return f"🥣 汤底揭晓：\n{solution}" if solution else "🥣 汤底已揭晓。"
        need = int(result.get("required") or 1)
        votes = int(result.get("votes") or 0)
        return (
            f"已发起揭晓（{votes}/{need}），还需要 {max(0, need - votes)} 人同意。"
            "同意的玩家发「汤 附议」，表决期间照常提问。"
        )
    if action in {"agree", "disagree"}:
        result = await plugin.manager.vote_soup_reveal(
            room, agree=action == "agree", source="qq", actor_qq=qq
        )
        if result.get("revealed"):
            solution = (game.puzzle.solution if game and game.puzzle else "").strip()
            return f"🥣 票数够了，汤底揭晓：\n{solution}" if solution else "🥣 票数够了，汤底揭晓。"
        need = int(result.get("required") or 1)
        votes = int(result.get("votes") or 0)
        return f"已记录你的意见（{votes}/{need}）。"
    if action in {"question", "answer"}:
        if not body:
            return "要提交什么内容？例如：汤 提问 是不是和灯塔有关"
        if action == "question":
            # 玩家出题、花火猜的玩法里，玩家发的「提问」其实是给花火的线索
            if game is not None and game.mode == "player_host" and not game.host_number:
                turn = await plugin.submit_reverse_turtle_soup_turn(
                    room, body, source="qq", actor_qq=qq
                )
                return f"🥣 {turn.get('reply') or '花火还在想…'}"
            result = await plugin.submit_turtle_soup_question(
                room, body, source="qq", actor_qq=qq
            )
            return _question_reply(game, body, result)
        result = await plugin.submit_turtle_soup_answer(
            room, body, source="qq", actor_qq=qq
        )
        return _answer_reply(result)
    return HELP_TEXT


async def run_soup_action(
    plugin: Any, room: GameRoom, action: str, body: str, qq: str, *, actor_name: str = ""
) -> str:
    """按动作执行一次群聊操作（指令入口、@ 入口与 LLM 工具共用）。

    统一在这里做「自动入座 + 活跃刷新 + 异常翻译」：
    * 群里说话的人还没绑座位时自动替他占一个（`turtle_soup.qq_auto_bind`），
      否则第一次玩要先跑去网页拿令牌，多数人到这一步就放弃了；
    * 未绑定座位是原来的最高频失败，现在只剩「座位满了」这类真没法办的情况，
      也要说人话；
    * 其余错误转成一句可读的话，不把异常原文丢给用户。
    """
    prefix = ""
    if action not in _NO_SEAT_ACTIONS:
        ok, note = await ensure_qq_seat(plugin, room, qq, actor_name)
        if not ok:
            return note
        prefix = note
    _mark_active(room, qq)
    try:
        text = await _dispatch(plugin, room, action, body, qq)
    except PermissionError as exc:
        return (
            f"{exc}\n"
            "（QQ 要先绑定玩家席：在网页里取一次性令牌，然后在群里发「绑定玩家 令牌」）"
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return f"操作失败：{exc}"
    return f"{prefix}\n{text}" if prefix else text


_DIFFICULTY_ALIASES: dict[str, str] = {
    "娱乐": "entertainment",
    "休闲": "entertainment",
    "放松": "entertainment",
    "entertainment": "entertainment",
    "简单": "easy",
    "容易": "easy",
    "easy": "easy",
    "普通": "normal",
    "一般": "normal",
    "normal": "normal",
    "困难": "hard",
    "难": "hard",
    "hard": "hard",
}

_DIFFICULTY_LABELS: dict[str, str] = {
    "entertainment": "娱乐",
    "easy": "简单",
    "normal": "普通",
    "hard": "困难",
}


def _difficulty_cn(difficulty: str | None) -> str:
    return _DIFFICULTY_LABELS.get(str(difficulty or "").strip().lower(), "普通")


async def _seat_and_start(plugin: Any, room: GameRoom, event: Any) -> str:
    """把开局的人安排进玩家席并立刻发题，返回一句说明。"""
    qq = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
    name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
    ok, note = await ensure_qq_seat(plugin, room, qq, name)
    if not ok:
        return f"房间已经建好，但没能把你安排进玩家席：{note}"
    token = ""
    if room.multiplayer.enabled:
        seat = room.multiplayer.seat_for_qq(qq)
        token = seat.visitor_token if seat is not None else ""
    token = token or room.player_token
    if room.status != "active" and token:
        try:
            await plugin.manager.start_game(room, token, "")
        except (ValueError, RuntimeError, OSError) as exc:
            return f"你已经在玩家席了，但自动开局失败：{exc}。可以在网页里点「开始」。"
    return (
        "已经把你排进玩家席并开局，选一种方式就能玩：\n"
        "· 汤 提问 你的问题\n"
        "· @我 问一句\n"
        "· @我 交一段推理"
    )


async def start_soup_room(plugin: Any, event: Any, body: str) -> str:
    """「汤 开局」：走确定性建房路径，建好就把人安排进座位并开局。

    为什么不交给模型：群里说「/汤」和「汤 开局」应该得到同一个结果，
    但模型那条路要过意图校验，还可能被上下文带偏。这里直接调建房逻辑，
    群聊建房权限（只有游戏管理员 / 已放开非管理员建房）完全复用同一套判断。
    """
    session_id = str(getattr(event, "unified_msg_origin", "") or "")
    if soup_room_for_session(plugin, session_id) is not None:
        return "当前会话已经有一局在玩了。发「汤 状态」看进度；想重开就先让这局结束。"
    difficulty = _DIFFICULTY_ALIASES.get(str(body or "").strip().lower(), "")
    if not difficulty:
        # 没写难度就用配置里的默认难度（海龟汤四档，走 _soup_difficulty；
        # 极简桩对象没有这个方法时退回三档解析）
        resolver = getattr(plugin, "_soup_difficulty", None)
        difficulty = resolver("") if callable(resolver) else plugin._difficulty(body)
    try:
        room, _reused, _same, _switched = await plugin._create_or_reuse_room_from_event(
            event, difficulty, "turtle_soup", confirm_abandon=False
        )
    except PermissionError as exc:
        return f"开不了房：{exc}"
    except (ValueError, RuntimeError, OSError) as exc:
        return f"开不了房：{exc}"

    note = await _seat_and_start(plugin, room, event)
    # 标题、链接、玩法各自成段：链接很长，跟标题挤一行在手机上会折得很难读，
    # 三种玩法挤在一句话里也分不清。链接单独一行方便直接点开。
    return (
        "🥣 海龟汤开好了\n\n"
        f"🔗 {plugin._room_url(room)}\n\n"
        f"{note}"
    )


async def handle_soup_command(plugin: Any, event: Any) -> str:
    """处理一条群消息；不需要响应时返回空串（让消息继续走正常聊天）。"""
    parsed = parse_soup_command(getattr(event, "message_str", "") or "")
    if parsed is None:
        return ""
    action, body = parsed
    session_id = str(getattr(event, "unified_msg_origin", "") or "")
    qq = str(event.get_sender_id() or "").strip()

    if action == "start":
        return await start_soup_room(plugin, event, body)

    room = soup_room_for_session(plugin, session_id)
    if room is None and action == "recap":
        # 复盘要能在「刚打完」用，那时候房间已经不在进行中了
        room = soup_latest_room_for_session(plugin, session_id)
    if room is None:
        if action == "help":
            return HELP_TEXT
        if action in _NO_ROOM_ACTIONS:
            return (
                "当前会话还没有进行中的海龟汤。"
                "发「汤 开局」直接开一局，或者先在网页开好再回来提问。"
            )
        return ""

    return await run_soup_action(
        plugin,
        room,
        action,
        body,
        qq,
        actor_name=str(getattr(event, "get_sender_name", lambda: "")() or "").strip(),
    )

# ---------------- @ 花火 直接说话（不用指令） ----------------

# 纯寒暄与应答：这些不该变成一次提问，否则群里一句「谢谢」也会被问出去。
_AT_GREETINGS: frozenset[str] = frozenset(
    {
        "你好",
        "您好",
        "在吗",
        "在不在",
        "嗨",
        "hi",
        "hello",
        "哈喽",
        "谢谢",
        "谢谢你",
        "多谢",
        "好的",
        "好",
        "行",
        "嗯",
        "嗯嗯",
        "收到",
        "辛苦了",
        "晚安",
        "早安",
        "早上好",
        "晚上好",
        "哈哈",
        "哈哈哈",
        "笑死",
        "666",
        "牛",
        "厉害",
        "随便",
        "算了",
        "没事",
        "没事了",
    }
)
_AT_GREETINGS_LOWER = frozenset(word.lower() for word in _AT_GREETINGS)

# 短句里带这些词八成是搭话而不是提问（「谢谢花火」「哈哈哈哈」）。
_AT_GREETING_PARTS: tuple[str, ...] = (
    "谢谢",
    "多谢",
    "辛苦",
    "哈哈",
    "笑死",
    "晚安",
    "早安",
    "早上好",
    "晚上好",
    "在吗",
    "在不在",
    "你好",
    "您好",
    "收到",
    "嗯嗯",
    "666",
)
_AT_SHORT_CHARS = 6

# 疑问特征：命中就当提问。提问判错的代价只是多问一句，
# 而推理判错会推进覆盖率甚至直接揭晓，所以宁可把模糊的句子当提问。
_AT_QUESTION_MARKERS: tuple[str, ...] = (
    "吗",
    "？",
    "?",
    "是不是",
    "是否",
    "有没有",
    "为什么",
    "怎么",
    "哪里",
    "哪儿",
    "谁",
    "多少",
    "几个",
    "多久",
    "能不能",
    "可不可以",
    "会不会",
)

# 明确的推理措辞：即使句子不长，也可以当推理提交。
_AT_ANSWER_HINTS: tuple[str, ...] = (
    "我觉得",
    "我猜",
    "我认为",
    "我怀疑",
    "我的推理",
    "答案是",
    "真相是",
    "他其实",
    "她其实",
    "所以整个",
    "故事是",
)
# 陈述句多长才敢当推理提交。太短的陈述（「灯灭了」）更像感叹，按提问处理。
_MIN_ANSWER_CHARS = 12

_AT_PREFIX_RE = re.compile(r"^(?:\s*\[CQ:at[^\]]*\]|\s*@\S+)\s*")


def _strip_at_prefix(text: str) -> str:
    return _AT_PREFIX_RE.sub("", str(text or "")).strip()


# @Bot 的整句动作**只保留「要提示」**：这是玩汤时最自然的口语动作，
# 而且就算判错也只消耗一次提示额度。
# 状态 / 复盘 / 揭晓 / 开局 / 帮助 / 查看 这些一律走 `/汤状态` 那样的明确指令，
# 不再靠一大堆整句关键词猜意图——否则群里有人 @ 模型说「答案」「状态」「帮助」
# 也会被当成游戏指令，把正常聊天吃掉。
# 注意：无论哪种动作，都只在当前群确实有进行中的海龟汤房间时才生效
# （门控在 handle_soup_at_message 最前面）。
_AT_INTENTS: dict[str, str] = {
    "提示": "hint",
    "给个提示": "hint",
    "给点提示": "hint",
    "提示一下": "hint",
    "来点提示": "hint",
    "来个提示": "hint",
    "要个提示": "hint",
    "求提示": "hint",
    "下一条提示": "hint",
    "再来个提示": "hint",
    "换个提示": "hint",
}
_AT_PUNCT_RE = re.compile(
    r"[\s，。！？!?、,.~～…·:：;；\"'“”‘’()（）\[\]【】<>《》]"
)


def classify_at_intent(text: str) -> tuple[str, str] | None:
    """把「@Bot 的整句话」判成提示动作；其余一律返回 None。

    只做整句匹配：`@我 提示` → hint；`@我 提示是什么意思` 走正常判定或闲聊。
    这里**只认提示**，别的动作请用明确指令，避免把普通聊天词当成指令。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return None
    bare = _AT_PUNCT_RE.sub("", normalized).lower()
    action = _AT_INTENTS.get(bare)
    if action is None:
        return None
    return (action, "")


def classify_at_message(text: str) -> str | None:
    """把「@Bot 说的一句话」判成 question / answer；不明确就交回正常聊天。

    只认**明确像提问或作答**的内容：带疑问特征（问号、吗、是不是、为什么……）
    算提问；够长的陈述句、或带「我觉得 / 答案是」这类措辞算推理提交，
    所以「@我 他是灯塔管理员，灯灭了导致沉船」依然能直接当推理。
    短句、感叹、搭话不再一律兜底成提问，免得群里 @ 模型聊天被吃掉。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return None
    bare = re.sub(
        r"[\s，。！？!?、,.~～…·:：;；\"'“”‘’()（）\[\]【】<>《》]",
        "",
        normalized,
    )
    if not bare:
        return None
    if bare.lower() in _AT_GREETINGS_LOWER:
        return None
    # 短短一句又带着搭话的词，当作招呼而不是提问
    if len(bare) <= _AT_SHORT_CHARS and any(
        part in bare for part in _AT_GREETING_PARTS
    ):
        return None
    if any(marker in normalized for marker in _AT_QUESTION_MARKERS):
        return "question"
    if len(bare) >= _MIN_ANSWER_CHARS or any(
        hint in normalized for hint in _AT_ANSWER_HINTS
    ):
        return "answer"
    # 不明确就不接管：交回模型正常聊天
    return None


def _qq_has_seat(room: GameRoom, qq: str) -> bool:
    if not qq:
        return False
    if room.multiplayer.enabled:
        return room.multiplayer.seat_for_qq(qq) is not None
    return bool(getattr(room, "player_qq", "")) and str(room.player_qq) == qq


async def ensure_qq_seat(
    plugin: Any, room: GameRoom, qq: str, display_name: str = ""
) -> tuple[bool, str]:
    """让群里说话的 QQ 坐进玩家席，返回 (是否可用, 需要前置说明的文案)。

    为什么不要求先去网页拿令牌：群里玩到一半被「你还没绑定」挡回去，多数人
    到这一步就放弃了。有空座位就直接让他坐下，网页端会看到一条「从群里自动
    加入玩家席」的系统消息，谁进来了是一目了然的。

    座位满了 / 单人房已经有人在玩时才回退到绑定引导——那两种是真没法自动安排。
    """
    if not qq:
        return False, "拿不到你的 QQ 号，先在网页绑定玩家席再用指令。"
    if _qq_has_seat(room, qq):
        return True, ""
    if not getattr(plugin, "turtle_soup_qq_auto_bind", True):
        return False, (
            "你还没绑定玩家席：在网页里取一次性令牌，然后在群里发「绑定玩家 令牌」。"
        )
    if room.status not in {"active", "paused", "setup", "waiting"}:
        return False, "这一局已经结束了，等下一局再进来。"

    if room.multiplayer.enabled:
        capacity = int(room.multiplayer.capacity or 0)
        if capacity and len(room.multiplayer.seats) >= capacity:
            return False, f"玩家席已经坐满（{capacity} 人），先在网页当观众吧。"
    elif room.player_qq and str(room.player_qq) != qq:
        return False, "这是单人房间，已经有玩家在玩了。"

    status_before = room.status
    try:
        visitor = await plugin.manager.join(room)
        if display_name:
            visitor.display_name = display_name
        await plugin.manager.assign_player(
            room, visitor.number, qq, allow_non_numeric=True
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        return False, f"自动加入玩家席失败：{exc}"
    # 极端情况下 assign_player 会把进行中的房间打回 setup，这里兜回来
    if status_before == "active" and room.status != "active":
        room.status = status_before
    room.add_message(
        "system",
        f"{display_name or qq} 从群里自动加入玩家席（{visitor.number} 号）。",
    )
    return True, f"（已把你加入玩家席：{visitor.number} 号）"


async def handle_soup_at_message(plugin: Any, event: Any) -> str:
    """有进行中的海龟汤时，才把 @Bot 的明确提问 / 推理 / 「提示」接到这一局。

    **门控放在最前面**：当前会话没有进行中的海龟汤房间就一个字都不接管，
    @Bot 照常走模型聊天。以前「@我 来局海龟汤」「@我 帮助」在没房间时也会
    凭空开局或弹菜单，群里只想跟模型说话的人全被当成指令。

    状态 / 复盘 / 揭晓 / 查看 / 开局 等动作请用 `/汤状态` 这类明确指令；
    这里只留「提示」和明确像提问、作答的句式。
    """
    session_id = str(getattr(event, "unified_msg_origin", "") or "")
    text = _strip_at_prefix(str(getattr(event, "message_str", "") or ""))
    # 已经写成指令的交给指令入口，避免两个入口各回一条
    if parse_soup_command(text) is not None:
        return ""
    room = soup_room_for_session(plugin, session_id)
    if room is None:
        return ""
    qq = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
    actor_name = str(getattr(event, "get_sender_name", lambda: "")() or "").strip()
    intent = classify_at_intent(text)
    if intent is not None:
        action, body = intent
        return await run_soup_action(
            plugin, room, action, body, qq, actor_name=actor_name
        )
    action = classify_at_message(text)
    if action is None:
        return ""
    # 没绑座位的人在这里自动入座（座位满了才回退成绑定引导）
    return await run_soup_action(
        plugin, room, action, text, qq, actor_name=actor_name
    )
