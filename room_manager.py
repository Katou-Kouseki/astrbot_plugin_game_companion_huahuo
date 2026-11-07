from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from .blackjack import BlackjackGame
from .draw_guess import DrawGuessGame
from .gomoku import BLACK, EMPTY, WHITE, Difficulty, GomokuGame
from .models import (
    DIFFICULTY_GAME_TYPES,
    GameRoom,
    GameType,
    GameVoteRequest,
    MultiplayerState,
    PlayerSeat,
    RoomSource,
    SeatSwapRequest,
    TurtleSoupMode,
    Visitor,
    _undercover_badges_for,
    _undercover_identity_key,
    _qq_avatar_url,
    _visitor_uid,
    soup_reveal_required,
)
from .othello import BLACK as OTHELLO_BLACK
from .othello import WHITE as OTHELLO_WHITE
from .othello import OthelloGame
from .pig_dice import PigDiceGame
from .pikafish import PikafishService
from .tictactoe import NOUGHT as TICTACTOE_NOUGHT
from .tictactoe import TicTacToeGame
from .tictactoe import X as TICTACTOE_X
from .trusted_identity import TrustedIdentity
from .turtle_soup import (
    SoupContentLevel,
    SoupPuzzle,
    SoupVerdict,
    TurtleSoupGame,
    clean_player_text,
)
from .turtle_soup_deck import redact_spoiler
from .undercover import UndercoverGame, UCPlayer
from .undercover_words import UndercoverWordStore
from .xiangqi import BLACK as XIANGQI_BLACK
from .xiangqi import RED as XIANGQI_RED
from .xiangqi import XiangqiGame
from .weiqi import DEFAULT_KOMI as WEIQI_DEFAULT_KOMI
from .weiqi import DEFAULT_SIZE as WEIQI_DEFAULT_SIZE
from .weiqi import SIZES as WEIQI_SIZES
from .weiqi import WeiqiGame

logger = logging.getLogger("astrbot_plugin_game_companion")

# 棋盘对弈类：共用「回合倒计时 / 掉线暂停 / 按座位执方 / 双人席位」这套逻辑。
# 新增棋类时把类加进来，其余分支只需补自己的走子与快照实现。
BOARD_GAME_CLASSES = (GomokuGame, OthelloGame, XiangqiGame, WeiqiGame)

# AI 玩家的性格形容词池（2 字），用于生成「火花·调皮(AI)」这类名称，并作为其答题人设
_UC_AI_ADJECTIVES: tuple[str, ...] = (
    "调皮", "沉静", "机敏", "憨厚", "狡黠", "急性", "慢热", "嘴硬",
    "心细", "迷糊", "大胆", "谨慎", "乐天", "腹黑", "直球", "闷骚",
    "严肃", "活泼", "文静", "高冷", "温柔", "率直", "稳重", "爱闹",
)
_UC_AI_USED: set[str] = set()  # 本进程已用过的形容词，避免同房重复

def _uc_ai_name(avoid: set[str] | None = None) -> tuple[str, str]:
    """生成 (显示名「火花·调皮(AI)」/「花火·调皮(AI)」, 性格形容词「调皮」)。

    基础名在「火花 / 花火」里随机，形容词从池中不重复抽取。
    """
    pool = [a for a in _UC_AI_ADJECTIVES if a not in _UC_AI_USED and (not avoid or a not in avoid)]
    if not pool:
        _UC_AI_USED.clear()
        pool = [a for a in _UC_AI_ADJECTIVES if not avoid or a not in avoid] or list(_UC_AI_ADJECTIVES)
    adj = random.choice(pool)
    _UC_AI_USED.add(adj)
    base = random.choice(("火花", "花火"))
    return f"{base}·{adj}(AI)", adj

RoomCallback = Callable[[str, GameRoom, dict[str, Any]], Awaitable[None]]

SUPPORTED_GAMES: tuple[GameType, ...] = (
    "gomoku",
    "xiangqi",
    "weiqi",
    "tictactoe",
    "othello",
    "turtle_soup",
    "pig_dice",
    "draw_guess",
    "blackjack",
    "undercover",
)

# 全部游戏类型：用于按游戏分桶全局战绩、以及旧扁平数据迁移识别。
# 直接由 SUPPORTED_GAMES 派生，新增玩法时无需再改这里。
_UC_GAME_TYPES: frozenset[str] = frozenset(SUPPORTED_GAMES)

# 已支持「玩家席（多人上座）」的玩法。
#
# 五子棋 / 黑白棋是「两人对弈」：固定 2 席，空着的位置可以等真人，也可以直接
# 「和花火对局」（花火占一个 AI 座位）。谁先入座谁执黑，再来一局双方换色。
# 海龟汤 / 黑杰克 / 谁是卧底是原有的人数型玩法。
#
# 未来方向：象棋、贪心骰子、你画我猜也要支持多人上座。届时把玩法加进下面两个
# 集合之一，并补齐该玩法的多方轮转/结算逻辑即可；前端只按座位状态判断，不再
# 硬编码玩法名。
SEAT_CAPABLE_GAMES: frozenset[GameType] = frozenset(
    {
        "turtle_soup",
        "blackjack",
        "undercover",
        "gomoku",
        "othello",
        "xiangqi",
        "weiqi",
    }
)

# 两人对弈类：座位顺序决定执色（棋类 1 号黑、2 号白；象棋 1 号红、2 号黑），
# 对局中不允许增减座位。
DUEL_GAMES: frozenset[GameType] = frozenset(
    {"gomoku", "othello", "xiangqi", "weiqi"}
)

# 各上座玩法的默认席位数；未列出的走 2 席。
SEAT_CAPACITY: dict[str, int] = {
    "gomoku": 2,
    "othello": 2,
    "xiangqi": 2,
    "weiqi": 2,
}

# 棋类全局榜里花火的稳定键与展示名：AI 座位每次都是随机 token，这里用固定键累计，
# 让花火也能在棋类排行榜里拥有自己的局数与胜率排名（而不是被当成散落的「火花·XX(AI)」）。
AI_LEADERBOARD_KEY = "__huahuo_ai__"
AI_LEADERBOARD_NAME = "花火"


def _parse_camp_scales(raw: object) -> tuple[int, int, int]:
    """解析"平民 卧底 白板"字符串，返回 tuple[int,int,int]。格式错误时兜底返回 (4,1,0)。"""
    text = str(raw or "").strip()
    parts = [p for p in text.replace(",", " ").split() if p]
    civ, uc, wb = 4, 1, 0
    try:
        if len(parts) == 1 and ":" in parts[0]:
            nums = parts[0].split(":")
            if len(nums) == 3:
                civ, uc, wb = int(nums[0]), int(nums[1]), int(nums[2])
        elif len(parts) == 3:
            civ, uc, wb = int(parts[0]), int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        civ, uc, wb = 4, 1, 0
    civ = max(1, civ)
    uc = max(1, uc)
    wb = max(0, wb)
    if civ + uc + wb < 2:
        civ = 4
        uc = 1
        wb = 0
    return civ, uc, wb


class RoomManager:
    """Own room quotas, seats, game transitions, and expiry rules."""

    CLOSED_ACCESS_TTL_SECONDS = 15 * 60
    MAX_CLOSED_ACCESS_RECORDS = 256
    FINISHED_PLAYER_LEAVE_GRACE_SECONDS = 8
    # 访客表回收：房间可能被反复打开（群链接长期有效），离线访客只增不减会让
    # 每次快照都越来越大。超过阈值后清理「长时间离线且没占座」的访客。
    # 有人守着房间但一直没操作（挂机看结算页 / 长时间思考）时的宽限上限。
    # 不能无限宽限：群聊默认只有 1 个房间配额，一个挂着的页面会把配额永久占死，
    # 别人再开房就报「群聊房间已达到并行上限」。
    AFK_IDLE_GRACE_SECONDS = 1800
    VISITOR_PRUNE_MIN_IDLE_SECONDS = 1800
    VISITOR_PRUNE_THRESHOLD = 40
    FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS = 60
    # 集结阶段离线玩家席自动移出：显式关闭网页（leave）宽限数秒防刷新误踢；
    # 心跳超时阈值放宽到 90s，容忍浏览器后台标签节流（否则切后台的玩家会被误踢）
    DEPARTED_LEAVE_GRACE_SECONDS = 8
    DEPARTED_HEARTBEAT_TIMEOUT_SECONDS = 90
    # 棋类对弈：心跳超过该秒数没更新就认为掉线，先暂停等重连而不是直接判负
    DUEL_OFFLINE_SECONDS = 15
    IDENTITY_TOKEN_TTL_SECONDS = 300
    CHAT_COOLDOWN_SECONDS = 0.8

    def __init__(
        self,
        *,
        max_group_rooms: int = 1,
        max_private_rooms: int = 1,
        empty_player_timeout: int = 60,
        idle_timeout: int = 300,
        turtle_soup_max_hints: int = 3,
        turtle_soup_content_level: SoupContentLevel = "normal",
        turtle_soup_max_players: int = 0,
        turtle_soup_max_questions: int = 0,
        turtle_soup_free_questioning: bool = True,
        turtle_soup_free_question_cooldown: int = 1,
        turtle_soup_auto_reveal_seconds: int = 1800,
        turtle_soup_bot_join: bool = True,
        multiplayer_turn_timeout: int = 60,
        duel_turn_timeout: int = 180,
        duel_offline_grace: int = 90,
        duel_afk_seat_timeout: int = 60,
        weiqi_board_size: int = WEIQI_DEFAULT_SIZE,
        weiqi_komi: float = WEIQI_DEFAULT_KOMI,
        swap_request_cooldown: int = 30,
        swap_request_expiry: int = 20,
        game_vote_enabled: bool = True,
        game_vote_expiry: int = 60,
        game_vote_cooldown: int = 30,
        game_vote_allow_while_playing: bool = False,
        duel_side_rotation: bool = True,
        ai_think_delay: float = 0.8,
        draw_guess_max_guesses: int = 5,
        draw_guess_duration_seconds: int = 120,
        pig_dice_target_score: int = 50,
        blackjack_max_players: int = 1,
        undercover_max_players: int = 10,
        undercover_min_players: int = 2,
        undercover_camp_scales_default: str = "4 1 0",
        undercover_allow_host_customize_camp_scales: bool = True,
        undercover_match_seconds: int = 180,
        undercover_prepare_seconds: int = 10,
        undercover_speaking_seconds: int = 160,
        undercover_voting_seconds: int = 120,
        undercover_first_round_non_voting: int = 3,
        undercover_send_identity_in_card: bool = True,
        undercover_show_voters: bool = False,
        undercover_similarity: int = 80,
        undercover_ai_fill_enabled: bool = True,
        undercover_ai_fill_min_players: int = 3,
        undercover_word_store: UndercoverWordStore | None = None,
        enabled_games: Mapping[GameType, bool] | None = None,
        xiangqi_engine: PikafishService | None = None,
        global_stats_path: str | Path | None = None,
        event_callback: RoomCallback | None = None,
    ) -> None:
        self.max_group_rooms = max(0, int(max_group_rooms))
        self.max_private_rooms = max(0, int(max_private_rooms))
        self.empty_player_timeout = max(0, int(empty_player_timeout))
        self.idle_timeout = max(0, int(idle_timeout))
        self.turtle_soup_max_hints = max(0, int(turtle_soup_max_hints))
        self.turtle_soup_content_level = turtle_soup_content_level
        self.turtle_soup_max_players = max(0, int(turtle_soup_max_players))
        self.turtle_soup_max_questions = max(0, int(turtle_soup_max_questions or 0))
        self.turtle_soup_free_questioning = bool(turtle_soup_free_questioning)
        self.turtle_soup_free_question_cooldown = max(0, int(turtle_soup_free_question_cooldown))
        # 海龟汤超时兜底揭晓：0 表示不自动揭晓（只能由玩家投票结束）
        self.turtle_soup_auto_reveal_seconds = max(
            0, int(turtle_soup_auto_reveal_seconds)
        )
        # 玩家出题时花火是否作为普通参与者一起猜；节奏按在线玩家数自动换算
        self.turtle_soup_bot_join = bool(turtle_soup_bot_join)
        self.multiplayer_turn_timeout = max(0, int(multiplayer_turn_timeout))
        # 棋类对弈：每步倒计时（0 关闭）与掉线后等待重连的宽限秒数
        self.duel_turn_timeout = max(0, int(duel_turn_timeout))
        self.duel_offline_grace = max(0, int(duel_offline_grace))
        # 棋类「备战阶段挂机让座」：页面开着但人走了，心跳一直新鲜，服务端只能靠
        # 「多久没有有效操作」判断。0 表示关闭（不自动让座、也不弹顶替确认）。
        self.duel_afk_seat_timeout = max(0, int(duel_afk_seat_timeout))
        # 围棋盘路与贴目：只允许 9 / 13 / 19 路，贴目按 0.5 的整数倍保存
        self.weiqi_board_size = (
            int(weiqi_board_size) if int(weiqi_board_size) in {9, 13, 19} else WEIQI_DEFAULT_SIZE
        )
        self.weiqi_komi = round(max(0.0, min(float(weiqi_komi), 30.0)) * 2) / 2
        self.swap_request_cooldown = max(0, int(swap_request_cooldown))
        self.swap_request_expiry = max(1, int(swap_request_expiry))
        # 房间内「投票换游戏」：开关、提案有效期、发起冷却、是否允许对局进行中发起
        self.game_vote_enabled = bool(game_vote_enabled)
        self.game_vote_expiry = max(10, min(int(game_vote_expiry), 600))
        self.game_vote_cooldown = max(0, min(int(game_vote_cooldown), 600))
        self.game_vote_allow_while_playing = bool(game_vote_allow_while_playing)
        # 再来一局是否换先：开着每局互换黑白（象棋红黑）；关掉就固定按座位号执色
        self.duel_side_rotation = bool(duel_side_rotation)
        # 花火「思考」的基础延迟（秒）：各难度在此基础上做小幅增减，
        # 避免 AI 先手时一开局就落子，看起来像没在想。
        self.ai_think_delay = max(0.0, min(float(ai_think_delay), 5.0))
        self.draw_guess_max_guesses = max(1, min(int(draw_guess_max_guesses), 10))
        self.draw_guess_duration_seconds = max(
            10, min(int(draw_guess_duration_seconds), 600)
        )
        self.pig_dice_target_score = max(20, min(int(pig_dice_target_score), 200))
        self.blackjack_max_players = max(1, min(int(blackjack_max_players), 6))
        self.undercover_max_players = max(2, min(int(undercover_max_players), 20))
        self.undercover_min_players = max(2, min(int(undercover_min_players), self.undercover_max_players))
        self.undercover_camp_scales_default = _parse_camp_scales(
            undercover_camp_scales_default
        )
        self.undercover_allow_host_customize_camp_scales = bool(undercover_allow_host_customize_camp_scales)
        self.undercover_match_seconds = max(10, min(int(undercover_match_seconds), 600))
        self.undercover_prepare_seconds = max(0, min(int(undercover_prepare_seconds), 120))
        self.undercover_speaking_seconds = max(
            0, min(int(undercover_speaking_seconds), 600)
        )
        self.undercover_voting_seconds = max(
            0, min(int(undercover_voting_seconds), 600)
        )
        self.undercover_first_round_non_voting = max(
            2, min(int(undercover_first_round_non_voting), 10)
        )
        self.undercover_send_identity_in_card = bool(undercover_send_identity_in_card)
        self.undercover_show_voters = bool(undercover_show_voters)
        self.undercover_similarity = max(0, min(int(undercover_similarity), 100))
        self.undercover_ai_fill_enabled = bool(undercover_ai_fill_enabled)
        self.undercover_ai_fill_min_players = max(
            self.undercover_min_players,
            min(int(undercover_ai_fill_min_players), 12),
        )
        self.undercover_word_store = undercover_word_store
        configured_games = enabled_games or {}
        self.enabled_games: dict[GameType, bool] = {
            game_type: bool(configured_games.get(game_type, True))
            for game_type in SUPPORTED_GAMES
        }
        self.xiangqi_engine = xiangqi_engine
        self.event_callback = event_callback
        self.rooms: dict[str, GameRoom] = {}
        # 后台执行的 AI 回合任务：开局/落子后先返回给浏览器，AI 想好了再推过去
        self._bot_tasks: set[asyncio.Task] = set()
        # 后台执行的卧底自动开局任务：避免 sweep 串行循环被取词（可能调 LLM）阻塞
        self._auto_start_tasks: set[asyncio.Task] = set()
        self._access_index: dict[str, str] = {}
        self._closed_access: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        # 全局胜场榜：按游戏类型分桶，game_type -> {已绑定 QQ -> {"name","wins"}}，跨房间汇总。
        # 这样右侧面板可按当前游戏展示「该玩法的全局战绩排行」。
        self.global_stats_path = Path(global_stats_path) if global_stats_path else None
        self.global_player_wins: dict[str, dict[str, dict[str, object]]] = (
            self._load_global_stats() if self.global_stats_path else {}
        )
        # 谁是卧底分阵营胜场（用于「藏品/护身符」徽章解锁）：name -> {"civilian","undercover","whiteboard"}
        self.undercover_camp_wins_path: Path | None = None
        if self.global_stats_path is not None:
            self.undercover_camp_wins_path = self.global_stats_path.with_name(
                "undercover_camp_wins.json"
            )
        self.undercover_camp_wins: dict[str, dict[str, int]] = (
            self._load_undercover_camp_wins()
            if self.undercover_camp_wins_path
            else {}
        )
        # 操作日志：管理台关键动作（清空排行榜/修正胜场/关房/安排玩家等）落盘，便于审阅
        self.operation_log_path: Path | None = None
        if self.global_stats_path is not None:
            self.operation_log_path = self.global_stats_path.with_name(
                "operation_log.jsonl"
            )
        # 卧底集结：全员就绪的时间戳（room_id -> now）。给玩家留出确认窗口，避免一键瞬间开局
        self._undercover_all_ready_at: dict[str, float] = {}
        # 全员就绪 -> 自动开局前的确认等待秒数（期间可取消准备）
        self.UC_ALL_READY_CONFIRM_SECONDS = 2.0

    @staticmethod
    def _bump_daily(
        entry: dict[str, object],
        *,
        wins: int = 0,
        games: int = 0,
        now: float | None = None,
    ) -> None:
        """维护 entry["daily"] = {YYYY-MM-DD: {"w": 胜, "g": 局}}，供日报/周报按时间窗口统计。

        以前「日报」和「周报」都读同一份累计榜，两种报表内容完全一样。
        这里按天分桶，只保留最近 60 天，避免统计文件无限增长。
        """
        if not wins and not games:
            return
        stamp = time.localtime(now if now is not None else time.time())
        day = time.strftime("%Y-%m-%d", stamp)
        daily = entry.get("daily")
        if not isinstance(daily, dict):
            daily = {}
            entry["daily"] = daily
        slot = daily.setdefault(day, {"w": 0, "g": 0})
        if not isinstance(slot, dict):
            slot = {"w": 0, "g": 0}
            daily[day] = slot
        slot["w"] = int(slot.get("w") or 0) + int(wins)
        slot["g"] = int(slot.get("g") or 0) + int(games)
        if len(daily) > 60:
            for key in sorted(daily)[:-60]:
                daily.pop(key, None)

    def global_leaderboard(
        self,
        game_type: str | None = None,
        limit: int = 50,
        *,
        days: int | None = None,
        now: float | None = None,
    ) -> list[dict[str, object]]:
        """返回指定游戏类型下的跨房间全局胜场榜（按胜场降序）。

        AI 座位的 qq 是每次对局新建的随机 token，会导致同一个 AI 名（如「花火·调皮(AI)」）
        被记成多条。这里按「名字」合并累计，使同名 AI 只显示一条、胜场累加。
        棋类玩法同时返回 `games`（总局数）与 `winrate`（胜率，百分比），供前端展示。
        """
        bucket = self.global_player_wins.get(game_type or "") or {}
        window = int(days) if days and int(days) > 0 else 0
        days_allowed: set[str] | None = None
        if window:
            today = time.localtime(now if now is not None else time.time())
            days_allowed = set()
            for offset in range(window):
                days_allowed.add(
                    time.strftime("%Y-%m-%d", time.localtime(
                        time.mktime(today) - offset * 86400
                    ))
                )
        merged: dict[str, dict[str, object]] = {}
        for _qq, info in bucket.items():
            name = str(info.get("name") or "未知玩家").strip() or "未知玩家"
            if days_allowed is not None:
                daily = info.get("daily")
                wins = games = 0
                if isinstance(daily, dict):
                    for day, slot in daily.items():
                        if day not in days_allowed or not isinstance(slot, dict):
                            continue
                        wins += int(slot.get("w") or 0)
                        games += int(slot.get("g") or 0)
                if not wins and not games:
                    continue
            else:
                wins = int(info.get("wins") or 0)
                games = int(info.get("games") or 0)
            merged.setdefault(name, {"name": name, "wins": 0, "games": 0})
            merged[name]["wins"] = int(merged[name]["wins"] or 0) + wins
            merged[name]["games"] = int(merged[name]["games"] or 0) + games
        rows: list[dict[str, object]] = []
        for info in sorted(
            merged.values(),
            key=lambda item: (
                int(item.get("wins") or 0),
                int(item.get("games") or 0),
            ),
            reverse=True,
        ):
            wins = int(info.get("wins") or 0)
            games = int(info.get("games") or 0)
            # 历史数据（0.5.20 及更早）只记「胜场」没记「总局数」，升级后 games 会从 0 开始，
            # 导致「12 胜 / 1 局」算成 1200%。这里兜底：局数至少等于胜场（每胜至少对应一局）。
            games = max(games, wins)
            rows.append(
                {
                    "name": str(info["name"]),
                    "wins": wins,
                    "games": games,
                    "winrate": round(wins * 100 / games, 1) if games else 0.0,
                }
            )
        return rows[: max(0, int(limit))]

    @staticmethod
    def _is_known_game_type(key: str) -> bool:
        return key in _UC_GAME_TYPES

    def _load_global_stats(self) -> dict[str, dict[str, dict[str, object]]]:
        """从磁盘加载全局胜场数据；文件缺失或损坏时回退为空。

        兼容旧版扁平结构（qq -> {"name","wins"}，仅统计过卧底）：若顶层出现非游戏类型的键，
        视为旧数据，整体纳入「undercover」分桶。
        """
        if self.global_stats_path is None or not self.global_stats_path.is_file():
            return {}
        try:
            raw = json.loads(self.global_stats_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            logger.warning("[GameCompanion] 全局胜场数据无法解析，将重新开始统计。")
            return {}
        if not isinstance(raw, dict):
            return {}
        # 旧版扁平结构迁移：顶层全是非游戏类型键（即 qq 键）→ 归为 undercover
        if raw and not any(self._is_known_game_type(k) for k in raw):
            raw = {"undercover": raw}
        cleaned: dict[str, dict[str, dict[str, object]]] = {}
        for gtype, players in raw.items():
            if not isinstance(gtype, str) or not isinstance(players, dict):
                continue
            cleaned[gtype] = {}
            for qq, info in players.items():
                if not isinstance(qq, str) or not qq.strip() or not isinstance(info, dict):
                    continue
                wins = max(0, int(info.get("wins") or 0))
                games = max(0, int(info.get("games") or 0))
                # 旧数据没有 games 字段：每胜至少对应一局，用 wins 兜底，避免出现 >100% 的胜率
                games = max(games, wins)
                cleaned[gtype][qq.strip()] = {
                    "name": str(info.get("name") or ""),
                    "wins": wins,
                    "games": games,
                }
        return cleaned

    def _save_global_stats(self) -> None:
        """将全局胜场数据写回磁盘。"""
        if self.global_stats_path is None:
            return
        try:
            self.global_stats_path.write_text(
                json.dumps(self.global_player_wins, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("[GameCompanion] 全局胜场数据保存失败。")

    def clear_global_stats(self, game_type: str | None = None) -> int:
        """清空全局胜场排行榜：game_type 为空时清空全部游戏类型，否则只清该类型。

        谁是卧底的分阵营胜场（徽章数据源 undercover_camp_wins.json）同步清空，
        避免管理台清完排行榜后徽章仍残留旧胜场。

        返回被清空的游戏类型数量（0 表示没有可清理的数据或未启用持久化）。
        """
        if self.global_stats_path is None:
            return 0
        cleared = 0
        if game_type:
            if game_type not in self.global_player_wins:
                return 0
            del self.global_player_wins[game_type]
            cleared = 1
        else:
            self.global_player_wins.clear()
            cleared = 1
        self._save_global_stats()
        # 谁是卧底分阵营胜场与排行榜同步清空（含清空全部时）
        if (not game_type or game_type == "undercover") and self.undercover_camp_wins:
            self.undercover_camp_wins.clear()
            self._save_undercover_camp_wins()
        return cleared

    def record_operation(
        self,
        action: str,
        detail: str = "",
        operator: str = "",
        *,
        room_id: str = "",
        game_type: str = "",
    ) -> None:
        """把关键动作写入操作日志（JSONL，每条独立成行，失败不抛出）。

        Args:
            action: 动作标识，如 clear_leaderboard / set_wins / close_room / assign。
            detail: 人类可读的动作描述。
            operator: 操作者身份（管理台为「游戏管理台」，房间内为玩家昵称）。
            room_id: 相关房间号（房间内操作与管理台房间操作都带上）。
            game_type: 相关玩法标识（如 gomoku / undercover）。
        """
        if self.operation_log_path is None:
            return
        entry = {
            "ts": time.time(),
            "time": self._format_time(time.time()),
            "action": str(action or ""),
            "detail": str(detail or ""),
            "operator": str(operator or ""),
            "room_id": str(room_id or ""),
            "game_type": str(game_type or ""),
        }
        try:
            with self.operation_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("[GameCompanion] 操作日志写入失败。")

    def operation_log(self, limit: int = 200) -> list[dict[str, object]]:
        """返回最近的操作日志（倒序 limit 条）；无路径或读取失败返回空列表。"""
        if self.operation_log_path is None or not self.operation_log_path.is_file():
            return []
        rows: list[dict[str, object]] = []
        try:
            with self.operation_log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        rows.reverse()
        return rows[: max(0, int(limit))]

    def set_global_wins(self, game_type: str, name: str, wins: int) -> int:
        """手动修正某玩家（按名字合并后）的全局胜场数。

        存储按 QQ 分键、展示按名字合并。修正时把该名字下所有 QQ 条目的胜场清零，
        再把新的总数写入第一条，使合并后的展示总数 == 期望值。
        返回改动了多少条 QQ 记录（0 表示未命中任何玩家）。
        """
        name = str(name or "").strip()
        if (
            self.global_stats_path is None
            or not name
            or not self._is_known_game_type(str(game_type or ""))
        ):
            raise ValueError("请选择已启用的玩法并提供有效玩家名")
        bucket = self.global_player_wins.get(str(game_type), {})
        if not isinstance(bucket, dict):
            bucket = {}
        targets = [qq for qq, info in bucket.items() if str(info.get("name") or "").strip() == name]
        if not targets:
            return 0
        wins = max(0, int(wins))
        for i, qq in enumerate(targets):
            bucket[qq]["wins"] = wins if i == 0 else 0
        self.global_player_wins[str(game_type)] = bucket
        self._save_global_stats()
        self.record_operation(
            "set_wins",
            f"将「{name}」在《{game_type}》的胜场修正为 {wins}",
            operator="游戏管理台",
            game_type=str(game_type),
        )
        return len(targets)

    @staticmethod
    def _format_time(stamp: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))

    def _load_undercover_camp_wins(self) -> dict[str, dict[str, object]]:
        if self.undercover_camp_wins_path is None or not self.undercover_camp_wins_path.is_file():
            return {}
        try:
            raw = json.loads(self.undercover_camp_wins_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        cleaned: dict[str, dict[str, object]] = {}
        valid = {"civilian", "undercover", "whiteboard"}
        for name, camps in raw.items():
            if not isinstance(name, str) or not isinstance(camps, dict):
                continue
            key = name.strip()
            # 旧版按裸昵称存 → 迁移为 name: 前缀；新版真人按 qq: 前缀、AI 按 name: 前缀
            if not (key.startswith("qq:") or key.startswith("name:")):
                key = f"name:{key}"
            entry: dict[str, object] = {}
            for field, val in camps.items():
                if field in valid:
                    entry[field] = max(0, int(val or 0))
                elif field == "name" and isinstance(val, str):
                    entry["name"] = val  # 保留随键存储的显示名，重载后不丢失
            cleaned[key] = entry
        return cleaned

    def _save_undercover_camp_wins(self) -> None:
        if self.undercover_camp_wins_path is None:
            return
        try:
            self.undercover_camp_wins_path.write_text(
                json.dumps(self.undercover_camp_wins, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("[GameCompanion] 卧底分阵营胜场保存失败。")

    def _credit_undercover_camp_wins(self, key: str, name: str, camp: str) -> None:
        """给分阵营胜场 +1：key 为稳定键（真人 qq:xxx / AI name:xxx），
        显示名随 key 一起存，改昵称后胜场仍跟 QQ 走不丢失。"""
        key = (key or "").strip()
        if not key or camp not in {"civilian", "undercover", "whiteboard"}:
            return
        existed = key in self.undercover_camp_wins
        entry = self.undercover_camp_wins.setdefault(key, {})
        if name:
            entry["name"] = name
        # 首次按稳定键记分：把旧版裸昵称迁移出的 name: 数据合并进 QQ 键，
        # 避免升级后第一次胜场徽章数“回退”（旧数据仍在文件里但查不到）
        if not existed and name:
            legacy_key = f"name:{name}"
            legacy = self.undercover_camp_wins.get(legacy_key)
            if isinstance(legacy, dict) and legacy is not entry:
                for camp_key in ("civilian", "undercover", "whiteboard"):
                    entry[camp_key] = (
                        int(entry.get(camp_key, 0) or 0)
                        + int(legacy.get(camp_key, 0) or 0)
                    )
                self.undercover_camp_wins.pop(legacy_key, None)
        entry[camp] = int(entry.get(camp, 0) or 0) + 1
        self._save_undercover_camp_wins()

    @staticmethod
    def _undercover_badges(seat: PlayerSeat, stats: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        """按稳定身份键（QQ / 名字）解锁「藏品/护身符」徽章（纯装饰，不影响对局）。

        与 models._undercover_badges_for 共用同一套阶梯规则。
        """
        key, name = _undercover_identity_key(seat)
        return _undercover_badges_for(key, name, stats)

    async def create_room(
        self,
        *,
        source: RoomSource,
        session_id: str,
        platform: str,
        group_id: str,
        creator_qq: str,
        creator_name: str,
        admin_room: bool,
        game_type: GameType = "gomoku",
        difficulty: Difficulty,
        turtle_soup_mode: TurtleSoupMode = "bot_host",
        bypass_limit: bool = False,
    ) -> GameRoom:
        """Create a room atomically under the source-wide quota.

        bypass_limit 只给游戏管理台「手动开房间」用：管理员在控制台开链接房间时，
        不该被群聊/私聊的并行上限挡住（那两项限制是给普通用户创建房间用的）。
        """
        self.require_game_enabled(game_type)
        async with self._lock:
            limit = (
                self.max_group_rooms if source == "group" else self.max_private_rooms
            )
            active_count = sum(room.source == source for room in self.rooms.values())
            if limit and not bypass_limit and active_count >= limit:
                label = "群聊" if source == "group" else "私聊"
                raise ValueError(f"{label}房间已达到并行上限 {limit}")
            room_id = secrets.token_hex(4)
            while room_id in self.rooms:
                room_id = secrets.token_hex(4)
            access_token = secrets.token_urlsafe(10)
            room = GameRoom(
                room_id=room_id,
                access_token=access_token,
                source=source,
                session_id=session_id,
                platform=platform,
                group_id=group_id,
                creator_qq=creator_qq,
                creator_name=creator_name,
                admin_room=admin_room,
                game_type=game_type,
                difficulty=difficulty,
                turtle_soup_mode=turtle_soup_mode,
                # 围棋盘路跟房间走：建好之后首位入座的玩家还能再改
                weiqi_size=self.weiqi_board_size,
                weiqi_komi=self.weiqi_komi,
                soup_bot_join=self.turtle_soup_bot_join,
            )
            self._configure_multiplayer(room)
            room.camp_wins_store = self.undercover_camp_wins  # 让座位徽章按实时阵营胜场计算
            self.rooms[room_id] = room
            self._access_index[access_token] = room_id
            return room

    def by_access_token(self, access_token: str) -> GameRoom | None:
        """Resolve an unguessable public room token."""
        room_id = self._access_index.get(str(access_token or ""))
        return self.rooms.get(room_id or "")

    def closed_reason_by_access_token(self, access_token: str) -> str:
        """Return a short-lived close reason without retaining room data."""
        self._purge_closed_access()
        record = self._closed_access.get(str(access_token or ""))
        return record[0] if record else ""

    def for_session(self, session_id: str) -> list[GameRoom]:
        """Return rooms attached to one real AstrBot conversation."""
        return [room for room in self.rooms.values() if room.session_id == session_id]

    async def join(
        self,
        room: GameRoom,
        visitor_token: str = "",
        *,
        trusted_identity: TrustedIdentity | None = None,
    ) -> Visitor:
        """Resume a browser identity or assign the next stable room number."""
        async with room.lock:
            visitor = room.visitors.get(str(visitor_token or ""))
            if trusted_identity is not None:
                existing = next(
                    (
                        item
                        for item in room.visitors.values()
                        if item.identity_confirmed and item.qq == trusted_identity.qq
                    ),
                    None,
                )
                if existing is not None:
                    visitor = existing
                elif visitor is None or (
                    visitor.identity_confirmed
                    and visitor.qq != trusted_identity.qq
                ):
                    visitor = Visitor(number=room.next_visitor_number)
                    room.next_visitor_number += 1
                    room.visitors[visitor.token] = visitor
                self._confirm_visitor_identity(
                    room,
                    visitor,
                    trusted_identity.qq,
                    trusted_identity.display_name,
                    allow_trusted_enrollment=False,
                )
            elif visitor is None:
                visitor = Visitor(number=room.next_visitor_number)
                room.next_visitor_number += 1
                room.visitors[visitor.token] = visitor
            visitor.connected = True
            visitor.last_seen_at = time.time()
            visitor.left_at = None
            visitor.ensure_binding_token(ttl=self.IDENTITY_TOKEN_TTL_SECONDS)
            return visitor

    async def bind_visitor_identity(
        self,
        *,
        session_id: str,
        identity_token: str,
        qq: str,
        display_name: str = "",
    ) -> tuple[GameRoom, Visitor]:
        """Consume a browser challenge from the matching QQ conversation."""
        normalized_token = str(identity_token or "").strip().upper()
        normalized_qq = str(qq or "").strip()
        if not normalized_token:
            raise ValueError("身份令牌不能为空")
        if not normalized_qq.isdigit():
            raise ValueError("发送者 QQ 号无效")
        now = time.time()
        candidates = [
            room
            for room in self.rooms.values()
            if room.session_id == str(session_id or "")
        ]
        for room in candidates:
            async with room.lock:
                if room.admin_room:
                    continue
                visitor = next(
                    (
                        item
                        for item in room.visitors.values()
                        if item.binding_token
                        and secrets.compare_digest(
                            item.binding_token.upper(), normalized_token
                        )
                        and item.binding_expires_at > now
                    ),
                    None,
                )
                if visitor is None:
                    continue
                existing = next(
                    (
                        item
                        for item in room.visitors.values()
                        if item.identity_confirmed
                        and item.qq == normalized_qq
                        and item.token != visitor.token
                    ),
                    None,
                )
                if existing is not None:
                    raise ValueError("这个 QQ 已经绑定了本房间的其他访客")
                self._confirm_visitor_identity(
                    room,
                    visitor,
                    normalized_qq,
                    display_name,
                    allow_trusted_enrollment=True,
                )
                room.touch()
                return room, visitor
        raise ValueError("身份令牌无效、已使用或已过期")

    def require_visitor(self, room: GameRoom, visitor_token: str) -> Visitor:
        """校验访客令牌确实属于这个房间（拿到链接的人不能凭链接代别人操作）。"""
        return self._visitor(room, visitor_token)

    def require_seated_player(self, room: GameRoom, visitor_token: str) -> Visitor:
        """校验调用者本人正坐在玩家席上（观众 / 无效令牌一律拒绝）。

        用于「通知到群」「复盘」这类会产生群消息或 LLM 花费的操作：
        仅凭房间链接即可调用的接口，等于把房间的群发能力开放给了任何拿到链接的人。
        """
        visitor = self._visitor(room, visitor_token)
        if room.multiplayer.enabled:
            if room.multiplayer.seat_for_token(visitor_token) is None:
                raise PermissionError("只有玩家席上的玩家可以操作")
        elif room.player_token and visitor_token != room.player_token:
            raise PermissionError("只有玩家可以操作")
        return visitor

    async def require_visitor_identity(
        self, room: GameRoom, visitor_token: str
    ) -> Visitor:
        """Require a browser visitor to have completed QQ binding."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if not visitor.identity_confirmed:
                raise PermissionError("请先在 QQ 中绑定页面令牌，再进入玩家席")
            return visitor

    async def heartbeat(self, room: GameRoom, visitor_token: str) -> Visitor:
        """Refresh presence without extending the room activity deadline."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            visitor.connected = True
            visitor.last_seen_at = time.time()
            visitor.left_at = None
            visitor.ensure_binding_token(ttl=self.IDENTITY_TOKEN_TTL_SECONDS)
            return visitor

    async def leave(self, room: GameRoom, visitor_token: str) -> None:
        """Record a browser departure without extending meaningful activity."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            visitor.connected = False
            visitor.left_at = time.time()

    async def begin_room_chat(
        self, room: GameRoom, visitor_token: str, text: str
    ) -> tuple[Visitor, str, bool, bool]:
        """Publish one UI chat message and return its authoritative room role."""
        cleaned = " ".join(str(text or "").strip().split())
        if not cleaned:
            raise ValueError("消息不能为空")
        if len(cleaned) > 500:
            raise ValueError("单条消息不能超过 500 个字符")
        now = time.time()
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if now - visitor.last_chat_at < self.CHAT_COOLDOWN_SECONDS:
                raise ValueError("发送得太快了，请稍后再试")
            visitor.last_chat_at = now
            is_player = self._is_player_token(room, visitor.token)
            is_current_player = visitor.token == (
                room.multiplayer.current_token
                if room.multiplayer.enabled
                else room.player_token
            )
            room.add_message("user", cleaned, visitor=visitor)
            room.record_chat_memory(visitor, "user", cleaned)
            room.touch()
            return visitor, cleaned, is_player, is_current_player

    async def add_room_chat_reply(
        self, room: GameRoom, visitor: Visitor, text: str, *, message_type: str = "chat"
    ) -> None:
        """Publish a Bot reply and privately associate it with the addressed QQ."""
        async with room.lock:
            if room.status == "closed" or room.room_id not in self.rooms:
                return
            room.add_message("bot", text, message_type=message_type)
            room.record_chat_memory(visitor, "bot", text)

    @staticmethod
    def _is_player_token(room: GameRoom, visitor_token: str) -> bool:
        if room.multiplayer.enabled:
            return room.multiplayer.seat_for_token(visitor_token) is not None
        return bool(room.player_token and room.player_token == visitor_token)

    async def claim_and_start(
        self,
        room: GameRoom,
        visitor_token: str,
        side: str,
        *,
        takeover: bool = False,
    ) -> dict[str, Any]:
        """Claim a normal room's empty player seat and start the first game.

        ``takeover=True`` 表示用户已确认「顶掉对面那个长时间没操作的座位」；
        未确认时如果检测到对手挂机，只返回 ``need_confirm``，不做任何座位变更。
        """
        self.require_game_enabled(room.game_type)
        start_required = False
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            # 管理台开出来的房间没有 QQ 会话，历史设计只能由管理员安排座位；
            # 若访客已经带着身份（受信任浏览器 / 已绑定 QQ），就让他自己入座，
            # 否则管理台每来一个人都要手动点一次「安排玩家」。
            if room.admin_room and not visitor.identity_confirmed:
                raise ValueError("这个房间需要管理员从游戏管理台安排玩家")
            if room.multiplayer.enabled:
                duel = room.game_type in DUEL_GAMES
                # 局内已经按开局号码记账，中途再塞人会让座位号与局内号错位，直接拒绝
                round_running = bool(
                    isinstance(room.game, (BlackjackGame, UndercoverGame))
                    and not room.game.finished
                )
                if room.status == "active" and (duel or round_running):
                    raise ValueError("本局进行中，请等这一局结束后再加入玩家席")
                if duel and room.status == "paused":
                    raise ValueError("对局已暂停，结束后再加入玩家席")
                if room.multiplayer.seat_for_token(visitor.token) is not None:
                    raise ValueError("你已经在玩家席")
                if duel:
                    # 两人对弈：先坐下、再决定对手。空位可以等真人，也可以让花火补位；
                    # 花火已经坐着时真人来了就让位（局间才会让，对局中上面已拦住）。
                    capacity = int(room.multiplayer.capacity or 2)
                    now = time.time()
                    # 坐下时会和对面直接开局：先确认对面不是「页面开着但人走了」的
                    # 挂机座位，否则会被锁进一局没人应手的棋，还要等 180 秒判负。
                    afk_seat = self._duel_afk_seat(room, now)
                    if afk_seat is not None:
                        if not takeover:
                            last = float(
                                getattr(afk_seat, "last_action_at", 0.0)
                                or afk_seat.seated_at
                            )
                            return {
                                "need_confirm": True,
                                "opponent_name": afk_seat.display_name
                                or f"{afk_seat.number}号",
                                "opponent_idle_seconds": int(max(0.0, now - last)),
                            }
                        afk_visitor = room.visitors.get(afk_seat.visitor_token)
                        label = (
                            self._visitor_label(afk_visitor)
                            if afk_visitor is not None
                            else "对手"
                        )
                        self._remove_multiplayer_seat(room, afk_seat.visitor_token)
                        self._assign_duel_sides(room)
                        room.add_message(
                            "system", f"{label} 长时间未操作，已让出座位。"
                        )
                    vacated_ai: PlayerSeat | None = None
                    if len(room.multiplayer.seats) >= capacity:
                        vacated_ai = self._ai_seat(room)
                        if vacated_ai is None:
                            raise ValueError("玩家席已经坐满")
                        room.multiplayer.seats = [
                            s for s in room.multiplayer.seats if not s.is_ai
                        ]
                        room.visitors.pop(vacated_ai.visitor_token, None)
                    room.multiplayer.seats.append(
                        PlayerSeat(
                            visitor_token=visitor.token,
                            qq=visitor.qq if visitor.identity_confirmed else "",
                            display_name=visitor.display_name,
                            identity_confirmed=visitor.identity_confirmed,
                        )
                    )
                    if visitor.identity_confirmed:
                        room.confirmed_participant_qqs.add(visitor.qq)
                        room.participant_names[visitor.qq] = visitor.display_name
                    self._sync_primary_player(room)
                    self._assign_duel_sides(room)
                    room.player_empty_since = None
                    room.status = "setup"
                    room.touch()
                    if vacated_ai is not None:
                        room.add_message(
                            "system",
                            f"{vacated_ai.display_name}把位置让给了"
                            f"{self._visitor_label(visitor)}，准备开始双人对局。",
                        )
                    else:
                        room.add_message(
                            "system",
                            f"{self._visitor_label(visitor)}入座。"
                            "还差一位对手，也可以直接和花火对局。",
                        )
                    # 两人到齐立即开局；只有一个人时停在 setup，等第二个座位有主
                    start_required = (
                        len(room.multiplayer.seats) >= capacity
                    )
                else:
                    if (
                        room.multiplayer.capacity
                        and len(room.multiplayer.seats) >= room.multiplayer.capacity
                    ):
                        raise ValueError("玩家席已经坐满")
                    first = not room.multiplayer.seats
                    room.multiplayer.seats.append(
                        PlayerSeat(
                            visitor_token=visitor.token,
                            qq=visitor.qq if visitor.identity_confirmed else "",
                            display_name=visitor.display_name,
                            identity_confirmed=visitor.identity_confirmed,
                        )
                    )
                    if visitor.identity_confirmed:
                        room.confirmed_participant_qqs.add(visitor.qq)
                        room.participant_names[visitor.qq] = visitor.display_name
                    self._sync_primary_player(room)
                    room.player_empty_since = None
                    room.touch()
                    if first:
                        room.status = "setup"
                        start_required = not (
                            (room.game_type == "blackjack" and room.multiplayer.capacity > 1)
                            or room.game_type == "undercover"
                            # 海龟汤是多人合力解谜：第一个人入座就开局的话，
                            # 后面的人根本来不及坐进来。多人房改为等房主点开始，
                            # 只有单人房（capacity == 1）才自动开局。
                            # 注意用 == 1 而不是 > 1：capacity=0 表示不限人数，
                            # 那显然不是单人房，不能被判成「入座即开局」。
                            or (
                                room.game_type == "turtle_soup"
                                and int(room.multiplayer.capacity or 0) != 1
                            )
                        )
                        if room.game_type == "undercover":
                            # 进入等待匹配阶段：设置倒计时 match_seconds 后自动开局
                            duration = max(10, int(self.undercover_match_seconds or 60))
                            state = room.multiplayer
                            state.turn_timeout_seconds = duration
                            state.turn_deadline = time.time() + duration
                    else:
                        room.add_message(
                            "system", f"{self._visitor_label(visitor)}加入了玩家席。"
                        )
                        if room.game_type == "undercover" and room.status == "setup":
                            # 人满时立即触发开局
                            live_count = sum(
                                1
                                for s in room.multiplayer.seats
                                if s.visitor_token
                                and (s.is_ai or s.visitor_token in room.visitors)
                            )
                            full = (
                                room.multiplayer.capacity > 0
                                and live_count >= room.multiplayer.capacity
                            )
                            if full:
                                room.multiplayer.turn_deadline = time.time()
                            elif room.multiplayer.turn_deadline:
                                # 有新玩家加入时开局倒计时顺延 10 秒，避免仓促开局
                                room.multiplayer.turn_deadline += 10
                                room.touch()
                    if room.game_type != "undercover" or room.status != "setup":
                        # 谁是卧底 setup 阶段使用 match_seconds 倒计时，不被这里重置
                        self._reset_turn_deadline(room)
            else:
                if room.player_token and room.player_token != visitor.token:
                    raise ValueError("玩家席已经有人，请联系创建者处理")
                if room.player_seat_locked and room.player_token != visitor.token:
                    raise ValueError("玩家席已由创建者锁定")
                room.player_token = visitor.token
                room.player_qq = visitor.qq if visitor.identity_confirmed else ""
                room.player_identity_confirmed = visitor.identity_confirmed
                if visitor.identity_confirmed:
                    room.confirmed_participant_qqs.add(visitor.qq)
                    room.participant_names[visitor.qq] = visitor.display_name
                room.player_empty_since = None
                room.status = "setup"
                room.touch()
                start_required = True
        if start_required:
            await self.start_game(room, visitor_token, side)
        else:
            await self._emit("seats_changed", room, {"joined": visitor.number})
        return {"claimed": True, "seated": True}

    async def assign_player(
        self,
        room: GameRoom,
        visitor_number: int,
        player_qq: str,
        *,
        allow_non_numeric: bool = False,
    ) -> None:
        """Assign an administrator-reviewed visitor and QQ identity."""
        async with room.lock:
            visitor = self._visitor_by_number(room, visitor_number)
            player_qq = str(player_qq or "").strip()
            if not player_qq:
                raise ValueError("玩家身份不能为空")
            if not allow_non_numeric and not player_qq.isdigit():
                raise ValueError("玩家 QQ 号必须只包含数字")
            if any(
                item.identity_confirmed
                and item.qq == player_qq
                and item.token != visitor.token
                for item in room.visitors.values()
            ):
                raise ValueError("这个 QQ 已经绑定了本房间的其他访客")
            if room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_token(visitor.token)
                if seat is None:
                    if (
                        room.status == "active"
                        and isinstance(room.game, (BlackjackGame, UndercoverGame))
                        and not room.game.finished
                    ):
                        raise ValueError("本局进行中，不能新增玩家席，请等这一局结束")
                    if (
                        room.multiplayer.capacity
                        and len(room.multiplayer.seats) >= room.multiplayer.capacity
                    ):
                        raise ValueError("玩家席已经坐满")
                    seat = PlayerSeat(visitor_token=visitor.token)
                    room.multiplayer.seats.append(seat)
                seat.qq = player_qq
                seat.display_name = visitor.display_name
                seat.identity_confirmed = True
                visitor.qq = player_qq
                if not visitor.display_name and player_qq == room.creator_qq:
                    visitor.display_name = room.creator_name
                    seat.display_name = visitor.display_name
                visitor.identity_confirmed = True
                visitor.binding_token = ""
                visitor.binding_expires_at = 0.0
                room.confirmed_participant_qqs.add(player_qq)
                room.participant_names[player_qq] = visitor.display_name
                first = room.player_token == ""
                self._sync_primary_player(room)
                room.player_seat_locked = True
                room.player_empty_since = None
                if room.game is None and room.status == "waiting":
                    room.status = "setup"
                if first:
                    room.status = "setup"
                # 谁是卧底 setup 阶段用 match_seconds 倒计时自动开局，不要在这里被重置覆盖
                if room.game_type != "undercover" or room.status != "setup":
                    self._reset_turn_deadline(room)
                room.touch()
            else:
                room.player_token = visitor.token
                room.player_qq = player_qq
                room.player_identity_confirmed = True
                visitor.qq = player_qq
                if not visitor.display_name and player_qq == room.creator_qq:
                    visitor.display_name = room.creator_name
                visitor.identity_confirmed = True
                visitor.binding_token = ""
                visitor.binding_expires_at = 0.0
                room.confirmed_participant_qqs.add(player_qq)
                room.participant_names[player_qq] = visitor.display_name
                room.player_seat_locked = True
                room.player_empty_since = None
                room.game = None
                room.status = "setup"
                room.touch()
        await self._emit("player_confirmed", room, {})

    async def correct_creator(
        self, room: GameRoom, actor_qq: str, visitor_number: int
    ) -> None:
        """Reset a stolen normal room and swap the creator into the player seat."""
        async with room.lock:
            if room.admin_room:
                raise ValueError("管理员房间请从游戏管理台重新安排玩家")
            if str(actor_qq) != room.creator_qq:
                raise PermissionError("只有房间创建者能纠正玩家身份")
            visitor = self._visitor_by_number(room, visitor_number)
            if room.multiplayer.enabled:
                seats = room.multiplayer.seats
                target_index = next(
                    (
                        index
                        for index, seat in enumerate(seats)
                        if seat.visitor_token == visitor.token
                    ),
                    -1,
                )
                creator_seat = PlayerSeat(
                    visitor_token=visitor.token,
                    qq=room.creator_qq,
                    display_name=visitor.display_name,
                    identity_confirmed=True,
                )
                if seats:
                    previous_primary = seats[0]
                    seats[0] = creator_seat
                    if target_index > 0:
                        seats[target_index] = PlayerSeat(
                            visitor_token=previous_primary.visitor_token,
                            qq=room.visitors.get(previous_primary.visitor_token).qq
                            if room.visitors.get(previous_primary.visitor_token)
                            and room.visitors.get(previous_primary.visitor_token).identity_confirmed
                            else "",
                            display_name=room.visitors.get(previous_primary.visitor_token).display_name
                            if room.visitors.get(previous_primary.visitor_token)
                            else "",
                            identity_confirmed=bool(
                                room.visitors.get(previous_primary.visitor_token)
                                and room.visitors.get(previous_primary.visitor_token).identity_confirmed
                            ),
                        )
                else:
                    seats.append(creator_seat)
                room.multiplayer.current_turn_index = 0
                room.multiplayer.swap_requests.clear()
                self._sync_primary_player(room)
                room.confirmed_participant_qqs.add(room.creator_qq)
                room.participant_names[room.creator_qq] = (
                    visitor.display_name or room.creator_name
                )
            else:
                room.player_token = visitor.token
                room.player_qq = room.creator_qq
                room.player_identity_confirmed = True
                room.confirmed_participant_qqs.add(room.creator_qq)
                room.participant_names[room.creator_qq] = (
                    visitor.display_name or room.creator_name
                )
            room.player_seat_locked = True
            room.player_empty_since = None
            room.game = None
            room.status = "setup"
            room.touch()
            self._reset_turn_deadline(room)
            room.add_message(
                "system", f"身份已纠正：{visitor.number} 号成为玩家，对局已重置。"
            )
        await self._emit("player_confirmed", room, {"corrected": True})

    async def confirm_creator(self, room: GameRoom, actor_qq: str) -> None:
        """Confirm that the current browser player is the room creator."""
        async with room.lock:
            if str(actor_qq) != room.creator_qq:
                raise PermissionError("只有房间创建者能确认自己的身份")
            if not room.player_token:
                raise ValueError("当前还没有人在玩家席")
            if room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_token(room.player_token)
                if seat is None:
                    raise ValueError("当前还没有主玩家席")
                seat.qq = room.creator_qq
                seat.display_name = room.creator_name
                seat.identity_confirmed = True
                player = room.visitors.get(seat.visitor_token)
                if player is not None:
                    player.qq = room.creator_qq
                    player.display_name = room.creator_name
                    player.identity_confirmed = True
                    player.binding_token = ""
                    player.binding_expires_at = 0.0
                room.confirmed_participant_qqs.add(room.creator_qq)
                room.participant_names[room.creator_qq] = room.creator_name
                self._sync_primary_player(room)
            else:
                room.player_qq = room.creator_qq
                room.player_identity_confirmed = True
                player = room.player
                if player is not None:
                    player.qq = room.creator_qq
                    player.display_name = room.creator_name
                    player.identity_confirmed = True
                    player.binding_token = ""
                    player.binding_expires_at = 0.0
                room.confirmed_participant_qqs.add(room.creator_qq)
                room.participant_names[room.creator_qq] = room.creator_name
            room.player_seat_locked = True
            room.touch()
        await self._emit("player_confirmed", room, {})

    async def _setup_undercover_game(
        self, room: GameRoom, word_pair: tuple[str, str] | None = None
    ) -> str:
        """初始化谁是卧底：AI 补位、重编座位号、取词条并分配身份。返回固定空 side_label。

        词条可能走 LLM（最长数秒），调用方应先在锁外取好并通过 word_pair 传入；
        未传入时才在当前位置兜底取词（例如老调用路径）。
        """
        if not room.multiplayer.enabled:
            raise ValueError("谁是卧底必须在多人模式下进行")
        live_seats = [
            seat
            for seat in room.multiplayer.seats
            if seat.is_ai or seat.visitor_token in room.visitors
        ]
        # AI 补位：人数少于 ai_fill_min_players 时追加 AI 玩家
        if (
            self.undercover_ai_fill_enabled
            and len(live_seats) < self.undercover_ai_fill_min_players
        ):
            import uuid
            extra = self.undercover_ai_fill_min_players - len(live_seats)
            # AI 编号全局递增：基于已存在的 AI 席位计数，避免多次补位都叫“AI1号”
            base_ai = sum(1 for s in room.multiplayer.seats if s.is_ai)
            for idx in range(extra):
                room.multiplayer.capacity = max(
                    room.multiplayer.capacity,
                    len(room.multiplayer.seats) + 1,
                )
                ai_token = f"ai-{uuid.uuid4().hex[:8]}"
                ai_display, _ = _uc_ai_name()  # 例如「火花·调皮(AI)」
                ai_seat = PlayerSeat(
                    number=len(room.multiplayer.seats) + 1,
                    visitor_token=ai_token,
                    qq=ai_token,
                    display_name=ai_display,
                    identity_confirmed=True,
                    is_ai=True,
                    ready=True,  # AI 自动算作已准备
                )
                # 注册 AI 到 visitors，保持玩家可见性一致
                ai_v = Visitor(
                    number=room.next_visitor_number,
                    token=ai_token,
                    qq=ai_token,
                    display_name=ai_display,
                    identity_confirmed=True,
                )
                ai_v.last_seen_at = time.time()
                ai_v.connected = True
                room.next_visitor_number += 1
                room.visitors[ai_token] = ai_v
                room.multiplayer.seats.append(ai_seat)
                live_seats.append(ai_seat)
        # 最终按列表顺序重编一次 seat.number，保证真人/AI 编号连续 1..N
        for idx, seat in enumerate(room.multiplayer.seats):
            seat.number = idx + 1
        self._sync_visitor_numbers(room)
        if len(live_seats) < self.undercover_min_players:
            raise ValueError(
                f"谁是卧底至少需要 {self.undercover_min_players} 位已入座玩家"
            )
        if word_pair is None:
            word_pair = await self._fetch_undercover_word_pair(room)
        # 阵营比例：优先房主自定义覆盖，否则默认
        camp_scales = self.undercover_camp_scales_default
        host_scales = getattr(room, "undercover_host_camp_scales", None)
        if isinstance(host_scales, str) and host_scales.strip():
            parsed = _parse_camp_scales(host_scales.strip())
            if parsed[1] > 0:  # 卧底数必须保证>0
                camp_scales = parsed
        room.game = UndercoverGame(
            camp_scales=camp_scales,
            first_round_non_voting=self.undercover_first_round_non_voting,
            similarity=self.undercover_similarity,
            reveal_identity=(
                room.undercover_reveal_identity
                if room.undercover_reveal_identity is not None
                else self.undercover_send_identity_in_card
            ),
            show_voters=self.undercover_show_voters,
        )
        room.game.attach_players(
            [(seat.number, seat.qq, seat.display_name) for seat in live_seats],
            [_visitor_uid(seat.visitor_token) for seat in live_seats],
        )
        room.game.assign_words(word_pair)
        room.multiplayer.current_turn_index = 0
        return ""

    async def start_game(self, room: GameRoom, visitor_token: str, side: str) -> None:
        """Start a game after a seat has been assigned."""
        self.require_game_enabled(room.game_type)
        # 谁是卧底需要在开局前取词条，可能调用 LLM（最长数秒）。这里在进入
        # room.lock 之前先把词取好，避免持有房间锁期间 await 网络/模型：
        # 一方面会锁死本房间的其它操作，另一方面 start_game 常被串行的
        # sweep_expired 链路调用，卡住会拖慢之后所有房间的清扫。
        # 取词前先做一次与锁内一致的轻量预检，尽量不浪费一次 LLM 调用；
        # 真正的鉴权/状态校验仍在锁内，取词完成后会再校验一遍。
        word_pair: tuple[str, str] | None = None
        if room.game_type == "undercover":
            visitor = self._visitor(room, visitor_token)
            if room.multiplayer.enabled:
                allowed = room.multiplayer.seat_for_token(visitor.token) is not None
            else:
                allowed = visitor.token == room.player_token
            if not allowed:
                raise PermissionError("只有当前玩家可以开始对局")
            if room.status not in {"setup", "finished", "rematch_pending"}:
                raise ValueError("当前房间状态不能开始新对局")
            word_pair = await self._fetch_undercover_word_pair(room)
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if room.multiplayer.enabled:
                allowed = room.multiplayer.seat_for_token(visitor.token) is not None
            else:
                allowed = visitor.token == room.player_token
            if not allowed:
                raise PermissionError("只有当前玩家可以开始对局")
            if room.status not in {"setup", "finished", "rematch_pending"}:
                raise ValueError("当前房间状态不能开始新对局")
            # 点「开始 / 再来一局」也算一次真实操作，刷新挂机让座判定
            self._touch_seat_action(room, visitor.token)
            if room.game_type == "xiangqi":
                engine = self._require_xiangqi_engine()
                # 空字符串表示「沿用入座时选好的执方」，不能当成默认红方覆盖掉
                normalized_side = str(side or "").strip().lower()
                if normalized_side not in {"human_red", "human_black", "random"}:
                    normalized_side = ""
                if normalized_side == "random":
                    normalized_side = secrets.choice(("human_red", "human_black"))
                if room.multiplayer.enabled:
                    # 两位真人同房：1 号执红先走、2 号执黑；只有一个人时花火补位。
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 换先：上一局执红的下局执黑；首局/换人后按入座时选的红黑
                    self._prepare_duel_sides(room, requested_side=normalized_side)
                    human_seats = self._human_seats(room)
                    human_side = self._seat_side(
                        room, human_seats[0] if human_seats else None
                    ) or XIANGQI_RED
                else:
                    human_side = (
                        XIANGQI_BLACK
                        if normalized_side == "human_black"
                        else XIANGQI_RED
                    )
                room.game = await XiangqiGame.create(
                    engine,
                    human_side=human_side,
                    difficulty=room.difficulty,
                )
                side_label = "红" if human_side == XIANGQI_RED else "黑"
            elif room.game_type == "weiqi":
                # 围棋：与五子棋同构（1 号黑、2 号白），只是多了盘路与贴目
                if room.multiplayer.enabled:
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 换先：上一局执黑的下局执白；首局/换人后按入座选的先后手
                    self._prepare_duel_sides(room, requested_side=str(side or ""))
                    human_color = self._duel_human_color(room)
                else:
                    normalized_side = str(side or "human_black").strip().lower()
                    if normalized_side not in {"human_black", "bot_black", "random"}:
                        normalized_side = "human_black"
                    if normalized_side == "random":
                        normalized_side = secrets.choice(
                            ("human_black", "bot_black")
                        )
                    human_color = (
                        BLACK if normalized_side == "human_black" else WHITE
                    )
                room.game = WeiqiGame(
                    human_color=human_color,
                    difficulty=room.difficulty,
                    size=room.weiqi_size,
                    komi=room.weiqi_komi,
                )
                side_label = "黑" if human_color == BLACK else "白"
            elif room.game_type in DUEL_GAMES:
                # 两人对弈：由座位决定执色（1 号黑、2 号白）。
                # 只有一个座位时自动让花火补位；已有对局历史时换先（黑白互换）。
                game_class = (
                    GomokuGame if room.game_type == "gomoku" else OthelloGame
                )
                if room.multiplayer.enabled:
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 换先：上一局执黑的下局执白；首局/换人后按入座选的先后手
                    self._prepare_duel_sides(room, requested_side=str(side or ""))
                    human_color = self._duel_human_color(room)
                else:
                    # 兼容未启用玩家席的历史房间
                    normalized_side = str(side or "human_black").strip().lower()
                    if normalized_side not in {"human_black", "bot_black", "random"}:
                        normalized_side = "human_black"
                    if normalized_side == "random":
                        normalized_side = secrets.choice(
                            ("human_black", "bot_black")
                        )
                    human_color = (
                        BLACK if normalized_side == "human_black" else WHITE
                    )
                room.game = game_class(
                    human_color=human_color, difficulty=room.difficulty
                )
                side_label = "黑" if human_color == BLACK else "白"
            elif room.game_type == "tictactoe":
                normalized_side = str(side or "human_x").strip().lower()
                if normalized_side not in {"human_x", "human_o", "random"}:
                    normalized_side = "human_x"
                if normalized_side == "random":
                    normalized_side = secrets.choice(("human_x", "human_o"))
                human_mark = (
                    TICTACTOE_X if normalized_side == "human_x" else TICTACTOE_NOUGHT
                )
                room.game = TicTacToeGame(
                    human_mark=human_mark, difficulty=room.difficulty
                )
                side_label = "X" if human_mark == TICTACTOE_X else "O"
            elif room.game_type == "pig_dice":
                room.game = PigDiceGame(
                    difficulty=room.difficulty,
                    target_score=self.pig_dice_target_score,
                )
                side_label = ""
            elif room.game_type == "draw_guess":
                room.game = DrawGuessGame(
                    difficulty=room.difficulty,
                    max_guesses=self.draw_guess_max_guesses,
                    duration_seconds=self.draw_guess_duration_seconds,
                )
                side_label = ""
            elif room.game_type == "blackjack":
                room.game = self._deal_blackjack(room)
                room.multiplayer.current_turn_index = 0
                side_label = ""
            elif room.game_type == "undercover":
                side_label = await self._setup_undercover_game(room, word_pair)
            else:
                room.game = TurtleSoupGame(
                    difficulty=room.difficulty,
                    max_hints=self.turtle_soup_max_hints,
                    content_level=self.turtle_soup_content_level,
                    mode=room.turtle_soup_mode,
                )
                # 正常开局（花火出题）要清掉没提交的出题申请，否则面板会一直显示
                # 「X 正在出题」，而这一局其实已经由花火出了
                room.soup_host_number = 0
                side_label = ""
            room.status = "active"
            room.touch()
            if isinstance(room.game, TurtleSoupGame):
                room.add_message(
                    "system",
                    "Bot 正在准备一道新的海龟汤。"
                    if room.game.mode == "bot_host"
                    else "玩家出题模式开始，请当前玩家提供第一条公开线索。",
                )
            elif isinstance(room.game, PigDiceGame):
                first = "玩家" if room.game.turn == "human" else "Bot"
                room.add_message(
                    "system", f"新一局贪心骰子开始，由{first}先掷，目标 50 分。"
                )
            elif isinstance(room.game, DrawGuessGame):
                room.add_message(
                    "system",
                    f"你画我猜开始。玩家有 {room.game.duration_seconds} 秒作画，"
                    f"可让 Bot 猜 {room.game.max_guesses} 次。",
                )
            elif isinstance(room.game, BlackjackGame):
                room.add_message(
                    "system",
                    "新一局二十一点开始，Bot 是庄家。轮到当前玩家要牌或停牌。",
                )
            elif isinstance(room.game, UndercoverGame):
                counts = room.game.camp_counts()
                civ = counts.get("civilian", 0)
                uc = counts.get("undercover", 0)
                wb = counts.get("whiteboard", 0)
                parts = [f"平民 {civ} 人", f"卧底 {uc} 人"]
                if wb:
                    parts.append(f"白板 {wb} 人")
                room.add_message(
                    "system",
                    "新一局谁是卧底开始。"
                    + "、".join(parts)
                    + "。按座位顺序依次发言描述你的词条。",
                )
            else:
                if room.game_type in DUEL_GAMES and room.multiplayer.enabled:
                    if room.game_type == "xiangqi":
                        red_seat = self._duel_seat_for_side(room, "red")
                        black_seat = self._duel_seat_for_side(room, "black")
                        if red_seat is not None and black_seat is not None:
                            room.add_message(
                                "system",
                                f"新对局开始，{self._seat_display(red_seat)}执红先走，"
                                f"{self._seat_display(black_seat)}执黑。",
                            )
                        else:
                            room.add_message(
                                "system", f"新对局开始，玩家执{side_label}。"
                            )
                    else:
                        black_seat = next(
                            (
                                s
                                for s in room.multiplayer.seats
                                if self._seat_color(room, s) == BLACK
                            ),
                            None,
                        )
                        white_seat = next(
                            (
                                s
                                for s in room.multiplayer.seats
                                if self._seat_color(room, s) == WHITE
                            ),
                            None,
                        )
                        if black_seat is not None and white_seat is not None:
                            room.add_message(
                                "system",
                                f"新对局开始，{self._seat_display(black_seat)}执黑，"
                                f"{self._seat_display(white_seat)}执白。",
                            )
                        else:
                            room.add_message(
                                "system",
                                f"新对局开始，玩家执{side_label}。",
                            )
                else:
                    room.add_message(
                        "system",
                        f"新对局开始，玩家执{side_label}，Bot 使用{self._difficulty_label(room.difficulty)}棋力。",
                    )
        if isinstance(room.game, TurtleSoupGame) and room.game.mode == "bot_host":
            await self._emit("soup_generation_requested", room, {})
            return
        if isinstance(room.game, TurtleSoupGame):
            async with room.lock:
                self._reset_turn_deadline(room)
        if isinstance(room.game, BlackjackGame):
            async with room.lock:
                if not room.game.finished:
                    self._advance_blackjack_turn(room)
                self._reset_turn_deadline(room)
            if room.game.finished:
                await self._finish_game(room)
                return
            if room.game.all_players_done():
                self._spawn_blackjack_dealer_turn(room)
                return
        if isinstance(room.game, UndercoverGame):
            async with room.lock:
                # 开局前准备期不强制推进，前端有 countdown 提示；
                # 但 turn deadline 设为发言轮次结束时
                self._reset_turn_deadline(room)
                await self._emit(
                    "undercover_game_started",
                    room,
                    {
                        "camp_counts": room.game.camp_counts(),
                        "words_assigned": True,
                    },
                )
        if isinstance(room.game, BOARD_GAME_CLASSES):
            async with room.lock:
                # 开局第一步就开始计时（轮到花火时倒计时会留空）
                room.multiplayer.duel_paused = False
                room.multiplayer.duel_pause_deadline = 0.0
                self._reset_turn_deadline(room)
        await self._emit("game_started", room, {})
        if self._is_bot_turn(room):
            self._spawn_bot_turn(room)

    async def player_move(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        row: int = -1,
        column: int = -1,
        from_row: int = -1,
        from_column: int = -1,
        to_row: int = -1,
        to_column: int = -1,
    ) -> None:
        """Apply one browser move, then calculate the Bot response off-loop."""
        board_event: dict[str, object] = {"actor": "human"}
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            seat: PlayerSeat | None = None
            if room.multiplayer.enabled:
                # 两人对弈：鉴权按「当前浏览器是否坐在某个玩家席」，而不是主玩家 token
                seat = room.multiplayer.seat_for_token(visitor.token)
                if seat is None or seat.is_ai:
                    raise PermissionError("当前浏览器不在玩家席")
            elif visitor.token != room.player_token:
                raise PermissionError("当前浏览器不在玩家席")
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有正在进行的对局")
            if isinstance(room.game, (TurtleSoupGame, PigDiceGame, DrawGuessGame)):
                raise ValueError("当前游戏不使用棋盘落子接口")
            if isinstance(room.game, XiangqiGame):
                # 两位真人同房：这一手属于哪个座位，就按那个座位的执方校验
                side = (
                    self._seat_side(room, seat)
                    if seat is not None
                    else room.game.human_side
                )
                board_event.update(
                    from_row=int(from_row),
                    from_column=int(from_column),
                    to_row=int(to_row),
                    to_column=int(to_column),
                )
                await room.game.place_side(
                    self._require_xiangqi_engine(),
                    side or room.game.human_side,
                    int(from_row),
                    int(from_column),
                    int(to_row),
                    int(to_column),
                )
            elif isinstance(room.game, WeiqiGame):
                color = self._seat_color(room, seat) if seat else EMPTY
                if not color:
                    color = room.game.human_color
                if room.game.turn != color:
                    raise ValueError("还没轮到你落子")
                captured = room.game.place(int(row), int(column), color)
                board_event.update(
                    row=int(row),
                    column=int(column),
                    color=color,
                    captured=int(captured),
                    captured_points=[
                        list(point) for point in room.game.last_captured_points
                    ],
                    eval=room.game.evaluate(),
                )
            elif isinstance(room.game, GomokuGame):
                color = self._seat_color(room, seat) if seat else EMPTY
                if not color:
                    color = room.game.human_color
                if room.game.turn != color:
                    raise ValueError("还没轮到你落子")
                room.game.place(int(row), int(column), color)
                board_event.update(
                    row=int(row),
                    column=int(column),
                    color=color,
                )
            elif isinstance(room.game, TicTacToeGame):
                room.game.place(int(row), int(column), room.game.human_mark)
                board_event.update(
                    row=int(row),
                    column=int(column),
                    color=room.game.human_mark,
                )
            elif isinstance(room.game, OthelloGame):
                color = self._seat_color(room, seat) if seat else EMPTY
                if not color:
                    color = room.game.human_color
                if room.game.turn != color:
                    raise ValueError("还没轮到你落子")
                room.game.place(int(row), int(column), color)
                board_event.update(
                    row=int(row),
                    column=int(column),
                    color=color,
                    passed_color=room.game.last_passed,
                    eval=room.game.evaluate(),
                )
                if room.game.last_passed:
                    passed = (
                        "黑"
                        if room.game.last_passed == OTHELLO_BLACK
                        else "白"
                    )
                    room.add_message(
                        "system", f"{passed}方没有合法落子，本回合跳过。"
                    )
            else:
                raise ValueError("当前游戏不支持棋盘落子")
            if seat is not None:
                # 解说要知道这一手属于哪个座位：两位真人时说明「谁走的」，而非笼统的「玩家」
                board_event.update(
                    seat_number=seat.number,
                    seat_name=self._seat_display(seat),
                    side=self._seat_side(room, seat),
                )
                # 真实落子 = 人在电脑前，刷新挂机判定
                seat.last_action_at = time.time()
            room.touch()
            finished = room.game.finished
            if isinstance(room.game, BOARD_GAME_CLASSES) and not finished:
                # 走子即代表本人回来了：解除掉线暂停，并把倒计时交给下一位
                room.multiplayer.duel_paused = False
                room.multiplayer.duel_pause_deadline = 0.0
                self._reset_turn_deadline(room)
        await self._emit("board_changed", room, board_event)
        if finished:
            await self._finish_game(room)
            return
        if self._is_bot_turn(room):
            # 后台执行：玩家自己的落子先回给浏览器，花火「想」完再落
            self._spawn_bot_turn(room)

    async def player_pass(self, room: GameRoom, visitor_token: str) -> None:
        """围棋停一手：双方连续停手即按数子法结算。"""
        board_event: dict[str, object] = {"actor": "human", "passed": True}
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            seat: PlayerSeat | None = None
            if room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_token(visitor.token)
                if seat is None or seat.is_ai:
                    raise PermissionError("当前浏览器不在玩家席")
            elif visitor.token != room.player_token:
                raise PermissionError("当前浏览器不在玩家席")
            if room.status != "active" or not isinstance(room.game, WeiqiGame):
                raise ValueError("当前没有正在进行的围棋对局")
            game = room.game
            color = self._seat_color(room, seat) if seat else game.human_color
            if game.turn != color:
                raise ValueError("还没轮到你停手")
            finished = game.pass_turn(color)
            board_event.update(color=color, eval=game.evaluate())
            if seat is not None:
                board_event.update(
                    seat_number=seat.number,
                    seat_name=self._seat_display(seat),
                    side=self._seat_side(room, seat),
                )
                seat.last_action_at = time.time()
            room.touch()
            if not finished:
                room.multiplayer.duel_paused = False
                room.multiplayer.duel_pause_deadline = 0.0
                self._reset_turn_deadline(room)
        await self._emit("board_changed", room, board_event)
        if finished:
            await self._finish_game(room)
            return
        if self._is_bot_turn(room):
            # 后台执行：玩家自己的落子先回给浏览器，花火「想」完再落
            self._spawn_bot_turn(room)

    async def set_weiqi_board(
        self,
        room: GameRoom,
        visitor_token: str,
        size: int,
        komi: float | None = None,
    ) -> None:
        """调整围棋盘路。

        只有「最早落座的那位真人」能在开局前改：盘路一换，整盘重新铺，
        所以已经落过子就不允许再改——半盘 13 路直接变 19 路会让棋谱失去意义。

        贴目（komi）只允许管理员改：它对胜负的影响是线性的，普通玩家在控制台
        传一个 30 就等于自带 30 目优势。普通房间固定用插件配置的贴目。
        """
        if room.game_type != "weiqi":
            raise ValueError("只有围棋房间可以调整盘路")
        try:
            normalized = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError("盘路只能是 9 / 13 / 19") from exc
        if normalized not in WEIQI_SIZES:
            raise ValueError("盘路只能是 9 / 13 / 19")
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if not self._is_first_seated_human(room, visitor.token):
                raise PermissionError("只有最先入座的玩家可以调整盘路")
            # 对局进行中一律不许动规则参数：以前只拦「已经落过子」，
            # 开局后还没落子的那几十秒仍能把贴目改成 30。
            if room.status in {"active", "paused"}:
                raise ValueError("对局已经开始，盘路只能在开局前调整")
            game = room.game
            if isinstance(game, WeiqiGame) and game.history and room.status != "finished":
                raise ValueError("对局已经开始，盘路只能在开局前调整")
            if room.weiqi_size == normalized and komi is None:
                return
            if komi is not None:
                if not room.admin_room:
                    raise PermissionError("贴目由房间设置决定，玩家不能修改")
                room.weiqi_komi = round(max(0.0, min(float(komi), 30.0)) * 2) / 2
            room.weiqi_size = normalized
            # 已经建好但还没落子的对局：就地换一块新棋盘，保留执色与棋力。
            # 上一局刚结束时保留原棋盘（那是复盘用的结果），新盘路等再来一局生效。
            if isinstance(game, WeiqiGame) and room.status != "finished":
                room.game = WeiqiGame(
                    human_color=game.human_color,
                    difficulty=game.difficulty,
                    size=normalized,
                    komi=room.weiqi_komi,
                )
            room.touch()
            room.add_message(
                "system",
                f"棋盘调整为 {normalized} 路（黑贴 {room.weiqi_komi:g} 目）。",
            )
        await self._emit("board_changed", room, {"actor": "system", "resized": True})

    def _is_first_seated_human(self, room: GameRoom, visitor_token: str) -> bool:
        """是不是房间里最早落座的那位真人（盘路只归他改）。"""
        if not visitor_token:
            return False
        if room.multiplayer.enabled:
            humans = [seat for seat in room.multiplayer.seats if not seat.is_ai]
            if not humans:
                return False
            return min(humans, key=lambda seat: seat.seated_at).visitor_token == visitor_token
        return visitor_token == room.player_token

    async def player_dice_action(
        self, room: GameRoom, visitor_token: str, action: str
    ) -> None:
        """Apply one authoritative player action in Pig, then run the Bot turn."""
        normalized = str(action or "").strip().lower()
        if normalized not in {"roll", "hold"}:
            raise ValueError("骰子操作只能是继续掷或收手")
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("当前浏览器不在玩家席")
            if room.status != "active" or not isinstance(room.game, PigDiceGame):
                raise ValueError("当前没有正在进行的贪心骰子")
            game = room.game
            if game.turn != "human":
                raise ValueError("现在是 Bot 的回合")
            if normalized == "roll":
                game.roll("human")
            else:
                game.hold("human")
            payload = dict(game.history[-1])
            room.touch()
            finished = game.finished
            bot_turn = not finished and game.turn == "bot"
        await self._emit("dice_changed", room, payload)
        if finished:
            await self._finish_game(room)
        elif bot_turn:
            self._spawn_bot_turn(room)

    def _blackjack_players(self, room: GameRoom) -> list[Visitor]:
        """当前在座且访客记录仍存在的玩家，按座位顺序。"""
        return [
            room.visitors[seat.visitor_token]
            for seat in room.multiplayer.seats
            if seat.visitor_token in room.visitors
        ]

    def _deal_blackjack(
        self, room: GameRoom, *, difficulty: Difficulty | None = None
    ) -> BlackjackGame:
        """开一局二十一点：手牌按稳定 uid 记账，座位号只用于展示。"""
        players = self._blackjack_players(room)
        return BlackjackGame.deal(
            difficulty=difficulty or room.difficulty,
            player_numbers=[visitor.number for visitor in players],
            player_uids=[_visitor_uid(visitor.token) for visitor in players],
        )

    async def player_blackjack_action(
        self, room: GameRoom, visitor_token: str, action: str
    ) -> None:
        """Apply one hit or stand for the player whose turn it is."""
        normalized = str(action or "").strip().lower()
        if normalized not in {"hit", "stand"}:
            raise ValueError("二十一点操作只能是要牌或停牌")
        dealer_ready = False
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if room.status != "active" or not isinstance(room.game, BlackjackGame):
                raise ValueError("当前没有正在进行的二十一点")
            game = room.game
            if game.finished or game.phase != "player_turns":
                raise ValueError("当前不是玩家要牌阶段")
            if visitor.token != room.multiplayer.current_token:
                raise PermissionError("还没轮到这位玩家")
            event = (
                game.hit(_visitor_uid(visitor.token))
                if normalized == "hit"
                else game.stand(_visitor_uid(visitor.token))
            )
            room.touch()
            if game.all_players_done():
                dealer_ready = True
                room.multiplayer.turn_deadline = 0.0
            else:
                dealer_ready = self._advance_blackjack_turn(room)
        await self._emit("blackjack_changed", room, event)
        if dealer_ready:
            self._spawn_blackjack_dealer_turn(room)

    def _advance_blackjack_turn(
        self, room: GameRoom, *, now: float | None = None
    ) -> bool:
        """Move to the next unresolved hand and return whether all hands are done."""
        state = room.multiplayer
        game = room.game
        if (
            not state.enabled
            or not state.seats
            or not isinstance(game, BlackjackGame)
            or game.phase != "player_turns"
            or game.finished
        ):
            state.turn_deadline = 0.0
            return False
        current = time.time() if now is None else float(now)
        count = len(state.seats)
        start = state.current_turn_index % count
        for offset in range(count):
            index = (start + offset) % count
            seat = state.seats[index]
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None:
                continue
            hand = game.hands.get(_visitor_uid(visitor.token))
            if hand is None or hand.status != "playing":
                continue
            if visitor.connected and current - visitor.last_seen_at < 15:
                state.current_turn_index = index
                self._reset_turn_deadline(room, now=current)
                return game.all_players_done()
            game.stand(_visitor_uid(visitor.token))
            room.add_message(
                "system",
                f"{self._visitor_label(visitor)}已离线，当前手牌自动停牌。",
            )
            if game.all_players_done():
                state.turn_deadline = 0.0
                return True
        state.turn_deadline = 0.0
        return game.all_players_done()

    async def _blackjack_dealer_turn(self, room: GameRoom) -> None:
        """Reveal the dealer hole card, draw to 17, then settle all hands."""
        async with room.lock:
            game = room.game
            if (
                room.status != "active"
                or not isinstance(game, BlackjackGame)
                or game.finished
                or game.phase != "player_turns"
                or not game.all_players_done()
            ):
                return
            game.phase = "dealer_turn"
            game.reveal_dealer()
            room.touch()
        await self._emit(
            "blackjack_dealer_revealed",
            room,
            {
                "dealer_total": game.dealer_total,
                "dealer_soft": game.dealer_soft,
            },
        )
        finished = False
        while True:
            async with room.lock:
                game = room.game
                if (
                    room.status != "active"
                    or not isinstance(game, BlackjackGame)
                    or game.phase != "dealer_turn"
                    or game.finished
                ):
                    return
                if game.has_pending_hands() and game.dealer_must_hit:
                    card = game.draw_dealer()
                    room.touch()
                    event = {
                        "action": "dealer_hit",
                        "card": card.as_dict(),
                        "dealer_total": game.dealer_total,
                        "dealer_soft": game.dealer_soft,
                    }
                else:
                    game.settle()
                    room.touch()
                    event = {
                        "action": "settle",
                        "dealer_total": game.dealer_total,
                        "dealer_soft": game.dealer_soft,
                        "results": {
                            str(key): value for key, value in game.results.items()
                        },
                    }
                    finished = True
            await self._emit("blackjack_changed", room, event)
            if finished:
                break
            await asyncio.sleep(0.65)
        await self._finish_game(room)

    async def update_drawing(
        self, room: GameRoom, visitor_token: str, strokes: Any
    ) -> None:
        """Replace the authoritative drawing with one bounded stroke document."""
        normalized = self._normalize_strokes(strokes)
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("只有玩家席可以操作画布")
            if room.status != "active" or not isinstance(room.game, DrawGuessGame):
                raise ValueError("当前没有正在进行的你画我猜")
            if room.game.processing:
                raise ValueError("Bot 正在看图，暂时不能修改画布")
            room.game.replace_strokes(normalized)
            room.touch()
        await self._emit("drawing_changed", room, {"revision": room.game.revision})

    async def begin_draw_guess(
        self, room: GameRoom, visitor_token: str
    ) -> DrawGuessGame:
        """Reserve one visual guess without exposing the hidden target."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("只有玩家席可以让 Bot 猜图")
            if room.status != "active" or not isinstance(room.game, DrawGuessGame):
                raise ValueError("当前没有正在进行的你画我猜")
            game = room.game
            if game.processing:
                raise ValueError("Bot 已经在看这幅画了")
            if game.finished:
                raise ValueError("本局已经结束")
            if not game.strokes:
                raise ValueError("请先画几笔，再让 Bot 猜")
            if game.is_expired():
                game.timeout()
                raise ValueError("作画时间已经结束")
            game.processing = True
            room.touch()
            return game

    async def abort_draw_guess(self, room: GameRoom) -> None:
        async with room.lock:
            if isinstance(room.game, DrawGuessGame):
                room.game.processing = False

    async def complete_draw_guess(
        self, room: GameRoom, visitor_token: str, guess: str
    ) -> dict[str, Any]:
        """Record one Bot guess and finish the cooperative round when appropriate."""
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if visitor.token != room.player_token:
                raise PermissionError("玩家席已经发生变化")
            if room.status != "active" or not isinstance(room.game, DrawGuessGame):
                raise ValueError("当前你画我猜已经结束")
            game = room.game
            if not game.processing:
                raise ValueError("当前没有待完成的 Bot 猜测")
            correct = game.matches(guess)
            item = game.record_guess(guess, correct=correct)
            game.processing = False
            room.touch()
            room.add_message(
                "bot",
                f"我猜是“{item['guess']}”。" + ("猜中了。" if correct else "好像不对。"),
                message_type="game",
            )
            finished = game.finished
        await self._emit("draw_guess_completed", room, dict(item))
        if finished:
            await self._finish_game(room)
        return dict(item)

    async def request_rematch(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        record_message: bool = True,
        request_text: str = "",
    ) -> None:
        """Put a finished room into a Bot-decided rematch request state."""
        self.require_game_enabled(room.game_type)
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            # 多人玩家席中的任意玩家（或单人的当前玩家）都能申请再来一局，不限于房主/当前回合
            if room.multiplayer.enabled:
                is_player = room.multiplayer.seat_for_token(visitor.token) is not None
            else:
                is_player = bool(room.player_token and visitor.token == room.player_token)
            if not is_player:
                raise PermissionError("只有玩家才能申请再来一局")
            if room.status != "finished":
                raise ValueError("当前还不能申请再来一局")
            if int(getattr(room, "soup_host_number", 0) or 0):
                # 有人已经在准备自己出题：这时候再让花火开一局，两套流程会打架
                raise ValueError(
                    "有人正在申请出题，等 TA 提交题目或撤回申请吧"
                )
            room.status = "rematch_pending"
            room.touch()
            if record_message:
                room.add_message(
                    "user", f"{self._visitor_label(visitor)}想再来一局。",
                    visitor=visitor, message_type="control",
                )
        await self._emit(
            "rematch_requested",
            room,
            {
                "visitor_token": visitor_token,
                "request_text": str(request_text or "想再来一局。").strip()[:240],
            },
        )

    async def resolve_rematch(
        self,
        room: GameRoom,
        *,
        accepted: bool,
        message: str = "",
        difficulty: Difficulty | None = None,
    ) -> bool:
        """Apply a pending Bot decision and reject stale concurrent decisions."""
        async with room.lock:
            if room.status != "rematch_pending":
                return False
            if message:
                room.add_message("bot", message)
            if accepted:
                if difficulty is not None:
                    room.difficulty = difficulty
                room.touch()
                return True
            # 花火不想再来一局：房间留在原地回到「已结束」，玩家还能改主意
            # 或换玩法。以前这里直接 destroy 房间，人还在页面上房间就没了。
            room.status = "finished"
            room.touch()
            room.add_message("system", "花火这次不想再来一局，房间留在结算页。")
            return True

    async def restart_finished_game(
        self, room: GameRoom, *, difficulty: Difficulty
    ) -> None:
        """Start another game in the same room, preserving its player and score."""
        self.require_game_enabled(room.game_type)
        async with room.lock:
            if room.multiplayer.enabled:
                has_players = any(
                    seat.is_ai or seat.visitor_token in room.visitors
                    for seat in room.multiplayer.seats
                )
            else:
                has_players = bool(room.player_token and room.player is not None)
            if not has_players:
                raise ValueError("当前房间没有可以继续对局的玩家")
            if room.status not in {"finished", "rematch_pending"}:
                raise ValueError("当前对局尚未结束，不能直接再来一局")
            if room.game is None:
                if room.game_type != "turtle_soup":
                    raise ValueError("当前对局尚未结束，不能直接再来一局")
                # 旧版本「申请出题」会清掉 room.game，这类房间会一直报无法开始。
                # 补一个占位对局，让下面的海龟汤分支正常重建（自愈）。
                room.game = TurtleSoupGame(
                    difficulty=room.difficulty,
                    max_hints=self.turtle_soup_max_hints,
                    content_level=self.turtle_soup_content_level,
                    mode=room.turtle_soup_mode,
                )
            if int(getattr(room, "soup_host_number", 0) or 0):
                raise ValueError(
                    "有人正在申请出题，请先提交题目或撤回申请，再开新的一局"
                )
            room.difficulty = difficulty
            generation_requested = (
                isinstance(room.game, TurtleSoupGame)
                and room.turtle_soup_mode == "bot_host"
            )
            if isinstance(room.game, TurtleSoupGame):
                room.game = TurtleSoupGame(
                    difficulty=difficulty,
                    max_hints=self.turtle_soup_max_hints,
                    content_level=self.turtle_soup_content_level,
                    mode=room.turtle_soup_mode,
                )
                side_label = ""
            elif isinstance(room.game, XiangqiGame):
                if room.multiplayer.enabled:
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 再来一局换先：就地互换（阵容变了则重新起序列）
                    self._prepare_duel_sides(room)
                    human_side = (
                        self._seat_side(room, room.multiplayer.seats[0]) or XIANGQI_RED
                    )
                else:
                    human_side = room.game.human_side
                room.game = await XiangqiGame.create(
                    self._require_xiangqi_engine(),
                    human_side=human_side,
                    difficulty=difficulty,
                )
                side_label = "红" if human_side == XIANGQI_RED else "黑"
            elif isinstance(room.game, WeiqiGame):
                if room.multiplayer.enabled:
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 再来一局换先：就地互换（阵容变了则重新起序列）
                    self._prepare_duel_sides(room)
                    human_color = self._duel_human_color(room)
                else:
                    human_color = room.game.human_color
                room.game = WeiqiGame(
                    human_color=human_color,
                    difficulty=difficulty,
                    size=room.weiqi_size,
                    komi=room.weiqi_komi,
                )
                side_label = "黑" if human_color == BLACK else "白"
            elif isinstance(room.game, (GomokuGame, OthelloGame)):
                game_class = (
                    GomokuGame if isinstance(room.game, GomokuGame) else OthelloGame
                )
                if room.multiplayer.enabled:
                    if len(room.multiplayer.seats) < 2:
                        self._append_ai_seat_locked(room)
                    # 再来一局换先：就地互换（阵容变了则重新起序列）
                    self._prepare_duel_sides(room)
                    human_color = self._duel_human_color(room)
                else:
                    human_color = room.game.human_color
                room.game = game_class(
                    human_color=human_color, difficulty=difficulty
                )
                side_label = "黑" if human_color == BLACK else "白"
            elif isinstance(room.game, TicTacToeGame):
                human_mark = room.game.human_mark
                room.game = TicTacToeGame(human_mark=human_mark, difficulty=difficulty)
                side_label = "X" if human_mark == TICTACTOE_X else "O"
            elif isinstance(room.game, PigDiceGame):
                room.game = PigDiceGame(
                    difficulty=difficulty,
                    target_score=self.pig_dice_target_score,
                )
                side_label = ""
            elif isinstance(room.game, DrawGuessGame):
                room.game = DrawGuessGame(
                    difficulty=difficulty,
                    max_guesses=self.draw_guess_max_guesses,
                    duration_seconds=self.draw_guess_duration_seconds,
                )
                side_label = ""
            elif isinstance(room.game, BlackjackGame):
                room.game = self._deal_blackjack(room, difficulty=difficulty)
                room.multiplayer.current_turn_index = 0
                side_label = ""
            elif isinstance(room.game, UndercoverGame):
                # 再来一局：不直接开局，回到玩家集结阶段。
                # 房主可重新调整卧底/平民/白板数量；玩家点击「准备」，
                # 全员就绪或倒计时结束会自动开局。
                room.game = None
                room.status = "setup"
                for seat in room.multiplayer.seats:
                    # 真人重置为未准备；AI 始终保持已准备，不影响“全员就绪自动开局”
                    if not seat.is_ai:
                        seat.ready = False
                duration = max(10, int(self.undercover_match_seconds or 60))
                state = room.multiplayer
                state.turn_timeout_seconds = duration
                state.turn_deadline = time.time() + duration
                room.add_message(
                    "system",
                    "房主发起了再来一局。玩家点击「准备」后，全员就绪或倒计时结束将自动开局；"
                    "房主可在开局前调整卧底/平民/白板数量。",
                )
                room.touch()
                return
            else:
                raise ValueError("当前游戏状态无法重新开始")
            room.status = "active"
            room.touch()
            if isinstance(room.game, TurtleSoupGame):
                room.add_message("system", "Bot 正在准备一道全新的海龟汤。")
                if room.game.mode == "player_host":
                    room.messages[-1]["content"] = (
                        "新一轮玩家出题开始，请当前玩家提供第一条公开线索。"
                    )
            elif isinstance(room.game, PigDiceGame):
                first = "玩家" if room.game.turn == "human" else "Bot"
                room.add_message(
                    "system", f"新一局贪心骰子开始，由{first}先掷，目标 50 分。"
                )
            elif isinstance(room.game, DrawGuessGame):
                room.add_message(
                    "system",
                    f"新一轮你画我猜开始，可让 Bot 猜 {room.game.max_guesses} 次。",
                )
            elif isinstance(room.game, BlackjackGame):
                room.add_message(
                    "system", "新一轮二十一点开始，Bot 是庄家，请当前玩家先行动。"
                )
            elif isinstance(room.game, UndercoverGame):
                counts = room.game.camp_counts()
                civ = counts.get("civilian", 0)
                uc = counts.get("undercover", 0)
                wb = counts.get("whiteboard", 0)
                parts = [f"平民 {civ} 人", f"卧底 {uc} 人"]
                if wb:
                    parts.append(f"白板 {wb} 人")
                room.add_message(
                    "system",
                    "新一局谁是卧底开始。"
                    + "、".join(parts)
                    + "。按座位顺序依次发言描述你的词条。",
                )
            else:
                room.add_message(
                    "system",
                    f"新对局开始，玩家继续执{side_label}，Bot 使用{self._difficulty_label(difficulty)}棋力。",
                )
            self._reset_turn_deadline(room)
        if generation_requested:
            await self._emit("soup_generation_requested", room, {"rematch": True})
            return
        if isinstance(room.game, BlackjackGame):
            async with room.lock:
                if not room.game.finished:
                    self._advance_blackjack_turn(room)
                self._reset_turn_deadline(room)
            if room.game.finished:
                await self._finish_game(room)
                return
            if room.game.all_players_done():
                self._spawn_blackjack_dealer_turn(room)
                return
        if isinstance(room.game, UndercoverGame):
            async with room.lock:
                await self._emit(
                    "undercover_game_started",
                    room,
                    {
                        "camp_counts": room.game.camp_counts(),
                        "words_assigned": True,
                    },
                )
        await self._emit("game_started", room, {"rematch": True})
        if self._is_bot_turn(room):
            self._spawn_bot_turn(room)

    async def set_difficulty(self, room: GameRoom, difficulty: Difficulty) -> None:
        """管理台直接切换棋力：只对棋类房间生效，立即影响 Bot 接下来的走子。"""
        if room.game_type not in DIFFICULTY_GAME_TYPES:
            raise ValueError(f"{self._game_label(room.game_type)}没有可调的棋力档位")
        if difficulty not in {"easy", "normal", "hard"}:
            raise ValueError("棋力只能是简单、普通、困难")
        async with room.lock:
            room.difficulty = difficulty
            game = room.game
            if game is not None and hasattr(game, "difficulty"):
                game.difficulty = difficulty
            room.touch()
            room.add_message(
                "system",
                f"管理员把棋力调整为{self._difficulty_label(difficulty)}。",
            )

    async def player_set_difficulty(
        self, room: GameRoom, visitor_token: str, difficulty: Difficulty
    ) -> None:
        """玩家在房间里自选花火棋力。

        仅棋类房间、仅「本人在玩家席」、且对面不是另一位真人时允许——
        真人对真人没有花火可调，这时改档会让人误以为在调对手强度。
        开局前选好即用于本局；对局中改则立即影响花火接下来的走子。
        """
        if room.game_type not in DIFFICULTY_GAME_TYPES:
            raise ValueError(f"{self._game_label(room.game_type)}没有可调的棋力档位")
        if difficulty not in {"easy", "normal", "hard"}:
            raise ValueError("棋力只能是简单、普通、困难")
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if room.multiplayer.enabled:
                if room.multiplayer.seat_for_token(visitor.token) is None:
                    raise PermissionError("请你先入座，再调整花火棋力")
                if room.game_type in DUEL_GAMES and self._duel_human_pair(room):
                    raise PermissionError("和真人对局时不能调整花火棋力")
            elif visitor.token != room.player_token:
                raise PermissionError("请你先入座，再调整花火棋力")
            # 调难度也是真实操作，刷新挂机让座判定
            self._touch_seat_action(room, visitor.token)
            room.difficulty = difficulty
            # 玩家手动选过就锁定，再战时不让模型把难度改回去
            room.difficulty_chosen_by_player = True
            game = room.game
            if game is not None and hasattr(game, "difficulty"):
                game.difficulty = difficulty
            room.touch()
            room.add_message(
                "system",
                f"{self._visitor_label(visitor)}把花火棋力调整为"
                f"{self._difficulty_label(difficulty)}。",
            )

    async def player_set_side_rotation(
        self, room: GameRoom, visitor_token: str, mode: str
    ) -> None:
        """玩家自选「再来一局」是每局换边还是保持执色。

        仅棋类、仅本人坐在玩家席、且对面不是另一位真人时允许：
        真人对真人的换边规则属于双方约定，不能由一方在页面上单方面改掉。
        """
        normalized = str(mode or "").strip().lower()
        if normalized not in {"auto", "fixed"}:
            raise ValueError("换边方式只能是「每局换边」或「保持执色」")
        if room.game_type not in DUEL_GAMES:
            raise ValueError(f"{self._game_label(room.game_type)}没有先后手可换")
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            seat = (
                room.multiplayer.seat_for_token(visitor.token)
                if room.multiplayer.enabled
                else None
            )
            if seat is None or seat.is_ai:
                raise PermissionError("请你先入座，再调整换边方式")
            if self._duel_human_pair(room):
                raise PermissionError("和真人对局时不能改换边方式")
            auto = normalized == "auto"
            if room.ai_side_rotation is auto:
                return
            room.ai_side_rotation = auto
            room.touch()
            room.add_message(
                "system",
                f"{self._visitor_label(visitor)}把再来一局的方式改为"
                f"「{'每局换边' if auto else '保持执色'}」。",
            )

    async def undo(self, room: GameRoom) -> int:
        """Undo the latest complete player round after a QQ-side decision."""
        async with room.lock:
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有可以悔棋的对局")
            if isinstance(room.game, (TurtleSoupGame, PigDiceGame, DrawGuessGame)):
                raise ValueError("当前游戏不支持悔棋")
            if room.multiplayer.enabled and self._duel_human_pair(room):
                # 和真人对局悔棋等于单方面改局面，必须双方同意，这里直接不允许
                raise PermissionError("和真人对局时不能悔棋")
            if isinstance(room.game, XiangqiGame):
                removed = await room.game.undo_round(self._require_xiangqi_engine())
            else:
                removed = room.game.undo_round()
            room.touch()
            room.add_message("system", "Bot 同意了悔棋。")
            return removed

    async def resign(self, room: GameRoom, *, visitor_token: str = "") -> None:
        """Finish a solo game as a Bot win, or surrender one Blackjack hand."""
        dealer_ready = False
        blackjack_continues = False
        async with room.lock:
            if room.status != "active" or room.game is None:
                raise ValueError("当前没有正在进行的对局")
            # 只有坐在玩家席上的本人可以认输。以前没有这层校验、还会用
            # room.player_token 兜底，任何拿到链接的人 POST 一次就能替玩家认输。
            self.require_seated_player(room, visitor_token)
            if isinstance(room.game, BlackjackGame):
                game = room.game
                if game.finished or game.phase != "player_turns":
                    raise ValueError("当前不是玩家要牌阶段")
                if (
                    not visitor_token
                    or visitor_token != room.multiplayer.current_token
                ):
                    raise PermissionError("还没轮到这位玩家投降")
                visitor = self._visitor(room, visitor_token)
                game.surrender(_visitor_uid(visitor.token))
                room.add_message(
                    "system",
                    f"{self._visitor_label(visitor)}选择投降，这一手记为输。",
                )
                room.touch()
                if game.all_players_done():
                    dealer_ready = True
                    room.multiplayer.turn_deadline = 0.0
                else:
                    self._advance_blackjack_turn(room)
                    blackjack_continues = True
            elif isinstance(room.game, TurtleSoupGame):
                # 海龟汤不按「投降」处理：单人房直接揭晓；多人房先表决（过半同意），
                # 票数不够时只记一票，函数尾部的 _finish_game 会因为 game 未结束而返回。
                soup_game = room.game
                if soup_game.mode == "player_host":
                    soup_game.give_up()
                else:
                    if soup_game.phase != "ready":
                        raise ValueError("题目还没有准备好，暂时不能揭晓")
                    uid = _visitor_uid(visitor_token) if visitor_token else ""
                    if soup_game.reveal_requested_by:
                        votes = int(
                            soup_game.cast_reveal_vote(uid, True).get("votes") or 0
                        )
                    else:
                        votes = int(soup_game.request_reveal(uid).get("votes") or 0)
                    required = soup_reveal_required(room)
                    if votes >= required:
                        soup_game.reveal(reason="revealed", source="qq")
                    else:
                        room.add_message(
                            "system",
                            f"已发起揭晓汤底（{votes}/{required}），"
                            f"还需要 {required - votes} 位玩家同意。",
                        )
            elif isinstance(room.game, XiangqiGame):
                # 两人对局：认输的是哪个座位，就把胜利判给对面
                loser_side = ""
                if room.multiplayer.enabled:
                    seat = room.multiplayer.seat_for_token(visitor_token)
                    if seat is not None:
                        loser_side = self._seat_side(room, seat)
                if loser_side == XIANGQI_RED:
                    room.game.winner = XIANGQI_BLACK
                elif loser_side == XIANGQI_BLACK:
                    room.game.winner = XIANGQI_RED
                else:
                    room.game.winner = room.game.bot_side
            elif isinstance(room.game, (GomokuGame, OthelloGame, WeiqiGame)):
                # 两人对弈：认输的是哪一方，就把胜利判给对方（围棋同样按座位颜色判负）
                loser_color = EMPTY
                if room.multiplayer.enabled:
                    seat = room.multiplayer.seat_for_token(visitor_token)
                    if seat is not None:
                        loser_color = self._seat_color(room, seat)
                room.game.winner = (
                    WHITE
                    if loser_color == BLACK
                    else BLACK
                    if loser_color == WHITE
                    else room.game.bot_color
                )
            elif isinstance(room.game, TicTacToeGame):
                room.game.winner = room.game.bot_mark
            elif isinstance(room.game, PigDiceGame):
                room.game.resign_human()
            elif isinstance(room.game, DrawGuessGame):
                room.game.timeout()
            else:
                raise ValueError("当前游戏不能认输")
            room.touch()
        if blackjack_continues:
            return
        if dealer_ready:
            self._spawn_blackjack_dealer_turn(room)
            return
        await self._finish_game(room)

    async def remove_player(
        self,
        room: GameRoom,
        visitor_number: int = 0,
        *,
        reason: str = "玩家已被移到观众席",
    ) -> None:
        """Clear one seat, preserving multiplayer games while seats remain."""
        dealer_ready = False
        async with room.lock:
            if room.multiplayer.enabled:
                target = (
                    self._visitor_by_number(room, visitor_number)
                    if visitor_number
                    else room.player
                )
                if target is None:
                    raise ValueError("当前没有玩家可以移到观众席")
                self._remove_multiplayer_seat(room, target.token)
                dealer_ready = bool(
                    isinstance(room.game, BlackjackGame)
                    and room.status == "active"
                    and not room.game.finished
                    and room.game.phase == "player_turns"
                    and room.game.all_players_done()
                )
                if room.multiplayer.seats:
                    room.player_empty_since = None
                    self._sync_primary_player(room)
                    self._reset_turn_deadline(room)
                else:
                    self._clear_primary_player(room)
                    room.player_empty_since = time.time()
                    room.game = None
                    room.status = "waiting"
            else:
                self._clear_primary_player(room)
                room.player_seat_locked = room.admin_room
                room.player_empty_since = time.time()
                room.game = None
                room.status = "waiting"
            room.touch()
            room.add_message("system", reason)
        if dealer_ready:
            self._spawn_blackjack_dealer_turn(room)

    async def kick_visitor(self, room: GameRoom, visitor_number: int) -> None:
        """Invalidate one browser identity and clear its seat if necessary."""
        dealer_ready = False
        async with room.lock:
            visitor = self._visitor_by_number(room, visitor_number)
            was_player = (
                room.multiplayer.seat_for_token(visitor.token) is not None
                if room.multiplayer.enabled
                else visitor.token == room.player_token
            )
            if room.multiplayer.enabled and was_player:
                self._remove_multiplayer_seat(room, visitor.token)
                dealer_ready = bool(
                    isinstance(room.game, BlackjackGame)
                    and room.status == "active"
                    and not room.game.finished
                    and room.game.phase == "player_turns"
                    and room.game.all_players_done()
                )
            room.visitors.pop(visitor.token, None)
            if was_player:
                if room.multiplayer.enabled and room.multiplayer.seats:
                    self._sync_primary_player(room)
                    room.player_empty_since = None
                    self._reset_turn_deadline(room)
                else:
                    self._clear_primary_player(room)
                    room.player_empty_since = time.time()
                    room.game = None
                    room.status = "waiting"
            room.touch()
            room.add_message("system", f"{visitor.number} 号已被移出房间。")
        if dealer_ready:
            self._spawn_blackjack_dealer_turn(room)

    async def pause(self, room: GameRoom) -> None:
        """Pause an active game without changing its board."""
        async with room.lock:
            if room.status != "active":
                raise ValueError("当前对局不能暂停")
            if isinstance(room.game, DrawGuessGame):
                room.game.pause()
            room.status = "paused"
            self._reset_turn_deadline(room)
            room.touch()
            room.add_message("system", "对局已暂停。")

    async def resume(self, room: GameRoom) -> None:
        """Resume a paused game."""
        async with room.lock:
            if room.status != "paused" or room.game is None:
                raise ValueError("当前没有已暂停的对局")
            if isinstance(room.game, DrawGuessGame):
                room.game.resume()
            room.status = "active"
            room.touch()
            self._reset_turn_deadline(room)
            room.add_message("system", "对局继续。")
            blackjack_dealer = bool(
                isinstance(room.game, BlackjackGame)
                and room.game.phase == "dealer_turn"
                and not room.game.finished
            )
            # 必须用房间级判定：_game_is_bot_turn 只看 game.bot_color，
            # 双真人局恢复时会把「对方那一手」误当花火的回合，替真人落子。
            bot_turn = self._is_bot_turn(room)
        if blackjack_dealer:
            self._spawn_blackjack_dealer_turn(room)
            return
        if bot_turn:
            self._spawn_bot_turn(room)

    async def switch_game(
        self,
        room: GameRoom,
        game_type: GameType,
        *,
        force: bool = False,
    ) -> bool:
        """Switch one room's game while preserving access, seats, and scores."""
        if game_type not in SUPPORTED_GAMES:
            raise ValueError("不支持的游戏类型")
        self.require_game_enabled(game_type)
        if game_type == "xiangqi":
            await self._require_xiangqi_engine().ensure_ready()
        async with room.lock:
            if room.game_type == game_type:
                return False
            if room.status in {"active", "paused"} and not force:
                raise ValueError("当前对局尚未结束，需要明确放弃本局后才能切换游戏")
            previous = room.game_type
            room.game_type = game_type
            self._configure_multiplayer(room)
            room.game = None
            room.status = "setup" if room.player_token else "waiting"
            room.player_empty_since = None if room.player_token else time.time()
            room.touch()
            room.add_message(
                "system",
                f"游戏已从{self._game_label(previous)}切换为{self._game_label(game_type)}。",
            )
        await self._emit(
            "game_switched", room, {"from": previous, "to": game_type, "forced": force}
        )
        return True

    # ------------------------------------------------------------------
    # 房间内「投票换游戏」
    #
    # 复用上面的 switch_game：投票通过后只是多一层「大家同意了」的判据，
    # 真正的切换、座位重排、系统消息、game_switched 事件都还是同一套逻辑。
    # ------------------------------------------------------------------

    # 判定「在线」的心跳窗口：与 Visitor.online 保持一致（15 秒）。
    GAME_VOTE_ONLINE_SECONDS = 15

    def switchable_games(self, room: GameRoom) -> list[dict[str, str]]:
        """房间内可投票切换的玩法（管理员已开放、且不是当前玩法）。"""
        return [
            {"key": game_type, "label": self._game_label(game_type)}
            for game_type in SUPPORTED_GAMES
            if game_type != room.game_type and self.game_enabled(game_type)
        ]

    def game_vote_cooldown_until(self, room: GameRoom) -> float:
        """下一次允许发起投票的时间戳（0 表示现在就能发起）。"""
        if not self.game_vote_enabled or not self.game_vote_cooldown:
            return 0.0
        return float(room.last_game_vote_at or 0.0) + self.game_vote_cooldown

    def _game_vote_online_tokens(self, room: GameRoom, now: float) -> list[str]:
        """当前在线的真人访客（排除花火占的 AI 座位）。"""
        ai_tokens = {
            seat.visitor_token for seat in room.multiplayer.seats if seat.is_ai
        }
        tokens: list[str] = []
        for token, visitor in room.visitors.items():
            if token in ai_tokens:
                continue
            if not getattr(visitor, "connected", False):
                continue
            last_seen = float(getattr(visitor, "last_seen_at", 0) or 0)
            if now - last_seen > self.GAME_VOTE_ONLINE_SECONDS:
                continue
            tokens.append(token)
        return tokens

    @staticmethod
    def game_vote_required(voter_base: int) -> int:
        """通过门槛：在线 1 人时发起人一票即通过，否则至少 2 票且过半。"""
        base = max(1, int(voter_base))
        if base <= 1:
            return 1
        return max(2, base // 2 + 1)

    def prune_game_vote(self, room: GameRoom, now: float | None = None) -> bool:
        """清理已过期的提案（惰性调用：读快照、发起、投票、看门狗都会走）。"""
        vote = room.game_vote
        if vote is None:
            return False
        current = time.time() if now is None else float(now)
        if current < float(vote.expires_at or 0):
            return False
        room.game_vote = None
        room.add_message("system", "换游戏投票已超时作废。")
        return True

    @staticmethod
    def game_vote_passed(room: GameRoom) -> bool:
        vote = room.game_vote
        if vote is None:
            return False
        in_favor, _against = vote.counts()
        return in_favor >= max(1, int(vote.required or 1))

    async def _apply_game_vote(self, room: GameRoom, target_game: GameType) -> None:
        """提案通过后真正执行切换（在锁外调用，switch_game 自己会加锁）。"""
        before = {
            seat.visitor_token: seat
            for seat in room.multiplayer.seats
            if not seat.is_ai
        }
        await self.switch_game(room, target_game, force=True)
        after = {seat.visitor_token for seat in room.multiplayer.seats}
        bumped = [
            seat for token, seat in before.items() if token and token not in after
        ]
        if bumped:
            names = "、".join(self._seat_display(seat) for seat in bumped)
            room.add_message(
                "system", f"座位不足，{names} 已回到观众席。"
            )

    async def propose_game_vote(
        self, room: GameRoom, visitor_token: str, game_type: str
    ) -> str:
        """发起一次换游戏投票；返回提案 id。"""
        if not self.game_vote_enabled:
            raise PermissionError("房间内投票换游戏已被管理员关闭")
        if game_type not in SUPPORTED_GAMES:
            raise ValueError("不支持的玩法")
        self.require_game_enabled(game_type)
        passed_target: GameType | None = None
        async with room.lock:
            now = time.time()
            self.prune_game_vote(room, now)
            if visitor_token not in room.visitors:
                raise PermissionError("你不在这个房间里")
            if room.status == "closed":
                raise ValueError("房间已关闭")
            if game_type == room.game_type:
                raise ValueError(f"当前已经在玩{self._game_label(game_type)}")
            if (
                room.status in {"active", "paused"}
                and not self.game_vote_allow_while_playing
            ):
                raise ValueError("对局进行中不能发起换游戏投票，等这一局结束后再来")
            if room.game_vote is not None:
                raise ValueError("已经有一个换游戏提案在投票中，先等它结束")
            cooldown_until = self.game_vote_cooldown_until(room)
            if cooldown_until > now:
                raise ValueError(
                    f"换游戏投票冷却中，还要等 {int(cooldown_until - now) + 1} 秒"
                )
            voter_base = max(1, len(self._game_vote_online_tokens(room, now)))
            required = self.game_vote_required(voter_base)
            request_id = secrets.token_urlsafe(8)
            room.game_vote = GameVoteRequest(
                request_id=request_id,
                proposer_token=visitor_token,
                target_game=game_type,
                created_at=now,
                expires_at=now + self.game_vote_expiry,
                voter_base=voter_base,
                required=required,
                # 发起即赞成：单人房间因此能一票通过
                votes={visitor_token: True},
            )
            room.last_game_vote_at = now
            proposer = room.visitors.get(visitor_token)
            proposer_name = self._visitor_label(proposer) if proposer else "有人"
            room.add_message(
                "system",
                f"{proposer_name} 发起投票：换到《{self._game_label(game_type)}》，"
                f"需 {required} 票赞成，{self.game_vote_expiry} 秒内有效。",
            )
            if self.game_vote_passed(room):
                passed_target = game_type  # type: ignore[assignment]
                room.game_vote = None
            room.touch()
        if passed_target is not None:
            await self._apply_game_vote(room, passed_target)
        return request_id

    async def cast_game_vote(
        self, room: GameRoom, visitor_token: str, approve: bool
    ) -> bool:
        """投赞成/反对票；返回这次投票是否直接通过了提案。"""
        if not self.game_vote_enabled:
            raise PermissionError("房间内投票换游戏已被管理员关闭")
        passed_target: GameType | None = None
        async with room.lock:
            now = time.time()
            self.prune_game_vote(room, now)
            vote = room.game_vote
            if vote is None:
                raise ValueError("当前没有进行中的换游戏投票")
            if visitor_token not in room.visitors:
                raise PermissionError("你不在这个房间里")
            vote.votes[visitor_token] = bool(approve)
            if self.game_vote_passed(room):
                passed_target = vote.target_game  # type: ignore[assignment]
                room.game_vote = None
            room.touch()
        if passed_target is not None:
            await self._apply_game_vote(room, passed_target)
            return True
        return False

    async def cancel_game_vote(self, room: GameRoom, visitor_token: str) -> None:
        """发起人撤回自己的提案。"""
        async with room.lock:
            vote = room.game_vote
            if vote is None:
                raise ValueError("当前没有进行中的换游戏投票")
            if vote.proposer_token != visitor_token:
                raise PermissionError("只有发起人才能撤回提案")
            room.game_vote = None
            room.add_message("system", "换游戏投票已撤回。")
            room.touch()

    def game_enabled(self, game_type: GameType) -> bool:
        """Return whether new rounds of one supported game may start."""
        return bool(self.enabled_games.get(game_type, False))

    def require_game_enabled(self, game_type: GameType) -> None:
        """Reject new rooms, switches and rematches for an administratively closed game."""
        if not self.game_enabled(game_type):
            raise PermissionError(f"管理员暂未开放{self._game_label(game_type)}")

    async def switch_turtle_soup_mode(
        self,
        room: GameRoom,
        mode: TurtleSoupMode,
        *,
        force: bool = False,
    ) -> bool:
        """Change only the turtle-soup variant while preserving room seats."""
        if mode not in {"bot_host", "player_host"}:
            raise ValueError("不支持的海龟汤玩法")
        async with room.lock:
            if room.game_type != "turtle_soup":
                raise ValueError("当前房间不是海龟汤")
            if room.turtle_soup_mode == mode:
                return False
            if room.status in {"active", "paused"} and not force:
                raise ValueError("当前海龟汤尚未结束，需要明确放弃后才能切换玩法")
            room.turtle_soup_mode = mode
            room.game = None
            room.soup_host_number = 0
            room.status = "setup" if room.player_token else "waiting"
            room.touch()
            room.add_message(
                "system",
                "海龟汤玩法已切换为"
                + (
                    "Bot 出题、玩家猜。" if mode == "bot_host" else "玩家出题、Bot 猜。"
                ),
            )
            self._reset_turn_deadline(room)
        await self._emit("game_switched", room, {"soup_mode": mode})
        return True

    async def complete_turtle_soup_generation(
        self,
        room: GameRoom,
        game: TurtleSoupGame,
        puzzle: SoupPuzzle,
    ) -> bool:
        """Publish one immutable puzzle if the room still expects it."""
        async with room.lock:
            if room.game is not game or room.status not in {"active", "paused"}:
                return False
            game.set_puzzle(puzzle)
            room.turtle_soup_recent_signatures.append(puzzle.signature)
            del room.turtle_soup_recent_signatures[:-8]
            room.touch()
            room.add_message("system", f"海龟汤《{puzzle.title}》已经准备好。")
            self._reset_turn_deadline(room)
        await self._emit("game_started", room, {"turtle_soup": True})
        return True

    # ---------------- 海龟汤：玩家出题人 ----------------

    def _online_soup_players(self, room: GameRoom, now: float | None = None) -> int:
        """当前在线、能提问的玩家数（不含玩家出题人）。

        花火的参与节奏按这个数算：N 个在线玩家各问一次，才轮到花火问一次，
        所以人越多它越不会抢话。单机位房间按 1 人算，保证它不会一直不动。
        阈值比回合轮转的 15 秒宽一点，免得浏览器后台节流把人数算少。
        """
        current = time.time() if now is None else float(now)
        state = room.multiplayer
        if not state.enabled or not state.seats:
            return 1
        game = room.game
        host = int(getattr(game, "host_number", 0) or 0) if game is not None else 0
        count = 0
        for seat in state.seats:
            if host and int(seat.number) == host:
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor and visitor.connected and current - visitor.last_seen_at < 30:
                count += 1
        return max(1, count)

    def _soup_host_seat(self, room: GameRoom) -> PlayerSeat | None:
        """房间当前自荐的出题人座位（还没开局时用 room.soup_host_number）。"""
        number = int(getattr(room, "soup_host_number", 0) or 0)
        if not number:
            return None
        return next(
            (seat for seat in room.multiplayer.seats if int(seat.number) == number),
            None,
        )

    def _seat_label(self, seat: PlayerSeat | None, number: int = 0) -> str:
        if seat is not None and seat.display_name:
            return seat.display_name
        if number:
            return f"{number} 号位"
        return "玩家"

    async def apply_soup_host(
        self, room: GameRoom, visitor_token: str
    ) -> dict[str, object]:
        """玩家自荐当出题人（默认仍是花火出题，这只是可选玩法）。

        只改「谁来出题」，不动座位与身份：申请人必须已经在玩家席上，
        免得一个观众悄悄把这一局的出题权拿走。
        """
        async with room.lock:
            if room.game_type != "turtle_soup":
                raise ValueError("当前房间不是海龟汤")
            visitor = self._visitor(room, visitor_token)
            seat = (
                room.multiplayer.seat_for_token(visitor.token)
                if room.multiplayer.enabled
                else None
            )
            if seat is None:
                raise ValueError("请先加入玩家席，再申请出题（观众不能出题）")
            if room.status in {"active", "paused"}:
                raise ValueError("本局还在进行，等这一局结束后再申请出题")
            number = int(seat.number)
            label = self._seat_label(seat, number)
            if int(getattr(room, "soup_host_number", 0) or 0) == number:
                return {"host_number": number, "host_name": label, "already": True}
            room.soup_host_number = number
            # 这里绝不能动 room.game：上一局的 TurtleSoupGame 还在，清掉之后
            # 前端整块对局区会变成空态（出题表单也拿不到 game），「再来一局」
            # 还会因为 game is None 直接失败。申请出题只改「下一局谁出题」。
            if room.status == "rematch_pending":
                # 有人正在等花火决定是否再来一局；现在有人明确要自己出题，
                # 把这个申请作废，免得两套流程打架（结果又开了花火的题）。
                room.status = "finished"
            room.touch()
            room.add_message("system", f"{label} 自荐当出题人，正在想题目。")
        await self._emit(
            "soup_host_applied", room, {"host_number": number, "host_name": label}
        )
        return {"host_number": number, "host_name": label, "already": False}

    async def cancel_soup_host(
        self, room: GameRoom, visitor_token: str = "", *, force: bool = False
    ) -> bool:
        """撤回出题申请（出题人自己或管理员）；已经开局后不能用这个取消。"""
        async with room.lock:
            if room.game_type != "turtle_soup":
                raise ValueError("当前房间不是海龟汤")
            seat = (
                room.multiplayer.seat_for_token(visitor_token)
                if room.multiplayer.enabled
                else None
            )
            number = int(getattr(room, "soup_host_number", 0) or 0)
            if not number:
                return False
            if not force and (seat is None or int(seat.number) != number):
                raise PermissionError("只有申请出题的人可以撤回")
            room.soup_host_number = 0
            room.touch()
            room.add_message("system", "出题申请已撤回，改回花火出题。")
        await self._emit("soup_host_cancelled", room, {})
        return True

    async def start_soup_host_game(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        title: str,
        surface: str,
        solution: str,
        hints: list[str] | None = None,
    ) -> TurtleSoupGame:
        """出题人提交汤面 + 汤底，直接开一局（不经过 LLM 出题）。"""
        from .turtle_soup import SoupPuzzle

        cleaned_surface = str(surface or "").strip()
        cleaned_solution = str(solution or "").strip()
        if len(cleaned_surface) < 8:
            raise ValueError("汤面太短了，至少写清「谁、在哪、发生了什么」")
        if len(cleaned_solution) < 8:
            raise ValueError("汤底太短了，至少写清真正的原因")
        hint_list = [str(item).strip()[:80] for item in (hints or []) if str(item).strip()]
        hint_list = hint_list[:3]
        async with room.lock:
            if room.game_type != "turtle_soup":
                raise ValueError("当前房间不是海龟汤")
            visitor = self._visitor(room, visitor_token)
            seat = (
                room.multiplayer.seat_for_token(visitor.token)
                if room.multiplayer.enabled
                else None
            )
            if seat is None or int(seat.number) != int(
                getattr(room, "soup_host_number", 0) or 0
            ):
                raise PermissionError("只有申请出题的人可以提交题目")
            if room.status in {"active", "paused"}:
                raise ValueError("本局还在进行，等结束后再出题")
            puzzle = SoupPuzzle(
                title=(str(title or "").strip()[:40] or f"{self._seat_label(seat)} 的汤"),
                surface=cleaned_surface[:800],
                solution=cleaned_solution[:1600],
                key_facts=(),
                acceptable_variants=(),
                hints=tuple(hint_list),
                content_level=self.turtle_soup_content_level,
                theme="玩家出题",
                trick="玩家出题",
            )
            game = TurtleSoupGame(
                difficulty=room.difficulty,
                max_hints=self.turtle_soup_max_hints,
                content_level=self.turtle_soup_content_level,
                mode="bot_host",
            )
            game.host_number = int(seat.number)
            game.set_puzzle(puzzle)
            room.game = game
            room.status = "active"
            room.soup_host_number = 0
            # 轮转从第一位不是出题人的座位开始
            for index, item in enumerate(room.multiplayer.seats):
                if int(item.number) != game.host_number:
                    room.multiplayer.current_turn_index = index
                    break
            self._reset_turn_deadline(room)
            room.touch()
            others = "其他人和花火一起猜" if room.soup_bot_join else "其他人来猜"
            room.add_message(
                "system",
                f"玩家出题模式：由 {self._seat_label(seat)} 出题，"
                f"{others}，出题人负责回答。",
            )
        await self._emit(
            "game_started", room, {"turtle_soup": True, "host_number": game.host_number}
        )
        return game

    async def begin_soup_host_judgement(
        self,
        room: GameRoom,
        text: str,
        *,
        kind: Literal["question", "answer"],
        source: Literal["web", "qq"] = "web",
        visitor_token: str = "",
        actor_qq: str = "",
        for_bot: bool = False,
        limit: int = 200,
    ) -> TurtleSoupGame:
        """把玩家（或花火）的提问/推理挂到出题人面前，等他用按钮裁决。"""
        cleaned = clean_player_text(text, limit=limit)
        async with room.lock:
            game = self._require_host_soup_game(room)
            number = 0
            name = "花火"
            if not for_bot:
                # 先认座位：出题人不管轮次，直接告诉他「你是出题人」
                asker_seat = (
                    room.multiplayer.seat_for_token(visitor_token)
                    if source == "web" and visitor_token
                    else (
                        room.multiplayer.seat_for_qq(actor_qq)
                        if source == "qq" and actor_qq
                        else None
                    )
                )
                if asker_seat is not None and int(asker_seat.number) == game.host_number:
                    raise ValueError("你是出题人，负责回答，不用提问")
                number = self._require_turtle_soup_player(
                    room,
                    source=source,
                    visitor_token=visitor_token,
                    actor_qq=actor_qq,
                )
                number = int(number)
                seat = next(
                    (s for s in room.multiplayer.seats if int(s.number) == number),
                    None,
                )
                name = self._seat_label(seat, number)
            game.set_pending_judge(
                kind, cleaned, asker_number=number, asker_name=name, for_bot=for_bot
            )
            room.multiplayer.turn_deadline = 0.0
            room.touch()
        await self._emit(
            "soup_host_question",
            room,
            {"kind": kind, "text": cleaned, "asker_name": name, "for_bot": for_bot},
        )
        return game

    async def judge_soup_pending(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        verdict: str,
    ) -> dict[str, object]:
        """出题人裁决：问题给四选一，推理给「猜对了 / 不对」。"""
        finished = False
        pending: dict[str, object] = {}
        async with room.lock:
            game = self._require_host_soup_game(room)
            seat = (
                room.multiplayer.seat_for_token(visitor_token)
                if room.multiplayer.enabled
                else None
            )
            if seat is None or int(seat.number) != game.host_number:
                raise PermissionError("只有出题人可以裁决")
            raw_pending = game.pending_judge
            if not raw_pending:
                raise ValueError("现在没有等待裁决的内容")
            pending = game.clear_pending_judge() or {}
            kind = str(pending.get("kind") or "question")
            text = str(pending.get("text") or "")
            asker = int(pending.get("asker_number") or 0)
            for_bot = bool(pending.get("for_bot"))
            if kind == "question":
                normalized = verdict if verdict in {
                    "yes", "no", "irrelevant", "partial", "compound",
                } else "irrelevant"
                game.record_question(
                    text,
                    normalized,  # type: ignore[arg-type]
                    source="web",
                    matched_facts=set(),
                    player_number=None if for_bot else asker,
                )
            else:
                correct = verdict == "correct"
                if correct and for_bot:
                    game.record_answer(
                        text,
                        solved=False,
                        source="web",
                        matched_facts=set(),
                        player_number=None,
                        reply="花火猜中了，汤底已经揭晓。",
                    )
                    game.bot_solved = True
                    game.solved = False
                    game.solver_number = 0
                    game.end_reason = "bot_solved"
                    game.phase = "finished"
                else:
                    game.record_answer(
                        text,
                        solved=correct,
                        source="web",
                        matched_facts=set(),
                        player_number=asker,
                        reply=(
                            "出题人认可：推理成立，汤底揭晓。"
                            if correct
                            else "出题人觉得还不对，再想想。"
                        ),
                    )
                    if correct:
                        game.solver_number = asker
            finished = game.finished
            if not finished and not self._turtle_soup_free_mode(room):
                self._advance_multiplayer_turn(room)
            room.touch()
        if finished:
            await self._finish_game(room)
        await self._emit(
            "soup_host_judged",
            room,
            {"kind": kind, "verdict": verdict, "finished": finished, "for_bot": for_bot},
        )
        return {
            "verdict": verdict,
            "finished": finished,
            "kind": kind,
            "asker_number": int(pending.get("asker_number") or 0),
        }

    def _require_host_soup_game(self, room: GameRoom) -> TurtleSoupGame:
        game = self._turtle_soup_game(room)
        if not game.host_number:
            raise ValueError("当前是花火出题，不需要裁决")
        if room.status != "active":
            raise ValueError("当前没有正在进行的海龟汤")
        return game

    async def begin_turtle_soup_interaction(
        self,
        room: GameRoom,
        text: str,
        *,
        source: str,
        visitor_token: str = "",
        actor_qq: str = "",
        limit: int,
    ) -> tuple[TurtleSoupGame, str]:
        """Reserve the single judge slot without holding the lock during LLM work."""
        cleaned = clean_player_text(text, limit=limit)
        async with room.lock:
            game = self._turtle_soup_game(room)
            player_number = self._require_turtle_soup_player(
                room,
                source=source,
                visitor_token=visitor_token,
                actor_qq=actor_qq,
            )
            if room.status == "paused":
                raise ValueError("当前海龟汤已经暂停")
            if room.status != "active":
                raise ValueError("当前没有正在进行的海龟汤")
            # 自由提问防刷：冷却期内丢弃新的提问/答题，避免玩家轮番提问把 LLM 打满。
            # （并发由 game.begin_processing() 的单格判断锁兜底，这里是更平滑的限速提示。）
            if (
                self._turtle_soup_free_mode(room)
                and self.turtle_soup_free_question_cooldown > 0
                and time.time()
                - float(getattr(game, "last_judge_at", 0) or 0)
                < self.turtle_soup_free_question_cooldown
            ):
                raise ValueError("别急，稍等片刻再问下一句～花火正在消化上一条推理。")
            game.begin_processing()
            game.processing_player_number = player_number
            room.multiplayer.turn_deadline = 0.0
            room.touch()
            return game, cleaned

    async def cancel_turtle_soup_interaction(
        self, room: GameRoom, game: TurtleSoupGame, reason: str
    ) -> None:
        async with room.lock:
            if room.game is game:
                game.cancel_processing(reason)
                self._reset_turn_deadline(room)

    async def resolve_turtle_soup_question(
        self,
        room: GameRoom,
        game: TurtleSoupGame,
        question: str,
        verdict: SoupVerdict,
        *,
        source: Literal["web", "qq"],
        matched_facts: set[int],
    ) -> bool:
        async with room.lock:
            if room.game is not game or room.status != "active":
                return False
            newly_discovered = game.record_question(
                question,
                verdict,
                source=source,
                matched_facts=matched_facts,
                player_number=game.processing_player_number,
            )
            room.touch()
            if not self._turtle_soup_free_mode(room):
                self._advance_multiplayer_turn(room)
        await self._emit(
            "soup_question_answered",
            room,
            {
                "source": source,
                "verdict": verdict,
                "new_facts": len(newly_discovered),
            },
        )
        return True

    async def resolve_turtle_soup_answer(
        self,
        room: GameRoom,
        game: TurtleSoupGame,
        answer: str,
        *,
        solved: bool,
        source: Literal["web", "qq"],
        matched_facts: set[int],
        reply: str = "",
        coverage: float = 0.0,
        missing: str = "",
    ) -> bool:
        async with room.lock:
            if room.game is not game or room.status != "active":
                return False
            newly_discovered = game.record_answer(
                answer,
                solved=solved,
                source=source,
                matched_facts=matched_facts,
                player_number=game.processing_player_number,
                reply=reply,
                coverage=coverage,
                missing=missing,
            )
            room.touch()
            if not self._turtle_soup_free_mode(room):
                self._advance_multiplayer_turn(room)
        if solved:
            await self._finish_game(room)
        else:
            await self._emit(
                "soup_answer_attempted",
                room,
                {"source": source, "new_facts": len(newly_discovered)},
            )
        return True

    async def request_turtle_soup_hint(
        self,
        room: GameRoom,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> str:
        async with room.lock:
            game = self._turtle_soup_game(room)
            player_number = self._require_turtle_soup_player(
                room,
                source=source,
                visitor_token=visitor_token,
                actor_qq=actor_qq,
            )
            if room.status != "active":
                raise ValueError("当前不能申请提示")
            if game.mode != "bot_host":
                raise ValueError("玩家出题模式不提供 Bot 预设提示")
            # 计算稳定 uid 作限次键：web 端直接取访客，QQ 端经座位反查。
            player_uid = ""
            if source == "web" and visitor_token:
                player_uid = _visitor_uid(self._visitor(room, visitor_token).token)
            elif source == "qq" and actor_qq and room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_qq(actor_qq)
                if seat is not None:
                    player_uid = _visitor_uid(seat.visitor_token)
            hint = game.reveal_hint(
                source=source, player_number=player_number, player_uid=player_uid
            )
            room.touch()
            self._advance_multiplayer_turn(room)
            # 提示同样消耗一轮：不说清楚的话，玩家会以为自己还能接着提问
            next_seat = room.multiplayer.seat_for_token(
                room.multiplayer.current_token
            )
            if next_seat is not None and not self._turtle_soup_free_mode(room):
                room.add_message(
                    "system",
                    "提示已给出，这一轮结束，接下来轮到 "
                    f"{self._seat_label(next_seat, int(next_seat.number))}。",
                )
        await self._emit(
            "soup_hint_revealed",
            room,
            {
                "source": source,
                "hint": hint,
                "visitor_token": visitor_token,
            },
        )
        return hint

    async def resolve_reverse_turtle_soup_turn(
        self,
        room: GameRoom,
        game: TurtleSoupGame,
        player_text: str,
        *,
        bot_action: Literal["question", "guess"],
        bot_text: str,
        source: Literal["web", "qq"],
    ) -> bool:
        """Publish one public Bot question or guess in player-hosted mode."""
        async with room.lock:
            if room.game is not game or room.status != "active":
                return False
            player_number = game.processing_player_number
            game.record_reverse_turn(
                player_text,
                bot_action=bot_action,
                bot_text=bot_text,
                source=source,
                player_number=player_number,
            )
            room.add_message("bot", bot_text)
            room.touch()
            self._advance_multiplayer_turn(room)
        await self._emit(
            "soup_reverse_turn",
            room,
            {"source": source, "bot_action": bot_action},
        )
        return True

    def _require_turtle_soup_seat(
        self,
        room: GameRoom,
        *,
        source: str,
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> int:
        """只校验「是玩家席成员」，不看轮次。

        「揭晓汤底」是整局的决定：谁想发起、谁来附议都不该被轮次卡住
        （以前投票也走轮次校验，导致发起之后别人根本投不了票）。
        """
        if source == "web":
            visitor = self._visitor(room, visitor_token)
            if room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_token(visitor.token)
                if seat is None:
                    raise PermissionError("只有玩家席的玩家可以决定是否揭晓")
                return int(seat.number)
            if not (room.player_token and visitor.token == room.player_token):
                raise PermissionError("只有玩家席的玩家可以决定是否揭晓")
            return int(visitor.number)
        if room.multiplayer.enabled:
            seat = room.multiplayer.seat_for_qq(actor_qq)
            if seat is None:
                raise PermissionError("你的 QQ 尚未绑定到玩家席，请在 WebUI 操作")
            return int(seat.number)
        raise PermissionError("只有玩家席的玩家可以决定是否揭晓")

    def _soup_player_uid(
        self,
        room: GameRoom,
        *,
        source: str,
        visitor_token: str,
        actor_qq: str,
    ) -> str:
        """揭晓投票用的稳定玩家标识：网页取访客、QQ 取座位绑定的访客。"""
        if source == "web" and visitor_token:
            return _visitor_uid(self._visitor(room, visitor_token).token)
        if source == "qq" and actor_qq:
            if room.multiplayer.enabled:
                seat = room.multiplayer.seat_for_qq(actor_qq)
                if seat is not None:
                    return _visitor_uid(seat.visitor_token)
            return f"qq:{actor_qq}"
        return ""

    async def request_soup_reveal(
        self,
        room: GameRoom,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, object]:
        """发起「揭晓汤底」。

        与「投降」的区别：海龟汤是多人在玩的，一个人不能替全队认输，
        所以这里只是发起表决——单人房（或已过半）立刻揭晓，多人房等其他人同意。
        """
        revealed = False
        async with room.lock:
            game = self._turtle_soup_game(room)
            early_host_seat = (
                room.multiplayer.seat_for_token(visitor_token)
                if source == "web" and visitor_token
                else (
                    room.multiplayer.seat_for_qq(actor_qq)
                    if source == "qq" and actor_qq
                    else None
                )
            )
            requester_is_host = bool(
                game.host_number
                and early_host_seat is not None
                and int(early_host_seat.number) == game.host_number
            )
            if not requester_is_host:
                # 揭晓是整局决定，任何在席玩家都能发起，不必等轮到自己
                self._require_turtle_soup_seat(
                    room, source=source, visitor_token=visitor_token, actor_qq=actor_qq
                )
            if room.status != "active":
                raise ValueError("当前没有正在进行的海龟汤")
            if game.phase != "ready":
                raise ValueError("题目还没有准备好，暂时不能揭晓")
            uid = self._soup_player_uid(
                room, source=source, visitor_token=visitor_token, actor_qq=actor_qq
            )
            host_seat = (
                room.multiplayer.seat_for_token(visitor_token)
                if source == "web" and visitor_token
                else (
                    room.multiplayer.seat_for_qq(actor_qq)
                    if source == "qq" and actor_qq
                    else None
                )
            )
            if game.host_number and host_seat is not None and int(
                host_seat.number
            ) == game.host_number:
                # 玩家出题人：汤底是他写的，自己就能揭晓，不需要凑票
                game.reveal(reason="revealed", source=source)
                votes, required, revealed = 1, 1, True
            else:
                result = game.request_reveal(uid)
                required = soup_reveal_required(room)
                votes = int(result.get("votes") or 0)
                revealed = votes >= required
                if revealed:
                    game.reveal(reason="revealed", source=source)
                else:
                    # 发起表决不消耗回合：这个人还是当前玩家，可以接着提问。
                    # 不说清楚的话，界面上只多了个 (1/2)，很容易以为轮次被吃掉了。
                    room.add_message(
                        "system",
                        f"{self._soup_actor_label(room, source=source, visitor_token=visitor_token, actor_qq=actor_qq)}"
                        f" 发起揭晓汤底（{votes}/{required}），还需要 {required - votes} 人同意；"
                        "表决期间照常提问，轮到谁就是谁。",
                    )
            room.touch()
        payload = {
            "votes": votes,
            "required": required,
            "revealed": revealed,
            "text": (
                "本题已揭晓汤底。"
                if revealed
                else f"已发起揭晓，还需要 {required - votes} 位玩家同意。"
            ),
        }
        if revealed:
            await self._finish_game(room)
            await self._emit("soup_revealed", room, {"reason": "revealed", "source": source})
        else:
            await self._emit("soup_reveal_requested", room, payload)
        return payload

    async def vote_soup_reveal(
        self,
        room: GameRoom,
        *,
        agree: bool,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, object]:
        revealed = False
        async with room.lock:
            game = self._turtle_soup_game(room)
            # 附议同样不受轮次限制，否则发起人之外的人永远投不了票
            self._require_turtle_soup_seat(
                room, source=source, visitor_token=visitor_token, actor_qq=actor_qq
            )
            if room.status != "active":
                raise ValueError("当前没有正在进行的海龟汤")
            uid = self._soup_player_uid(
                room, source=source, visitor_token=visitor_token, actor_qq=actor_qq
            )
            result = game.cast_reveal_vote(uid, agree)
            required = soup_reveal_required(room)
            votes = int(result.get("votes") or 0)
            revealed = bool(agree) and votes >= required
            actor = self._soup_actor_label(
                room, source=source, visitor_token=visitor_token, actor_qq=actor_qq
            )
            if revealed:
                game.reveal(reason="revealed", source=source)
            else:
                room.add_message(
                    "system",
                    f"{actor}{'同意' if agree else '不同意'}揭晓（{votes}/{required}）。",
                )
            room.touch()
        payload = {
            "votes": votes,
            "required": required,
            "revealed": revealed,
            "text": (
                "本题已揭晓汤底。"
                if revealed
                else f"已记录你的意见（{votes}/{required}）。"
            ),
        }
        if revealed:
            await self._finish_game(room)
            await self._emit("soup_revealed", room, {"reason": "revealed", "source": source})
        else:
            await self._emit("soup_reveal_voted", room, payload)
        return payload

    def _soup_actor_label(
        self,
        room: GameRoom,
        *,
        source: str,
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> str:
        """按 web / QQ 两种来源取出「谁在操作」，用于系统消息。"""
        seat = None
        if source == "web" and visitor_token:
            seat = room.multiplayer.seat_for_token(visitor_token)
        elif actor_qq:
            seat = room.multiplayer.seat_for_qq(actor_qq)
        if seat is None:
            return "有玩家"
        return self._seat_label(seat, int(seat.number))

    async def force_reveal_soup(self, room: GameRoom, *, reason: str = "timeout") -> bool:
        """超时兜底：没人解出也没人发起表决时，自动收题并揭晓汤底。

        没有这一步，一题可以无限期挂着——房间永远显示「进行中」，玩家也不好意思
        主动点揭晓。判定为「无人解开」，比分记花火守住。
        """
        async with room.lock:
            if room.game_type != "turtle_soup" or not isinstance(
                room.game, TurtleSoupGame
            ):
                return False
            game = room.game
            if game.phase != "ready" or room.status != "active":
                return False
            game.reveal(reason=reason, source="web")
            room.touch()
        await self._finish_game(room)
        await self._emit("soup_revealed", room, {"reason": reason, "source": "system"})
        return True

    def _snapshot_soup_result(self, room: GameRoom) -> None:
        """冻结海龟汤本局信息，供群通知 / 分享战报 / 复盘使用。

        注意：快照里**刻意不含 solution**。群通知与海报是「拉人入坑」的物料，
        剧透了后来的人就没得玩了；需要汤底的地方（结算卡）直接从游戏对象读。
        """
        game = room.game
        if not isinstance(game, TurtleSoupGame) or game.puzzle is None:
            room.last_soup_result = None
            return
        puzzle = game.puzzle
        entries: list[dict[str, object]] = []
        for entry in game.entries[-24:]:
            if entry.kind not in {"question", "answer", "hint"}:
                continue
            # 提问会出现在群通知里：把贴着汤底的片段遮掉，避免「一句话剧透」
            entries.append(
                {
                    "kind": entry.kind,
                    "prompt": redact_spoiler(entry.prompt, puzzle)[:220],
                    "response": entry.response[:80],
                    "verdict": entry.verdict,
                    "player_number": entry.player_number,
                }
            )
        # 参与玩家：战报要写清「谁在这场里」，花火是主持人不是玩家，跳过 AI 座位
        players: list[dict[str, object]] = []
        for seat in getattr(room.multiplayer, "seats", []) or []:
            if bool(getattr(seat, "is_ai", False)):
                continue
            number = int(getattr(seat, "number", 0) or 0)
            name = str(getattr(seat, "display_name", "") or "").strip()
            players.append(
                {
                    "number": number,
                    "name": name or f"{number} 号位",
                    "avatar_url": (
                        _qq_avatar_url(getattr(seat, "qq", ""))
                        if getattr(seat, "identity_confirmed", False)
                        else ""
                    ),
                }
            )
        room.last_soup_result = {
            "game_uid": str(getattr(game, "game_uid", "") or ""),
            "completed_games": int(room.completed_games),
            "game_type": "turtle_soup",
            "mode": game.mode,
            "players": players,
            "title": puzzle.title,
            "surface": puzzle.surface,
            "theme": puzzle.theme,
            "content_level": puzzle.content_level,
            "question_count": int(game.question_count),
            "answer_attempts": int(game.answer_attempts),
            "hints_used": int(game.hints_used),
            "key_fact_count": len(puzzle.key_facts),
            "discovered_fact_count": len(game.discovered_facts),
            "solved": bool(game.solved),
            "gave_up": bool(game.gave_up),
            # 玩家出题模式：谁出的题、谁猜中的（0 = 花火）
            "host_number": int(getattr(game, "host_number", 0) or 0),
            "host_name": next(
                (
                    str(seat.display_name or "")
                    for seat in getattr(room.multiplayer, "seats", []) or []
                    if int(getattr(seat, "number", 0) or 0)
                    == int(getattr(game, "host_number", 0) or 0)
                ),
                "",
            ),
            "solver_number": int(getattr(game, "solver_number", 0) or 0),
            "bot_solved": bool(game.bot_solved),
            "end_reason": str(game.end_reason or ("solved" if game.solved else "")),
            "entries": entries,
        }

    async def confirm_reverse_turtle_soup_guess(
        self,
        room: GameRoom,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> None:
        """Let the current player authoritatively mark the Bot's guess correct."""
        async with room.lock:
            game = self._turtle_soup_game(room)
            self._require_turtle_soup_player(
                room,
                source=source,
                visitor_token=visitor_token,
                actor_qq=actor_qq,
            )
            if room.status != "active":
                raise ValueError("当前不能判定 Bot 的猜测")
            game.confirm_bot_guess(correct=True)
            room.touch()
        await self._finish_game(room)

    async def request_seat_swap(
        self,
        room: GameRoom,
        visitor_token: str,
        target_number: int,
        *,
        now: float | None = None,
    ) -> str:
        """Create a rate-limited spectator request for one occupied seat."""
        current = time.time() if now is None else float(now)
        async with room.lock:
            state = room.multiplayer
            if not state.enabled:
                raise ValueError("当前游戏不支持多人席位交换")
            requester = self._visitor(room, visitor_token)
            self._purge_swap_requests(room, current)
            running_round = bool(
                isinstance(room.game, (BlackjackGame, UndercoverGame))
                and not room.game.finished
            )
            if (
                room.game_type in DUEL_GAMES and room.status in {"active", "paused"}
            ) or (room.status == "active" and running_round):
                # 对局中换座会让执方/局内号码与座位对不上，等这一局结束再换
                raise ValueError("对局进行中不能换座，请等这一局结束")
            if state.seat_for_token(requester.token) is not None:
                raise ValueError("玩家席内不能申请交换其他玩家")
            # 先按座位号定位（访客号与座位号可能不一致），再回退到访客号
            target_seat = next(
                (
                    seat
                    for seat in state.seats
                    if int(seat.number or 0) == int(target_number)
                ),
                None,
            )
            if target_seat is None:
                target = self._visitor_by_number(room, target_number)
                target_seat = state.seat_for_token(target.token)
            if target_seat is None:
                raise ValueError("目标访客当前不在玩家席")
            if target_seat.is_ai:
                raise ValueError("花火的座位不接受交换，直接入座等对手即可")
            target_token = target_seat.visitor_token
            cooldown_until = (
                state.last_swap_request_at.get(requester.token, 0)
                + state.swap_cooldown_seconds
            )
            if state.swap_cooldown_seconds and current < cooldown_until:
                remaining = max(1, int(cooldown_until - current + 0.999))
                raise ValueError(f"请等待 {remaining} 秒后再发送交换申请")
            if any(
                item.requester_token == requester.token
                for item in state.swap_requests.values()
            ):
                raise ValueError("你已经有一条等待处理的交换申请")
            request_id = secrets.token_urlsafe(10)
            state.swap_requests[request_id] = SeatSwapRequest(
                request_id=request_id,
                requester_token=requester.token,
                target_token=target_token,
                created_at=current,
                expires_at=current + state.swap_request_expiry_seconds,
            )
            state.last_swap_request_at[requester.token] = current
        return request_id

    async def resolve_seat_swap(
        self,
        room: GameRoom,
        visitor_token: str,
        request_id: str,
        *,
        accepted: bool,
        now: float | None = None,
    ) -> bool:
        """Accept or decline one request as its target player."""
        current = time.time() if now is None else float(now)
        async with room.lock:
            state = room.multiplayer
            visitor = self._visitor(room, visitor_token)
            self._purge_swap_requests(room, current)
            swap = state.swap_requests.get(str(request_id or ""))
            if swap is None:
                raise ValueError("交换申请不存在或已经失效")
            if swap.target_token != visitor.token:
                raise PermissionError("只有被申请的玩家可以处理这条申请")
            requester = room.visitors.get(swap.requester_token)
            state.swap_requests.pop(swap.request_id, None)
            if not accepted:
                return False
            if room.game_type in DUEL_GAMES and room.status in {"active", "paused"}:
                raise ValueError("对局进行中不能换座，请等这一局结束")
            if requester is None or state.seat_for_token(requester.token) is not None:
                raise ValueError("申请者已经离开或身份已经变化")
            if not room.admin_room and not requester.identity_confirmed:
                raise PermissionError("申请者需要先在 QQ 中绑定页面令牌")
            target_index = next(
                (
                    index
                    for index, seat in enumerate(state.seats)
                    if seat.visitor_token == swap.target_token
                ),
                -1,
            )
            if target_index < 0:
                raise ValueError("目标玩家已经不在玩家席")
            # 换座时把离席者（visitor/target）的 number 换成入席者（requester）原来的
            # number：随后 _sync_visitor_numbers 只会同步「在座」访客的 number，
            # 离席者借此保留一个独立编号，避免和入席者撞号（否则按编号定位访客会
            # 错位，remove_player / kick_visitor 会失效）。
            visitor.number = requester.number
            state.seats[target_index] = PlayerSeat(
                visitor_token=requester.token,
                qq=requester.qq if requester.identity_confirmed else "",
                display_name=requester.display_name,
                identity_confirmed=requester.identity_confirmed,
            )
            if requester.identity_confirmed:
                room.confirmed_participant_qqs.add(requester.qq)
                room.participant_names[requester.qq] = requester.display_name
            state.swap_requests = {
                key: item
                for key, item in state.swap_requests.items()
                if requester.token not in {item.requester_token, item.target_token}
                and visitor.token not in {item.requester_token, item.target_token}
            }
            self._sync_primary_player(room)
            if room.game_type in DUEL_GAMES:
                # 换座等于换了执方，局间重新按座位顺序分配
                self._assign_duel_sides(room)
            self._reset_turn_deadline(room, now=current)
            room.add_message(
                "system",
                f"{self._visitor_label(requester)}与 {self._visitor_label(visitor)}完成席位交换。",
            )
            room.touch()
        await self._emit("seats_changed", room, {"swapped": True})
        return True

    async def destroy(self, room_id: str, reason: str) -> GameRoom | None:
        """Destroy a room and release its quota exactly once."""
        async with self._lock:
            room = self.rooms.pop(str(room_id or ""), None)
            if room is None:
                return None
            self._access_index.pop(room.access_token, None)
            self._undercover_all_ready_at.pop(str(room_id or ""), None)
            room.status = "closed"
            room.close_reason = str(reason or "房间已结束")[:200]
            self._closed_access[room.access_token] = (
                room.close_reason,
                time.time() + self.CLOSED_ACCESS_TTL_SECONDS,
            )
            self._purge_closed_access()
            # 房间级缓存放掉引用：复盘文本 / 连战队列 / 终局快照。房间已 closed，
            # 这些内容不会再被读，留着只会随「开过多少房」一路线性增长。
            if isinstance(getattr(room, "recap_cache", None), dict):
                room.recap_cache.clear()
            room.pending_board_announces = []
            room.last_board_result = None
            room.last_soup_result = None
        await self._emit("room_destroyed", room, {"reason": room.close_reason})
        return room

    def _has_present_visitors(self, room: GameRoom, now: float) -> bool:
        """Whether any real (non-AI) browser visitor is still present in the room.

        玩家「退出玩家席」后仍会留在房间里当观众，页面持续发心跳（last_seen_at 保持新鲜）；
        只要还有真人访客在线，房间就不能被 empty_player_timeout 当作「空房」销毁，
        否则一局结束后两人退出玩家席、房间立刻在几十秒内被秒杀，无法继续开局或聊天。
        AI 座位（花火）不算真人，不参与判定。
        """
        ai_tokens = {
            seat.visitor_token for seat in room.multiplayer.seats if seat.is_ai
        }
        for visitor in room.visitors.values():
            if visitor.token in ai_tokens:
                continue
            if (
                visitor.connected
                and now - visitor.last_seen_at < self.DEPARTED_HEARTBEAT_TIMEOUT_SECONDS
            ):
                return True
        return False

    async def sweep_expired(self, *, now: float | None = None) -> list[str]:
        """Destroy rooms that exceed the configured empty or idle timeout."""
        current = time.time() if now is None else float(now)
        expired: list[tuple[str, str]] = []
        for room in list(self.rooms.values()):
            try:
                # 换游戏投票的过期是惰性的：看门狗每 2 秒顺带清一次，
                # 读快照时也会再清一次，保证前端不会看到「已过期还在倒计时」。
                self.prune_game_vote(room, current)
                hit = await self._sweep_room(room, current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 一个房间出错不能拖垮整轮清扫
                # 以前这里没有 try：任意一个房间 tick 抛异常（竞态、字段缺失、
                # 引擎异常）都会中断整轮清扫，后面的房间永远轮不到判负/销毁。
                logger.warning(
                    "[GameCompanion] 房间 %s 超时检查失败，本轮跳过: %s",
                    getattr(room, "room_id", "?"),
                    exc,
                )
                continue
            if hit is not None:
                expired.append(hit)
        for room_id, reason in expired:
            try:
                await self.destroy(room_id, reason)
            except Exception as exc:  # noqa: BLE001 - 销毁失败不影响其它房间
                logger.warning(
                    "[GameCompanion] 销毁房间 %s 失败: %s", room_id, exc
                )
        return [room_id for room_id, _reason in expired]

    def _prune_visitors(self, room: GameRoom, current: float) -> None:
        """回收长时间离线且没占座的访客，避免房间访客表只增不减。

        保留：占着玩家席的、还连着心跳的、房间主玩家、AI、以及最近 30 分钟内出现过的。
        被回收的人下次打开链接会拿到新的访客身份（不占座，不影响对局）。
        """
        if len(room.visitors) <= self.VISITOR_PRUNE_THRESHOLD:
            return
        seats = {seat.visitor_token for seat in room.multiplayer.seats}
        keep_tokens = set(seats)
        if room.player_token:
            keep_tokens.add(room.player_token)
        keep_tokens.update(
            str(seat.visitor_token) for seat in room.multiplayer.seats if seat.is_ai
        )
        for token, visitor in list(room.visitors.items()):
            if token in keep_tokens or getattr(visitor, "connected", False):
                continue
            recent = max(
                float(getattr(visitor, "last_seen_at", 0) or 0),
                float(getattr(visitor, "left_at", 0) or 0),
            )
            if current - recent < self.VISITOR_PRUNE_MIN_IDLE_SECONDS:
                continue
            room.visitors.pop(token, None)

    def _finished_room_departed(
        self, room: GameRoom, current: float
    ) -> tuple[str, str] | None:
        """结算后「玩家都走了且没有观众」→ 快速回收房间。

        必须在 `_tick_multiplayer_room` **之前**判断：tick 会把离线的座位清掉
        （finished 阶段也会清，见 `_vacate_departed_seats`），座位清空后
        `room.player` 变成 None，这条规则再也不会命中，房间只能等更长的超时。
        """
        player = room.player
        if room.status != "finished" or not self.empty_player_timeout:
            return None
        if player is None:
            # 座位已经被清空（离线玩家被判负后释放了座位）：只要没人在场就可以回收，
            # 否则房间会一直挂到「无操作超时」，白占着群聊房间配额。
            if room.multiplayer.seats:
                return None
            if self._has_present_visitors(room, current):
                return None
            return (room.room_id, "本局结束后玩家已离开，房间已自动销毁")
        seated_visitors = (
            [
                room.visitors[seat.visitor_token]
                for seat in room.multiplayer.seats
                if seat.visitor_token in room.visitors
            ]
            if room.multiplayer.enabled
            else [player]
        )
        all_departed = bool(seated_visitors) and all(
            (
                visitor.left_at is not None
                and current - visitor.left_at
                >= self.FINISHED_PLAYER_LEAVE_GRACE_SECONDS
            )
            or current - visitor.last_seen_at
            >= self.FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS
            for visitor in seated_visitors
        )
        # 玩家都离开了，但房间里还有观众（或其他真人访客）在线时，
        # 不能当作「本局结束、人去房空」销毁——观众可能还在看、或等下一局。
        # 另外：这条「人去房空快速回收」属于「玩家席无人」这一类自动销毁，
        # 已由开头的 `empty_player_timeout` 判断单独控制——设为 0（关闭该项）时
        # 一并停用，玩家走光后不再被 8 秒回收，只能等「房间无有效操作」超时
        # 或手动关闭。否则「只把玩家席无人设 0 仍被秒杀」不符合直觉。
        if all_departed and not self._has_present_visitors(room, current):
            return (room.room_id, "本局结束后玩家已离开，房间已自动销毁")
        return None

    async def _sweep_room(
        self, room: GameRoom, current: float
    ) -> tuple[str, str] | None:
        """检查单个房间；命中销毁规则时返回 (room_id, 原因)，否则 None。"""
        departed = self._finished_room_departed(room, current)
        if departed is not None:
            return departed
        await self._tick_multiplayer_room(room, current)
        self._prune_visitors(room, current)
        if (
            room.status == "active"
            and isinstance(room.game, DrawGuessGame)
            and not room.game.processing
            and room.game.is_expired(current)
        ):
            async with room.lock:
                room.game.timeout()
                room.touch()
            await self._finish_game(room)
        # 海龟汤「出题中」既没有玩家入座、也没有操作，容易被两条规则秒杀，
        # 所以豁免「玩家席无人」；但不能连 idle 一起豁免——出题链路异常时
        # 房间会永远留在内存里，还占着群房配额。
        preparing = bool(
            isinstance(room.game, TurtleSoupGame) and room.game.phase == "preparing"
        )
        # 管理台手动开出来的「链接房间」由管理员在管理台显式关闭，不参与
        # 「玩家席无人」「长时间无操作」的自动销毁：管理员把链接发给朋友后，
        # 对方可能过很久才点开，提前销毁就会变成「链接进不去」。
        if room.admin_room:
            return None
        if (
            self.empty_player_timeout
            and room.player_empty_since is not None
            and current - room.player_empty_since >= self.empty_player_timeout
            and not preparing
            and not self._has_present_visitors(room, current)
        ):
            return (room.room_id, "玩家席长时间无人，房间已自动销毁")
        # 「无操作」销毁：有人守着时给足宽限（思考 / 看结算页），但必须有上限——
        # 群聊默认只有 1 个房间配额，无限宽限会让一个挂着的页面永久占死配额。
        if self.idle_timeout:
            threshold = self.idle_timeout
            if room.status in {"active", "paused", "finished"} and (
                self._has_present_visitors(room, current)
            ):
                threshold = max(self.idle_timeout, self.AFK_IDLE_GRACE_SECONDS)
            if current - room.last_activity_at >= threshold:
                return (room.room_id, "房间长时间无操作，已自动销毁")
        return None

    async def close_all(self, reason: str = "插件已重载") -> None:
        """Destroy every in-memory room without persistence."""
        for room_id in list(self.rooms):
            await self.destroy(room_id, reason)

    def bot_think_delay(self, room: GameRoom) -> float:
        """花火这一步要「想」多久。

        统一由插件配置的基础延迟决定，各难度做小幅增减（简单更急、困难更慢），
        所以调「整体思考延迟」就能整体变慢/变快，不用逐档改代码。
        """
        base = max(0.0, float(getattr(self, "ai_think_delay", 0.8) or 0.0))
        bonus = {"easy": -0.15, "normal": 0.05, "hard": 0.3}.get(
            room.difficulty, 0.05
        )
        return max(0.15, base + bonus)

    def _spawn_bot_turn(self, room: GameRoom) -> asyncio.Task:
        """把花火的回合丢到后台执行。

        以前 start_game 会 await 完 AI 再返回，浏览器拿到响应时棋盘上已经有子了，
        看起来像「花火秒落子」。改成后台任务后，页面先看到空棋盘/自己的落子，
        过一会儿才出现花火的应手，和真人思考的节奏一致。
        """
        task = asyncio.create_task(self._bot_turn(room))
        self._bot_tasks.add(task)
        task.add_done_callback(self._bot_tasks.discard)
        return task

    def _spawn_blackjack_dealer_turn(self, room: GameRoom) -> asyncio.Task:
        """庄家回合后台执行：翻牌、逐张补牌、结算，不阻塞玩家要牌/停牌的响应。

        庄家补牌有逐张间隔，若同步 await 会让玩家点「要牌」后一直卡到庄家补完牌
        才拿到响应，前端一次性蹦出全部补牌（观感「节奏太快」）。丢到后台后，
        前端轮询能逐张看到庄家翻牌与补牌。
        """
        task = asyncio.create_task(self._blackjack_dealer_turn(room))
        self._bot_tasks.add(task)
        task.add_done_callback(self._bot_tasks.discard)
        return task

    async def wait_bot_turns(self) -> None:
        """等所有在途的 AI 回合结束（测试与关房清理用）。"""
        pending = [task for task in list(self._bot_tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _spawn_auto_start(self, room: GameRoom, token: str) -> None:
        """把卧底自动开局丢到后台执行，避免阻塞 sweep 的串行循环。

        start_game 里的取词可能调用 LLM（最长数秒）。自动开局是看门狗在清扫
        时触发的，若同步 await 会让之后所有房间的判负/销毁都晚数秒。
        """

        async def _run() -> None:
            try:
                await self.start_game(room, token, "")
            except Exception as exc:  # noqa: BLE001 - 开局失败不阻塞 sweep
                logger.debug(
                    "[GameCompanion] undercover auto-start failed: %s", exc
                )
            finally:
                room.undercover_auto_starting = False

        task = asyncio.create_task(_run())
        self._auto_start_tasks.add(task)
        task.add_done_callback(self._auto_start_tasks.discard)

    async def wait_auto_starts(self) -> None:
        """等所有在途的自动开局结束（测试用）。"""
        pending = [task for task in list(self._auto_start_tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _plan_bot_move(
        self, room: GameRoom, game_at_start: Any
    ) -> dict[str, Any] | None:
        """锁外计算花火这一手：只读棋局，不修改任何状态。

        围棋搜索（默认 0.6 秒）、象棋引擎（movetime 900ms、启动/IO 最长 8 秒）
        都发生在这里。以前它们跑在 `async with room.lock` 里面，期间网页每秒一次
        的心跳轮询、落子、入座全部排队，整间房发木——这才是「房间卡顿」的主因之一。
        """
        game = room.game
        if game is None or game is not game_at_start:
            return None
        if room.status != "active" or getattr(game, "finished", False):
            return None
        if isinstance(game, (TurtleSoupGame, DrawGuessGame)):
            return None
        # 不是花火的回合就不要白算（象棋一次搜索要 900ms，围棋 600ms）。
        # 正常调度下不会走到这里，但「再来一局换先」「两位真人」等场景下
        # 旧任务可能醒在一个不该它走的局面上。
        if not self._is_bot_turn(room):
            return None
        marker = self._bot_plan_marker(game)
        if isinstance(game, XiangqiGame):
            try:
                move = await self._require_xiangqi_engine().choose_move(
                    game.moves, game.difficulty
                )
            except Exception as exc:  # noqa: BLE001 - 引擎故障交给锁内那段统一暂停播报
                return {"kind": "xiangqi_error", "marker": marker, "error": exc}
            return {"kind": "xiangqi", "move": move, "marker": marker}
        if isinstance(game, GomokuGame):
            return {
                "kind": "gomoku",
                "move": await asyncio.to_thread(game.choose_bot_move),
                "marker": marker,
            }
        if isinstance(game, WeiqiGame):
            return {
                "kind": "weiqi",
                "move": await asyncio.to_thread(game.choose_bot_move),
                "marker": marker,
            }
        if isinstance(game, OthelloGame):
            return {
                "kind": "othello",
                "move": await asyncio.to_thread(game.choose_bot_move),
                "marker": marker,
            }
        return {"kind": "plain", "move": game.choose_bot_move(), "marker": marker}

    @staticmethod
    def _bot_plan_marker(game: Any) -> tuple[Any, ...]:
        """「锁外算棋」期间的局面指纹：轮到谁 + 是否已结束 + 手数。

        算完回到锁里时用它比对：只要对手在计算期间悔棋、停一手或被判负，
        指纹就会变，这一手直接丢弃——否则花火的棋会落到一个已经变过的局面上。
        """
        history = getattr(game, "history", None)
        moves = getattr(game, "moves", None)
        if isinstance(history, list):
            count = len(history)
        elif isinstance(moves, list):
            count = len(moves)
        else:
            count = 0
        return (
            getattr(game, "turn", None),
            bool(getattr(game, "finished", False)),
            count,
        )

    async def _bot_turn(self, room: GameRoom) -> None:
        # 记下这一局：睡眠期间玩家可能「再来一局」或退席，room.game 会被换掉，
        # 旧任务醒来后不能再往新棋盘上落子。
        game_at_start = room.game
        while True:
            await asyncio.sleep(self.bot_think_delay(room))
            if isinstance(room.game, PigDiceGame):
                await self._pig_dice_bot_turn(room)
                return
            # 第一段（不持锁）：只读棋局，算出「这一手走哪儿」。
            plan = await self._plan_bot_move(room, game_at_start)
            if plan is None:
                return
            board_event: dict[str, object] = {"actor": "bot"}
            finished = False
            # 第二段（持锁）：校验局面没被改过，再把这一手写进去（毫秒级）。
            async with room.lock:
                if room.game is not game_at_start or room.status != "active":
                    return
                game = room.game
                if game is None or game.finished:
                    return
                if self._bot_plan_marker(game) != plan["marker"]:
                    # 计算期间棋盘变了（悔棋 / 换局 / 判负）：这一手作废，
                    # 改局面的那一方会重新触发一次花火回合。
                    return
                ai_seat = self._ai_seat(room)
                if ai_seat is not None:
                    # 让解说能说「花火执白走了这一步」，而不是只有 human/bot 的二元信息
                    board_event.update(
                        seat_number=ai_seat.number,
                        seat_name="花火",
                        side=self._seat_side(room, ai_seat),
                    )
                kind = str(plan["kind"])
                if kind == "xiangqi_error":
                    room.status = "paused"
                    room.add_message(
                        "system",
                        "象棋引擎暂时不可用，对局已暂停。恢复引擎后可在 QQ 或管理台继续。",
                    )
                    error = plan.get("error")
                    raise (
                        error
                        if isinstance(error, BaseException)
                        else RuntimeError("象棋引擎暂时不可用")
                    )
                if kind == "xiangqi":
                    try:
                        await game.apply_bot_move(
                            self._require_xiangqi_engine(), str(plan["move"])
                        )
                    except Exception:
                        # 落子阶段引擎故障（搜索能过、perft 挂了、进程中途退出）：
                        # 和以前一样先暂停并播报。不暂停的话房间会停在
                        # 「轮到花火、但没有任务会再来重试」的半局状态。
                        room.status = "paused"
                        room.add_message(
                            "system",
                            "象棋引擎暂时不可用，对局已暂停。恢复引擎后可在 QQ 或管理台继续。",
                        )
                        raise
                elif kind == "gomoku":
                    move = plan["move"]
                    game.place(move[0], move[1], game.bot_color)
                    board_event.update(
                        row=move[0], column=move[1], color=game.bot_color
                    )
                elif kind == "weiqi":
                    move = plan["move"]
                    if move is None:
                        # Bot 判断局面已经收官，停一手；双方连续停手由规则层结算
                        game.pass_turn(game.bot_color)
                        board_event.update(
                            passed=True,
                            color=game.bot_color,
                            eval=game.evaluate(),
                        )
                    else:
                        captured = game.place(move[0], move[1], game.bot_color)
                        board_event.update(
                            row=move[0],
                            column=move[1],
                            color=game.bot_color,
                            captured=int(captured),
                            captured_points=[
                                list(point) for point in game.last_captured_points
                            ],
                            eval=game.evaluate(),
                        )
                elif kind == "othello":
                    move = plan["move"]
                    game.place(move[0], move[1], game.bot_color)
                    board_event.update(
                        row=move[0],
                        column=move[1],
                        color=game.bot_color,
                        passed_color=game.last_passed,
                        eval=game.evaluate(),
                    )
                    if game.last_passed:
                        passed = (
                            "黑"
                            if game.last_passed == OTHELLO_BLACK
                            else "白"
                        )
                        room.add_message(
                            "system", f"{passed}方没有合法落子，本回合跳过。"
                        )
                else:
                    move = plan["move"]
                    game.place(move[0], move[1], game.bot_mark)
                    board_event.update(
                        row=move[0], column=move[1], color=game.bot_mark
                    )
                finished = game.finished
                if not finished and isinstance(
                    game, BOARD_GAME_CLASSES
                ):
                    # 花火走完把倒计时还给真人座位
                    self._reset_turn_deadline(room)
            await self._emit("board_changed", room, board_event)
            if finished:
                await self._finish_game(room)
                return
            # Othello permits a pass. If the human has no legal move, the Bot
            # keeps playing instead of leaving the room waiting on an impossible move.
            if not isinstance(room.game, OthelloGame) or not self._is_bot_turn(room):
                return

    async def _pig_dice_bot_turn(self, room: GameRoom) -> None:
        """Play a visible multi-roll Bot turn without invoking the language model."""
        while True:
            async with room.lock:
                if (
                    room.status != "active"
                    or not isinstance(room.game, PigDiceGame)
                    or room.game.finished
                    or room.game.turn != "bot"
                ):
                    return
                game = room.game
                if game.bot_should_hold():
                    game.hold("bot")
                else:
                    game.roll("bot")
                payload = dict(game.history[-1])
                room.touch()
                finished = game.finished
                bot_done = finished or game.turn != "bot"
            await self._emit("dice_changed", room, payload)
            if finished:
                await self._finish_game(room)
                return
            if bot_done:
                return
            # 花火每次投掷之间给一个轻量随机间隔，观感更像真人在掷，而不是机械匀速
            await asyncio.sleep(random.uniform(0.5, 1.1))

    async def _finish_game(self, room: GameRoom) -> None:
        async with room.lock:
            if room.game is None or not room.game.finished or room.status == "finished":
                return
            room.status = "finished"
            room.completed_games += 1
            duel_credit: set[str] | None = None
            duel_handled = False
            if isinstance(room.game, TurtleSoupGame):
                room.turtle_soup_stats.questions += room.game.question_count
                room.turtle_soup_stats.hints += room.game.hints_used
                room.turtle_soup_stats.answer_attempts += room.game.answer_attempts
                if room.game.winner == "human":
                    room.human_wins += 1
                    result = "human_win"
                else:
                    room.bot_wins += 1
                    result = "bot_win"
            elif isinstance(room.game, DrawGuessGame):
                if room.game.solved:
                    room.human_wins += 1
                    result = "cooperative_success"
                else:
                    room.bot_wins += 1
                    result = "cooperative_unsolved"
            elif isinstance(room.game, BlackjackGame):
                results = list(room.game.results.values())
                human = sum(
                    1 for value in results if value in {"win", "blackjack_win"}
                )
                bot = sum(1 for value in results if value == "loss")
                pushes = sum(1 for value in results if value == "push")
                room.human_wins += human
                room.bot_wins += bot
                room.draws += pushes
                if human and not bot:
                    result = "human_win"
                elif bot and not human:
                    result = "bot_win"
                elif human and bot:
                    result = "mixed"
                else:
                    result = "draw"
            elif isinstance(room.game, UndercoverGame):
                # 谁是卧底：阵营式胜负，不区分 human/bot
                winner = room.game.winner or {}
                camp = winner.get("camp") or "unknown"
                room.draws += 0
                if camp == "civilian":
                    result = "uc_civilian_win"
                elif camp == "undercover":
                    result = "uc_undercover_win"
                elif camp == "whiteboard":
                    result = "uc_whiteboard_win"
                else:
                    result = "draw"
                # 房间内获胜次数排行榜：获胜阵营的每位玩家 +1
                if camp in {"civilian", "undercover", "whiteboard"}:
                    for uc_player in room.game.players:
                        if uc_player.camp != camp:
                            continue
                        seat = next(
                            (
                                s
                                for s in room.multiplayer.seats
                                if uc_player.uid
                                and _visitor_uid(s.visitor_token) == uc_player.uid
                            ),
                            None,
                        ) or next(
                            (
                                s
                                for s in room.multiplayer.seats
                                if s.number == uc_player.number
                            ),
                            None,
                        )
                        if seat is None:
                            continue
                        entry = room.player_win_counts.setdefault(
                            seat.visitor_token, {"name": "", "wins": 0}
                        )
                        entry["wins"] = int(entry.get("wins") or 0) + 1
                        entry["name"] = (
                            seat.display_name.strip()
                            or uc_player.display_name.strip()
                            or f"{uc_player.number}号"
                        )
                        # 全局胜场榜：按「undercover」分桶累计已绑定 QQ 玩家（含 AI），同名由排行榜合并展示
                        qq = (seat.qq or "").strip()
                        if seat.identity_confirmed and qq:
                            bucket = self.global_player_wins.setdefault("undercover", {})
                            global_entry = bucket.setdefault(
                                qq, {"name": "", "wins": 0}
                            )
                            global_entry["wins"] = (
                                int(global_entry.get("wins") or 0) + 1
                            )
                            if entry["name"]:
                                global_entry["name"] = entry["name"]
                            self._bump_daily(global_entry, wins=1)
                            # 分阵营胜场：用于解锁「藏品/护身符」徽章；
                            # 按 QQ 稳定键存储，改昵称后胜场与徽章不丢失
                            self._credit_undercover_camp_wins(
                                f"qq:{qq}", entry["name"], camp
                            )
                            seat.undercover_badges = type(self)._undercover_badges(
                                seat, self.undercover_camp_wins
                            )
                            self._save_global_stats()
            elif (
                room.game_type in DUEL_GAMES
                and room.multiplayer.enabled
                and isinstance(room.game, BOARD_GAME_CLASSES)
            ):
                # 两人对弈：胜负按「赢家座位」归属，输家不记胜。
                duel_handled = True
                winner_seat = self._duel_winner_seat(room)
                if winner_seat is None:
                    room.draws += 1
                    result = "draw"
                    duel_credit = set()
                else:
                    # 房间比分仍是两列：1 号座位进「玩家」列、2 号进「花火」列，
                    # 双人局时前端会把这两列标题换成两个座位昵称。
                    try:
                        winner_index = room.multiplayer.seats.index(winner_seat)
                    except ValueError:
                        winner_index = 0
                    if winner_index == 0:
                        room.human_wins += 1
                    else:
                        room.bot_wins += 1
                    if winner_seat.is_ai:
                        result = "bot_win"
                        duel_credit = set()
                    else:
                        result = "human_win"
                        self._bump_seat_win(room, winner_seat)
                        duel_credit = {winner_seat.visitor_token}
            elif getattr(room.game, "draw", False):
                room.draws += 1
                result = "draw"
            elif self._game_human_won(room.game):
                room.human_wins += 1
                result = "human_win"
            else:
                room.bot_wins += 1
                result = "bot_win"
            # 非卧底游戏全局战绩：棋类按「局数 + 胜场」统计（花火也有自己的胜率排名），
            # 其余合作 / 单人玩法沿用「获胜方 +1」的旧逻辑（AI 不记）。
            if room.game_type in DIFFICULTY_GAME_TYPES:
                winner_tokens = (
                    duel_credit
                    if duel_handled
                    else ({room.player_token} if result == "human_win" else set())
                )
                self._credit_board_game_global(
                    room,
                    winner_tokens=winner_tokens or set(),
                    ai_won=(result == "bot_win"),
                )
            elif room.game_type in {"blackjack", "pig_dice"}:
                # 二十一点/贪心骰子：花火是对手/庄家，每局记局数，赢局记胜场
                self._credit_ai_opponent_game(room, ai_won=(result == "bot_win"))
                if result == "human_win":
                    self._credit_human_global_wins(room)
            elif duel_handled:
                # 两人对弈只给赢家记胜，AI 座位不计入全局榜
                self._credit_human_global_wins(room, winner_tokens=duel_credit)
            elif (
                result in {"human_win", "cooperative_success"}
                and room.game_type != "undercover"
            ):
                self._credit_human_global_wins(room)
            # 棋类终局快照：在 room.game 被下一局替换前冻结本局数据，
            # 供异步播报 / 复盘 / 海报使用（否则玩家秒点再战会播成 0 手）。
            if room.game_type in DUEL_GAMES and isinstance(
                room.game, BOARD_GAME_CLASSES
            ):
                self._snapshot_board_result(room)
            # 海龟汤终局快照：群通知 / 海报 / 复盘都读它（同样为了防「秒点再战」）
            if isinstance(room.game, TurtleSoupGame):
                self._snapshot_soup_result(room)
            room.touch()
        await self._emit("game_finished", room, {"result": result})

    def _snapshot_board_result(self, room: GameRoom) -> None:
        """冻结棋类本局结果，供播报 / 复盘 / 海报读取。

        播报是异步任务，玩家可能在它跑完前就点了「再来一局」；届时 room.game
        已换成新对局，手数、终局盘面都会变成 0/1 手，播报就会说谎。
        """
        game = room.game
        if game is None:
            return
        raw_board = getattr(game, "board", None)
        board = raw_board() if callable(raw_board) else raw_board
        payload: dict[str, object] = {
            "game_uid": str(getattr(game, "game_uid", "") or ""),
            "completed_games": int(room.completed_games),
            "game_type": room.game_type,
            "moves": len(getattr(game, "history", None) or []),
            "draw": bool(getattr(game, "draw", False)),
            "winner_token": "",
            "winner_name": "",
            "winner_side": "",
            "board": [list(row) for row in board] if board else None,
        }
        winner_seat = self._duel_winner_seat(room)
        if winner_seat is not None:
            payload["winner_token"] = winner_seat.visitor_token
            payload["winner_name"] = self._seat_display(winner_seat)
            payload["winner_side"] = self._seat_side(room, winner_seat)
            # 战报里带上败者：双人对弈 / 单人 vs 花火都能从座位里定位到对家
            loser_seat = self._duel_loser_seat(room)
            if loser_seat is not None:
                payload["loser_name"] = self._seat_display(loser_seat)
                payload["loser_side"] = self._seat_side(room, loser_seat)
        elif not payload["draw"]:
            # 未启用玩家席的单人房间：按执方判胜负，供播报文案兜底
            if isinstance(game, XiangqiGame):
                payload["human_won"] = game.winner == getattr(
                    game, "human_side", None
                )
            else:
                winner = int(getattr(game, "winner", 0) or 0)
                human_color = int(getattr(game, "human_color", 0) or 0)
                if winner and human_color:
                    payload["human_won"] = winner == human_color
        # 战报海报的终局高亮信息：五子棋获胜连线 / 象棋最后一手 / 黑白棋落子与子数
        if isinstance(game, GomokuGame) and game.winner:
            line = self._gomoku_winning_line(board, game.winner)
            if line:
                payload["winning_line"] = [list(point) for point in line]
        elif isinstance(game, OthelloGame):
            if game.history:
                last_row, last_col, last_color = game.history[-1]
                payload["last_move"] = {"to": [last_row, last_col], "color": int(last_color)}
            payload["piece_count"] = {
                "black": int(game.black_count),
                "white": int(game.white_count),
            }
        elif isinstance(game, XiangqiGame) and game.moves:
            from_row, from_col, to_row, to_col = game.iccs_to_coordinates(game.moves[-1])
            before = game._board_before_last_move()
            captured = (
                before[to_row][to_col]
                if 0 <= to_row < len(before)
                and 0 <= to_col < len(before[0])
                and before[to_row][to_col] != "."
                else ""
            )
            piece = (
                board[to_row][to_col]
                if board
                and 0 <= to_row < len(board)
                and 0 <= to_col < len(board[0])
                else ""
            )
            payload["last_move"] = {
                "from": [from_row, from_col],
                "to": [to_row, to_col],
                "piece": piece,
                "captured": captured,
            }
        room.last_board_result = payload

    @staticmethod
    def _gomoku_winning_line(
        board: list[list[int]] | None, color: int
    ) -> list[tuple[int, int]]:
        """从终局盘面找出获胜方的一条连线（横/竖/两斜，取连续同色 ≥5 的那一串）。"""
        if not board or not board[0]:
            return []
        size = len(board)
        for row in range(size):
            for col in range(size):
                if board[row][col] != color:
                    continue
                for delta_row, delta_col in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    segment = [(row, col)]
                    scan_row, scan_col = row + delta_row, col + delta_col
                    while (
                        0 <= scan_row < size
                        and 0 <= scan_col < size
                        and board[scan_row][scan_col] == color
                    ):
                        segment.append((scan_row, scan_col))
                        scan_row += delta_row
                        scan_col += delta_col
                    scan_row, scan_col = row - delta_row, col - delta_col
                    while (
                        0 <= scan_row < size
                        and 0 <= scan_col < size
                        and board[scan_row][scan_col] == color
                    ):
                        segment.insert(0, (scan_row, scan_col))
                        scan_row -= delta_row
                        scan_col -= delta_col
                    if len(segment) >= 5:
                        return segment
        return []

    def _credit_board_game_global(
        self,
        room: GameRoom,
        *,
        winner_tokens: set[str],
        ai_won: bool,
    ) -> None:
        """给棋类全局榜记「局数 + 胜场」，让花火也拥有自己的胜率排名。

        每位真人参与者 +1 局；`winner_tokens` 里的获胜真人 +1 胜。花火在场时同样
        +1 局，`ai_won` 为真时花火 +1 胜。平局只记局数、不记胜场。
        谁是卧底等玩法不在这里统计（它们有各自的阵营胜场逻辑）。
        """
        if room.game_type not in DIFFICULTY_GAME_TYPES:
            return
        bucket = self.global_player_wins.setdefault(room.game_type, {})
        winners = winner_tokens or set()
        humans: list[tuple[str, str, str]] = []  # (qq, name, visitor_token)
        ai_present = False
        if room.multiplayer.enabled:
            for seat in room.multiplayer.seats:
                if seat.is_ai:
                    ai_present = True
                    continue
                qq = (seat.qq or "").strip()
                if seat.identity_confirmed and qq:
                    humans.append(
                        (
                            qq,
                            (seat.display_name or "").strip() or qq,
                            seat.visitor_token,
                        )
                    )
        else:
            # 单人局（玩家 vs 花火）
            qq = (room.player_qq or "").strip()
            if qq and room.player_identity_confirmed:
                name = ""
                visitor = room.visitors.get(room.player_token or "")
                if visitor and visitor.display_name:
                    name = visitor.display_name.strip()
                humans.append((qq, name or qq, room.player_token or ""))
            ai_present = True
        if not humans and not ai_present:
            return
        for qq, name, token in humans:
            entry = bucket.setdefault(qq, {"name": "", "wins": 0, "games": 0})
            entry["games"] = int(entry.get("games") or 0) + 1
            won = token in winners
            if won:
                entry["wins"] = int(entry.get("wins") or 0) + 1
            if name:
                entry["name"] = name
            self._bump_daily(entry, wins=1 if won else 0, games=1)
        if ai_present:
            entry = bucket.setdefault(
                AI_LEADERBOARD_KEY,
                {"name": AI_LEADERBOARD_NAME, "wins": 0, "games": 0},
            )
            entry["games"] = int(entry.get("games") or 0) + 1
            if ai_won:
                entry["wins"] = int(entry.get("wins") or 0) + 1
            self._bump_daily(entry, wins=1 if ai_won else 0, games=1)
        self._save_global_stats()

    def _credit_ai_opponent_game(self, room: GameRoom, *, ai_won: bool) -> None:
        """二十一点/贪心骰子等「单人 vs 花火」玩法：花火是对手/庄家，每局记局数，赢局记胜场。

        与棋类榜共用 AI_LEADERBOARD_KEY / AI_LEADERBOARD_NAME，让花火在这些玩法里
        也有自己的胜率排名（而不是只出现在棋类榜里）。
        """
        if room.game_type not in {"blackjack", "pig_dice"}:
            return
        bucket = self.global_player_wins.setdefault(room.game_type, {})
        entry = bucket.setdefault(
            AI_LEADERBOARD_KEY,
            {"name": AI_LEADERBOARD_NAME, "wins": 0, "games": 0},
        )
        entry["games"] = int(entry.get("games") or 0) + 1
        if ai_won:
            entry["wins"] = int(entry.get("wins") or 0) + 1
        self._bump_daily(entry, wins=1 if ai_won else 0, games=1)
        self._save_global_stats()

    def _credit_human_global_wins(
        self,
        room: GameRoom,
        *,
        winner_tokens: set[str] | None = None,
    ) -> None:
        """非卧底游戏获胜时，把当前已绑定玩家按「本局游戏类型」计入全局胜场榜。

        多人座位玩法累计已入座的绑定玩家；单人对局（无多人座位）则累计已绑定身份的主玩家。
        `winner_tokens` 用于两人对弈等「有输家」的场景：只给指定座位的玩家记胜，
        避免输家（以及 AI 座位）白拿一胜。
        """
        bucket = self.global_player_wins.setdefault(room.game_type, {})
        awarded: dict[str, str] = {}
        if winner_tokens is not None:
            for seat in room.multiplayer.seats:
                if seat.visitor_token not in winner_tokens or seat.is_ai:
                    continue
                qq = (seat.qq or "").strip()
                if not seat.identity_confirmed or not qq:
                    continue
                awarded.setdefault(qq, (seat.display_name or "").strip() or qq)
        else:
            # 多人座位：已绑定身份的入座玩家
            for seat in room.multiplayer.seats:
                qq = (seat.qq or "").strip()
                if not seat.identity_confirmed or not qq:
                    continue
                awarded.setdefault(qq, (seat.display_name or "").strip() or qq)
            # 单人对局（无多人座位）：给已绑定身份的主玩家 +1
            if not room.multiplayer.enabled:
                qq = (room.player_qq or "").strip()
                if qq and room.player_identity_confirmed:
                    name = ""
                    v = room.visitors.get(room.player_token or "")
                    if v and v.display_name:
                        name = v.display_name.strip()
                    awarded.setdefault(qq, name or qq)
        if not awarded:
            return
        for qq, name in awarded.items():
            if not qq:
                continue
            global_entry = bucket.setdefault(qq, {"name": "", "wins": 0})
            global_entry["wins"] = int(global_entry.get("wins") or 0) + 1
            if name:
                global_entry["name"] = name
            self._bump_daily(global_entry, wins=1)
        self._save_global_stats()

    @staticmethod
    def _bump_seat_win(room: GameRoom, seat: PlayerSeat) -> None:
        """房间内「按座位」胜场 +1（两人对弈用于排行榜/结算卡）。"""
        entry = room.player_win_counts.setdefault(
            seat.visitor_token, {"name": "", "wins": 0}
        )
        entry["wins"] = int(entry.get("wins") or 0) + 1
        entry["name"] = (
            (seat.display_name or "").strip() or f"{seat.number}号"
        )

    async def _emit(self, event: str, room: GameRoom, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            await self.event_callback(event, room, payload)

    def _vacate_departed_seats(self, room: GameRoom, now: float) -> None:
        """把「已经不在」的玩家移出玩家席，任何阶段都清。

        - 显式关闭网页（leave 已设置 left_at）：宽限 8 秒后移出，刷新会清掉 left_at；
        - 心跳中断（断网/崩溃/后台标签节流）：按 90 秒阈值移出，避免误踢切后台的玩家。
        移出后保留访客记录（QQ 绑定身份不丢），回到观众席；AI 座位不清理。

        **唯一例外是「棋类对弈还在进行中」**：那一局必须先走完
        「掉线暂停 → 宽限 → 判负」，否则座位先被摘掉，棋盘就会停在没有对手的
        半局状态（执色、回合归属都对不上）。判负结束、状态变成 finished 后，
        下一次清扫就会把这个座位释放掉。

        **谁是卧底对局中也不摘座位**，改为标记「托管」：局内玩家的存活状态由
        `is_out` 决定，摘掉座位不会让他出局，投票名单里照样有他，但补票逻辑只能
        按座位找人——结果整局永远差一张票、卡死在投票轮。保留座位后，托管由
        LLM 代发言/代投票，重连还能自动解除。

        为什么不能只清 waiting/setup：以前结算阶段不清，棋类掉线判负后输家一直
        占着座位，玩家席永久停在 2/2；对局中（人数型玩法）不清，关掉网页的人
        也会一直占着位置，别人进不来。
        """
        state = room.multiplayer
        if not state.enabled or not state.seats:
            return
        game = room.game
        duel_in_progress = bool(
            room.status in ("active", "paused")
            and isinstance(game, BOARD_GAME_CLASSES)
            and not game.finished
        )
        if duel_in_progress:
            return
        undercover_live = bool(
            room.status == "active"
            and isinstance(game, UndercoverGame)
            and not game.finished
        )
        for seat in list(state.seats):
            if seat.is_ai:
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None:
                continue
            left_at = getattr(visitor, "left_at", None)
            departed = (
                left_at is not None
                and now - left_at >= self.DEPARTED_LEAVE_GRACE_SECONDS
            ) or (
                now - visitor.last_seen_at
                >= self.DEPARTED_HEARTBEAT_TIMEOUT_SECONDS
            )
            if not departed:
                continue
            label = self._visitor_label(visitor)  # 已含（X号）
            if undercover_live:
                # 对局中只切托管，不摘座位：座位是「补票/跳过」定位玩家的锚点
                player = game.player_for_uid(_visitor_uid(seat.visitor_token))
                if player is None or player.autopilot:
                    continue
                player.autopilot = True
                room.add_message("system", f"{label} 掉线，已切换托管。")
                continue
            self._remove_multiplayer_seat(room, seat.visitor_token)
            if room.multiplayer.seats:
                self._sync_primary_player(room)
            elif room.status == "finished":
                # 结算阶段座位全空：只释放座位，保留终局盘面给还在场的观众看，
                # 交给「玩家席无人超时」兜底回收房间。
                self._clear_primary_player(room)
                room.player_empty_since = now
                room.multiplayer.turn_deadline = 0.0
            else:
                self._clear_primary_player(room)
                room.player_empty_since = now
                room.game = None
                room.status = "waiting"
                room.multiplayer.turn_deadline = 0.0
            if room.status != "finished":
                # 结算阶段清座位属于「收尾」，不算房间活跃：不能刷新 last_activity_at，
                # 否则玩家走光后「无操作超时」会被无限推迟，房间一直占着配额。
                room.touch()
            room.add_message(
                "system", f"{label} 已离线，已移出玩家席。"
            )
            if room.game_type == "undercover":
                self._undercover_all_ready_at.pop(room.room_id, None)

    def _touch_seat_action(
        self, room: GameRoom, visitor_token: str, *, now: float | None = None
    ) -> None:
        """记录一次「真实操作」的时间，供挂机让座判断。心跳不调用这里。"""
        state = room.multiplayer
        if not state.enabled:
            return
        seat = state.seat_for_token(visitor_token)
        if seat is None:
            return
        seat.last_action_at = time.time() if now is None else float(now)

    def _duel_afk_seat(self, room: GameRoom, now: float) -> PlayerSeat | None:
        """找一个「页面上仍在线、但长时间没有任何操作」的真人棋类座位。

        和离线不同：这种访客心跳一直新鲜，`left_at` 也一直是空，服务端不认为他走了；
        只有「最后一次有效操作」能暴露他其实不在电脑前。
        """
        if not self.duel_afk_seat_timeout:
            return None
        for seat in room.multiplayer.seats:
            if seat.is_ai or not seat.visitor_token:
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None or not visitor.connected:
                continue
            if now - visitor.last_seen_at >= self.DEPARTED_HEARTBEAT_TIMEOUT_SECONDS:
                continue
            last = float(getattr(seat, "last_action_at", 0.0) or seat.seated_at)
            if now - last >= self.duel_afk_seat_timeout:
                return seat
        return None

    def _duel_has_standby_visitor(self, room: GameRoom, now: float) -> bool:
        """房间里是否有「没座位的在线真人」在等着入座。"""
        seat_tokens = {seat.visitor_token for seat in room.multiplayer.seats}
        for visitor in room.visitors.values():
            if visitor.token in seat_tokens:
                continue
            if (
                visitor.connected
                and now - visitor.last_seen_at
                < self.DEPARTED_HEARTBEAT_TIMEOUT_SECONDS
            ):
                return True
        return False

    def _release_afk_duel_seats(self, room: GameRoom, now: float) -> None:
        """备战阶段让座：有人在等着入座时，把长时间没操作的座位腾出来。

        只在 waiting/setup 生效——对局中（active/paused）有「掉线暂停 → 判负」，
        这里插手会把棋局直接打断。没有候补观众时也不动，避免误伤「在等朋友」的人。
        """
        if not self.duel_afk_seat_timeout:
            return
        if room.game_type not in DUEL_GAMES or room.status not in ("waiting", "setup"):
            return
        state = room.multiplayer
        if not state.enabled or not state.seats:
            return
        if not self._duel_has_standby_visitor(room, now):
            return
        for seat in list(state.seats):
            if seat.is_ai or not seat.visitor_token:
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None:
                continue
            # 真正离线的人交给 _vacate_departed_seats（有自己的 8 秒 / 90 秒宽限）
            if (
                not visitor.connected
                or now - visitor.last_seen_at
                >= self.DEPARTED_HEARTBEAT_TIMEOUT_SECONDS
            ):
                continue
            last = float(getattr(seat, "last_action_at", 0.0) or seat.seated_at)
            if now - last < self.duel_afk_seat_timeout:
                continue
            label = self._visitor_label(visitor)
            self._remove_multiplayer_seat(room, seat.visitor_token)
            if state.seats:
                self._sync_primary_player(room)
                self._assign_duel_sides(room)
            else:
                self._clear_primary_player(room)
                room.player_empty_since = now
                room.game = None
                room.status = "waiting"
                state.turn_deadline = 0.0
            room.touch()
            room.add_message("system", f"{label} 长时间未操作，已让出座位。")

    def _undercover_seat_for_number(
        self, room: GameRoom, number: int
    ) -> PlayerSeat | None:
        """按局内冻结号码找当前座位（座位号可能被重编，优先按稳定 uid 匹配）。"""
        game = room.game
        if not isinstance(game, UndercoverGame):
            return None
        player = next(
            (p for p in game.players if int(p.number) == int(number)), None
        )
        if player is not None and player.uid:
            for seat in room.multiplayer.seats:
                if _visitor_uid(seat.visitor_token) == player.uid:
                    return seat
        return next(
            (s for s in room.multiplayer.seats if int(s.number) == int(number)),
            None,
        )

    def _undercover_seat_detached(
        self, room: GameRoom, seat: PlayerSeat | None, now: float
    ) -> bool:
        """该座位是否「没人操作」：托管中、访客已离开、或心跳超时。"""
        if seat is None:
            return True
        player = self._undercover_player_for_seat(room, seat)
        if player is not None and getattr(player, "autopilot", False):
            return True
        visitor = room.visitors.get(seat.visitor_token)
        if visitor is None or not visitor.connected:
            return True
        return now - visitor.last_seen_at >= self.DUEL_OFFLINE_SECONDS

    def _restore_undercover_autopilot(self, room: GameRoom, now: float) -> None:
        """玩家重新连上（心跳恢复）后自动解除托管。"""
        game = room.game
        if not isinstance(game, UndercoverGame) or game.finished:
            return
        for player in game.players:
            if not getattr(player, "autopilot", False):
                continue
            seat = next(
                (
                    s
                    for s in room.multiplayer.seats
                    if player.uid and _visitor_uid(s.visitor_token) == player.uid
                ),
                None,
            )
            if seat is None:
                # 座位已经不在（开局前离席/退到观众席）：清标记，避免状态残留
                player.autopilot = False
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None or not visitor.connected:
                continue
            if now - visitor.last_seen_at >= self.DUEL_OFFLINE_SECONDS:
                continue
            player.autopilot = False
            room.add_message(
                "system", f"{self._visitor_label(visitor)} 已重新连接，托管已解除。"
            )

    def _undercover_autopilot_due(self, room: GameRoom, now: float) -> bool:
        """是否有「托管/离线玩家」正卡着当前行动点，需要立刻代打。

        发言轮：当前发言人已经离线 → 不等 60 秒超时，马上跳过；
        投票轮：还有已离线的投票者没投 → 立刻让他这一票由 LLM 接管。
        """
        game = room.game
        if not isinstance(game, UndercoverGame) or game.finished:
            return False
        if room.status != "active":
            return False
        if now - float(getattr(room, "undercover_autopilot_at", 0.0) or 0.0) < 3.0:
            return False
        if game.phase in ("speech", "pk"):
            expected = game.expected_speaker_number
            if expected is None:
                return False
            seat = self._undercover_seat_for_number(room, int(expected))
            return self._undercover_seat_detached(room, seat, now)
        if game.phase == "voting":
            pending = game.pending_round
            if pending is None:
                return False
            for number in pending.vote_player_numbers:
                if pending.has_voted(int(number)):
                    continue
                seat = self._undercover_seat_for_number(room, int(number))
                if self._undercover_seat_detached(room, seat, now):
                    return True
        return False

    async def _tick_multiplayer_room(self, room: GameRoom, now: float) -> None:
        """Expire swap requests and rotate an overdue active turn."""
        dealer_ready = False
        duel_finished = False
        need_start_undercover: None | tuple[GameRoom, str] = None
        need_undercover_timeout: GameRoom | None = None
        need_undercover_autopilot: GameRoom | None = None
        need_soup_reveal: GameRoom | None = None
        async with room.lock:
            state = room.multiplayer
            if not state.enabled:
                return
            self._purge_swap_requests(room, now)
            # 等待/准备阶段先把已离开的玩家移出玩家席，避免离线占座挡住就绪与开局
            self._vacate_departed_seats(room, now)
            # 再处理「在线但人不在」的挂机占座：有人候补时把超时未操作的座位让出来
            self._release_afk_duel_seats(room, now)
            game = room.game
            if isinstance(game, BlackjackGame):
                dealer_ready = self._tick_blackjack_locked(room, now)
            elif isinstance(game, BOARD_GAME_CLASSES):
                action = self._tick_duel_locked(room, now)
                if action and "forfeit" in action:
                    duel_finished = self._forfeit_duel_locked(
                        room,
                        action["forfeit"],
                        str(action.get("reason") or "timeout"),
                    )
            elif (
                room.game_type == "undercover"
                and room.status == "setup"
                and game is None
            ):
                # 谁是卧底等待匹配阶段：全员就绪、人满或倒计时到了就自动开始
                if not state.turn_deadline:
                    # 兜底：若之前没设置倒计时，则设置一个
                    duration = max(10, int(self.undercover_match_seconds or 60))
                    state.turn_timeout_seconds = duration
                    state.turn_deadline = now + duration
                live_seats = [
                    s
                    for s in state.seats
                    if s.visitor_token
                    and (s.is_ai or s.visitor_token in room.visitors)
                ]
                live_count = len(live_seats)
                full = state.capacity > 0 and live_count >= state.capacity
                all_ready = (
                    live_count >= self.undercover_min_players
                    and all(s.ready for s in live_seats)
                )
                timed_out = bool(now >= state.turn_deadline)
                # 全员就绪：先记录确认窗口起点，等待 2 秒（玩家可取消）再开局，避免秒开。
                all_ready_confirm = False
                if all_ready:
                    ready_at = self._undercover_all_ready_at.get(room.room_id)
                    if ready_at is None:
                        self._undercover_all_ready_at[room.room_id] = now
                        ready_at = now
                        room.add_message(
                            "system",
                            f"所有玩家均已准备，{int(self.UC_ALL_READY_CONFIRM_SECONDS)} 秒后自动开始（可取消准备）。",
                        )
                        room.touch()
                    all_ready_confirm = (now - ready_at) >= self.UC_ALL_READY_CONFIRM_SECONDS
                else:
                    # 有人取消准备或退出玩家席：撤销已进入倒计时的自动开局，避免秒开后缺人
                    if self._undercover_all_ready_at.pop(room.room_id, None) is not None:
                        room.add_message(
                            "system",
                            "有玩家取消了准备或退出玩家席，自动开局已取消，等待重新就绪。",
                        )
                        room.touch()
                if (
                    not room.undercover_auto_starting
                    and (full or timed_out or all_ready_confirm)
                    and live_count >= self.undercover_min_players
                ):
                    target = next(
                        (
                            s.visitor_token
                            for s in live_seats
                        ),
                        None,
                    )
                    if target:
                        # 先把 turn_deadline 清零，避免重入
                        state.turn_deadline = 0.0
                        self._undercover_all_ready_at.pop(room.room_id, None)
                        # 置「自动开局在途」标记：取词可能调 LLM 数秒，期间看门狗
                        # 每 2 秒一轮，若不清标记会重复触发同一场开局。
                        room.undercover_auto_starting = True
                        need_start_undercover = (room, target)
            elif (
                room.game_type == "undercover"
                and room.status == "active"
                and isinstance(game, UndercoverGame)
            ):
                # 谁是卧底对局中：维护轮次/发言/投票倒计时，超时驱动 AI 或跳过挂机真人，
                # 避免因某个 AI/真人一直不出招而把整局卡死在“该谁发言”上。
                self._restore_undercover_autopilot(room, now)
                if game.finished or not state.turn_timeout_seconds:
                    state.turn_deadline = 0.0
                elif self._undercover_autopilot_due(room, now):
                    # 托管/离线玩家正卡着行动点：不等整轮超时，立刻让 LLM 代打。
                    # 3 秒节流由 _undercover_autopilot_due 控制，避免每轮清扫重复触发。
                    room.undercover_autopilot_at = now
                    self._reset_turn_deadline(room, now=now)
                    need_undercover_autopilot = room
                elif not state.turn_deadline:
                    self._reset_turn_deadline(room, now=now)
                elif now >= state.turn_deadline:
                    state.turn_deadline = 0.0  # 先清零，防止每个 housekeeping 周期重复触发
                    need_undercover_timeout = room
            else:
                # 海龟汤超时兜底：长时间没有任何推进（提问/推理/提示）且还没解开，
                # 自动收题并揭晓汤底。必须放在下面的 return 之前判断，否则永远轮不到。
                # 正在判定（processing）时跳过，避免和这一条判定抢同一个状态。
                soup_reveal_due = bool(
                    room.status == "active"
                    and isinstance(game, TurtleSoupGame)
                    and game.phase == "ready"
                    and not game.processing
                    and self.turtle_soup_auto_reveal_seconds > 0
                    and now
                    - float(getattr(game, "last_progress_at", now) or now)
                    >= self.turtle_soup_auto_reveal_seconds
                )
                if soup_reveal_due:
                    state.turn_deadline = 0.0
                    need_soup_reveal = room
                else:
                    # 自由提问模式不强制轮次：关闭回合超时轮换，玩家可随时提问
                    free_soup = self._turtle_soup_free_mode(room)
                    turn_active = bool(
                        room.status == "active"
                        and isinstance(game, TurtleSoupGame)
                        and game.phase == "ready"
                        and not game.processing
                        and state.seats
                        and state.turn_timeout_seconds
                        and not free_soup
                    )
                    if not turn_active:
                        state.turn_deadline = 0.0
                        return
                    if not state.turn_deadline:
                        self._reset_turn_deadline(room, now=now)
                        return
                    if now < state.turn_deadline:
                        return
                    previous = room.visitors.get(state.current_token)
                    self._advance_multiplayer_turn(room, now=now)
                    current = room.visitors.get(state.current_token)
                    if previous and current and previous.token != current.token:
                        room.add_message(
                            "system",
                            f"{self._visitor_label(previous)}回合超时，已轮到 {self._visitor_label(current)}。",
                        )
        if dealer_ready:
            self._spawn_blackjack_dealer_turn(room)
        if duel_finished:
            await self._finish_game(room)
        if need_start_undercover is not None:
            # 丢到后台执行：start_game 里的卧底取词可能调 LLM 数秒，若在这里
            # await 会阻塞 sweep 的串行 for 循环，拖慢之后所有房间的清扫。
            self._spawn_auto_start(need_start_undercover[0], need_start_undercover[1])
        if need_undercover_timeout is not None:
            await self._emit(
                "undercover_turn_timeout",
                need_undercover_timeout,
                {},
            )
        if need_undercover_autopilot is not None:
            await self._emit(
                "undercover_autopilot",
                need_undercover_autopilot,
                {},
            )
        if need_soup_reveal is not None:
            # 这里已经出了 room.lock，可以安全地走「加锁 → 结束 → 播报」的完整流程
            await self.force_reveal_soup(need_soup_reveal, reason="timeout")

    def _tick_duel_locked(
        self, room: GameRoom, now: float
    ) -> dict[str, Any] | None:
        """棋类对弈回合倒计时：掉线先暂停等重连，超时或宽限结束判负。

        返回 {"forfeit": seat} 时由调用方在锁外结算；paused/resumed 只做提示。
        """
        state = room.multiplayer
        seat = self._duel_turn_owner(room)
        if seat is None or seat.is_ai or not self._duel_human_pair(room):
            # 不是「两位真人」的回合（AI 自动落子 / 对局结束 / 单人练习局）：不计时
            state.turn_deadline = 0.0
            state.duel_paused = False
            state.duel_pause_deadline = 0.0
            return None
        if not state.turn_timeout_seconds:
            state.turn_deadline = 0.0
            return None
        visitor = room.visitors.get(seat.visitor_token)
        online = bool(
            visitor is not None
            and visitor.connected
            and now - visitor.last_seen_at < self.DUEL_OFFLINE_SECONDS
        )
        label = self._seat_display(seat)
        if not online:
            if state.duel_paused:
                if now < state.duel_pause_deadline:
                    return None
                return {"forfeit": seat, "label": label, "reason": "offline"}
            if self.duel_offline_grace <= 0:
                return {"forfeit": seat, "label": label, "reason": "offline"}
            state.duel_paused = True
            state.duel_pause_deadline = now + self.duel_offline_grace
            state.turn_deadline = state.duel_pause_deadline
            room.add_message(
                "system",
                f"{label} 掉线了，对局暂停；{self.duel_offline_grace} 秒内回来可以继续，否则判负。",
            )
            room.touch()
            return {"paused": seat, "label": label}
        if state.duel_paused:
            state.duel_paused = False
            state.duel_pause_deadline = 0.0
            state.turn_deadline = now + state.turn_timeout_seconds
            room.add_message("system", f"{label} 已回到棋盘，回合倒计时重新开始。")
            room.touch()
            return {"resumed": seat, "label": label}
        if not state.turn_deadline:
            state.turn_deadline = now + state.turn_timeout_seconds
            return None
        if now >= state.turn_deadline:
            return {"forfeit": seat, "label": label, "reason": "timeout"}
        return None

    def _forfeit_duel_locked(
        self, room: GameRoom, seat: PlayerSeat, reason: str
    ) -> bool:
        """把还在思考的一方判负（对手获胜），返回是否真的结束了本局。"""
        game = room.game
        if not isinstance(game, BOARD_GAME_CLASSES) or game.finished:
            return False
        if isinstance(game, XiangqiGame):
            loser_side = self._seat_side(room, seat)
            if not loser_side:
                return False
            game.winner = (
                XIANGQI_BLACK if loser_side == XIANGQI_RED else XIANGQI_RED
            )
        else:
            loser_color = self._seat_color(room, seat)
            if not loser_color:
                return False
            game.winner = WHITE if loser_color == BLACK else BLACK
        state = room.multiplayer
        state.duel_paused = False
        state.duel_pause_deadline = 0.0
        state.turn_deadline = 0.0
        detail = (
            "掉线后没有回来"
            if reason == "offline"
            else f"超过 {state.turn_timeout_seconds} 秒没有落子"
        )
        room.add_message(
            "system", f"{self._seat_display(seat)} {detail}，本局判负。"
        )
        room.touch()
        return True

    def _tick_blackjack_locked(self, room: GameRoom, now: float) -> bool:
        """Rotate or auto-stand an overdue Blackjack hand; return dealer-ready."""
        state = room.multiplayer
        game = room.game
        if (
            room.status != "active"
            or not isinstance(game, BlackjackGame)
            or game.finished
            or game.phase != "player_turns"
        ):
            state.turn_deadline = 0.0
            return False
        if game.all_players_done():
            state.turn_deadline = 0.0
            return True
        previous = room.visitors.get(state.current_token)
        if previous is not None:
            hand = game.hands.get(_visitor_uid(previous.token))
            if hand is not None and hand.status == "playing":
                offline = not (
                    previous.connected and now - previous.last_seen_at < 15
                )
                if offline:
                    game.stand(_visitor_uid(previous.token))
                    room.add_message(
                        "system",
                        f"{self._visitor_label(previous)}已离线，当前手牌自动停牌。",
                    )
                    if game.all_players_done():
                        state.turn_deadline = 0.0
                        return True
                    return self._advance_blackjack_turn(room, now=now)
        if not state.seats or not state.turn_timeout_seconds:
            state.turn_deadline = 0.0
            return False
        if not state.turn_deadline:
            self._reset_turn_deadline(room, now=now)
            return False
        if now < state.turn_deadline:
            return False
        if previous is not None:
            hand = game.hands.get(_visitor_uid(previous.token))
            if hand is not None and hand.status == "playing":
                game.stand(_visitor_uid(previous.token))
                room.add_message(
                    "system",
                    f"{self._visitor_label(previous)}回合超时，已自动停牌。",
                )
        if game.all_players_done():
            state.turn_deadline = 0.0
            return True
        return self._advance_blackjack_turn(room, now=now)

    def _configure_multiplayer(self, room: GameRoom) -> None:
        """Apply the current game's seat policy without losing its primary player."""
        if room.game_type not in SEAT_CAPABLE_GAMES:
            # 单人玩法（玩家 vs AI）：不启用玩家席。未来把玩法加入 SEAT_CAPABLE_GAMES
            # 后即可复用下面的席位创建逻辑（新玩法默认 2 席，可再补专属容量配置）。
            if room.multiplayer.enabled:
                self._sync_primary_player(room)
                # 统一重新编号，保证 seat.number 是 1..N
                for idx, seat in enumerate(room.multiplayer.seats):
                    seat.number = idx + 1
            room.multiplayer = MultiplayerState()
            return
        if room.game_type == "undercover":
            capacity = self.undercover_max_players
            # 把 manager 级配置同步到 room，供前端 snapshot 显示
            room.undercover_min_players = self.undercover_min_players
            room.undercover_prepare_seconds = self.undercover_prepare_seconds
            room.undercover_allow_host_customize_camp_scales = (
                self.undercover_allow_host_customize_camp_scales
            )
            if room.multiplayer.enabled:
                room.multiplayer.capacity = capacity
                room.multiplayer.turn_timeout_seconds = self.undercover_speaking_seconds or self.multiplayer_turn_timeout
                room.multiplayer.swap_cooldown_seconds = self.swap_request_cooldown
                room.multiplayer.swap_request_expiry_seconds = self.swap_request_expiry
                for idx, seat in enumerate(room.multiplayer.seats):
                    seat.number = idx + 1
                return
            seats = (
                [
                    PlayerSeat(
                        visitor_token=room.player_token,
                        qq=room.player_qq,
                        display_name=(
                            room.visitors.get(room.player_token).display_name
                            if room.visitors.get(room.player_token)
                            else ""
                        ),
                        identity_confirmed=room.player_identity_confirmed,
                    )
                ]
                if room.player_token
                else []
            )
            room.multiplayer = MultiplayerState(
                enabled=True,
                capacity=capacity,
                turn_timeout_seconds=(
                    self.undercover_speaking_seconds or self.multiplayer_turn_timeout
                ),
                swap_cooldown_seconds=self.swap_request_cooldown,
                swap_request_expiry_seconds=self.swap_request_expiry,
                seats=seats,
            )
            self._sync_primary_player(room)
            return
        if room.game_type in SEAT_CAPABLE_GAMES:
            # 两人对弈固定 2 席；海龟汤 / 黑杰克走各自配置
            capacity = {
                "turtle_soup": self.turtle_soup_max_players,
                "blackjack": self.blackjack_max_players,
            }.get(room.game_type, SEAT_CAPACITY.get(room.game_type, 2))
            if room.multiplayer.enabled:
                room.multiplayer.capacity = capacity
                # 两人对弈走独立的每步倒计时（默认 180 秒）：超时判负，掉线先暂停等待重连
                room.multiplayer.turn_timeout_seconds = (
                    self.duel_turn_timeout
                    if room.game_type in DUEL_GAMES
                    else self.multiplayer_turn_timeout
                )
                room.multiplayer.swap_cooldown_seconds = self.swap_request_cooldown
                room.multiplayer.swap_request_expiry_seconds = self.swap_request_expiry
                if room.game_type in DUEL_GAMES:
                    # 从人数型玩法切过来时席位可能超过 2 个：只保留前两位，其余回观众席
                    limit = int(capacity or 2)
                    if len(room.multiplayer.seats) > limit:
                        room.multiplayer.seats = room.multiplayer.seats[:limit]
                        self._sync_primary_player(room)
                    self._assign_duel_sides(room)
                return
            seats = (
                [
                    PlayerSeat(
                        visitor_token=room.player_token,
                        qq=room.player_qq,
                        display_name=(
                            room.visitors.get(room.player_token).display_name
                            if room.visitors.get(room.player_token)
                            else ""
                        ),
                        identity_confirmed=room.player_identity_confirmed,
                    )
                ]
                if room.player_token
                else []
            )
            room.multiplayer = MultiplayerState(
                enabled=True,
                capacity=capacity,
                turn_timeout_seconds=(
                    self.duel_turn_timeout
                    if room.game_type in DUEL_GAMES
                    else self.multiplayer_turn_timeout
                ),
                swap_cooldown_seconds=self.swap_request_cooldown,
                swap_request_expiry_seconds=self.swap_request_expiry,
                seats=seats,
            )
            self._sync_primary_player(room)
            # 给所有玩家席按列表顺序编号
            for idx, seat in enumerate(room.multiplayer.seats):
                seat.number = idx + 1
            if room.game_type in DUEL_GAMES:
                self._assign_duel_sides(room)
            return

    @staticmethod
    def _visitor_label(visitor: Visitor) -> str:
        return (
            f"{visitor.display_name}（{visitor.number}号）"
            if visitor.display_name
            else f"{visitor.number}号"
        )

    @staticmethod
    def _clear_primary_player(room: GameRoom) -> None:
        room.player_token = ""
        room.player_qq = ""
        room.player_identity_confirmed = False

    # ------------------------------------------------------------------
    # 两人对弈（五子棋 / 黑白棋）的座位与执色
    # ------------------------------------------------------------------

    @staticmethod
    def _side_color(side: str) -> int:
        """执色名 -> 棋盘颜色常量（黑白棋与五子棋共用 1=黑 / 2=白）。"""
        if side == "black":
            return BLACK
        if side == "white":
            return WHITE
        return EMPTY

    def _assign_duel_sides(self, room: GameRoom, *, rotate: bool = False) -> None:
        """按座位顺序分配执色：1 号先手（棋类黑 / 象棋红），rotate 时整体交换。

        象棋用「红 / 黑」描述执方，五子棋与黑白棋用「黑 / 白」，两套词表不能混用，
        所以这里按玩法决定写进座位的 side 值。

        注意：这是「从固定座位顺序重新算」，同一个座位顺序下 rotate=True 恒等，
        不能拿它做每局换先（换先请走 _swap_duel_sides / _prepare_duel_sides）。
        """
        if room.game_type not in DUEL_GAMES:
            return
        first, second = (
            ("red", "black") if room.game_type == "xiangqi" else ("black", "white")
        )
        offset = 1 if rotate else 0
        for idx, seat in enumerate(room.multiplayer.seats):
            seat.side = first if (idx + offset) % 2 == 0 else second

    @staticmethod
    def _other_duel_side(room: GameRoom, side: str) -> str:
        if room.game_type == "xiangqi":
            return "black" if side == "red" else "red"
        return "white" if side == "black" else "black"

    def _duel_sides_ready(self, room: GameRoom) -> bool:
        """每个座位都有合法执色且互不重复。"""
        allowed = (
            {"red", "black"} if room.game_type == "xiangqi" else {"black", "white"}
        )
        sides = [seat.side for seat in room.multiplayer.seats]
        return (
            bool(sides)
            and all(side in allowed for side in sides)
            and len(set(sides)) == len(sides)
        )

    def _swap_duel_sides(self, room: GameRoom) -> None:
        """真正的换先：就地互换每个座位的执色，不依赖座位序号。"""
        for seat in room.multiplayer.seats:
            if seat.side:
                seat.side = self._other_duel_side(room, seat.side)

    def _ai_side_rotation_active(self, room: GameRoom) -> bool:
        """再来一局是否换边：房间内自选优先，但只在人机对局里生效。

        两位真人开局后，房间级的自选项一律忽略——换边规则不能由其中一方
        在自己页面上悄悄改掉，否则对面会觉得「明明说好换先怎么没换」。
        """
        if room.ai_side_rotation is not None and not self._duel_human_pair(room):
            return bool(room.ai_side_rotation)
        return bool(self.duel_side_rotation)

    def side_rotation_state(
        self, room: GameRoom, visitor_token: str = ""
    ) -> dict[str, object]:
        """给前端的「再来一局换边」状态：能不能改 + 当前是哪种。"""
        rotation = self._ai_side_rotation_active(room)
        seat = (
            room.multiplayer.seat_for_token(visitor_token)
            if room.multiplayer.enabled
            else None
        )
        configurable = bool(
            room.game_type in DUEL_GAMES
            and seat is not None
            and not seat.is_ai
            and not self._duel_human_pair(room)
            and room.status != "closed"
        )
        return {
            "configurable": configurable,
            "mode": "auto" if rotation else "fixed",
            "explicit": room.ai_side_rotation is not None,
        }

    def _apply_requested_duel_side(self, room: GameRoom, requested_side: str) -> None:
        """首局按入座时选的先后手定色（象棋：我执红 / 我执黑）。"""
        desired = str(requested_side or "").strip().lower()
        if not desired:
            return
        human_seats = [seat for seat in room.multiplayer.seats if not seat.is_ai]
        if len(human_seats) != 1:
            return
        if room.game_type == "xiangqi":
            if desired == "random":
                desired = secrets.choice(("human_red", "human_black"))
            if desired not in {"human_red", "human_black"}:
                return
            human_side = "red" if desired == "human_red" else "black"
        else:
            if desired == "random":
                desired = secrets.choice(("human_black", "bot_black"))
            if desired not in {"human_black", "bot_black", "human_white"}:
                return
            human_side = "white" if desired in {"bot_black", "human_white"} else "black"
        human_seats[0].side = human_side
        other = self._other_duel_side(room, human_side)
        for seat in room.multiplayer.seats:
            if seat is not human_seats[0]:
                seat.side = other

    def _prepare_duel_sides(
        self, room: GameRoom, *, requested_side: str = ""
    ) -> None:
        """开局前决定双方执色。

        - 玩家席成员变了（有人入座/退席/花火让位）或首局：重新按座位顺序定色，
          首局还会尊重入座时选的先后手；
        - 阵容没变：按「换先」配置决定是互换还是固定不变。
        """
        if room.game_type not in DUEL_GAMES or not room.multiplayer.seats:
            return
        signature = tuple(seat.visitor_token for seat in room.multiplayer.seats)
        reset = (
            signature != room.duel_seat_signature
            or room.completed_games == 0
            or not self._duel_sides_ready(room)
        )
        if reset:
            if not self._duel_sides_ready(room):
                self._assign_duel_sides(room)
            room.duel_seat_signature = signature
            if room.completed_games == 0:
                self._apply_requested_duel_side(room, requested_side)
            return
        if self._ai_side_rotation_active(room):
            self._swap_duel_sides(room)

    def _seat_side(self, room: GameRoom, seat: PlayerSeat | None) -> str:
        """座位执方：象棋返回 red/black，棋类返回 black/white。"""
        if seat is None:
            return ""
        if seat.side:
            return seat.side
        # 兼容尚未分配 side 的历史房间：按座位序号推断
        try:
            index = room.multiplayer.seats.index(seat)
        except ValueError:
            return ""
        if room.game_type == "xiangqi":
            return "red" if index % 2 == 0 else "black"
        return "black" if index % 2 == 0 else "white"

    def _seat_color(self, room: GameRoom, seat: PlayerSeat | None) -> int:
        """座位在五子棋 / 黑白棋里的颜色常量（象棋不适用，返回 EMPTY）。"""
        return type(self)._side_color(self._seat_side(room, seat))

    def _duel_seat_for_side(self, room: GameRoom, side: str) -> PlayerSeat | None:
        """按执方（black/white/red）反查座位。"""
        if not side:
            return None
        return next(
            (
                seat
                for seat in room.multiplayer.seats
                if self._seat_side(room, seat) == side
            ),
            None,
        )

    def _duel_seat_for_color(
        self, room: GameRoom, color: int
    ) -> PlayerSeat | None:
        """按棋盘颜色反查座位（1=黑 / 2=白）。"""
        if not color:
            return None
        return self._duel_seat_for_side(room, "black" if color == BLACK else "white")

    def _duel_turn_owner(self, room: GameRoom) -> PlayerSeat | None:
        """当前该落子/走棋的座位；单人局 / 对局结束 / 非活跃局返回 None。"""
        game = room.game
        if (
            not isinstance(game, BOARD_GAME_CLASSES)
            or game.finished
            or not room.multiplayer.enabled
            or room.status != "active"
            or len(room.multiplayer.seats) < 2
        ):
            return None
        if isinstance(game, XiangqiGame):
            return self._duel_seat_for_side(room, str(game.turn))
        owner = self._duel_seat_for_color(room, int(game.turn))
        if owner is None:
            return None
        return owner

    def _duel_winner_seat(self, room: GameRoom) -> PlayerSeat | None:
        """本局赢家座位：象棋按执方查，棋类按颜色查；和棋返回 None。"""
        game = room.game
        if isinstance(game, XiangqiGame):
            return self._duel_seat_for_side(room, str(game.winner or ""))
        return self._duel_seat_for_color(
            room, int(getattr(game, "winner", EMPTY) or EMPTY)
        )

    def _duel_loser_seat(self, room: GameRoom) -> PlayerSeat | None:
        """本局败家座位：象棋按执方查，棋类按颜色查；和棋 / 找不到对家返回 None。

        只有在「座位里能定位到胜负双方」时才返回败者（双人对弈、或单人 vs 花火
        AI 座位）；纯单人练习没有对家座位，返回 None，战报仍用「玩家/花火」二元兜底。
        """
        game = room.game
        if isinstance(game, XiangqiGame):
            loser = {"red": "black", "black": "red"}.get(
                str(game.winner or ""), ""
            )
            return self._duel_seat_for_side(room, loser)
        winner = int(getattr(game, "winner", EMPTY) or EMPTY)
        if winner == BLACK:
            loser = WHITE
        elif winner == WHITE:
            loser = BLACK
        else:
            return None
        return self._duel_seat_for_color(room, loser)

    @staticmethod
    def _duel_human_pair(room: GameRoom) -> bool:
        """是否「两位真人」对弈。

        只有真人对真人才计时判负：和花火对局属于练习局，挂机不该被直接判输。
        """
        return (
            sum(1 for seat in room.multiplayer.seats if not seat.is_ai) >= 2
        )

    @staticmethod
    def _ai_seat(room: GameRoom) -> PlayerSeat | None:
        return next(
            (seat for seat in room.multiplayer.seats if seat.is_ai),
            None,
        )

    @staticmethod
    def _human_seats(room: GameRoom) -> list[PlayerSeat]:
        return [seat for seat in room.multiplayer.seats if not seat.is_ai]

    @staticmethod
    def _seat_display(seat: PlayerSeat) -> str:
        """座位在文案里的称呼：AI 座位一律叫「花火」，不暴露内部 AI 命名。"""
        if seat.is_ai:
            return "花火"
        return (seat.display_name or "").strip() or f"{seat.number}号"

    def _duel_human_color(self, room: GameRoom) -> int:
        """构造棋局时的 human_color。

        棋类引擎把「人」和「Bot」抽象成两种颜色，所以这里保证：
        - 有一个 AI 座位时，human_color = 那个真人座位的颜色，bot_color 正好落在 AI 身上；
        - 两个真人时，human_color = 1 号座位的颜色（仅用于悔棋基准与展示）。
        """
        seats = room.multiplayer.seats
        if not seats:
            return BLACK
        ai_seat = self._ai_seat(room)
        if ai_seat is not None:
            human = next((s for s in seats if not s.is_ai), None)
            if human is not None:
                return self._seat_color(room, human)
            return WHITE if self._seat_color(room, ai_seat) == BLACK else BLACK
        return self._seat_color(room, seats[0])

    def _append_ai_seat_locked(self, room: GameRoom) -> PlayerSeat:
        """在锁内补一个 AI 座位（调用方负责校验状态、容量与权限）。"""
        token = f"ai-{secrets.token_hex(4)}"
        display_name, _ = _uc_ai_name()
        ai_seat = PlayerSeat(
            number=0,  # 由 _sync_primary_player 统一编号
            visitor_token=token,
            qq=token,
            display_name=display_name,
            identity_confirmed=True,
            is_ai=True,
            ready=True,
        )
        ai_visitor = Visitor(
            number=room.next_visitor_number,
            token=token,
            qq=token,
            display_name=display_name,
            identity_confirmed=True,
        )
        ai_visitor.last_seen_at = time.time()
        ai_visitor.connected = True
        room.next_visitor_number += 1
        room.visitors[token] = ai_visitor
        room.multiplayer.seats.append(ai_seat)
        self._sync_primary_player(room)
        self._assign_duel_sides(room)
        return ai_seat

    def _remove_ai_seats_locked(self, room: GameRoom) -> list[PlayerSeat]:
        """在锁内移除全部 AI 座位，返回被移除的座位。"""
        removed = [seat for seat in room.multiplayer.seats if seat.is_ai]
        if not removed:
            return []
        kept = [seat for seat in room.multiplayer.seats if not seat.is_ai]
        room.multiplayer.seats = kept
        for seat in removed:
            room.visitors.pop(seat.visitor_token, None)
        self._sync_primary_player(room)
        self._assign_duel_sides(room)
        return removed

    def _seats_numbering_frozen(self, room: GameRoom) -> bool:
        """对局进行中冻结座位编号。

        二十一点/谁是卧底在开局时把座位号快照进局内（手牌、发言顺序、投票），
        中途有人掉线被移出玩家席时若重新紧凑编号，剩下的玩家就会“换号”，
        展示号与局内号错位。这里在局面未结束时跳过重编，等本局结束后再收紧。
        """
        game = room.game
        if room.status != "active" or game is None:
            return False
        if isinstance(game, (BlackjackGame, UndercoverGame)):
            return not game.finished
        return False

    def _sync_primary_player(self, room: GameRoom) -> None:
        if not room.multiplayer.enabled or not room.multiplayer.seats:
            self._clear_primary_player(room)
            return
        # 每次入席/换座/离席后按列表顺序重新编号，保证 seat.number 是 1..N；
        # 对局进行中的二十一点/谁是卧底例外：局内号码已快照，重编会造成错位。
        if not self._seats_numbering_frozen(room):
            for idx, seat in enumerate(room.multiplayer.seats):
                seat.number = idx + 1
        # 同步已入座访客的 number 与 seat.number，保证 WebUI “我的号码”＝游戏内座位号，
        # 避免“轮到你却提示不是你的轮次”的错觉
        self._sync_visitor_numbers(room)
        seat = room.multiplayer.seats[0]
        room.player_token = seat.visitor_token
        room.player_qq = seat.qq
        room.player_identity_confirmed = seat.identity_confirmed

    def _sync_visitor_numbers(self, room: GameRoom) -> None:
        """让已入座访客（含 AI）的 number 与 seat.number 保持一致。"""
        for seat in room.multiplayer.seats:
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is not None:
                visitor.number = seat.number

    def _remove_multiplayer_seat(self, room: GameRoom, visitor_token: str) -> None:
        state = room.multiplayer
        old_index = next(
            (
                index
                for index, seat in enumerate(state.seats)
                if seat.visitor_token == visitor_token
            ),
            -1,
        )
        if old_index < 0:
            raise ValueError("该访客当前不在玩家席")
        was_current = old_index == state.current_turn_index
        departing = room.visitors.get(visitor_token)
        if (
            departing is not None
            and isinstance(room.game, BlackjackGame)
            and not room.game.finished
        ):
            hand = room.game.hands.get(_visitor_uid(departing.token))
            if (
                hand is not None
                and hand.status in {"playing", "stand", "blackjack"}
                and not hand.result
            ):
                room.game.surrender(_visitor_uid(departing.token))
        state.seats.pop(old_index)
        if state.seats:
            if old_index < state.current_turn_index:
                state.current_turn_index -= 1
            elif was_current:
                state.current_turn_index %= len(state.seats)
            else:
                state.current_turn_index %= len(state.seats)
        else:
            state.current_turn_index = 0
            state.turn_deadline = 0.0
        if (
            state.seats
            and isinstance(room.game, BlackjackGame)
            and room.status == "active"
            and room.game.phase == "player_turns"
            and not room.game.finished
        ):
            self._advance_blackjack_turn(room)
        state.swap_requests = {
            key: item
            for key, item in state.swap_requests.items()
            if visitor_token not in {item.requester_token, item.target_token}
        }

    def _advance_multiplayer_turn(
        self, room: GameRoom, *, now: float | None = None
    ) -> None:
        state = room.multiplayer
        if not state.enabled or not state.seats:
            return
        current = time.time() if now is None else float(now)
        count = len(state.seats)
        start = state.current_turn_index % count
        chosen = start
        # 玩家出题人只管裁决，不参与提问轮转
        game = room.game
        host_number = (
            int(getattr(game, "host_number", 0) or 0)
            if isinstance(game, TurtleSoupGame)
            else 0
        )
        for offset in range(1, count + 1):
            index = (start + offset) % count
            seat = state.seats[index]
            if host_number and int(seat.number) == host_number:
                continue
            visitor = room.visitors.get(seat.visitor_token)
            if visitor and visitor.connected and current - visitor.last_seen_at < 15:
                chosen = index
                break
        state.current_turn_index = chosen
        self._reset_turn_deadline(room, now=current)

    def _reset_turn_deadline(self, room: GameRoom, *, now: float | None = None) -> None:
        state = room.multiplayer
        game = room.game
        if not state.enabled:
            return
        if isinstance(game, UndercoverGame):
            # 谁是卧底：按当前阶段设置正确的发言/投票时长，避免沿用匹配期的旧时长
            self._reset_undercover_deadline(room, now=now)
            return
        if isinstance(game, BOARD_GAME_CLASSES):
            # 棋类对弈：只有轮到真人座位时才走倒计时；掉线暂停期间保持不变，
            # 由 _tick_duel_locked 决定恢复或判负。
            if state.duel_paused:
                return
            base = time.time() if now is None else float(now)
            owner = self._duel_turn_owner(room)
            active_duel = bool(
                owner is not None
                and not owner.is_ai
                and self._duel_human_pair(room)
            )
            state.turn_deadline = (
                base + state.turn_timeout_seconds
                if active_duel and state.turn_timeout_seconds
                else 0.0
            )
            return
        active = bool(
            state.turn_timeout_seconds
            and state.seats
            and room.status == "active"
            and not (
                isinstance(game, TurtleSoupGame) and self._turtle_soup_free_mode(room)
            )
            and (
                (
                    isinstance(game, TurtleSoupGame)
                    and game.phase == "ready"
                    and not game.processing
                )
                or (
                    isinstance(game, BlackjackGame)
                    and game.phase == "player_turns"
                    and not game.finished
                    and not game.all_players_done()
                )
                or (
                    isinstance(game, UndercoverGame)
                    and not game.finished
                )
            )
        )
        state.turn_deadline = (
            (time.time() if now is None else float(now)) + state.turn_timeout_seconds
            if active
            else 0.0
        )

    def _reset_undercover_deadline(self, room: GameRoom, *, now: float | None = None) -> None:
        """谁是卧底专属：按当前阶段设置合适的单格倒计时时长与截止时间。

        发言/PK → speaking_seconds；投票 → voting_seconds；发词 → prepare_seconds。
        这样前端能实时显示真实倒计时，不再出现“剩余约 0 秒”或沿用匹配期时长。
        """
        state = room.multiplayer
        game = room.game
        if not isinstance(game, UndercoverGame):
            return
        base = time.time() if now is None else float(now)
        if game.phase in ("speech", "pk"):
            duration = max(0, int(getattr(self, "undercover_speaking_seconds", 0) or 0))
        elif game.phase == "voting":
            duration = max(
                0,
                int(
                    getattr(self, "undercover_voting_seconds", 0)
                    or getattr(self, "undercover_speaking_seconds", 0)
                    or 0
                ),
            )
        elif game.phase == "preparing":
            duration = max(0, int(getattr(self, "undercover_prepare_seconds", 0) or 0))
        else:
            duration = 0
        if not duration or game.finished or room.status != "active" or not state.seats:
            state.turn_deadline = 0.0
            # 不清零 turn_timeout_seconds，避免 _tick 分支误判
            return
        state.turn_timeout_seconds = duration
        state.turn_deadline = base + duration

    @staticmethod
    def _purge_swap_requests(room: GameRoom, now: float) -> None:
        state = room.multiplayer
        state.swap_requests = {
            key: item
            for key, item in state.swap_requests.items()
            if item.expires_at > now
            and item.requester_token in room.visitors
            and item.target_token in room.visitors
        }

    def _purge_closed_access(self) -> None:
        now = time.time()
        expired = [
            token
            for token, (_reason, expires_at) in self._closed_access.items()
            if expires_at <= now
        ]
        for token in expired:
            self._closed_access.pop(token, None)
        overflow = len(self._closed_access) - self.MAX_CLOSED_ACCESS_RECORDS
        if overflow > 0:
            oldest = sorted(
                self._closed_access, key=lambda token: self._closed_access[token][1]
            )[:overflow]
            for token in oldest:
                self._closed_access.pop(token, None)

    @staticmethod
    def _visitor(room: GameRoom, token: str) -> Visitor:
        visitor = room.visitors.get(str(token or ""))
        if visitor is None:
            raise PermissionError("访客身份无效，请重新打开房间")
        return visitor

    @staticmethod
    def _visitor_by_number(room: GameRoom, number: int) -> Visitor:
        visitor = next(
            (item for item in room.visitors.values() if item.number == int(number)),
            None,
        )
        if visitor is None:
            raise ValueError(f"房间内没有 {number} 号访客")
        return visitor

    @staticmethod
    def _undercover_player_for_seat(
        room: GameRoom, seat: PlayerSeat | None
    ) -> UCPlayer | None:
        """按稳定 uid 找出座位对应的卧底玩家（座位号会在中途重编，不能用它反查）。"""
        game = room.game
        if seat is None or not isinstance(game, UndercoverGame):
            return None
        return game.player_for_uid(_visitor_uid(seat.visitor_token))

    @staticmethod
    def _visitor_for_undercover_player(
        room: GameRoom, player: UCPlayer | None
    ) -> Visitor | None:
        """按玩家的稳定 uid 找回访客记录（用于文案里的名字与号码）。"""
        if player is None or not player.uid:
            return None
        return next(
            (
                visitor
                for token, visitor in room.visitors.items()
                if _visitor_uid(token) == player.uid
            ),
            None,
        )

    @staticmethod
    def _normalize_strokes(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("画布笔迹格式无效")
        if len(value) > 400:
            raise ValueError("画布笔画过多，请先清理部分内容")
        normalized: list[dict[str, Any]] = []
        total_points = 0
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("画布笔迹格式无效")
            color = str(raw.get("color") or "#202522").lower()
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ValueError("画笔颜色无效")
            try:
                width = float(raw.get("width") or 4)
            except (TypeError, ValueError):
                raise ValueError("画笔粗细无效") from None
            if not math.isfinite(width) or not 1 <= width <= 32:
                raise ValueError("画笔粗细超出范围")
            raw_points = raw.get("points")
            if not isinstance(raw_points, list) or not raw_points:
                raise ValueError("笔画没有有效坐标")
            points: list[list[float]] = []
            for point in raw_points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("笔画坐标格式无效")
                try:
                    x, y = float(point[0]), float(point[1])
                except (TypeError, ValueError):
                    raise ValueError("笔画坐标格式无效") from None
                if not math.isfinite(x) or not math.isfinite(y) or not (
                    0 <= x <= 1 and 0 <= y <= 1
                ):
                    raise ValueError("笔画坐标超出画布")
                points.append([round(x, 5), round(y, 5)])
            total_points += len(points)
            if total_points > 12000:
                raise ValueError("画布坐标过多，请先清理部分内容")
            normalized.append(
                {"color": color, "width": round(width, 2), "points": points}
            )
        return normalized

    def _confirm_visitor_identity(
        self,
        room: GameRoom,
        visitor: Visitor,
        qq: str,
        display_name: str,
        *,
        allow_trusted_enrollment: bool,
    ) -> None:
        visitor.qq = str(qq or "").strip()
        visitor.display_name = str(display_name or "").strip()[:40]
        visitor.identity_confirmed = True
        visitor.binding_token = ""
        visitor.binding_expires_at = 0.0
        visitor.trusted_browser_enrollment_pending = allow_trusted_enrollment
        seat = room.multiplayer.seat_for_token(visitor.token)
        if seat is not None:
            seat.qq = visitor.qq
            seat.display_name = visitor.display_name
            seat.identity_confirmed = True
            room.confirmed_participant_qqs.add(visitor.qq)
            room.participant_names[visitor.qq] = visitor.display_name
            self._sync_primary_player(room)

    @staticmethod
    def _difficulty_label(difficulty: Difficulty) -> str:
        return {"easy": "简单", "normal": "普通", "hard": "困难"}[difficulty]

    def _require_xiangqi_engine(self) -> PikafishService:
        if self.xiangqi_engine is None:
            raise RuntimeError("象棋引擎服务尚未配置")
        return self.xiangqi_engine

    def _is_bot_turn(self, room: GameRoom) -> bool:
        if not room.game:
            return False
        if isinstance(
            room.game,
            (TurtleSoupGame, DrawGuessGame, BlackjackGame, UndercoverGame),
        ):
            return False
        # 两人对弈：只有坐在 AI 座位上的那一方才是「花火的回合」。
        # 两个真人时不触发任何自动落子，否则会替对手走棋。
        if room.multiplayer.enabled and isinstance(
            room.game, BOARD_GAME_CLASSES
        ):
            ai_seat = self._ai_seat(room)
            if ai_seat is None:
                return False
            if isinstance(room.game, XiangqiGame):
                return room.game.turn == self._seat_side(room, ai_seat)
            return room.game.turn == self._seat_color(room, ai_seat)
        return self._game_is_bot_turn(room.game)

    @staticmethod
    def _game_is_bot_turn(
        game: GomokuGame
        | OthelloGame
        | XiangqiGame
        | TicTacToeGame
        | PigDiceGame
        | BlackjackGame,
    ) -> bool:
        if isinstance(game, BlackjackGame):
            return False
        if isinstance(game, UndercoverGame):
            return False
        if isinstance(game, PigDiceGame):
            return game.turn == "bot"
        if isinstance(game, XiangqiGame):
            return game.turn == game.bot_side
        if isinstance(game, (GomokuGame, OthelloGame, WeiqiGame)):
            return game.turn == game.bot_color
        return game.turn == game.bot_mark

    @staticmethod
    def _game_human_won(
        game: (
            GomokuGame
            | OthelloGame
            | XiangqiGame
            | TicTacToeGame
            | TurtleSoupGame
            | PigDiceGame
            | DrawGuessGame
        ),
    ) -> bool:
        if isinstance(game, PigDiceGame):
            return game.winner == "human"
        if isinstance(game, TurtleSoupGame):
            return game.winner == "human"
        if isinstance(game, DrawGuessGame):
            return game.solved
        if isinstance(game, XiangqiGame):
            return game.winner == game.human_side
        if isinstance(game, (GomokuGame, OthelloGame, WeiqiGame)):
            return game.winner == game.human_color
        return game.winner == game.human_mark

    @staticmethod
    def _game_label(game_type: GameType) -> str:
        return {
            "gomoku": "五子棋",
            "xiangqi": "中国象棋",
            "weiqi": "围棋",
            "tictactoe": "井字棋",
            "othello": "黑白棋",
            "turtle_soup": "海龟汤",
            "pig_dice": "贪心骰子",
            "draw_guess": "你画我猜",
            "blackjack": "二十一点",
            "undercover": "谁是卧底",
        }[game_type]

    @staticmethod
    def _turtle_soup_game(room: GameRoom) -> TurtleSoupGame:
        if room.game_type != "turtle_soup" or not isinstance(room.game, TurtleSoupGame):
            raise ValueError("当前房间不是海龟汤")
        return room.game

    def _turtle_soup_free_mode(self, room: GameRoom) -> bool:
        """自由提问：海龟汤常规玩法（Bot 出题）且开启免轮次开关。

        开启后不再强制按玩家席轮次发言，任意玩家席成员都能提问/答题；
        同时关闭回合超时轮换，避免「还没轮到你」卡住多人进度。
        """
        return bool(
            self.turtle_soup_free_questioning
            and isinstance(room.game, TurtleSoupGame)
            and room.game.mode == "bot_host"
        )

    def _require_turtle_soup_player(
        self,
        room: GameRoom,
        *,
        source: str,
        visitor_token: str,
        actor_qq: str,
    ) -> int:
        expected_token = (
            room.multiplayer.current_token
            if room.multiplayer.enabled
            else room.player_token
        )
        free = self._turtle_soup_free_mode(room)
        if source == "web":
            visitor = self._visitor(room, visitor_token)
            if not free and visitor.token != expected_token:
                raise PermissionError("还没有轮到你这个当前玩家推进海龟汤")
            if free:
                # 自由提问：无需轮到顺位，但必须是玩家席成员（观众不能推进游戏）
                if room.multiplayer.enabled:
                    if room.multiplayer.seat_for_token(visitor.token) is None:
                        raise PermissionError("只有玩家席成员可以提问/答题")
                elif not (room.player_token and visitor.token == room.player_token):
                    raise PermissionError("只有玩家席成员可以提问/答题")
            return visitor.number
        if room.multiplayer.enabled:
            seat = room.multiplayer.seat_for_qq(actor_qq)
            if seat is None:
                raise PermissionError("你的 QQ 尚未绑定到玩家席，请在 WebUI 操作")
            if not free and seat.visitor_token != expected_token:
                raise PermissionError("还没有轮到你这个当前玩家推进海龟汤")
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None:
                raise PermissionError("绑定的玩家席已经失效")
            return visitor.number
        if not room.player_qq or str(actor_qq or "") != room.player_qq:
            raise PermissionError("只有当前玩家可以推进海龟汤")
        player = room.player
        if player is None:
            raise PermissionError("当前玩家席已经失效")
        return player.number

    # ------------------------------------------------------------------
    # 谁是卧底：取词 / 发言 / 投票 / PK 续轮
    # ------------------------------------------------------------------

    async def _fetch_undercover_word_pair(self, room: GameRoom) -> tuple[str, str]:
        """先向 plugin 请求 LLM 生成词条，失败或重复时从本地词库 fallback。"""
        payload: dict[str, Any] = {"word_pair": None}
        try:
            await self._emit("undercover_word_pair_requested", room, payload)
        except Exception:
            payload["word_pair"] = None
        pair = payload.get("word_pair")
        if (
            isinstance(pair, tuple)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
            and pair[0].strip()
            and pair[1].strip()
        ):
            return (pair[0].strip(), pair[1].strip())
        store = self.undercover_word_store
        if store is None:
            raise RuntimeError("谁是卧底词库不可用，请检查配置")
        # 本地兜底同样避开已用过的词对，配合 LLM 去重
        exclude: set[tuple[str, str]] = set()
        try:
            exclude = set(store.used_pairs(200))
        except Exception:
            exclude = set()
        return store.random_pair(exclude=exclude)

    async def player_undercover_speech(
        self, room: GameRoom, visitor_token: str, content: str
    ) -> dict[str, Any]:
        cleaned = str(content or "").strip()
        if not cleaned:
            raise ValueError("发言内容不能为空")
        async with room.lock:
            if room.status != "active" or not isinstance(room.game, UndercoverGame):
                raise ValueError("当前没有进行中的谁是卧底对局")
            visitor = self._visitor(room, visitor_token)
            seat = room.multiplayer.seat_for_token(visitor.token)
            if seat is None:
                raise PermissionError("只有已入座玩家可以发言")
            uc_player = self._undercover_player_for_seat(room, seat)
            if uc_player is None:
                raise PermissionError("这局没有你的位置（你可能已被移出玩家席）")
            player_number = uc_player.number
            # AI 玩家的发言豁免相似度拦截：避免最后一轮只剩几名 AI 时本地兜底文案互相雷同被拒而卡死
            accepted = room.game.submit_speech(
                player_number, cleaned, skip_similarity=bool(getattr(seat, "is_ai", False))
            )
            if not accepted:
                reason = getattr(room.game, "last_speech_reject_reason", None)
                if not reason:
                    exp_now = room.game.expected_speaker_number
                    if exp_now is not None and player_number != exp_now:
                        reason = (
                            f"当前轮到 {exp_now} 号发言"
                            + (f"（你现在是 {player_number} 号）" if player_number != exp_now else "")
                            + "，请等待轮到你再提交。"
                        )
                    else:
                        reason = "当前不是你的发言轮次，请等待轮到你再提交。"
                # 相似度拦截只拒绝本次发言：对方已成功的发言保持不变，无需任何人“重新发言”。
                raise ValueError(reason)
            phase = room.game.phase
            round_number = room.game.current_round_number
            expected = room.game.expected_speaker_number
            self._reset_turn_deadline(room)
            room.touch()
            snapshot = room.game.snapshot(player_number)
            # 状态转义：平票 PK 子轮不需要自动推进，等玩家继续
            if expected is None and phase in {"speech", "pk"}:
                # 发言轮结束：系统会在 snapshot 里标记 phase=voting，自动让前端显示投票卡
                if room.game.phase == "voting":
                    room.add_message(
                        "system",
                        f"第 {round_number} 轮发言全部完成，请各位存活玩家投票投出心目中的卧底。",
                    )
            else:
                next_player = self._visitor_for_undercover_player(
                    room,
                    room.game.player_for_number(expected)
                    if expected is not None
                    else None,
                )
                if next_player is not None:
                    label = self._visitor_label(next_player)
                    room.add_message(
                        "system",
                        f"接下来由 {label} 发言。",
                    )
        await self._emit(
            "undercover_speech_submitted",
            room,
            {
                "player_number": player_number,
                "round": round_number,
                "phase": phase,
                "content": cleaned,
                # 非白板发言被词条打码即视为“说出词条”违规（打码只会发生在非白板），携带其 QQ 供外层群禁言。
                # AI 座位的 qq 是内部随机 token（如 ai-xxx），并非真实数字 QQ，跳过禁言。
                "violated_qq": (
                    seat.qq
                    if getattr(room.game, "last_speech_masked", False)
                    and seat.qq
                    and not getattr(seat, "is_ai", False)
                    and str(seat.qq or "").isdigit()
                    else None
                ),
            },
        )
        # 白板发言说出词条会直接结束游戏：触发统一结算流程（发公告/更新战绩）
        if room.game.finished:
            await self._finish_game(room)
        return snapshot

    async def skip_undercover_speaker(self, room: GameRoom, visitor_token: str) -> None:
        """发言超时自动跳过当前发言者：只推进发言指针，不产生占位发言，
        避免时间线残留虚假文案、不进入相似度比对池。"""
        async with room.lock:
            if room.status != "active" or not isinstance(room.game, UndercoverGame):
                return
            if room.game.phase not in ("speech", "pk"):
                return
            visitor = self._visitor(room, visitor_token)
            expected = room.game.expected_speaker_number
            uc_player = self._undercover_player_for_seat(
                room, room.multiplayer.seat_for_token(visitor.token)
            )
            # 优先按稳定 uid 认人；极端情况下（早期快照缺少 uid）退回按号码比对。
            my_number = (
                uc_player.number
                if uc_player is not None
                else visitor.number
            )
            if expected is None or my_number != expected:
                return
            skipped = room.game.expected_speaker_number
            room.game.skip_current_speaker()
            round_number = room.game.current_round_number
            self._reset_turn_deadline(room)
            room.touch()
            room.add_message(
                "system",
                f"{self._visitor_label(visitor)}发言超时，自动跳过本轮。",
            )
            # 推进后若直接进入投票，给出投票提示
            if room.game.phase == "voting":
                room.add_message(
                    "system",
                    f"第 {round_number} 轮发言全部完成，请各位存活玩家投票。",
                )
        await self._emit(
            "undercover_speech_skipped", room, {"player_number": skipped}
        )

    async def player_undercover_vote(
        self, room: GameRoom, visitor_token: str, target_number: int
    ) -> dict[str, Any]:
        target = int(target_number)
        async with room.lock:
            if room.status != "active" or not isinstance(room.game, UndercoverGame):
                raise ValueError("当前没有进行中的谁是卧底对局")
            visitor = self._visitor(room, visitor_token)
            seat = room.multiplayer.seat_for_token(visitor.token)
            if seat is None:
                raise PermissionError("只有已入座玩家可以投票")
            uc_player = self._undercover_player_for_seat(room, seat)
            if uc_player is None:
                raise PermissionError("这局没有你的位置（你可能已被移出玩家席）")
            voter_number = uc_player.number
            result = room.game.submit_vote(voter_number, target)
            self._reset_turn_deadline(room)
            room.touch()
            round_number = room.game.current_round_number
            need_pk = bool(result.get("need_pk"))
            pk_targets = [int(x) for x in result.get("pk_targets") or []]
            out_player = result.get("out_player")
            finished = room.game.finished
            snapshot = room.game.snapshot(voter_number)
            # 开关「展示具体投票人」开启时，房间对话同步附上本轮的逐票明细
            vote_detail = room.game.round_vote_breakdown() if self.undercover_show_voters else ""
            if out_player is not None:
                label = f"{out_player['display_name']}（{out_player['number']}号）"
                msg = f"第 {round_number} 轮投票结束：{label} 被投出局。"
                if vote_detail:
                    msg += f"票型：{vote_detail}。"
                room.add_message("system", msg)
            elif need_pk and pk_targets:
                labels = "、".join(str(x) + "号" for x in pk_targets)
                msg = f"第 {round_number} 轮投票平票：{labels} 最高票相同，进入 PK 发言轮。"
                if vote_detail:
                    msg += f"票型：{vote_detail}。"
                room.add_message("system", msg)
                # 启动 PK 子轮
                room.game.continue_pk(pk_targets)
                room.touch()
        await self._emit(
            "undercover_vote_submitted",
            room,
            {
                "voter_number": voter_number,
                "target_number": target,
                "round": round_number,
                "need_pk": need_pk,
                "pk_targets": pk_targets,
                "out_player_number": (
                    out_player["number"] if out_player is not None else None
                ),
            },
        )
        if finished:
            await self._finish_game(room)
        return {
            "snapshot": snapshot,
            "need_pk": need_pk,
            "pk_targets": pk_targets,
            "finished": finished,
        }

    async def continue_undercover_pk(
        self,
        room: GameRoom,
        visitor_token: str,
        pk_targets: list[int],
    ) -> dict[str, Any]:
        targets = sorted({int(x) for x in (pk_targets or []) if int(x) > 0})
        if len(targets) < 2:
            raise ValueError("PK 子轮至少需要 2 位玩家")
        async with room.lock:
            if room.status != "active" or not isinstance(room.game, UndercoverGame):
                raise ValueError("当前没有进行中的谁是卧底对局")
            visitor = self._visitor(room, visitor_token)
            seat = room.multiplayer.seat_for_token(visitor.token)
            if seat is None:
                raise PermissionError("只有已入座玩家可以推进 PK")
            room.game.continue_pk(targets)
            round_number = room.game.current_round_number
            uc_player = self._undercover_player_for_seat(room, seat)
            player_number = uc_player.number if uc_player is not None else seat.number
            self._reset_turn_deadline(room)
            room.touch()
            snapshot = room.game.snapshot(player_number)
            labels = "、".join(str(x) + "号" for x in targets)
            room.add_message(
                "system",
                f"PK 子轮开始：{labels} 再依次补充描述，之后立即投票分出胜负。",
            )
        await self._emit(
            "undercover_pk_started",
            room,
            {"pk_targets": targets, "round": round_number},
        )
        return snapshot

    async def set_undercover_host_camp_scales(
        self,
        room: GameRoom,
        visitor_token: str,
        scales_str: str,
    ) -> tuple[int, int, int]:
        """首位玩家（房主）自定义默认阵营比例（游戏未开始时才允许）。

        Args:
            scales_str: "4 1 0" 或 "4:1:0" 格式字符串。
        Returns:
            解析后的 (civilian, undercover, whiteboard) 三元组。
        Raises:
            PermissionError: 开关关闭 / 非房主 / 非 1 号玩家 / 对局已开始。
            ValueError: 比例字符串格式错误。
        """
        async with room.lock:
            if not self.undercover_allow_host_customize_camp_scales:
                raise PermissionError("当前房间未开放房主自定义阵营比例")
            if room.status not in ("waiting", "setup"):
                raise PermissionError("对局已开始，不能再修改阵营比例")
            if room.game_type != "undercover":
                raise PermissionError("仅谁是卧底房间支持本设置")
            visitor = self._visitor(room, visitor_token)
            seats = room.multiplayer.seats
            seat_index = next(
                (i for i, s in enumerate(seats) if s.visitor_token == visitor.token),
                -1,
            )
            if seat_index < 0:
                raise PermissionError("您尚未加入玩家席，无法设置阵营比例")
            seat = seats[seat_index]
            # 以列表位置作为「真源」重编 seat.number，避免任何漏 renumber 分支
            for idx, s in enumerate(seats):
                s.number = idx + 1
            if seat.number != 1:
                raise PermissionError(
                    f"只有首位入座玩家（1号房主）可以设置阵营比例（您是{seat.number}号）"
                )
            # —— 上方判断为兼容保留；新的"房主"真源：第一个绑定QQ身份的真人
            host_seat = type(self)._undercover_host_seat(room)
            if host_seat is None:
                raise PermissionError(
                    "房间内暂无已绑定QQ身份的玩家，暂无法作为房主修改房间设置"
                )
            if host_seat.visitor_token != visitor.token:
                raise PermissionError(
                    f"只有第一个绑定QQ身份的玩家（{host_seat.display_name or f'{host_seat.number}号'}）"
                    f"可作为房主修改房间设置"
                )
            parsed = _parse_camp_scales(scales_str)
            if parsed[1] <= 0:
                raise ValueError("阵营比例中卧底数量必须大于 0")
            room.undercover_host_camp_scales = "{} {} {}".format(*parsed)
            room.touch()
            return parsed

    @staticmethod
    def _undercover_host_seat(room: GameRoom) -> PlayerSeat | None:
        """返回"房主" seat：第一个 已绑定QQ身份(identity_confirmed)的 真人玩家。

        管理台房间(admin_room=True)直接返回 seats[0]（管理台进入的"首位访客"就是管理员）。
        若都没有绑定身份 → 返回 None。
        """
        if not room.multiplayer.enabled:
            return None
        seats = room.multiplayer.seats
        if room.admin_room and seats:
            # 管理台房间：第一个入座者作为"管理员"拥有房主权限
            for seat in seats:
                if not seat.is_ai:
                    return seat
            return seats[0] if seats else None
        # 群聊/私聊房间：找第一个 identity_confirmed=True 的真人
        for seat in seats:
            if seat.is_ai:
                continue
            if seat.identity_confirmed:
                return seat
        return None

    def _announce_host_transfer(
        self, room: GameRoom, previous_host_token: str | None
    ) -> None:
        """房主更替时在房内刷一条提示，避免静默交接。

        房主始终按 _undercover_host_seat 动态取「第一个绑定QQ身份的真人」，
        此处只负责在房主位置因退席/解绑发生变迁时，向全房同步新接任者。
        若此前无房主或房主未变（如普通玩家离开），则不打扰。
        """
        if room.game_type != "undercover" or not room.multiplayer.enabled:
            return
        host = type(self)._undercover_host_seat(room)
        if host is None or host.visitor_token == previous_host_token:
            return
        room.add_message(
            "system",
            f"{host.display_name or f'{host.number}号'} 已成为本房房主，"
            "可在集结界面调整阵营比例或追加 AI 玩家。",
        )
        room.touch()

    async def add_ai_seat(
        self,
        room: GameRoom,
        visitor_token: str,
        *,
        side: str = "",
    ) -> dict[str, object]:
        """「和花火对局」：在空座位上放一位花火，凑齐两人后立即开局。

        对外只有「和花火对局」这一种说法，内部仍然是往玩家席里加一个 AI 座位，
        这样执色、轮次、结算都复用同一套座位逻辑，也让「两人对弈」与「人机对局」
        共用一条代码路径。
        """
        if room.game_type == "undercover":
            return await self.add_undercover_ai_seat(room, visitor_token)
        start_required = False
        async with room.lock:
            if room.game_type not in DUEL_GAMES:
                raise PermissionError("当前玩法还不支持和花火对局")
            if room.status not in (
                "waiting",
                "setup",
                "finished",
                "rematch_pending",
            ):
                raise PermissionError("对局进行中，结束后再约花火吧")
            requester = self._visitor(room, visitor_token)
            if room.multiplayer.seat_for_token(requester.token) is None:
                raise PermissionError("请你先入座，再和花火对局")
            # 「和花火对局」是明确操作：刷新挂机让座判定
            self._touch_seat_action(room, requester.token)
            if self._ai_seat(room) is not None:
                return {
                    "added": False,
                    "started": False,
                    "live_count": len(room.multiplayer.seats),
                    "capacity": int(room.multiplayer.capacity or 2),
                }
            capacity = int(room.multiplayer.capacity or 2)
            if len(room.multiplayer.seats) >= capacity:
                raise ValueError("玩家席已经坐满，不用再补对手了")
            ai_seat = self._append_ai_seat_locked(room)
            # 首局尊重入座时选的执方：象棋「我执红 / 我执黑」，
            # 五子棋与黑白棋「我先手（执黑）/ 花火先手（我执白）」，随机时抽一个
            desired = str(side or "").strip().lower()
            if room.game_type == "xiangqi":
                if desired == "random":
                    desired = secrets.choice(("human_red", "human_black"))
                if desired not in {"human_red", "human_black"}:
                    desired = ""
                human_wanted = "red" if desired == "human_red" else "black"
                ai_wanted = "black" if human_wanted == "red" else "red"
            else:
                if desired == "random":
                    desired = secrets.choice(("human_black", "bot_black"))
                if desired not in {"human_black", "bot_black"}:
                    desired = ""
                human_wanted = "white" if desired == "bot_black" else "black"
                ai_wanted = "black" if human_wanted == "white" else "white"
            if desired:
                human_seat = room.multiplayer.seat_for_token(requester.token)
                if human_seat is not None:
                    human_seat.side, ai_seat.side = human_wanted, ai_wanted
            start_required = len(room.multiplayer.seats) >= capacity
            if room.status in ("waiting", "finished", "rematch_pending"):
                room.status = "setup"
            room.touch()
            payload: dict[str, object] = {
                "added": True,
                "started": start_required,
                "number": ai_seat.number,
                "live_count": len(room.multiplayer.seats),
                "capacity": capacity,
            }
        if start_required:
            await self.start_game(room, visitor_token, "")
        return payload

    async def remove_ai_seat(
        self,
        room: GameRoom,
        visitor_token: str,
    ) -> dict[str, object]:
        """「等对手来」：把花火请下座位，把位置留给真人。

        对局中不允许更换对手；局间移除后回到 setup，玩家可以等真人或再约花火。
        """
        if room.game_type == "undercover":
            return await self.remove_undercover_ai_seat(room, visitor_token)
        async with room.lock:
            if room.game_type not in DUEL_GAMES:
                raise PermissionError("当前玩法还不支持和花火对局")
            if room.status in ("active", "paused"):
                raise PermissionError("对局进行中，结束后再更换对手")
            requester = self._visitor(room, visitor_token)
            if (
                not room.admin_room
                and room.multiplayer.seat_for_token(requester.token) is None
            ):
                raise PermissionError("只有玩家席上的玩家才能更换对手")
            # 「等对手来」是明确操作：刷新挂机让座判定
            self._touch_seat_action(room, requester.token)
            removed = self._remove_ai_seats_locked(room)
            if not removed:
                return {
                    "removed": False,
                    "live_count": len(room.multiplayer.seats),
                    "capacity": int(room.multiplayer.capacity or 2),
                }
            # 局间换对手：清掉上一局的棋盘，回到「还差一位对手」
            room.game = None
            room.status = "setup" if room.multiplayer.seats else "waiting"
            room.multiplayer.turn_deadline = 0.0
            room.touch()
            room.add_message(
                "system", "花火离开了座位，这个位置留给下一位对手。"
            )
            return {
                "removed": True,
                "display_name": removed[0].display_name,
                "live_count": len(room.multiplayer.seats),
                "capacity": int(room.multiplayer.capacity or 2),
            }

    async def add_undercover_ai_seat(
        self,
        room: GameRoom,
        visitor_token: str,
    ) -> dict[str, object]:
        """房主/管理员手动追加一位 AI 玩家作为人数不足的补位。

        Returns:
            {"added": True, "display_name": str, "number": int, "live_count": int, "capacity": int}
        """
        import uuid
        async with room.lock:
            if room.game_type != "undercover":
                raise PermissionError("仅谁是卧底房间支持本设置")
            if room.status not in ("waiting", "setup"):
                raise PermissionError("对局已开始，不能再追加 AI 玩家")
            # 权限：管理台房间任意访客可操作；普通房间需是"房主"(第一个绑定身份)
            if not room.admin_room:
                host_seat = type(self)._undercover_host_seat(room)
                requester = self._visitor(room, visitor_token)
                if host_seat is None:
                    raise PermissionError(
                        "房间内暂无已绑定QQ身份的玩家，暂无法追加 AI 玩家"
                    )
                if host_seat.visitor_token != requester.token:
                    raise PermissionError(
                        f"只有房主（{host_seat.display_name or f'{host_seat.number}号'}）才能追加 AI 玩家"
                    )
            capacity = int(room.multiplayer.capacity or 0)
            if capacity <= 0:
                capacity = max(
                    6,
                    int(self.undercover_max_players or 6),
                )
            if len(room.multiplayer.seats) >= capacity:
                raise ValueError(
                    f"玩家席已满（{len(room.multiplayer.seats)}/{capacity}），无法再追加 AI 玩家"
                )
            ai_token = f"ai-{uuid.uuid4().hex[:8]}"
            display_name, _ = _uc_ai_name()  # 例如「火花·调皮(AI)」
            ai_seat = PlayerSeat(
                number=0,  # 稍后统一编号
                visitor_token=ai_token,
                qq=ai_token,
                display_name=display_name,
                identity_confirmed=True,
                is_ai=True,
                ready=True,  # AI 自动算作已准备
            )
            # 注册 AI 到 visitors，让玩家列表/玩家标签/聊天名单可见
            ai_v = Visitor(
                number=room.next_visitor_number,
                token=ai_token,
                qq=ai_token,
                display_name=display_name,
                identity_confirmed=True,
            )
            ai_v.last_seen_at = time.time()
            ai_v.connected = True
            room.next_visitor_number += 1
            room.visitors[ai_token] = ai_v
            room.multiplayer.capacity = capacity
            room.multiplayer.seats.append(ai_seat)
            self._sync_primary_player(room)  # 统一 seat.number
            # 如果满足 AI 补位人数，把倒计时稍微收紧点
            live_count = len(room.multiplayer.seats)
            min_p = int(self.undercover_min_players or 2)
            if live_count >= min_p:
                now = time.time()
                current_dl = float(room.multiplayer.turn_deadline or 0)
                # 保持现有倒计时，或者如果超过 30 秒就缩短到 20 秒（避免用户手动添完AI后仍要等1分钟）
                if current_dl - now > 20:
                    room.multiplayer.turn_deadline = now + 20
            room.touch()
            return {
                "added": True,
                "display_name": display_name,
                "number": ai_seat.number,
                "live_count": live_count,
                "capacity": capacity,
            }

    async def remove_undercover_ai_seat(
        self,
        room: GameRoom,
        visitor_token: str,
    ) -> dict[str, object]:
        """房主/管理员手动移除一位 AI 玩家（每次一个，优先移除编号最大的）。

        Returns:
            {"removed": True, "display_name": str, "number": int, "live_count": int, "capacity": int}
        """
        async with room.lock:
            if room.game_type != "undercover":
                raise PermissionError("仅谁是卧底房间支持本设置")
            if room.status not in ("waiting", "setup"):
                raise PermissionError("对局已开始，不能再移除 AI 玩家")
            # 权限与追加 AI 一致：管理台房间任意访客；普通房间需是房主
            if not room.admin_room:
                host_seat = type(self)._undercover_host_seat(room)
                requester = self._visitor(room, visitor_token)
                if host_seat is None:
                    raise PermissionError(
                        "房间内暂无已绑定QQ身份的玩家，暂无法移除 AI 玩家"
                    )
                if host_seat.visitor_token != requester.token:
                    raise PermissionError(
                        f"只有房主（{host_seat.display_name or f'{host_seat.number}号'}）才能移除 AI 玩家"
                    )
            ai_seats = [s for s in room.multiplayer.seats if s.is_ai]
            if not ai_seats:
                raise ValueError("当前没有可移除的 AI 玩家")
            ai_seat = max(ai_seats, key=lambda s: int(s.number or 0))
            token = ai_seat.visitor_token
            number = ai_seat.number
            display_name = ai_seat.display_name or f"{number}号"
            self._remove_multiplayer_seat(room, token)
            room.visitors.pop(token, None)  # AI 的访客记录是临时注册的，一并清理
            if room.multiplayer.seats:
                self._sync_primary_player(room)
            else:
                self._clear_primary_player(room)
                room.player_empty_since = time.time()
                room.game = None
                room.status = "waiting"
                room.multiplayer.turn_deadline = 0.0
            # AI 自动算已就绪：移除后重置全员就绪确认窗口，重新按在场玩家评估
            self._undercover_all_ready_at.pop(room.room_id, None)
            room.touch()
            return {
                "removed": True,
                "display_name": display_name,
                "number": number,
                "live_count": len(room.multiplayer.seats),
                "capacity": room.multiplayer.capacity or 0,
            }

    async def set_player_ready(
        self, room: GameRoom, visitor_token: str, ready: bool
    ) -> None:
        """玩家在谁是卧底集结阶段点击「准备」/「取消准备」。

        全员就绪后先给出确认窗口再自动开局；否则等待倒计时强制开局。
        """
        async with room.lock:
            if (
                room.game_type != "undercover"
                or room.status != "setup"
                or room.game is not None
            ):
                raise ValueError("当前不是谁是卧底的集结阶段")
            visitor = self._visitor(room, visitor_token)
            seat = room.multiplayer.seat_for_token(visitor.token)
            if seat is None:
                raise PermissionError("您还没有加入玩家席")
            seat.ready = bool(ready)
            room.touch()
            live_seats = [
                s
                for s in room.multiplayer.seats
                if s.visitor_token
                and (s.is_ai or s.visitor_token in room.visitors)
            ]
            all_ready = (
                len(live_seats) >= int(self.undercover_min_players or 2)
                and all(s.ready for s in live_seats)
            )
            if all_ready:
                # 全员就绪：记录确认窗口起点（避免重复提示）；真正开局交给 housekeeping 延时触发
                if self._undercover_all_ready_at.get(room.room_id) is None:
                    self._undercover_all_ready_at[room.room_id] = time.time()
                    room.add_message(
                        "system", "所有玩家均已准备，2 秒后自动开始（可取消准备）。"
                    )
            else:
                self._undercover_all_ready_at.pop(room.room_id, None)
        await self._emit("seats_changed", room, {"ready": bool(ready)})

    async def leave_player_seat(self, room: GameRoom, visitor_token: str) -> None:
        """玩家主动从玩家席退到观众席（仅本人可操作，不销毁房间）。

        复用 _remove_multiplayer_seat 的席位清理逻辑；黑杰克会处理该玩家手牌 surrender。
        若玩家席清空，房间回到 waiting（供重新有人入座开始新对局）。
        """
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            if not room.multiplayer.enabled:
                if room.player_token == visitor.token:
                    self._clear_primary_player(room)
                    room.player_empty_since = time.time()
                    if room.admin_room:
                        room.player_seat_locked = True
                    room.game = None
                    room.status = "waiting"
                    room.touch()
                    room.add_message(
                        "system",
                        f"{self._visitor_label(visitor)}退出玩家席，已回到观众席。",
                    )
                    return
                raise PermissionError("您当前不在玩家席")
            # 对局进行中不允许退席（会在中途破坏发言/投票状态），请等本局结束
            if room.status == "active":
                # 两人对弈只有「对面是花火」时允许中途退出（等于弃局）；
                # 对面是真人时必须等这局结束，否则棋局会直接失去一方。
                opponent_is_human = any(
                    not s.is_ai and s.visitor_token != visitor.token
                    for s in room.multiplayer.seats
                )
                if room.game_type not in DUEL_GAMES or opponent_is_human:
                    raise PermissionError("本局游戏已开始，请稍后再试吧。")
            seat = room.multiplayer.seat_for_token(visitor.token)
            if seat is None:
                raise PermissionError("您当前不在玩家席（观众无需退出）")
            # 谁是卧底进行中退席：标记为该玩家离场，不再参与本轮发言/投票
            if (
                room.game_type == "undercover"
                and room.game is not None
                and getattr(room.game, "players", None)
            ):
                p = room.game.player_for_uid(_visitor_uid(seat.visitor_token))
                if p is not None:
                    p.is_out = True
            # 记录退席前的房主，退席后若房主位置迁移则向全房同步新接任者
            previous_host = type(self)._undercover_host_seat(room)
            previous_host_token = (
                previous_host.visitor_token if previous_host else None
            )
            # _remove_multiplayer_seat 自带黑杰克手牌 surrender 与席位轮转清理
            self._remove_multiplayer_seat(room, visitor.token)
            self._announce_host_transfer(room, previous_host_token)
            if room.game_type in DUEL_GAMES and not self._human_seats(room):
                # 真人全部离席：花火一起离座，房间回到 waiting。
                # 只在 active 时清花火会留下「备战/结算阶段的空壳 AI 座位」，
                # 下一个人入座时座位直接凑满→秒开局，连先手后手都没得选。
                if room.status == "active" and room.game is not None:
                    room.add_message("system", "本局已按弃局处理。")
                room.game = None
                self._remove_ai_seats_locked(room)
                room.status = "waiting"
                room.multiplayer.turn_deadline = 0.0
            if room.multiplayer.seats:
                self._sync_primary_player(room)
                if room.status != "active":
                    self._reset_turn_deadline(room)
                room.player_empty_since = None
            else:
                self._clear_primary_player(room)
                room.player_empty_since = time.time()
                room.game = None
                room.status = "waiting"
                room.multiplayer.turn_deadline = 0.0
            room.touch()
            room.add_message(
                "system",
                f"{self._visitor_label(visitor)}退出玩家席，已回到观众席。",
            )
            # 谁是卧底有人退席：一律先清除「全员就绪确认窗口」标记——
            # 若玩家席因此清空、房间退回 waiting，标记若残留会在下次全员就绪时
            # 被误当成“早已到点”而秒开，所以这里不依赖 room.status 判断直接清除。
            if room.game_type == "undercover":
                had_window = self._undercover_all_ready_at.pop(
                    room.room_id, None
                ) is not None
                if (
                    had_window
                    and room.status == "setup"
                    and room.game is None
                ):
                    room.add_message(
                        "system",
                        "有玩家退出了玩家席，自动开局已重置，等待全员重新就绪。",
                    )
        await self._emit("seats_changed", room, {"left": visitor_token})

    async def set_undercover_reveal_identity(
        self, room: GameRoom, visitor_token: str, reveal_identity: bool
    ) -> bool:
        """房主在集结阶段调整「告知身份」开关，本局生效。"""
        async with room.lock:
            if room.game_type != "undercover":
                raise PermissionError("仅谁是卧底房间支持本设置")
            if room.status not in ("waiting", "setup"):
                raise PermissionError("对局已开始，不能再修改身份告知设置")
            host_seat = type(self)._undercover_host_seat(room)
            requester = self._visitor(room, visitor_token)
            if host_seat is None:
                raise PermissionError(
                    "房间内暂无已绑定QQ身份的玩家，暂无法作为房主修改房间设置"
                )
            if host_seat.visitor_token != requester.token:
                raise PermissionError(
                    f"只有房主（{host_seat.display_name or f'{host_seat.number}号'}）"
                    f"才能修改身份告知设置"
                )
            room.undercover_reveal_identity = bool(reveal_identity)
            room.add_message(
                "system",
                (
                    "房主已开启「告知身份」：开场会同时发放身份与词条卡。"
                    if reveal_identity
                    else "房主已关闭「告知身份」：开场只发放词条，不告知平民/卧底身份。"
                ),
            )
            room.touch()
            return room.undercover_reveal_identity

    async def unbind_identity(self, room: GameRoom, visitor_token: str) -> None:
        """解绑当前访客的 QQ 身份：清空身份并把该访客直接移出房间。

        解绑即视为放弃当前设备：移出玩家席（若入座）并从房间移除该访客，
        防止“安卓解绑 → 电脑重绑”后旧设备占座残留（旧设备页面后续轮询会失效）。
        """
        if room.status == "active":
            raise PermissionError("对局进行中，无法解绑玩家，请等本局结束后再操作")
        async with room.lock:
            visitor = self._visitor(room, visitor_token)
            old_qq = visitor.qq or ""
            # 记录解绑前的房主，解绑后若房主位置迁移则向全房同步新接任者
            previous_host = type(self)._undercover_host_seat(room)
            previous_host_token = (
                previous_host.visitor_token if previous_host else None
            )
            if room.multiplayer.enabled:
                if room.multiplayer.seat_for_token(visitor.token) is not None:
                    self._remove_multiplayer_seat(room, visitor.token)
                    if room.multiplayer.seats:
                        self._sync_primary_player(room)
                    else:
                        self._clear_primary_player(room)
                        room.player_empty_since = time.time()
                        room.game = None
                        room.status = "waiting"
                        room.multiplayer.turn_deadline = 0.0
            else:
                if room.player_token == visitor.token:
                    self._clear_primary_player(room)
                    room.player_empty_since = time.time()
                    room.game = None
                    room.status = "waiting"
            room.visitors.pop(visitor.token, None)
            if room.game_type == "undercover":
                self._undercover_all_ready_at.pop(room.room_id, None)
            if old_qq and old_qq in room.confirmed_participant_qqs:
                room.confirmed_participant_qqs.discard(old_qq)
            self._announce_host_transfer(room, previous_host_token)
            room.touch()
