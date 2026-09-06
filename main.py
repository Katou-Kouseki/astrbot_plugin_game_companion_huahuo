from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request

from .blackjack import BlackjackGame
from .draw_guess import DrawGuessGame
from .gomoku import BLACK as GOMOKU_BLACK
from .gomoku import Difficulty, GomokuGame
from .models import GameRoom, GameType, TurtleSoupMode, Visitor
from .pig_dice import PigDiceGame
from .pikafish import PikafishService
from .room_manager import SUPPORTED_GAMES, RoomManager
from .server import GameRoomServer
from .tictactoe import NOUGHT as TICTACTOE_NOUGHT
from .tictactoe import TicTacToeGame
from .tictactoe import X as TICTACTOE_X
from .trusted_identity import TrustedIdentityStore
from .tunnel import QuickTunnel
from .turtle_soup import (
    VERDICT_LABELS,
    TurtleSoupGame,
    fallback_puzzle,
    normalize_content_level,
    puzzle_from_mapping,
)
from .turtle_soup_ai import (
    answer_judge_prompt,
    extract_json_object,
    generation_prompt,
    parse_answer_judgment,
    parse_question_judgment,
    parse_reverse_turn,
    public_judge_history,
    question_judge_prompt,
    reverse_public_history,
    reverse_turn_prompt,
    validation_passed,
    validation_prompt,
)
from .undercover import UndercoverGame
from .undercover_words import UndercoverWordStore
from .xiangqi import BLACK as XIANGQI_BLACK
from .xiangqi import RED as XIANGQI_RED
from .xiangqi import XiangqiGame

PLUGIN_NAME = "astrbot_plugin_game_companion_huahuo"
PLUGIN_VERSION = "0.3.8"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

GAME_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "game_type": "gomoku",
        "label": "五子棋",
        "description": "15×15 棋盘，由 Bot 人格决定三档棋力。",
        "fields": (),
    },
    {
        "game_type": "xiangqi",
        "label": "中国象棋",
        "description": "使用独立 Pikafish 引擎进行对局。",
        "fields": (
            {
                "key": "allow_engine_download",
                "config_key": "xiangqi.allow_engine_download",
                "label": "允许管理台下载引擎",
                "type": "bool",
                "default": True,
                "hint": "关闭后管理台不能安装或更新 Pikafish。",
            },
            {
                "key": "auto_download_engine",
                "config_key": "xiangqi.auto_download_engine",
                "label": "首次使用时自动下载",
                "type": "bool",
                "default": False,
                "hint": "推荐保持关闭，由管理员先在管理台确认安装。",
            },
        ),
    },
    {
        "game_type": "tictactoe",
        "label": "井字棋",
        "description": "3×3 棋盘，由 Bot 人格决定三档棋力。",
        "fields": (),
    },
    {
        "game_type": "turtle_soup",
        "label": "海龟汤",
        "description": "支持 Bot 出题、玩家出题和多人轮流参与。",
        "fields": (
            {
                "key": "max_hints",
                "config_key": "turtle_soup.max_hints",
                "label": "每题最多提示",
                "type": "int",
                "default": 3,
                "minimum": 0,
                "maximum": 8,
                "unit": "次",
                "hint": "0 表示允许依次查看全部预生成提示。",
            },
            {
                "key": "content_level",
                "config_key": "turtle_soup.content_level",
                "label": "题目内容等级",
                "type": "select",
                "default": "normal",
                "options": (
                    {"value": "all_ages", "label": "全年龄"},
                    {"value": "normal", "label": "普通"},
                    {"value": "unrestricted", "label": "不限制"},
                ),
                "hint": "仍会遵守模型和平台安全限制。",
            },
            {
                "key": "max_players",
                "config_key": "turtle_soup.max_players",
                "label": "最大玩家席",
                "type": "int",
                "default": 6,
                "minimum": 0,
                "maximum": 100,
                "unit": "人",
                "hint": "0 表示不限制人数。",
            },
            {
                "key": "turn_timeout_seconds",
                "config_key": "multiplayer.turn_timeout_seconds",
                "label": "单人回合时间",
                "type": "int",
                "default": 60,
                "minimum": 0,
                "maximum": 3600,
                "unit": "秒",
                "hint": "0 表示关闭回合倒计时。",
            },
            {
                "key": "swap_request_cooldown_seconds",
                "config_key": "multiplayer.swap_request_cooldown_seconds",
                "label": "交换申请冷却",
                "type": "int",
                "default": 30,
                "minimum": 0,
                "maximum": 3600,
                "unit": "秒",
                "hint": "0 表示不限制申请频率。",
            },
            {
                "key": "swap_request_expiry_seconds",
                "config_key": "multiplayer.swap_request_expiry_seconds",
                "label": "交换申请有效期",
                "type": "int",
                "default": 20,
                "minimum": 1,
                "maximum": 600,
                "unit": "秒",
                "hint": "过期申请会自动清理。",
            },
        ),
    },
    {
        "game_type": "pig_dice",
        "label": "贪心骰子",
        "description": "继续冒险或及时收手，由 Bot 人格决定风险倾向。",
        "fields": (
            {
                "key": "target_score",
                "config_key": "pig_dice.target_score",
                "label": "获胜目标分数",
                "type": "int",
                "default": 50,
                "minimum": 20,
                "maximum": 200,
                "unit": "分",
                "hint": "新一局中先达到目标分数的一方获胜。",
            },
        ),
    },
    {
        "game_type": "draw_guess",
        "label": "你画我猜",
        "description": "用户作画，Bot 使用视觉模型猜图。",
        "fields": (
            {
                "key": "vision_provider_id",
                "config_key": "draw_guess.vision_provider_id",
                "label": "视觉模型 Provider ID",
                "type": "string",
                "default": "agnes-ai/agnes-image-2.5-flash",
                "maximum_length": 200,
                "hint": "你画我猜使用的视觉 Provider ID；留空时使用当前会话模型。",
            },
            {
                "key": "duration_seconds",
                "config_key": "draw_guess.duration_seconds",
                "label": "作画倒计时",
                "type": "int",
                "default": 120,
                "minimum": 10,
                "maximum": 600,
                "unit": "秒",
                "hint": "倒计时结束后本轮自动结算。",
            },
            {
                "key": "max_guesses",
                "config_key": "draw_guess.max_guesses",
                "label": "Bot 最大猜测次数",
                "type": "int",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
                "unit": "次",
                "hint": "每次点击让 Bot 猜都会消耗一次。",
            },
        ),
    },
    {
        "game_type": "blackjack",
        "label": "二十一点",
        "description": "用户当闲家、Bot 当庄家比点数；支持 1-6 位玩家各自对庄。",
        "fields": (
            {
                "key": "max_players",
                "config_key": "blackjack.max_players",
                "label": "最大玩家席",
                "type": "int",
                "default": 1,
                "minimum": 1,
                "maximum": 6,
                "unit": "人",
                "hint": "默认 1 人，即用户单独和 Bot 庄家对局。",
            },
        ),
    },
    {
        "game_type": "undercover",
        "label": "谁是卧底",
        "description": "多人社交推理：每人随机词条，按轮次发言描述，投票淘汰可疑玩家。",
        "fields": (
            {
                "key": "max_players",
                "config_key": "undercover.max_players",
                "label": "最大玩家席",
                "type": "int",
                "default": 10,
                "minimum": 2,
                "maximum": 20,
                "unit": "人",
                "hint": "默认 10 人；2 人即可开局（民 1 + 卧底 1）。",
            },
            {
                "key": "min_players",
                "config_key": "undercover.min_players",
                "label": "最低开局人数",
                "type": "int",
                "default": 2,
                "minimum": 2,
                "maximum": 10,
                "unit": "人",
                "hint": "低于该人数不会自动开局；手动开始也会拒绝。",
            },
            {
                "key": "camp_scales_default",
                "config_key": "undercover.camp_scales_default",
                "label": "默认阵营比例（平民 卧底 白板）",
                "type": "str",
                "default": "4 1 0",
                "hint": "以空格或冒号分隔，如 4:1:0；实际会按参与人数按比例分配，保证卧底至少 1 人。",
            },
            {
                "key": "allow_host_customize_camp_scales",
                "config_key": "undercover.allow_host_customize_camp_scales",
                "label": "允许首位玩家（房主）自定义阵营比例",
                "type": "bool",
                "default": True,
                "hint": "开启后，第一个进入谁是卧底房间玩家席的玩家可以在 WebUI 调整默认的民/卧/白比例，无需进入管理台。",
            },
            {
                "key": "match_seconds",
                "config_key": "undercover.match_seconds",
                "label": "匹配等待时长",
                "type": "int",
                "default": 180,
                "minimum": 10,
                "maximum": 600,
                "unit": "秒",
                "hint": "达到上限时若仍未凑满最大席位，按当前已入座人数开局。",
            },
            {
                "key": "prepare_seconds",
                "config_key": "undercover.prepare_seconds",
                "label": "发词准备时长",
                "type": "int",
                "default": 10,
                "minimum": 0,
                "maximum": 120,
                "unit": "秒",
                "hint": "开局前让玩家查看身份词条的缓冲时间。",
            },
            {
                "key": "speaking_seconds",
                "config_key": "undercover.speaking_seconds",
                "label": "单人次发言时长上限",
                "type": "int",
                "default": 160,
                "minimum": 0,
                "maximum": 600,
                "unit": "秒",
                "hint": "默认 160 秒；设为 0 表示由前端自动跳过当前玩家（本轮不做超时强跳）。",
            },
            {
                "key": "voting_seconds",
                "config_key": "undercover.voting_seconds",
                "label": "投票时长上限",
                "type": "int",
                "default": 120,
                "minimum": 0,
                "maximum": 600,
                "unit": "秒",
                "hint": "默认 120 秒；超时未投票视为本轮弃权。",
            },
            {
                "key": "first_round_non_voting",
                "config_key": "undercover.first_round_non_voting",
                "label": "首轮不投票的最低存活人数",
                "type": "int",
                "default": 3,
                "minimum": 2,
                "maximum": 10,
                "unit": "人",
                "hint": "首轮存活人数 ≤ 该值时，第一轮发言后直接进入下一轮，不再投票。",
            },
            {
                "key": "send_identity_in_card",
                "config_key": "undercover.send_identity_in_card",
                "label": "告知身份（开场发放身份/词条卡）",
                "type": "bool",
                "default": True,
                "hint": "开启后开场会以动画卡片告知玩家身份与词条；关闭则不展示身份卡。",
            },
            {
                "key": "show_voters",
                "config_key": "undercover.show_voters",
                "label": "投票结算展示具体投票人",
                "type": "bool",
                "default": False,
                "hint": "开启后，投票结果会展示每张票投给了谁（时间线与房间对话同步）；关闭则只显示各玩家得票数与被淘汰结果。",
            },
            {
                "key": "similarity",
                "config_key": "undercover.similarity",
                "label": "发言相似度阈值",
                "type": "int",
                "default": 80,
                "minimum": 0,
                "maximum": 100,
                "unit": "%",
                "hint": "发言与历史发言相似度超过该阈值会被驳回并要求换说法，防止复读；0 表示不检测。",
            },
            {
                "key": "failed_mute_seconds",
                "config_key": "undercover.failed_mute_seconds",
                "label": "失败方禁言时长",
                "type": "int",
                "default": 60,
                "minimum": 0,
                "maximum": 3600,
                "unit": "秒",
                "hint": "游戏失败阵营被禁言的秒数；0 表示不禁言。",
            },
            {
                "key": "violated_mute_seconds",
                "config_key": "undercover.violated_mute_seconds",
                "label": "违规（说出词条）禁言时长",
                "type": "int",
                "default": 300,
                "minimum": 0,
                "maximum": 3600,
                "unit": "秒",
                "hint": "平民/卧底误说自己的词条后禁言秒数；0 表示不禁言。",
            },
            {
                "key": "ai_fill_enabled",
                "config_key": "undercover.ai_fill_enabled",
                "label": "开启 AI 玩家自动补位（可作为人数不足的后备玩法）",
                "type": "bool",
                "default": True,
                "hint": "开启后，如果人数少于最低开局人数，Bot 会自动以 AI 玩家身份补位。AI 玩家会自动发言描述和投票。",
            },
            {
                "key": "ai_fill_min_players",
                "config_key": "undercover.ai_fill_min_players",
                "label": "AI 补位后的最低总人数",
                "type": "int",
                "default": 3,
                "minimum": 2,
                "maximum": 8,
                "unit": "人",
                "hint": "真实玩家 + AI 玩家达到该人数才开局；真实玩家如果已≥最低开局人数，则不强制补 AI。",
            },
        ),
    },
)


@dataclass(slots=True)
class _RecentPrivateGameResult:
    room_id: str
    user_qq: str
    summary: str
    expires_at: float = 0.0


def _sanitize_uc_ai_speech(text: str) -> str:
    """把 AI 发言规整为「不超过 1 个逗号、总体 20 字以内」的短句。"""
    text = str(text or "").strip().strip("“”\"'「」")
    # 最多保留 1 个逗号：按中文/英文逗号、顿号、分号切分，只保留前两句
    parts = re.split(r"[，,、；;]", text)
    if len(parts) > 2:
        text = (parts[0].strip() + "，" + parts[1].strip()) if parts[1].strip() else parts[0].strip()
    # 总体不超过 20 字
    text = text[:20].strip()
    # 去掉可能残留的首尾标点
    return text.strip("，,、；;。！？!?")


def _uc_ai_fallback(camp: str, round_no: int) -> str:
    """谁是卧底 AI 发言的本地兜底文案：按轮次轮换，避免整局复读同一句，且尽量不露馅。"""
    index = max(0, (round_no - 1) % 4)
    if camp == "whiteboard":
        pool = [
            "刚看到它的时候我还愣了一下，好像之前在哪见过。",
            "怎么说呢，它给我的第一印象就是挺顺手、挺实用的。",
            "反正最近家里一直在用，我媳妇还念叨来着。",
            "这个嘛，跟别的比起来没什么好挑的，习惯了就好。",
        ]
    else:
        pool = [
            "说起来昨天我还用到它了，当时就觉得挺顺手的。",
            "我倒是觉得它挺经用的，家里那个用了好几年也没坏。",
            "反正吧，它在我这儿的存在感挺高的，一天不落。",
            "这东西说不上稀罕，但少了它还真有点不方便。",
        ]
    return pool[index]


@register(
    PLUGIN_NAME,
    "Katou-Kouseki",
    "让 Bot 与用户通过可视化房间自然地一起玩游戏。",
    PLUGIN_VERSION,
)
class GameCompanionPlugin(Star):
    """Game rooms that preserve AstrBot's normal conversation pipeline."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_root = Path(__file__).resolve().parent
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.server_enabled = self._cfg_bool("server.enabled", True)
        self.server_host = self._cfg_str("server.host", "127.0.0.1") or "127.0.0.1"
        self.access_host = self._cfg_str("server.access_host", "")
        self.server_port = self._cfg_int("server.port", 6331, minimum=1, maximum=65535)
        self.public_base_url = self._validated_public_url(
            self._cfg_str("server.public_base_url", "")
        )
        self.external_base_url = self._validated_external_url(
            self._cfg_str("server.external_base_url", "")
        )
        self.auto_quick_tunnel = self._cfg_bool("server.auto_quick_tunnel", True)
        self.cloudflared_path = self._cfg_str("server.cloudflared_path", "")
        self.cloudflared_download_proxy = self._cfg_str(
            "server.cloudflared_download_proxy", ""
        )
        self.allow_cloudflared_download = self._cfg_bool(
            "server.allow_cloudflared_download", True
        )
        self.log_level = self._cfg_str("logging.level", "inherit").lower() or "inherit"
        self._apply_log_level()
        self.trusted_browser_requested = self._cfg_bool(
            "identity.enable_trusted_browser", False
        )
        self.trusted_browser_ttl_days = self._cfg_int(
            "identity.trusted_browser_ttl_days", 30, minimum=1, maximum=365
        )
        configured_access = self._configured_access_base()
        self.trusted_browser_enabled = bool(
            self.trusted_browser_requested
            and configured_access
            and urlsplit(configured_access).scheme == "https"
        )
        public_path = urlsplit(configured_access).path.rstrip("/")
        self.trusted_browser_cookie_path = public_path or "/"
        self.trusted_identity_store = TrustedIdentityStore(
            self.data_dir / "trusted_browsers.json",
            ttl_days=self.trusted_browser_ttl_days,
        )
        if self.trusted_browser_requested and not self.trusted_browser_enabled:
            logger.warning(
                "[GameCompanion] 受信任浏览器需要有效的 HTTPS 外部地址，当前已自动禁用"
            )

        self.group_rooms_enabled = self._cfg_bool("rooms.enable_group_rooms", True)
        self.private_rooms_enabled = self._cfg_bool("rooms.enable_private_rooms", True)
        self.allow_non_admin_group_creation = self._cfg_bool(
            "rooms.allow_non_admin_group_creation", False
        )
        self.game_admin_ids = self._parse_qq_ids(
            self._cfg("rooms.game_admin_qq_ids", "")
        )
        self.record_shared_experience = self._cfg_bool(
            "memory.record_shared_experience", True
        )
        self.private_qq_game_context_enabled = self._cfg_bool(
            "context.enable_private_qq_game_context", True
        )
        self.recent_game_result_ttl_seconds = self._cfg_int(
            "context.recent_game_result_ttl_minutes",
            30,
            minimum=0,
            maximum=24 * 60,
        ) * 60
        self.companion_afterglow_enabled = self._cfg_bool(
            "companion_integration.enable_emotional_afterglow", False
        )
        self.companion_invites_enabled = self._cfg_bool(
            "companion_integration.enable_proactive_invites", False
        )
        self.companion_invite_probability = self._cfg_int(
            "companion_integration.proactive_invite_probability_percent",
            18,
            minimum=0,
            maximum=100,
        ) / 100.0
        self.companion_invite_cooldown_hours = self._cfg_int(
            "companion_integration.proactive_invite_cooldown_hours",
            24,
            minimum=0,
            maximum=24 * 30,
        )
        self.commentary_cooldown = self._cfg_int(
            "game.commentary_cooldown_seconds", 45, minimum=10, maximum=600
        )
        self.enabled_games: dict[GameType, bool] = {
            game_type: self._cfg_bool(f"{game_type}.enabled", True)
            for game_type in SUPPORTED_GAMES
        }
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=8
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
        )
        self.turtle_soup_max_players = self._cfg_non_negative(
            "turtle_soup.max_players", 6
        )
        self.draw_guess_vision_provider_id = self._cfg_str(
            "draw_guess.vision_provider_id", "agnes-ai/agnes-image-2.5-flash"
        )
        self.draw_guess_max_guesses = self._cfg_int(
            "draw_guess.max_guesses", 5, minimum=1, maximum=10
        )
        self.draw_guess_duration_seconds = self._cfg_int(
            "draw_guess.duration_seconds", 120, minimum=10, maximum=600
        )
        self.pig_dice_target_score = self._cfg_int(
            "pig_dice.target_score", 50, minimum=20, maximum=200
        )
        self.blackjack_max_players = self._cfg_int(
            "blackjack.max_players", 1, minimum=1, maximum=6
        )
        # ------------------------ 谁是卧底配置 ------------------------
        self.undercover_max_players = self._cfg_int(
            "undercover.max_players", 10, minimum=2, maximum=20
        )
        self.undercover_min_players = self._cfg_int(
            "undercover.min_players", 2, minimum=2, maximum=10
        )
        self.undercover_camp_scales_default = (
            self._cfg_str("undercover.camp_scales_default", "4 1 0") or "4 1 0"
        )
        self.undercover_allow_host_customize_camp_scales = self._cfg_bool(
            "undercover.allow_host_customize_camp_scales", True
        )
        self.undercover_match_seconds = self._cfg_int(
            "undercover.match_seconds", 180, minimum=10, maximum=600
        )
        self.undercover_prepare_seconds = self._cfg_int(
            "undercover.prepare_seconds", 10, minimum=0, maximum=120
        )
        self.undercover_speaking_seconds = self._cfg_int(
            "undercover.speaking_seconds", 160, minimum=0, maximum=600
        )
        self.undercover_voting_seconds = self._cfg_int(
            "undercover.voting_seconds", 120, minimum=0, maximum=600
        )
        self.undercover_first_round_non_voting = self._cfg_int(
            "undercover.first_round_non_voting", 3, minimum=2, maximum=10
        )
        self.undercover_send_identity_in_card = self._cfg_bool(
            "undercover.send_identity_in_card", True
        )
        self.undercover_show_voters = self._cfg_bool(
            "undercover.show_voters", False
        )
        self.undercover_similarity = self._cfg_int(
            "undercover.similarity", 80, minimum=0, maximum=100
        )
        # 失败禁言 / 违规禁言（单位：秒，0 表示不禁言；默认 60 / 300）
        self.undercover_failed_mute_seconds = self._cfg_int(
            "undercover.failed_mute_seconds", 60, minimum=0, maximum=3600
        )
        self.undercover_violated_mute_seconds = self._cfg_int(
            "undercover.violated_mute_seconds", 300, minimum=0, maximum=3600
        )
        self.undercover_ai_fill_enabled = self._cfg_bool(
            "undercover.ai_fill_enabled", True
        )
        self.undercover_ai_fill_min_players = self._cfg_int(
            "undercover.ai_fill_min_players", 3, minimum=2, maximum=8
        )
        # 去重窗口：记录 N 次抽取到的词条，避免短时间重复
        self._undercover_word_window: list[tuple[str, str]] = []
        self._undercover_word_window_max = 10
        self.undercover_word_store = UndercoverWordStore(
            self.data_dir / "undercover_words.json"
        )
        self.multiplayer_turn_timeout = self._cfg_non_negative(
            "multiplayer.turn_timeout_seconds", 60
        )
        self.swap_request_cooldown = self._cfg_non_negative(
            "multiplayer.swap_request_cooldown_seconds", 30
        )
        self.swap_request_expiry = self._cfg_int(
            "multiplayer.swap_request_expiry_seconds", 20, minimum=1, maximum=600
        )

        self.xiangqi_engine = PikafishService(
            data_dir=self.data_dir,
            configured_path=self._cfg_str("xiangqi.engine_path", ""),
            download_proxy=self._cfg_str("xiangqi.download_proxy", ""),
            allow_download=self._cfg_bool("xiangqi.allow_engine_download", True),
            auto_download=self._cfg_bool("xiangqi.auto_download_engine", False),
        )

        self.manager = RoomManager(
            max_group_rooms=self._cfg_non_negative("rooms.max_group_rooms", 1),
            max_private_rooms=self._cfg_non_negative("rooms.max_private_rooms", 1),
            empty_player_timeout=self._cfg_non_negative(
                "rooms.empty_player_timeout_seconds", 60
            ),
            idle_timeout=self._cfg_non_negative("rooms.idle_timeout_seconds", 300),
            turtle_soup_max_hints=self.turtle_soup_max_hints,
            turtle_soup_content_level=self.turtle_soup_content_level,
            turtle_soup_max_players=self.turtle_soup_max_players,
            multiplayer_turn_timeout=self.multiplayer_turn_timeout,
            swap_request_cooldown=self.swap_request_cooldown,
            swap_request_expiry=self.swap_request_expiry,
            draw_guess_max_guesses=self.draw_guess_max_guesses,
            draw_guess_duration_seconds=self.draw_guess_duration_seconds,
            pig_dice_target_score=self.pig_dice_target_score,
            blackjack_max_players=self.blackjack_max_players,
            undercover_max_players=self.undercover_max_players,
            undercover_min_players=self.undercover_min_players,
            undercover_camp_scales_default=self.undercover_camp_scales_default,
            undercover_allow_host_customize_camp_scales=self.undercover_allow_host_customize_camp_scales,
            undercover_match_seconds=self.undercover_match_seconds,
            undercover_prepare_seconds=self.undercover_prepare_seconds,
            undercover_speaking_seconds=self.undercover_speaking_seconds,
            undercover_voting_seconds=self.undercover_voting_seconds,
            undercover_first_round_non_voting=self.undercover_first_round_non_voting,
            undercover_send_identity_in_card=self.undercover_send_identity_in_card,
            undercover_show_voters=self.undercover_show_voters,
            undercover_similarity=self.undercover_similarity,
            undercover_ai_fill_enabled=self.undercover_ai_fill_enabled,
            undercover_ai_fill_min_players=self.undercover_ai_fill_min_players,
            undercover_word_store=self.undercover_word_store,
            enabled_games=self.enabled_games,
            xiangqi_engine=self.xiangqi_engine,
            global_stats_path=self.data_dir / "global_leaderboard.json",
            event_callback=self._on_room_event,
        )
        self.room_server = GameRoomServer(
            self,
            self.manager,
            host=self.server_host,
            port=self.server_port,
            web_root=self.plugin_root / "web",
        )
        self.quick_tunnel = QuickTunnel(
            self.room_server.local_base_url,
            search_paths=[
                self.data_dir.parent.parent / "tools" / "bin",
                self.plugin_root / "tools",
            ],
            configured_path=self.cloudflared_path,
            download_dir=self.data_dir / "tools" / "bin",
            download_proxy=self.cloudflared_download_proxy,
            allow_download=self.allow_cloudflared_download,
        )
        self._watchdog_task: asyncio.Task | None = None
        self._tunnel_recovery_task: asyncio.Task | None = None
        self._next_tunnel_retry_at = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._companion_round_event_tasks: dict[str, asyncio.Task] = {}
        self._recent_private_game_results: dict[str, _RecentPrivateGameResult] = {}
        self._companion_invite_api: Any | None = None
        self._next_companion_registration_at = 0.0
        self._settings_lock = asyncio.Lock()
        self._register_page_api()

    def mobile_status(self, *, via_mobile_gateway: bool = False) -> dict[str, Any]:
        """Expose the game catalog to the authenticated companion gateway."""
        games = [
            {
                "game_type": str(item.get("game_type") or ""),
                "label": str(item.get("label") or ""),
                "description": str(item.get("description") or ""),
                "enabled": bool(self.enabled_games.get(str(item.get("game_type")), False)),
            }
            for item in GAME_CATALOG
        ]
        ready = bool(self.server_enabled and self.private_rooms_enabled)
        blockers: list[str] = []
        if not self.server_enabled:
            blockers.append("游戏房间服务未启用")
        if not self.private_rooms_enabled:
            blockers.append("私聊游戏房间未启用")
        local_access_available = bool(self._local_access_base())
        if (
            not via_mobile_gateway
            and not self._configured_access_base()
            and not local_access_available
            and not bool(getattr(self.quick_tunnel, "ready", False))
        ):
            ready = False
            blockers.append("手机房间需要可访问的监听地址或固定 HTTPS 地址")
        if not any(item["enabled"] for item in games):
            ready = False
            blockers.append("没有已启用的游戏")
        return {
            "available": True,
            "enabled": self.server_enabled,
            "running": self.room_server.running,
            "ready": ready,
            "blockers": blockers,
            "games": games,
        }

    async def mobile_create_room(
        self,
        user_id: str,
        game_type: str,
        *,
        via_mobile_gateway: bool = False,
    ) -> dict[str, Any]:
        """Create a game room for a paired phone user and return its WebUI URL."""
        normalized_user = str(user_id or "").strip()[:120]
        if not normalized_user:
            raise ValueError("手机陪伴用户身份不能为空")
        selected_game = self._game_type(game_type)
        if not self.server_enabled:
            raise RuntimeError("游戏房间服务已在插件配置中关闭")
        if not self.private_rooms_enabled:
            raise PermissionError("私聊创建游戏房间已关闭")
        if not self.manager.game_enabled(selected_game):
            raise PermissionError(f"管理员暂未开放{self._game_label(selected_game)}")

        session_id = f"mobile:{normalized_user}"
        rooms = self.manager.for_session(session_id)
        if len(rooms) > 1:
            raise ValueError("当前手机陪伴用户已有多个活动房间")
        if via_mobile_gateway:
            if not self.room_server.running:
                await self.room_server.start()
            mobile_base_url = self.room_server.local_base_url
        else:
            mobile_base_url = await self._ensure_mobile_room_access()
        reused = bool(rooms)
        switched_game = False
        if reused:
            room = rooms[0]
            visitor_token = room.player_token
            if not visitor_token:
                visitor = await self.manager.join(room)
                await self.manager.assign_player(
                    room,
                    visitor.number,
                    normalized_user,
                    allow_non_numeric=True,
                )
                visitor_token = visitor.token
            if room.game_type != selected_game:
                switched_game = await self.manager.switch_game(
                    room,
                    selected_game,
                    force=True,
                )
                await self.manager.start_game(room, visitor_token, "human_black")
        else:
            if selected_game == "xiangqi":
                await self.xiangqi_engine.ensure_ready()
            room = await self.manager.create_room(
                source="private",
                session_id=session_id,
                platform="android",
                group_id="",
                creator_qq=normalized_user,
                creator_name="手机陪伴终端",
                admin_room=False,
                game_type=selected_game,
                difficulty="normal",
                turtle_soup_mode="bot_host",
            )
            visitor = await self.manager.join(room)
            await self.manager.assign_player(
                room,
                visitor.number,
                normalized_user,
                allow_non_numeric=True,
            )
            await self.manager.start_game(room, visitor.token, "human_black")
            visitor_token = visitor.token

        url = (
            f"{mobile_base_url.rstrip('/')}/room/{quote(room.access_token, safe='')}"
            f"?visitor_token={quote(visitor_token, safe='')}"
        )
        logger.info(
            "[GameCompanion] 移动端房间已准备: room=%s game=%s reused=%s access=%s",
            room.room_id,
            room.game_type,
            reused,
            mobile_base_url,
        )
        return {
            "url": url,
            "room_id": room.room_id,
            "game_type": room.game_type,
            "reused_room": reused,
            "switched_game": switched_game,
        }

    async def _ensure_mobile_room_access(self) -> str:
        """Start the room server without forcing a public tunnel for LAN phones."""
        if not self.room_server.running:
            await self.room_server.start()
        configured_access = self._configured_access_base()
        if configured_access:
            logger.info("[GameCompanion] 移动端使用配置的外部地址: %s", configured_access)
            return configured_access
        if bool(getattr(self.quick_tunnel, "ready", False)) and self.quick_tunnel.url:
            return self.quick_tunnel.url
        local_access = self._local_access_base()
        if local_access:
            logger.info("[GameCompanion] 移动端使用局域网访问地址: %s", local_access)
            return local_access
        if str(self.server_host).strip().lower() in {"127.0.0.1", "localhost", "::1"}:
            if not self.auto_quick_tunnel:
                raise RuntimeError("手机房间需要可访问的监听地址或固定 HTTPS 地址")
            await self._ensure_public_access()
            if bool(getattr(self.quick_tunnel, "ready", False)) and self.quick_tunnel.url:
                return self.quick_tunnel.url
            raise RuntimeError("手机房间访问通道尚未就绪")
        if not self.auto_quick_tunnel:
            fallback = self._local_access_base(allow_unresolved=True)
            if fallback:
                logger.warning(
                    "[GameCompanion] 无法自动确认局域网地址，返回监听地址 %s；"
                    "建议配置 server.access_host",
                    fallback,
                )
                return fallback
            raise RuntimeError("手机房间需要可访问的监听地址或固定 HTTPS 地址")
        await self._ensure_public_access()
        if bool(getattr(self.quick_tunnel, "ready", False)) and self.quick_tunnel.url:
            return self.quick_tunnel.url
        raise RuntimeError("手机房间访问通道尚未就绪")

    async def initialize(self) -> None:
        """Start only the in-memory watchdog; the port opens lazily on demand."""
        self._watchdog_task = asyncio.create_task(self._watchdog())
        self._register_companion_invite_ability()
        logger.info(
            "[GameCompanion] 花火陪你玩已加载；房间服务将在首次创建房间时按需启动"
        )

    async def terminate(self) -> None:
        """Invalidate every room and stop only plugin-owned resources."""
        self._unregister_companion_invite_ability()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        await self.manager.close_all("AstrBot 或游戏插件已重载")
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self.quick_tunnel.stop()
        await self.room_server.stop()
        await self.xiangqi_engine.close()
        logger.info("[GameCompanion] 所有运行态房间均已销毁")

    @filter.llm_tool(name="game_companion_create_room")
    async def create_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """仅在用户明确想和 Bot 玩游戏时创建可视化游戏房间。

        难度必须由你结合当前人格、关系和用户请求自行决定，不能把难度选择交给网页用户。
        支持 gomoku（五子棋）、xiangqi（中国象棋）、tictactoe（井字棋）、
        turtle_soup（海龟汤）、pig_dice（贪心骰子）、draw_guess（你画我猜）、
        blackjack（二十一点，Bot 当庄家）和 undercover（谁是卧底，多人社交推理）。
        不要因为普通聊天中偶然提到游戏名称就调用本工具。
        当前 QQ 会话已有房间时只返回原房间入口；切换游戏、再来一局和其他局内操作
        全部由用户进入 WebUI 后完成，不能在 QQ 中代替用户执行。

        Args:
            game_type(string): 游戏类型，只能是 gomoku、xiangqi、tictactoe、turtle_soup、pig_dice、draw_guess、blackjack 或 undercover。
            difficulty(string): 你决定使用的难度，只能是 easy、normal、hard；贪心骰子中分别表示稳健、均衡和大胆，二十一点中影响庄家软 17 规则；谁是卧底时忽略。
            turtle_soup_mode(string): 海龟汤玩法；bot_host 表示 Bot 出题玩家猜，player_host 表示玩家给线索 Bot 猜。非海龟汤时忽略。
            admin_room(boolean): 仅当群聊中的游戏管理员明确要求创建管理员房间时传 true。普通群聊房间必须传 false；非游戏管理员不能创建管理员房间。
            confirm_abandon(boolean): 切换游戏且当前局未结束时，用户是否已明确同意放弃本局。
        """
        try:
            game_type = self._game_type(kwargs.get("game_type"))
        except ValueError as exc:
            return self._json_error(str(exc))
        difficulty = self._difficulty(kwargs.get("difficulty"))
        turtle_soup_mode = self._turtle_soup_mode(kwargs.get("turtle_soup_mode"))
        try:
            room, reused, restarted = await self._create_or_reuse_room_from_event(
                event,
                difficulty,
                game_type,
                turtle_soup_mode=turtle_soup_mode,
                requested_admin_room=self._admin_room_requested(
                    str(getattr(event, "message_str", "") or ""),
                    self._value_bool(kwargs.get("admin_room")),
                ),
                confirm_abandon=self._value_bool(kwargs.get("confirm_abandon")),
            )
            url = self._room_url(room)
            link_delivered = await self._deliver_room_link(
                room,
                url,
                reused=reused,
                restarted=restarted,
            )
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return self._json_error(str(exc))
        return json.dumps(
            {
                "ok": True,
                # room_id 仅为内部标识，供多房间会话下后续 control/status 工具引用；
                # 对用户没有意义，切勿在聊天里复述或解释它。
                "room_id": room.room_id,
                "room_url": "" if link_delivered else url,
                "link_delivered": link_delivered,
                "game_type": room.game_type,
                "difficulty": room.difficulty,
                "admin_room": room.admin_room,
                "turtle_soup_mode": room.turtle_soup_mode,
                "reused_room": reused,
                "restarted_game": restarted,
                "entry_timeout_seconds": self.manager.empty_player_timeout,
                "instruction": (
                    "房间链接已由插件作为独立纯文字消息发送；正常延续人格聊天，"
                    "不要复述、改写或重新生成链接，也不要提起内部房间编号 room_id。"
                    if link_delivered
                    else "已复用当前会话的原房间，不得关闭它或创建新房间；完整保留 room_url。"
                    if reused
                    else self._room_link_instruction(room)
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="game_companion_control_room")
    async def control_room_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """引导用户到 WebUI 完成游戏房间操作。

        QQ 只用于创建房间、取得入口和绑定身份。悔棋、暂停、继续、认输、再来一局、
        切换游戏和结束房间均不能在 QQ 中执行。

        Args:
            action(string): status、undo、pause、resume、resign、rematch、switch_game、close。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        _ = event, kwargs
        return self._json_error(
            "游戏内操作已移至 WebUI，请打开当前房间后在 Bot 对话栏中操作"
        )

    @filter.llm_tool(name="game_companion_turtle_soup")
    async def turtle_soup_tool(self, event: AstrMessageEvent, **kwargs: Any) -> str:
        """引导用户到 WebUI 继续海龟汤问答。

        Args:
            action(string): Bot 出题模式使用 ask、answer、hint；玩家出题模式使用 respond 或 correct。
            text(string): 问题、完整推理，或玩家给 Bot 的公开回答/线索。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        _ = event, kwargs
        return self._json_error("海龟汤问答已移至 WebUI，请在房间的 Bot 对话栏中继续")

    @filter.command("花火陪你玩")
    async def game_companion_status(self, event: AstrMessageEvent):
        """Return a small fallback status without taking over ordinary chat."""
        rooms = self.manager.for_session(event.unified_msg_origin)
        if not rooms:
            available = "、".join(
                self._game_label(game_type)
                for game_type in SUPPORTED_GAMES
                if self.manager.game_enabled(game_type)
            )
            yield event.plain_result(
                "当前会话没有活动游戏房间。"
                + (
                    f"直接告诉我想玩{available}即可。"
                    if available
                    else "管理员暂未开放任何游戏。"
                )
            )
            return
        labels = [
            f"{room.room_id}：{self._game_label(room.game_type)}，{room.status}"
            for room in rooms
        ]
        yield event.plain_result("当前游戏房间：\n" + "\n".join(labels))

    @filter.command_group("game")
    def game_commands(self):
        """花火陪你玩的显式 QQ 指令。"""
        pass

    @game_commands.command("游戏菜单", alias={"菜单", "menu"})
    async def game_menu(self, event: AstrMessageEvent):
        """列出游戏和全局房间容量。"""
        group_count = sum(
            room.source == "group" for room in self.manager.rooms.values()
        )
        private_count = sum(
            room.source == "private" for room in self.manager.rooms.values()
        )

        def capacity(current: int, limit: int, enabled: bool) -> str:
            maximum = "不限" if limit == 0 else str(limit)
            state = "允许创建" if enabled else "已关闭创建"
            return f"{current}/{maximum}（{state}）"

        descriptions = {
            "gomoku": "15×15 连成五子",
            "xiangqi": "使用 Pikafish 引擎",
            "tictactoe": "三连即可获胜",
            "turtle_soup": "通过是非提问还原汤底",
            "pig_dice": f"继续掷或收手，先到 {self.manager.pig_dice_target_score} 分获胜",
            "draw_guess": "用户在网页作画，Bot 通过视觉模型猜词",
            "blackjack": "玩家对 Bot 庄家比点数，可 1-6 人各自对庄",
            "undercover": "多人社交推理，按轮次发言描述→投票淘汰找卧底",
        }
        enabled = [
            game_type
            for game_type in SUPPORTED_GAMES
            if self.manager.game_enabled(game_type)
        ]
        disabled = [
            game_type
            for game_type in SUPPORTED_GAMES
            if not self.manager.game_enabled(game_type)
        ]
        game_lines = [
            f"{index}. {self._game_label(game_type)}：{descriptions[game_type]}"
            for index, game_type in enumerate(enabled, start=1)
        ] or ["当前没有已开放的游戏。"]
        lines = [
            "花火陪你玩 · 游戏菜单",
            "",
            *game_lines,
            *(
                ["", "管理员已关闭：" + "、".join(map(self._game_label, disabled))]
                if disabled
                else []
            ),
            "",
            "房间容量",
            f"群聊：{capacity(group_count, self.manager.max_group_rooms, self.group_rooms_enabled)}",
            f"私聊：{capacity(private_count, self.manager.max_private_rooms, self.private_rooms_enabled)}",
            "",
            "直接用自然语言告诉 Bot 想玩哪个已开放游戏即可。",
        ]
        yield event.plain_result("\n".join(lines))

    @game_commands.command("撤销网页绑定", alias={"撤销浏览器绑定", "撤销受信任浏览器"})
    async def revoke_trusted_browsers(self, event: AstrMessageEvent):
        """Revoke every persistent game-browser credential owned by the sender."""
        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            yield event.plain_result("无法识别当前 QQ，未撤销网页绑定。")
            return
        count = await self.trusted_identity_store.revoke_qq(qq)
        if count:
            yield event.plain_result(
                f"已撤销 {count} 个受信任浏览器。当前房间身份保持到房间结束，"
                "以后进入新房间需要重新绑定。"
            )
        else:
            yield event.plain_result("当前 QQ 没有有效的受信任浏览器绑定。")

    async def _bind_game_player_text(
        self, event: AstrMessageEvent, identity_token: str
    ) -> str:
        """Bind a browser visitor to the QQ sender and return a short reply."""
        try:
            _room, visitor = await self.manager.bind_visitor_identity(
                session_id=event.unified_msg_origin,
                identity_token=identity_token,
                qq=str(event.get_sender_id() or "").strip(),
                display_name=str(event.get_sender_name() or "").strip(),
            )
        except (ValueError, RuntimeError, PermissionError) as exc:
            return f"玩家身份绑定失败：{exc}"
        label = (
            f"{visitor.display_name}（{visitor.number}号）"
            if visitor.display_name
            else f"{visitor.number}号玩家"
        )
        return f"已将你绑定为本房间的 {label}。请回到网页点击“加入玩家席”。"

    @filter.command("绑定玩家", alias={"绑定令牌"})
    async def bind_game_player(self, event: AstrMessageEvent, identity_token: str):
        """Bind a browser visitor using the one-time token shown in its WebUI."""
        yield event.plain_result(
            await self._bind_game_player_text(event, identity_token)
        )

    @filter.regex(r"^[A-HJ-NP-Za-hj-np-z2-9]{8}$")
    async def bind_game_player_bare_token(self, event: AstrMessageEvent):
        """Also accept a bare token when the user explicitly addresses the Bot."""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        yield event.plain_result(
            await self._bind_game_player_text(event, event.message_str.strip())
        )

    @filter.on_llm_request(priority=-10)
    async def inject_game_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Inject read-only facts for the matching private single-player session."""
        if not getattr(self, "private_qq_game_context_enabled", False):
            return
        sender_getter = getattr(event, "get_sender_id", None)
        sender_qq = str(sender_getter() or "").strip() if callable(sender_getter) else ""
        if not sender_qq:
            return

        session_id = str(event.unified_msg_origin)
        lines: list[str] = []
        for room in self.manager.for_session(session_id):
            if self._private_context_user(room) != sender_qq:
                continue
            lines.append(self._private_qq_room_state(room))

        recent_results = getattr(self, "_recent_private_game_results", {})
        recent = recent_results.get(session_id)
        if recent is not None and recent.user_qq == sender_qq:
            if recent.expires_at and recent.expires_at <= time.time():
                recent_results.pop(session_id, None)
            else:
                lines.append(recent.summary)
        if not lines:
            return

        context_lines = [
            "<game_companion_private_context>",
            "以下是游戏插件为当前私聊用户提供的临时只读事实。仅在用户谈及当前或刚结束的游戏时使用；"
            "无关话题不要主动提起。不得据此声称已在 QQ 中落子、投降、悔棋、切换游戏或改变房间状态，"
            "所有游戏操作仍只能在 WebUI 完成。",
            *lines,
            "这里不包含 WebUI 对话记录，也不得猜测未列出的棋局细节或海龟汤隐藏内容。",
            "</game_companion_private_context>",
        ]
        req.system_prompt = (
            str(req.system_prompt or "") + "\n\n" + "\n".join(context_lines)
        ).strip()

    @staticmethod
    def _private_context_user(room: GameRoom) -> str:
        if room.source != "private" or not room.creator_qq:
            return ""
        if room.multiplayer.enabled:
            if len(room.multiplayer.seats) > 1:
                return ""
            if room.multiplayer.seats:
                seat = room.multiplayer.seats[0]
                if not seat.identity_confirmed or seat.qq != room.creator_qq:
                    return ""
        elif room.player_token and (
            not room.player_identity_confirmed or room.player_qq != room.creator_qq
        ):
            return ""
        return room.creator_qq

    @classmethod
    def _private_qq_room_state(cls, room: GameRoom) -> str:
        status = {
            "waiting": "等待用户绑定并进入玩家席",
            "setup": "等待开始",
            "active": "进行中",
            "finished": "本局已结束，房间仍开放",
            "rematch_pending": "等待 Bot 决定是否再来一局",
            "paused": "已暂停",
            "closed": "已关闭",
        }.get(room.status, room.status)
        base = (
            f"当前私聊房间：游戏={cls._game_label(room.game_type)}，状态={status}；"
            f"该游戏在本房间累计{cls._private_score_text(room)}。"
        )
        game = room.game
        if game is None:
            return base
        if isinstance(game, GomokuGame):
            human = "黑" if game.human_color == GOMOKU_BLACK else "白"
            bot = "黑" if game.bot_color == GOMOKU_BLACK else "白"
            turn = "玩家" if game.turn == game.human_color else "Bot"
            detail = (
                f"玩家执{human}，Bot 执{bot}，黑方固定先手；已落子 {len(game.history)} 手"
                + ("。" if game.finished else f"，当前轮到{turn}。")
            )
        elif isinstance(game, TicTacToeGame):
            human = "X" if game.human_mark == TICTACTOE_X else "O"
            bot = "X" if game.bot_mark == TICTACTOE_X else "O"
            turn = "玩家" if game.turn == game.human_mark else "Bot"
            detail = (
                f"玩家执 {human}，Bot 执 {bot}，X 固定先手；已落子 {len(game.history)} 手"
                + ("。" if game.finished else f"，当前轮到{turn}。")
            )
        elif isinstance(game, XiangqiGame):
            human = "红" if game.human_side == XIANGQI_RED else "黑"
            bot = "红" if game.bot_side == XIANGQI_RED else "黑"
            turn = "玩家" if game.turn == game.human_side else "Bot"
            detail = (
                f"玩家执{human}，Bot 执{bot}，红方固定先手；已走 {len(game.moves)} 手"
                + ("。" if game.finished else f"，当前轮到{turn}。")
            )
        elif isinstance(game, PigDiceGame):
            turn = "玩家" if game.turn == "human" else "Bot"
            detail = (
                f"玩家 {game.human_score} 分，Bot {game.bot_score} 分，目标 {game.target_score} 分；"
                f"当前轮到{turn}，本回合暂存 {game.turn_total} 分。"
            )
        elif isinstance(game, DrawGuessGame):
            state = "已猜中" if game.solved else "已结束" if game.finished else "作画中"
            detail = (
                f"合作玩法，状态={state}；Bot 已猜 {len(game.guesses)}/{game.max_guesses} 次。"
            )
        elif isinstance(game, TurtleSoupGame):
            mode = "Bot 出题、玩家猜" if game.mode == "bot_host" else "玩家出题、Bot 猜"
            detail = (
                f"玩法={mode}；公开回合 {game.turn_count} 次，提问 {game.question_count} 次，"
                f"完整猜测 {game.answer_attempts} 次，已使用提示 {game.hints_used} 次。"
            )
        else:
            detail = ""
        return base + detail

    @staticmethod
    def _private_score_text(room: GameRoom) -> str:
        score = room.current_score
        if room.game_type == "draw_guess":
            return (
                f"合作成功 {score.human_wins}、未完成 {score.bot_wins}、"
                f"完成 {score.completed} 轮"
            )
        if room.game_type == "turtle_soup":
            return (
                f"玩家侧计分 {score.human_wins}、Bot 侧计分 {score.bot_wins}、"
                f"完成 {score.completed} 题"
            )
        return (
            f"玩家胜 {score.human_wins}、Bot 胜 {score.bot_wins}、"
            f"平局 {score.draws}、完成 {score.completed} 局"
        )

    @staticmethod
    def _live_game_state(room: GameRoom) -> list[str]:
        game = room.game
        score = room.current_score
        lines = [
            f"本房间累计：玩家胜 {score.human_wins}，Bot 胜 {score.bot_wins}，"
            f"平局 {score.draws}，已完成 {score.completed}。"
        ]
        if game is None:
            lines.append("当前尚未开始具体一局。")
            return lines

        if isinstance(game, PigDiceGame):
            if game.human_score == game.bot_score:
                advantage = "双方已存总分相同"
            elif game.human_score > game.bot_score:
                advantage = f"玩家已存总分领先 {game.human_score - game.bot_score} 分"
            else:
                advantage = f"Bot 已存总分领先 {game.bot_score - game.human_score} 分"
            turn = "玩家" if game.turn == "human" else "Bot"
            lines.append(
                f"实时状态：玩家已存 {game.human_score} 分，Bot 已存 {game.bot_score} 分，"
                f"{advantage}；当前轮到{turn}，本回合暂存 {game.turn_total} 分，"
                f"最近点数={game.last_roll or '无'}，目标 {game.target_score} 分。"
            )
            return lines

        if isinstance(game, DrawGuessGame):
            state = (
                "已经猜中"
                if game.solved
                else "本轮已经结束"
                if game.finished
                else "正在看图"
                if game.processing
                else "等待玩家继续作画"
            )
            recent = "、".join(item["guess"] for item in game.guesses[-3:]) or "暂无"
            lines.append(
                f"实时进度：{state}，Bot 已猜 {len(game.guesses)}/{game.max_guesses} 次，"
                f"最近猜测：{recent}。这是合作玩法，不按双方对抗优劣描述。"
            )
            return lines

        if isinstance(game, BlackjackGame):
            upcard = game.dealer_upcard
            state_text = (
                "本局已经结束"
                if game.finished
                else "庄家正在补牌"
                if game.phase == "dealer_turn"
                else "玩家轮流要牌或停牌"
            )
            hands = "、".join(
                f"{number}号{hand.value}点"
                + ("（21点）" if hand.blackjack else "")
                for number, hand in sorted(game.hands.items())
            ) or "暂无"
            lines.append(
                f"实时局面：Bot 是庄家，明牌{upcard.rank + upcard.suit if upcard else '未发'}，"
                f"暗牌未公开；{state_text}。各家点数：{hands}。"
                "庄家只按固定规则补牌，不能主观作弊。"
            )
            return lines

        if isinstance(game, UndercoverGame):
            counts = game.camp_counts()
            live = game.camp_counts(live_only=True)
            camp_line = "阵营分布：平民{}/{}、卧底{}/{}、白板{}/{}".format(
                live.get("civilian", 0), counts.get("civilian", 0),
                live.get("undercover", 0), counts.get("undercover", 0),
                live.get("whiteboard", 0), counts.get("whiteboard", 0),
            )
            phase_desc = {
                "preparing": "正在发词准备",
                "speech": "发言轮次",
                "pk": "平票 PK 发言轮",
                "voting": "投票阶段",
                "finished": "本局已结束",
            }.get(str(game.phase), "进行中")
            round_desc = (
                f"第 {game.current_round_number} 轮"
                if game.current_round_number
                else "准备期"
            )
            expected = game.expected_speaker_number
            lines.append(
                f"实时状态：谁是卧底 {round_desc}，阶段={phase_desc}。"
                + camp_line
                + "。"
                + (
                    f"当前轮到 {expected} 号玩家发言。"
                    if expected is not None and phase_desc != "本局已结束"
                    else "存活玩家正在投票淘汰可疑目标。"
                    if game.phase == "voting"
                    else ""
                )
            )
            lines.append(
                "注：进行中不能公布玩家身份，结束后会在 WebUI 一次性揭晓全员身份和词条。"
            )
            return lines

        if isinstance(game, TicTacToeGame):
            marks = {0: ".", TICTACTOE_X: "X", TICTACTOE_NOUGHT: "O"}
            board = "/".join("".join(marks[cell] for cell in row) for row in game.board)
            bot_mark = "X" if game.bot_mark == TICTACTOE_X else "O"
            human_mark = "X" if game.human_mark == TICTACTOE_X else "O"
            turn = "X" if game.turn == TICTACTOE_X else "O"
            lines.append(
                f"实时棋盘={board}；玩家执 {human_mark}，Bot 执 {bot_mark}，当前轮到 {turn}。"
            )
            return lines

        if isinstance(game, GomokuGame):
            human_stones = sum(
                cell == game.human_color for row in game.board for cell in row
            )
            bot_stones = sum(
                cell == game.bot_color for row in game.board for cell in row
            )
            turn = "玩家" if game.turn == game.human_color else "Bot"
            facts = [
                f"实时局面：玩家棋子 {human_stones}，Bot 棋子 {bot_stones}，当前轮到{turn}"
            ]
            human_tactical = game.tactical_state(game.human_color)
            bot_tactical = game.tactical_state(game.bot_color)
            tactical_labels = {
                "four": "存在四子威胁",
                "three": "存在三子潜力",
                "win": "已经获胜",
            }
            if human_tactical:
                facts.append(
                    f"玩家{tactical_labels.get(human_tactical, human_tactical)}"
                )
            if bot_tactical:
                facts.append(f"Bot {tactical_labels.get(bot_tactical, bot_tactical)}")
            lines.append(
                "；".join(facts) + "。局势只按已知威胁描述，不要仅凭棋子数判断优劣。"
            )
            return lines

        if isinstance(game, XiangqiGame):
            values = {"a": 2, "b": 2, "n": 4, "r": 9, "c": 4, "p": 1, "k": 0}
            red_material = sum(
                values.get(piece.lower(), 0)
                for row in game.board()
                for piece in row
                if piece != "." and piece.isupper()
            )
            black_material = sum(
                values.get(piece.lower(), 0)
                for row in game.board()
                for piece in row
                if piece != "." and piece.islower()
            )
            human_material = (
                red_material if game.human_side == XIANGQI_RED else black_material
            )
            bot_material = (
                black_material if game.bot_side == XIANGQI_BLACK else red_material
            )
            difference = bot_material - human_material
            material = (
                "材料大致相当"
                if abs(difference) <= 1
                else f"Bot 材料领先 {difference}"
                if difference > 0
                else f"玩家材料领先 {-difference}"
            )
            turn = "玩家" if game.turn == game.human_side else "Bot"
            lines.append(
                f"实时局面：当前轮到{turn}，已走 {len(game.moves)} 手，{material}。"
                "材料只是局部参考，不等同于引擎胜率。"
            )
            return lines

        if isinstance(game, TurtleSoupGame):
            if game.mode == "player_host":
                snapshot = room.public_snapshot()
                current_number = snapshot.get("current_player_number")
                current_name = snapshot.get("current_player_name")
                current_label = (
                    f"{current_name}（{current_number}号）"
                    if current_name and current_number
                    else f"{current_number}号" if current_number else "未知"
                )
                recent = [
                    f"玩家线索/回答：{entry.prompt}；Bot {('猜测' if entry.bot_action == 'guess' else '提问')}：{entry.response}"
                    for entry in game.entries[-2:]
                    if entry.kind == "reverse"
                ]
                lines.append(
                    f"实时进度：玩家出题、Bot 猜，公开回合 {game.turn_count} 次，"
                    f"Bot 提问 {game.question_count} 次、猜测 {game.answer_attempts} 次，"
                    f"当前轮到 {current_label} 玩家。Bot 不知道未公开汤底。"
                )
                lines.extend(recent)
                return lines
            puzzle = game.puzzle
            title = puzzle.title if puzzle else "出题中"
            snapshot = room.public_snapshot()
            current_number = snapshot.get("current_player_number")
            current_name = snapshot.get("current_player_name")
            current_label = (
                f"{current_name}（{current_number}号）"
                if current_name and current_number
                else f"{current_number}号" if current_number else "未知"
            )
            lines.append(
                f"实时进度：题目《{title}》，提问 {game.question_count} 次，"
                f"提示 {game.hints_used} 次，发现公开关键进度 {len(game.discovered_facts)}/"
                f"{len(puzzle.key_facts) if puzzle else 0}，当前轮到 {current_label}。"
                "不得推测或泄露隐藏汤底。"
            )
        return lines

    async def _create_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
        turtle_soup_mode: TurtleSoupMode = "bot_host",
        *,
        requested_admin_room: bool = False,
    ) -> GameRoom:
        if not self.server_enabled:
            raise RuntimeError("游戏房间服务已在插件配置中关闭")
        group_id = str(event.get_group_id() or "").strip()
        source = "group" if group_id else "private"
        creator_qq = str(event.get_sender_id() or "").strip()
        is_game_admin = creator_qq in self.game_admin_ids
        admin_room = False
        if source == "group":
            if not self.group_rooms_enabled:
                raise PermissionError("群聊创建游戏房间已关闭")
            if (
                not self.allow_non_admin_group_creation
                and not is_game_admin
            ):
                raise PermissionError("当前只允许插件配置中的游戏管理员创建群聊房间")
            if not self.allow_non_admin_group_creation:
                admin_room = True
            elif requested_admin_room:
                if not is_game_admin:
                    raise PermissionError("只有游戏管理员可以创建管理员房间")
                admin_room = True
        elif not self.private_rooms_enabled:
            raise PermissionError("私聊创建游戏房间已关闭")
        elif requested_admin_room:
            raise ValueError("管理员房间仅支持群聊创建")
        await self._ensure_public_access()
        room = await self.manager.create_room(
            source=source,
            session_id=event.unified_msg_origin,
            platform=str(event.get_platform_id() or ""),
            group_id=group_id,
            creator_qq=creator_qq,
            creator_name=str(event.get_sender_name() or "").strip(),
            admin_room=admin_room,
            game_type=game_type,
            difficulty=difficulty,
            turtle_soup_mode=turtle_soup_mode,
        )
        return room

    async def _create_or_reuse_room_from_event(
        self,
        event: AstrMessageEvent,
        difficulty: Difficulty,
        game_type: GameType,
        *,
        turtle_soup_mode: TurtleSoupMode = "bot_host",
        requested_admin_room: bool = False,
        confirm_abandon: bool,
    ) -> tuple[GameRoom, bool, bool]:
        rooms = self.manager.for_session(event.unified_msg_origin)
        if len(rooms) > 1:
            raise ValueError("当前会话已有多个活动房间，请先说明要使用的房间编号")
        if not rooms:
            if game_type == "xiangqi":
                await self.xiangqi_engine.ensure_ready()
            room = await self._create_room_from_event(
                event,
                difficulty,
                game_type,
                turtle_soup_mode,
                requested_admin_room=requested_admin_room,
            )
            return room, False, False
        room = rooms[0]
        _ = (
            difficulty,
            game_type,
            turtle_soup_mode,
            requested_admin_room,
            confirm_abandon,
        )
        await self._ensure_public_access()
        return room, True, False

    async def _ensure_public_access(self) -> None:
        if not self.room_server.running:
            await self.room_server.start()
            if self.room_server.port != self.room_server.requested_port:
                logger.warning(
                    "[GameCompanion] 端口 %s 被占用，房间服务改用 %s",
                    self.room_server.requested_port,
                    self.room_server.port,
                )
        if self._configured_access_base():
            return
        local_access = self._local_access_base()
        if local_access:
            logger.info(
                "[GameCompanion] 使用局域网访问地址，不启动 Quick Tunnel: %s",
                local_access,
            )
            return
        fallback = self._local_access_base(allow_unresolved=True)
        if fallback and not self.auto_quick_tunnel:
            logger.warning(
                "[GameCompanion] 无法自动确认局域网地址，将使用监听地址 %s；"
                "建议配置 server.access_host",
                fallback,
            )
            return
        if not self.auto_quick_tunnel:
            await self.room_server.stop()
            raise RuntimeError("未配置外部访问地址，并且临时公网访问已关闭")
        self.quick_tunnel.local_url = self.room_server.local_base_url
        try:
            await self.quick_tunnel.start(timeout=40)
        except Exception:
            if not self.manager.rooms:
                await self.room_server.stop()
            raise

    def _room_url(self, room: GameRoom) -> str:
        base = self._configured_access_base() or (
            self.quick_tunnel.url if bool(getattr(self.quick_tunnel, "ready", False)) else ""
        ) or self._local_access_base(allow_unresolved=True)
        if not base:
            raise RuntimeError("外部访问地址尚未就绪")
        return f"{base.rstrip('/')}/room/{quote(room.access_token, safe='')}"

    def _configured_access_base(self) -> str:
        return self.public_base_url or getattr(self, "external_base_url", "")

    def _local_access_base(self, *, allow_unresolved: bool = False) -> str:
        """Return a browser-reachable LAN URL when the server is not loopback-only."""
        host = str(getattr(self, "server_host", "127.0.0.1") or "127.0.0.1").strip()
        normalized = host.lower()
        if normalized in {"127.0.0.1", "localhost", "::1"}:
            return ""
        access_host = str(getattr(self, "access_host", "") or "").strip()
        if normalized in {"0.0.0.0", "::", "[::]"}:
            access_host = access_host or self._detect_access_host()
        else:
            access_host = access_host or host
        if not access_host or (
            not allow_unresolved
            and access_host.lower() in {
                "0.0.0.0",
                "::",
                "[::]",
                "127.0.0.1",
                "localhost",
                "::1",
            }
        ):
            return ""
        if ":" in access_host and not access_host.startswith("["):
            access_host = f"[{access_host}]"
        port = int(
            getattr(
                self.room_server,
                "port",
                getattr(self, "server_port", 6331),
            )
            or 6331
        )
        return f"http://{access_host}:{port}"

    def _detect_access_host(self) -> str:
        configured = getattr(self, "access_host", "")
        if configured:
            return configured
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            host = str(probe.getsockname()[0])
            probe.close()
            if host:
                return host
        except OSError:
            pass
        if str(self.server_host).strip().lower() in {"0.0.0.0", "::", "[::]"}:
            logger.warning(
                "[GameCompanion] 未能自动探测局域网地址，将使用监听地址 %s；建议配置 server.access_host",
                self.server_host,
            )
            return self.server_host
        return "127.0.0.1"

    def _resolve_event_room(self, event: AstrMessageEvent, room_id: str) -> GameRoom:
        actor = str(event.get_sender_id() or "")
        if room_id:
            room = self.manager.rooms.get(room_id)
            if room is None:
                raise ValueError("找不到指定房间")
            if (
                room.session_id != event.unified_msg_origin
                and actor not in self.game_admin_ids
            ):
                raise PermissionError("该房间不属于当前 QQ 会话")
            return room
        rooms = self.manager.for_session(event.unified_msg_origin)
        if len(rooms) != 1:
            raise ValueError("当前会话没有唯一活动房间，请说明房间编号")
        return rooms[0]

    async def _on_room_event(
        self, event_name: str, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        game_label = self._game_label(room.game_type)
        if event_name == "soup_generation_requested":
            if isinstance(room.game, TurtleSoupGame):
                self._spawn(self._prepare_turtle_soup(room, room.game))
            return
        if event_name == "undercover_word_pair_requested":
            # 尝试用 LLM 生成一对词条，经去重窗口检查后持久化；失败留给 room_manager 本地 fallback
            pair = await self._llm_generate_undercover_words(room)
            if pair is not None:
                payload["word_pair"] = pair
            return
        if event_name == "undercover_game_started":
            send_card = (
                room.undercover_reveal_identity
                if room.undercover_reveal_identity is not None
                else self.undercover_send_identity_in_card
            )
            if send_card and isinstance(room.game, UndercoverGame):
                self._spawn(self._deliver_undercover_identities(room, room.game))
            if isinstance(room.game, UndercoverGame):
                # 开局后轮到第一位玩家发言，如果第一位是 AI，立即驱动
                self._spawn(self._undercover_ai_step_if_needed(room))
            return
        if event_name == "undercover_speech_submitted":
            self._spawn(self._undercover_commentary_speech(room, payload))
            self._spawn(self._undercover_ai_step_if_needed(room))
            return
        if event_name == "undercover_vote_submitted":
            self._spawn(self._undercover_commentary_vote(room, payload))
            self._spawn(self._undercover_ai_step_if_needed(room))
            return
        if event_name == "undercover_pk_started":
            return
        if event_name == "undercover_turn_timeout":
            self._spawn(self._undercover_turn_timeout(room))
            return
        if event_name == "game_started":
            self._capture_round_participants(room, reset=True)
            if room.player_identity_confirmed:
                self._notify_companion_activity(room, "updated")
            opening = self._opening_commentary_prompt(room)
            self._spawn(
                self._comment(
                    room,
                    opening,
                )
            )
            return
        if event_name == "player_confirmed":
            self._capture_round_participants(room)
            self._notify_companion_activity(room, "started")
            return
        if event_name == "seats_changed":
            self._capture_round_participants(room)
            return
        if event_name == "board_changed" and room.game is not None:
            if room.game_type == "gomoku":
                tactical_prompt = self._gomoku_commentary_prompt(room, payload)
            elif room.game_type == "tictactoe":
                side = (
                    room.game.human_mark
                    if payload.get("actor") == "human"
                    else room.game.bot_mark
                )
                tactical = room.game.tactical_state(side)
                tactical_prompt = {
                    "fork": "井字棋盘面刚出现了双重威胁",
                }.get(tactical)
            else:
                side = None
                tactical = room.game.tactical_state(side)
                tactical_prompt = {
                    "major_capture": "棋盘上刚发生了一次重要吃子",
                }.get(tactical)
            if tactical_prompt and (
                time.time() - room.last_commentary_at >= self.commentary_cooldown
            ):
                room.last_commentary_at = time.time()
                self._spawn(
                    self._comment(
                        room,
                        tactical_prompt,
                    )
                )
            return
        if event_name == "soup_question_answered":
            if int(payload.get("new_facts") or 0) > 0:
                self._spawn(
                    self._comment(
                        room,
                        "玩家刚通过提问触及了海龟汤的关键事实。请用当前人格简短回应，"
                        "不要透露汤底或任何尚未公开的线索。",
                    )
                )
            return
        if event_name == "soup_answer_attempted":
            if int(payload.get("new_facts") or 0) > 0:
                self._spawn(
                    self._comment(
                        room,
                        "玩家提交的海龟汤推理已经接近答案但仍不完整。请简短鼓励，"
                        "不要指出缺少的事实。",
                    )
                )
            return
        if event_name == "soup_hint_revealed":
            if payload.get("source") == "web":
                hint = str(payload.get("hint") or "")
                visitor = room.visitors.get(str(payload.get("visitor_token") or ""))
                self._spawn(
                    self._announce_turtle_soup_hint(room, hint, visitor=visitor)
                )
            return
        if event_name == "dice_changed":
            action = str(payload.get("action") or "")
            actor = "玩家" if payload.get("actor") == "human" else "Bot"
            lost = int(payload.get("lost") or 0)
            banked = int(payload.get("banked") or 0)
            rolls = int(payload.get("turn_rolls") or 0)
            key_event = ""
            if action == "bust" and lost >= 10:
                key_event = f"{actor}掷出 1，本回合损失了 {lost} 分"
            elif action == "roll" and rolls == 4:
                key_event = f"{actor}已经连续成功掷了四次，仍在冒险"
            elif action in {"hold", "win"} and banked >= 15:
                key_event = f"{actor}一次存下了 {banked} 分"
            if key_event and (
                time.time() - room.last_commentary_at >= self.commentary_cooldown
            ):
                room.last_commentary_at = time.time()
                self._spawn(
                    self._comment(
                        room,
                        f"贪心骰子刚发生关键节点：{key_event}。请结合当前人格简短自然地回应。",
                    )
                )
            return
        if event_name in {"drawing_changed", "draw_guess_completed"}:
            return
        if event_name == "game_finished":
            self._queue_companion_round_event(room, payload)
            self._remember_private_game_result(room, payload)
            result = self._round_result_text(room, payload, reveal_answer=True)
            self._spawn(
                self._comment(
                    room,
                    f"{game_label}本局结果是：{result}。请用当前人格简短回应。",
                )
            )
            return
        if event_name == "rematch_requested":
            visitor = room.visitors.get(str(payload.get("visitor_token") or ""))
            pending = self._companion_round_event_tasks.get(room.room_id)
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=8)
                except TimeoutError:
                    pass
            await self._report_companion_game_event(
                room,
                "rematch_requested",
                payload,
                visitors=[visitor] if visitor is not None else [],
            )
            self._spawn(self._decide_rematch(room, visitor=visitor))
            return
        if event_name == "game_switched":
            self._notify_companion_activity(room, "updated")
            return
        if event_name == "room_destroyed":
            self._notify_companion_activity(room, "ended")
            self._finalize_private_game_result(room)
            await self._record_room_memory(room)

    @classmethod
    def _opening_commentary_prompt(cls, room: GameRoom) -> str:
        game = room.game
        if isinstance(game, GomokuGame):
            human = "黑" if game.human_color == GOMOKU_BLACK else "白"
            bot = "黑" if game.bot_color == GOMOKU_BLACK else "白"
            first = "玩家" if game.human_color == GOMOKU_BLACK else "Bot"
            facts = (
                f"玩家执{human}，Bot 执{bot}；五子棋固定由黑方先手，因此本局由{first}先行。"
            )
        elif isinstance(game, XiangqiGame):
            human = "红" if game.human_side == XIANGQI_RED else "黑"
            bot = "红" if game.bot_side == XIANGQI_RED else "黑"
            first = "玩家" if game.human_side == XIANGQI_RED else "Bot"
            facts = (
                f"玩家执{human}，Bot 执{bot}；中国象棋固定由红方先手，因此本局由{first}先行。"
            )
        elif isinstance(game, TicTacToeGame):
            human = "X" if game.human_mark == TICTACTOE_X else "O"
            bot = "X" if game.bot_mark == TICTACTOE_X else "O"
            first = "玩家" if game.human_mark == TICTACTOE_X else "Bot"
            facts = f"玩家执 {human}，Bot 执 {bot}；井字棋固定由 X 先手，因此本局由{first}先行。"
        elif isinstance(game, PigDiceGame):
            first = "玩家" if game.turn == "human" else "Bot"
            facts = f"本局随机先手结果已经确定，由{first}先掷，目标是先得到 {game.target_score} 分。"
        elif isinstance(game, DrawGuessGame):
            facts = (
                f"这是合作玩法：用户始终作画，Bot 始终猜图，Bot 不参与绘画；限时 {game.duration_seconds} 秒，"
                f"Bot 最多猜 {game.max_guesses} 次。"
            )
        elif isinstance(game, BlackjackGame):
            facts = (
                f"本局 Bot 是庄家，{len(game.hands)} 位闲家各持一手牌；"
                "闲家先决定要牌或停牌，全部完成后庄家才翻开暗牌并按规则补牌。"
            )
        elif isinstance(game, TurtleSoupGame):
            facts = (
                "新题已经准备完成，由 Bot 出题、玩家提问。"
                if game.mode == "bot_host"
                else "当前由玩家提供公开线索，Bot 负责提问和猜测。"
            )
        else:
            facts = f"新的一局{cls._game_label(room.game_type)}已经开始。"
        return (
            f"{cls._game_label(room.game_type)}开局事实：{facts}"
            "请严格依据这些事实，用当前人格简短自然地说一句开场话；"
            "不得说反双方身份、颜色、标记或先后手，也不要复述完整规则。"
        )

    @staticmethod
    def _round_result_text(
        room: GameRoom, payload: dict[str, Any], *, reveal_answer: bool
    ) -> str:
        game = room.game
        if isinstance(game, TurtleSoupGame):
            if game.mode == "player_host":
                return "Bot 成功猜中玩家的汤底" if game.bot_solved else "玩家结束了出题"
            return "玩家成功解开汤底" if game.solved else "玩家放弃，汤底已揭晓"
        if isinstance(game, DrawGuessGame):
            if game.solved:
                return (
                    f"用户负责作画，Bot 在第 {len(game.guesses)} 次猜中了“{game.answer}”"
                    if reveal_answer
                    else f"用户负责作画，Bot 在第 {len(game.guesses)} 次成功猜中"
                )
            return (
                f"用户负责作画，Bot 本轮未能猜中，答案是“{game.answer}”"
                if reveal_answer
                else "用户负责作画，Bot 本轮未能猜中"
            )
        return {
            "human_win": "玩家获胜",
            "bot_win": "Bot 获胜",
            "draw": "平局",
            "mixed": "本局多名玩家各有胜负",
            "cooperative_success": "合作成功",
            "cooperative_unsolved": "合作未完成",
            "uc_civilian_win": "平民阵营获得最终胜利",
            "uc_undercover_win": "卧底阵营坚持到最后并获得胜利",
            "uc_whiteboard_win": "白板在没有卧底的情况下撑到最后",
        }.get(str(payload.get("result")), "对局结束")

    @classmethod
    def _private_result_summary(
        cls, room: GameRoom, payload: dict[str, Any]
    ) -> str:
        game = room.game
        side = ""
        if isinstance(game, GomokuGame):
            human = "黑" if game.human_color == GOMOKU_BLACK else "白"
            bot = "黑" if game.bot_color == GOMOKU_BLACK else "白"
            side = f"本局玩家执{human}、Bot 执{bot}；"
        elif isinstance(game, XiangqiGame):
            human = "红" if game.human_side == XIANGQI_RED else "黑"
            bot = "红" if game.bot_side == XIANGQI_RED else "黑"
            side = f"本局玩家执{human}、Bot 执{bot}；"
        elif isinstance(game, TicTacToeGame):
            human = "X" if game.human_mark == TICTACTOE_X else "O"
            bot = "X" if game.bot_mark == TICTACTOE_X else "O"
            side = f"本局玩家执 {human}、Bot 执 {bot}；"
        elif isinstance(game, BlackjackGame):
            side = f"本局 Bot 担任庄家、{len(game.hands)} 位闲家各自对庄；"
        return (
            f"最近一局结果：{cls._game_label(room.game_type)}，"
            f"{cls._round_result_text(room, payload, reveal_answer=False)}；{side}"
            f"该游戏在此房间累计{cls._private_score_text(room)}。"
        )

    def _remember_private_game_result(
        self, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        if not getattr(self, "private_qq_game_context_enabled", False):
            return
        user_qq = self._private_context_user(room)
        if not user_qq:
            return
        self._recent_private_game_results[room.session_id] = _RecentPrivateGameResult(
            room_id=room.room_id,
            user_qq=user_qq,
            summary=self._private_result_summary(room, payload),
        )

    def _finalize_private_game_result(self, room: GameRoom) -> None:
        recent = getattr(self, "_recent_private_game_results", {}).get(room.session_id)
        if recent is None or recent.room_id != room.room_id:
            return
        ttl = getattr(self, "recent_game_result_ttl_seconds", 0)
        if ttl <= 0:
            self._recent_private_game_results.pop(room.session_id, None)
        else:
            recent.expires_at = time.time() + ttl

    async def submit_room_chat(
        self, room: GameRoom, text: str, *, visitor_token: str
    ) -> dict[str, Any]:
        """Handle one isolated WebUI conversation turn without sending to QQ."""
        async with room.chat_lock:
            visitor, cleaned, is_player, is_current_player = (
                await self.manager.begin_room_chat(room, visitor_token, text)
            )
            # 谁是卧底：当前发言玩家的聊天消息自动记作本轮发言（优先级高于控制命令）
            if (
                room.game_type == "undercover"
                and isinstance(room.game, UndercoverGame)
                and room.game.phase in {"speech", "pk"}
                and is_player
                and visitor.number == room.game.expected_speaker_number
            ):
                try:
                    await self.manager.player_undercover_speech(
                        room, visitor.token, cleaned
                    )
                    reply = "已将此条聊天作为你本轮的发言记录。"
                    await self.manager.add_room_chat_reply(
                        room,
                        visitor,
                        reply,
                        message_type="control",
                    )
                    return {
                        "action": "undercover_speech",
                        "reply": reply,
                    }
                except (ValueError, RuntimeError, PermissionError) as exc:
                    # 失败时走普通聊天回应对话
                    pass
            action, options = self._room_chat_action(room, cleaned)
            if action in {"soup_question", "soup_answer", "soup_respond"}:
                action = await self._refine_turtle_chat_action(
                    room, cleaned, proposed_action=action
                )
            if action and not is_player:
                reply = "你现在在观众席，不能执行游戏指令；可以继续在这里和我聊天。"
                await self.manager.add_room_chat_reply(
                    room, visitor, reply, message_type="permission"
                )
                return {"action": "denied", "reply": reply}

            try:
                if action == "close":
                    reply = "好，这个房间就到这里。"
                    await self.manager.add_room_chat_reply(
                        room, visitor, reply, message_type="control"
                    )
                    await self.manager.destroy(room.room_id, "玩家通过 WebUI 结束了房间")
                    return {"action": action, "reply": reply}
                if action == "switch_game":
                    target = options["game_type"]
                    switched = await self.manager.switch_game(room, target, force=True)
                    soup_mode = options.get("turtle_soup_mode")
                    if target == "turtle_soup" and soup_mode:
                        await self.manager.switch_turtle_soup_mode(
                            room, soup_mode, force=True
                        )
                    reply = (
                        f"已经切换到{self._game_label(target)}，房间和玩家席都保留着。"
                        if switched
                        else f"现在玩的已经是{self._game_label(target)}。"
                    )
                elif action == "switch_soup_mode":
                    mode = options["turtle_soup_mode"]
                    switched = await self.manager.switch_turtle_soup_mode(
                        room, mode, force=True
                    )
                    label = "我出题、玩家猜" if mode == "bot_host" else "玩家出题、我来猜"
                    reply = f"海龟汤已切换为{label}。" if switched else f"现在已经是{label}。"
                elif action == "rematch":
                    await self.manager.request_rematch(
                        room,
                        visitor.token,
                        record_message=False,
                        request_text=cleaned,
                    )
                    return {"action": action, "reply": ""}
                elif action == "undo":
                    accepted, reply = await self._decide_ui_undo(room, visitor)
                    if accepted:
                        await self.manager.undo(room)
                elif action == "pause":
                    await self.manager.pause(room)
                    reply = "先暂停一下，我会保留当前进度。"
                elif action == "resume":
                    await self.manager.resume(room)
                    reply = "继续吧，当前进度没有变化。"
                elif action == "resign":
                    await self.manager.resign(room, visitor_token=visitor.token)
                    reply = (
                        "好，这一题就先揭晓到这里。"
                        if room.game_type == "turtle_soup"
                        else "收到，这一手记为输。"
                        if room.game_type == "blackjack"
                        else "收到，本局按你认输结束。"
                    )
                elif action == "soup_hint":
                    await self.manager.request_turtle_soup_hint(
                        room, source="web", visitor_token=visitor.token
                    )
                    return {"action": action, "reply": ""}
                elif action == "soup_correct":
                    await self.manager.confirm_reverse_turtle_soup_guess(
                        room, source="web", visitor_token=visitor.token
                    )
                    reply = "明白，这次猜测确认正确。"
                elif action == "soup_answer":
                    result = await self.submit_turtle_soup_answer(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "我已经看过这份推理。")
                elif action == "soup_question":
                    result = await self.submit_turtle_soup_question(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "无关")
                elif action == "soup_respond":
                    result = await self.submit_reverse_turtle_soup_turn(
                        room, cleaned, source="web", visitor_token=visitor.token
                    )
                    reply = str(result.get("reply") or "")
                    room.record_chat_memory(visitor, "bot", reply)
                    return {"action": action, "reply": reply}
                else:
                    reply = await self._generate_room_chat_reply(
                        room,
                        visitor,
                        cleaned,
                        is_player=is_player,
                        is_current_player=is_current_player,
                    )
            except (ValueError, RuntimeError, PermissionError, OSError) as exc:
                reply = str(exc)
                message_type = "permission" if isinstance(exc, PermissionError) else "error"
                await self.manager.add_room_chat_reply(
                    room, visitor, reply, message_type=message_type
                )
                return {"action": action or "chat", "reply": reply}

            if not reply:
                reply = "我在，继续说吧。"
            await self.manager.add_room_chat_reply(
                room,
                visitor,
                reply,
                message_type="control" if action else "chat",
            )
            return {"action": action or "chat", "reply": reply}

    async def submit_draw_guess(
        self, room: GameRoom, *, visitor_token: str, image_data_url: str
    ) -> dict[str, Any]:
        """Send one bounded canvas image to a visual provider for a single guess."""
        safe_image = self._validated_drawing_image(image_data_url)
        game = await self.manager.begin_draw_guess(room, visitor_token)
        try:
            guess = await self._guess_drawing(room, game, safe_image)
            item = await self.manager.complete_draw_guess(room, visitor_token, guess)
        except Exception:
            await self.manager.abort_draw_guess(room)
            raise
        return {
            "guess": item["guess"],
            "correct": item["correct"],
            "number": item["number"],
        }

    async def _guess_drawing(
        self, room: GameRoom, game: DrawGuessGame, image_data_url: str
    ) -> str:
        provider = None
        if self.draw_guess_vision_provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                provider = getter(self.draw_guess_vision_provider_id)
            if provider is None:
                raise RuntimeError("你画我猜配置的视觉模型 Provider 不存在")
        else:
            provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError("当前会话没有可用的视觉模型")
        previous = "、".join(item["guess"] for item in game.guesses) or "暂无"
        prompt = (
            "请观察这张用户在白色画布上的简笔画，猜一个最可能的中文词语。"
            "只回答一个答案，不解释，不列举候选，不复述任务。"
            f"此前已经猜过且不正确的答案：{previous}。不要重复这些答案。"
        )
        system_prompt = (
            "你正在玩你画我猜。隐藏答案绝不会提供给你，必须只根据图片判断。"
            "输出一个简短中文名词或成语；不要使用斜杠、顿号或逗号列出多个答案。"
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_urls=[image_data_url],
                ),
                timeout=45,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("视觉模型看图超时，请稍后再试") from exc
        except Exception as exc:
            raise RuntimeError(
                "视觉模型无法读取画布；请检查当前模型是否支持图片，或配置专用视觉 Provider"
            ) from exc
        raw = str(getattr(response, "completion_text", response) or "").strip()
        guess = self._clean_draw_guess(raw)
        if not guess:
            raise RuntimeError("视觉模型没有给出有效猜测")
        return guess

    @staticmethod
    def _clean_draw_guess(value: Any) -> str:
        text = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
        text = re.sub(r"^(?:我猜(?:是)?|答案(?:是)?|可能是)[:：\s]*", "", text)
        text = re.split(r"[，,、/；;]", text, maxsplit=1)[0]
        return text.strip(" \t\r\n。！？!?\"'“”‘’《》")[:30]

    @staticmethod
    def _validated_drawing_image(value: Any) -> str:
        image = str(value or "").strip()
        match = re.fullmatch(
            r"data:image/(png|webp);base64,([A-Za-z0-9+/]+={0,2})", image
        )
        if not match:
            raise ValueError("画布图片必须是 PNG 或 WebP")
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("画布图片编码无效") from None
        if not 256 <= len(content) <= 384 * 1024:
            raise ValueError("画布图片大小必须在 256 B 到 384 KB 之间")
        if match.group(1) == "png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PNG 画布图片签名无效")
        if match.group(1) == "webp" and not (
            content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        ):
            raise ValueError("WebP 画布图片签名无效")
        return image

    @staticmethod
    def _room_chat_action(
        room: GameRoom, text: str
    ) -> tuple[str, dict[str, Any]]:
        """Recognize authoritative game intents; ordinary conversation stays chat."""
        normalized = re.sub(r"[\s，。！!？?、]", "", str(text or "").lower())
        if any(phrase in normalized for phrase in ("关闭房间", "结束房间", "销毁房间")):
            return "close", {}

        aliases: tuple[tuple[GameType, tuple[str, ...]], ...] = (
            ("turtle_soup", ("海龟汤",)),
            ("tictactoe", ("井字棋", "圈叉棋")),
            ("xiangqi", ("中国象棋", "象棋")),
            ("gomoku", ("五子棋",)),
            ("pig_dice", ("贪心骰子", "小猪骰子", "骰子")),
            ("draw_guess", ("你画我猜", "画画猜词", "画图猜词")),
            ("blackjack", ("二十一点", "21点", "黑杰克")),
        )
        switch_words = (
            "切换",
            "换成",
            "换个游戏",
            "换游戏",
            "改成",
            "改玩",
            "想玩",
            "玩一局",
            "来一局",
            "来一盘",
        )
        for game_type, names in aliases:
            mentions_game = any(name in normalized for name in names)
            starts_game_request = normalized.startswith(
                ("玩", "来玩", "我们玩", "下", "来下", "开一局", "来一盘")
            )
            if mentions_game and (
                any(word in normalized for word in switch_words) or starts_game_request
            ):
                if game_type == room.game_type and any(
                    word in normalized for word in ("再来一局", "再玩一局", "下一局")
                ):
                    return "rematch", {}
                options: dict[str, Any] = {"game_type": game_type}
                if game_type == "turtle_soup":
                    if any(word in normalized for word in ("我出题", "你来猜", "bot猜")):
                        options["turtle_soup_mode"] = "player_host"
                    elif any(word in normalized for word in ("你出题", "我来猜", "bot出题")):
                        options["turtle_soup_mode"] = "bot_host"
                return "switch_game", options

        if room.game_type == "turtle_soup" and any(
            word in normalized for word in ("切换玩法", "换玩法", "我出题", "你出题")
        ):
            mode: TurtleSoupMode = (
                "player_host"
                if any(word in normalized for word in ("我出题", "你来猜", "bot猜"))
                else "bot_host"
            )
            return "switch_soup_mode", {"turtle_soup_mode": mode}
        if any(word in normalized for word in ("再来一局", "再来一题", "再玩一局", "重新开一局", "下一局")):
            return "rematch", {}
        if any(word in normalized for word in ("悔棋", "撤回上一步", "撤销上一步")):
            return "undo", {}
        if normalized in {"暂停", "先暂停", "暂停一下", "暂停游戏"}:
            return "pause", {}
        if normalized in {"继续", "继续游戏", "恢复游戏", "接着玩"}:
            return "resume", {}
        if any(word in normalized for word in ("投降", "认输", "揭晓答案", "公布答案", "看汤底", "放弃本局", "放弃这题")):
            return "resign", {}

        if room.game_type != "turtle_soup" or not isinstance(room.game, TurtleSoupGame):
            return "", {}
        if any(word in normalized for word in ("给个提示", "来个提示", "申请提示", "提示一下")):
            return "soup_hint", {}
        if room.game.mode == "player_host":
            if any(word in normalized for word in ("你猜对了", "bot猜对了", "猜中了", "答案正确")):
                return "soup_correct", {}
            if room.status == "active":
                return "soup_respond", {}
            return "", {}
        if any(word in normalized for word in ("我猜答案", "完整答案", "完整推理", "真相是", "答案是")):
            return "soup_answer", {}
        if room.status == "active" and any(
            marker in str(text) for marker in ("?", "？", "吗", "是否", "是不是", "有没有", "为什么", "会不会", "能否")
        ):
            return "soup_question", {}
        return "", {}

    async def _refine_turtle_chat_action(
        self, room: GameRoom, text: str, *, proposed_action: str
    ) -> str:
        """Separate turtle-soup gameplay from casual room chat using public facts only."""
        game = room.game
        if not isinstance(game, TurtleSoupGame):
            return proposed_action
        mode = game.mode
        allowed = (
            {"chat", "soup_respond"}
            if mode == "player_host"
            else {"chat", "soup_question", "soup_answer"}
        )
        puzzle_surface = (
            game.puzzle.surface if mode == "bot_host" and game.puzzle is not None else ""
        )
        public_entries = []
        for entry in game.entries[-6:]:
            public_entries.append(
                {
                    "prompt": entry.prompt,
                    "response": entry.response,
                    "kind": entry.kind,
                }
            )
        system_prompt = (
            "你只负责判断一条 WebUI 消息是海龟汤游戏输入还是普通闲聊。"
            "不得回答消息，不得推测汤底，只输出一个允许的动作名称。"
        )
        choices = "、".join(sorted(allowed))
        prompt = (
            f"玩法={mode}；允许动作={choices}；汤面={puzzle_surface or '玩家出题，Bot 只看公开线索'}；"
            f"最近公开回合={json.dumps(public_entries, ensure_ascii=False)}；消息={text}\n"
            "与当前汤题、Bot 最近问题或公开线索无关的内容必须判为 chat。"
        )
        try:
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=15
            )
        except RuntimeError:
            return proposed_action
        normalized = raw.strip().lower().strip("`'\" 。")
        return normalized if normalized in allowed else proposed_action

    async def _decide_ui_undo(
        self, room: GameRoom, visitor: Visitor
    ) -> tuple[bool, str]:
        raw = await self._generate_persona_text(
            room,
            "玩家在房间对话中请求悔棋。结合当前人格决定是否同意，只输出 JSON："
            '{"accept":true或false,"reply":"一句简短自然回复"}。',
        )
        accepted = True
        reply = "这次可以，退回上一轮。"
        for candidate in re.findall(r"\{.*?\}", raw or "", re.DOTALL):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            accepted = bool(data.get("accept"))
            reply = str(data.get("reply") or reply).strip()[:300]
            break
        return accepted, reply

    async def _generate_room_chat_reply(
        self,
        room: GameRoom,
        visitor: Visitor,
        text: str,
        *,
        is_player: bool,
        is_current_player: bool,
    ) -> str:
        persona = await self._persona_prompt(room)
        memory = await self._memory_context_for_visitor(room, visitor, text)
        scene = self._companion_scene_for_visitor(room, visitor)
        identity = (
            "玩家"
            if is_player
            else "已绑定观众"
            if visitor.identity_confirmed
            else "匿名观众"
        )
        public_name = (
            visitor.display_name if visitor.identity_confirmed and visitor.display_name else "匿名观众"
        )
        recent_lines: list[str] = []
        for message in room.messages[-16:]:
            role = str(message.get("role") or "system")
            if role == "user":
                sender = str(message.get("sender_name") or "匿名观众")
                number = message.get("sender_number")
                label = f"{sender}（{number}号）" if number else sender
            elif role == "bot":
                label = "Bot"
            else:
                label = "系统"
            recent_lines.append(f"{label}：{str(message.get('content') or '')[:500]}")
        state = "\n".join(self._live_game_state(room))
        system_prompt = (
            f"{persona}\n\n{scene}\n\n{memory}\n\n"
            f"你正在花火陪你玩 WebUI 的房间中与用户聊天，当前游戏是{self._game_label(room.game_type)}。"
            "这里的聊天只属于当前房间，不得声称已向 QQ 发消息。保持原有人格、关系和自然语气。"
            "系统会在模型调用前执行有权限的游戏指令；你不能自行声称已经落子、切换游戏、投降、"
            "暂停、悔棋或改变房间状态。海龟汤中绝不能透露未公开的汤底或隐藏事实。"
        ).strip()
        prompt = (
            f"当前发言者：{public_name}（{visitor.number}号），身份={identity}，"
            f"是否当前回合玩家={'是' if is_current_player else '否'}。\n"
            f"当前公开游戏状态：\n{state}\n\n"
            "房间最近公开对话：\n"
            + ("\n".join(recent_lines) or "暂无")
            + f"\n\n请只回复当前这条消息：{text}"
        )
        try:
            return (
                await self._call_room_model(
                    room, system_prompt=system_prompt, prompt=prompt, timeout=35
                )
            )[:500]
        except RuntimeError:
            return "我现在暂时没法组织好回复，稍后再和我说一次。"

    async def _memory_context_for_visitor(
        self, room: GameRoom, visitor: Visitor, query: str
    ) -> str:
        if (
            not visitor.identity_confirmed
            or not visitor.qq
            or room.source != "private"
            or len(room.visitors) != 1
        ):
            return ""
        bridge = self._memory_bridge()
        composer = getattr(bridge, "compose_context", None) if bridge else None
        if not callable(composer):
            return ""
        try:
            return str(
                await composer(
                    query=query,
                    session_context={
                        "scope": room.source,
                        "session_id": room.session_id,
                        "platform": room.platform,
                        "user_id": visitor.qq,
                        "group_id": room.group_id,
                    },
                    top_k=4,
                    max_chars=1800,
                    retrieval_profile="companion",
                )
                or ""
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 读取 WebUI 发言者记忆失败: %s", exc)
            return ""

    def _companion_scene_for_visitor(self, room: GameRoom, visitor: Visitor) -> str:
        if (
            not visitor.identity_confirmed
            or not visitor.qq
            or room.source != "private"
            or len(room.visitors) != 1
        ):
            return ""
        api = self._private_companion_api()
        getter = getattr(api, "get_realtime_context", None) if api else None
        if not callable(getter):
            return ""
        try:
            result = getter(visitor.qq, purpose="game")
            return str(result.get("prompt") or "") if isinstance(result, dict) else ""
        except Exception as exc:
            logger.debug("[GameCompanion] 读取 WebUI 发言者陪伴场景失败: %s", exc)
            return ""

    async def _comment(self, room: GameRoom, prompt: str) -> None:
        text = await self._generate_persona_text(room, prompt)
        if not text or room.status == "closed":
            return
        room.add_message("bot", text)

    async def _decide_rematch(
        self, room: GameRoom, *, visitor: Visitor | None = None
    ) -> None:
        raw = await self._generate_persona_text(
            room,
            "玩家在网页申请再来一局。请结合当前人格决定是否接受，只输出 JSON："
            '{"accept":true或false,"difficulty":"easy/normal/hard","reply":"一句自然回复"}。'
            "如果接受，可以根据人格和此前胜负重新选择本局棋力；贪心骰子中 difficulty "
            "分别代表稳健、均衡和大胆的风险倾向；二十一点中 easy/normal 庄家软 17 停牌，"
            "hard 庄家软 17 继续补牌。",
        )
        accept = True
        reply = "那就再来一局。"
        difficulty: Difficulty = room.difficulty
        for candidate in re.findall(r"\{.*?\}", raw or "", re.DOTALL):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            accept = bool(data.get("accept"))
            reply = str(data.get("reply") or reply).strip()[:300]
            difficulty = self._difficulty(data.get("difficulty") or room.difficulty)
            break
        if room.room_id not in self.manager.rooms:
            return
        applied = await self.manager.resolve_rematch(
            room,
            accepted=accept,
            message=reply,
            difficulty=difficulty,
        )
        if not applied:
            return
        if visitor is not None:
            room.record_chat_memory(visitor, "bot", reply)
        if accept and room.status == "rematch_pending":
            try:
                await self.manager.restart_finished_game(room, difficulty=difficulty)
            except (ValueError, PermissionError, RuntimeError) as exc:
                logger.warning("[GameCompanion] 再来一局无法开始: %s", exc)
                async with room.lock:
                    if room.status == "rematch_pending":
                        room.status = "finished"
                        room.add_message("system", "再来一局暂时无法开始，房间已回到结束状态。")

    async def _prepare_turtle_soup(self, room: GameRoom, game: TurtleSoupGame) -> None:
        if game.mode != "bot_host":
            return
        recent = list(room.turtle_soup_recent_signatures)
        persona = await self._persona_prompt(room)
        last_error = ""
        for attempt in range(1, 4):
            try:
                system_prompt, prompt = generation_prompt(
                    difficulty=room.difficulty,
                    content_level=game.content_level,
                    recent_signatures=recent,
                )
                if persona:
                    system_prompt = (
                        f"{persona}\n\n{system_prompt}\n"
                        "人格只影响叙事气质，不得把真实用户、私人记忆或生活场景写进题目。"
                    )
                raw = await self._call_room_model(
                    room, system_prompt=system_prompt, prompt=prompt, timeout=35
                )
                data = extract_json_object(raw)
                if data is None:
                    raise ValueError("出题结果不是有效 JSON")
                puzzle = puzzle_from_mapping(data, content_level=game.content_level)
                if puzzle.signature in set(recent):
                    raise ValueError("题目与本房间最近的主题重复")
                check_system, check_prompt = validation_prompt(puzzle)
                check = await self._call_room_model(
                    room,
                    system_prompt=check_system,
                    prompt=check_prompt,
                    timeout=30,
                )
                if not validation_passed(check):
                    raise ValueError("题目未通过独立自洽性校验")
                if await self.manager.complete_turtle_soup_generation(
                    room, game, puzzle
                ):
                    return
                return
            except (RuntimeError, ValueError, OSError) as exc:
                last_error = str(exc)
                logger.info(
                    "[GameCompanion] 海龟汤第 %s 次出题未采用: %s",
                    attempt,
                    exc,
                )
        puzzle = fallback_puzzle(
            content_level=game.content_level,
            excluded_signatures=set(recent),
        )
        applied = await self.manager.complete_turtle_soup_generation(room, game, puzzle)
        if applied:
            logger.warning(
                "[GameCompanion] Bot 出题连续失败，当前局使用内置兜底题: %s",
                last_error or "模型不可用",
            )

    async def submit_turtle_soup_question(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, question = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=200,
        )
        try:
            if game.puzzle is None:
                raise RuntimeError("题目尚未准备完成")
            system_prompt, prompt = question_judge_prompt(
                game.puzzle,
                question=question,
                public_history=public_judge_history(game.entries),
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            verdict, matched_facts = parse_question_judgment(
                raw, fact_count=len(game.puzzle.key_facts)
            )
            if verdict == "compound":
                matched_facts.clear()
            applied = await self.manager.resolve_turtle_soup_question(
                room,
                game,
                question,
                verdict,
                source=source,
                matched_facts=matched_facts,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前题目")
            return {
                "verdict": verdict,
                "reply": VERDICT_LABELS[verdict],
                "question_count": game.question_count,
            }
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法判断这个问题，请稍后重试") from exc

    async def submit_turtle_soup_answer(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, answer = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=800,
        )
        try:
            if game.puzzle is None:
                raise RuntimeError("题目尚未准备完成")
            system_prompt, prompt = answer_judge_prompt(
                game.puzzle,
                answer=answer,
                discovered_facts=game.discovered_facts,
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            solved, coverage, matched_facts = parse_answer_judgment(
                raw, fact_count=len(game.puzzle.key_facts)
            )
            applied = await self.manager.resolve_turtle_soup_answer(
                room,
                game,
                answer,
                solved=solved,
                source=source,
                matched_facts=matched_facts,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前题目")
            result: dict[str, Any] = {
                "solved": solved,
                "coverage": round(coverage, 2),
                "reply": (
                    "推理正确，汤底已经揭晓。"
                    if solved
                    else "已经接近了，但还缺少关键环节。"
                ),
            }
            if solved:
                result["solution"] = game.puzzle.solution
            return result
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法判断这份推理，请稍后重试") from exc

    async def submit_reverse_turtle_soup_turn(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        game, player_text = await self.manager.begin_turtle_soup_interaction(
            room,
            text,
            source=source,
            visitor_token=visitor_token,
            actor_qq=actor_qq,
            limit=800,
        )
        try:
            if game.mode != "player_host":
                raise ValueError("当前不是玩家出题、Bot 猜的玩法")
            system_prompt, prompt = reverse_turn_prompt(
                player_text=player_text,
                public_history=reverse_public_history(game.entries),
                persona=await self._persona_prompt(room),
            )
            raw = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=30
            )
            bot_action, bot_text = parse_reverse_turn(raw)
            applied = await self.manager.resolve_reverse_turtle_soup_turn(
                room,
                game,
                player_text,
                bot_action=bot_action,
                bot_text=bot_text,
                source=source,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前回合")
            return {
                "bot_action": bot_action,
                "reply": bot_text,
                "turn_count": game.turn_count,
            }
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法继续推理，请稍后重试") from exc

    async def _call_room_model(
        self,
        room: GameRoom,
        *,
        system_prompt: str,
        prompt: str,
        timeout: int,
    ) -> str:
        provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError("当前会话没有可用的大语言模型")
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("模型响应超时") from exc
        except Exception as exc:
            raise RuntimeError("模型调用失败") from exc
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("模型没有返回有效内容")
        return text

    async def _announce_turtle_soup_hint(
        self, room: GameRoom, hint: str, *, visitor: Visitor | None = None
    ) -> None:
        if not hint or room.status == "closed":
            return
        intro = await self._generate_persona_text(
            room,
            "玩家刚申请了一次海龟汤提示。请用当前人格说一句很短的引子，"
            "不要猜测或补充任何线索。",
        )
        text = f"{intro}\n提示：{hint}" if intro else f"提示：{hint}"
        room.add_message("bot", text)
        if visitor is not None:
            room.record_chat_memory(visitor, "bot", text)

    async def _generate_persona_text(self, room: GameRoom, prompt: str) -> str:
        provider = self.context.get_using_provider(room.session_id)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return ""
        persona = await self._persona_prompt(room)
        memory = await self._memory_context(room, prompt)
        companion_scene = self._companion_scene_prompt(room)
        role_constraint = ""
        if isinstance(room.game, DrawGuessGame):
            role_constraint = (
                "你画我猜中的角色固定为：用户始终负责作画，你（Bot）始终负责看图猜答案。"
                "你没有参与绘画，任何时候都不得声称自己画得好或不好。"
            )
        system_prompt = (
            f"{persona}\n\n{companion_scene}\n\n{memory}\n\n"
            f"你正在与用户通过花火陪你玩 WebUI 玩{self._game_label(room.game_type)}。保持原有人格和关系语气，"
            "只回应当前游戏事件，不输出规则说明或格式标签。"
            f"{role_constraint}海龟汤中绝不能猜测或泄露尚未公开的汤底。"
        ).strip()
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=30,
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 生成人格化游戏回复失败: %s", exc)
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()[:500]

    async def _llm_gen_neutral(self, prompt: str, *, system_prompt: str = "") -> str:
        """管理台等无会话场景的中性 LLM 生成：不注入游戏人格与记忆。

        优先使用任一活跃房间的会话 Provider，其次使用默认 Provider；均不可用时返回空字符串，
        由调用方决定本地兜底策略。
        """
        provider = None
        candidates = [None]
        try:
            for r in self.manager.rooms.values():
                sid = getattr(r, "session_id", None)
                if sid:
                    candidates.append(sid)
        except Exception:
            pass
        for session_id in candidates:
            try:
                p = self.context.get_using_provider(session_id)
            except Exception as exc:
                logger.debug("[GameCompanion] 获取中立 LLM Provider 失败: %s", exc)
                p = None
            if p is not None and callable(getattr(p, "text_chat", None)):
                provider = p
                if session_id:
                    break
        if provider is None:
            return ""
        use_system = (
            system_prompt
            or "你是中文助手。请严格按照要求只输出结构化内容，不要任何解释、前后缀或 markdown 代码块。"
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=use_system),
                timeout=30,
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 中立 LLM 生成失败: %s", exc)
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()

    @staticmethod
    def _persona_text(persona: object) -> str:
        if isinstance(persona, dict):
            return str(persona.get("prompt") or persona.get("system_prompt") or "")
        return str(
            getattr(persona, "prompt", "")
            or getattr(persona, "system_prompt", "")
        )

    @staticmethod
    async def _resolve_maybe_awaitable(value: object, *, timeout: float = 3) -> object:
        if inspect.isawaitable(value):
            return await asyncio.wait_for(value, timeout=timeout)
        return value

    async def _persona_prompt(self, room: GameRoom) -> str:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""

        conversation_persona_id: str | None = None
        try:
            conversation_manager = getattr(self.context, "conversation_manager", None)
            current_getter = getattr(
                conversation_manager, "get_curr_conversation_id", None
            )
            conversation_getter = getattr(conversation_manager, "get_conversation", None)
            if callable(current_getter) and callable(conversation_getter):
                conversation_id = await self._resolve_maybe_awaitable(
                    current_getter(room.session_id)
                )
                if conversation_id:
                    conversation = await self._resolve_maybe_awaitable(
                        conversation_getter(room.session_id, conversation_id)
                    )
                    if isinstance(conversation, dict):
                        raw_persona_id = conversation.get("persona_id")
                    else:
                        raw_persona_id = getattr(conversation, "persona_id", None)
                    if raw_persona_id is not None:
                        conversation_persona_id = str(raw_persona_id)
        except Exception as exc:
            logger.debug("[GameCompanion] 读取当前会话人格选择失败: %s", exc)

        resolver = getattr(manager, "resolve_selected_persona", None)
        if callable(resolver):
            provider_settings: dict[str, Any] | None = None
            try:
                config_manager = getattr(manager, "acm", None) or getattr(
                    self.context, "astrbot_config_mgr", None
                )
                config_getter = getattr(config_manager, "get_conf", None)
                if callable(config_getter):
                    config = await self._resolve_maybe_awaitable(
                        config_getter(room.session_id)
                    )
                    if callable(getattr(config, "get", None)):
                        settings = config.get("provider_settings", {})
                        if isinstance(settings, dict):
                            provider_settings = settings
                resolved = await self._resolve_maybe_awaitable(
                    resolver(
                        umo=room.session_id,
                        conversation_persona_id=conversation_persona_id,
                        platform_name=room.session_id.split(":", 1)[0],
                        provider_settings=provider_settings,
                    )
                )
                persona = resolved[1] if isinstance(resolved, (tuple, list)) else resolved
                return self._persona_text(persona)
            except Exception as exc:
                logger.debug("[GameCompanion] 按会话解析人格失败，尝试兼容回退: %s", exc)

        getter = getattr(manager, "get_default_persona_v3", None)
        if not callable(getter):
            return ""
        try:
            try:
                value = getter(room.session_id)
            except TypeError:
                value = getter()
            persona = await self._resolve_maybe_awaitable(value)
            return self._persona_text(persona)
        except Exception as exc:
            logger.debug("[GameCompanion] 读取默认人格失败: %s", exc)
            return ""

    @staticmethod
    def _gomoku_commentary_prompt(
        room: GameRoom, payload: dict[str, Any]
    ) -> str:
        game = room.game
        if not isinstance(game, GomokuGame):
            return ""
        try:
            row = int(payload["row"])
            column = int(payload["column"])
            color = int(payload["color"])
        except (KeyError, TypeError, ValueError):
            return ""
        threat = game.move_threat(row, column, color)
        if threat.kind not in {"multiple", "single"}:
            return ""

        actor_is_human = payload.get("actor") == "human"
        actor = "玩家" if actor_is_human else "Bot"
        opponent = "Bot" if actor_is_human else "玩家"
        color_label = "黑" if color == GOMOKU_BLACK else "白"
        opponent_color = game.bot_color if actor_is_human else game.human_color
        opponent_color_label = "黑" if opponent_color == GOMOKU_BLACK else "白"
        human_stones = sum(
            cell == game.human_color for board_row in game.board for cell in board_row
        )
        bot_stones = sum(
            cell == game.bot_color for board_row in game.board for cell in board_row
        )
        turn = "本局已经结束" if game.finished else (
            "当前轮到玩家" if game.turn == game.human_color else "当前轮到 Bot"
        )
        consequence = (
            f"刚才这步留下了 {len(threat.winning_points)} 个下一手即可连成五子的空位，"
            f"{opponent}下一手无法全部封住。"
            if threat.kind == "multiple"
            else f"刚才这步留下了 1 个下一手即可连成五子的空位，{opponent}下一手仍可封住。"
        )
        return (
            f"五子棋刚发生了一个值得回应的节点。触发者是{actor}，执{color_label}；"
            f"对手是{opponent}，执{opponent_color_label}；最后落子在第 {row + 1} 行第 {column + 1} 列。"
            f"当前玩家有 {human_stones} 颗棋子，Bot 有 {bot_stones} 颗棋子，{turn}。"
            f"{consequence}只围绕这一步和当前情绪，用当前人格简短自然回应；"
            "避免使用专业棋型名称，不要虚构其他落子或胜负。"
        )

    async def _memory_context(self, room: GameRoom, query: str) -> str:
        if (
            not room.player_identity_confirmed
            or room.source != "private"
            or len(room.visitors) != 1
            or (room.multiplayer.enabled and len(room.multiplayer.seats) > 1)
        ):
            return ""
        bridge = self._memory_bridge()
        composer = getattr(bridge, "compose_context", None) if bridge else None
        if not callable(composer):
            return ""
        try:
            return str(
                await composer(
                    query=query,
                    session_context={
                        "scope": room.source,
                        "session_id": room.session_id,
                        "platform": room.platform,
                        "user_id": room.player_qq or room.creator_qq,
                        "group_id": room.group_id,
                    },
                    top_k=4,
                    max_chars=1800,
                    retrieval_profile="companion",
                )
                or ""
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 读取陪伴记忆上下文失败: %s", exc)
            return ""

    async def _record_room_memory(self, room: GameRoom) -> None:
        total_completed = sum(score.completed for score in room.scores.values())
        participants = self._memory_participant_qqs(room)
        has_bound_chat = any(room.chat_transcripts.values())
        if (
            not self.record_shared_experience
            or not participants
            or (total_completed < 1 and not has_bound_chat)
        ):
            return
        bridge = self._memory_bridge()
        recorder = getattr(bridge, "record_shared_experience", None) if bridge else None
        if not callable(recorder):
            return
        summaries = []
        for game_type, score in room.scores.items():
            if score.completed:
                if game_type == "turtle_soup":
                    summaries.append(
                        f"海龟汤 {score.completed} 题（玩家侧记分 {score.human_wins} 题，"
                        f"Bot 侧记分 {score.bot_wins} 题，共提问 {room.turtle_soup_stats.questions} 次，"
                        f"使用提示 {room.turtle_soup_stats.hints} 次）"
                    )
                elif game_type == "draw_guess":
                    summaries.append(
                        f"你画我猜 {score.completed} 轮（合作猜中 {score.human_wins} 轮，"
                        f"未猜中 {score.bot_wins} 轮）"
                    )
                else:
                    summaries.append(
                        f"{self._game_label(game_type)} {score.completed} 局（用户胜 {score.human_wins} 局，"
                        f"Bot 胜 {score.bot_wins} 局，平局 {score.draws} 局）"
                    )
        summary = (
            "Bot 与用户完成了游戏：" + "；".join(summaries) + "。"
            if summaries
            else "用户在花火陪你玩房间中与 Bot 和其他房间成员进行了交流。"
        )
        metadata = {
            "games": {
                game_type: {
                    "completed": score.completed,
                    "human_wins": score.human_wins,
                    "bot_wins": score.bot_wins,
                    "draws": score.draws,
                }
                for game_type, score in room.scores.items()
                if score.completed
            },
            "room_id": room.room_id,
            "difficulty": room.difficulty,
            "completed_games": total_completed,
            "turtle_soup": {
                "questions": room.turtle_soup_stats.questions,
                "hints": room.turtle_soup_stats.hints,
                "answer_attempts": room.turtle_soup_stats.answer_attempts,
            },
            "participant_count": len(participants),
        }
        for player_qq in participants:
            transcript = room.chat_transcripts.get(player_qq, [])
            chat_excerpt = self._chat_memory_excerpt(transcript)
            content = summary
            if chat_excerpt:
                content += " 与该用户有关的房间对话摘录：" + chat_excerpt
            try:
                await recorder(
                    content=content,
                    experience_type="game",
                    user_id=player_qq,
                    user_name=room.participant_names.get(
                        player_qq,
                        room.creator_name if player_qq == room.creator_qq else "",
                    ),
                    scope=room.source,
                    session_id=room.session_id,
                    platform=room.platform,
                    source_plugin=PLUGIN_NAME,
                    memory_id=f"game-companion-{room.room_id}-{player_qq}",
                    confidence=0.95,
                    importance=0.66,
                    metadata={**metadata, "chat_turns": len(transcript)},
                )
            except Exception as exc:
                logger.debug(
                    "[GameCompanion] 为玩家 %s 写入共同游戏经历失败: %s",
                    player_qq,
                    exc,
                )

    @staticmethod
    def _chat_memory_excerpt(transcript: list[dict[str, str]]) -> str:
        """Build a bounded per-user excerpt without mixing other visitors' speech."""
        parts: list[str] = []
        for entry in transcript[-12:]:
            content = " ".join(str(entry.get("content") or "").split())[:140]
            if not content:
                continue
            label = "用户" if entry.get("role") == "user" else "Bot"
            parts.append(f"{label}：{content}")
        return "；".join(parts)[:1600]

    @staticmethod
    def _memory_participant_qqs(room: GameRoom) -> list[str]:
        if room.multiplayer.enabled:
            return list(
                dict.fromkeys(
                    [
                        seat.qq
                        for seat in room.multiplayer.seats
                        if seat.identity_confirmed and seat.qq
                    ]
                    + sorted(room.confirmed_participant_qqs)
                )
            )
        return list(
            dict.fromkeys(
                (
                    [room.player_qq]
                    if room.player_identity_confirmed and room.player_qq
                    else []
                )
                + sorted(room.confirmed_participant_qqs)
            )
        )

    def _memory_bridge(self) -> Any | None:
        for name in (
            "data.plugins.astrbot_plugin_memory_companion.main",
            "astrbot_plugin_memory_companion.main",
        ):
            module = sys.modules.get(name)
            getter = (
                getattr(module, "get_memory_companion_bridge", None) if module else None
            )
            if callable(getter):
                bridge = getter()
                if bridge is not None:
                    return bridge
        return None

    def _private_companion_api(self) -> Any | None:
        for name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            module = sys.modules.get(name)
            getter = (
                getattr(module, "get_private_companion_api", None) if module else None
            )
            if callable(getter):
                api = getter()
                if api is not None:
                    return api
        return None

    def _companion_scene_prompt(self, room: GameRoom) -> str:
        if (
            not room.player_identity_confirmed
            or room.source != "private"
            or len(room.visitors) != 1
            or (room.multiplayer.enabled and len(room.multiplayer.seats) > 1)
        ):
            return ""
        api = self._private_companion_api()
        getter = getattr(api, "get_realtime_context", None) if api else None
        if not callable(getter):
            return ""
        try:
            result = getter(room.player_qq or room.creator_qq, purpose="game")
            return str(result.get("prompt") or "") if isinstance(result, dict) else ""
        except Exception as exc:
            logger.debug("[GameCompanion] 读取陪伴生活场景失败: %s", exc)
            return ""

    def _notify_companion_activity(self, room: GameRoom, phase: str) -> None:
        api = self._private_companion_api()
        if api is None:
            return
        activity_id = f"game-companion:{room.room_id}"
        try:
            if phase == "ended":
                notifier = getattr(api, "notify_external_activity_ended", None)
                if callable(notifier):
                    notifier(activity_id)
                return
            method_name = (
                "notify_external_activity_started"
                if phase == "started"
                else "notify_external_activity_updated"
            )
            notifier = getattr(api, method_name, None)
            if callable(notifier):
                notifier(
                    activity_id,
                    user_id=room.player_qq or room.creator_qq,
                    kind="shared_game",
                    label=f"正在和用户玩{self._game_label(room.game_type)}",
                    source_plugin=PLUGIN_NAME,
                    ttl_seconds=max(60, self.manager.idle_timeout or 300),
                    metadata={"room_id": room.room_id, "game": room.game_type},
                )
        except Exception as exc:
            logger.debug("[GameCompanion] 同步陪伴活动状态失败: %s", exc)

    @staticmethod
    def _current_player_visitors(room: GameRoom) -> list[Visitor]:
        if room.multiplayer.enabled:
            return [
                visitor
                for seat in room.multiplayer.seats
                if (visitor := room.visitors.get(seat.visitor_token)) is not None
                and visitor.identity_confirmed
                and visitor.qq
            ]
        player = room.player
        return (
            [player]
            if player is not None and player.identity_confirmed and player.qq
            else []
        )

    def _capture_round_participants(self, room: GameRoom, *, reset: bool = False) -> None:
        if reset:
            room.round_participant_qqs.clear()
        for visitor in self._current_player_visitors(room):
            room.round_participant_qqs.add(visitor.qq)
            room.participant_names[visitor.qq] = visitor.display_name

    def _queue_companion_round_event(
        self, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        if not self.companion_afterglow_enabled:
            return
        task = self._spawn(
            self._report_companion_game_event(room, "round_finished", payload)
        )
        self._companion_round_event_tasks[room.room_id] = task

        def clear(finished: asyncio.Task) -> None:
            if self._companion_round_event_tasks.get(room.room_id) is finished:
                self._companion_round_event_tasks.pop(room.room_id, None)

        task.add_done_callback(clear)

    async def _report_companion_game_event(
        self,
        room: GameRoom,
        event_type: str,
        payload: dict[str, Any],
        *,
        visitors: list[Visitor] | None = None,
    ) -> None:
        if not self.companion_afterglow_enabled:
            return
        api = self._private_companion_api()
        recorder = getattr(api, "record_game_event", None) if api else None
        if not callable(recorder):
            logger.debug(
                "[GameCompanion] 陪伴插件未提供游戏余韵 API，已跳过联动"
            )
            return
        if visitors is None:
            by_qq = {visitor.qq: visitor for visitor in self._current_player_visitors(room)}
            for qq in room.round_participant_qqs:
                if qq not in by_qq:
                    by_qq[qq] = Visitor(
                        token="",
                        number=0,
                        qq=qq,
                        display_name=room.participant_names.get(qq, ""),
                        identity_confirmed=True,
                    )
            participants = list(by_qq.values())
        else:
            participants = [
                visitor
                for visitor in visitors
                if visitor.identity_confirmed and visitor.qq
            ]
        if not participants:
            return
        raw_result = str(payload.get("result") or "")
        bot_result = {
            "human_win": "bot_loss",
            "bot_win": "bot_win",
            "draw": "draw",
        }.get(raw_result, "completed")
        score = room.current_score
        round_number = score.completed

        async def submit(visitor: Visitor) -> None:
            event_id = (
                f"{room.room_id}:{room.game_type}:{round_number}:"
                f"{event_type}:{visitor.qq}"
            )
            event_payload = {
                "event_id": event_id,
                "event_type": event_type,
                "user_id": visitor.qq,
                "user_name": visitor.display_name,
                "game": room.game_type,
                "game_label": self._game_label(room.game_type),
                "bot_result": bot_result,
                "request_text": str(payload.get("request_text") or "")[:240],
                "recent_context": self._companion_game_recent_context(
                    room, visitor.qq
                ),
                "room_id": room.room_id,
                "session_id": room.session_id,
                "scope": room.source,
                "difficulty": room.difficulty,
                "round_number": round_number,
                "score": {
                    "completed": score.completed,
                    "human_wins": score.human_wins,
                    "bot_wins": score.bot_wins,
                    "draws": score.draws,
                },
                "occurred_at": time.time(),
                "source_plugin": PLUGIN_NAME,
            }
            try:
                result = recorder(event_payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(
                    "[GameCompanion] 为玩家 %s 上报游戏余韵失败: %s",
                    visitor.qq,
                    exc,
                )

        await asyncio.gather(*(submit(visitor) for visitor in participants))

    @staticmethod
    def _companion_game_recent_context(room: GameRoom, qq: str) -> str:
        transcript = room.chat_transcripts.get(str(qq or ""), [])
        lines: list[str] = []
        for entry in transcript[-6:]:
            content = " ".join(str(entry.get("content") or "").split())[:180]
            if not content:
                continue
            role = "用户" if entry.get("role") == "user" else "Bot"
            lines.append(f"{role}：{content}")
        return "\n".join(lines)[:900]

    def _register_companion_invite_ability(self) -> bool:
        if not self.companion_invites_enabled:
            return False
        now = time.monotonic()
        if now < self._next_companion_registration_at:
            return bool(self._companion_invite_api)
        self._next_companion_registration_at = now + 15
        api = self._private_companion_api()
        if api is None:
            return False
        if api is self._companion_invite_api:
            return True
        if self._companion_invite_api is not None:
            self._unregister_companion_invite_ability()
        registrar = getattr(api, "register_proactive_ability", None)
        if not callable(registrar):
            return False
        try:
            registered = bool(
                registrar(
                    {
                        "name": "game_companion_invite",
                        "module": "花火陪你玩",
                        "label": "邀请一起玩游戏",
                        "description": "结合近期共同游戏、当前人格和生活状态，自然邀请用户玩一局游戏。",
                        "when": "有闲暇、想陪用户玩，或对最近胜负仍有余味时",
                        "use_for": "提出低压力的游戏邀请，或自然约一次再战",
                        "avoid": "用户正在游戏、房间已满、关系或免打扰不适合时不要邀请；不要提前创建房间",
                        "share_probability": self.companion_invite_probability,
                        "min_interval_hours": self.companion_invite_cooldown_hours,
                        "default_enabled": True,
                        "availability": self._companion_invite_available,
                        "executor": self._execute_companion_invite,
                    }
                )
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 注册陪伴主动邀请失败: %s", exc)
            return False
        if registered:
            self._companion_invite_api = api
            logger.info("[GameCompanion] 已向陪伴插件注册主动游戏邀请能力")
        return registered

    def _unregister_companion_invite_ability(self) -> None:
        api = self._companion_invite_api
        self._companion_invite_api = None
        if api is None:
            return
        unregister = getattr(api, "unregister_proactive_ability", None)
        if callable(unregister):
            try:
                unregister("game_companion_invite")
            except Exception as exc:
                logger.debug("[GameCompanion] 注销陪伴主动邀请失败: %s", exc)

    def _companion_invite_available(self, context: dict[str, Any]) -> bool:
        if not (
            self.companion_invites_enabled
            and self.server_enabled
            and self.private_rooms_enabled
            and any(self.manager.enabled_games.values())
        ):
            return False
        user = context.get("user") if isinstance(context, dict) else {}
        user = user if isinstance(user, dict) else {}
        user_id = str(user.get("user_id") or "").strip()
        if not user_id:
            return False
        for room in self.manager.rooms.values():
            if user_id in {room.creator_qq, room.player_qq}:
                return False
            if any(visitor.qq == user_id for visitor in self._current_player_visitors(room)):
                return False
        active_private = sum(
            room.source == "private" for room in self.manager.rooms.values()
        )
        if self.manager.max_private_rooms and active_private >= self.manager.max_private_rooms:
            return False
        afterglow = user.get("game_afterglow")
        if isinstance(afterglow, dict):
            expires_at = self._safe_float(afterglow.get("expires_at"))
            invite_interest = self._safe_int(afterglow.get("invite_interest"))
            if expires_at > time.time() and invite_interest < 20:
                return False
        return True

    def _execute_companion_invite(self, context: dict[str, Any]) -> dict[str, Any]:
        user = context.get("user") if isinstance(context, dict) else {}
        user = user if isinstance(user, dict) else {}
        afterglow = user.get("game_afterglow")
        afterglow = afterglow if isinstance(afterglow, dict) else {}
        active_afterglow = self._safe_float(afterglow.get("expires_at")) > time.time()
        last_game = str(afterglow.get("game_label") or "").strip()
        tone = str(afterglow.get("tone") or "").strip()[:160]
        games = "、".join(
            self._game_label(game_type)
            for game_type in SUPPORTED_GAMES
            if self.manager.game_enabled(game_type)
        )
        details = (
            f"最近和该用户玩的游戏是{last_game}，当前余味是：{tone}。"
            if active_afterglow and last_game and tone
            else f"可以从{games}中按人格和用户偏好自然挑一种。"
        )
        return {
            "ok": True,
            "context": (
                "请按当前人格向该用户发出一次轻松、可拒绝的游戏邀请。"
                f"{details}只表达邀请，不创建房间、不生成链接；等用户明确接受后再由正常对话工具创建。"
            ),
            "summary": "想邀请用户一起玩游戏",
            "status": "已形成游戏邀请动机",
        }

    async def _send_to_origin(self, room: GameRoom, text: str) -> None:
        if not text:
            return
        try:
            await self.context.send_message(
                room.session_id, MessageChain([Plain(text)])
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 回发游戏消息失败: %s", exc)

    async def _deliver_room_link(
        self,
        room: GameRoom,
        url: str,
        *,
        reused: bool,
        restarted: bool,
    ) -> bool:
        if restarted:
            title = "新一局已在原游戏房间开始："
        elif reused:
            title = "继续使用当前游戏房间："
        else:
            title = f"{self._game_label(room.game_type)}房间已准备好："
        lines = [title, url]
        if room.player is None and self.manager.empty_player_timeout:
            lines.append(
                f"请在 {self.manager.empty_player_timeout} 秒内进入玩家席，"
                "否则房间会自动销毁。"
            )
        try:
            delivered = await self.context.send_message(
                room.session_id,
                MessageChain([Plain("\n".join(lines))]),
            )
        except Exception as exc:
            logger.warning(
                "[GameCompanion] 独立发送房间链接失败，将交由模型回复回退: %s",
                exc,
            )
            return False
        if not delivered:
            logger.warning(
                "[GameCompanion] 未找到房间会话对应平台，将交由模型回复回退: session=%s",
                room.session_id,
            )
            return False
        logger.info(
            "[GameCompanion] 房间链接已作为独立纯文字消息发送: room=%s session=%s",
            room.room_id,
            room.session_id,
        )
        return True

    async def _watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                await self.manager.sweep_expired()
                self._schedule_tunnel_recovery()
                self._register_companion_invite_ability()
        except asyncio.CancelledError:
            raise

    def _schedule_tunnel_recovery(self) -> None:
        if (
            self._configured_access_base()
            or not self.auto_quick_tunnel
            or not self.manager.rooms
            or not self.room_server.running
            or bool(getattr(self.quick_tunnel, "ready", False))
            or (
                self._tunnel_recovery_task is not None
                and not self._tunnel_recovery_task.done()
            )
        ):
            return
        now = asyncio.get_running_loop().time()
        if now < self._next_tunnel_retry_at:
            return
        self._next_tunnel_retry_at = now + 15
        self._tunnel_recovery_task = self._spawn(self._recover_quick_tunnel())

    async def _recover_quick_tunnel(self) -> None:
        try:
            self.quick_tunnel.local_url = self.room_server.local_base_url
            url = await self.quick_tunnel.start(timeout=40)
        except Exception as exc:
            logger.warning("[GameCompanion] 临时访问通道恢复失败，将稍后重试: %s", exc)
            return
        finally:
            self._tunnel_recovery_task = None
        logger.warning("[GameCompanion] 临时访问通道已恢复，新地址: %s", url)
        for room in list(self.manager.rooms.values()):
            await self._send_to_origin(
                room,
                "游戏访问通道已恢复，原临时链接已经失效。请使用新链接："
                f"{self._room_url(room)}",
            )

    def _room_link_instruction(self, room: GameRoom) -> str:
        instruction = "最终回复必须完整保留 room_url；"
        if room.admin_room:
            instruction += "这是管理员审核房间，访客需要由管理员在游戏管理台安排玩家。"
        else:
            # 统一的绑定引导：无论群/私都是发 /绑定玩家 命令
            bind_hint = (
                "群里发送："
                if room.source == "group"
                else "私聊中发送："
            )
            instruction += (
                f"提醒用户打开页面后查看页面顶部的一次性绑定码，并在{bind_hint}发送该绑定码"
                "（形如：绑定玩家 8位大写字母数字；原私聊也可直接发送绑定码）。"
                "或点击页面「一键复制」按钮后直接粘贴发送。绑定成功后再点击加入玩家席。"
            )
        if room.game_type == "turtle_soup":
            instruction += "说明玩家进入玩家席后由 Bot 准备题目。"
        elif room.game_type == "pig_dice":
            instruction += "说明玩家进入玩家席后直接开始，先手由系统随机决定。"
        elif room.game_type == "draw_guess":
            instruction += "说明玩家进入玩家席后在网页画布作画，并手动点击让 Bot 猜。"
        elif room.game_type == "undercover":
            instruction += (
                f"说明谁是卧底最低 {self.undercover_min_players} 人即可开局。"
                "进入玩家席后会在页面顶部倒计时自动开始（人满立即开局）；"
                "系统会给每位玩家派发身份词条，按照座位顺序依次发言描述，投票淘汰可疑玩家。"
                "在发言轮次，当前发言玩家可以直接在房间聊天区输入消息，会被自动记入发言。"
            )
        else:
            instruction += "说明玩家进入后可选择执棋方。"
        timeout = self.manager.empty_player_timeout
        if timeout:
            instruction += (
                f"明确提醒玩家在 {timeout} 秒内进入玩家席，否则房间会自动销毁。"
            )
        return instruction

    def _register_page_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        register_api(f"{PAGE_API_PREFIX}/rooms", self.page_rooms, ["GET"], "Game rooms")
        register_api(
            f"{PAGE_API_PREFIX}/room/action",
            self.page_room_action,
            ["POST"],
            "Manage a game room",
        )
        register_api(
            f"{PAGE_API_PREFIX}/tunnel/start",
            self.page_tunnel_start,
            ["POST"],
            "Start game quick tunnel",
        )
        register_api(
            f"{PAGE_API_PREFIX}/tunnel/stop",
            self.page_tunnel_stop,
            ["POST"],
            "Stop game quick tunnel",
        )
        register_api(
            f"{PAGE_API_PREFIX}/xiangqi/install",
            self.page_xiangqi_install,
            ["POST"],
            "Install Pikafish",
        )
        register_api(
            f"{PAGE_API_PREFIX}/cloudflared/install",
            self.page_cloudflared_install,
            ["POST"],
            "Install cloudflared",
        )
        register_api(
            f"{PAGE_API_PREFIX}/settings",
            self.page_game_settings,
            ["GET"],
            "Read game settings",
        )
        register_api(
            f"{PAGE_API_PREFIX}/settings/update",
            self.page_game_settings_update,
            ["POST"],
            "Update game settings",
        )
        register_api(
            f"{PAGE_API_PREFIX}/undercover_words",
            self.page_undercover_words,
            ["GET"],
            "List undercover word pairs",
        )
        register_api(
            f"{PAGE_API_PREFIX}/undercover_words/add",
            self.page_undercover_words_add,
            ["POST"],
            "Add undercover word pair",
        )
        register_api(
            f"{PAGE_API_PREFIX}/undercover_words/delete",
            self.page_undercover_words_delete,
            ["POST"],
            "Delete undercover word pair",
        )
        register_api(
            f"{PAGE_API_PREFIX}/undercover_words/batch_import",
            self.page_undercover_words_batch_import,
            ["POST"],
            "Batch import undercover word pairs",
        )
        register_api(
            f"{PAGE_API_PREFIX}/undercover_words/llm_generate_batch",
            self.page_undercover_words_llm_generate_batch,
            ["POST"],
            "Ask LLM to generate undercover word pairs in batch",
        )

    async def page_undercover_words(self) -> dict[str, Any]:
        store = self.undercover_word_store
        items = store.list_all() if store is not None else []
        return {"status": "ok", "data": {"items": items}}

    async def page_undercover_words_add(
        self, payload: Any = None
    ) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = payload or {}
        w1 = str(payload.get("word1") or "").strip()
        w2 = str(payload.get("word2") or "").strip()
        if not w1 or not w2:
            return {
                "status": "error",
                "message": "两个词条都不能为空",
                "data": {"items": store.list_all()},
            }
        if len(w1) > 10 or len(w2) > 10:
            return {
                "status": "error",
                "message": "每个词条不超过 10 个字",
                "data": {"items": store.list_all()},
            }
        added = store.add(w1, w2)
        if added is None:
            return {
                "status": "error",
                "message": "这对词条（或反向）已存在，或长度无效",
                "data": {"items": store.list_all()},
            }
        return {"status": "ok", "data": {"items": store.list_all(), "added": added}}

    async def page_undercover_words_delete(
        self, payload: Any = None
    ) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = payload or {}
        try:
            item_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return {
                "status": "error",
                "message": "id 必须是整数",
                "data": {"items": store.list_all()},
            }
        ok = store.delete(item_id)
        if not ok:
            return {
                "status": "error",
                "message": "词条不存在或已被删除",
                "data": {"items": store.list_all()},
            }
        return {"status": "ok", "data": {"items": store.list_all()}}

    async def page_undercover_words_batch_import(
        self, payload: Any = None
    ) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        import re
        payload = payload or {}
        text = str(payload.get("text") or "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        added = 0
        skipped: list[str] = []
        for line in lines:
            tokens = [t for t in re.split(r"[\s,，:：\t]+", line) if t]
            if len(tokens) != 2:
                skipped.append(f"格式错误：{line}")
                continue
            w1, w2 = tokens
            if store.add(w1, w2) is not None:
                added += 1
            else:
                skipped.append(f"重复/非法：{w1} vs {w2}")
        items = store.list_all()
        return {
            "status": "ok",
            "data": {
                "added_count": added,
                "skipped_count": len(skipped),
                "skipped": skipped,
                "items": items,
            },
        }

    async def page_undercover_words_llm_generate_batch(
        self, payload: Any = None
    ) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = payload or {}
        try:
            count = max(3, min(int(payload.get("count") or 10), 30))
        except (TypeError, ValueError):
            count = 10
        added = 0
        skipped = 0
        # 用严格 JSON list 让模型一次返回多对
        prompt = (
            f"请返回恰好 {count} 对中文 \"谁是卧底\" 游戏词条，JSON 数组格式："
            f"[{{\"word1\":\"词A\",\"word2\":\"词B\"}}, ...]。"
            "要求：两个词必须是中文，长度 2-10 字，语义相关但有明确差异（例如：可乐/雪碧、牛奶/豆浆、口红/唇釉）；"
            "不要重复；不要输出任何其他文字；不要 markdown 代码块；"
            "词条类型尽量多样化，覆盖食物、日用品、人物、影视、科技、游戏等领域。"
        )
        try:
            text = await asyncio.wait_for(
                self._llm_gen_neutral(prompt),
                timeout=30,
            )
            parsed = extract_json_object(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                pairs_src = parsed["items"]
            elif isinstance(parsed, list):
                pairs_src = parsed
            else:
                # 尝试多种包装
                pairs_src = []
                for key in ("pairs", "words", "result", "data"):
                    if isinstance(parsed, dict) and isinstance(parsed.get(key), list):
                        pairs_src = parsed[key]; break
        except Exception:
            pairs_src = []
        # 遍历入库；窗口去重用已有的机制
        for item in pairs_src:
            if not isinstance(item, dict): continue
            w1 = str(item.get("word1") or item.get("w1") or "").strip()
            w2 = str(item.get("word2") or item.get("w2") or "").strip()
            if not (2 <= len(w1) <= 10 and 2 <= len(w2) <= 10 and w1 != w2):
                skipped += 1; continue
            if self._uc_word_in_window((w1, w2)):
                skipped += 1; continue
            record = store.add(w1, w2)
            if record is None:
                skipped += 1; continue
            self._push_uc_word_window((w1, w2))
            added += 1
        items = store.list_all()
        return {
            "status": "ok" if added > 0 else "error",
            "message": "" if added > 0 else "LLM 未返回有效词条，请稍后再试",
            "data": {
                "added": added,
                "skipped": skipped,
                "items": items,
            },
        }

    async def page_rooms(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": {
                "rooms": [
                    room.admin_snapshot() for room in self.manager.rooms.values()
                ],
                "server": {
                    "running": self.room_server.running,
                    "port": self.room_server.port if self.room_server.running else None,
                    "public_base_url": self.public_base_url,
                    "external_base_url": self.external_base_url,
                    "access_host": self.access_host,
                },
                "tunnel": self.quick_tunnel.status(),
                "xiangqi_engine": self.xiangqi_engine.status(),
                "limits": {
                    "group": self.manager.max_group_rooms,
                    "private": self.manager.max_private_rooms,
                },
                "enabled_games": dict(self.manager.enabled_games),
            },
        }

    async def page_room_action(self) -> dict[str, Any]:
        payload = await request.json(default={}) or {}
        room = self.manager.rooms.get(str(payload.get("room_id") or ""))
        if room is None:
            return {"status": "error", "message": "房间不存在或已经结束", "data": {}}
        action = str(payload.get("action") or "").strip().lower()
        try:
            if action == "assign":
                await self.manager.assign_player(
                    room,
                    int(payload.get("visitor_number") or 0),
                    str(payload.get("player_qq") or ""),
                )
            elif action == "demote":
                await self.manager.remove_player(
                    room, int(payload.get("visitor_number") or 0)
                )
            elif action == "kick":
                await self.manager.kick_visitor(
                    room, int(payload.get("visitor_number") or 0)
                )
            elif action == "pause":
                await self.manager.pause(room)
            elif action == "resume":
                await self.manager.resume(room)
            elif action == "switch_game":
                await self.manager.switch_game(
                    room,
                    self._game_type(payload.get("game_type")),
                    force=self._value_bool(payload.get("confirm_abandon")),
                )
            elif action == "close":
                await self.manager.destroy(room.room_id, "管理员关闭了房间")
            else:
                raise ValueError("不支持的管理操作")
        except (ValueError, RuntimeError, PermissionError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"room_id": room.room_id, "action": action}}

    async def page_xiangqi_install(self) -> dict[str, Any]:
        try:
            status = await self.xiangqi_engine.install_latest()
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"xiangqi_engine": status}}

    async def page_cloudflared_install(self) -> dict[str, Any]:
        try:
            status = await self.quick_tunnel.install_latest()
        except (ValueError, RuntimeError, PermissionError, OSError) as exc:
            logger.warning("[GameCompanion] cloudflared 安装失败: %s", exc)
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"tunnel": status}}

    async def page_game_settings(self) -> dict[str, Any]:
        return {"status": "ok", "data": self._game_settings_snapshot()}

    async def page_game_settings_update(self) -> dict[str, Any]:
        payload = await request.json(default={}) or {}
        try:
            changes = self._validated_game_settings(payload)
            async with self._settings_lock:
                patch = self._game_settings_config_patch(changes)
                await self._persist_game_settings(patch)
                self._apply_game_settings_runtime()
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {
            "status": "ok",
            "message": "游戏配置已保存；新房间和新一局将使用最新设置",
            "data": self._game_settings_snapshot(),
        }

    def _game_settings_snapshot(self) -> dict[str, Any]:
        games: list[dict[str, Any]] = []
        for definition in GAME_CATALOG:
            game_type = str(definition["game_type"])
            fields: list[dict[str, Any]] = []
            for field in definition["fields"]:
                item = {
                    key: value
                    for key, value in field.items()
                    if key != "config_key"
                }
                item["value"] = self._cfg(
                    str(field["config_key"]), field.get("default")
                )
                fields.append(item)
            games.append(
                {
                    "game_type": game_type,
                    "label": definition["label"],
                    "description": definition["description"],
                    "enabled": self._cfg_bool(f"{game_type}.enabled", True),
                    "fields": fields,
                }
            )
        return {
            "version": PLUGIN_VERSION,
            "games": games,
            "notice": "设置立即用于新房间和新一局；正在进行的对局保持原参数。",
        }

    @staticmethod
    def _setting_field_map() -> dict[str, dict[str, dict[str, Any]]]:
        return {
            str(definition["game_type"]): {
                str(field["key"]): field for field in definition["fields"]
            }
            for definition in GAME_CATALOG
        }

    def _validated_game_settings(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("games"), dict):
            raise TypeError("游戏配置格式无效")
        submitted_games = payload["games"]
        field_map = self._setting_field_map()
        unknown_games = set(submitted_games) - set(field_map)
        if unknown_games:
            raise ValueError("包含不支持的游戏配置")
        changes: dict[str, Any] = {}
        for game_type, submitted in submitted_games.items():
            if not isinstance(submitted, dict):
                raise TypeError(f"{self._game_label(game_type)}配置格式无效")
            allowed = {"enabled", *field_map[game_type]}
            if set(submitted) - allowed:
                raise ValueError(f"{self._game_label(game_type)}包含未知配置项")
            if "enabled" in submitted:
                if not isinstance(submitted["enabled"], bool):
                    raise ValueError(f"{self._game_label(game_type)}开关必须是布尔值")
                changes[f"{game_type}.enabled"] = submitted["enabled"]
            for key, value in submitted.items():
                if key == "enabled":
                    continue
                field = field_map[game_type][key]
                changes[str(field["config_key"])] = self._validated_setting_value(
                    field, value
                )
        if not changes:
            raise ValueError("没有需要保存的游戏配置")
        return changes

    @staticmethod
    def _validated_setting_value(field: dict[str, Any], value: Any) -> Any:
        field_type = str(field.get("type") or "")
        label = str(field.get("label") or field.get("key") or "配置")
        if field_type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{label}必须是布尔值")
            return value
        if field_type == "int":
            if isinstance(value, bool):
                raise ValueError(f"{label}必须是整数")
            try:
                normalized = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{label}必须是整数") from None
            if str(value).strip() != str(normalized):
                raise ValueError(f"{label}必须是整数")
            minimum = int(field.get("minimum", normalized))
            maximum = int(field.get("maximum", normalized))
            if not minimum <= normalized <= maximum:
                raise ValueError(f"{label}必须在 {minimum}-{maximum} 之间")
            return normalized
        normalized = str(value or "").strip()
        if field_type == "select":
            allowed = {str(item["value"]) for item in field.get("options", ())}
            if normalized not in allowed:
                raise ValueError(f"{label}选项无效")
            return normalized
        if field_type in ("string", "str"):
            maximum_length = int(field.get("maximum_length", 500))
            if len(normalized) > maximum_length:
                raise ValueError(f"{label}不能超过 {maximum_length} 个字符")
            return normalized
        raise ValueError(f"{label}类型不受支持")

    def _game_settings_config_patch(
        self, changes: dict[str, Any]
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for dotted_key, value in changes.items():
            if dotted_key in self.config:
                patch[dotted_key] = value
                continue
            section, key = dotted_key.split(".", 1)
            if section not in patch:
                current = self.config.get(section, {})
                patch[section] = dict(current) if isinstance(current, dict) else {}
            patch[section][key] = value
        return patch

    async def _persist_game_settings(
        self, patch: dict[str, Any]
    ) -> None:
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            committed = await save_async(patch)
            if committed is False:
                raise RuntimeError("配置同时被其他操作更新，请刷新后重试")
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            await asyncio.to_thread(save, patch)
            return
        self.config.update(patch)

    def _apply_game_settings_runtime(self) -> None:
        self.enabled_games = {
            game_type: self._cfg_bool(f"{game_type}.enabled", True)
            for game_type in SUPPORTED_GAMES
        }
        self.manager.enabled_games.update(self.enabled_games)
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=8
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
        )
        self.turtle_soup_max_players = self._cfg_int(
            "turtle_soup.max_players", 6, minimum=0, maximum=100
        )
        self.multiplayer_turn_timeout = self._cfg_int(
            "multiplayer.turn_timeout_seconds", 60, minimum=0, maximum=3600
        )
        self.swap_request_cooldown = self._cfg_int(
            "multiplayer.swap_request_cooldown_seconds",
            30,
            minimum=0,
            maximum=3600,
        )
        self.swap_request_expiry = self._cfg_int(
            "multiplayer.swap_request_expiry_seconds",
            20,
            minimum=1,
            maximum=600,
        )
        self.draw_guess_vision_provider_id = self._cfg_str(
            "draw_guess.vision_provider_id", "agnes-ai/agnes-image-2.5-flash"
        )
        self.draw_guess_duration_seconds = self._cfg_int(
            "draw_guess.duration_seconds", 120, minimum=10, maximum=600
        )
        self.draw_guess_max_guesses = self._cfg_int(
            "draw_guess.max_guesses", 5, minimum=1, maximum=10
        )
        self.pig_dice_target_score = self._cfg_int(
            "pig_dice.target_score", 50, minimum=20, maximum=200
        )
        self.blackjack_max_players = self._cfg_int(
            "blackjack.max_players", 1, minimum=1, maximum=6
        )
        self.manager.turtle_soup_max_hints = self.turtle_soup_max_hints
        self.manager.turtle_soup_content_level = self.turtle_soup_content_level
        self.manager.turtle_soup_max_players = self.turtle_soup_max_players
        self.manager.multiplayer_turn_timeout = self.multiplayer_turn_timeout
        self.manager.swap_request_cooldown = self.swap_request_cooldown
        self.manager.swap_request_expiry = self.swap_request_expiry
        self.manager.draw_guess_duration_seconds = self.draw_guess_duration_seconds
        self.manager.draw_guess_max_guesses = self.draw_guess_max_guesses
        self.manager.pig_dice_target_score = self.pig_dice_target_score
        self.manager.blackjack_max_players = self.blackjack_max_players
        self.xiangqi_engine.allow_download = self._cfg_bool(
            "xiangqi.allow_engine_download", True
        )
        self.xiangqi_engine.auto_download = self._cfg_bool(
            "xiangqi.auto_download_engine", False
        )

    async def page_tunnel_start(self) -> dict[str, Any]:
        if self._configured_access_base():
            return {"status": "error", "message": "已配置外部访问地址", "data": {}}
        try:
            if not self.room_server.running:
                await self.room_server.start()
            self.quick_tunnel.local_url = self.room_server.local_base_url
            url = await self.quick_tunnel.start(timeout=40)
        except Exception as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {
            "status": "ok",
            "data": {"url": url, "tunnel": self.quick_tunnel.status()},
        }

    async def page_tunnel_stop(self) -> dict[str, Any]:
        if self.manager.rooms:
            return {
                "status": "error",
                "message": "仍有活动房间，不能停止访问通道",
                "data": {},
            }
        await self.quick_tunnel.stop()
        await self.room_server.stop()
        return {"status": "ok", "data": {"tunnel": self.quick_tunnel.status()}}

    # =====================================================================
    # 谁是卧底：LLM 取词 / 身份分发 / 旁白解说
    # =====================================================================

    def _uc_word_in_window(self, pair: tuple[str, str]) -> bool:
        w1, w2 = pair
        for (a, b) in self._undercover_word_window:
            if {w1, w2} == {a, b}:
                return True
        return False

    def _push_uc_word_window(self, pair: tuple[str, str]) -> None:
        if self._uc_word_in_window(pair):
            return
        self._undercover_word_window.append(pair)
        over = len(self._undercover_word_window) - self._undercover_word_window_max
        if over > 0:
            del self._undercover_word_window[:over]

    async def _llm_generate_undercover_words(
        self, room: GameRoom
    ) -> tuple[str, str] | None:
        """尝试用 LLM 生成一对词条。成功则持久化并记录窗口；任何失败返回 None。"""
        store = self.undercover_word_store
        # 已用词统计注入 prompt：让 LLM 明确避开之前选用过的词对（跨局去重）
        used = []
        try:
            used = store.used_pairs(40)
        except Exception:
            used = []
        used_line = ""
        if used:
            used_line = (
                "以下词对已经在之前的对局中使用过，请绝对不要再次选择："
                + "、".join(f"{a}/{b}" for a, b in used)
                + "。"
            )
        prompt = (
            "生成一对 2-4 字的中文常见名词作为谁是卧底词库，"
            "两个词意思接近但不同，比如 \"牛奶/豆浆\"、\"苹果/梨\"。"
            f"{used_line}"
            "只输出严格 JSON，不要加任何解释或额外文字："
            '{ "word1": "平民词", "word2": "卧底词" }'
        )
        try:
            raw = await asyncio.wait_for(
                self._generate_persona_text(room, prompt), timeout=8.0
            )
        except Exception:
            return None
        obj = extract_json_object(str(raw or ""))
        if not isinstance(obj, dict):
            return None
        w1 = str(obj.get("word1") or "").strip()
        w2 = str(obj.get("word2") or "").strip()
        if not w1 or not w2 or w1 == w2:
            return None
        if len(w1) > 10 or len(w2) > 10:
            return None
        pair = (w1, w2)
        if self._uc_word_in_window(pair):
            return None  # 短时间重复则 fallback，保证不重复
        try:
            store.add(w1, w2)  # 去重失败也没关系
            store.mark_used(w1, w2)  # 记录已用，供后续去重
        except Exception:
            pass
        self._push_uc_word_window(pair)
        return pair

    async def _deliver_undercover_identities(
        self, room: GameRoom, game: UndercoverGame
    ) -> None:
        """通过 AstrBot 私聊把身份词条发给每个玩家；失败或未绑定 QQ 时仅依赖 WebUI 身份卡。

        关闭「告知身份」时（game.reveal_identity=False），平民/卧底只发词条不告知身份，
        白板始终正常告知。
        """
        for player in game.players:
            try:
                snap = game.snapshot(player.player_number)
                mine = snap.get("my") or {}
                camp = str(mine.get("camp") or "")
                word = str(mine.get("word") or "")
                qq = str(player.qq or "")
                if not qq:
                    continue
                name = player.display_name or f"{player.player_number}号玩家"
                if camp == "whiteboard":
                    text = (
                        f"【花火陪你玩·谁是卧底】{name} 你是：白板。"
                        "白板没有词条，你的目标是在不暴露的情况下，模仿他人描述坚持到卧底被淘汰。"
                    )
                elif camp:
                    camp_label = {
                        "civilian": "平民",
                        "undercover": "卧底",
                    }.get(camp, camp)
                    text = (
                        f"【花火陪你玩·谁是卧底】{name} 你是：{camp_label}。"
                        f"你的词条是：「{word}」。"
                    )
                else:
                    # 未告知身份：仅发放词条
                    text = f"【花火陪你玩·谁是卧底】{name} 你的词条是：「{word}」。"
                text += "请勿向其他人泄露；更多细节请在 WebUI 身份卡查看。"
                # AstrBot 的私聊接口：如果 fail 直接吞掉
                try:
                    from astrbot.api.message_components import Plain

                    await self.context.send_private_message(
                        qq,
                        Plain(text),
                    )
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            except Exception:
                pass
        # 统一提示：身份已派发
        room.add_message(
            "system",
            "身份词条已分别发送至各位玩家的 WebUI 身份卡；"
            "如开启私聊通道，也会同步到 QQ 私聊。请各自前往查看。",
        )

    async def _undercover_commentary_speech(
        self, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        if time.time() - room.last_commentary_at < self.commentary_cooldown:
            return
        player_number = payload.get("player_number") or "?"
        content = str(payload.get("content") or "")
        if len(content) > 120:
            content = content[:120] + "…"
        prompt = (
            f"谁是卧底第 {payload.get('round') or '?'} 轮："
            f"{player_number} 号玩家刚刚描述了自己的词条。"
            f"请用花火人格，以 40 字以内自然点评，绝对不要指出任何玩家的身份或词条。"
            f"发言摘要：{content}"
        )
        try:
            room.last_commentary_at = time.time()
            await self._comment(room, prompt)
        except Exception:
            pass

    async def _undercover_commentary_vote(
        self, room: GameRoom, payload: dict[str, Any]
    ) -> None:
        if time.time() - room.last_commentary_at < self.commentary_cooldown:
            return
        need_pk = bool(payload.get("need_pk"))
        out_num = payload.get("out_player_number")
        round_num = payload.get("round") or "?"
        if need_pk:
            targets = payload.get("pk_targets") or []
            labels = "、".join(str(x) + "号" for x in targets) or "平票玩家"
            prompt = (
                f"谁是卧底第 {round_num} 轮投票：平票！{labels} 将进入 PK 发言。"
                f"请用花火人格 50 字内渲染气氛，不要暴露任何玩家身份或词条。"
            )
        elif out_num is not None:
            prompt = (
                f"谁是卧底第 {round_num} 轮投票结束：{out_num} 号玩家被大家投出局了。"
                f"请用花火人格 50 字内渲染气氛，不要暴露他是平民/卧底/白板。"
            )
        else:
            prompt = (
                f"谁是卧底第 {round_num} 轮投票结束。"
                f"请用花火人格 40 字内简短说一句悬念。"
            )
        try:
            room.last_commentary_at = time.time()
            await self._comment(room, prompt)
        except Exception:
            pass

    async def _undercover_ai_step_if_needed(self, room: GameRoom) -> None:
        """检查是否轮到 AI 玩家发言 / 还有 AI 玩家没投票，如果是则自动驱动。

        只要房间里存在 AI 玩家席（无论是自动补位还是房主手动添加）就驱动，
        不依赖 undercover_ai_fill_enabled 开关——那个开关只控制“自动补位”行为。
        """
        has_ai = any(
            getattr(seat, "is_ai", False)
            for seat in (getattr(room.multiplayer, "seats", None) or [])
        )
        if not has_ai:
            return
        if not isinstance(room.game, UndercoverGame):
            return
        if room.game.finished:
            return
        game = room.game
        try:
            # 1) 发言阶段：当前是 AI 玩家就自动发言
            if game.phase in ("speech", "pk"):
                exp = game.expected_speaker_number
                seat = next(
                    (s for s in room.multiplayer.seats if s.number == exp), None
                )
                if seat is not None and getattr(seat, "is_ai", False):
                    # 小延时避免栈过深
                    await asyncio.sleep(0.6)
                    await self._undercover_ai_do_speech(room, seat)
                    return
            # 2) 投票阶段：还有 AI 没投就自动投（只要有任何一个没投的 AI 就投一次，递归继续直到全投完）
            if game.phase == "voting":
                all_live_nums = sorted([
                    p.number for p in game.players if not p.is_out
                ])
                voted = set(game.snapshot(None).get("voted_this_round_player_numbers") or [])
                for n in all_live_nums:
                    if n in voted: continue
                    seat = next((s for s in room.multiplayer.seats if s.number == n), None)
                    if seat and getattr(seat, "is_ai", False):
                        await asyncio.sleep(0.5)
                        await self._undercover_ai_do_vote(room, seat)
                        # 递归推进（可能下一个还是 AI）
                        self._spawn(self._undercover_ai_step_if_needed(room))
                        return
        except Exception:
            # AI 出错不影响真人玩家流程
            return

    async def _undercover_turn_timeout(self, room: GameRoom) -> None:
        """发言/投票回合超时后的兜底驱动，防止整局卡死在“该谁发言”。

        若超时的是 AI → 走 AI 自动发言/投票；
        若超时的是真人 → 自动跳过其发言并继续推进，避免挂机卡局。
        """
        game = room.game
        if not isinstance(game, UndercoverGame) or game.finished:
            return
        if game.phase in ("speech", "pk"):
            exp = game.expected_speaker_number
            if exp is None:
                return
            seat = next(
                (s for s in room.multiplayer.seats if s.number == exp), None
            )
            if seat is None:
                return
            if getattr(seat, "is_ai", False):
                await self._undercover_ai_step_if_needed(room)
                return
            # 真人在限定时间内未发言：自动跳过并推进
            try:
                await self.manager.player_undercover_speech(
                    room, seat.visitor_token, "（本回合发言超时，自动跳过）"
                )
            except Exception:
                return
            # 下一位可能是 AI，顺手驱动
            await self._undercover_ai_step_if_needed(room)
            return
        if game.phase == "voting":
            await self._undercover_ai_step_if_needed(room)

    async def _undercover_ai_do_speech(
        self, room: GameRoom, seat
    ) -> None:
        game: UndercoverGame = room.game  # type: ignore[assignment]
        snap = game.snapshot(seat.number)
        # 注意：不能用快照里的 my.camp/my.word——「告知身份」关闭时快照会隐去阵营，
        # AI 必须直接从游戏内部拿到自己的真实阵营与词条，否则会因 camp 为空而沉默卡局。
        ai_player = next(
            (p for p in game.players if int(p.number) == int(seat.number)), None
        )
        camp = str(ai_player.camp or "") if ai_player else ""
        word = str(ai_player.word or "") if ai_player else ""
        # 收集本轮已经说完的玩家发言
        rounds = snap.get("rounds_public") or []
        prev_speeches: list[str] = []
        if rounds:
            for s in rounds[-1].get("speeches") or []:
                prev_speeches.append(f"{s.get('player_number')}号：{s.get('content') or ''}")
        others_context = (
            ("已发言玩家：" + "；".join(prev_speeches[-6:])) if prev_speeches else "本轮你第一个发言"
        )
        # 收集 AI 自己之前几轮说过的话，提示它不要重复（这是“连续两轮一模一样”的根因之一）
        own_history = [
            str(s["content"])
            for s in game.rounds_speeches()
            if int(s.get("player_number", 0)) == int(seat.number)
        ]
        round_no = max(1, int(game.current_round_number or 0))
        anti_repeat = (
            f"这是第{round_no}轮发言，你之前已经说过：{'；'.join(own_history[-3:])}。"
            f"请务必换一种新的说法和套路，绝不要重复或大幅雷同自己之前任何一句，要有差异化。"
            if own_history
            else f"这是第{round_no}轮发言，你还没有说过任何话。"
        )
        if camp == "whiteboard":
            prompt = (
                "你正在玩中文「谁是卧底」，你抽到的是【白板】（没有词条，只能靠猜）。"
                f"{anti_repeat}。{others_context}。"
                "请以「花火」的身份，像真人玩家一样，顺着前一位玩家的语气和话题方向，"
                "用一句 20-45 字的话随口圆一个合群的描述，让人感觉你手里确实有词条。"
                "绝对不要直接说出或试探性说出平民或卧底的词条，也不要问『你们说的是不是XX』；"
                "别说得太空泛（例如『大家都懂的东西』这种一句带过），尽量聊一个具体的生活细节或感受；"
                "可以有语气词、口头禅或小犹豫，越自然越好；"
                "不要输出任何引号或说明；只输出你要说的那一句话。"
            )
        elif camp in ("civilian", "undercover"):
            role = "平民" if camp == "civilian" else "卧底"
            other = "卧底" if camp == "civilian" else "平民"
            prompt = (
                f"你正在玩中文「谁是卧底」，你是{role}，抽到的词条是【{word}】。"
                f"{anti_repeat}。{others_context}。"
                f"请以「花火」的身份，像真人玩家闲聊一样，用一句 20-45 字的话，"
                f"只从【{word}】的某一个具体生活侧面入手（比如你最近用到它的一个场景、对它的一个感受、"
                "一个联想或一句随口吐槽），说得自然、有画面感，像在聊天而不是在做谜题描述。"
                "硬性要求："
                f"话里绝不能出现【{word}】本身、它的同义词或它含有的字眼；"
                "不要用『我抽到/我的词条/它是一款』这类暴露句式；"
                "不要列点罗列特征（颜色、形状、用途、价格逐项说），那一眼就会被{other}锁定；"
                "可以带语气词和日常感，但别机械、别每句都用『嗯嗯』开头；"
                "不要输出任何引号或说明；只输出你要说的那一句话。"
            )
        else:
            return
        # 注入性格人设（出自「火花·调皮(AI)」中的形容词）+ 阶段性措辞 + 长度约束
        m = re.search(r"·([^·()（）]+)\(AI\)", seat.display_name or "")
        persona = f"你的性格是【{m.group(1)}】，用词和口吻要贴合这个性格。" if m else ""
        if round_no <= 1:
            stage_hint = "现在是第1轮，大家还很模糊，尽量笼统一带而过，别展开具体细节。"
        elif round_no <= 3:
            stage_hint = f"现在是第{round_no}轮，可以稍微具体一点点，但仍要合群委婉，别点明词条。"
        else:
            stage_hint = f"现在是第{round_no}轮，可以适当说具体些争取信任，但仍绝不能点明词条。"
        prompt += persona + stage_hint + "整句务必控制在20字以内、最多1个逗号，越简短利落越好。"
        try:
            content = await asyncio.wait_for(
                self._generate_persona_text(room, prompt),
                timeout=15,
            )
            content = str(content or "").strip()
            # 规整为「≤1 逗号、≤20 字」的短句
            content = _sanitize_uc_ai_speech(content)
            if len(content) < 4:
                content = _uc_ai_fallback(camp, round_no)
            # 白板 AI 兜底：一旦模型说出的内容泄露了任一词条（含「刷牙」之于「牙刷」这类），
            # 立即换成安全的兜底文案，避免 AI 因“白板说词直接获胜”而莫名其妙结束整局。
            if camp == "whiteboard" and game.leaks_word(content):
                content = _uc_ai_fallback(camp, round_no)
            await self.manager.player_undercover_speech(
                room, seat.visitor_token, content
            )
        except Exception:
            # 失败时用一句本地兜底文案（按轮次轮换，避免整局一句复读），避免卡住整轮
            fallback = _uc_ai_fallback(camp, round_no)
            try:
                await self.manager.player_undercover_speech(
                    room, seat.visitor_token, fallback
                )
            except Exception:
                pass

    async def _undercover_ai_do_vote(
        self, room: GameRoom, seat
    ) -> None:
        game: UndercoverGame = room.game  # type: ignore[assignment]
        my_snap = game.snapshot(seat.number)
        rounds = my_snap.get("rounds_public") or []
        live_players = my_snap.get("players_public") or []
        live_nums = [int(p["player_number"]) for p in live_players if not p.get("is_out")]
        if not rounds:
            # 没有发言记录，随便投一个非自己的
            targets = [n for n in live_nums if n != seat.number]
            if not targets: return
            await self.manager.player_undercover_vote(room, seat.visitor_token, targets[0])
            return
        last = rounds[-1]
        round_speeches = last.get("speeches") or []
        # 组装提示：把每个存活玩家本轮的发言列出来
        lines = []
        for s in round_speeches:
            pn = int(s.get("player_number") or 0)
            if pn in live_nums and pn != seat.number:
                lines.append(f"{pn}号玩家说：{s.get('content') or ''}")
        if not lines:
            targets = [n for n in live_nums if n != seat.number]
            if targets:
                await self.manager.player_undercover_vote(room, seat.visitor_token, targets[0])
            return
        my_camp = str((my_snap.get("my") or {}).get("camp") or "")
        my_word = str((my_snap.get("my") or {}).get("word") or "")
        # 同上：AI 投票也需用真实阵营/词条，避免「告知身份」关闭时拿到被隐去的空值
        ai_vp = next(
            (p for p in game.players if int(p.number) == int(seat.number)), None
        )
        if ai_vp is not None:
            my_camp = str(ai_vp.camp or "")
            my_word = str(ai_vp.word or "")
        hint = (
            f"你是白板，你不知道任何词条。"
            if my_camp == "whiteboard"
            else (
                f"你是{'平民' if my_camp == 'civilian' else '卧底'}，你的词是【{my_word}】。"
                + ("平民要尽量投出卧底；" if my_camp == "civilian" else "卧底要尽量投掉平民；")
            )
        )
        prompt = (
            f"你正在玩「谁是卧底」。{hint}"
            f"以下是本轮除你之外所有存活玩家的发言：\n"
            + "\n".join(lines[:8])
            + f"\n请判断谁最可疑（最不像自己阵营的人），只返回一个整数（玩家编号 1-{max(live_nums)}），"
            "不要返回任何其他文字或说明。"
        )
        target = None
        try:
            text = await asyncio.wait_for(
                self._generate_persona_text(room, prompt),
                timeout=15,
            )
            m = __import__("re").search(r"\d+", str(text or ""))
            if m:
                n = int(m.group())
                if n in live_nums and n != seat.number:
                    target = n
        except Exception:
            target = None
        if target is None:
            candidates = [n for n in live_nums if n != seat.number]
            target = candidates[0] if candidates else None
        if target is not None:
            try:
                await self.manager.player_undercover_vote(
                    room, seat.visitor_token, int(target)
                )
            except Exception:
                pass

    def _cfg(self, dotted_key: str, default: Any = None) -> Any:
        if dotted_key in self.config:
            return self.config.get(dotted_key, default)
        current: Any = self.config
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return default if current is None else current

    def _cfg_str(self, dotted_key: str, default: str = "") -> str:
        return str(self._cfg(dotted_key, default) or "").strip()

    def _apply_log_level(self) -> None:
        """Apply an optional plugin-only override; inherit leaves AstrBot's level intact."""
        if self.log_level in {"inherit", ""}:
            return
        level = getattr(logging, self.log_level.upper(), None)
        if isinstance(level, int):
            try:
                from astrbot.core.log import LogManager

                plugin_logger = LogManager.get_plugin_logger(PLUGIN_NAME)
            except (ImportError, AttributeError):
                plugin_logger = logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}")
            plugin_logger.setLevel(level)
        else:
            logger.warning(
                "[GameCompanion] 未知日志等级 %s，将跟随 AstrBot 全局设置",
                self.log_level,
            )

    def _cfg_bool(self, dotted_key: str, default: bool) -> bool:
        value = self._cfg(dotted_key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "是", "开启"}
        return bool(value)

    def _cfg_int(
        self, dotted_key: str, default: int, *, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(self._cfg(dotted_key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _cfg_non_negative(self, dotted_key: str, default: int) -> int:
        try:
            return max(0, int(self._cfg(dotted_key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_qq_ids(value: Any) -> set[str]:
        if isinstance(value, list):
            values = value
        else:
            values = re.split(r"[\s,，;；]+", str(value or ""))
        return {str(item).strip() for item in values if str(item).strip().isdigit()}

    @staticmethod
    def _difficulty(value: Any) -> Difficulty:
        normalized = str(value or "normal").strip().lower()
        return normalized if normalized in {"easy", "normal", "hard"} else "normal"  # type: ignore[return-value]

    @staticmethod
    def _turtle_soup_mode(value: Any) -> TurtleSoupMode:
        normalized = str(value or "bot_host").strip().lower()
        aliases = {
            "bot_host": "bot_host",
            "bot-host": "bot_host",
            "bot出题": "bot_host",
            "你出题": "bot_host",
            "player_host": "player_host",
            "player-host": "player_host",
            "玩家出题": "player_host",
            "我出题": "player_host",
            "bot猜": "player_host",
        }
        return aliases.get(normalized, "bot_host")  # type: ignore[return-value]

    def _room_actor_authorized(self, room: GameRoom, actor_qq: str) -> bool:
        if (
            actor_qq in {room.creator_qq, room.player_qq}
            or actor_qq in self.game_admin_ids
        ):
            return True
        return bool(
            room.multiplayer.enabled
            and room.multiplayer.seat_for_qq(actor_qq) is not None
        )

    @staticmethod
    def _game_type(value: Any) -> GameType:
        normalized = str(value or "gomoku").strip().lower()
        aliases = {
            "gomoku": "gomoku",
            "五子棋": "gomoku",
            "xiangqi": "xiangqi",
            "象棋": "xiangqi",
            "中国象棋": "xiangqi",
            "tictactoe": "tictactoe",
            "tic-tac-toe": "tictactoe",
            "tic_tac_toe": "tictactoe",
            "井字棋": "tictactoe",
            "圈叉棋": "tictactoe",
            "turtle_soup": "turtle_soup",
            "turtle-soup": "turtle_soup",
            "海龟汤": "turtle_soup",
            "pig_dice": "pig_dice",
            "pig-dice": "pig_dice",
            "pig": "pig_dice",
            "贪心骰子": "pig_dice",
            "贪心骰": "pig_dice",
            "骰子": "pig_dice",
            "draw_guess": "draw_guess",
            "draw-guess": "draw_guess",
            "你画我猜": "draw_guess",
            "画画猜词": "draw_guess",
            "画图猜词": "draw_guess",
            "blackjack": "blackjack",
            "black-jack": "blackjack",
            "21点": "blackjack",
            "21點": "blackjack",
            "二十一点": "blackjack",
            "黑杰克": "blackjack",
            "undercover": "undercover",
            "under-cover": "undercover",
            "谁是卧底": "undercover",
            "誰是臥底": "undercover",
            "卧底": "undercover",
            "臥底": "undercover",
            "谁是臥底": "undercover",
        }
        if normalized not in aliases:
            raise ValueError("目前只支持五子棋、中国象棋、井字棋、海龟汤、贪心骰子、你画我猜、二十一点和谁是卧底")
        return aliases[normalized]  # type: ignore[return-value]

    @staticmethod
    def _game_label(game_type: GameType) -> str:
        return {
            "gomoku": "五子棋",
            "xiangqi": "中国象棋",
            "tictactoe": "井字棋",
            "turtle_soup": "海龟汤",
            "pig_dice": "贪心骰子",
            "draw_guess": "你画我猜",
            "blackjack": "二十一点",
            "undercover": "谁是卧底",
        }[game_type]

    @staticmethod
    def _value_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "是", "确认"}
        return value is True

    @staticmethod
    def _admin_room_requested(message: str, tool_value: bool = False) -> bool:
        """Keep explicit room mode reliable even if the model omits the tool flag."""
        if tool_value:
            return True
        normalized = re.sub(r"[\s，。！!？?、]", "", str(message or "").lower())
        if any(
            phrase in normalized
            for phrase in (
                "普通房间",
                "普通模式",
                "不要管理员房间",
                "不是管理员房间",
                "非管理员房间",
            )
        ):
            return False
        return any(
            phrase in normalized
            for phrase in (
                "管理员房间",
                "管理员模式",
                "管理房",
                "审核房间",
                "需要我审核玩家",
            )
        )

    @staticmethod
    def _validated_public_url(value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc:
            logger.warning("[GameCompanion] 外部访问地址必须是 HTTPS，当前配置已忽略")
            return ""
        return value.rstrip("/")

    @staticmethod
    def _validated_external_url(value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            logger.warning("[GameCompanion] 外部访问地址必须是 HTTP 或 HTTPS，当前配置已忽略")
            return ""
        return value.rstrip("/")

    @staticmethod
    def _json_error(message: str) -> str:
        return json.dumps({"ok": False, "error": str(message)}, ensure_ascii=False)

    def _spawn(self, operation: Any) -> asyncio.Task:
        task = asyncio.create_task(operation)
        self._background_tasks.add(task)

        def finish(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("[GameCompanion] 后台任务失败: %s", exc)

        task.add_done_callback(finish)
        return task
