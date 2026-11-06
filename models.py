from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from .blackjack import BlackjackGame
from .draw_guess import DrawGuessGame
from .gomoku import Difficulty, GomokuGame
from .othello import OthelloGame
from .pig_dice import PigDiceGame
from .tictactoe import TicTacToeGame
from .turtle_soup import TurtleSoupGame
from .undercover import UndercoverGame
from .weiqi import WeiqiGame
from .xiangqi import XiangqiGame

RoomSource = Literal["group", "private"]

def _visitor_uid(token: str) -> str:
    """访客的公开稳定标识：够短、不可逆、座位重编号也不会变。

    前端用它做「谁进谁出」的差量判断——`number` 会随入座重排（seat.number 1..N）
    并且可能与观众号重复（花火座位就是 2 号），不能当身份用。
    """
    token = str(token or "")
    if not token:
        # 空 token（AI 座位）返回空串：前端用「有没有 uid」判断该座位是否有人
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


GameType = Literal[
    "gomoku", "xiangqi", "weiqi", "tictactoe", "othello", "turtle_soup",
    "pig_dice", "draw_guess", "blackjack", "undercover",
]
# 有「棋力档位」的玩法：管理台允许在这些房间上直接切换 easy/normal/hard。
DIFFICULTY_GAME_TYPES: frozenset[str] = frozenset(
    {"gomoku", "othello", "xiangqi", "weiqi", "tictactoe", "pig_dice", "blackjack"}
)
RoomStatus = Literal[
    "waiting", "setup", "active", "finished", "rematch_pending", "paused", "closed"
]
TurtleSoupMode = Literal["bot_host", "player_host"]


@dataclass(slots=True)
class GameScore:
    completed: int = 0
    human_wins: int = 0
    bot_wins: int = 0
    draws: int = 0


@dataclass(slots=True)
class TurtleSoupStats:
    questions: int = 0
    hints: int = 0
    answer_attempts: int = 0


@dataclass(slots=True)
class PlayerSeat:
    """A browser visitor occupying one reusable multiplayer seat."""

    number: int = 0
    visitor_token: str = ""
    qq: str = ""
    display_name: str = ""
    identity_confirmed: bool = False
    seated_at: float = field(default_factory=time.time)
    # 最后一次「有效操作」时间（入座、落子、选边、再来一局）。
    # 心跳不刷新它：用来识别「页面还开着但人已经走了」的挂机占座，
    # 这种人在服务端眼里永远在线，靠 left_at / 心跳都清不掉。
    last_action_at: float = field(default_factory=time.time)
    is_ai: bool = False
    # 两人对弈类玩法的执色（"black" / "white"）；其余玩法留空。
    # 由座位顺序决定，再来一局时可整体交换，用于实现先后手轮换。
    side: str = ""
    ready: bool = False
    # 谁是卧底「藏品/护身符」徽章缓存（若 room.camp_wins_store 存在则以实时计算为准）
    undercover_badges: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class SeatSwapRequest:
    """A short-lived request from one spectator to one occupied seat."""

    request_id: str
    requester_token: str
    target_token: str
    created_at: float
    expires_at: float


@dataclass(slots=True)
class GameVoteRequest:
    """房间内「投票换游戏」的一次提案（同一时间只允许一条）。

    voter_base 是**创建时锁定的在线人数**：中途有人刷新或离开不会把门槛抬高或压低，
    否则票数可能被「分母抖动」卡死或意外通过。
    """

    request_id: str
    proposer_token: str
    target_game: str
    created_at: float
    expires_at: float
    voter_base: int = 1
    # 通过门槛：创建时按 voter_base 算好并存下，避免每次读快照重算口径不一致
    required: int = 1
    # visitor_token -> 是否赞成（发起人默认赞成）
    votes: dict[str, bool] = field(default_factory=dict)

    def counts(self) -> tuple[int, int]:
        """返回 (赞成票, 反对票)。"""
        in_favor = sum(1 for approved in self.votes.values() if approved)
        against = sum(1 for approved in self.votes.values() if not approved)
        return in_favor, against


@dataclass(slots=True)
class MultiplayerState:
    """Game-agnostic seats, round order and spectator swap requests."""

    enabled: bool = False
    capacity: int = 1
    turn_timeout_seconds: int = 0
    swap_cooldown_seconds: int = 0
    swap_request_expiry_seconds: int = 20
    seats: list[PlayerSeat] = field(default_factory=list)
    current_turn_index: int = 0
    turn_deadline: float = 0.0
    # 棋类对弈：轮到的真人离线时先冻结棋盘等重连，超过宽限再判负
    duel_paused: bool = False
    duel_pause_deadline: float = 0.0
    swap_requests: dict[str, SeatSwapRequest] = field(default_factory=dict)
    last_swap_request_at: dict[str, float] = field(default_factory=dict)

    @property
    def current_token(self) -> str:
        if not self.seats:
            return ""
        self.current_turn_index %= len(self.seats)
        return self.seats[self.current_turn_index].visitor_token

    def seat_for_token(self, token: str) -> PlayerSeat | None:
        return next(
            (seat for seat in self.seats if seat.visitor_token == str(token or "")),
            None,
        )

    def seat_for_qq(self, qq: str) -> PlayerSeat | None:
        return next(
            (
                seat
                for seat in self.seats
                if seat.identity_confirmed and seat.qq and seat.qq == str(qq or "")
            ),
            None,
        )


def _qq_avatar_url(qq: str) -> str:
    """由 QQ 号拼出官方头像地址；空/非法 QQ 号返回空串。"""
    qq = str(qq or "").strip()
    if not qq.isdigit() or len(qq) < 5:
        return ""
    return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"


def _undercover_identity_key(seat: "PlayerSeat") -> tuple[str, str]:
    """返回 (分阵营胜场存储键, 显示名)。

    已绑定 QQ 的真人用 `qq:xxx` 作稳定键：改 QQ 昵称/换设备都不丢胜场与徽章；
    AI / 未绑定玩家按名字存（`name:xxx`）。
    """
    qq = str(getattr(seat, "qq", "") or "").strip()
    name = (getattr(seat, "display_name", "") or "").strip()
    if qq and getattr(seat, "identity_confirmed", False):
        return f"qq:{qq}", name
    return f"name:{name or '?'}", name


def soup_reveal_required(room: "GameRoom") -> int:
    """海龟汤「揭晓汤底」需要多少人同意：在席真人过半，单人房直接放行。

    这里不把 AI 座位算进分母：花火是出题方，不该参与「要不要看答案」的表决。
    """
    state = room.multiplayer
    if not state.enabled:
        return 1
    people = sum(
        1
        for seat in state.seats
        if not seat.is_ai and str(seat.visitor_token or "")
    )
    if people <= 1:
        return 1
    return people // 2 + 1


def _seat_badges(seat: "PlayerSeat", room: "GameRoom") -> list[dict[str, object]]:
    """按玩家身份（QQ 稳定键 / 名字）即时计算「藏品/护身符」徽章。

    若房间持有阵营胜场存储（room.camp_wins_store，引用管理器同一 dict），
    实时计算；否则回退到游戏中缓存的 seat.undercover_badges。
    """
    store = getattr(room, "camp_wins_store", None)
    if isinstance(store, dict):
        key, name = _undercover_identity_key(seat)
        return _undercover_badges_for(key, name, store)
    cached = getattr(seat, "undercover_badges", None)
    return list(cached) if isinstance(cached, list) else []


def _undercover_badges_for(
    key: str, name: str, stats: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """分阵营胜场的「藏品/护身符」徽章规则（与 room_manager 保持一致）。

    按稳定键读胜场（qq:xxx / name:xxx）；键下无数据时回退到旧版裸昵称数据的
    name: 前缀键，保证升级前已积累的胜场仍能点亮徽章。

    卧底/平民/白板各有专属阶梯，胜场越多点亮越高阶；同时按总胜场点亮一枚荣誉段位。
    只返回「当前已达成的最高阶」每阵营一枚，避免玩家卡被徽章刷屏。
    """
    stats = stats if isinstance(stats, dict) else {}
    entry = stats.get(key) or {}
    if not isinstance(entry, dict) or not entry:
        legacy = stats.get(f"name:{name}") if name else {}
        entry = legacy if isinstance(legacy, dict) else {}
    civ = int(entry.get("civilian", 0) or 0)
    uc = int(entry.get("undercover", 0) or 0)
    wb = int(entry.get("whiteboard", 0) or 0)
    total = civ + uc + wb

    def top(tiers, wins):
        chosen = None
        for threshold, label, emoji, tone in tiers:
            if wins >= threshold:
                chosen = {"id": f"{tone}_{threshold}", "label": label, "emoji": emoji, "tone": tone, "wins": wins}
        return chosen

    badges = []
    # 总胜场的荣誉段位（金色，只亮最高阶，最靠前）
    for threshold, label, emoji in (
        (1, "入局新手", "🎮"),
        (5, "常胜新人", "🟢"),
        (12, "老练玩家", "🔱"),
        (20, "高手玩家", "🏆"),
        (35, "卧底传奇", "👑"),
    ):
        if total >= threshold:
            badges = [{"id": f"gold_{threshold}", "label": label, "emoji": emoji, "tone": "gold", "wins": total}]
    # 三大阵营专属阶梯（每阵营只亮最高阶）
    civ_badge = top(
        (
            (1, "平民见习", "🤝", "civ"),
            (3, "平民守护者", "🛡️", "civ"),
            (8, "平民核心", "⚔️", "civ"),
            (15, "平民之神", "👑", "civ"),
        ),
        civ,
    )
    uc_badge = top(
        (
            (1, "卧底见习", "🕶️", "uc"),
            (3, "卧底刺客", "🕵️", "uc"),
            (7, "卧底大师", "🎩", "uc"),
            (12, "卧底影帝", "🎭", "uc"),
        ),
        uc,
    )
    wb_badge = top(
        (
            (1, "白板见习", "🌀", "wb"),
            (3, "白板棋士", "♟️", "wb"),
            (6, "白板策士", "📋", "wb"),
            (10, "白板幻影", "🫥", "wb"),
        ),
        wb,
    )
    for badge in (civ_badge, uc_badge, wb_badge):
        if badge is not None:
            badges.append(badge)
    return badges


@dataclass(slots=True)
class Visitor:
    """One browser identity inside a room."""

    number: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    joined_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    left_at: float | None = None
    connected: bool = True
    qq: str = ""
    display_name: str = ""
    identity_confirmed: bool = False
    binding_token: str = ""
    binding_expires_at: float = 0.0
    trusted_browser_enrollment_pending: bool = False
    last_chat_at: float = 0.0

    BINDING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def ensure_binding_token(self, *, now: float | None = None, ttl: int = 300) -> None:
        """Create a short-lived challenge only while this visitor is unbound."""
        if self.identity_confirmed:
            self.binding_token = ""
            self.binding_expires_at = 0.0
            return
        current = time.time() if now is None else float(now)
        if self.binding_token and self.binding_expires_at > current:
            return
        self.binding_token = "".join(
            secrets.choice(self.BINDING_ALPHABET) for _index in range(8)
        )
        self.binding_expires_at = current + max(1, int(ttl))

    def public_snapshot(
        self,
        *,
        is_player: bool,
        is_current_player: bool = False,
        is_ai: bool = False,
        include_qq_avatar: bool = False,
    ) -> dict[str, object]:
        """Return fields safe to show to all room members.

        QQ 头像地址里编码了 QQ 号，属于隐私数据：只有查看者本人已绑定身份
        （identity_confirmed）时才下发，匿名观众拿到的 visitor 快照不含头像。
        """
        return {
            "number": self.number,
            # 稳定身份（座位重编号 / 号码重复时也不会混淆）
            "uid": _visitor_uid(self.token),
            "online": self.connected and time.time() - self.last_seen_at < 15,
            # 显式离开（关页面 / 点离开）才会写入；切后台导致的心跳超时不写。
            # 前端据此区分「真的走了」和「只是标签页被节流」，不再误报离开/回来。
            "left_at": float(self.left_at or 0.0),
            "is_player": is_player,
            "is_current_player": is_current_player,
            # 花火占的座位不接受换座申请，前端据此隐藏按钮
            "is_ai": bool(is_ai),
            "display_name": self.display_name if self.identity_confirmed else "",
            "identity_confirmed": self.identity_confirmed,
            # QQ 头像：仅查看者已确认身份、且本人已确认身份时下发
            "avatar_url": (
                _qq_avatar_url(self.qq)
                if self.qq and self.identity_confirmed and include_qq_avatar
                else ""
            ),
        }


@dataclass(slots=True)
class GameRoom:
    """In-memory room state. Active rooms intentionally do not survive reloads."""

    room_id: str
    access_token: str
    source: RoomSource
    session_id: str
    platform: str
    group_id: str
    creator_qq: str
    creator_name: str
    admin_room: bool
    game_type: GameType
    difficulty: Difficulty
    turtle_soup_mode: TurtleSoupMode = "bot_host"
    # 自荐当出题人的座位号（0 = 花火出题）。开局后转到 game.host_number，
    # 这里只用于「还没开局时」把「X 正在出题」展示给房间。
    soup_host_number: int = 0
    # 玩家出题时花火是否作为普通参与者一起猜（插件配置 turtle_soup.bot_join_player_host）
    soup_bot_join: bool = True
    # 围棋盘路与贴目：房间级设置。首位入座的玩家可以在开局前自行调整，
    # 调整后连空棋盘也一起重绘（见 RoomManager.set_weiqi_board）。
    weiqi_size: int = 13
    weiqi_komi: float = 7.5
    undercover_host_camp_scales: str | None = None
    undercover_ai_fill_enabled: bool = False
    undercover_min_players: int = 2
    undercover_prepare_seconds: int = 0
    undercover_allow_host_customize_camp_scales: bool = True
    undercover_reveal_identity: bool | None = None
    # 房间内「投票换游戏」：同时只允许一条提案；冷却按房间计时。
    game_vote: GameVoteRequest | None = None
    last_game_vote_at: float = 0.0
    # 自动开局任务在途标记：满员/就绪触发自动开局后置位，任务结束清位。
    # 用于防止取词（可能调 LLM）期间看门狗下一轮又重复触发同一场自动开局。
    undercover_auto_starting: bool = False
    # 托管代打的节流时间戳：离线玩家卡住发言/投票时，看门狗每 2 秒一轮，
    # 这里记下上次触发时间，避免同一行动点被反复 emit。
    undercover_autopilot_at: float = 0.0
    # 引用管理器「卧底分阵营胜场」同一 dict；存在时座位徽章按实时数据计算
    camp_wins_store: dict[str, dict[str, int]] | None = None
    # 单局群通报与复盘缓存：按 game_uid 记录，保证「一局只通报一次」「复盘缓存不重复请求 LLM」
    last_announced_uid: str = ""
    last_announced_text: str = ""
    recap_cache: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    # 「玩家席无人」倒计时只在玩家离开后才开始计时；新建房间（还没有人入座）
    # 不应一出生就被 empty_player_timeout 秒杀，否则管理台开房/LLM 开房后，
    # 访客还没点开链接房间就被销毁了（表现为「链接进不去」「管理台没显示」）。
    player_empty_since: float | None = None
    status: RoomStatus = "waiting"
    visitors: dict[str, Visitor] = field(default_factory=dict)
    next_visitor_number: int = 1
    player_token: str = ""
    player_qq: str = ""
    player_identity_confirmed: bool = False
    player_seat_locked: bool = False
    # 玩家在房间里自己选过花火棋力：再战时不接受 LLM 的难度改写，
    # 否则「我选了简单，再来一局又被模型改回普通」。
    difficulty_chosen_by_player: bool = False
    # 棋类终局快照：播报 / 复盘 / 海报都读这里。玩家可能立刻点「再来一局」，
    # room.game 会被新对局替换，异步任务若直接读 room.game 就会播成新局的 0/1 手。
    last_board_result: dict[str, object] | None = None
    # 海龟汤终局快照：群通知 / 分享战报 / 复盘读取本局汤面、提问与结果（不含汤底）
    last_soup_result: dict[str, object] | None = None
    # 上一次自动播报的时间戳（0 = 从未播报），用于刷屏节流
    last_board_announce_at: float = 0.0
    # 冷却窗口内结束的对局先攒在这里，窗口结束时合并成一条「连战 N 局」发出，
    # 既不刷群也不丢战报（每条为 _board_announce_entry 压出的摘要 dict）
    pending_board_announces: list[dict[str, object]] = field(default_factory=list)
    # 计划中的合并播报时间点（0 = 当前没有排队任务）
    pending_board_flush_at: float = 0.0
    # 换先序列：记录上次开局时的座位成员；成员一变就重新起序列（按座位号定色）
    duel_seat_signature: tuple[str, ...] = ()
    # 房间内「再来一局换边」：None = 跟随插件配置；True/False = 玩家在房间页里自己选的。
    # 只对「一位真人 + 花火」的人机对局生效，两位真人对局一律按插件配置，避免改到别人。
    ai_side_rotation: bool | None = None
    confirmed_participant_qqs: set[str] = field(default_factory=set)
    participant_names: dict[str, str] = field(default_factory=dict)
    round_participant_qqs: set[str] = field(default_factory=set)
    multiplayer: MultiplayerState = field(default_factory=MultiplayerState)
    game: (
        GomokuGame
        | OthelloGame
        | XiangqiGame
        | TicTacToeGame
        | TurtleSoupGame
        | PigDiceGame
        | DrawGuessGame
        | BlackjackGame
        | UndercoverGame
        | WeiqiGame
        | None
    ) = None
    scores: dict[GameType, GameScore] = field(
        default_factory=lambda: {
            "gomoku": GameScore(),
            "xiangqi": GameScore(),
            "weiqi": GameScore(),
            "tictactoe": GameScore(),
            "othello": GameScore(),
            "turtle_soup": GameScore(),
            "pig_dice": GameScore(),
            "draw_guess": GameScore(),
            "blackjack": GameScore(),
            "undercover": GameScore(),
        }
    )
    turtle_soup_stats: TurtleSoupStats = field(default_factory=TurtleSoupStats)
    turtle_soup_recent_signatures: list[str] = field(default_factory=list)
    messages: list[dict[str, object]] = field(default_factory=list)
    next_message_id: int = 1
    chat_transcripts: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    close_reason: str = ""
    last_commentary_at: float = 0.0
    # 正在生成的解说条数：>0 时网页显示「花火正在组织语言」占位并加快轮询
    bot_typing_pending: int = 0
    # 正在生成的聊天回复条数：聊天接口改成异步后，回复在后台生成、由轮询推给前端，
    # 这里用独立计数器，让网页显示「花火正在打字」而不是「正在看着棋盘」。
    bot_chat_pending: int = 0
    # 房间内玩家获胜次数排行榜：visitor_token -> {"name": str, "wins": int}
    player_win_counts: dict[str, dict[str, object]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    chat_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def player(self) -> Visitor | None:
        return self.visitors.get(self.player_token)

    @property
    def current_score(self) -> GameScore:
        return self.scores[self.game_type]

    @property
    def completed_games(self) -> int:
        return self.current_score.completed

    @completed_games.setter
    def completed_games(self, value: int) -> None:
        self.current_score.completed = int(value)

    @property
    def human_wins(self) -> int:
        return self.current_score.human_wins

    @human_wins.setter
    def human_wins(self, value: int) -> None:
        self.current_score.human_wins = int(value)

    @property
    def bot_wins(self) -> int:
        return self.current_score.bot_wins

    @bot_wins.setter
    def bot_wins(self, value: int) -> None:
        self.current_score.bot_wins = int(value)

    @property
    def draws(self) -> int:
        return self.current_score.draws

    @draws.setter
    def draws(self, value: int) -> None:
        self.current_score.draws = int(value)

    def add_message(
        self,
        role: str,
        content: str,
        *,
        visitor: Visitor | None = None,
        message_type: str = "chat",
    ) -> None:
        """Append one bounded room message without exposing private identity fields."""
        cleaned = str(content or "").strip()
        if not cleaned:
            return
        sender_name = ""
        sender_number: int | None = None
        sender_bound = False
        if visitor is not None:
            sender_number = visitor.number
            sender_bound = visitor.identity_confirmed
            sender_name = (
                visitor.display_name.strip()
                if visitor.identity_confirmed and visitor.display_name.strip()
                else "匿名观众"
            )
        self.messages.append(
            {
                "id": self.next_message_id,
                "role": role,
                "content": cleaned[:800],
                "at": time.time(),
                "game_type": self.game_type,
                "message_type": (
                    "event"
                    if role == "system" and message_type == "chat"
                    else str(message_type or "chat")[:30]
                ),
                "sender_name": sender_name,
                "sender_number": sender_number,
                "sender_bound": sender_bound,
            }
        )
        self.next_message_id += 1
        del self.messages[:-80]

    def record_chat_memory(self, visitor: Visitor, role: str, content: str) -> None:
        """Retain private per-QQ chat excerpts only for confirmed identities."""
        if not visitor.identity_confirmed or not visitor.qq:
            return
        qq = visitor.qq
        self.confirmed_participant_qqs.add(qq)
        self.participant_names[qq] = visitor.display_name
        transcript = self.chat_transcripts.setdefault(qq, [])
        transcript.append(
            {
                "role": str(role or "user")[:12],
                "content": str(content or "").strip()[:500],
            }
        )
        del transcript[:-24]

    def touch(self) -> None:
        """Mark a meaningful room operation."""
        self.last_activity_at = time.time()

    def _game_vote_snapshot(self, visitor_token: str) -> dict[str, object] | None:
        """把当前的换游戏提案整理成前端能直接渲染的结构（无提案返回 None）。"""
        vote = self.game_vote
        if vote is None:
            return None
        proposer = self.visitors.get(vote.proposer_token)
        if proposer is None:
            proposer_name = ""
        elif proposer.identity_confirmed and proposer.display_name:
            proposer_name = proposer.display_name
        else:
            proposer_name = f"{proposer.number}号"
        in_favor, against = vote.counts()
        my_vote = vote.votes.get(visitor_token)
        return {
            "request_id": vote.request_id,
            "target_game": vote.target_game,
            "proposer_name": proposer_name,
            "votes_for": in_favor,
            "votes_against": against,
            "required": max(1, int(vote.required or 1)),
            "voter_base": max(1, int(vote.voter_base or 1)),
            "expires_at": vote.expires_at,
            "my_vote": "for" if my_vote is True else ("against" if my_vote is False else ""),
            "is_proposer": bool(
                visitor_token and visitor_token == vote.proposer_token
            ),
        }

    def public_snapshot(
        self,
        visitor_token: str = "",
        global_leaderboard: list[dict[str, object]] | None = None,
        switchable_games: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        """Return room state without QQ identities or management secrets."""
        visitor = self.visitors.get(visitor_token)
        player = self.player
        multiplayer = self.multiplayer
        player_tokens = (
            [seat.visitor_token for seat in multiplayer.seats]
            if multiplayer.enabled
            else ([self.player_token] if self.player_token else [])
        )
        current_token = (
            multiplayer.current_token if multiplayer.enabled else self.player_token
        )
        ai_tokens = {
            seat.visitor_token for seat in multiplayer.seats if seat.is_ai
        }
        now = time.time()
        incoming_requests: list[dict[str, object]] = []
        outgoing_request: dict[str, object] | None = None
        player_names = [
            self.visitors[token].display_name
            for token in player_tokens
            if token in self.visitors and self.visitors[token].display_name
        ]
        player_labels = [
            (
                f"{self.visitors[token].display_name}（{self.visitors[token].number}号）"
                if token in self.visitors and self.visitors[token].display_name
                else f"{self.visitors[token].number}号"
            )
            for token in player_tokens
            if token in self.visitors
        ]
        current_player = self.visitors.get(current_token)
        # 围棋盘路调整权：只给「最早落座的那位真人」。
        # 第二位入座的人、观众、AI 都不该改棋盘大小。
        weiqi_first_token = ""
        if self.game_type == "weiqi":
            if multiplayer.enabled:
                humans = [seat for seat in multiplayer.seats if not seat.is_ai]
            else:
                humans = []
            if humans:
                weiqi_first_token = min(humans, key=lambda seat: seat.seated_at).visitor_token
            elif not multiplayer.enabled:
                weiqi_first_token = self.player_token
        weiqi_moves = (
            len(getattr(self.game, "history", []) or [])
            if isinstance(self.game, WeiqiGame)
            else 0
        )
        # 象棋快照按「当前浏览器坐在哪个座位」下发可走法：两位真人同房时，
        # 对手的走法不该出现在自己页面上。
        my_seat = (
            next(
                (
                    seat
                    for seat in multiplayer.seats
                    if seat.visitor_token and seat.visitor_token == visitor_token
                ),
                None,
            )
            if multiplayer.enabled
            else None
        )
        if multiplayer.enabled and visitor:
            for swap in multiplayer.swap_requests.values():
                requester = self.visitors.get(swap.requester_token)
                target = self.visitors.get(swap.target_token)
                if swap.target_token == visitor.token and requester:
                    incoming_requests.append(
                        {
                            "request_id": swap.request_id,
                            "requester_number": requester.number,
                            "expires_at": swap.expires_at,
                        }
                    )
                if swap.requester_token == visitor.token and target:
                    outgoing_request = {
                        "request_id": swap.request_id,
                        "target_number": target.number,
                        "expires_at": swap.expires_at,
                    }
        return {
            "room_id": self.room_id,
            "game_type": self.game_type,
            "source": self.source,
            "admin_room": self.admin_room,
            "status": self.status,
            "created_at": self.created_at,
            "visitor_number": visitor.number if visitor else None,
            "visitor_uid": _visitor_uid(visitor.token) if visitor else "",
            "visitor_display_name": (
                visitor.display_name if visitor and visitor.identity_confirmed else ""
            ),
            "is_player": bool(visitor and visitor.token in player_tokens),
            "is_current_player": bool(visitor and visitor.token == current_token),
            "player_number": player.number if player else None,
            "player_numbers": [
                self.visitors[token].number
                for token in player_tokens
                if token in self.visitors
            ],
            "player_names": player_names,
            "player_labels": player_labels,
            "current_player_name": (
                current_player.display_name
                if current_player and current_player.identity_confirmed
                else ""
            ),
            "player_capacity": multiplayer.capacity if multiplayer.enabled else 1,
            "multiplayer_enabled": multiplayer.enabled,
            # 对局模式：solo_ai（玩家 vs AI）/ multiplayer（玩家席多人对局）。
            # 前端按此字段决定座位 UI 与中途退席策略；未来棋类等玩法开放多人上座时，
            # 只要这里返回 multiplayer，前端无需再按玩法名改判断。
            "player_mode": "multiplayer" if multiplayer.enabled else "solo_ai",
            "current_player_number": (
                self.visitors[current_token].number
                if current_token in self.visitors
                else None
            ),
            "turn_deadline": multiplayer.turn_deadline if multiplayer.enabled else 0,
            "turn_timeout_seconds": (
                multiplayer.turn_timeout_seconds if multiplayer.enabled else 0
            ),
            # 棋类对弈掉线暂停：前端据此把倒计时文案换成「等待重连」
            "duel_paused": bool(multiplayer.enabled and multiplayer.duel_paused),
            "duel_pause_remaining": (
                max(0, int(round(multiplayer.duel_pause_deadline - now)))
                if multiplayer.enabled and multiplayer.duel_paused
                else 0
            ),
            "incoming_swap_requests": incoming_requests,
            "outgoing_swap_request": outgoing_request,
            "swap_cooldown_until": (
                multiplayer.last_swap_request_at.get(visitor.token, 0)
                + multiplayer.swap_cooldown_seconds
                if multiplayer.enabled and visitor
                else 0
            ),
            "server_time": now,
            # 花火正在生成解说：网页据此显示占位并把轮询提速到 400ms
            "bot_typing": self.bot_typing_pending > 0,
            "bot_chat_typing": self.bot_chat_pending > 0,
            "player_confirmed": (
                visitor.identity_confirmed
                if visitor is not None
                else self.player_identity_confirmed
            ),
            "identity_token": (
                visitor.binding_token
                if visitor and not visitor.identity_confirmed and not self.admin_room
                else ""
            ),
            "identity_token_expires_at": (
                visitor.binding_expires_at
                if visitor and not visitor.identity_confirmed and not self.admin_room
                else 0
            ),
            "turtle_soup_mode": self.turtle_soup_mode,
            "soup_host_number": int(self.soup_host_number or 0),
            "soup_bot_join": bool(self.soup_bot_join),
            "difficulty": self.difficulty,
            # 房间内投票换游戏：可选玩法（只含管理员已开放、且不是当前玩法的）
            # 以及正在进行的提案。门槛由后端算好，前端只负责展示。
            "switchable_games": list(switchable_games or []),
            "game_vote": self._game_vote_snapshot(visitor_token),
            # 棋类/贪心骰子/二十一点可中途改难度（其余玩法的 difficulty 是内容等级，不是强度）
            "difficulty_adjustable": self.game_type in DIFFICULTY_GAME_TYPES,
            # 房间内自选的换边方式（None = 跟随插件配置）；前端据此渲染「每局换边/保持执色」
            "ai_side_rotation": self.ai_side_rotation,
            # 围棋盘路：房间级设置，首位落座的玩家在开局前可以自己改，
            # 改完连空棋盘也一起换路数（前端按 size 重绘网格）。
            "weiqi": {
                "size": self.weiqi_size,
                "komi": self.weiqi_komi,
                "moves": weiqi_moves,
                "adjustable": bool(
                    self.game_type == "weiqi"
                    and visitor is not None
                    and weiqi_first_token
                    and visitor.token == weiqi_first_token
                    and (not weiqi_moves or self.status == "finished")
                    and self.status != "closed"
                ),
            },
            "undercover_host_camp_scales": self.undercover_host_camp_scales,
            "undercover_ai_fill_enabled": self.undercover_ai_fill_enabled,
            "undercover_min_players": self.undercover_min_players,
            "undercover_prepare_seconds": self.undercover_prepare_seconds,
            "undercover_allow_host_customize_camp_scales": (
                self.undercover_allow_host_customize_camp_scales
            ),
            "undercover_reveal_identity": self.undercover_reveal_identity,
            "player_seats": [
                {
                    "number": seat.number,
                    # 只下发不可逆的短 uid：快照对全房间可见，绝不能带原始 token
                    # （token 就是整套接口的身份凭证，泄露等于把座位让给别人）
                    "uid": _visitor_uid(seat.visitor_token),
                    "display_name": seat.display_name,
                    "identity_confirmed": bool(seat.identity_confirmed),
                    "is_ai": bool(seat.is_ai),
                    "side": seat.side,
                    "seated_at": seat.seated_at,
                    "ready": bool(seat.ready),
                    # 藏品/护身符徽章：优先按当前阵营胜场即时计算，回退到游戏中缓存的 seat 值
                    "badges": _seat_badges(seat, self),
                    # QQ 头像：仅已确认身份的玩家下发头像地址；AI 座位前端回退显示首字
                    "avatar_url": (
                        _qq_avatar_url(seat.qq)
                        if seat.qq and seat.identity_confirmed
                        else ""
                    ),
                }
                for seat in (multiplayer.seats if multiplayer.enabled else [])
            ],
            "visitors": [
                item.public_snapshot(
                    is_player=item.token in player_tokens,
                    is_current_player=item.token == current_token,
                    is_ai=item.token in ai_tokens,
                    include_qq_avatar=bool(
                        visitor and visitor.identity_confirmed
                    ),
                )
                for item in sorted(
                    self.visitors.values(), key=lambda value: value.number
                )
            ],
            "game": (
                self.game.snapshot(
                    reveal_answer=bool(
                        visitor and visitor.token in player_tokens
                    )
                    or self.status in {"finished", "rematch_pending"}
                )
                if isinstance(self.game, DrawGuessGame)
                else self.game.snapshot(
                    visitor_uid=_visitor_uid(visitor.token) if visitor else None
                )
                if isinstance(self.game, UndercoverGame)
                else self.game.snapshot(
                    viewer_side=my_seat.side if my_seat else None
                )
                if isinstance(self.game, XiangqiGame)
                else self.game.snapshot(
                    viewer_uid=_visitor_uid(visitor.token) if visitor else None,
                    reveal_required=soup_reveal_required(self),
                )
                if isinstance(self.game, TurtleSoupGame)
                else self.game.snapshot()
                if self.game
                else None
            ),
            "score": {
                "human": self.human_wins,
                "bot": self.bot_wins,
                "draws": self.draws,
                "games": self.completed_games,
            },
            # 本局（棋类）是否已自动/手动通报到群：前端据此把「通知到群」按钮置灰
            "board_announced": bool(
                self.game_type in {"gomoku", "othello", "xiangqi", "weiqi"}
                and self.last_announced_uid
                == f"board:{int(self.completed_games or 0)}"
            ),
            # 海龟汤本局结果快照（不含汤底）：分享战报 / 花火复盘 / 通知到群都读它，
            # 这样玩家点「再来一道」后仍能取到上一局的汤面与提问。
            "soup_result": self.last_soup_result,
            "soup_announced": bool(
                self.game_type == "turtle_soup"
                and self.last_announced_uid
                == f"soup:{int(self.completed_games or 0)}"
            ),
            "leaderboard": (
                [
                    {
                        "name": str(info.get("name") or "未知玩家"),
                        "wins": int(info.get("wins") or 0),
                        "games": int(info.get("games") or 0),
                        "winrate": float(info.get("winrate") or 0),
                    }
                    for info in global_leaderboard
                ]
                if global_leaderboard is not None
                else [
                    {
                        "name": str(info.get("name") or "未知玩家"),
                        "wins": int(info.get("wins") or 0),
                        "games": int(info.get("games") or 0),
                        "winrate": 0.0,
                    }
                    for _token, info in sorted(
                        self.player_win_counts.items(),
                        key=lambda kv: int(kv[1].get("wins") or 0),
                        reverse=True,
                    )
                ]
            ),
            "turtle_soup_stats": {
                "questions": self.turtle_soup_stats.questions,
                "hints": self.turtle_soup_stats.hints,
                "answer_attempts": self.turtle_soup_stats.answer_attempts,
            },
            "messages": self.messages[-60:],
            "close_reason": self.close_reason,
        }

    def admin_snapshot(self) -> dict[str, object]:
        """Return operational data for the authenticated plugin page."""
        player = self.player
        multiplayer = self.multiplayer
        player_tokens = (
            [seat.visitor_token for seat in multiplayer.seats]
            if multiplayer.enabled
            else ([self.player_token] if self.player_token else [])
        )
        current_token = (
            multiplayer.current_token if multiplayer.enabled else self.player_token
        )
        ai_tokens = {
            seat.visitor_token for seat in multiplayer.seats if seat.is_ai
        }
        return {
            "room_id": self.room_id,
            "source": self.source,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "creator_qq": self.creator_qq,
            "creator_name": self.creator_name,
            "admin_room": self.admin_room,
            "game_type": self.game_type,
            "turtle_soup_mode": self.turtle_soup_mode,
            "soup_host_number": int(self.soup_host_number or 0),
            "soup_bot_join": bool(self.soup_bot_join),
            "status": self.status,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "difficulty": self.difficulty,
            # 棋类/贪心骰子/二十一点可中途改难度（其余玩法的 difficulty 是内容等级，不是强度）
            "difficulty_adjustable": self.game_type in DIFFICULTY_GAME_TYPES,
            "scores": {
                game_type: {
                    "human": score.human_wins,
                    "bot": score.bot_wins,
                    "draws": score.draws,
                    "games": score.completed,
                }
                for game_type, score in self.scores.items()
            },
            "turtle_soup_progress": self._turtle_soup_progress(),
            "pig_dice_progress": self._pig_dice_progress(),
            "draw_guess_progress": self._draw_guess_progress(),
            "blackjack_progress": self._blackjack_progress(),
            "undercover_progress": self._undercover_progress(),
            "player_number": player.number if player else None,
            "player_numbers": [
                self.visitors[token].number
                for token in player_tokens
                if token in self.visitors
            ],
            "player_names": [
                self.visitors[token].display_name
                for token in player_tokens
                if token in self.visitors and self.visitors[token].display_name
            ],
            "player_labels": [
                (
                    f"{self.visitors[token].display_name}（{self.visitors[token].number}号）"
                    if token in self.visitors and self.visitors[token].display_name
                    else f"{self.visitors[token].number}号"
                )
                for token in player_tokens
                if token in self.visitors
            ],
            "player_capacity": multiplayer.capacity if multiplayer.enabled else 1,
            "current_player_number": (
                self.visitors[current_token].number
                if current_token in self.visitors
                else None
            ),
            "player_qq": self.player_qq,
            "player_confirmed": self.player_identity_confirmed,
            "visitors": [
                {
                    **item.public_snapshot(
                        is_player=item.token in player_tokens,
                        is_current_player=item.token == current_token,
                        is_ai=item.token in ai_tokens,
                    ),
                    "player_qq": (
                        multiplayer.seat_for_token(item.token).qq
                        if multiplayer.enabled
                        and multiplayer.seat_for_token(item.token) is not None
                        and multiplayer.seat_for_token(item.token).qq
                        else item.qq
                        if item.identity_confirmed
                        else self.player_qq
                        if item.token == self.player_token
                        else ""
                    ),
                    "display_name": (
                        multiplayer.seat_for_token(item.token).display_name
                        if multiplayer.enabled
                        and multiplayer.seat_for_token(item.token) is not None
                        else self.player.display_name
                        if item.token == self.player_token and self.player
                        else item.display_name
                    ),
                }
                for item in sorted(
                    self.visitors.values(), key=lambda value: value.number
                )
            ],
        }

    def _turtle_soup_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, TurtleSoupGame):
            return None
        return {
            "phase": self.game.phase,
            "processing": self.game.processing,
            "question_count": self.game.question_count,
            "hints_used": self.game.hints_used,
            "answer_attempts": self.game.answer_attempts,
            "solved": self.game.solved,
            "gave_up": self.game.gave_up,
            "content_level": self.game.content_level,
            "mode": self.game.mode,
            "turn_count": self.game.turn_count,
        }

    def _pig_dice_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, PigDiceGame):
            return None
        return {
            "turn": self.game.turn,
            "human_score": self.game.human_score,
            "bot_score": self.game.bot_score,
            "turn_total": self.game.turn_total,
            "target_score": self.game.target_score,
            "risk_style": self.game.bot_risk_style,
        }

    def _draw_guess_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, DrawGuessGame):
            return None
        return {
            "guess_count": len(self.game.guesses),
            "max_guesses": self.game.max_guesses,
            "processing": self.game.processing,
            "solved": self.game.solved,
            "timed_out": self.game.timed_out,
            "remaining_seconds": self.game.remaining_seconds,
        }

    def _blackjack_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, BlackjackGame):
            return None
        return {
            "phase": self.game.phase,
            "finished": self.game.finished,
            "dealer_blackjack": self.game.dealer_blackjack,
            "hand_count": len(self.game.hands),
        }

    def _undercover_progress(self) -> dict[str, object] | None:
        if not isinstance(self.game, UndercoverGame):
            return None
        return self.game.progress()
