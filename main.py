from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import random
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
from .gomoku import WHITE as GOMOKU_WHITE
from .gomoku import Difficulty, GomokuGame
from .models import (
    GameRoom,
    GameType,
    TurtleSoupMode,
    Visitor,
    _qq_avatar_url,
    _visitor_uid,
    soup_reveal_required,
)
from .othello import BLACK as OTHELLO_BLACK
from .othello import WHITE as OTHELLO_WHITE
from .othello import OthelloGame
from .pig_dice import PigDiceGame
from .pikafish import PikafishService
from .qq_bridge import (
    handle_soup_at_message,
    handle_soup_command,
    run_soup_action,
    soup_room_for_session,
    start_soup_room,
)
from .room_manager import (
    BOARD_GAME_CLASSES,
    DUEL_GAMES,
    SUPPORTED_GAMES,
    RoomManager,
    _parse_camp_scales,
)
from .server import GameRoomServer
from .soup_custom import CustomSoupStore, handle_custom_command
from .soup_generated import GeneratedSoupStore
from .soup_library import SoupLibrary
from .tictactoe import NOUGHT as TICTACTOE_NOUGHT
from .tictactoe import TicTacToeGame
from .tictactoe import X as TICTACTOE_X
from .turtle_soup_deck import (
    SoupDeck,
    fetch_online_puzzles,
    normalize_text,
    redact_spoiler,
)
from .trusted_identity import TrustedIdentityStore
from .tunnel import QuickTunnel
from .turtle_soup import (
    VERDICT_LABELS,
    TurtleSoupGame,
    all_fallback_puzzles,
    normalize_content_level,
    puzzle_from_mapping,
    soup_profile,
    soup_solve_policy,
)
from .turtle_soup_ai import (
    JUDGE_RETRY_SUFFIX,
    answer_judge_prompt,
    answer_judgment_usable,
    coverage_reply,
    extract_json_object,
    generation_prompt,
    parse_answer_judgment_detail,
    parse_question_judgment,
    parse_reverse_turn,
    public_judge_history,
    question_judge_prompt,
    question_judgment_usable,
    reverse_public_history,
    reverse_turn_prompt,
    validation_passed,
    validation_prompt,
)
from .undercover import UndercoverGame
from .undercover import _text_similarity as _uc_text_similarity
from .undercover_words import UndercoverWordStore
from .xiangqi import BLACK as XIANGQI_BLACK
from .xiangqi import RED as XIANGQI_RED
from .xiangqi import XiangqiGame
from .weiqi import WeiqiGame

PLUGIN_NAME = "astrbot_plugin_game_companion_huahuo"


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号，全局只保留这一处来源。

    以前这里写死过一个常量，升级时忘了同步，管理台就一直显示旧版本。
    """
    try:
        text = Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'''^version:\s*["']?([0-9][^"'\s#]*)''', text, re.MULTILINE)
    return match.group(1).strip() if match else "0.0.0"


PLUGIN_VERSION = _read_plugin_version()
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"
# 管理台手动开的房间没有 QQ 会话，用这个假 session 占位：
# 版本/人格/解说回退到默认 Provider，主动发消息会被 _send_to_origin 静默吞掉。
ADMIN_CONSOLE_SESSION = "admin:console"

# 棋盘执方的中文写法：象棋红/黑，棋类黑/白
COMMENTARY_SIDE_LABELS = {"red": "红", "black": "黑", "white": "白"}
# 先手或展示顺序：象棋红先，棋类黑先（white 只作为兜底）
COMMENTARY_SIDE_ORDER = ("red", "black", "white")

# 连战汇总的「离线」判定阈值：真人无心跳超过该秒数视为已离线（用户要求放宽到 3~5 分钟）
BOARD_SERIES_OFFLINE_SECONDS = 240


def _commentary_seat_map(room: GameRoom) -> dict[str, str]:
    """执方（red/black/white）→ 坐在这一方的人；花火座位统一叫「花火」。"""
    mapping: dict[str, str] = {}
    state = room.multiplayer
    if not state.enabled:
        return mapping
    for seat in state.seats:
        side = str(seat.side or "")
        if not side:
            # 兼容尚未分配 side 的历史房间：按座位序号推断
            try:
                index = state.seats.index(seat)
            except ValueError:
                continue
            if room.game_type == "xiangqi":
                side = "red" if index % 2 == 0 else "black"
            else:
                side = "black" if index % 2 == 0 else "white"
        mapping[side] = (
            "花火"
            if seat.is_ai
            else ((seat.display_name or "").strip() or f"{seat.number}号位")
        )
    return mapping


def _commentary_duel_room(room: GameRoom) -> bool:
    """两位真人同房的对弈：花火只旁观解说，不执任何一方。

    必须限定棋类：海龟汤/卧底这类多人玩法也有两个以上座位，以前会被误判成
    「你在旁观两位真人下棋」，解说里冒出「执黑的一方」。
    """
    if room.game_type not in DUEL_GAMES:
        return False
    mapping = _commentary_seat_map(room)
    return len(mapping) >= 2 and "花火" not in mapping.values()


# 模型偶尔会把 system 里的人设字段当成台词复述出来，开头就带一行
# 「角色名：花火」「【姓名：花火】」；这里把这层壳剥掉，只留真正要说的话。
_ROLE_PREFIX_RE = re.compile(
    r"""
    ^[\s\*_#>·\-]*              # 行首的 markdown / 列表修饰
    (?:【|\[|（|\()?             # 可选开括号
    \s*
    (?:角色名|角色名称|角色|人物名|姓名|名字|name)\s*[:：]\s*
    [^\s】\]）\)\n\*:：]{0,30}   # 名字本身（不含空白与标点，避免把正文一起吃掉）
    \s*(?:】|\]|）|\))?         # 可选闭括号
    [\s\*_：:—\-]*
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _strip_role_prefix(text: str) -> str:
    """去掉回复开头的「角色名：XXX」标签行，最多剥三层，避免误删正文。"""
    cleaned = text or ""
    for _ in range(3):
        stripped = _ROLE_PREFIX_RE.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned.lstrip("\n ")


def _commentary_sides_text(room: GameRoom, *, actor_side: str = "") -> str:
    """「小明执黑，阿花执白」；actor_side 标注刚走完这一步的一方。"""
    mapping = _commentary_seat_map(room)
    parts: list[str] = []
    for side in COMMENTARY_SIDE_ORDER:
        name = mapping.get(side)
        if not name:
            continue
        label = COMMENTARY_SIDE_LABELS.get(side, side)
        mark = "，刚走完这一步" if side == actor_side else ""
        parts.append(f"{name}执{label}{mark}")
    return "，".join(parts)


def _commentary_side_name(room: GameRoom, side: str) -> str:
    """某一方在这盘棋里的称呼：真人昵称 / 花火 / 「黑方」兜底。"""
    name = _commentary_seat_map(room).get(side)
    if name:
        return name
    return f"{COMMENTARY_SIDE_LABELS.get(side, side)}方"


def _commentary_turn_text(room: GameRoom) -> str:
    """当前轮到谁走；非棋类或已结束返回空串。"""
    game = room.game
    if game is None or getattr(game, "finished", False):
        return "本局已经结束"
    side = ""
    if isinstance(game, GomokuGame):
        side = "black" if int(game.turn) == GOMOKU_BLACK else "white"
    elif isinstance(game, OthelloGame):
        side = "black" if int(game.turn) == OTHELLO_BLACK else "white"
    elif isinstance(game, XiangqiGame):
        side = "red" if game.turn == XIANGQI_RED else "black"
    elif isinstance(game, WeiqiGame):
        side = "black" if int(game.turn) == GOMOKU_BLACK else "white"
    if not side:
        return ""
    return f"现在轮到{_commentary_side_name(room, side)}走"


def _commentary_facts(
    room: GameRoom, payload: dict[str, Any] | None = None
) -> str:
    """拼出「谁执哪方 / 刚才是谁走的 / 现在轮到谁」，供解说拼提示词。"""
    payload = payload or {}
    side = str(payload.get("side") or "")
    pieces: list[str] = []
    sides = _commentary_sides_text(room, actor_side=side)
    if sides:
        pieces.append(sides)
    elif side:
        pieces.append(f"{COMMENTARY_SIDE_LABELS.get(side, side)}方刚走完一步")
    turn = _commentary_turn_text(room)
    if turn:
        pieces.append(turn)
    return "；".join(part for part in pieces if part)

GAME_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "game_type": "gomoku",
        "label": "五子棋",
        "description": "15×15 棋盘，由 Bot 人格决定三档棋力。棋类通用参数在插件配置「多人席位」里调整。",
        # 棋类时间/掉线/让座是全局配置（multiplayer.*），只放在插件配置项里。
        # 以前这里也放了一份，结果四个棋类面板提交同一组 config_key，
        # 保存时后遍历的玩法会覆盖前面的值——改哪个面板都不生效。
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
        "game_type": "weiqi",
        "label": "围棋",
        "description": "9 / 13 / 19 路棋盘，中国规则数子，界面右侧实时显示黑白胜率。",
        "fields": (
            {
                "key": "board_size",
                "config_key": "weiqi.board_size",
                "label": "棋盘路数",
                "type": "select",
                "default": "13",
                "options": (
                    {"value": "9", "label": "9 路"},
                    {"value": "13", "label": "13 路"},
                    {"value": "19", "label": "19 路"},
                ),
                "hint": "9 路最快，19 路最接近正式对局；只对新开局生效。",
            },
            {
                "key": "komi",
                "config_key": "weiqi.komi",
                "label": "黑贴目",
                "type": "select",
                "default": "7.5",
                "options": (
                    {"value": "0.5", "label": "0.5 目"},
                    {"value": "5.5", "label": "5.5 目"},
                    {"value": "6.5", "label": "6.5 目"},
                    {"value": "7.5", "label": "7.5 目"},
                ),
                "hint": "中国规则数子法，贴目越高对白方越有利。",
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
        "game_type": "othello",
        "label": "黑白棋",
        "description": "8×8 棋盘，合法落子会夹住并翻转对方棋子。棋类通用参数在插件配置「多人席位」里调整。",
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
                "default": 0,
                "minimum": 0,
                "maximum": 100,
                "unit": "人",
                "hint": "0 表示不限制人数（默认），群里谁想加入都能坐下。",
            },
            {
                "key": "max_questions",
                "config_key": "turtle_soup.max_questions",
                "label": "每局提问上限",
                "type": "int",
                "default": 15,
                "minimum": 0,
                "maximum": 999,
                "unit": "次",
                "hint": "默认 15 次，0 表示不限制。只限是/否提问，推理随时可以提交。",
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
            {
                "key": "auto_reveal_seconds",
                "config_key": "turtle_soup.auto_reveal_seconds",
                "label": "超时揭晓汤底",
                "type": "int",
                "default": 1800,
                "minimum": 0,
                "maximum": 7200,
                "unit": "秒",
                "hint": "长时间没人提问、推理也没解开时，自动收题并公布汤底，避免一题永远挂着；0 表示关闭（只保留玩家投票揭晓）。",
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
                "hint": "开局身份卡上「确认」按钮按此时长锁定（倒计时结束后才可确认开局），期间可查看身份与词条；设为 0 则直接可确认。",
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
                "hint": "默认 160 秒；设为 0 表示不限时：后端不设超时、不强跳发言玩家，由玩家自行发言推进（前端显示「不限时」）。",
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


# 0.5.12 曾短暂把谁是卧底对局参数写成中文键；若插件已加载过该版本（配置里已有中文键），
# 加载时回迁为英文键，保持与全插件其余配置一致的英文点分键风格。
# 值为 (英文键, schema 默认值)：英文键只有默认值时同样会被中文键的真实值覆盖，避免丢参数。
_UNDERCOVER_CN_TO_EN_KEYS: dict[str, tuple[str, object]] = {
    "undercover.启用谁是卧底": ("undercover.enabled", True),
    "undercover.最大玩家席": ("undercover.max_players", 10),
    "undercover.最低开局人数": ("undercover.min_players", 2),
    "undercover.默认阵营比例": ("undercover.camp_scales_default", "4 1 0"),
    "undercover.允许房主自定义阵营比例": ("undercover.allow_host_customize_camp_scales", True),
    "undercover.匹配等待时长": ("undercover.match_seconds", 180),
    "undercover.发词准备时长": ("undercover.prepare_seconds", 10),
    "undercover.单人次发言时长上限": ("undercover.speaking_seconds", 160),
    "undercover.投票时长上限": ("undercover.voting_seconds", 120),
    "undercover.首轮不投票的最低存活人数": ("undercover.first_round_non_voting", 3),
    "undercover.告知身份": ("undercover.send_identity_in_card", True),
    "undercover.投票结算展示具体投票人": ("undercover.show_voters", False),
    "undercover.发言相似度阈值": ("undercover.similarity", 80),
    "undercover.开启AI玩家自动补位": ("undercover.ai_fill_enabled", True),
    "undercover.AI补位后的最低总人数": ("undercover.ai_fill_min_players", 3),
}


# 服务端「战况通知」海报：Pillow 无彩色 emoji 字体时会把 emoji 画成方框，绘制前统一剔除
_POSTER_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 表情符号 Emoji
    "\u2600-\u27BF"           # 杂项符号 / 装饰符
    "\u2B00-\u2BFF"           # 箭头 / 星形等
    "\uFE0F\u200D"            # 变体选择符 / 零宽连接符
    "]"
)
# 常见中文字体候选（按平台探测，找到即用；找不到时自动播报回退纯文字）
_POSTER_FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("C:/Windows/Fonts/msyhbd.ttc", 0),   # 微软雅黑 Bold
    ("C:/Windows/Fonts/msyh.ttc", 1),     # 微软雅黑（index 1 = Bold）
    ("C:/Windows/Fonts/simhei.ttf", 0),   # 黑体
    ("C:/Windows/Fonts/simsun.ttc", 0),   # 宋体
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
)
_POSTER_FONT_PATH: dict[bool, tuple[str, int]] = {}  # 已探测到的字体路径（bold → (path, index)）
_POSTER_FONT_CACHE: dict[tuple[bool, int], Any] = {}  # (bold, px) → ImageFont

# Pillow 兜底海报的 emoji 字体：单色轮廓渲染（无彩色 emoji 也能出图，避免 tofu 方块）。
# 找不到可用 emoji 字体时，回退为剥离 emoji（保持可读）。
_POSTER_EMOJI_FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/seguiemj.ttf",           # Windows Segoe UI Emoji
    "/System/Library/Fonts/Apple Symbols.ttf",  # macOS 符号字体
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    "/usr/share/fonts/truetype/symbola/Symbola.ttf",
    "/usr/share/fonts/opentype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
)
_POSTER_EMOJI_FONT_PATH: str = ""  # 已探测到的 emoji 字体路径（探测成功后缓存）

# ---- Pillow 海报共用绘制助手（战况海报与排行榜海报共用；Pillow 为可选依赖） ----

_EMOJI_SPLIT_RE = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+)"
)


def _pillow_blend(
    base: tuple[int, int, int],
    fg: tuple[int, int, int],
    alpha: int,
) -> tuple[int, int, int]:
    """手动把前景色按 alpha 混到背景色上，返回不透明 RGB。

    本环境定制版 Pillow 对形状（ellipse/rounded_rectangle/rectangle/line）的
    RGBA 填充不做 alpha 混合（直接存原始色），转 RGB 后变成实色；因此半透明
    背景/卡片/圆章需要预先算好混合色，用不透明值绘制。
    """
    alpha = max(0, min(255, int(alpha)))
    return tuple(
        round(b + (c - b) * alpha / 255) for b, c in zip(base, fg)
    )


def _pillow_font(bold: bool, px: int) -> Any:
    """按 (粗体, 字号) 缓存中文字体；找不到可用字体时抛错，由调用方回退纯文字。"""
    key = (bold, px)
    cached = _POSTER_FONT_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import ImageFont

    resolved = _POSTER_FONT_PATH.get(bold)
    if resolved is None:
        msyh = "C:/Windows/Fonts/msyh.ttc"
        # 微软雅黑 ttc 内含常规/粗体两页：常规 0、粗体 1；非粗体跳过纯粗体字体（msyhbd）
        first = [(msyh, 1 if bold else 0)]
        rest = [
            (candidate, index)
            for candidate, index in _POSTER_FONT_CANDIDATES
            if candidate != msyh and not (not bold and "msyhbd" in candidate)
        ]
        for candidate, index in first + rest:
            try:
                ImageFont.truetype(candidate, px, index=index)
                resolved = (candidate, index)
                _POSTER_FONT_PATH[bold] = resolved
                break
            except OSError:
                continue
        if resolved is None:
            raise RuntimeError("未找到可用的中文字体")
    f = ImageFont.truetype(resolved[0], px, index=resolved[1])
    _POSTER_FONT_CACHE[key] = f
    return f


def _pillow_emoji_font(px: int) -> Any:
    """按字号缓存 emoji 字体；找不到可用字体返回 None（调用方剥离 emoji）。"""
    key = ("emoji", px)
    cached = _POSTER_FONT_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import ImageFont

    path = _POSTER_EMOJI_FONT_PATH
    if not path:
        for candidate in _POSTER_EMOJI_FONT_CANDIDATES:
            try:
                ImageFont.truetype(candidate, px)
                path = candidate
                globals()["_POSTER_EMOJI_FONT_PATH"] = candidate
                break
            except OSError:
                continue
        if not path:
            return None
    f = ImageFont.truetype(path, px)
    _POSTER_FONT_CACHE[key] = f
    return f


def _pillow_segments(text: str, efont: Any) -> list[tuple[str, Any]]:
    """把文本切成 (片段, 字体) 列表：emoji 片段用 emoji 字体，其余用常规字体。

    没有可用 emoji 字体时直接剥离 emoji，避免渲染成 tofu 方块。
    """
    if efont is None:
        text = _POSTER_EMOJI_RE.sub("", str(text or ""))
        return [(text, None)] if text else []
    out: list[tuple[str, Any]] = []
    for seg in _EMOJI_SPLIT_RE.split(str(text or "")):
        if not seg:
            continue
        out.append((seg, efont) if _EMOJI_SPLIT_RE.fullmatch(seg) else (seg, None))
    return out


def _pillow_font_length(d: Any, text: str, f: Any, efont: Any = None) -> float:
    return sum(
        d.textlength(seg, font=seg_font or f)
        for seg, seg_font in _pillow_segments(text, efont)
    )


def _pillow_truncate(d: Any, text: str, max_w: int, f: Any, efont: Any = None) -> str:
    text = str(text or "")
    if _pillow_font_length(d, text, f, efont) <= max_w:
        return text
    for index in range(len(text) - 1, 0, -1):
        if _pillow_font_length(d, text[:index] + "…", f, efont) <= max_w:
            return text[:index] + "…"
    return text[:1]


def _pillow_wrap(
    d: Any, text: str, max_w: int, f: Any, max_lines: int, efont: Any = None
) -> list[str]:
    out: list[str] = []
    # 先按显式换行切段（战报里「每个玩家一行」就靠它），再对每段按宽度折行
    for segment in str(text or "").split("\n"):
        cur = ""
        for ch in segment:
            nxt = cur + ch
            if cur and _pillow_font_length(d, nxt, f, efont) > max_w:
                out.append(cur)
                cur = ch
            else:
                cur = nxt
        if cur:
            out.append(cur)
        elif segment == "":
            # 连续换行导致的空段也保留成空行，别把相邻两行黏在一起
            out.append("")
    if len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = (out[-1] or "")[:-1] + "…"
    return out or [""]


def _pillow_draw_text(
    d: Any,
    x: int,
    y: int,
    text: str,
    f: Any,
    fill: Any,
    *,
    anchor: str = "ls",
    efont: Any = None,
    stroke_width: int = 0,
    stroke_fill: Any = None,
) -> None:
    """混合字体绘制：anchor 支持 ls/rs/lm/rm/mm。

    垂直对齐交给 Pillow 原生锚点（lm/ls），不手算基线偏移——
    中文与 emoji 字体的度量不同，手算会导致文字下移出框。
    """
    segs = _pillow_segments(text, efont)
    if not segs:
        return
    total = sum(d.textlength(seg, font=seg_font or f) for seg, seg_font in segs)
    x0 = x
    draw_anchor = "ls"  # 基线左对齐（默认）
    if anchor == "rs":
        x0 = x - total
    elif anchor == "rm":
        x0 = x - total
        draw_anchor = "lm"
    elif anchor == "mm":
        x0 = x - total / 2
        draw_anchor = "lm"
    elif anchor == "lm":
        draw_anchor = "lm"
    for seg, seg_font in segs:
        d.text(
            (x0, y), seg, font=seg_font or f, fill=fill, anchor=draw_anchor,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )
        x0 += d.textlength(seg, font=seg_font or f)


def _pillow_draw_avatar(
    img: Any,
    d: Any,
    raw: bytes,
    initial: str,
    color: str,
    cx: float,
    cy: float,
    dia: int,
    font: Any,
    efont: Any = None,
) -> None:
    """在 (cx, cy) 画一个直径 dia 的圆形头像；没有 QQ 头像时画首字圆牌。

    Pillow 的 paste 需要原图尺寸与蒙版一致，先 resize 再贴；解码失败
    按「没有头像」处理，不能让一张坏图毁掉整张海报。
    """
    x0 = int(cx - dia / 2)
    y0 = int(cy - dia / 2)
    if raw:
        try:
            from io import BytesIO

            from PIL import Image, ImageDraw

            avatar = Image.open(BytesIO(raw)).convert("RGB").resize((dia, dia))
            mask = Image.new("L", (dia, dia), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, dia, dia), fill=255)
            img.paste(avatar, (x0, y0), mask)
            return
        except Exception:
            pass
    rgb = tuple(int(color.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4))
    d.ellipse([x0, y0, x0 + dia, y0 + dia], fill=rgb + (255,))
    _pillow_draw_text(
        d, cx, cy, initial, font, (255, 245, 230, 255), anchor="mm", efont=efont
    )

# 服务端「战况通知」海报的 HTML/Jinja2 模板（AstrBot t2i 渲染，样式与前端战况大字报一致）。
# 由 t2i 服务端做 Jinja2 渲染，支持循环/条件；浏览器渲染天然支持 emoji/渐变/描边，效果优于 Pillow。
_UNDERCOVER_POSTER_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  html, body { margin: 0; padding: 0; }
  .poster {
    min-width: 720px; min-height: 720px; box-sizing: border-box;
    padding: 34px 46px 30px; display: flex; flex-direction: column; position: relative;
    background: linear-gradient(135deg, #5a1c16 0%, #200907 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; color: #fff;
  }
  .poster::before {
    content: ''; position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(45deg, rgba(255,255,255,.05) 0 2px, transparent 2px 36px);
  }
  .bar { position: absolute; left: 0; right: 0; bottom: 0; height: 10px; background: {{ camp_color }}; }
  .head { display: flex; justify-content: space-between; align-items: center; position: relative; }
  .head .t { font-size: 22px; font-weight: 800; color: #ffd9a0; }
  .head .r { font-size: 16px; color: rgba(255,255,255,.6); max-width: 340px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .big { margin-top: 76px; text-align: center; font-size: 88px; font-weight: 900; line-height: 1.1;
    color: {{ camp_color }};
    text-shadow: 5px 0 1px #fff, 5px 0 1px #fff, 0 5px 1px #fff, 0 5px 1px #fff,
                 0 6px 24px rgba(0,0,0,.3);
    position: relative; }
  .winners { margin-top: 44px; text-align: center; font-size: 26px; font-weight: 700;
    color: #ffe3b0; line-height: 40px; position: relative; }
  .pills { margin-top: 44px; display: flex; justify-content: center; gap: 20px; position: relative; }
  .pill { height: 46px; line-height: 46px; padding: 0 17px; border-radius: 23px; font-size: 20px; font-weight: 700; white-space: nowrap; }
  .pill.c { background: rgba(125,255,176,.16); color: #b6ffd6; }
  .pill.u { background: rgba(221,169,255,.16); color: #ecc9ff; }
  .losers { margin-top: auto; font-size: 17px; color: rgba(255,255,255,.55); line-height: 26px; position: relative; }
  .wm { margin-top: 10px; text-align: right; font-size: 14px; color: rgba(255,255,255,.4); position: relative; }
</style>
</head>
<body>
<div class="poster">
  <div class="head"><span class="t">📢 谁是卧底 · 战况通知</span><span class="r">{{ title }}</span></div>
  <div class="big">{{ camp_cn }} 获胜</div>
  {% if winners %}<div class="winners">🎉 {% for w in winners %}{{ w }}{% if not loop.last %}<br>{% endif %}{% endfor %}</div>{% endif %}
  {% if cw or uw %}
  <div class="pills">{% if cw %}<span class="pill c">🛡️ 平民「{{ cw }}」</span>{% endif %}{% if uw %}<span class="pill u">🕵️ 卧底「{{ uw }}」</span>{% endif %}</div>
  {% endif %}
  {% if losers %}<div class="losers">💔 败方：{% for l in losers %}{{ l }}{% if not loop.last %}<br>{% endif %}{% endfor %}</div>{% endif %}
  <div class="wm">由 花火 监督生成</div>
  <div class="bar"></div>
</div>
</body>
</html>"""

# 服务端「棋类战报」海报的 HTML/Jinja2 模板（深绿对弈卡风格：图标头 + 大字结果 +
# 副信息 + 内嵌终局棋盘 SVG + 底部脚标）。board_svg 由 _board_svg 预生成，标记 safe。
_BOARD_POSTER_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  html, body { margin: 0; padding: 0; }
  .poster {
    min-width: 720px; min-height: 720px; box-sizing: border-box;
    padding: 32px 46px 28px; display: flex; flex-direction: column; position: relative;
    background: linear-gradient(135deg, #1e4334 0%, #0d1f17 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; color: #fff;
  }
  .poster::before {
    content: ''; position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(45deg, rgba(255,255,255,.04) 0 2px, transparent 2px 36px);
  }
  .bar { position: absolute; left: 0; right: 0; bottom: 0; height: 10px; background: {{ accent }}; }
  .head { display: flex; justify-content: space-between; align-items: center; position: relative; }
  .head .t { font-size: 22px; font-weight: 800; color: #ffe0a6; }
  .head .r { font-size: 30px; }
  .big { margin-top: 22px; text-align: center; font-size: 52px; font-weight: 900; line-height: 1.15;
    color: {{ accent }};
    text-shadow: 3px 0 1px #fff, -1px 0 1px #fff, 0 3px 1px #fff, 0 -1px 1px #fff,
                 0 4px 18px rgba(0,0,0,.3);
    position: relative; overflow-wrap: anywhere; word-break: break-all; }
  .sub { margin-top: 8px; text-align: center; font-size: 18px; font-weight: 600;
    color: rgba(255,255,255,.8); position: relative; overflow-wrap: anywhere; word-break: break-all; }
  .sub2 { margin-top: 5px; text-align: center; font-size: 16px; font-weight: 500;
    color: rgba(255,255,255,.58); position: relative; overflow-wrap: anywhere; word-break: break-all; }
  .boardwrap { margin-top: 30px; display: flex; justify-content: center; position: relative; }
  .boardwrap svg { width: 410px; height: 410px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
  .wm { margin-top: auto; text-align: right; font-size: 14px; color: rgba(255,255,255,.4); position: relative; }
</style>
</head>
<body>
<div class="poster">
  <div class="head"><span class="t">{{ icon }} {{ label }} · 战报</span><span class="r">{{ badge }}</span></div>
  <div class="big">{{ title }}</div>
  {% if sub %}<div class="sub">{{ sub }}</div>{% endif %}
  {% if sub2 %}<div class="sub2">{{ sub2 }}</div>{% endif %}
  <div class="boardwrap">{{ board_svg | safe }}</div>
  <div class="wm">由 花火 生成</div>
  <div class="bar"></div>
</div>
</body>
</html>"""

# 服务端「海龟汤问答速报」卡片模板（t2i 渲染）：网页里的提问攒成一张图发群。
# 比纯文字多一个好处——群里看到的人会想点进房间，所以这里刻意做得像一张「小报」。
_SOUP_SYNC_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  @page { margin: 0; }
  html, body { margin: 0; padding: 0; background: #1b100b; }
  .wrap { display: flex; flex-direction: column; gap: 5px; padding: 5px; }
  .strip { display: flex; align-items: center; gap: 9px; box-sizing: border-box;
    padding: 8px 11px; border-radius: 9px; background: #2a1c12;
    border: 1px solid rgba(255,215,154,.16); }
  .avatar { flex: 0 0 auto; width: 30px; height: 30px; border-radius: 50%; overflow: hidden;
    background: rgba(232,163,61,.22); color: #ffd79a; font-size: 12px; font-weight: 800;
    text-align: center; line-height: 30px; }
  .avatar img { width: 30px; height: 30px; object-fit: cover; }
  .body { flex: 1 1 auto; min-width: 0; }
  .meta { font-size: 11.5px; color: rgba(246,231,211,.55); margin-bottom: 1px; }
  .text { font-size: 15px; line-height: 1.4; color: #fdf3e6; word-break: break-word; }
  .chip { flex: 0 0 auto; max-width: 130px; padding: 3px 10px; border-radius: 999px;
    font-size: 12.5px; font-weight: 700; line-height: 1.3; word-break: break-word; }
  .chip.yes { background: rgba(125,220,154,.2); color: #a8f0c0; }
  .chip.no { background: rgba(255,255,255,.1); color: rgba(246,231,211,.8); }
  .chip.near { background: rgba(232,163,61,.22); color: #ffd79a; }
  .chip.info { background: rgba(150,190,255,.18); color: #bcd6ff; }
</style>
</head>
<body>
<div class="wrap">
  {% for item in items %}
  <div class="strip">
    <div class="avatar">{% if item.avatar %}<img src="{{ item.avatar }}" alt="">{% else %}{{ item.number }}{% endif %}</div>
    <div class="body">
      <div class="meta">{{ item.source }} · {{ item.number }} · {{ item.kind }}</div>
      <div class="text">{{ item.text }}</div>
    </div>
    <div class="chip {{ item.tone }}">{{ item.result }}</div>
  </div>
  {% endfor %}
</div>
</body>
</html>"""

# 服务端「海龟汤开局卡」模板：题目一出好就发到群里，汤面、难度、次数和玩法都在
# 一张图上。群里以前只收到一句「房间链接」，看不到题面，得点进网页才知道在玩什么。
_SOUP_OPEN_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  @page { margin: 0; }
  html, body { margin: 0; padding: 0; background: #140e09; }
  .card {
    width: 720px; box-sizing: border-box; padding: 30px 42px 26px;
    background: linear-gradient(160deg, #2b1d13 0%, #140e09 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif;
    color: #fff; position: relative; overflow: hidden;
  }
  .glow { position: absolute; top: -80px; left: -40px; width: 260px; height: 260px;
    background: radial-gradient(circle, rgba(255,209,102,.22), transparent 70%); }
  .head { display: flex; align-items: center; gap: 12px; position: relative;
    padding-bottom: 12px; border-bottom: 3px solid #ffd166; }
  .head .icon { font-size: 30px; }
  .head .title { font-size: 25px; font-weight: 900; color: #ffd79a; }
  .head .badge { margin-left: auto; padding: 4px 14px; border-radius: 999px;
    background: rgba(255,209,102,.16); color: #ffd79a; font-size: 15px; font-weight: 700; }
  .name { margin-top: 22px; font-size: 17px; color: rgba(246,231,211,.62); position: relative; }
  .surface { margin-top: 10px; font-size: 25px; line-height: 1.62; font-weight: 700;
    color: #fff8ec; position: relative; word-break: break-word; }
  .meta { margin-top: 22px; display: flex; gap: 10px; position: relative; flex-wrap: wrap; }
  .meta span { padding: 7px 14px; border-radius: 10px; font-size: 15px;
    background: rgba(255,255,255,.06); border: 1px solid rgba(255,209,102,.2);
    color: #e8d6b8; }
  .meta b { color: #ffd79a; font-weight: 800; }
  .how { margin-top: 22px; padding: 14px 16px; border-radius: 12px; position: relative;
    background: rgba(255,255,255,.05); border: 1px dashed rgba(255,209,102,.32);
    font-size: 15px; line-height: 1.7; color: #e2d0b4; }
  .how div + div { margin-top: 4px; }
  .how b { color: #ffd79a; }
  .foot { margin-top: 20px; display: flex; justify-content: space-between;
    font-size: 13px; color: rgba(246,231,211,.4); position: relative; }
</style>
</head>
<body>
<div class="card">
  <div class="glow"></div>
  <div class="head">
    <span class="icon">🥣</span>
    <span class="title">海龟汤 · 开局</span>
    <span class="badge">{{ difficulty }}</span>
  </div>
  {% if name %}<div class="name">《{{ name }}》{% if source %} · 题目来源：{{ source }}{% endif %}</div>{% endif %}
  <div class="surface">{{ surface }}</div>
  <div class="meta">
    <span>提问 <b>{{ questions }}</b></span>
    <span>提示 <b>{{ hints }}</b></span>
    <span>推理 <b>{{ answers }}</b></span>
    <span>玩家 <b>{{ players }}</b></span>
  </div>
  <div class="how">
    <div>💬 群里 <b>@花火 直接问一句</b> 就是提问，比如「@花火 和灯塔有关吗」</div>
    <div>💡 要提示发 <b>汤 提示</b>；交推理发 <b>汤 推理 你的推断</b>；看进度发 <b>汤 状态</b></div>
    <div>🖥 网页房间能看完整时间线，更适合多人一起问</div>
  </div>
  <div class="foot"><span>花火 · 海龟汤</span><span>汤底只有揭晓后才公开</span></div>
</div>
</body>
</html>"""
# 服务端「海龟汤战报」海报的 HTML/Jinja2 模板（深夜汤屋的暖棕渐变，与前端 canvas 海报
# 同一套视觉：汤面 → 统计 → 「他们问过」卡片 → 结果条）。刻意不含汤底。
_SOUP_POSTER_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  html, body { margin: 0; padding: 0; background: #140e09; }
  .poster {
    width: 720px; min-height: 720px; box-sizing: border-box;
    padding: 30px 46px 26px; display: flex; flex-direction: column; position: relative;
    background: linear-gradient(160deg, #2b1d13 0%, #140e09 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; color: #fff;
  }
  .poster::before {
    content: ''; position: absolute; inset: 0; pointer-events: none;
    background: radial-gradient(70% 30% at 50% 0%, rgba(255,209,102,.18), transparent 70%);
  }
  .bar { position: absolute; left: 0; right: 0; bottom: 0; height: 10px; background: #ffd166; }
  .head {
    display: flex; justify-content: space-between; align-items: baseline;
    position: relative; padding-bottom: 12px; border-bottom: 4px solid #ffd166;
  }
  .head .t { font-size: 20px; font-weight: 700; color: #e5c99a; }
  .head .r { font-size: 14px; color: rgba(229,201,154,.7); }
  .title {
    margin-top: 38px; font-size: 40px; font-weight: 900; line-height: 1.22; color: #fff;
    position: relative; overflow-wrap: anywhere; word-break: break-all;
  }
  .surface {
    margin-top: 18px; font-size: 21px; line-height: 1.55; color: #f0e4d2;
    position: relative; overflow-wrap: anywhere; word-break: break-all;
  }
  .stats { margin-top: 14px; font-size: 16px; color: #c0a887; position: relative; }
  .card {
    margin-top: 18px; padding: 14px 18px; border-radius: 12px; position: relative;
    background: rgba(255,255,255,.05); border: 1px solid rgba(255,209,102,.22);
  }
  .card .ct { font-size: 15px; color: #e5c99a; }
  .card .qa {
    margin-top: 8px; font-size: 17px; line-height: 1.5; color: #dccdb6;
    overflow-wrap: anywhere; word-break: break-all;
  }
  .card .qa div + div { margin-top: 4px; }
  .pcard {
    margin-top: 18px; padding: 14px 18px 16px; border-radius: 12px; position: relative;
    background: rgba(255,255,255,.05); border: 1px solid rgba(255,209,102,.22);
  }
  .pcard .ptitle { font-size: 15px; color: #e5c99a; }
  .plist { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px 14px; justify-content: center; }
  .pitem {
    width: 84px; display: flex; flex-direction: column; align-items: center;
  }
  .pav {
    width: 54px; height: 54px; border-radius: 50%; object-fit: cover;
    display: block; background: rgba(255,255,255,.08);
  }
  .pinit {
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; font-weight: 800; color: #fff5e6;
  }
  .pname {
    margin-top: 6px; max-width: 84px; font-size: 13px; color: #dccdb6;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .result { margin-top: 18px; padding: 13px 18px; border-radius: 12px; position: relative; }
  .result.solved { background: rgba(255,209,102,.16); }
  .result.failed { background: rgba(255,255,255,.06); }
  .result .rt { font-size: 22px; font-weight: 800; color: #ffd166; }
  .result.failed .rt { color: #c9bda8; }
  .result .rs { margin-top: 4px; font-size: 13px; color: rgba(240,228,210,.55); }
  .foot { margin-top: auto; padding-top: 22px; text-align: center; position: relative; }
  .foot .f1 { font-size: 18px; font-weight: 800; color: #ffd166; }
  .foot .f2 { margin-top: 6px; font-size: 13px; color: rgba(229,201,154,.55); }
</style>
</head>
<body>
<div class="poster">
  <div class="head"><span class="t">🥣 海龟汤 · 单局战报</span><span class="r">{{ date }}</span></div>
  <div class="title">{{ title }}</div>
  {% if surface %}<div class="surface">{{ surface }}</div>{% endif %}
  <div class="stats">{{ stats }}</div>
  {% if questions %}
  <div class="card">
    <div class="ct">他们问过</div>
    <div class="qa">{% for q in questions %}<div>{{ q }}</div>{% endfor %}</div>
  </div>
  {% endif %}
  {% if players %}
  <div class="pcard">
    <div class="ptitle">本局玩家</div>
    <div class="plist">
      {% for p in players %}
      <div class="pitem">
        {% if p.avatar %}<img class="pav" src="{{ p.avatar }}" alt="">{% else %}<span class="pav pinit" style="background:{{ p.color }}">{{ p.initial }}</span>{% endif %}
        <span class="pname">{{ p.name }}</span>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  <div class="result {{ 'solved' if solved else 'failed' }}">
    <div class="rt">{{ result_text }}</div>
    <div class="rs">{{ result_sub }}</div>
  </div>
  <div class="foot">
    <div class="f1">花火陪你玩 · 想知道答案，就自己来试一局</div>
    <div class="f2">{{ date }}</div>
  </div>
  <div class="bar"></div>
</div>
</body>
</html>"""

# 服务端「战绩日报/周报」排行榜海报的 HTML/Jinja2 模板（深蓝榜单卡风格：图标头、
# 金色圆章奖牌 + 名字 + 进度条 + 胜场，前三名金色高亮）。
_UNDERCOVER_REPORT_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  html, body { margin: 0; padding: 0; }
  .poster {
    min-width: 720px; min-height: 560px; box-sizing: border-box;
    padding: 40px 44px 30px; position: relative; overflow: hidden;
    background: linear-gradient(160deg, #1c2b4e 0%, #0c1526 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; color: #fff;
  }
  .poster::before { content: ''; position: absolute; inset: 0; pointer-events: none;
    background: radial-gradient(120% 80% at 85% -10%, rgba(90,130,220,.28), transparent 60%),
                radial-gradient(90% 60% at -10% 110%, rgba(255,190,90,.12), transparent 55%); }
  .poster::after { content: ''; position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(135deg, rgba(255,255,255,.03) 0 2px, transparent 2px 40px); }
  .header { display: flex; align-items: center; gap: 16px; position: relative; }
  .icon { width: 54px; height: 54px; border-radius: 14px; flex: none; display: flex; align-items: center;
    justify-content: center; font-size: 30px; background: rgba(255,215,130,.16); border: 1px solid rgba(255,215,130,.35); }
  .titles { flex: 1; min-width: 0; }
  .title { font-size: 26px; font-weight: 800; color: #ffe096; letter-spacing: 1px; }
  .sub { margin-top: 4px; font-size: 13px; color: rgba(255,255,255,.5); }
  .badge { flex: none; padding: 6px 18px; border-radius: 999px; font-size: 15px; font-weight: 700;
    color: #3a2a08; background: linear-gradient(135deg, #ffd782, #ffb34d); box-shadow: 0 4px 14px rgba(255,180,80,.35); }
  .rule { margin-top: 26px; height: 1px; position: relative;
    background: linear-gradient(90deg, rgba(255,255,255,.02), rgba(255,255,255,.18), rgba(255,255,255,.02)); }
  .rows { margin-top: 20px; display: flex; flex-direction: column; gap: 10px; position: relative; }
  .row { height: 56px; border-radius: 14px; padding: 0 16px; display: flex; align-items: center; gap: 14px;
    background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.05); }
  .row.top { background: linear-gradient(90deg, rgba(255,215,130,.12), rgba(255,215,130,.03));
    border-color: rgba(255,215,130,.26); }
  .rank { width: 40px; height: 40px; border-radius: 50%; flex: none; display: flex; align-items: center;
    justify-content: center; font-size: 20px; font-weight: 700; color: rgba(255,255,255,.75); background: rgba(255,255,255,.08); }
  .rank.medal { background: radial-gradient(circle at 35% 30%, #fff7e0, #ffd782); font-size: 22px;
    box-shadow: 0 3px 10px rgba(255,200,110,.4); }
  .name { flex: 1; min-width: 0; font-size: 20px; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-wrap { flex: none; width: 120px; height: 8px; border-radius: 999px; background: rgba(0,0,0,.22); overflow: hidden; }
  .bar { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #f5a623, #e8912d); }
  .row:not(.top) .bar { background: rgba(255,255,255,.4); }
  .wins { flex: none; font-size: 19px; font-weight: 700; color: #ffe096; }
  .wins small { font-size: 13px; font-weight: 500; color: rgba(255,255,255,.55); }
  .empty { margin-top: 36px; text-align: center; font-size: 20px; color: rgba(255,255,255,.55); position: relative; }
  .footer { margin-top: 26px; text-align: center; font-size: 13px; color: rgba(255,255,255,.45); position: relative; }
  .accent { position: absolute; left: 0; right: 0; bottom: 0; height: 6px;
    background: linear-gradient(90deg, #ffd782, #ff9d5c, #ffd782); }
</style>
</head>
<body>
<div class="poster">
  <div class="header">
    <div class="icon">📊</div>
    <div class="titles">
      <div class="title">谁是卧底 · 战绩报告</div>
      <div class="sub">跨房间全局战绩榜 · 前 {{ rows|length }} 名</div>
    </div>
    <div class="badge">{{ subtitle }}</div>
  </div>
  <div class="rule"></div>
  {% if rows %}
  <div class="rows">
  {% for row in rows %}
    <div class="row {{ 'top' if loop.index0 < 3 else '' }}">
      <div class="rank {{ 'medal' if loop.index0 < 3 else '' }}">{{ row.rank }}</div>
      <div class="name">{{ row.name }}</div>
      <div class="bar-wrap"><div class="bar" style="width: {{ row.pct }}%"></div></div>
      <div class="wins">{{ row.wins }}<small> 胜</small></div>
    </div>
  {% endfor %}
  </div>
  {% else %}
  <div class="empty">当前暂无卧底战绩记录。</div>
  {% endif %}
  <div class="footer">数据来自跨房间全局战绩榜 · 由 花火 监督生成</div>
  <div class="accent"></div>
</div>
</body>
</html>"""


# 服务端「棋类战绩日报/周报」排行榜海报的 HTML/Jinja2 模板（深蓝榜单卡风格，与卧底
# 日报同族，但按四种棋类分区，每区展示该棋类前 3 名：图标头 + 奖牌 + 名字 + 胜场/胜率）。
_BOARD_REPORT_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=720">
<style>
  html, body { margin: 0; padding: 0; }
  .poster {
    min-width: 720px; min-height: 560px; box-sizing: border-box;
    padding: 40px 44px 30px; position: relative; overflow: hidden;
    background: linear-gradient(160deg, #1c2b4e 0%, #0c1526 100%);
    font-family: 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif; color: #fff;
  }
  .poster::before { content: ''; position: absolute; inset: 0; pointer-events: none;
    background: radial-gradient(120% 80% at 85% -10%, rgba(90,130,220,.28), transparent 60%),
                radial-gradient(90% 60% at -10% 110%, rgba(255,190,90,.12), transparent 55%); }
  .poster::after { content: ''; position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(135deg, rgba(255,255,255,.03) 0 2px, transparent 2px 40px); }
  .header { display: flex; align-items: center; gap: 16px; position: relative; }
  .icon { width: 54px; height: 54px; border-radius: 14px; flex: none; display: flex; align-items: center;
    justify-content: center; font-size: 30px; background: rgba(255,215,130,.16); border: 1px solid rgba(255,215,130,.35); }
  .titles { flex: 1; min-width: 0; }
  .title { font-size: 26px; font-weight: 800; color: #ffe096; letter-spacing: 1px; }
  .sub { margin-top: 4px; font-size: 13px; color: rgba(255,255,255,.5); }
  .badge { flex: none; padding: 6px 18px; border-radius: 999px; font-size: 15px; font-weight: 700;
    color: #3a2a08; background: linear-gradient(135deg, #ffd782, #ffb34d); box-shadow: 0 4px 14px rgba(255,180,80,.35); }
  .rule { margin-top: 26px; height: 1px; position: relative;
    background: linear-gradient(90deg, rgba(255,255,255,.02), rgba(255,255,255,.18), rgba(255,255,255,.02)); }
  .cards { margin-top: 20px; display: flex; flex-direction: column; gap: 14px; position: relative; }
  .card { border-radius: 16px; padding: 14px 18px 16px; background: rgba(255,255,255,.045);
    border: 1px solid rgba(255,255,255,.06); }
  .card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .card-head .cicon { font-size: 20px; line-height: 1; }
  .card-head .clabel { font-size: 18px; font-weight: 800; color: #ffe096; }
  .row { display: flex; align-items: center; gap: 12px; height: 36px; }
  .rank { width: 26px; height: 26px; border-radius: 50%; flex: none; display: flex; align-items: center;
    justify-content: center; font-size: 15px; font-weight: 700; color: rgba(255,255,255,.75); background: rgba(255,255,255,.08); }
  .rank.medal { background: radial-gradient(circle at 35% 30%, #fff7e0, #ffd782); color: #5b3a08; font-size: 16px; }
  .name { flex: 1; min-width: 0; font-size: 18px; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat { flex: none; font-size: 15px; font-weight: 700; color: #ffe096; }
  .empty { margin-top: 36px; text-align: center; font-size: 20px; color: rgba(255,255,255,.55); position: relative; }
  .footer { margin-top: 26px; text-align: center; font-size: 13px; color: rgba(255,255,255,.45); position: relative; }
  .accent { position: absolute; left: 0; right: 0; bottom: 0; height: 6px;
    background: linear-gradient(90deg, #ffd782, #ff9d5c, #ffd782); }
</style>
</head>
<body>
<div class="poster">
  <div class="header">
    <div class="icon">♟️</div>
    <div class="titles">
      <div class="title">棋类对局 · 战绩报告</div>
      <div class="sub">跨房间全局战绩榜 · {{ scope }}</div>
    </div>
    <div class="badge">{{ subtitle }}</div>
  </div>
  <div class="rule"></div>
  {% if sections %}
  <div class="cards">
  {% for s in sections %}
    <div class="card">
      <div class="card-head"><span class="cicon">{{ s.icon }}</span><span class="clabel">{{ s.label }}</span></div>
      {% for r in s.rows %}
      <div class="row">
        <span class="rank {{ 'medal' if loop.index0 < 3 else '' }}">{{ r.rank }}</span>
        <span class="name">{{ r.name }}</span>
        <span class="stat">{{ r.wins }} 胜 · {{ r.winrate }}%</span>
      </div>
      {% endfor %}
    </div>
  {% endfor %}
  </div>
  {% else %}
  <div class="empty">当前暂无棋类战绩记录。</div>
  {% endif %}
  <div class="footer">数据来自跨房间全局战绩榜 · 由 花火 监督生成</div>
  <div class="accent"></div>
</div>
</body>
</html>"""


@dataclass(slots=True)
class _RecentPrivateGameResult:
    room_id: str
    user_qq: str
    summary: str
    expires_at: float = 0.0


def _sanitize_uc_ai_speech(text: str) -> str:
    """把 AI 发言规整为「不超过 1 个逗号、总体 20 字以内」的短句，并去掉表情/提示词残留。"""
    text = str(text or "").strip().strip("“”\"'「」")
    # 去掉 `&&happy&&` 这类表情/心情提示词 token，并清掉任何残留的 `&`（含孤立的 `&&`）
    text = re.sub(r"&{2,}.*?&{2,}", "", text)
    text = text.replace("&", "")
    text = re.sub(r"\s+", " ", text).strip()
    # 最多保留 1 个逗号：按中文/英文逗号、顿号、分号切分，只保留前两句
    parts = re.split(r"[，,、；;]", text)
    if len(parts) > 2:
        text = (parts[0].strip() + "，" + parts[1].strip()) if parts[1].strip() else parts[0].strip()
    # 总体不超过 20 字
    text = text[:20].strip()
    # 去掉可能残留的首尾标点
    return text.strip("，,、；;。！？!?")


_EMOTION_TOKEN_RE = re.compile(r"&+[A-Za-z0-9_\u4e00-\u9fa5]{1,16}&+")


def _clean_emotion_tokens(text: str) -> str:
    """清洗模型偶发输出的表情/心情提示词 token（如 &&happy&&、&sigh&&、&amp;&amp;confused&&）。

    先还原常见 HTML 实体（模型有时把 & 输出成 &amp;），再删除 &...& 形式的 token（单/双 &
    都算），最后清理任何残留的孤立 &，返回干净正文。
    """
    text = str(text or "")
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _EMOTION_TOKEN_RE.sub("", text)
    text = text.replace("&", "")
    return text.strip()


def _uc_eligible_vote_targets(game, voter_num: int) -> list[int]:
    """返回某玩家当前可投的合法目标。

    普通轮投任意存活非本人；PK 子轮只能投重新发言的平票候选人（否则服务端会拒绝，
    导致供应商故障时 AI 兜底票永远失效而整局卡死）。
    """
    live = [p.number for p in game.players if not p.is_out]
    pending = getattr(game, "pending_round", None)
    if pending is not None and getattr(pending, "pk_reason", ""):
        tgt = [
            n
            for n in pending.speech_player_numbers
            if n in live and n != voter_num
        ]
        if tgt:
            return tgt
    return [n for n in live if n != voter_num]


def _undercover_title_text(game: Any) -> str:
    """还原谁是卧底结局标题（平民/卧底/白板获胜）。"""
    try:
        camp = getattr(game, "winner_camp", None) or (
            dict(getattr(game, "winner", {}) or {}).get("camp")
        )
        text = {
            "civilian": "平民获胜", "undercover": "卧底获胜", "whiteboard": "白板获胜",
        }.get(camp)
        return text or "本局结束"
    except Exception:
        return "本局结束"


def _undercover_game_summary(game: Any) -> str:
    """把一局谁是卧底的关键信息拼成可读文本，供复盘/战报使用。"""
    try:
        parts = []
        civ = getattr(game, "civilian_word", "") or ""
        uc = getattr(game, "undercover_word", "") or ""
        if civ or uc:
            parts.append(f"平民词条「{civ}」/ 卧底词条「{uc}」")
        players = getattr(game, "players", None) or []
        for p in players:
            camp = getattr(p, "camp", "") or ""
            camp_cn = {
                "civilian": "平民", "undercover": "卧底", "whiteboard": "白板",
            }.get(camp, camp or "?")
            out = "已出局" if getattr(p, "is_out", False) else "存活"
            name = getattr(p, "display_name", "") or ""
            parts.append(
                f"{getattr(p, 'number', '?')}号{'·' + name if name else ''}({camp_cn},{out})"
            )
        return "；".join(parts)
    except Exception:
        return ""


def _uc_ai_fallback(
    camp: str, round_no: int, seed: int = 0, *, used: list[str] | None = None
) -> str:
    """谁是卧底 AI 发言的本地兜底文案：按轮次 + 座号偏移轮换，且尽量不重复已说过的话。

    `used` 传入该玩家（或全场）已经说过的句子时，会跳过与自己历史雷同的候选，
    只挑一句新的；连兜底都被用光时才退回相似度最低的那句。
    """
    if camp == "whiteboard":
        pool = [
            "刚看到它的时候我还愣了一下，好像之前在哪见过。",
            "怎么说呢，它给我的第一印象就是挺顺手、挺实用的。",
            "反正最近家里一直在用，我媳妇还念叨来着。",
            "这个嘛，跟别的比起来没什么好挑的，习惯了就好。",
            "它在我这儿的存在感一直挺高的，几乎天天见。",
            "说起来也不贵，但用着用着就觉得离不开它了。",
            "反正我第一个想到的就是它，没别的想法。",
            "奇奇怪怪的，我怎么越看越觉得它眼熟。",
            "哎，这东西摆在那儿总让我有种说不上来的熟悉感。",
            "要说的话，我大概只会用一个词形容它，可惜不能说。",
            "上次还跟人聊起过它，结果谁也没说出个所以然。",
            "我对它的态度一直是可有可无，但真没了会别扭。",
        ]
    else:
        pool = [
            "说起来昨天我还用到它了，当时就觉得挺顺手的。",
            "我倒是觉得它挺经用的，家里那个用了好几年也没坏。",
            "反正吧，它在我这儿的存在感挺高的，一天不落。",
            "这东西说不上稀罕，但少了它还真有点不方便。",
            "我印象里这玩意儿还挺常见的，走哪都能碰上。",
            "它对我来说就是顺手两个字，没什么好纠结的。",
            "对了，上次它还帮了我一把，一直印象很深。",
            "别的不好说，反正它挺对我的胃口。",
            "我妈那边也备了一个，用起来差不多。",
            "说起来有点怪，我好像越用越挑。",
            "平时不怎么在意，缺了才发现挺要紧的。",
            "我一般不会主动买，都是别人送的。",
        ]
    ordered = pool[round_no % len(pool) :] + pool[: round_no % len(pool)]
    # 座号偏移：同轮多名 AI 兜底时不会撞到同一句
    if seed:
        shift = int(seed) % len(ordered)
        ordered = ordered[shift:] + ordered[:shift]
    history = [str(item) for item in (used or []) if str(item or "").strip()]
    best_line = ordered[0]
    best_score = 2.0
    for line in ordered:
        if not _uc_speech_is_repeat(line, history):
            return line
        score = max((_uc_text_similarity(line, old) for old in history), default=0.0)
        if score < best_score:
            best_line, best_score = line, score
    return best_line


# AI 发言的「角度」池：每次发言随机分配一个，PK 加赛时再换一个，
# 避免模型在同一个词条上反复输出同一句话（平票加赛后重复发言的根因）。
UC_SPEECH_ANGLES: tuple[str, ...] = (
    "最近一次亲手用它时的具体场景",
    "它在你家里固定摆放的位置",
    "用它时出过的一个小意外或小麻烦",
    "别人对它的一句评价",
    "第一次接触它时的印象",
    "它和你家人或朋友之间的一件小事",
    "你和它之间的一个固定时间点（早上、睡前之类）",
    "它的手感、声音或者气味",
    "它和你用过的同类东西的一次比较",
    "弄丢或弄坏它时的一次着急",
    "一个跟它有关的小习惯",
    "最近一次因为它被夸或被吐槽",
)


def _uc_speech_is_repeat(
    content: str, history: list[str] | tuple[str, ...], *, threshold: float = 0.72
) -> bool:
    """这句话是否和自己（或全场）已经说过的某句重复。

    完全一致直接算重复；短句只差几个字时 SequenceMatcher 的比例仍然很高，
    所以用相似度阈值兜住「换个词说同一件事」的复读。
    """
    text = "".join(str(content or "").split())
    if not text:
        return False
    for old in history or ():
        candidate = "".join(str(old or "").split())
        if not candidate:
            continue
        if text == candidate:
            return True
        if _uc_text_similarity(text, candidate) >= threshold:
            return True
    return False


@register(
    PLUGIN_NAME,
    "Katou-Kouseki",
    "让 Bot 与用户通过可视化房间自然地一起玩游戏。",
    PLUGIN_VERSION,
)
class GameCompanionPlugin(Star):
    """Game rooms that preserve AstrBot's normal conversation pipeline."""

    # ---------------------------------------------------------------
    # 模型调用闸门
    #
    # 群里十几个人同时提问时，请求会一窝蜂打到模型上：先撞限流，然后每条都
    # 要等 30 秒超时才失败，玩家看到的是「Bot 暂时无法判断」——其实只是排队
    # 没排上。这里限住同时在跑的调用数，多出来的排队等；真撞上 429 就退避
    # 重试，因为限流是临时状态，等一两秒通常就过去了。
    # ---------------------------------------------------------------
    LLM_MAX_CONCURRENCY = 3
    # 排队上限：等这么久还没轮到就别让玩家干等，直说「人有点多」
    LLM_QUEUE_TIMEOUT = 25.0
    # 限流退避：重试比重开一次请求便宜，也避免把上游越打越限
    LLM_RETRY_DELAYS = (0.8, 2.0)
    # 判定为「临时性限流」的线索（各家 SDK 的报错措辞不一样）
    LLM_RATE_LIMIT_HINTS = (
        "429",
        "rate limit",
        "ratelimit",
        "rate_limit",
        "too many requests",
        "quota",
        "overloaded",
        "503",
        "限流",
        "频率",
    )

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
        # 兼容 0.5.12 短暂使用的中文键：回迁为英文键（必须在任何 undercover 读取之前执行）
        self._revert_undercover_chinese_config_keys()

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
        # 解说可用独立 Provider：留空则跟随所在会话的聊天模型
        self.commentary_provider_id = self._cfg_str("commentary.provider_id", "")
        self.enabled_games: dict[GameType, bool] = {
            game_type: self._cfg_bool(f"{game_type}.enabled", True)
            for game_type in SUPPORTED_GAMES
        }
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=20
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
        )
        self.turtle_soup_max_players = self._cfg_non_negative(
            "turtle_soup.max_players", 0
        )
        # 每局提问上限：默认 15，0 = 不限。只限「提问」，推理永远可以继续提交，
        # 不然次数用完后这局就没法解开了。
        self.turtle_soup_max_questions = self._cfg_non_negative(
            "turtle_soup.max_questions", 15
        )
        # 每局推理上限：整局共享，用完还没人答对就直接揭晓汤底（0 = 不限）。
        # 人多时按人头发额度等于没有上限，控的是这一局总共能调几次判定模型。
        self.turtle_soup_max_answers = self._cfg_non_negative(
            "turtle_soup.max_answers", 8
        )
        self.turtle_soup_free_questioning = self._cfg_bool(
            "turtle_soup.free_questioning", True
        )
        self.turtle_soup_free_question_cooldown = self._cfg_int(
            "turtle_soup.free_question_cooldown", 1, minimum=0, maximum=30
        )
        # 超时兜底揭晓：长时间没有推进就自动收题并公布汤底（0 = 关闭）
        self.turtle_soup_auto_reveal_seconds = self._cfg_non_negative(
            "turtle_soup.auto_reveal_seconds", 1800
        )
        # 网络题库：留空表示只用 LLM + 内置题库；地址需返回 JSON 题目数组
        self.turtle_soup_online_pool_url = self._cfg_str(
            "turtle_soup.online_pool_url", ""
        ).strip()
        # 内置题库（soup_library.json，随插件分发）：出题优先级在网络题库之后、
        # LLM 之前；关掉就只剩「网络题库 → LLM → 内置兜底 12 条」。
        self.turtle_soup_library_enabled = self._cfg_bool(
            "turtle_soup.library_enabled", True
        )
        # 内置题库与网络题库谁优先：内置不联网、出得快；网络题库更新鲜。
        # 自定义投稿永远排最前（玩家投了就该被玩到），这项只管后两者。
        self.turtle_soup_library_priority = (
            self._cfg_str("turtle_soup.library_priority", "builtin_first")
            .strip()
            .lower()
            or "builtin_first"
        )
        if self.turtle_soup_library_priority not in {
            "builtin_first",
            "online_first",
            "generated_first",
        }:
            self.turtle_soup_library_priority = "builtin_first"
        # 预生成：开局出好题后，后台再让模型备一道「下一题」存起来。
        # 池子空了就得现场等生成 + 自检几十秒，提前备好下一局直接命中。
        # 0 表示关闭，不再单独放一个开关，省得两个配置项互相打架。
        self.turtle_soup_prefetch_target = self._cfg_int(
            "turtle_soup.prefetch_target", 1, minimum=0, maximum=3
        )
        # 海龟汤默认难度：娱乐 / 简单 / 普通 / 困难（房间里仍可改）
        self.turtle_soup_difficulty = self._soup_difficulty(
            self._cfg_str("turtle_soup.difficulty", "normal")
        )
        # 次数是否跟难度走：开时按难度表，关时用 max_questions / max_hints / max_answers
        self.turtle_soup_follow_difficulty = self._cfg_bool(
            "turtle_soup.follow_difficulty", True
        )
        # 群聊互通总开关：关掉后群里不参与海龟汤（指令、@ 入口、模型工具全部停用）
        self.turtle_soup_qq_bridge = self._cfg_bool(
            "turtle_soup.qq_bridge_enabled", True
        )
        # 群里 @ 花火直接说话是否当作这一局的提问（不用写「汤 提问」）
        self.turtle_soup_qq_at_question = self._cfg_bool(
            "turtle_soup.qq_at_question", True
        )
        # 群里说话的人还没坐进玩家席时，自动安排一个空位（座位满了才回退到绑定引导）
        self.turtle_soup_qq_auto_bind = self._cfg_bool(
            "turtle_soup.qq_auto_bind", True
        )
        # 网页里问的题要不要同步到群里（合并成一条，不刷屏）
        self.turtle_soup_qq_sync = self._cfg_bool(
            "turtle_soup.qq_sync_questions", True
        )
        # 同步到群里的形态：text=纯文字（默认，最简单），image=一条横条图片
        self.turtle_soup_qq_sync_style = self._cfg_str(
            "turtle_soup.qq_sync_style", "text"
        ) or "text"
        # 严格轮次（关掉 free_questioning）时，轮次换人到群里播报并 @ 下一位。
        # 默认开：群里看不到网页上的「轮到 X 号」，不说话就没人知道该谁问。
        self.turtle_soup_qq_turn_announce = self._cfg_bool(
            "turtle_soup.qq_turn_announce", True
        )
        # 题目就绪时往群里发一张「开局卡」（汤面 + 难度 + 次数），默认开：
        # 群里开的局以前连题面都看不到，必须点进网页才知道在玩什么。
        self.turtle_soup_qq_opening_card = self._cfg_bool(
            "turtle_soup.qq_opening_card", True
        )
        self.turtle_soup_group_announce_enabled = self._cfg_bool(
            "turtle_soup.group_announce_enabled", False
        )
        self.turtle_soup_group_announce_auto = self._cfg_bool(
            "turtle_soup.group_announce_auto", False
        )
        self.turtle_soup_group_announce_style = self._cfg_str(
            "turtle_soup.group_announce_style", "text"
        ) or "text"
        self.turtle_soup_group_announce_recap = self._cfg_bool(
            "turtle_soup.group_announce_recap", True
        )
        # 玩家出题时花火是否作为参与者一起猜；节奏按在线玩家数自动换算
        self.turtle_soup_bot_join = self._cfg_bool(
            "turtle_soup.bot_join_player_host", True
        )
        # 出题与判断可以各用各的模型：出题偏思考型，判断偏快（留空跟随会话模型）
        self.turtle_soup_generation_provider_id = self._cfg_str(
            "turtle_soup.generation_provider_id", ""
        ).strip()
        self.turtle_soup_judge_provider_id = self._cfg_str(
            "turtle_soup.judge_provider_id", ""
        ).strip()
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
        # 群通报（本局胜负+惩罚发到开房游戏群）：总开关 + 是否自动播报；
        # 关闭自动时由结算卡「通报到群」按钮手动触发。属插件配置，不在管理台展示。
        self.group_announce_enabled = self._cfg_bool(
            "undercover.group_announce_enabled", False
        )
        self.group_announce_auto = self._cfg_bool(
            "undercover.group_announce_auto", False
        )
        self.group_announce_style = (
            self._cfg_str("undercover.group_announce_style", "text") or "text"
        ).lower()
        # 群通报是否附带「花火复盘」：关闭后只发战报本身，图文模式复盘放在图片之后
        self.group_announce_recap = self._cfg_bool(
            "undercover.group_announce_recap", True
        )
        # 谁是卧底战绩报表（配置项先行，定时触发后续）：白名单为空格/逗号分隔的 UMO 会话串，
        # 例如 UMO:huahuo:GroupMessage:454366619。
        self.undercover_report_enabled = self._cfg_bool(
            "undercover.report_enabled", False
        )
        self.undercover_report_mode = self._cfg_str(
            "undercover.report_mode", "daily"
        ) or "daily"
        self.undercover_report_groups = self._cfg(
            "undercover.report_groups", ""
        )
        self.undercover_report_time = self._cfg_str(
            "undercover.report_time", "09:00"
        ) or "09:00"
        self.undercover_report_weekday = self._cfg_int(
            "undercover.report_weekday", 1, minimum=0, maximum=6
        )
        # 棋类播报（五子棋/黑白棋/象棋/围棋）：与谁是卧底同构的独立配置段，
        # 群通报开关 / 花火获胜自动播报 / 发送方式 / 复盘 / 战绩日报周报。
        self.board_group_announce_enabled = self._cfg_bool(
            "board.group_announce_enabled", False
        )
        self.board_group_announce_auto = self._cfg_bool(
            "board.group_announce_auto", False
        )
        self.board_group_announce_auto_all = self._cfg_bool(
            "board.group_announce_auto_all", False
        )
        self.board_group_announce_style = (
            self._cfg_str("board.group_announce_style", "text") or "text"
        ).lower()
        self.board_group_announce_recap = self._cfg_bool(
            "board.group_announce_recap", True
        )
        # 播报节流：连续速战时避免每局刷群
        self.board_group_announce_cooldown = self._cfg_non_negative(
            "board.group_announce_cooldown_seconds", 30
        )
        self.board_group_announce_min_moves = self._cfg_non_negative(
            "board.group_announce_min_moves", 5
        )
        # 连战合并：玩家在房间内就攒着（离席/离线再统一发），兜底上限避免永久憋着
        self.board_group_announce_wait_while_active = self._cfg_bool(
            "board.group_announce_wait_while_active", True
        )
        self.board_group_announce_series_idle = self._cfg_non_negative(
            "board.group_announce_series_idle_seconds", 1800
        )
        self.board_report_enabled = self._cfg_bool(
            "board.report_enabled", False
        )
        self.board_report_mode = self._cfg_str(
            "board.report_mode", "daily"
        ) or "daily"
        self.board_report_groups = self._cfg(
            "board.report_groups", ""
        )
        self.board_report_time = self._cfg_str(
            "board.report_time", "09:00"
        ) or "09:00"
        self.board_report_weekday = self._cfg_int(
            "board.report_weekday", 1, minimum=0, maximum=6
        )
        # 去重窗口：记录 N 次抽取到的词条，避免短时间重复
        self._undercover_word_window: list[tuple[str, str]] = []
        self._undercover_word_window_max = 10
        # 会话 -> {"bot": aiocqhttp bot, "group_id": int, "self_id": int}
        # 用于违规/失败时直接调用群禁言（不依赖 LLM 工具），拿不到句柄则只记录而不阻断游戏
        self._session_bot_ctx: dict[str, dict] = {}
        self.undercover_word_store = UndercoverWordStore(
            self.data_dir / "undercover_words.json"
        )
        # 海龟汤题库账本：跨房间、跨重启记录已出过的题目，用于汤面去重
        self.soup_deck = SoupDeck(self.data_dir / "turtle_soup_deck.json")
        # 私聊「新增词条」后的待录入状态：发送者 QQ -> 过期时间戳（time.time）
        self._uc_word_pending: dict[str, float] = {}
        self.multiplayer_turn_timeout = self._cfg_non_negative(
            "multiplayer.turn_timeout_seconds", 60
        )
        # 棋类对弈：每步倒计时（超时判负）与掉线后等待重连的宽限
        self.duel_turn_timeout = self._cfg_non_negative(
            "multiplayer.duel_turn_timeout_seconds", 180
        )
        self.duel_offline_grace = self._cfg_non_negative(
            "multiplayer.duel_offline_grace_seconds", 90
        )
        # 棋类备战阶段的「挂机让座」阈值：页面开着但长时间没有有效操作。
        self.duel_afk_seat_timeout = self._cfg_non_negative(
            "multiplayer.duel_afk_seat_timeout_seconds", 60
        )
        # 围棋：盘路（9/13/19）与黑贴目；只作用于新开的一局
        self.weiqi_board_size = self._cfg_int(
            "weiqi.board_size", 13, minimum=9, maximum=19
        )
        self.weiqi_komi = self._safe_float(self._cfg("weiqi.komi", 7.5), 7.5)
        self.swap_request_cooldown = self._cfg_non_negative(
            "multiplayer.swap_request_cooldown_seconds", 30
        )
        self.swap_request_expiry = self._cfg_int(
            "multiplayer.swap_request_expiry_seconds", 20, minimum=1, maximum=600
        )
        # 房间内投票换游戏
        self.game_vote_enabled = self._cfg_bool("multiplayer.game_vote_enabled", True)
        self.game_vote_expiry = self._cfg_int(
            "multiplayer.game_vote_expiry_seconds", 60, minimum=10, maximum=600
        )
        self.game_vote_cooldown = self._cfg_non_negative(
            "multiplayer.game_vote_cooldown_seconds", 30
        )
        self.game_vote_allow_while_playing = self._cfg_bool(
            "multiplayer.game_vote_allow_while_playing", False
        )
        # 再来一局是否换先（黑/白、象棋红/黑互换）
        self.duel_side_rotation = self._cfg_bool(
            "multiplayer.duel_side_rotation", True
        )
        # 花火整体思考延迟（秒）：简单/普通/困难在此基础上小幅增减，
        # 调这一个值就能整体变快或变慢，避免 AI 先手时一开局就落子。
        self.ai_think_delay = max(
            0.0,
            min(
                5.0,
                self._safe_float(self._cfg("game.ai_think_delay_seconds", 0.8), 0.8),
            ),
        )

        # 谁是卧底 AI 动作节流：同一房间同一时刻只允许一个 AI 动作（发言/投票）在途，
        # 避免大量 AI 玩家连续触发时对模型供应商并发/高频请求导致限流（429）而卡局。
        # key = room_id -> asyncio.Lock；同时记录“当前在途 AI 动作”的座位号与动作类型，防止同座重复驱动。
        self._uc_ai_lock: dict[str, asyncio.Lock] = {}
        self._uc_ai_inflight: dict[str, tuple[int, str]] = {}

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
            turtle_soup_max_questions=self.turtle_soup_max_questions,
            turtle_soup_max_answers=self.turtle_soup_max_answers,
            turtle_soup_follow_difficulty=self.turtle_soup_follow_difficulty,
            turtle_soup_free_questioning=self.turtle_soup_free_questioning,
            turtle_soup_free_question_cooldown=self.turtle_soup_free_question_cooldown,
            turtle_soup_auto_reveal_seconds=self.turtle_soup_auto_reveal_seconds,
            turtle_soup_bot_join=self.turtle_soup_bot_join,
            multiplayer_turn_timeout=self.multiplayer_turn_timeout,
            duel_turn_timeout=self.duel_turn_timeout,
            duel_offline_grace=self.duel_offline_grace,
            duel_afk_seat_timeout=self.duel_afk_seat_timeout,
            swap_request_cooldown=self.swap_request_cooldown,
            swap_request_expiry=self.swap_request_expiry,
            game_vote_enabled=self.game_vote_enabled,
            game_vote_expiry=self.game_vote_expiry,
            game_vote_cooldown=self.game_vote_cooldown,
            game_vote_allow_while_playing=self.game_vote_allow_while_playing,
            duel_side_rotation=self.duel_side_rotation,
            ai_think_delay=self.ai_think_delay,
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
            weiqi_board_size=self.weiqi_board_size,
            weiqi_komi=self.weiqi_komi,
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
        # 海龟汤「预生成下一题」的后台任务：同一时刻只跑一个，避免多房间把生成模型打满
        self._soup_prefetch_task: asyncio.Task | None = None
        self._tunnel_recovery_task: asyncio.Task | None = None
        self._next_tunnel_retry_at = 0.0
        # 房间访问地址的 base（协议+主机+端口）缓存：首次解析成功后固定下来，
        # 让管理台/群里的房间链接不会因为 Quick Tunnel 短暂抖动或局域网探测反复变化。
        self._access_base_cache: str = ""
        self._background_tasks: set[asyncio.Task] = set()
        # 房间 id → 计划中的「连战 N 局」合并播报任务（同一房间只排一个）
        self._board_flush_tasks: dict[str, asyncio.Task] = {}
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
        self._register_report_scheduler()
        self._register_board_report_scheduler()
        logger.info(
            "[GameCompanion] 花火陪你玩已加载；房间服务将在首次创建房间时按需启动"
        )

    async def terminate(self) -> None:
        """Invalidate every room and stop only plugin-owned resources."""
        self._unregister_companion_invite_ability()
        self._unregister_report_scheduler()
        self._unregister_board_report_scheduler()
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
        支持 gomoku（五子棋）、xiangqi（中国象棋）、weiqi（围棋）、tictactoe（井字棋）、othello（黑白棋）、
        turtle_soup（海龟汤）、pig_dice（贪心骰子）、draw_guess（你画我猜）、
        blackjack（二十一点，Bot 当庄家）和 undercover（谁是卧底，多人社交推理）。
        不要因为普通聊天中偶然提到游戏名称就调用本工具。

        当前会话已有房间时的规则：
        - 用户要的是同一种游戏 → 复用原房间，只返回原入口；
        - 用户要的是**另一种**游戏，且旧房间已空闲（没有进行中的对局、无人占座）→
          本工具会**自动把旧房间切换成新玩法**，沿用原链接，你照常告知即可；
        - 用户要的是另一种游戏，但旧房间还有人在用 → 工具会另开一个新房间。
        所以「开个你画我猜的房间」这种请求，只要旧房间空着，直接按 draw_guess 调用即可，
        不要擅自改成旧玩法，也不要说「还是原来的五子棋房间、没换」。

        悔棋、再来一局等局内操作仍由用户进入 WebUI 后完成，不能在 QQ 中代替用户执行。

        Args:
            game_type(string): 游戏类型，只能是 gomoku、xiangqi、weiqi、tictactoe、othello、turtle_soup、pig_dice、draw_guess、blackjack 或 undercover。
            difficulty(string): 你决定使用的难度，只能是 easy、normal、hard；贪心骰子中分别表示稳健、均衡和大胆，二十一点中影响庄家软 17 规则；谁是卧底时忽略。
            turtle_soup_mode(string): 海龟汤玩法；bot_host 表示 Bot 出题玩家猜，player_host 表示玩家给线索 Bot 猜。非海龟汤时忽略。
            admin_room(boolean): 仅当群聊中的游戏管理员明确要求创建管理员房间时传 true。普通群聊房间必须传 false；非游戏管理员不能创建管理员房间。
            confirm_abandon(boolean): 切换游戏且当前局未结束时，用户是否已明确同意放弃本局。
        """
        try:
            game_type = self._game_type(kwargs.get("game_type"))
        except ValueError as exc:
            return self._json_error(str(exc))
        message = str(getattr(event, "message_str", "") or "")
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        # 只在群聊里把关：群里发一条链接会打扰所有人，是这次要治的场景；
        # 私聊就一个人，误开一局影响很小，不值得再收紧。
        if self._event_group_id(event):
            allowed, intent_reason = self._room_request_intent(message, game_type)
            if not allowed and self._room_followup_confirmed(session_id, message):
                allowed, intent_reason = True, "刚追问过，用户回了肯定答复"
            if not allowed:
                self._remember_room_intent_block(session_id)
                logger.info(
                    "[GameCompanion] 已拦截群里没提出开局请求的建房调用: 依据=%s 游戏=%s 会话=%s 消息=%r",
                    intent_reason,
                    game_type,
                    session_id,
                    message[:120],
                )
                return self._json_error(
                    f"这条消息并没有要求开一局游戏（{intent_reason}）。"
                    "不要创建房间，也不要发任何链接；按普通聊天正常回复即可。"
                    "如果你确实想陪用户玩，可以先用一句话问问要不要来一局，"
                    "等用户明确答应后再调用本工具。"
                )
            logger.info(
                "[GameCompanion] 群聊建房请求通过意图校验: 依据=%s 游戏=%s 会话=%s 消息=%r",
                intent_reason,
                game_type,
                session_id,
                message[:120],
            )
        difficulty = self._difficulty_for_game(
            game_type, kwargs.get("difficulty")
        )
        turtle_soup_mode = self._turtle_soup_mode(kwargs.get("turtle_soup_mode"))
        try:
            room, reused, restarted, switched = await self._create_or_reuse_room_from_event(
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
            self._remember_bot_ctx(room, event)  # 缓存群禁言句柄（拿不到也不影响）
            url = self._room_url(room)
            link_delivered = await self._deliver_room_link(
                room,
                url,
                reused=reused,
                restarted=restarted,
                switched=switched,
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
                "switched_game": switched,
                "entry_timeout_seconds": self.manager.empty_player_timeout,
                "instruction": (
                    "房间链接已由插件作为独立纯文字消息发送；正常延续人格聊天，"
                    "不要复述、改写或重新生成链接，也不要提起内部房间编号 room_id。"
                    if link_delivered
                    else "已把空闲的旧房间切换成新玩法并复用原链接，不得关闭它或创建新房间；"
                    "完整保留 room_url。"
                    if switched
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
        """在群聊里开一局海龟汤，或参与当前正在进行的一局。

        用户明确说「来一局海龟汤 / 开一局汤」时用 start：它会建房并发房间链接，
        同时把发起人排进玩家席。已有进行中的一局时不要用 start，改用 ask / answer / hint。
        用户只是普通聊天、没有明确要问一句或提交推理时，不要调用。

        Args:
            action(string): start=开一局海龟汤；ask=问一个是/否问题；answer=提交对汤底的完整推理；hint=要一条提示；status=看进度。
            text(string): 问题或推理正文，尽量保留用户原话；start 可填难度（娱乐/简单/普通/困难），hint 与 status 可留空。
            room_id(string): 可选房间编号；当前会话只有一个房间时可以留空。
        """
        action = str(kwargs.get("action") or "").strip().lower()
        text = str(kwargs.get("text") or "").strip()
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return self._json_error(
                "群内互通已关闭，海龟汤只能在网页房间里进行；不要创建房间，也不要发链接。"
            )
        if action == "start":
            reply = await start_soup_room(self, event, text)
            return json.dumps({"ok": True, "reply": reply}, ensure_ascii=False)
        target = {
            "ask": "question",
            "question": "question",
            "answer": "answer",
            "hint": "hint",
            "status": "status",
        }.get(action)
        if target is None:
            return self._json_error("action 只能是 start、ask、answer、hint 或 status")
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        qq = str(event.get_sender_id() or "").strip()
        room = soup_room_for_session(self, session_id)
        if room is None:
            return self._json_error(
                "当前会话没有进行中的海龟汤。不要创建房间，也不要发链接；"
                "如果你想陪用户玩，先问一句要不要开一局。"
            )
        if target in {"question", "answer"} and not text:
            return self._json_error("text 不能为空：把用户的问题或推理原话放进来")
        reply = await run_soup_action(
            self,
            room,
            target,
            text,
            qq,
            actor_name=str(getattr(event, "get_sender_name", lambda: "")() or "").strip(),
        )
        return json.dumps({"ok": True, "reply": reply}, ensure_ascii=False)

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

    @filter.command("汤", alias={"海龟汤"})
    async def turtle_soup_command(self, event: AstrMessageEvent):
        """海龟汤显式指令：单独一句「汤」直接开局，汤+内容触发提问/推理/提示等。

        AstrBot 标准指令带 wake 前缀（`/` 或 @Bot）时走这里，会显示在指令列表；
        群里不 @ 直接发「汤 xx」仍由下面的正则兜底，两条路共用同一套解析。
        """
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        reply = await handle_soup_command(self, event)
        if not reply:
            return
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        yield event.plain_result(reply)

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
            "weiqi": "9/13/19 路棋盘，附带实时胜率",
            "tictactoe": "三连即可获胜",
            "othello": "8×8 棋盘，夹住对方棋子即可翻转；双方无合法步时棋子较多者获胜",
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

    @filter.command("谁是卧底日报", alias={"卧底日报", "谁是卧底周报", "卧底周报"})
    async def undercover_report_now(self, event: AstrMessageEvent):
        """立即生成一份谁是卧底战绩日报/周报海报发到当前会话（样式测试/手动补发）。

        顶层指令：直接发送「谁是卧底日报 / 卧底日报」等即可触发（无需游戏指令组前缀）。
        输出排行榜海报图（t2i 优先、Pillow 兜底）；都不可用时回退纯文字榜单。
        内容与定时日报/周报相同（全局战绩榜前 20），不依赖 report_enabled 开关。
        """
        message = str(getattr(event, "message_str", "") or "")
        subtitle = "周报" if ("周报" in message and "日报" not in message) else "日报"
        data = self._undercover_report_data(subtitle=subtitle)
        try:
            image_data = await self._undercover_report_poster(data)
        except Exception as exc:
            logger.warning("[GameCompanion] 生成战绩海报失败，回退纯文字: %s", exc)
            yield event.plain_result(self._build_undercover_report())
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain([Image(file=f"base64://{image_data}")])
            )
        except Exception as exc:
            yield event.plain_result(f"战绩海报已生成但发送失败：{exc}")

    @filter.command("谁是卧底海报", alias={"卧底海报", "战况海报测试"})
    async def undercover_poster_test(self, event: AstrMessageEvent, camp: str = ""):
        """用示例数据生成一张「战况通知」海报（服务端渲染），用于测试海报样式。

        顶层指令：直接发送「谁是卧底海报 / 卧底海报」即可触发。
        可选参数：平民 / 卧底 / 白板（默认平民），例如「卧底海报 卧底」。
        优先用 AstrBot t2i（HTML 渲染）出图，不可用时回退服务端 Pillow。
        """
        room = self._undercover_sample_room(camp)
        try:
            image_data = await self._undercover_announce_poster(room)
        except Exception as exc:
            yield event.plain_result(f"生成测试海报失败：{exc}（需要服务端 Pillow 或可用的 AstrBot t2i 服务）")
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain([Image(file=f"base64://{image_data}")])
            )
        except Exception as exc:
            yield event.plain_result(f"海报已生成但发送失败：{exc}")

    @filter.command("谁是卧底战报", alias={"卧底战报", "战报样式测试"})
    async def undercover_report_test(self, event: AstrMessageEvent, camp: str = ""):
        """用示例数据生成一条「群通报」战报文案，用于测试战报文字样式。

        顶层指令：直接发送「谁是卧底战报 / 卧底战报」即可触发。
        可选参数：平民 / 卧底 / 白板（默认平民）；复盘为示例占位，真实场景由 LLM 生成。
        """
        text = self._undercover_announce_text(
            self._undercover_sample_room(camp).game
        )
        yield event.plain_result(text + "\n\n📝 复盘：示例复盘文案（真实场景由 AI 生成）。")

    @filter.command(
        "新增词条", alias={"添加词条", "新增词库", "添加词库", "录入词条"}
    )
    async def undercover_words_add_start(self, event: AstrMessageEvent):
        """私聊管理员：开始批量录入「谁是卧底」词条。

        发「新增词条 / 添加词条」后 Bot 会提示格式，随后把「词条1 词条2」每行
        一组的文本一次性发来即可入库；发「取消」退出。仅游戏管理员可在私聊使用。
        """
        sender = str(event.get_sender_id() or "").strip()
        if not sender or sender not in self.game_admin_ids:
            yield event.plain_result(
                "只有游戏管理员可以修改词库。可让管理员在插件配置的"
                "「rooms.game_admin_qq_ids（游戏管理员 QQ）」里加入你。"
            )
            return
        if not self._event_is_private(event):
            yield event.plain_result("请在私聊里使用该指令，避免在群里刷屏。")
            return
        now = time.time()
        # 顺手清掉其他已过期的待录入条目，避免长期堆积
        for stale in [key for key, exp in self._uc_word_pending.items() if exp <= now]:
            self._uc_word_pending.pop(stale, None)
        self._uc_word_pending[sender] = now + 600
        yield event.plain_result(
            "请把要添加的词条按下面格式发给我（每行一组，两个词用空格或逗号分隔）：\n"
            "词条1 词条2\n"
            "词条1 词条2\n\n"
            "我会逐行录入并回报成功 / 跳过情况。发送「取消」可退出（10 分钟内有效）。"
        )

    @filter.regex(r"^[\s\S]+$")
    async def undercover_words_add_receive(self, event: AstrMessageEvent):
        """接收「新增词条」之后管理员私聊发来的批量词条文本。"""
        sender = str(event.get_sender_id() or "").strip()
        expires_at = self._uc_word_pending.get(sender)
        if not expires_at or expires_at <= time.time():
            self._uc_word_pending.pop(sender, None)
            return
        if not self._event_is_private(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        # 指令本身也会被这条通配正则命中，交回指令处理器
        if text.startswith(("新增词条", "添加词条", "新增词库", "添加词库", "录入词条")):
            return
        # 命中待录入状态：吃掉这条消息，避免再被 LLM 当成闲聊回复一遍
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            try:
                stop_event()
            except Exception:
                pass
        if text in {"取消", "退出", "cancel", "Cancel"}:
            self._uc_word_pending.pop(sender, None)
            yield event.plain_result("已退出词条录入。")
            return
        store = self.undercover_word_store
        if store is None:
            self._uc_word_pending.pop(sender, None)
            yield event.plain_result("词库不可用，请稍后再试。")
            return
        pairs, malformed = self._parse_uc_word_lines(text)
        if not pairs and not malformed:
            yield event.plain_result(
                "没有识别到内容，请按「词条1 词条2」每行一组重新发送，或发「取消」退出。"
            )
            return
        added_pairs: list[tuple[str, str]] = []
        skipped: list[str] = [f"格式错误：{line}" for line in malformed]
        for w1, w2 in pairs:
            if store.add(w1, w2) is None:
                skipped.append(f"重复/非法：{w1} vs {w2}")
                continue
            added_pairs.append((w1, w2))
        self._uc_word_pending.pop(sender, None)
        lines = [
            f"词库录入完成：成功 {len(added_pairs)} 对，跳过 {len(skipped)} 对。"
        ]
        if added_pairs:
            preview = "、".join(f"{w1}/{w2}" for w1, w2 in added_pairs[:5])
            tail = f" 等 {len(added_pairs)} 对" if len(added_pairs) > 5 else ""
            lines.append(f"✅ 新增：{preview}{tail}")
        if skipped:
            preview = "；".join(skipped[:5])
            tail = f" …共 {len(skipped)} 条" if len(skipped) > 5 else ""
            lines.append(f"⏭ 跳过：{preview}{tail}")
        lines.append(f"📚 当前词库共 {len(store.list_all())} 对。")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _event_is_private(event: AstrMessageEvent) -> bool:
        """判断消息是否来自私聊（兼容 FriendMessage / PrivateMessage / :private:）。"""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return (
            "FriendMessage" in origin
            or "PrivateMessage" in origin
            or ":private:" in origin
        )

    @filter.command("棋类日报", alias={"棋类周报", "棋类战绩日报", "棋类战绩周报"})
    async def board_report_now(self, event: AstrMessageEvent):
        """立即生成一份棋类战绩日报/周报海报发到当前会话（样式测试/手动补发）。

        顶层指令：直接发送「棋类日报 / 棋类周报」等即可触发（无需游戏指令组前缀）。
        输出棋类排行榜海报（t2i 优先、Pillow 兜底）；都不可用时回退纯文字榜单。
        内容与定时棋类战绩报告相同（四种棋类全局榜），不依赖 board_report_enabled 开关。
        """
        message = str(getattr(event, "message_str", "") or "")
        subtitle = "周报" if ("周报" in message and "日报" not in message) else "日报"
        data = self._board_report_data(subtitle=subtitle)
        try:
            image_data = await self._board_report_poster(data)
        except Exception as exc:
            logger.warning("[GameCompanion] 生成棋类战绩海报失败，回退纯文字: %s", exc)
            yield event.plain_result(self._build_board_report())
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain([Image(file=f"base64://{image_data}")])
            )
        except Exception as exc:
            yield event.plain_result(f"棋类战绩海报已生成但发送失败：{exc}")

    @filter.command("棋类战报", alias={"棋类战报样式测试"})
    async def board_report_test(self, event: AstrMessageEvent, game: str = ""):
        """用示例对局生成一条「棋类战报」海报（文字 + 图），用于测试战报样式与终局高亮。

        顶层指令：直接发送「棋类战报 / 棋类战报样式测试」即可触发（默认五子棋）。
        可选参数：五子棋 / 黑白棋 / 中国象棋 / 围棋，例如「棋类战报 中国象棋」。
        图片优先用 AstrBot t2i（HTML 渲染），不可用时回退服务端 Pillow。
        """
        room = self._board_sample_room(game)
        text = self._board_announce_text(room) or "示例棋类战报生成失败"
        try:
            image_data = await self._board_announce_poster(room)
        except Exception as exc:
            yield event.plain_result(text + f"\n\n（海报生成失败：{exc}）")
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain(
                    [Plain(text + "\n\n"), Image(file=f"base64://{image_data}")]
                )
            )
        except Exception as exc:
            yield event.plain_result(text + f"\n\n（海报已生成但发送失败：{exc}）")

    @filter.command("海龟汤战报", alias={"汤战报", "海龟汤战报样式测试"})
    async def soup_report_test(self, event: AstrMessageEvent, state: str = ""):
        """用示例数据生成一局海龟汤的战报（群通知文案 + 花火复盘 + 战报海报）。

        顶层指令：直接发送「海龟汤战报 / 汤战报」即可触发。
        可选参数：解开 / 未解开（默认解开），例如「海龟汤战报 未解开」。
        示例数据是完整的（含汤底与 5 条提问），汤底只在聊天文字里给出并标注「仅测试」：
        正式的海报与群通知都不含汤底，这样没玩过的人还能自己玩。
        """
        room = self._soup_sample_room(solved=not self._soup_state_unsolved(state))
        announce = self._soup_announce_text(room) or "示例战报文案生成失败"
        header = "🧪 海龟汤战报测试（示例数据）\n\n"
        tail = (
            f"\n\n🔒 汤底（仅测试指令展示，正式海报 / 群通知都不包含）："
            f"{getattr(room, 'sample_solution', '')}"
            f"\n\n📝 复盘（示例文案，真实场景由 AI 生成）："
            "这局大家一直围着「他在等谁」打转，好几次都摸到门口了，"
            "可惜最后一步没敢问下去。想知道答案就自己来试一局吧～"
        )
        try:
            image_data = await self._soup_announce_poster(room)
        except Exception as exc:
            yield event.plain_result(header + announce + tail + f"\n\n（海报生成失败：{exc}）")
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain(
                    [Plain(header + announce + tail + "\n\n"), Image(file=f"base64://{image_data}")]
                )
            )
        except Exception as exc:
            yield event.plain_result(header + announce + tail + f"\n\n（海报已生成但发送失败：{exc}）")

    @filter.command("海龟汤海报", alias={"汤海报", "海龟汤海报样式测试"})
    async def soup_poster_test(self, event: AstrMessageEvent, state: str = ""):
        """只用示例数据出一张海龟汤战报海报（纯图，方便快速看版式）。

        顶层指令：直接发送「海龟汤海报 / 汤海报」即可触发。
        可选参数：解开 / 未解开（默认解开），例如「海龟汤海报 未解开」。
        """
        room = self._soup_sample_room(solved=not self._soup_state_unsolved(state))
        try:
            image_data = await self._soup_announce_poster(room)
        except Exception as exc:
            yield event.plain_result(
                f"生成测试海报失败：{exc}（需要 AstrBot t2i 或服务端 Pillow + 中文字体）"
            )
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain([Image(file=f"base64://{image_data}")])
            )
        except Exception as exc:
            yield event.plain_result(f"海报已生成但发送失败：{exc}")

    @filter.command("海龟汤速报", alias={"汤速报", "海龟汤速报样式测试"})
    async def soup_sync_test(self, event: AstrMessageEvent):
        """用示例数据渲染一张「问答速报」卡片（纯图，方便看版式）。

        顶层指令：「海龟汤速报 / 汤速报」。这张图就是网页提问同步到群时
        发的那张，改样式后可以直接看效果。
        """
        items = [
            {
                "number": "3号",
                "source": "网页",
                "kind_cn": "提问",
                "prompt": "是不是和灯塔有关？",
                "result": "是",
                "tone": "yes",
            },
            {
                "number": "5号",
                "source": "网页",
                "kind_cn": "提问",
                "prompt": "他是不是一个人住？",
                "result": "是",
                "tone": "yes",
            },
            {
                "number": "4号",
                "source": "群里",
                "kind_cn": "提问",
                "prompt": "碗筷是给客人的吗？",
                "result": "否",
                "tone": "no",
            },
            {
                "number": "6号",
                "source": "网页",
                "kind_cn": "推理",
                "prompt": "他妻子已经去世了，摆碗筷是不肯承认",
                "result": "部分正确",
                "tone": "near",
            },
        ]
        data = self._soup_sync_image_data(items, "每天两副碗筷")
        try:
            b64 = await self._soup_sync_t2i(data)
        except Exception as exc:
            yield event.plain_result(
                f"生成速报卡片失败：{exc}（需要 AstrBot t2i 渲染服务）"
            )
            return
        try:
            from astrbot.api.message_components import Image

            yield event.chain_result(
                MessageChain([Image(file=f"base64://{b64}")])
            )
        except Exception as exc:
            yield event.plain_result(f"速报卡片已生成但发送失败：{exc}")

    @staticmethod
    def _soup_state_unsolved(state: str) -> bool:
        """战报测试指令的参数解析：判断要不要出「没人解开」的版式。"""
        text = str(state or "").strip().lower()
        return text in {
            "未解开", "没解开", "失败", "无人解开", "timeout", "failed", "unsolved", "no",
        }

    def _soup_sample_room(self, *, solved: bool = True) -> Any:
        """构造一局示例海龟汤（含汤底与 5 条提问），供战报/海报测试指令使用。

        走真实的 TurtleSoupGame + manager._snapshot_soup_result，等于顺带验证了
        「快照 → 文案 / 海报」这条链路（含提问的剧透遮蔽）。
        """
        from types import SimpleNamespace

        from .turtle_soup import SoupEntry, SoupPuzzle, TurtleSoupGame

        puzzle = SoupPuzzle(
            title="每天两副碗筷",
            surface=(
                "男人独居十年，每天下班回家都要摆两副碗筷，把饭吃完，"
                "再把另一副洗干净收进橱柜。邻居劝过他很多次，他总说「她只是还没回来」。"
                "这是为什么？"
            ),
            solution=(
                "妻子三年前在那个雨夜突发心梗，倒在厨房里再没醒过来。"
                "他从此每天摆出第二副碗筷，好像她只是加班晚归；"
                "直到妹妹带他去看医生，他才把碗筷收进了柜子最深处。"
            ),
            key_facts=(
                "他一个人住",
                "第二副碗筷不是给客人的",
                "妻子已经去世",
                "摆碗筷是他不肯承认的习惯",
            ),
            acceptable_variants=(),
            hints=(
                "和「等待」有关",
                "屋子里发生过一件事",
                "问题不在碗筷，在谁缺席",
            ),
            content_level="normal",
            theme="家庭",
            trick="独居男人多摆一副碗筷，是留给再也回不来的妻子",
        )
        game = TurtleSoupGame(difficulty="normal", max_hints=3, content_level="normal")
        game.puzzle = puzzle
        game.phase = "finished"
        game.entries = [
            SoupEntry(
                kind="question",
                prompt="他在等人回来吗？",
                response="是",
                source="web",
                verdict="yes",
                player_number=1,
            ),
            SoupEntry(
                kind="question",
                prompt="等的人已经不在人世了吗？",
                response="是，而且走得很突然",
                source="web",
                verdict="yes",
                player_number=2,
            ),
            SoupEntry(
                kind="hint",
                prompt="提示 1",
                response="和「等待」有关",
                source="system",
            ),
            SoupEntry(
                kind="question",
                prompt="是不是和一场意外有关？",
                response="部分正确，是一起突发的意外",
                source="qq",
                verdict="partial",
                player_number=3,
            ),
            SoupEntry(
                kind="answer",
                prompt="他是在给去世的妻子留位置",
                response="还差一点：不是「留位置」，是「不肯承认」",
                source="web",
                verdict="partial",
                player_number=1,
            ),
            SoupEntry(
                kind="question",
                prompt="邻居知道这件事吗？",
                response="无关",
                source="web",
                verdict="irrelevant",
                player_number=2,
            ),
            SoupEntry(
                kind="question",
                prompt="他后来停下来了吗？",
                response="停了，但花了好几年",
                source="web",
                verdict="yes",
                player_number=3,
            ),
        ]
        game.question_count = 5
        game.hints_used = 2
        game.answer_attempts = 2 if solved else 3
        game.discovered_facts = {0, 1, 3} if solved else {0, 2}
        game.solved = solved
        game.end_reason = "solved" if solved else "timeout"
        room = SimpleNamespace(
            game_type="turtle_soup",
            completed_games=3,
            status="finished",
            difficulty="normal",
            multiplayer=SimpleNamespace(
                seats=[
                    SimpleNamespace(number=1, display_name="张三", is_ai=False),
                    SimpleNamespace(number=2, display_name="李四", is_ai=False),
                    SimpleNamespace(number=3, display_name="王五", is_ai=False),
                    # 花火是主持人（AI 座位），不该出现在「玩家」里
                    SimpleNamespace(number=4, display_name="花火", is_ai=True),
                ]
            ),
            game=game,
            recap_cache={},
        )
        self.manager._snapshot_soup_result(room)
        room.sample_solution = puzzle.solution
        return room

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

    @filter.regex(
        r"^\s*(?:\[CQ:at[^\]]*\]\s*|@[^\s]{1,24}\s+)?[/!！]?\s*"
        r"(?:海龟汤|汤(?=\s|$|开局|开房|开始|提问|问一下|推理|答题|答案|看答案|揭晓"
        r"|附议|同意|反对|提示|状态|查看|帮助|菜单))"
    )
    async def soup_qq_command(self, event: AstrMessageEvent):
        """群聊里的海龟汤指令：提问 / 推理 / 提示 / 揭晓 / 状态。

        只在当前会话有进行中的海龟汤房间时才真的处理；其余情况静默放行，
        让这条消息继续走正常聊天（「汤圆好吃」之类不能被当成指令）。
        """
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        reply = await handle_soup_command(self, event)
        if not reply:
            return
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        yield event.plain_result(reply)

    @filter.regex(r"^[\s\S]+$")
    async def soup_qq_at_say(self, event: AstrMessageEvent):
        """群里 @ 花火直接说话，就当作这一局的提问，不用写「汤 提问」。

        只接管「有进行中的海龟汤 + 发送者已在玩家席」的消息。其他群友 @ 花火
        仍然是正常聊天——不能因为群里开了一局，就把所有人的搭话都吃掉。
        想回到纯指令模式，把 `turtle_soup.qq_at_question` 关掉即可。
        """
        if not getattr(self, "turtle_soup_qq_at_question", True):
            return
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if not getattr(event, "is_at_or_wake_command", False):
            return
        reply = await handle_soup_at_message(self, event)
        if not reply:
            return
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        yield event.plain_result(reply)

    @filter.regex(
        r"^\s*[/!！]?\s*(?:添加海龟汤|添加汤|投稿海龟汤|投稿汤"
        r"|我的海龟汤|我的题库|海龟汤题库|删除海龟汤|删除汤)"
    )
    async def soup_custom_command(self, event: AstrMessageEvent):
        """自定义海龟汤：私聊投稿 / 查看 / 删除。

        只在私聊生效。群里发「汤面|汤底」等于当众剧透，而且刷得很快，
        所以群聊里直接不响应，让消息照常走聊天。
        """
        if self._event_group_id(event):
            return
        sender_getter = getattr(event, "get_sender_id", None)
        owner = (
            str(sender_getter() or "").strip() if callable(sender_getter) else ""
        )
        reply = handle_custom_command(self, event.message_str or "", owner)
        if not reply:
            return
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        yield event.plain_result(reply)

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
        elif isinstance(game, OthelloGame):
            human = "黑" if game.human_color == OTHELLO_BLACK else "白"
            bot = "黑" if game.bot_color == OTHELLO_BLACK else "白"
            turn = "玩家" if game.turn == game.human_color else "Bot"
            passed = (
                "；上一手后对方无合法落子，已跳过"
                if game.last_passed
                else ""
            )
            detail = (
                f"玩家执{human}、Bot 执{bot}；黑方先行，黑 {game.black_count} 子、"
                f"白 {game.white_count} 子，当前轮到{turn}{passed}。"
            )
        elif isinstance(game, XiangqiGame):
            human = "红" if game.human_side == XIANGQI_RED else "黑"
            bot = "红" if game.bot_side == XIANGQI_RED else "黑"
            turn = "玩家" if game.turn == game.human_side else "Bot"
            detail = (
                f"玩家执{human}，Bot 执{bot}，红方固定先手；已走 {len(game.moves)} 手"
                + ("。" if game.finished else f"，当前轮到{turn}。")
            )
        elif isinstance(game, WeiqiGame):
            human = "黑" if game.human_color == GOMOKU_BLACK else "白"
            bot = "黑" if game.bot_color == GOMOKU_BLACK else "白"
            turn = "玩家" if game.turn == game.human_color else "Bot"
            winrate = game.evaluate()
            detail = (
                f"{game.size} 路棋盘、黑贴 {game.komi} 目；玩家执{human}、Bot 执{bot}；"
                f"已下 {len(game.history)} 手，黑提 {game.captures.get(GOMOKU_BLACK, 0)} 子、"
                f"白提 {game.captures.get(GOMOKU_WHITE, 0)} 子；"
                f"当前胜率 黑 {winrate['black']}% / 白 {winrate['white']}%"
                + ("。" if game.finished else f"，轮到{turn}。")
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
                f"{hand.number}号{hand.value}点"
                + ("（21点）" if hand.blackjack else "")
                for hand in sorted(
                    game.hands.values(), key=lambda item: item.number
                )
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

        if isinstance(game, OthelloGame):
            symbols = {0: ".", OTHELLO_BLACK: "B", OTHELLO_WHITE: "W"}
            board = "/".join("".join(symbols[cell] for cell in row) for row in game.board)
            human = "黑" if game.human_color == OTHELLO_BLACK else "白"
            bot = "黑" if game.bot_color == OTHELLO_BLACK else "白"
            turn = "玩家" if game.turn == game.human_color else "Bot"
            lines.append(
                f"实时棋盘={board}；玩家执{human}，Bot 执{bot}；黑 {game.black_count} 子、"
                f"白 {game.white_count} 子；当前轮到{turn}；当前可落子点数 {len(game.legal_moves())}。"
                "黑白棋落子需夹住对方棋子，不能只按当前棋子数量判断胜负。"
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
    ) -> tuple[GameRoom, bool, bool, bool]:
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
            return room, False, False, False
        room = rooms[0]
        # 同一种游戏：直接复用原房间，避免同一群重复开房
        if room.game_type == game_type:
            await self._ensure_public_access()
            return room, True, False, False
        # 不同游戏：先看旧房间是否还有人占用
        if self._room_in_use(room):
            # 还有人在玩 → 不抢原房间链接，另起一个新房间（受「全部群聊合计最大房间数」约束）
            if game_type == "xiangqi":
                await self.xiangqi_engine.ensure_ready()
            new_room = await self._create_room_from_event(
                event,
                difficulty,
                game_type,
                turtle_soup_mode,
                requested_admin_room=requested_admin_room,
            )
            return new_room, False, False, False
        # 旧房间空闲（没销毁也没人玩）→ 直接切换游戏类型，沿用原链接
        _ = confirm_abandon
        await self.manager.switch_game(room, game_type, force=True)
        if game_type == "turtle_soup":
            room.turtle_soup_mode = turtle_soup_mode
        room.difficulty = difficulty
        await self._ensure_public_access()
        return room, True, False, True

    @staticmethod
    def _room_in_use(room: GameRoom) -> bool:
        """判断房间是否「还有人在用」：进行中的对局、单人玩家占用、已落座的真人玩家都算。

        用于群聊唤起另一种游戏时的分流：占用中 → 另开新房；空闲 → 切换游戏类型复用链接。
        """
        if room.status in {"active", "paused"}:
            return True
        if room.player_token:
            return True
        multiplayer = getattr(room, "multiplayer", None)
        if multiplayer is not None:
            for seat in getattr(multiplayer, "seats", None) or []:
                if not getattr(seat, "is_ai", False) and getattr(
                    seat, "visitor_token", ""
                ):
                    return True
        return False

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

    def _resolve_access_base(self) -> str:
        """解析并缓存房间访问地址的 base（协议+主机+端口）。

        首次解析成功后固定下来，后续直接复用：这样管理台/群里展示的房间链接不会
        因为 Quick Tunnel 短暂抖动、`ready` 标记翻转或局域网探测在多次调用间返回
        不同结果而「自己变来变去」。只有隧道重建（恢复）时才清缓存、换新地址。
        """
        cached = getattr(self, "_access_base_cache", "")
        if cached:
            # 缓存可能指向已经失效的隧道域名：只要隧道换了地址就必须重算，
            # 否则长期给用户发「打不开的旧链接」（重建路径之外的隧道切换不会清缓存）。
            tunnel_url = str(getattr(self.quick_tunnel, "url", "") or "").rstrip("/")
            configured = self._configured_access_base()
            if configured or not tunnel_url or tunnel_url in cached:
                return cached
            self._access_base_cache = ""
        base = self._configured_access_base() or (
            self.quick_tunnel.url if bool(getattr(self.quick_tunnel, "ready", False)) else ""
        ) or self._local_access_base(allow_unresolved=True)
        if base:
            self._access_base_cache = base
        return base

    def _room_url(self, room: GameRoom) -> str:
        base = self._resolve_access_base()
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
        if event_name == "soup_turn_changed":
            # 严格轮次换了人：往群里播报「轮到谁」，能 @ 就 @ 那一位
            await self._announce_soup_turn(room, payload)
            return
        if event_name == "soup_host_judged":
            game = room.game
            if isinstance(game, TurtleSoupGame):
                # 玩家出题模式不走模型判定，之前的群同步挂在问答事件上，
                # 这类房间的问答一条都到不了群里。裁决落库就是「有结果了」的时刻。
                self._queue_soup_group_sync(room)
            if game.host_number and not game.finished:
                self._spawn(self._soup_bot_turn(room, game))
            return
        if event_name == "soup_generation_requested":
            if isinstance(room.game, TurtleSoupGame):
                self._spawn(self._prepare_turtle_soup_task(room, room.game))
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
            # 说出词条违规 → 群里禁言该玩家
            violated_qq = payload.get("violated_qq")
            if violated_qq and self.undercover_violated_mute_seconds:
                self._spawn(
                    self._mute_room_qq(
                        room, str(violated_qq), self.undercover_violated_mute_seconds, "说出词条违规"
                    )
                )
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
        if event_name == "undercover_autopilot":
            self._spawn(self._undercover_autopilot_tick(room))
            return
        if event_name == "game_started":
            self._capture_round_participants(room, reset=True)
            if room.player_identity_confirmed:
                self._notify_companion_activity(room, "updated")
            if room.game_type == "turtle_soup":
                # 海龟汤没有棋谱解说：题目就绪时把汤面/难度/次数发成一张开局卡到群里，
                # 群里的人不进网页也知道在玩什么。
                self._spawn(self._announce_soup_opening(room))
                return
            opening = self._opening_commentary_prompt(room)
            # 开场白本身算一次解说：把冷却时间从这里起算，
            # 免得玩家手速快时先收到「刚落子」的解说、再收到开场白，看起来像串了半拍。
            room.last_commentary_at = time.time()
            self._spawn(
                self._comment(
                    room,
                    opening,
                    # 开场白只需要棋局事实：跳过记忆检索，少一次 embedding/向量查询的等待
                    with_memory=False,
                )
            )
            return
        if event_name == "player_confirmed":
            self._capture_round_participants(room)
            self._notify_companion_activity(room, "started")
            return
        if event_name == "seats_changed":
            self._capture_round_participants(room)
            # 连战汇总：真人全走了就别再等空闲计时，立刻把攒下的几局合并播报掉
            self._flush_board_series_when_seats_empty(room)
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
            elif room.game_type == "weiqi":
                tactical_prompt = self._weiqi_commentary_prompt(room, payload)
            elif room.game_type == "othello":
                side = int(payload.get("color") or room.game.human_color)
                tactical = room.game.tactical_state(side)
                event_text = {
                    "major_capture": "刚发生了一次较大的翻子",
                    "win": "这一局刚刚结束",
                }.get(tactical)
                tactical_prompt = self._board_commentary_prompt(
                    room, payload, event_text
                )
            else:
                side = None
                tactical = room.game.tactical_state(side)
                event_text = {
                    "major_capture": "刚发生了一次重要吃子",
                }.get(tactical)
                tactical_prompt = self._board_commentary_prompt(
                    room, payload, event_text
                )
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
            # 网页和群里的问答一起攒批：网页那半段进群是新增信息，
            # 群里那半段放进去是为了让「合并聊天记录」里能看到完整流水
            self._queue_soup_group_sync(room)
            if int(payload.get("new_facts") or 0) > 0:
                self._spawn(
                    self._comment(
                        room,
                        "玩家刚通过提问触及了海龟汤的关键事实。请用当前人格简短回应，"
                        "不要透露汤底或任何尚未公开的线索。",
                    )
                )
            return
        if event_name == "soup_reveal_requested":
            self._spawn(self._announce_soup_reveal_vote(room, payload, kind="requested"))
            return
        if event_name == "soup_reveal_voted":
            self._spawn(self._announce_soup_reveal_vote(room, payload, kind="voted"))
            return
        if event_name == "soup_revealed":
            source = str(payload.get("source") or "")
            if source == "qq":
                # 群内有人附议（或群里的人的推理触发揭晓）→ 把完整汤底发到群
                self._spawn(self._announce_soup_reveal(room, with_solution=True))
            elif payload.get("reason") == "answer_limit":
                # 网页玩家的推理额度用完自动揭晓：群里说一声，但汤底不主动给
                # （他们没参与揭晓，想看答案进网页）。
                self._spawn(self._announce_soup_reveal(room, with_solution=False))
            return
        if event_name == "soup_answer_attempted":
            self._queue_soup_group_sync(room)
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
                # 网页玩家申请的提示也同步到群（跟随「问答同步到群的形态」）
                self._queue_soup_group_sync(room)
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
            # 谁是卧底失败方禁言：胜方外的存活/参与成员在群里禁言 failed_mute_seconds
            if (
                room.game_type == "undercover"
                and isinstance(room.game, UndercoverGame)
                and self.undercover_failed_mute_seconds
                and room.game.winner_camp
            ):
                loser_qqs: list[str] = []
                for seat in room.multiplayer.seats:
                    if seat.is_ai or not seat.qq:
                        continue
                    pl = self._uc_player_for_seat(room, seat)
                    if pl is not None and (pl.camp or "") != room.game.winner_camp:
                        loser_qqs.append(seat.qq)
                for qq in sorted(set(loser_qqs)):
                    self._spawn(
                        self._mute_room_qq(
                            room, qq, self.undercover_failed_mute_seconds, "失败方"
                        )
                    )
            # 自动播报：开启「自动播报」时局末自动发群通报（无需手动点按钮）
            if (
                room.game_type == "undercover"
                and self.group_announce_enabled
                and self.group_announce_auto
            ):
                self._spawn(self._auto_announce_undercover(room))
            # 海龟汤自动播报：只发汤面 + 提问 + 结果，不剧透汤底
            if (
                room.game_type == "turtle_soup"
                and self.turtle_soup_group_announce_enabled
                and self.turtle_soup_group_announce_auto
            ):
                self._spawn(self._auto_announce_soup(room))
            # 棋类自动播报（board 配置）：
            # - group_announce_auto_all：所有对局结束都自动播报（玩家 vs 玩家 / 玩家 vs 花火，谁赢都播）
            # - group_announce_auto：仅「和花火对局」且花火获胜时自动播报
            if (
                room.game_type in DUEL_GAMES
                and self.board_group_announce_enabled
            ):
                auto_all = self.board_group_announce_auto_all
                auto_huahuo_win = False
                if (
                    self.board_group_announce_auto
                    and room.multiplayer.enabled
                    and self.manager._ai_seat(room) is not None
                ):
                    winner_seat = self.manager._duel_winner_seat(room)
                    auto_huahuo_win = (
                        winner_seat is not None and winner_seat.is_ai
                    )
                if auto_all or auto_huahuo_win:
                    self._spawn(self._auto_announce_board(room))
            current_uid = str(getattr(room.game, "game_uid", "") or "")
            self._spawn(
                self._comment(
                    room,
                    f"{game_label}本局结果是：{self._bot_perspective_result(result)}。请用当前人格简短回应。",
                    expect_game_uid=current_uid,
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
            # 换游戏时旧玩法的连战先发出去，新玩法连战重新起算（第一局按单局通报）
            old_game = str(payload.get("from") or "")
            if old_game:
                await self._flush_board_series_on_game_switch(room, old_game)
            self._notify_companion_activity(room, "updated")
            return
        if event_name == "room_destroyed":
            self._notify_companion_activity(room, "ended")
            self._finalize_private_game_result(room)
            await self._record_room_memory(room)
            # 按 room_id 建的锁与后台任务句柄要一起清掉，否则长期运行会越攒越多
            self._uc_ai_lock.pop(room.room_id, None)
            # 未到期的问答速报队列跟着房间一起丢，否则会在房间没了之后再往群里发
            soup_sync = self.__dict__.get("_soup_sync_state")
            if isinstance(soup_sync, dict):
                soup_sync.pop(room.room_id, None)
            tasks = getattr(self, "_board_flush_tasks", None)
            if isinstance(tasks, dict):
                pending_task = tasks.pop(room.room_id, None)
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()

    @classmethod
    def _opening_commentary_prompt(cls, room: GameRoom) -> str:
        game = room.game
        if _commentary_duel_room(room):
            # 两位真人：花火只是旁观者，开场白不能把自己算进棋局
            mapping = _commentary_seat_map(room)
            first_side = next(
                (side for side in COMMENTARY_SIDE_ORDER if side in mapping), ""
            )
            first_name = mapping.get(first_side, "")
            facts = (
                "这是两位真人之间的对局，你不在棋盘上，也没有执任何一方，只负责旁观解说。"
                f"{_commentary_sides_text(room)}；由{first_name}先行。"
            )
            return (
                f"{cls._game_label(room.game_type)}开局事实：{facts}"
                "请严格依据这些事实，用当前人格简短自然地说一句开场话；"
                "用昵称或「执某方的一方」称呼两位玩家，不得说反执方与先后手，"
                "不要说「我」「我的对手」这类把自己算进棋局的话，也不要复述完整规则。"
            )
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
        elif isinstance(game, WeiqiGame):
            human = "黑" if game.human_color == GOMOKU_BLACK else "白"
            bot = "黑" if game.bot_color == GOMOKU_BLACK else "白"
            first = "玩家" if game.human_color == GOMOKU_BLACK else "Bot"
            facts = (
                f"{game.size} 路棋盘，黑贴 {game.komi} 目；玩家执{human}，Bot 执{bot}，"
                f"围棋固定由黑方先手，因此本局由{first}先行。"
            )
        elif isinstance(game, TicTacToeGame):
            human = "X" if game.human_mark == TICTACTOE_X else "O"
            bot = "X" if game.bot_mark == TICTACTOE_X else "O"
            first = "玩家" if game.human_mark == TICTACTOE_X else "Bot"
            facts = f"玩家执 {human}，Bot 执 {bot}；井字棋固定由 X 先手，因此本局由{first}先行。"
        elif isinstance(game, OthelloGame):
            human = "黑" if game.human_color == OTHELLO_BLACK else "白"
            bot = "黑" if game.bot_color == OTHELLO_BLACK else "白"
            first = "玩家" if game.human_color == OTHELLO_BLACK else "Bot"
            facts = (
                f"玩家执{human}，Bot 执{bot}；黑白棋固定由黑方先行，因此本局由{first}先行。"
                "双方每步需夹住并翻转至少一枚对方棋子，无合法步时自动跳过；"
                "棋盘填满或双方都无合法步时，棋子较多者获胜。"
            )
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
            if game.host_number:
                facts = (
                    f"本局由 {game.host_number} 号玩家出题并负责回答，"
                    "其他玩家和花火一起提问、一起猜。"
                )
            elif game.mode == "bot_host":
                facts = "新题已经准备完成，由 Bot 出题、玩家提问。"
            else:
                facts = "当前由玩家提供公开线索，Bot 负责提问和猜测。"
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
            if game.host_number:
                if game.bot_solved:
                    return "花火猜中了玩家的汤底"
                if game.solved:
                    return "有玩家猜中了汤底"
                return "出题人揭晓了汤底"
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
        # 两人对弈（五子棋 / 黑白棋 / 象棋）：胜负属于某个座位，不再套用「玩家 / Bot」文案
        if (
            room.game_type in {"gomoku", "othello", "xiangqi", "weiqi"}
            and room.multiplayer.enabled
            and sum(1 for seat in room.multiplayer.seats if not seat.is_ai) > 1
            and getattr(game, "winner", None)
        ):
            winner = getattr(game, "winner")
            if room.game_type == "xiangqi":
                winner_side = str(winner)
                winner_seat = next(
                    (
                        seat
                        for seat in room.multiplayer.seats
                        if seat.side == winner_side
                    ),
                    None,
                )
            else:
                winner_side = "black" if winner == GOMOKU_BLACK else "white"
                winner_seat = next(
                    (
                        seat
                        for seat in room.multiplayer.seats
                        if seat.side == winner_side
                    ),
                    None,
                )
            if winner_seat is not None:
                name = (
                    "花火"
                    if winner_seat.is_ai
                    else (winner_seat.display_name or "").strip()
                    or f"{winner_seat.number}号"
                )
                return f"{name}执{COMMENTARY_SIDE_LABELS.get(winner_side, '')}获胜"
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

    @staticmethod
    def _bot_perspective_result(text: str) -> str:
        """把对局结果文案里的内部标签换成花火（Bot）第一视角。

        解说走花火人格（system prompt 用「你 / 用户」），而结果文案里是
        「Bot / 玩家」，人称不一致会让 LLM 把胜负搞反（玩家输了却说成自己输了）。
        这里统一成「你（花火）/ 用户」视角，消除指代歧义。
        """
        if not text:
            return text
        return (
            text.replace("Bot 获胜", "你赢了")
            .replace("玩家获胜", "你输了")
            .replace("Bot ", "你")
            .replace("Bot", "你")
            .replace("玩家", "用户")
        )

    @classmethod
    def _private_result_summary(
        cls, room: GameRoom, payload: dict[str, Any]
    ) -> str:
        game = room.game
        side = ""
        # 两人对弈（五子棋 / 黑白棋）：按座位说执色，避免把真人对手写成「Bot」
        duel_seats = [
            seat
            for seat in room.multiplayer.seats
            if not seat.is_ai and seat.side
        ] if room.multiplayer.enabled else []
        if (
            isinstance(game, (GomokuGame, OthelloGame, XiangqiGame, WeiqiGame))
            and len(duel_seats) > 1
        ):
            parts = "、".join(
                f"{(seat.display_name or '').strip() or f'{seat.number}号'}"
                f"执{COMMENTARY_SIDE_LABELS.get(seat.side, seat.side)}"
                for seat in duel_seats
            )
            side = f"本局 {parts}；"
            if isinstance(game, OthelloGame):
                side += (
                    f"终局黑 {game.black_count} 子、白 {game.white_count} 子；"
                )
            elif isinstance(game, WeiqiGame) and game.result:
                side += (
                    f"终局数子黑 {game.result.get('black')} 目（含贴目 "
                    f"{game.result.get('komi')}）对白 {game.result.get('white')} 目；"
                )
        elif isinstance(game, GomokuGame):
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
        elif isinstance(game, OthelloGame):
            human = "黑" if game.human_color == OTHELLO_BLACK else "白"
            bot = "黑" if game.bot_color == OTHELLO_BLACK else "白"
            side = (
                f"本局玩家执{human}、Bot 执{bot}；终局黑 {game.black_count} 子、"
                f"白 {game.white_count} 子；"
            )
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
        """WebUI 发一条消息：只负责落库并立刻返回，花火的回复交给后台任务补齐。

        以前这个接口要等模型（最长 35 秒）才返回，而前端 12 秒就 abort——于是
        「消息其实已经记进房间了，界面却弹请求超时、把文字塞回输入框」。
        现在 HTTP 只做三件事：记录消息、挂上「正在打字」占位、把生成丢进后台；
        回复由每秒一次的轮询推给前端，模型再慢也只是多等一会儿，不会报错。
        """
        visitor, cleaned, is_player, is_current_player = (
            await self.manager.begin_room_chat(room, visitor_token, text)
        )
        room.bot_chat_pending += 1
        task = self._spawn(
            self._room_chat_turn(
                room,
                visitor,
                cleaned,
                is_player=is_player,
                is_current_player=is_current_player,
            )
        )
        chat_tasks = self.__dict__.setdefault("_chat_tasks", set())
        chat_tasks.add(task)

        def finish(finished: asyncio.Task) -> None:
            chat_tasks.discard(finished)
            room.bot_chat_pending = max(0, room.bot_chat_pending - 1)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 后台聊天失败不能拖垮房间
                logger.warning("[GameCompanion] 房间聊天后台处理失败：%s", exc)

        task.add_done_callback(finish)
        return {"action": "chat_pending", "reply": "", "pending": True}

    async def wait_room_chat_turns(self) -> None:
        """等所有在途的聊天后台任务结束（测试、关房前收尾用）。"""
        pending = [
            task
            for task in list(self.__dict__.get("_chat_tasks") or ())
            if not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _room_chat_turn(
        self,
        room: GameRoom,
        visitor: Visitor,
        cleaned: str,
        *,
        is_player: bool,
        is_current_player: bool,
    ) -> dict[str, Any]:
        """后台收尾一条聊天：串行执行游戏指令 / 生成回复，回复以 Bot 消息入库。

        同一间房仍然串行（room.chat_lock），保证「先问先答」，但不再把 HTTP 请求
        卡在模型上——聊天框提示超时的根因就在这里。
        """
        async with room.chat_lock:
            if room.status == "closed" or room.room_id not in self.manager.rooms:
                return {"action": "closed", "reply": ""}
            # 谁是卧底：当前发言玩家的聊天消息自动记作本轮发言（优先级高于控制命令）
            if (
                room.game_type == "undercover"
                and isinstance(room.game, UndercoverGame)
                and room.game.phase in {"speech", "pk"}
                and is_player
                and self._uc_number_for_visitor(room, visitor)
                == room.game.expected_speaker_number
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
            if (
                action in {"soup_question", "soup_answer", "soup_respond"}
                and not options.get("confident")
            ):
                # 只有「拿不准」的发言才请模型复核；带问号的问题与明确的推理
                # 直接进判定，结果才稳定（也少一次模型调用）。
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
                    if room.game_type == "turtle_soup":
                        game = room.game
                        if isinstance(game, TurtleSoupGame) and not game.finished:
                            # 多人房票数不够：只发起了表决，别说成「已揭晓」
                            votes = sum(
                                1 for value in game.reveal_votes.values() if value
                            )
                            required = soup_reveal_required(room)
                            reply = (
                                f"已发起揭晓（{votes}/{required}），要等其他玩家同意。"
                            )
                        else:
                            reply = "好，这一题就先揭晓到这里。"
                    elif room.game_type == "blackjack":
                        reply = "收到，这一手记为输。"
                    else:
                        reply = "收到，本局按你认输结束。"
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
        raw_answer_text = str(text or "").strip()
        if any(phrase in normalized for phrase in ("关闭房间", "结束房间", "销毁房间")):
            return "close", {}

        aliases: tuple[tuple[GameType, tuple[str, ...]], ...] = (
            ("turtle_soup", ("海龟汤",)),
            ("tictactoe", ("井字棋", "圈叉棋")),
            ("othello", ("黑白棋", "奥赛罗", "奥赛罗棋", "黑白子")),
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
        # 答案识别分「强标记 / 弱猜词」两档，避免漏判玩家五花八门的答法。
        # 强标记：语义确定的答案提交，无条件视为答案。
        strong_answer_words = (
            "答案是",
            "我的答案是",
            "我答案是",
            "我猜答案",
            "正确答案是",
            "最终答案是",
            "完整答案",
            "完整推理",
            "真相是",
            "真相就是",
            "谜底是",
            "谜底就是",
            "揭晓答案",
            "公布答案",
        )
        # 弱猜词：玩家常用的猜测式表达，通常也算答案，但在疑问语气下回落为提问。
        weak_guess_words = (
            "我猜是",
            "我认为是",
            "我觉得是",
            "应该是",
            "我想是",
            "应该是是",
            "推测是",
            "推理是",
            "推理结果是",
            "我推理出",
            "想想是",
        )
        strong_answer = bool(
            any(word in normalized for word in strong_answer_words)
            or bool(re.match(r"^答[：:\s]", raw_answer_text))
            or normalized.startswith("答案")
        )
        raw_text = str(text or "").strip()
        interrogative = (
            bool(re.search(r"[?？]", raw_text))
            or normalized.endswith(("吗", "呢", "吧", "嘛"))
            or any(
                q in normalized
                for q in ("是不是", "是否", "有没有", "会不会", "能否", "为什么")
            )
        )
        weak_guess = any(word in normalized for word in weak_guess_words) and not interrogative
        if strong_answer or weak_guess:
            return "soup_answer", {"confident": True}
        if (
            room.status == "active"
            and len(raw_text) >= 2
        ):
            # 内容有信息量的发言交给统一判定：既可能是“是/否”问题，也可能已经
            # 把真相说完整（此时 LLM 会在同一次判定里返回 solved=true 判定答对），
            # 无需玩家刻意加“答案/真相”等字样。太短的语气词仍走聊天。
            #
            # `confident`：带问号的句子在海龟汤房里就是提问，这是最明确的意图，
            # 不再交给复核模型改判。复核器以前时不时把「电梯门还能正常打开吗？」
            # 判成闲聊，于是同一句话一次进判定、一次被当成聊天回复，玩家看到的是
            # 花火一会儿说「能」一会儿说「否」——两个都不是判定结果。
            return "soup_question", {"confident": bool(interrogative)}
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
                room,
                system_prompt=system_prompt,
                prompt=prompt,
                timeout=15,
                provider=self._soup_judge_provider(room),
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
        # 默认拒绝：模型超时 / 返回非 JSON / 字段缺失时不能当「同意」处理——
        # 悔棋会直接改局面，宁可不退也不能凭空替玩家改棋。
        accepted = False
        reply = "这次先不退，接着下吧。"
        for candidate in re.findall(r"\{.*?\}", raw or "", re.DOTALL):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or "accept" not in data:
                continue
            accepted = data.get("accept") is True
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
            # 现实里出现过：玩家问「电梯上升时门还能打开吗」，消息被误判成闲聊，
            # 花火就顺着人格直接答「能」——绕过判定给了个可能错的答案，还带剧透风险。
            "玩家关于当前汤题的是/否提问由游戏判定流程回答，你不要替它给「是」或「否」，"
            "也不要猜测汤底；这类问题可以只说一句「这个我用提问来答」，或提醒他在提问框里问。"
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
            reply = await self._call_room_model(
                room, system_prompt=system_prompt, prompt=prompt, timeout=35
            )
            return _clean_emotion_tokens(reply)[:500]
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

    async def _comment(
        self,
        room: GameRoom,
        prompt: str,
        *,
        with_memory: bool = True,
        expect_game_uid: str = "",
    ) -> None:
        # 先举起「正在输入」：网页立刻显示占位并把轮询提速，
        # 否则从开局到第一句解说之间的 3~5 秒完全是死等。
        room.bot_typing_pending += 1
        try:
            text = await self._generate_persona_text(
                room, prompt, with_memory=with_memory
            )
        finally:
            room.bot_typing_pending = max(0, room.bot_typing_pending - 1)
        if not text or room.status == "closed":
            return
        # 生成期间玩家可能已经开了下一局：局号变了就丢弃这条过时解说，
        # 否则上一局的点评会冒到新对局里（频繁速战时尤其明显）。
        if expect_game_uid and (
            str(getattr(room.game, "game_uid", "") or "") != expect_game_uid
        ):
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
            # 玩家在房间里手动选过棋力 → 再战保持玩家选择，不被模型的
            # difficulty 改写（否则「选了简单，再来一局又变回普通」）。
            if not getattr(room, "difficulty_chosen_by_player", False):
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
                        room.add_message(
                            "system",
                            f"再来一局暂时无法开始（{exc}），房间已回到结束状态。",
                        )

    def _soup_deck(self) -> SoupDeck:
        """题库账本（懒初始化：方便单测里用 __new__ 造的最小实例）。"""
        deck = getattr(self, "soup_deck", None)
        if deck is None:
            deck = SoupDeck(Path(getattr(self, "data_dir", Path("."))) / "turtle_soup_deck.json")
            self.soup_deck = deck
        return deck

    def _soup_library(self) -> SoupLibrary:
        """内置题库（懒初始化，方便单测里用 __new__ 造的最小实例）。"""
        library = getattr(self, "soup_library_store", None)
        if library is None:
            root = Path(__file__).resolve().parent
            library = SoupLibrary(
                root / "soup_library.json",
                Path(getattr(self, "data_dir", Path("."))) / "soup_library_cache.json",
                overrides_path=(
                    Path(getattr(self, "data_dir", Path(".")))
                    / "soup_library_overrides.json"
                ),
            )
            self.soup_library_store = library
        return library

    def _soup_custom(self) -> CustomSoupStore:
        """玩家私聊投稿的题库（懒初始化，方便单测里用 __new__ 造的最小实例）。"""
        store = getattr(self, "soup_custom_store", None)
        if store is None:
            store = CustomSoupStore(
                Path(getattr(self, "data_dir", Path(".")))
                / "turtle_soup_custom.json"
            )
            self.soup_custom_store = store
        return store

    def _soup_generated(self) -> GeneratedSoupStore:
        """模型出过的题（懒初始化）：管理台可看可改，出题时优先复用。"""
        store = getattr(self, "soup_generated_store", None)
        if store is None:
            store = GeneratedSoupStore(
                Path(getattr(self, "data_dir", Path(".")))
                / "turtle_soup_generated.json"
            )
            self.soup_generated_store = store
        return store

    def _soup_library_enricher(self, room: GameRoom):
        """给题库的懒补全注入「用哪个模型」：跟出题用同一个 Provider。"""

        async def enrich(system_prompt: str, prompt: str) -> str:
            return await self._call_room_model(
                room,
                system_prompt=system_prompt,
                prompt=prompt,
                timeout=30,
                provider=self._soup_generation_provider(room),
            )

        return enrich

    def _pick_fallback_puzzle(
        self,
        content_level: str,
        deck: SoupDeck,
        excluded: set[str],
    ):
        """本地兜底：优先没出过的；全都出过就挑「最久以前出过」的那道。

        旧实现「全部排除后随机」会导致刚玩过的题立刻再来一次，正是重复感的来源。
        """
        candidates = all_fallback_puzzles(content_level=content_level)
        if not candidates:
            raise ValueError("内置题库为空")
        fresh = [
            puzzle
            for puzzle in candidates
            if puzzle.signature not in excluded and not deck.has(puzzle)
        ]
        if fresh:
            return random.choice(fresh)
        ranked: list[tuple[float, object]] = []
        for puzzle in candidates:
            record = deck.find_duplicate(puzzle)
            ranked.append((float(record.get("at") or 0.0) if record else 0.0, puzzle))
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    async def _prepare_turtle_soup(self, room: GameRoom, game: TurtleSoupGame) -> None:
        """出题闭环：自定义投稿 →（可选）往期生成 → 网络题库 / 内置题库 → 往期生成
        → LLM 生成 → 兜底题。

        各段的共同约束是「不能和账本里的题目实质重复」，且任一环节成功就记账，
        让下一次出题（不论哪个房间、是否重启）都能看到它。

        自定义投稿排最前：玩家私聊投的题如果总被两百多道内置题挤掉，投了也白投。
        内置题库放在 LLM 之前：现成的题出得快也更省调用，挑不出新题时才让模型现编。
        预生成池（`turtle_soup.library_priority = generated_first`）可以提到最前：
        题是后台提前备好的，命中即出，等于用「慢的时候多花一次调用」换「每次都秒开」。
        """
        if game.mode != "bot_host":
            return
        deck = self._soup_deck()
        excluded = set(room.turtle_soup_recent_signatures) | set(
            deck.recent_signatures(12)
        )
        last_error = ""
        priority = (
            str(getattr(self, "turtle_soup_library_priority", "builtin_first"))
            or "builtin_first"
        ).strip().lower()

        # ⓪ 自定义题目（玩家私聊投稿）：优先级最高
        try:
            custom_puzzles = [
                puzzle
                for puzzle in self._soup_custom().puzzles(game.content_level)
                if puzzle.signature not in excluded and not deck.has(puzzle)
            ]
        except (OSError, ValueError):
            custom_puzzles = []
        if custom_puzzles:
            puzzle = random.choice(custom_puzzles)
            owner = ""
            try:
                owner = self._soup_custom().owner_for_surface(puzzle.surface)
            except (OSError, ValueError):
                owner = ""
            if await self.manager.complete_turtle_soup_generation(
                room, game, puzzle, source="custom", note=owner
            ):
                deck.remember(puzzle, source="custom")
                logger.info(
                    "[GameCompanion] 海龟汤采用自定义题目：《%s》", puzzle.title
                )
                return

        # ① 预生成池优先：后台已经备好的新题，出题是瞬时的
        if priority == "generated_first":
            if await self._generated_soup_puzzle(room, game, deck, excluded):
                return

        # ② / ③ 网络题库与内置题库：谁先谁后由 `turtle_soup.library_priority`
        # 决定。内置题不用联网、出得快，网络题更新鲜，两种需求都有人要。
        prefer_builtin = priority == "builtin_first"
        loaders = (
            ("内置题库", self._builtin_soup_puzzle),
            ("网络题库", self._online_soup_puzzle),
        )
        if not prefer_builtin:
            loaders = tuple(reversed(loaders))
        for label, loader in loaders:
            try:
                if await loader(room, game, deck, excluded):
                    return
            except (RuntimeError, ValueError, OSError) as exc:
                last_error = f"{label}不可用：{exc}"
                logger.info("[GameCompanion] %s不可用，换下一种来源：%s", label, exc)

        # ④ 往期生成：模型以前出过、被存下来的题。能用就先用，省一次生成 + 一次校验；
        # 管理台改过的题也走这里，改完下一局就能生效。
        if priority != "generated_first":
            if await self._generated_soup_puzzle(room, game, deck, excluded):
                return

        # ⑤ LLM 生成（最多 3 次）+ 独立校验
        try:
            puzzle = await self._generate_soup_puzzle(
                room,
                content_level=game.content_level,
                difficulty=room.difficulty,
                excluded=excluded,
                deck=deck,
                attempts=3,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = str(exc)
            puzzle = None
        if puzzle is not None:
            # 无论是否成功落库都要收手：落库失败通常意味着这一局已经换题/结束了
            if await self.manager.complete_turtle_soup_generation(
                room, game, puzzle, source="llm"
            ):
                deck.remember(puzzle, source="llm")
                # 存下来：管理台能看能改，以后也能直接复用，不必每局都现编
                try:
                    self._soup_generated().add(puzzle)
                except (OSError, ValueError):
                    pass
            return

        # ⑥ 本地兜底题（按账本挑最久没出过的）
        try:
            puzzle = self._pick_fallback_puzzle(game.content_level, deck, excluded)
        except ValueError as exc:
            logger.warning("[GameCompanion] 海龟汤无可用兜底题：%s", exc)
            return
        applied = await self.manager.complete_turtle_soup_generation(
            room, game, puzzle, source="fallback"
        )
        if applied:
            deck.remember(puzzle, source="fallback")
            logger.warning(
                "[GameCompanion] Bot 出题连续失败，当前局使用内置兜底题《%s》: %s",
                puzzle.title,
                last_error or "模型不可用",
            )

    async def _generated_soup_puzzle(
        self, room: GameRoom, game: TurtleSoupGame, deck: Any, excluded: set[str]
    ) -> bool:
        """从「往期生成」存档里拿一道没出过的题并开局。命中返回 True。"""
        try:
            store = self._soup_generated()
            reused = [
                puzzle
                for puzzle in store.puzzles(game.content_level)
                if puzzle.signature not in excluded and not deck.has(puzzle)
            ]
        except (OSError, ValueError):
            reused = []
        if not reused:
            return False
        puzzle = random.choice(reused)
        if not await self.manager.complete_turtle_soup_generation(
            room, game, puzzle, source="generated"
        ):
            return False
        deck.remember(puzzle, source="generated")
        store.mark_used(puzzle.signature)
        logger.info("[GameCompanion] 海龟汤复用往期生成题：《%s》", puzzle.title)
        return True

    async def _generate_soup_puzzle(
        self,
        room: GameRoom,
        *,
        content_level: str,
        difficulty: str,
        excluded: set[str],
        deck: Any,
        attempts: int = 3,
    ) -> Any:
        """让模型现编一道题并通过独立自检；失败抛最后一次的错误。

        只负责「编 + 校验 + 去重」，不落库：正常出题要交给房间开局，
        预生成只要存进存档，两者共用这一段。
        """
        persona = await self._persona_prompt(room)
        last_error = ""
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                system_prompt, prompt = generation_prompt(
                    difficulty=difficulty,
                    content_level=content_level,
                    recent_signatures=sorted(excluded),
                    recent_titles=deck.recent_titles(5),
                    recent_surfaces=deck.recent_surfaces(3),
                )
                if persona:
                    system_prompt = (
                        f"{persona}\n\n{system_prompt}\n"
                        "人格只影响叙事气质，不得把真实用户、私人记忆或生活场景写进题目。"
                    )
                raw = await self._call_room_model(
                    room,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    timeout=35,
                    provider=self._soup_generation_provider(room),
                )
                data = extract_json_object(raw)
                if data is None:
                    raise ValueError("出题结果不是有效 JSON")
                puzzle = puzzle_from_mapping(data, content_level=content_level)
                duplicate = deck.find_duplicate(puzzle)
                if duplicate is not None or puzzle.signature in excluded:
                    title = (duplicate or {}).get("title") or puzzle.title
                    raise ValueError(f"题目与已出过的《{title}》重复")
                check_system, check_prompt = validation_prompt(puzzle)
                # 自检与出题用同一个模型：判断模型可能更轻，未必查得出题目自洽问题
                check = await self._call_room_model(
                    room,
                    system_prompt=check_system,
                    prompt=check_prompt,
                    timeout=30,
                    provider=self._soup_generation_provider(room),
                )
                if not validation_passed(check):
                    raise ValueError("题目未通过独立自洽性校验")
                return puzzle
            except (RuntimeError, ValueError, OSError) as exc:
                last_error = str(exc)
                logger.info(
                    "[GameCompanion] 海龟汤第 %s 次出题未采用: %s",
                    attempt,
                    exc,
                )
        raise ValueError(last_error or "模型没有出出可用的题")

    def _soup_prefetch_enabled(self) -> bool:
        """备用题目标数为 0 即关闭预生成（不再单独放一个开关）。"""
        return int(getattr(self, "turtle_soup_prefetch_target", 1) or 0) > 0

    async def _prepare_turtle_soup_task(
        self, room: GameRoom, game: TurtleSoupGame
    ) -> None:
        """开局出题的后台任务：出完当前题，顺手把「下一题」备进存档。"""
        try:
            await self._prepare_turtle_soup(room, game)
        finally:
            self._schedule_soup_prefetch(room, game)

    def _schedule_soup_prefetch(self, room: GameRoom, game: TurtleSoupGame) -> None:
        """把「预生成下一题」丢到后台，绝不挡当前这一局。

        池子空了就得在现场等模型生成 + 自检（几十秒，还常被判重打回）；
        提前备一道，下一局（换房间也一样）直接命中「往期生成」，出题是瞬时的。
        已经有一道在生成中就不再排队，避免同时开几个房间时把生成模型打满。
        """
        if not self._soup_prefetch_enabled():
            return
        if getattr(game, "mode", "") != "bot_host":
            return
        running = getattr(self, "_soup_prefetch_task", None)
        if running is not None and not running.done():
            return
        content_level = str(getattr(game, "content_level", "normal") or "normal")
        difficulty = str(getattr(room, "difficulty", "normal") or "normal")
        try:
            self._soup_prefetch_task = self._spawn(
                self._soup_prefetch(room, content_level, difficulty)
            )
        except RuntimeError:
            # 事件循环正在关闭（插件重载/退出）：补货没意义，安静跳过
            self._soup_prefetch_task = None

    async def _soup_prefetch(
        self, room: GameRoom, content_level: str, difficulty: str
    ) -> None:
        """后台补货：让「没出过的生成题」保持 `prefetch_target` 道。

        任何失败都只记日志——这是锦上添花的事，不能影响正在进行的对局，
        也不能因为模型抖动就每局都重试到天亮。
        """
        try:
            target = max(0, int(getattr(self, "turtle_soup_prefetch_target", 1) or 0))
            if target <= 0:
                return
            store = self._soup_generated()
            deck = self._soup_deck()
            try:
                need = target - len(store.unused(content_level))
            except (OSError, ValueError):
                return
            if need <= 0:
                return
            excluded = set(deck.recent_signatures(12)) | store.signatures()
            for _ in range(min(need, 2)):
                try:
                    puzzle = await self._generate_soup_puzzle(
                        room,
                        content_level=content_level,
                        difficulty=difficulty,
                        excluded=excluded,
                        deck=deck,
                        attempts=2,
                    )
                except (RuntimeError, ValueError, OSError) as exc:
                    logger.info(
                        "[GameCompanion] 预生成海龟汤没成功（不影响当前对局）：%s", exc
                    )
                    return
                if store.add(puzzle):
                    excluded.add(puzzle.signature)
                    logger.info(
                        "[GameCompanion] 已预生成一道海龟汤备用：《%s》", puzzle.title
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台补货失败不许炸到游戏
            logger.debug("[GameCompanion] 预生成海龟汤异常: %s", exc)

    async def _online_soup_puzzle(
        self, room: GameRoom, game: TurtleSoupGame, deck: Any, excluded: set[str]
    ) -> bool:
        """网络题库：命中即用，省一次 LLM 生成 + 一次校验。没配地址就跳过。"""
        if not getattr(self, "turtle_soup_online_pool_url", ""):
            return False
        online = await fetch_online_puzzles(
            self.turtle_soup_online_pool_url,
            content_level=game.content_level,
            limit=12,
            timeout=12,
        )
        usable = [
            puzzle
            for puzzle in deck.would_repeat(online)
            if puzzle.signature not in excluded
        ]
        if not usable:
            return False
        puzzle = random.choice(usable)
        if not await self.manager.complete_turtle_soup_generation(
            room, game, puzzle, source="online"
        ):
            return False
        deck.remember(puzzle, source="online")
        logger.info("[GameCompanion] 海龟汤采用网络题库题目：%s", puzzle.title)
        return True

    async def _builtin_soup_puzzle(
        self, room: GameRoom, game: TurtleSoupGame, deck: Any, excluded: set[str]
    ) -> bool:
        """内置题库（soup_library.json）：先用「汤面完全相同」快速筛掉出过的，
        再对候选做一次语义判重。命中缓存直接用，没缓存就先让模型补全字段，
        补全失败自动退回规则版——不能让「模型不可用」变成「出不了题」。
        """
        if not getattr(self, "turtle_soup_library_enabled", True):
            return False
        library = self._soup_library()
        candidates = library.entries_for(game.content_level)
        if not candidates:
            return False
        recent_surfaces = deck.known_surfaces()
        fresh = [
            entry
            for entry in candidates
            if normalize_text(entry.surface) not in recent_surfaces
        ]
        pool = list(fresh or candidates)
        random.shuffle(pool)
        enricher = self._soup_library_enricher(room)
        for entry in pool[:3]:
            rule_puzzle = library.rule_puzzle(entry)
            if rule_puzzle is None:
                continue
            if rule_puzzle.signature in excluded or deck.has(rule_puzzle):
                continue
            puzzle = await library.puzzle_for(entry, enricher) or rule_puzzle
            if await self.manager.complete_turtle_soup_generation(
                room, game, puzzle, source="library"
            ):
                deck.remember(puzzle, source="library")
                logger.info(
                    "[GameCompanion] 海龟汤采用内置题库题目：《%s》", puzzle.title
                )
                return True
        return False

    async def _soup_bot_turn(self, room: GameRoom, game: TurtleSoupGame) -> None:
        """玩家出题人模式：花火作为普通参与者，节奏按在线玩家数自动换算。

        规则是「在线玩家各问一次，才轮到花火问一次」：3 个玩家在线就攒够 3 条
        玩家问答才行动，2 个就攒 2 条。所以人越多它越不抢话，不会出现玩家问一句
        它跟一句的交替。轮到它时自己决定提问还是推理；公开问答太少时只允许提问，
        免得开局就瞎猜。花火不知道汤底——它和玩家一样只能看汤面与已公开问答。
        """
        try:
            if not getattr(self, "turtle_soup_bot_join", True):
                return
            if (
                not game.host_number
                or game.finished
                or game.pending_judge
                or game.puzzle is None
                or room.status != "active"
            ):
                return
            # 一位在线玩家一票：攒够「人数」条玩家问答才轮到花火
            interval = max(1, int(self.manager._online_soup_players(room)))
            timeline = [
                entry for entry in game.entries if entry.kind in {"question", "answer"}
            ]
            last_bot = max(
                (
                    index
                    for index, entry in enumerate(timeline)
                    if not entry.player_number
                ),
                default=-1,
            )
            human_moves = [
                entry for entry in timeline[last_bot + 1 :] if entry.player_number
            ]
            if len(human_moves) < interval:
                return
            questions = [entry for entry in game.entries if entry.kind == "question"]
            # 线索太少时不许猜：继续提问比乱交一份推理更像「一起玩的人」
            can_guess = len(questions) >= 3
            history = "\n".join(
                f"- {'花火' if not entry.player_number else f'{entry.player_number} 号'}"
                f" 问：{entry.prompt} → {entry.response}"
                for entry in questions[-10:]
            )
            task = (
                "现在轮到你行动，你可以提一个问题，也可以提交一次完整推理。\n"
                + ("" if can_guess else "现在公开问答还太少，这一轮只能提问。\n")
                + '提问输出：{"action":"question","text":"……"}（不超过 60 字，必须是非问句，不能重复已问过的）\n'
                + (
                    '推理输出：{"action":"guess","answer":"……"}（不超过 120 字）\n'
                    if can_guess
                    else ""
                )
            )
            prompt = (
                "你在和一群玩家玩海龟汤，你是参与者「花火」，不是主持人。\n"
                f"汤面：{game.puzzle.surface}\n"
                f"已经问过的内容：\n{history or '（还没有）'}\n\n"
                + task
                + "只输出 JSON，不要解释。"
            )
            raw = await self._call_room_model(
                room,
                system_prompt="你是海龟汤玩家「花火」，只输出 JSON。",
                prompt=prompt,
                timeout=20,
            )
            data = extract_json_object(raw)
            if not isinstance(data, dict):
                return
            action = str(data.get("action") or "").strip().lower()
            if action == "guess":
                if not can_guess:
                    return
                answer = str(data.get("answer") or "").strip()
                if not answer:
                    return
                await self.manager.begin_soup_host_judgement(
                    room, answer, kind="answer", for_bot=True
                )
                return
            question = str(data.get("text") or "").strip()
            if not question:
                return
            if any(
                question in entry.prompt or entry.prompt in question
                for entry in questions
            ):
                return
            await self.manager.begin_soup_host_judgement(
                room, question, kind="question", for_bot=True
            )
        except Exception as exc:
            logger.info("[GameCompanion] 花火参与玩家出题的回合失败: %s", exc)

    async def submit_turtle_soup_question(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        # 玩家出题人：不经过 LLM，直接把问题挂到他面前等裁决
        host_game = (
            self.manager._turtle_soup_game(room)
            if room.game_type == "turtle_soup"
            else None
        )
        # 提问次数上限：只挡提问，不挡推理——次数用完后推理仍然可以提交，
        # 否则这局永远解不开。上限为 0 表示不限。
        limit = self._soup_question_limit(room)
        if (
            limit
            and host_game is not None
            and int(getattr(host_game, "player_question_count", host_game.question_count))
            >= limit
        ):
            progress = self._soup_progress(host_game, room)
            return {
                "limited": True,
                "verdict": None,
                "action": "",
                "reply": (
                    f"本局 {limit} 次提问已经用完。"
                    "还想继续就直接提交完整推理，或者发起「揭晓汤底」。"
                ),
                **progress,
            }
        if host_game is not None and host_game.host_number:
            await self.manager.begin_soup_host_judgement(
                room,
                text,
                kind="question",
                source=source,
                visitor_token=visitor_token,
                actor_qq=actor_qq,
            )
            return {
                "action": "awaiting_host",
                "verdict": None,
                "reply": "问题已提交，等出题人回答。",
                "question_count": host_game.question_count,
                **self._soup_progress(host_game, room),
            }
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
            raw = await self._call_judge_model(
                room,
                system_prompt=system_prompt,
                prompt=prompt,
                usable=question_judgment_usable,
                timeout=30,
                provider=self._soup_judge_provider(room),
                what="判断",
            )
            verdict, matched_facts, solved = parse_question_judgment(
                raw, fact_count=len(game.puzzle.key_facts)
            )
            if verdict == "compound":
                matched_facts.clear()
            if solved:
                # 玩家这句话本身已是完整真相：直接判对，无需出现“答案/真相”等字样。
                applied = await self.manager.resolve_turtle_soup_answer(
                    room,
                    game,
                    question,
                    solved=True,
                    source=source,
                    matched_facts=matched_facts,
                )
                if not applied:
                    raise RuntimeError("房间状态已经变化，请重新查看当前题目")
                return {
                    "solved": True,
                    "coverage": 1.0,
                    "reply": "推理正确，汤底已经揭晓。",
                    "solution": game.puzzle.solution,
                    **self._soup_progress(game, room),
                }
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
                **self._soup_progress(game, room),
            }
        except Exception as exc:
            await self.manager.cancel_turtle_soup_interaction(room, game, str(exc))
            if isinstance(exc, (ValueError, RuntimeError, PermissionError, OSError)):
                raise
            raise RuntimeError("Bot 暂时无法判断这个问题，请稍后重试") from exc

    def _soup_limits(self, room: GameRoom | None) -> dict[str, int]:
        """本局的提问/提示/推理次数：按难度档位，或按自定义配置。

        次数与难度联动后，这三个数字不能再从插件实例上直接取——同一进程里
        不同房间的难度可以不一样（一个娱乐局、一个困难局）。`room` 缺省时退回
        插件级数值，方便不依赖房间的调用方（例如只算展示文案）。
        """
        manager = getattr(self, "manager", None)
        if room is not None and manager is not None and hasattr(manager, "soup_limits"):
            return manager.soup_limits(room)
        return {
            "questions": max(0, int(getattr(self, "turtle_soup_max_questions", 15) or 0)),
            "hints": max(0, int(getattr(self, "turtle_soup_max_hints", 3) or 0)),
            "answers": max(0, int(getattr(self, "turtle_soup_max_answers", 8) or 0)),
        }

    def _soup_question_limit(self, room: GameRoom | None = None) -> int:
        """每局提问上限；0 表示不限。"""
        return int(self._soup_limits(room)["questions"])

    def _soup_progress(
        self, game: TurtleSoupGame | None, room: GameRoom | None = None
    ) -> dict[str, Any]:
        """网页与群聊共用的进度字段：还剩几次提问、几次提示、几次推理。

        以前群里只回一句判定，玩家看不见「问了几次、还剩几次」，人一多就会
        不停地问。这些字段同时进网页快照和群回复，两边数字永远一致。
        `question_left` / `answer_left` 为 None 表示本局不限制。
        """
        if game is None:
            return {
                "question_count": 0,
                "question_limit": 0,
                "question_left": None,
                "answer_count": 0,
                "answer_limit": 0,
                "answer_left": None,
                "hint_left": 0,
            }
        limit = self._soup_question_limit(room)
        # 只算玩家问的：花火的提问不该吃掉玩家的额度，否则「已问 15/15」里
        # 有一半是花火问的，玩家会以为自己被限得莫名其妙。
        asked = int(getattr(game, "player_question_count", game.question_count))
        # 推理额度与提问额度是两套：整局共享，用完自动揭晓
        answer_limit = self._soup_answer_limit(room)
        answered = int(getattr(game, "answer_attempts", 0) or 0)
        return {
            "question_count": asked,
            "question_limit": limit,
            "question_left": max(0, limit - asked) if limit else None,
            "answer_count": answered,
            "answer_limit": answer_limit,
            "answer_left": max(0, answer_limit - answered) if answer_limit else None,
            # 直接用游戏本身的额度：`max_hints=0` 表示不限，那时上限是提示条数，
            # 不能拿 0 去减，否则群里永远显示「提示剩 0」而网页还有提示。
            "hint_left": int(getattr(game, "hint_left", 0)),
        }

    def _soup_answer_limit(self, room: GameRoom) -> int:
        """每局推理上限；0 表示不限。与 manager 用同一个值，别各算一遍。"""
        return int(self._soup_limits(room)["answers"])

    @staticmethod
    def _soup_answer_solved(room: GameRoom, judgment: Any) -> bool:
        """按本局难度的「猜中条件」判断这次推理算不算解开。

        娱乐：完全还原 / 核心推理正确 / 部分正确都算过；
        简单、普通：核心推理正确以上；
        困难：必须完全还原，且覆盖度足够高才认。
        """
        policy = soup_solve_policy(getattr(room, "difficulty", "normal"))
        coverage = float(getattr(judgment, "coverage", 0) or 0)
        if bool(getattr(judgment, "solved", False)):
            return policy != "exact" or coverage >= 0.9
        threshold = {"partial": 0.45, "core": 0.7}.get(policy)
        return threshold is not None and coverage >= threshold

    @staticmethod
    def _soup_bonus_hint(
        game: TurtleSoupGame, judgment: Any, solved: bool = False
    ) -> str:
        """连续三次「很接近」却解不出时，主动送一条中等强度的提示。

        玩家反馈的死循环是：「一直是接近了、但还缺关键内容」，可模型不给方向、
        提示又要自己花次数申请。这里的闭环是：连续 3 次覆盖率达到 0.6 仍未解开，
        免费送一条题库中段的提示（最多一次），既推得动，也不至于直接泄底。
        """
        if solved or judgment.solved or game.puzzle is None:
            return ""
        if int(getattr(game, "auto_hints_sent", 0) or 0) >= 1:
            return ""
        history = list(game.coverage_history[-2:]) + [float(judgment.coverage or 0.0)]
        if len(history) < 3 or min(history) < 0.6:
            return ""
        hints = list(game.puzzle.hints)
        if not hints:
            return ""
        index = min(len(hints) - 1, max(0, len(hints) // 2))
        game.auto_hints_sent = int(getattr(game, "auto_hints_sent", 0) or 0) + 1
        return hints[index]

    async def submit_turtle_soup_answer(
        self,
        room: GameRoom,
        text: str,
        *,
        source: Literal["web", "qq"],
        visitor_token: str = "",
        actor_qq: str = "",
    ) -> dict[str, Any]:
        host_game = (
            self.manager._turtle_soup_game(room)
            if room.game_type == "turtle_soup"
            else None
        )
        if host_game is not None and host_game.host_number:
            await self.manager.begin_soup_host_judgement(
                room,
                text,
                kind="answer",
                source=source,
                visitor_token=visitor_token,
                actor_qq=actor_qq,
                limit=800,
            )
            return {
                "action": "awaiting_host",
                "solved": False,
                "reply": "推理已提交，等出题人判断。",
                "answer_attempts": host_game.answer_attempts,
                **self._soup_progress(host_game, room),
            }
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
            raw = await self._call_judge_model(
                room,
                system_prompt=system_prompt,
                prompt=prompt,
                usable=answer_judgment_usable,
                timeout=30,
                provider=self._soup_judge_provider(room),
                what="判定",
            )
            judgment = parse_answer_judgment_detail(
                raw,
                fact_count=len(game.puzzle.key_facts),
                discovered_facts=game.discovered_facts,
            )
            solved = self._soup_answer_solved(room, judgment)
            bonus_hint = self._soup_bonus_hint(game, judgment, solved=solved)
            reply = coverage_reply(
                judgment.coverage,
                judgment.missing,
                solved=solved,
                bonus_hint=bonus_hint,
            )
            applied = await self.manager.resolve_turtle_soup_answer(
                room,
                game,
                answer,
                solved=solved,
                source=source,
                matched_facts=judgment.matched,
                reply=reply,
                coverage=judgment.coverage,
                missing=judgment.missing,
            )
            if not applied:
                raise RuntimeError("房间状态已经变化，请重新查看当前题目")
            result: dict[str, Any] = {
                "solved": solved,
                "coverage": round(judgment.coverage, 2),
                "reply": reply,
                # 差在哪一环：前端可以直接展示，也可以只用来渲染进度条
                "missing": judgment.missing,
                **self._soup_progress(game, room),
            }
            if bonus_hint:
                result["bonus_hint"] = bonus_hint
            if solved:
                result["solution"] = game.puzzle.solution
            elif game.finished and str(getattr(game, "end_reason", "")) == "answer_limit":
                # 这次没答对，而且推理额度已经用完：manager 那边直接揭晓了，
                # 这里把汤底一并给出来，网页和群里都不用再点一次「查看汤底」。
                result["answer_limit_reached"] = True
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

    def _llm_gate(self) -> asyncio.Semaphore:
        """模型调用的并发闸门，按事件循环缓存。

        不能做成实例上的单个 Semaphore：测试里每个用例一个事件循环，
        Semaphore 复用会报「绑定到不同 loop」；长跑进程里也可能重建 loop。
        """
        loop = asyncio.get_running_loop()
        gates: dict[int, asyncio.Semaphore] = self.__dict__.setdefault(
            "_llm_gates", {}
        )
        key = id(loop)
        gate = gates.get(key)
        if gate is None:
            if len(gates) >= 8:
                gates.clear()  # 换过很多次 loop 后别无限增长
            gate = asyncio.Semaphore(max(1, int(self.LLM_MAX_CONCURRENCY)))
            gates[key] = gate
        return gate

    @classmethod
    def _is_rate_limited(cls, exc: BaseException) -> bool:
        text = f"{exc}".lower()
        return any(hint in text for hint in cls.LLM_RATE_LIMIT_HINTS)

    async def _call_room_model(
        self,
        room: GameRoom,
        *,
        system_prompt: str,
        prompt: str,
        timeout: int,
        provider: Any | None = None,
    ) -> str:
        provider = provider or self._configured_provider(room, "", label="")
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            raise RuntimeError("当前会话没有可用的大语言模型")
        gate = self._llm_gate()
        delays = (0.0, *self.LLM_RETRY_DELAYS)
        last_error: BaseException | None = None
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                await asyncio.wait_for(gate.acquire(), timeout=self.LLM_QUEUE_TIMEOUT)
            except asyncio.TimeoutError as exc:
                # 排队等太久：这时玩家已经等了 25 秒，再等下去只会更烦
                raise RuntimeError(
                    "问的人有点多，花火忙不过来，等十几秒再问一次吧"
                ) from exc
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                # 超时不重试：上游已经卡住了，再等一轮只是让玩家多等一个超时
                raise RuntimeError("模型响应超时") from exc
            except Exception as exc:
                last_error = exc
                if self._is_rate_limited(exc):
                    if attempt + 1 < len(delays):
                        logger.info(
                            "[GameCompanion] 模型限流，%.1fs 后重试（第 %d 次）：%s",
                            delays[attempt + 1],
                            attempt + 1,
                            exc,
                        )
                        continue
                    raise RuntimeError("模型正在限流，等十几秒再试") from exc
                raise RuntimeError("模型调用失败") from exc
            finally:
                gate.release()
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text:
                return text
            # 空回复多半是上游抖动：留一次重试机会，别直接把「没有返回」甩给玩家
            last_error = RuntimeError("模型没有返回有效内容")
        raise last_error or RuntimeError("模型没有返回有效内容")

    async def _call_judge_model(
        self,
        room: GameRoom,
        *,
        system_prompt: str,
        prompt: str,
        usable: Any,
        timeout: int = 30,
        provider: Any | None = None,
        what: str = "判断",
    ) -> str:
        """调用海龟汤判定模型，输出明显跑偏时重试一次。

        推理型模型偶尔不按约定输出 JSON，而是把思考过程整段吐出来：
        「让我想想……汤底里提到灯塔，所以这个问题……」。旧逻辑解析不到 JSON 就
        静默当成 `irrelevant` / `wrong`——玩家明明问在点上，却被告知「无关」，
        还白扣一次提问额度。

        这里宁可多花一次调用，也不把跑偏的输出当成真判定：
        重试一次仍不像 JSON 就抛错，由上层取消本次交互（不计数），并告诉玩家
        「这次没判出来，不算次数」。比悄悄判错划算。
        """
        raw = await self._call_room_model(
            room,
            system_prompt=system_prompt,
            prompt=prompt,
            timeout=timeout,
            provider=provider,
        )
        if usable(raw):
            return raw
        logger.info(
            "[GameCompanion] 海龟汤%s输出不是约定格式，重试一次：%s",
            what,
            raw[:200],
        )
        raw = await self._call_room_model(
            room,
            system_prompt=system_prompt,
            prompt=prompt + JUDGE_RETRY_SUFFIX,
            timeout=timeout,
            provider=provider,
        )
        if usable(raw):
            return raw
        logger.warning(
            "[GameCompanion] 海龟汤%s重试后仍不符合格式，按「没判出来」处理：%s",
            what,
            raw[:200],
        )
        raise RuntimeError(
            f"花火这次没{what}出来（模型把话说跑偏了），这句话不算次数，再说一次试试"
        )

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

    async def _generate_persona_text(
        self, room: GameRoom, prompt: str, *, with_memory: bool = True
    ) -> str:
        provider = self._commentary_provider(room)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return ""
        # 人格读取与记忆检索互不依赖，并发跑：串行等待会让模型调用前的空转翻倍，
        # 开局第一句就会被这段等待拖成「慢半拍」。
        if with_memory:
            persona, memory = await asyncio.gather(
                self._persona_prompt(room), self._memory_context(room, prompt)
            )
        else:
            persona = await self._persona_prompt(room)
            memory = ""
        companion_scene = self._companion_scene_prompt(room)
        role_constraint = ""
        if isinstance(room.game, DrawGuessGame):
            role_constraint = (
                "你画我猜中的角色固定为：用户始终负责作画，你（Bot）始终负责看图猜答案。"
                "你没有参与绘画，任何时候都不得声称自己画得好或不好。"
            )
        if _commentary_duel_room(room):
            scene_line = (
                f"你正在旁观两位真人玩{self._game_label(room.game_type)}，"
                "你不在棋盘上，也没有执任何一方，只负责解说。"
            )
            role_constraint += (
                "这盘棋是两位真人之间的对局：不要说自己下了哪一步，"
                "不要替任何一方认输、宣称自己要赢，也不要把其中一位称作「你的对手」；"
                "用昵称或「执黑的一方」这样的第三人称称呼双方。"
            )
        else:
            scene_line = (
                f"你正在与用户通过花火陪你玩 WebUI 玩{self._game_label(room.game_type)}。"
            )
        system_prompt = (
            f"{persona}\n\n{companion_scene}\n\n{memory}\n\n"
            f"{scene_line}保持原有人格和关系语气，"
            "只回应当前游戏事件，不输出规则说明或格式标签。"
            "直接说你要说的那句话：不要以「角色名：」「姓名：」这类标签开头，"
            "也不要复述人格设定里的字段。"
            f"{role_constraint}海龟汤中绝不能猜测或泄露尚未公开的汤底。"
        ).strip()
        kwargs: dict[str, Any] = {"prompt": prompt, "system_prompt": system_prompt}
        try:
            response = await asyncio.wait_for(
                provider.text_chat(**kwargs),
                timeout=30,
            )
        except Exception as exc:
            logger.debug("[GameCompanion] 生成人格化游戏回复失败: %s", exc)
            return ""
        text = str(getattr(response, "completion_text", "") or "")
        return _clean_emotion_tokens(_strip_role_prefix(text).strip())[:500]

    def _configured_provider(self, room: GameRoom, provider_id: str, *, label: str):
        """按配置取 Provider：找不到就回退会话模型，再回退默认模型。

        解说、海龟汤出题、海龟汤判断共用这一套回退逻辑，避免三份实现各写一遍
        而行为不一致（尤其是「管理台房间没有会话」的情况）。
        """
        provider_id = (provider_id or "").strip()
        if provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                provider = getter(provider_id)
                if provider is not None:
                    return provider
                logger.warning(
                    "[GameCompanion] %s Provider %s 不存在，回退当前会话模型",
                    label or "指定",
                    provider_id,
                )
        try:
            provider = self.context.get_using_provider(room.session_id)
        except Exception:
            provider = None
        if provider is not None:
            return provider
        # 管理台手动开的房间没有真实会话，取不到会话 Provider 时退回默认 Provider
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _commentary_provider(self, room: GameRoom):
        """解说使用的模型：优先全局配置的 Provider，缺失时回退会话模型。"""
        return self._configured_provider(
            room, getattr(self, "commentary_provider_id", ""), label="解说"
        )

    def _soup_generation_provider(self, room: GameRoom):
        """海龟汤出题模型：可指定更擅长推理的模型，留空跟随会话模型。"""
        return self._configured_provider(
            room,
            getattr(self, "turtle_soup_generation_provider_id", ""),
            label="海龟汤出题",
        )

    def _soup_judge_provider(self, room: GameRoom):
        """海龟汤判断模型：判定提问/答案，通常选响应更快的模型。"""
        return self._configured_provider(
            room,
            getattr(self, "turtle_soup_judge_provider_id", ""),
            label="海龟汤判断",
        )

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

        actor_side = "black" if color == GOMOKU_BLACK else "white"
        opponent_side = "white" if actor_side == "black" else "black"
        actor_name = _commentary_side_name(room, actor_side)
        opponent_name = _commentary_side_name(room, opponent_side)
        actor_label = COMMENTARY_SIDE_LABELS[actor_side]
        opponent_label = COMMENTARY_SIDE_LABELS[opponent_side]
        black_stones = sum(
            cell == GOMOKU_BLACK for board_row in game.board for cell in board_row
        )
        white_stones = sum(
            cell == GOMOKU_WHITE for board_row in game.board for cell in board_row
        )
        turn = _commentary_turn_text(room)
        consequence = (
            f"刚才这步留下了 {len(threat.winning_points)} 个下一手即可连成五子的空位，"
            f"{opponent_name}下一手无法全部封住。"
            if threat.kind == "multiple"
            else f"刚才这步留下了 1 个下一手即可连成五子的空位，{opponent_name}下一手仍可封住。"
        )
        return (
            f"五子棋刚发生了一个值得回应的节点：{actor_name}（执{actor_label}）"
            f"在第 {row + 1} 行第 {column + 1} 列落子；"
            f"对手是{opponent_name}（执{opponent_label}）。"
            f"当前黑方 {black_stones} 颗、白方 {white_stones} 颗，{turn}。"
            f"{consequence}只围绕这一步和当前情绪，用当前人格简短自然回应；"
            "避免使用专业棋型名称，不要虚构其他落子或胜负。"
        )

    @staticmethod
    def _weiqi_commentary_prompt(
        room: GameRoom, payload: dict[str, Any]
    ) -> str:
        """围棋：只在提子 / 停一手这类节点说话，并带上实时胜率与目差。"""
        game = room.game
        if not isinstance(game, WeiqiGame):
            return ""
        try:
            color = int(payload.get("color") or 0)
        except (TypeError, ValueError):
            color = 0
        if color not in (GOMOKU_BLACK, GOMOKU_WHITE):
            return ""
        actor_side = "black" if color == GOMOKU_BLACK else "white"
        opponent_side = "white" if actor_side == "black" else "black"
        actor = _commentary_side_name(room, actor_side)
        opponent = _commentary_side_name(room, opponent_side)
        actor_label = COMMENTARY_SIDE_LABELS[actor_side]
        opponent_label = COMMENTARY_SIDE_LABELS[opponent_side]
        eval_data = payload.get("eval")
        if not isinstance(eval_data, dict):
            eval_data = game.evaluate()
        black_rate = eval_data.get("black")
        white_rate = eval_data.get("white")
        lead = float(eval_data.get("lead") or 0)
        lead_text = (
            f"黑方领先约 {abs(lead):.1f} 目"
            if lead > 0.4
            else f"白方领先约 {abs(lead):.1f} 目"
            if lead < -0.4
            else "双方大致持平"
        )
        if payload.get("passed"):
            event = f"{actor}（执{actor_label}）选择停一手"
        else:
            captured = int(payload.get("captured") or 0)
            if captured <= 0:
                return ""
            row = int(payload.get("row") or 0) + 1
            column = int(payload.get("column") or 0) + 1
            event = (
                f"{actor}（执{actor_label}）在第 {row} 行第 {column} 列落子，"
                f"一提吃掉了 {captured} 颗{opponent_label}子"
            )
        return (
            f"围棋关键节点：{event}；这是第 {len(game.history)} 手；"
            f"当前评估黑方胜率 {black_rate}%、白方胜率 {white_rate}%，{lead_text}。"
            f"只围绕这一手和当前形势，用当前人格简短自然回应；"
            "不要虚构未发生的落子或胜负，也不要报出评估里没有的数字。"
        )

    def _board_commentary_prompt(
        self, room: GameRoom, payload: dict[str, Any], event_text: str | None
    ) -> str | None:
        """黑白棋 / 象棋的关键节点提示：补上执方、刚走棋的人与轮次。"""
        if not event_text:
            return None
        facts = _commentary_facts(room, payload)
        head = f"{self._game_label(room.game_type)}关键节点：{event_text}"
        body = f"{head}；{facts}。" if facts else f"{head}。"
        return (
            f"{body}只围绕这一步和当前局面，用当前人格简短自然回应；"
            "不要虚构未发生的走子、吃子或胜负。"
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

    # 网页问答同步到群：单条发会刷屏，攒一小段再合并成一条
    SOUP_SYNC_WINDOW = 12.0
    SOUP_SYNC_MAX_LINES = 4
    SOUP_SYNC_MAX_NODES = 12

    def _seat_qq_for_number(self, room: GameRoom, number: int) -> str:
        """按座位号找这个玩家的 QQ（没绑定 / 观众席返回空串）。"""
        if not number or not room.multiplayer.enabled:
            return ""
        for seat in room.multiplayer.seats:
            if int(seat.number) != int(number):
                continue
            qq = str(getattr(seat, "qq", "") or "").strip()
            if qq.isdigit():
                return qq
            visitor = room.visitors.get(seat.visitor_token)
            qq = str(getattr(visitor, "qq", "") or "").strip()
            return qq if qq.isdigit() else ""
        return ""

    def _queue_soup_group_sync(self, room: GameRoom) -> None:
        """把刚发生的一条问答攒进队列，过几秒合并成一条群消息。

        为什么不在每次提问后立刻发：一个房间里五个人轮流问，一分钟就是十几条，
        群里会被刷屏（这也是之前干脆不发的原因）。这里攒 12 秒，然后用 QQ 的
        **合并聊天记录**发出去——一条折叠消息里是完整的问答流水，不占屏也不会漏。

        网页和群里的条目一起收：只发网页那半段，群里的人看不到自己刚问的内容，
        对照起来会断片；但一整批里如果全是群里问的，就不发了（他们本来就看到了）。
        """
        if not getattr(self, "turtle_soup_qq_sync", True):
            return
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if getattr(room, "source", "") != "group" or not room.session_id:
            return
        game = room.game
        if not isinstance(game, TurtleSoupGame) or not game.entries:
            return
        entry = game.entries[-1]
        if entry.kind not in {"question", "answer", "hint"}:
            return
        # 揭晓 / 放弃生成的合成「结束」条目不是玩家问答：同步成「花火推理」会让人
        # 误以为有人又交了推理，还带一句「汤底见下方」却看不到汤底。
        if (
            entry.kind == "answer"
            and entry.verdict is None
            and entry.player_number is None
        ):
            return
        # 群里展示要防剧透：贴着汤底的片段先遮掉
        prompt = entry.prompt
        if game.puzzle is not None:
            prompt = redact_spoiler(prompt, game.puzzle)
        prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()
        # 不按字数截断：以前砍到 60 字，长问题在群里只剩半句、图上也被省略，
        # 读的人根本不知道问的是什么。图片会自己换行，合并记录也不怕长。
        seat_qq = self._seat_qq_for_number(room, int(entry.player_number or 0))
        avatar_url = _qq_avatar_url(seat_qq, size=100) if seat_qq.isdigit() else ""
        # 没有座位号的就是花火自己——玩家出题模式下它也是参与者，
        # 以前这里一律写「玩家」，群里会以为是自己人问的。
        who = f"{entry.player_number}号" if entry.player_number else "花火"
        from_qq = entry.source == "qq"
        if entry.kind == "hint":
            # 提示正文在 response 里（prompt 固定是「申请提示」）：群里贴提示内容
            hint_text = re.sub(r"\s+", " ", str(entry.response or "")).strip()
            head = "提示"
            label = f"{who}提示·{'群' if from_qq else '网页'}"
            line = f"· {who}申请了提示：{hint_text or '（无内容）'}"
            display_text = hint_text or "（无内容）"
            result_text = "💡"
            tone = "near"
        else:
            head = "提问" if entry.kind == "question" else "推理"
            label = f"{who}{head}·{'群' if from_qq else '网页'}"
            line = f"· {who}{'问' if entry.kind == 'question' else '推理'}：{prompt or '（内容为空）'} → {entry.response or '已记录'}"
            display_text = prompt or "（内容为空）"
            tone = {
                "yes": "yes",
                "no": "no",
                "irrelevant": "no",
                "partial": "near",
                "compound": "near",
            }.get(str(entry.verdict or ""), "info")
            # 判定结果简写：提问只给「是 / 否 / 无关 / 部分正确」，推理保留完整判定
            # 原文（「沾到边了，但还差致命机制」），群里一眼看懂又不啰嗦。
            verdict_label = VERDICT_LABELS.get(str(entry.verdict or ""), "")
            if entry.kind == "question":
                result_text = verdict_label or (entry.response or "已记录")
            else:
                result_text = entry.response or "已记录"

        state = self.__dict__.setdefault("_soup_sync_state", {})
        bucket = state.get(room.room_id)
        if bucket is None:
            bucket = {
                "room": room,
                "items": [],
                "seen": set(),
                "has_web": False,
                # 题目标题在入队时定下来：这 12 秒里玩家可能已经点了「再来一道」，
                # 等到 flush 再读 room.game 就把旧问答配到新题上了。
                "puzzle": (
                    game.puzzle.title
                    if getattr(game, "puzzle", None) is not None
                    else ""
                ),
            }
            state[room.room_id] = bucket
            self._spawn(self._flush_soup_group_sync(room.room_id))
        # 同一条记录只进队一次：两个事件（问答/推理）都可能触发到这里
        token = (round(float(entry.at or 0), 3), entry.kind, entry.prompt[:80])
        if token in bucket["seen"]:
            return
        bucket["seen"].add(token)
        bucket["items"].append(
            {
                "label": label,
                "text": line,
                "kind": entry.kind,
                "number": (
                    f"{entry.player_number}号" if entry.player_number else "花火"
                ),
                # 号码要留一份原始值：速报图按座位号配头像，显示文本带「号」没法用
                "number_value": int(entry.player_number or 0),
                "avatar_url": avatar_url,
                "source": "群里" if from_qq else "网页",
                "kind_cn": head,
                "prompt": display_text,
                "result": result_text,
                "tone": tone,
            }
        )
        if not from_qq:
            bucket["has_web"] = True

    def _soup_sync_chain(self, items: list[dict[str, str]], title: str):
        """把一批问答做成 QQ 合并聊天记录；环境不支持时返回 None。

        `Node` / `Nodes` 只在较新的 AstrBot 里才有，且不是所有平台适配器都支持
        合并转发，所以这里一律「拿不到就退回纯文本」，不能让同步功能因为环境
        差异直接哑掉。
        """
        try:
            from astrbot.api.message_components import Node, Nodes
        except ImportError:
            return None
        nodes = [Node(content=[Plain(title)], name="花火·海龟汤", uin="0")]
        for item in items[: self.SOUP_SYNC_MAX_NODES]:
            nodes.append(
                Node(content=[Plain(item["text"])], name=item["label"], uin="0")
            )
        extra = len(items) - self.SOUP_SYNC_MAX_NODES
        if extra > 0:
            nodes.append(
                Node(content=[Plain(f"还有 {extra} 条没列出来")], name="……", uin="0")
            )
        try:
            return MessageChain([Nodes(nodes)])
        except (TypeError, ValueError):
            return None

    SOUP_SYNC_IMAGE_ROWS = 6

    def _soup_sync_image_data(
        self,
        items: list[dict[str, Any]],
        puzzle: str,
        avatars: dict[int, str] | None = None,
    ) -> dict[str, Any]:
        """把一批问答整理成速报卡片的数据（只保留能上图的字段）。

        `avatars` 是「座位号 → 内联 data URI」；拿不到头像的座位退回号码圆牌。
        问句**不截断**：以前按 60 字砍，长问题在群里只剩半句，读起来像被打断。
        """
        shown = items[: self.SOUP_SYNC_IMAGE_ROWS]
        avatar_map = avatars or {}
        return {
            "count": len(items),
            "puzzle": str(puzzle or ""),
            "items": [
                {
                    "number": item.get("number") or "?",
                    "source": item.get("source") or "",
                    "kind": item.get("kind_cn") or "",
                    "text": item.get("prompt") or "",
                    "result": item.get("result") or "",
                    "tone": item.get("tone") or "info",
                    "avatar": avatar_map.get(
                        int(item.get("number_value") or 0), ""
                    ),
                }
                for item in shown
            ],
            "overflow": max(0, len(items) - len(shown)),
        }

    async def _soup_sync_avatars(self, items: list[dict[str, Any]]) -> dict[int, str]:
        """并发预取速报里要显示的 QQ 头像，转成内联 data URI。"""
        targets: list[tuple[int, str]] = []
        for item in items[: self.SOUP_SYNC_IMAGE_ROWS]:
            number = int(item.get("number_value") or 0)
            url = str(item.get("avatar_url") or "")
            if number and url:
                targets.append((number, url))
        if not targets:
            return {}
        results = await asyncio.gather(
            *[self._soup_avatar_bytes(url) for _number, url in targets],
            return_exceptions=True,
        )
        avatars: dict[int, str] = {}
        for (number, _url), raw in zip(targets, results):
            if isinstance(raw, bytes) and raw:
                avatars[number] = (
                    "data:image/png;base64,"
                    + base64.b64encode(raw).decode("ascii")
                )
        return avatars

    async def _soup_t2i(
        self,
        template: str,
        data: dict[str, Any],
        *,
        viewport_width: int = 720,
        viewport_height: int | None = None,
        scale: str = "ultra",
    ) -> str:
        """用 AstrBot 的 t2i 把一份 HTML 模板渲染成图片，返回 base64。

        `viewport_height` 用来「压扁」横条图：视口默认 720 高，内容只有几十像素时，
        full_page 会按 `max(视口高, 内容高)` 截出一整屏，底部全是空白。把视口高设得
        比内容还小，full_page 就按内容高度截，正好一条横条。
        """
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": viewport_width,
            "device_scale_factor_level": scale,
            "timeout": 20000,
        }
        if viewport_height:
            options["viewport_height"] = viewport_height
        url = await asyncio.wait_for(
            render(template, data, options=options), timeout=10
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    async def _soup_sync_t2i(self, data: dict[str, Any]) -> str:
        """把问答速报渲染成横条图片：压扁、缩短、不塞标题和脚注，消除底部空白。"""
        return await self._soup_t2i(
            _SOUP_SYNC_TMPL,
            data,
            viewport_width=440,
            viewport_height=60,
            scale="high",
        )

    # ---------------- 海龟汤：开局卡（群里也能看见题目） ----------------

    @staticmethod
    def _limit_text(value: int, suffix: str = " 次") -> str:
        return f"{int(value)}{suffix}" if int(value) > 0 else "不限"

    def _soup_opening_data(
        self, room: GameRoom, game: TurtleSoupGame
    ) -> dict[str, Any] | None:
        puzzle = getattr(game, "puzzle", None)
        if puzzle is None:
            return None
        limits = self._soup_limits(room)
        profile = soup_profile(room.difficulty)
        players = 0
        if room.multiplayer.enabled:
            players = len([seat for seat in room.multiplayer.seats if not seat.is_ai])
        return {
            "difficulty": str(profile.get("label") or "普通"),
            "name": str(puzzle.title or ""),
            "source": game.puzzle_source_label,
            "surface": puzzle.surface,
            "questions": self._limit_text(int(limits.get("questions") or 0)),
            "hints": self._limit_text(int(limits.get("hints") or 0)),
            "answers": self._limit_text(int(limits.get("answers") or 0)),
            "players": f"{players or 1} 人",
        }

    def _soup_opening_text(self, room: GameRoom, game: TurtleSoupGame) -> str:
        puzzle = getattr(game, "puzzle", None)
        if puzzle is None:
            return ""
        limits = self._soup_limits(room)
        profile = soup_profile(room.difficulty)
        source = game.puzzle_source_label
        lines = [
            f"🥣 海龟汤开局 · {profile.get('label') or '普通'}"
            + (f" · 题目来源：{source}" if source else ""),
            f"《{puzzle.title}》" if puzzle.title else "",
            puzzle.surface,
            "提问 "
            + self._limit_text(int(limits.get("questions") or 0))
            + " · 提示 "
            + self._limit_text(int(limits.get("hints") or 0))
            + " · 推理 "
            + self._limit_text(int(limits.get("answers") or 0)),
            "群里 @花火 直接问一句就是提问；「汤 提示」要提示，「汤 推理 你的推断」交推理。",
        ]
        return "\n".join(line for line in lines if line)

    async def _announce_soup_opening(self, room: GameRoom) -> None:
        """题目一出好就往群里发一张开局卡：汤面、难度、次数和玩法。

        以前群里只收到「房间链接」，不进网页就不知道在玩什么；群里开的局更是
        连题面都看不到。t2i 渲染失败就退回纯文字，绝不让这一局没声音。
        """
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if not getattr(self, "turtle_soup_qq_opening_card", True):
            return
        game = room.game
        if not isinstance(game, TurtleSoupGame):
            return
        if getattr(game, "opening_announced", False):
            return
        data = self._soup_opening_data(room, game)
        if data is None:
            return
        if not self.announce_target(room):
            return
        game.opening_announced = True
        try:
            # viewport_height 压到内容以下，full_page 就按内容高度截，去掉底部黑边
            b64 = await self._soup_t2i(_SOUP_OPEN_TMPL, data, viewport_height=100)
        except Exception as exc:
            logger.info("[GameCompanion] 开局卡渲染失败，回退纯文字：%s", exc)
            b64 = ""
        if b64 and await self._send_to_origin_image(room, "", b64):
            return
        text = self._soup_opening_text(room, game)
        if text:
            await self._send_to_origin(room, text)

    async def _announce_soup_reveal_vote(
        self, room: GameRoom, payload: dict[str, Any], *, kind: str
    ) -> None:
        """揭晓投票同步到群：发起 / 投票都喊一声，群友才知道能参与。

        网页端发起揭晓时群里完全没动静，群友根本不知道在表决，也就没法附议。
        这里只在群房里发，复用群聊互通开关（和问答同步一致）。
        """
        if getattr(room, "source", "") != "group":
            return
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if not self.announce_target(room):
            return
        votes = int(payload.get("votes") or 0)
        required = int(payload.get("required") or 0)
        actor = str(payload.get("actor") or "有玩家")
        if kind == "requested":
            left = max(0, required - votes)
            text = (
                f"🗳 {actor} 发起揭晓汤底（{votes}/{required}）。"
                f"还差 {left} 人：发「汤 附议」同意，或「汤 反对」否决。"
            )
        else:
            word = "同意" if bool(payload.get("agree")) else "不同意"
            text = f"🗳 {actor} {word}揭晓（{votes}/{required}）。"
        await self._send_to_origin(room, text)

    async def _announce_soup_reveal(self, room: GameRoom, *, with_solution: bool) -> None:
        """汤底揭晓时通知到群。

        群内有人附议（`source=qq`）才算「群里参与了揭晓」，这时才把完整汤底发到群；
        只有网页玩家揭晓时不主动给汤底，只说明「已揭晓、进网页看」，避免剧透没参与的
        群友。网页玩家的推理额度用完自动揭晓（answer_limit）也走后者。
        """
        if getattr(room, "source", "") != "group":
            return
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if not self.announce_target(room):
            return
        game = room.game
        if not isinstance(game, TurtleSoupGame) or game.puzzle is None:
            return
        if with_solution:
            text = f"🥣 汤底揭晓：\n{game.puzzle.solution}"
        else:
            text = "🥣 本局汤底已揭晓。"
            tail = self._soup_room_link_tail(room)
            if tail:
                text += "\n" + tail
        await self._send_to_origin(room, text)

    async def _announce_soup_turn(self, room: GameRoom, payload: dict[str, Any]) -> None:
        """严格轮次的「轮到谁提问」播报到群里，能 @ 就 @。

        只在群房里发：私聊房的「轮到 3 号」没有任何意义，还会在私聊里冒出一条
        莫名其妙的消息。网页上的玩家多半没绑 QQ，这时退化成带座位号的纯文本，
        不带 @（@ 一个空 QQ 会把整条消息吞掉）。
        """
        if room.source != "group":
            return
        if not getattr(self, "turtle_soup_qq_bridge", True):
            return
        if not getattr(self, "turtle_soup_qq_turn_announce", True):
            return
        number = int(payload.get("seat_number") or 0)
        if not number:
            return
        qq = str(payload.get("qq") or "").strip()
        name = str(payload.get("display_name") or "").strip()
        seconds = int(payload.get("seconds") or 0)
        tail = f" · {seconds} 秒内没动静就自动换下一位" if seconds else ""
        parts: list[Any] = [Plain("🥣 轮到 ")]
        if qq.isdigit() and len(qq) >= 5:
            try:
                from astrbot.api.message_components import At

                parts.append(At(qq=qq))
            except Exception:
                # 适配器没有 At 组件时退回名字，别让整条播报丢掉
                parts.append(Plain(name or f"{number} 号玩家"))
        else:
            label = f"{number} 号玩家" + (f"（{name}）" if name else "")
            parts.append(Plain(label))
        parts.append(Plain(f" 提问了{tail}"))
        target = self.announce_target(room)
        if not target:
            return
        await self._deliver(target, MessageChain(parts), what="轮次播报")

    async def _flush_soup_group_sync(self, room_id: str) -> None:
        """窗口到期后把攒下的网页问答同步到群里（默认纯文字，可切横条图）。"""
        await asyncio.sleep(self.SOUP_SYNC_WINDOW)
        state = self.__dict__.get("_soup_sync_state") or {}
        bucket = state.pop(room_id, None)
        if not bucket or not bucket["items"]:
            return
        if not bucket["has_web"]:
            # 全是群里自己问的，群里本来就看到了，不再折叠发一遍
            return
        room = bucket["room"]
        # 房间已经关掉/销毁就别再往那个群发了：队列是延迟 12 秒的，
        # 这期间玩家完全可能已经关房走人。
        if str(getattr(room, "status", "")) == "closed":
            return
        items: list[dict[str, str]] = list(bucket["items"])
        # 只同步网页那边问的：群里自己问的，群里本来就看得到
        web_items = [it for it in items if it.get("source") == "网页"]
        if not web_items:
            return

        style = (getattr(self, "turtle_soup_qq_sync_style", "text") or "text").lower()
        if style == "image":
            if await self._flush_soup_sync_image(room, web_items):
                return
            # 横条图失败退回纯文字
        text = self._soup_sync_text(web_items)
        await self._send_to_origin(room, text)

    def _soup_sync_text(self, items: list[dict[str, Any]]) -> str:
        """把一批网页问答拼成纯文字：一句一条，最省事也不刷屏。"""
        lines: list[str] = []
        for item in items:
            number = item.get("number") or "花火"
            if item.get("kind") == "hint":
                lines.append(f"网页玩家{number} 申请了提示：{item.get('prompt') or ''}")
            else:
                action = "提问" if item.get("kind") == "question" else "推理"
                lines.append(
                    f"网页玩家{number}{action}：{item.get('prompt') or ''} "
                    f"→ {item.get('result') or '已记录'}"
                )
        if len(lines) == 1:
            return lines[0]
        kinds = {item["kind"] for item in items}
        if kinds == {"question"}:
            head = f"网页那边刚问了 {len(items)} 条："
        elif kinds == {"answer"}:
            head = f"网页那边刚交了 {len(items)} 份推理："
        elif kinds == {"hint"}:
            head = f"网页那边刚申请了 {len(items)} 条提示："
        else:
            head = f"网页那边刚有 {len(items)} 条动态："
        return f"🥣 {head}\n" + "\n".join(lines)

    async def _flush_soup_sync_image(
        self, room: GameRoom, items: list[dict[str, Any]]
    ) -> bool:
        """把每条网页问答渲染成一条压扁的横条图，逐条发到群里。

        不再发「一张大卡片塞一堆问答」：那种图又高、底部又空，还容易刷屏。
        横条只留头像 + 网页玩家信息 + 提问 + 判定结果，宽 720、高几十像素。
        """
        sent_any = False
        for item in items[: self.SOUP_SYNC_MAX_LINES]:
            try:
                avatars = await self._soup_sync_avatars([item])
            except Exception as exc:
                logger.info("[GameCompanion] 问答速报头像拉取失败（用号码牌）：%s", exc)
                avatars = {}
            try:
                b64 = await self._soup_sync_t2i(
                    self._soup_sync_image_data([item], "", avatars)
                )
            except Exception as exc:
                logger.info("[GameCompanion] 问答速报横条渲染失败：%s", exc)
                continue
            try:
                if await self._send_to_origin_image(room, "", b64):
                    sent_any = True
            except Exception as exc:
                logger.info("[GameCompanion] 问答速报横条发送失败：%s", exc)
        return sent_any

    @staticmethod
    def _normalize_session(session: str) -> str:
        """把 `平台:group:123` 这类非标准写法换成 AstrBot 认的 MessageType。

        AstrBot 的会话字符串是「平台:MessageType:会话ID」，MessageType 只有
        GroupMessage / FriendMessage / OtherMessage 三个取值。写成小写 `group`
        会在发送前就被判成「不合法的 session 字符串」，消息根本发不出去
        （管理台开房绑群号时踩过：日志里的 `'group' is not a valid MessageType`）。
        """
        text = str(session or "").strip()
        parts = text.split(":")
        if len(parts) == 3 and parts[1] in {"group", "private"}:
            parts[1] = "GroupMessage" if parts[1] == "group" else "FriendMessage"
            return ":".join(parts)
        return text

    @staticmethod
    def _platform_meta_of(inst: Any) -> tuple[str, str]:
        """取适配器实例的 (id, name)。

        `id` 是这条适配器配置的唯一标识，`name` 是「aiocqhttp」这类适配器类型名
        （AstrBot 事件里的 `platform_meta.name` 就是它）。两者不是一回事：同一个
        aiocqhttp 适配器可以被用户改名，id 也可能是 `aiocqhttp-1` 这种带后缀的。
        """
        meta: Any = None
        try:
            meta = inst.meta() if callable(getattr(inst, "meta", None)) else getattr(
                inst, "meta", None
            )
        except Exception:  # noqa: BLE001 - 适配器自定义实现什么都可能抛
            meta = None
        pid = str(getattr(meta, "id", "") or "").strip()
        name = str(getattr(meta, "name", "") or "").strip()
        if not pid:
            pid = str(getattr(inst, "platform_id", "") or "").strip()
        if not name:
            name = str(getattr(inst, "platform_name", "") or "").strip()
        return pid, name

    def _platform_instances(self) -> list[Any]:
        manager = getattr(getattr(self, "context", None), "platform_manager", None)
        insts = getattr(manager, "platform_insts", None)
        try:
            return list(insts or ())
        except TypeError:
            return []

    def _platform_ids(self) -> list[str]:
        return [
            pid
            for pid, _ in (self._platform_meta_of(inst) for inst in self._platform_instances())
            if pid
        ]

    def _resolve_platform_id(self, preferred: str) -> str:
        """把会话串里的平台 id 换成本机真实存在的那个。

        AstrBot 的 `context.send_message` 是按 `platform.meta().id` 找适配器的
        （找不到只打一行 warning 然后 return False）。管理台建的房间没有来源
        事件，只能按适配器类型名猜 id；写死 `aiocqhttp` 时，用户改过 id、或同时
        挂了两条 QQ 适配器，消息就会死在
        `cannot find platform for session aiocqhttp:GroupMessage:xxx`。
        """
        available = [
            (pid, name)
            for pid, name in (self._platform_meta_of(inst) for inst in self._platform_instances())
            if pid
        ]
        if not available:
            # 拿不到注册表（老版本框架 / 单测最小实例）：保持原样，交给框架处理
            return preferred
        ids = [pid for pid, _ in available]
        if preferred and preferred in ids:
            return preferred
        # 明确指向别的适配器（qq_official / telegram 等）时不要挪到 QQ 上：
        # 发错平台比发不出去更糟，这里只兜「QQ 适配器改过 id」这一种情况。
        if preferred and preferred.strip().lower() not in {
            "", "aiocqhttp", "onebot", "admin", "web",
        }:
            return preferred
        for pid, name in available:
            lowered = name.lower()
            if "aiocqhttp" in lowered or "onebot" in lowered:
                return pid
        return preferred

    def _session_with_platform(self, session: str) -> str:
        """会话串校正：MessageType 合法 + 平台 id 是本机真实存在的那个。"""
        target = GameCompanionPlugin._normalize_session(session)
        parts = target.split(":")
        if len(parts) != 3:
            return target
        resolved = self._resolve_platform_id(parts[0])
        if resolved and resolved != parts[0]:
            parts[0] = resolved
            return ":".join(parts)
        return target

    def announce_problem(self, room: GameRoom) -> str:
        """播报发不出去的原因（空串代表可以发）。前端提示用，不再笼统说「没绑群」。"""
        target = self.announce_target(room)
        if not target:
            return "这个房间没有绑定 QQ 群，先在管理台绑定群号（或在群里开房）再通知"
        resolved = self._session_with_platform(target)
        ids = self._platform_ids()
        if ids and resolved.split(":")[0] not in ids:
            return (
                "没找到能发群的 QQ 适配器（当前已加载："
                + "、".join(ids)
                + "），请确认 QQ 平台已启用后再通知"
            )
        return ""

    @staticmethod
    def announce_target(room: GameRoom) -> str:
        """播报要发到哪个会话；发不出去时返回空串。

        群房直接用开房会话。管理台开的链接房 `session_id` 是 `admin:console`，
        根本没有对应的平台会话，往里发必然失败——这类房间靠管理台绑定的群号
        （room.group_id）拼出真实群会话。`platform` 为 admin/web 时按 aiocqhttp
        处理，因为管理台里能拿到群号的只有 QQ 适配器（真实 id 由
        `_session_with_platform` 在发送前校正）。

        拼出来的第二段必须是 `GroupMessage`（不是 `group`），否则 AstrBot 解析
        会话时就抛错，消息连发送环节都到不了。
        """
        session = str(getattr(room, "session_id", "") or "")
        group = str(getattr(room, "group_id", "") or "").strip()
        if session and not session.startswith(("admin:", "web:")):
            return GameCompanionPlugin._normalize_session(session)
        if not group:
            return ""
        platform = str(getattr(room, "platform", "") or "").strip()
        if not platform or platform in {"admin", "web"}:
            platform = "aiocqhttp"
        return f"{platform}:GroupMessage:{group}"

    async def _deliver(self, session: str, chain: Any, *, what: str = "消息") -> bool:
        """按真实平台 id 主动发一条消息，返回是否真的发出去了。

        平台不在本机时提前判定失败：`context.send_message` 遇到不存在的平台只会
        打一行 warning 再返回 False，调用方要是没看返回值，就会误写幂等标记，
        之后这一局再也播不出去（管理台开台通知就是这么丢的）。
        """
        target = self._session_with_platform(session)
        if not target:
            return False
        ids = self._platform_ids()
        platform = target.split(":")[0]
        if ids and platform not in ids:
            logger.warning(
                "[GameCompanion] %s没发出去：会话 %s 的平台「%s」不在本机适配器里（现有：%s）",
                what,
                target,
                platform,
                "、".join(ids),
            )
            return False
        try:
            sent = await self.context.send_message(target, chain)
        except Exception as exc:  # noqa: BLE001 - 平台侧异常不该打断游戏
            logger.warning("[GameCompanion] %s发送失败（%s）：%s", what, target, exc)
            return False
        if sent is False:
            logger.warning("[GameCompanion] %s没发出去：平台未找到会话 %s", what, target)
            return False
        return True

    async def _send_to_origin(self, room: GameRoom, text: str) -> bool:
        """把纯文字发到开房群，返回是否真的发出去了。

        返回值的意义：播报类调用方会按「有没有发出去」写幂等标记。以前这里
        吞掉异常且不返回结果，机器人在群里被踢/被禁言时，调用方照样认为发成功
        并把 `last_announced_uid` 写死——这一局之后再也播不出去。
        """
        if not text:
            return False
        target = self.announce_target(room)
        if not target:
            return False
        return await self._deliver(target, MessageChain([Plain(text)]), what="群消息")

    async def undercover_recap(self, room: GameRoom) -> str:
        """生成一局谁是卧底的短复盘摘要（结算卡「生成复盘」/「通知到群」共用）。

        按 game_uid 缓存：同局第一次请求才调 LLM，之后直接复用，避免重复请求。
        依据房间内的本局结果（胜方、阵营、词条、出局情况）生成 2-3 句要点；
        LLM 不可用或超时时返回本地兜底文案，保证始终有内容可展示。
        """
        game = room.game
        uid = str(getattr(game, "game_uid", "") or "") if game else ""
        cache = getattr(room, "recap_cache", None) or {}
        if uid and cache.get(uid):
            return cache[uid]
        gap = _undercover_game_summary(game)
        title = _undercover_title_text(game)
        if not gap and not title:
            text = self._undercover_fallback_recap()
        else:
            prompt = (
                "请为一局「谁是卧底」生成简短复盘（中文，2~4 句，轻松俏皮口吻，"
                "可以带点网络梗和适度 emoji，不要标题、不要编号、不要客套感谢语）。\n"
                f"结局：{title}\n对局情况：{gap or '（无额外明细）'}\n"
                "风格参考：像一起玩的朋友在茶余饭后聊这场对局，轻松幽默、善意调侃，"
                "绝不攻击或贬低任何玩家；点一下局里的名场面（谁最快暴露、谁演技在线、"
                "谁被冤出局），结尾可以随口开个无恶意的玩笑。"
            )
            text = await self._llm_gen_neutral(
                prompt,
                system_prompt=(
                    "你是古灵精怪但温和的复盘解说员「花火」，点评轻松俏皮、善意不伤人，"
                    "只用中文输出复盘正文，不要标题、不要编号。"
                ),
            )
            text = (text or self._undercover_fallback_recap()).strip()
        if uid:
            room.recap_cache[uid] = text
        return text

    @staticmethod
    def _undercover_fallback_recap() -> str:
        return (
            "这局水有点深呀～建议复盘下关键的几轮发言和投票，"
            "看看是谁演技炸裂、又是谁被冤出局的。"
        )

    async def undercover_announce_result(
        self, room: GameRoom, image_data: str = ""
    ) -> str:
        """把本局胜负（+复盘）发到开房群，返回已发送的文案。

        文案顺序：标题 → 词条 → 胜利方（列出玩家名）→ 失败方；
        复盘与图片都放在文字之后（📝 复盘跟在图片后面）。
        若开启图片发送（undercover.group_announce_style = image/both）且前端传来海报图，
        随文案一起发送；仅图片时只发图。仅在插件配置开启群通报时真正发送。
        """
        if not getattr(self, "group_announce_enabled", False):
            return ""
        game = room.game
        title = _undercover_title_text(game) or "本局已结束"
        winner = getattr(game, "winner", None) or {}
        winner_get = (
            (lambda key, _d=None: winner.get(key, _d)) if isinstance(winner, dict) else (lambda key, _d=None: getattr(winner, key, _d))
        )
        camp = winner_get("camp")
        cw = winner_get("civilian_word") or ""
        uw = winner_get("undercover_word") or ""
        # 文案：标题 → 词条 → 胜利方（列出玩家名）→ 失败方（不写惩罚）
        text = self._undercover_announce_text(game)
        # 结合「花火复盘」：共用同一局缓存；是否附带复盘由配置 group_announce_recap 控制
        recap = ""
        if getattr(self, "group_announce_recap", True):
            try:
                recap = await self.undercover_recap(room)
            except Exception:
                recap = ""
        style = (getattr(self, "group_announce_style", "text") or "text").lower()
        want_image = style in {"image", "both"}
        want_text = style in {"text", "both"}
        if not want_text and not want_image:
            want_text = True  # 兜底：未知配置按仅文字
        if style == "image":
            if image_data:
                # 纯图片模式：只发图（可选附带复盘）；附带复盘时图片前加一行标题，不发详情
                title_line = f"🕵️ 谁是卧底战报：{title}" if recap else ""
                sent = await self._send_to_origin_image(
                    room, title_line, image_data, recap
                )
                if not sent:
                    return ""
                # 未带复盘时也要返回非空标记：保证幂等判定与「已通知」提示正确
                return title_line or "🖼️ 战报图已发送"
            # 海报缺失/生成失败：回退纯文字，避免群里收不到通知
            sent = await self._send_to_origin(
                room, text + (f"\n📝 复盘：{recap}" if recap else "")
            )
            return text if sent else ""
        if want_image and image_data:
            sent = await self._send_to_origin_image(
                room, text if want_text else "", image_data, recap if want_text else ""
            )
            return text if sent else ""
        if want_text:
            sent = await self._send_to_origin(
                room, text + (f"\n📝 复盘：{recap}" if recap else "")
            )
            return text if sent else ""
        return text

    # ---------------- 海龟汤：群通知 / 分享战报 / 花火复盘 ----------------

    def _soup_result_payload(self, room: GameRoom) -> dict[str, Any] | None:
        """本局海龟汤快照（不带汤底）。

        播报是异步任务，玩家可能已经点了「再来一道」；届时 room.game 已换成新题，
        直接读游戏对象会播成新题的汤面。所以统一读 _finish_game 冻结的快照。
        """
        snap = getattr(room, "last_soup_result", None)
        if isinstance(snap, dict) and snap:
            return snap
        return None

    @staticmethod
    def _soup_pick_questions(
        payload: dict[str, Any], limit: int = 4
    ) -> list[dict[str, Any]]:
        """挑几条「有信息量」的提问：优先问出关键事实的，其次按时间取最近的。

        同一问题可能被不同玩家各问一遍（或同一人换个措辞再问），战报「他们问过」
        里重复出现会很啰嗦——按去空格去标点的正文去重，只保留最早那条。
        """
        entries = [
            entry
            for entry in (payload.get("entries") or [])
            if isinstance(entry, dict) and entry.get("kind") == "question"
        ]
        if not entries:
            return []
        strong = [
            entry
            for entry in entries
            if str(entry.get("verdict") or "") in {"yes", "partial", "compound"}
        ]
        picked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in reversed(strong or entries):
            if len(picked) >= limit:
                break
            prompt = re.sub(r"\s+", "", str(entry.get("prompt") or ""))
            # 去掉常见标点，让「和灯塔有关吗？」与「和灯塔有关吗」算同一问
            key = prompt.translate(str.maketrans("", "", "？?！!，,。.、：:"))
            if not key or key in seen:
                continue
            seen.add(key)
            picked.append(entry)
        return list(reversed(picked))

    @staticmethod
    def _soup_player_line(payload: dict[str, Any], limit: int = 6) -> str:
        """把参与玩家拼成一行（战报文案与海报共用）；花火是主持人，已在快照里剔除。"""
        names = [
            str(entry.get("name") or "").strip()
            for entry in (payload.get("players") or [])
            if isinstance(entry, dict)
        ]
        names = [name for name in names if name]
        if not names:
            return ""
        line = " · ".join(names[:limit])
        if len(names) > limit:
            line += f" 等 {len(names)} 人"
        return line

    def _soup_announce_text(self, room: GameRoom) -> str:
        """群通知文案：只给汤面和几条提问，制造「想知道答案」的钩子。

        硬性约束：不出现汤底、不复述玩家最后猜中的原话——群的通知会被还没玩的人
        看到，剧透了这道题就废了。
        """
        payload = self._soup_result_payload(room)
        if payload is None:
            return ""
        title = str(payload.get("title") or "海龟汤")
        surface = str(payload.get("surface") or "").strip()
        questions = int(payload.get("question_count") or 0)
        hints = int(payload.get("hints_used") or 0)
        attempts = int(payload.get("answer_attempts") or 0)
        solved = bool(payload.get("solved"))
        end_reason = str(payload.get("end_reason") or "")
        lines = ["🥣 海龟汤 ·《" + title + "》"]
        if surface:
            lines.append(surface)
        lines.append(
            f"—— 本局提问 {questions} 次 · 提示 {hints} 次 · 推理 {attempts} 次"
        )
        player_line = self._soup_player_line(payload)
        if player_line:
            lines.append(f"👥 玩家：{player_line}")
        for entry in self._soup_pick_questions(payload, limit=4):
            prompt = str(entry.get("prompt") or "").strip()
            verdict = str(entry.get("response") or "").strip()
            if not prompt:
                continue
            prompt = prompt if len(prompt) <= 60 else prompt[:58] + "…"
            lines.append(f"💬 「{prompt}」→ {verdict or '已答'}")
        if bool(payload.get("bot_solved")):
            lines.append("🤖 花火和玩家一起猜，最后被花火猜中了——汤底先不告诉你。")
        elif solved:
            lines.append("🏆 这道题被解开了——但汤底是什么，先不告诉你。")
        elif end_reason == "timeout":
            lines.append("🌫 这题问了很久也没人解开，汤底已经悄悄揭晓。")
        else:
            lines.append("🌫 这题最终没人解开，汤底已经揭晓。")
        lines.append("想知道答案？开一局，或者去问玩过的人。")
        # 战报里附上房间链接：群友点进去就能在网页里看完整汤底（战报本身为防剧透不露汤底）
        tail = self._soup_room_link_tail(room)
        if tail:
            lines.append(tail)
        return "\n".join(lines)

    def _soup_room_link_tail(self, room: GameRoom) -> str:
        """战报尾部的房间链接与「进网页看完整汤底」引导；拿不到链接返回空串。"""
        try:
            url = self._room_url(room)
        except Exception:
            return ""
        if not url:
            return ""
        return f"🔗 房间链接：{url}\n想看完整汤底？点上面的链接进网页房间查看。"

    def _soup_game_summary(self, room: GameRoom) -> str:
        """把本局关键信息拼成复盘用的摘要（明确排除汤底）。"""
        payload = self._soup_result_payload(room)
        if payload is None:
            return ""
        questions = int(payload.get("question_count") or 0)
        hints = int(payload.get("hints_used") or 0)
        attempts = int(payload.get("answer_attempts") or 0)
        solved = bool(payload.get("solved"))
        end_reason = str(payload.get("end_reason") or "")
        outcome = (
            "有人解开了汤底"
            if solved
            else ("无人解开，超时后揭晓" if end_reason == "timeout" else "无人解开，最后揭晓")
        )
        asked = "；".join(
            str(entry.get("prompt") or "")[:40]
            for entry in self._soup_pick_questions(payload, limit=5)
        )
        return (
            f"《{payload.get('title') or '海龟汤'}》汤面：{payload.get('surface') or ''}\n"
            f"本局共提问 {questions} 次、用了 {hints} 次提示、提交 {attempts} 次推理，{outcome}。\n"
            f"玩家主要问过：{asked or '（还没来得及提问）'}"
        )

    # 首字占位头像的底色（没有 QQ 头像时用）：按名字+座位号稳定取色
    _SOUP_AVATAR_COLORS = (
        "#8c5a3c", "#a8574b", "#6b7f4e", "#4f6f8c", "#7a5c8c", "#8c7a3c",
    )

    @classmethod
    def _soup_avatar_color(cls, name: str, number: int) -> str:
        seed = f"{name}:{number}"
        digest = 0
        for char in seed:
            digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
        return cls._SOUP_AVATAR_COLORS[digest % len(cls._SOUP_AVATAR_COLORS)]

    def _soup_poster_data(
        self, room: GameRoom, avatars: dict[int, bytes] | None = None
    ) -> dict[str, Any]:
        """提取海龟汤战报海报所需数据（t2i 模板与 Pillow 兜底共用）。

        与 `_soup_announce_text` 同一套取舍：只给汤面、提问摘录与结果，**不含汤底**，
        也不复述玩家最后猜中的原话——海报会被转到别的群里，剧透就废题。

        avatars 是「座位号 → 头像字节」的预下载结果；没有头像（未绑定 QQ / 拉取失败）
        时前端口径会回退成「首字圆牌」，不会出现破图。
        """
        payload = self._soup_result_payload(room) or {}
        questions = int(payload.get("question_count") or 0)
        hints = int(payload.get("hints_used") or 0)
        attempts = int(payload.get("answer_attempts") or 0)
        facts = int(payload.get("key_fact_count") or 0)
        discovered = int(payload.get("discovered_fact_count") or 0)
        stats = f"提问 {questions} 次 · 提示 {hints} 次 · 推理 {attempts} 次"
        if facts:
            stats += f" · 关键事实 {discovered}/{facts}"
        qa: list[str] = []
        for entry in self._soup_pick_questions(payload, limit=4):
            prompt = str(entry.get("prompt") or "").strip()
            if not prompt:
                continue
            # 提问常是一长串假设，海报一行放不下：按字符截断，别把版面撑爆
            if len(prompt) > 60:
                prompt = prompt[:58] + "…"
            answer = str(entry.get("response") or "已答").strip() or "已答"
            qa.append(f"「{prompt}」→ {answer[:40]}")
        solved = bool(payload.get("solved"))
        end_reason = str(payload.get("end_reason") or "")
        if bool(payload.get("bot_solved")):
            result_text = "🤖 花火猜中了"
        elif solved:
            result_text = "🏆 汤底被解开了"
        elif end_reason == "timeout":
            result_text = "🌫 没人解开，超时揭晓"
        else:
            result_text = "🌫 这道题没人解开"
        avatar_bytes = avatars or {}
        players: list[dict[str, Any]] = []
        for entry in payload.get("players") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            number = int(entry.get("number") or 0)
            raw = avatar_bytes.get(number) or b""
            players.append(
                {
                    "number": number,
                    "name": name,
                    "initial": name[:1] or "？",
                    "color": self._soup_avatar_color(name, number),
                    "avatar": (
                        "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
                        if raw
                        else ""
                    ),
                }
            )
        return {
            "date": time.strftime("%Y-%m-%d"),
            "title": str(payload.get("title") or "海龟汤"),
            "players": players,
            "players_text": self._soup_player_line(payload, limit=8),
            "surface": str(payload.get("surface") or "").strip(),
            "stats": stats,
            "questions": qa,
            "solved": solved,
            "result_text": result_text,
            "result_sub": "汤底？先卖个关子。",
        }

    async def _soup_avatar_bytes(self, url: str) -> bytes:
        """拉取一张 QQ 头像并缓存（t2i 要内联 data URI，Pillow 要字节流）。

        头像拉不到（网络/隐私设置/未绑定 QQ）时返回空字节，海报回退成首字圆牌。
        """
        if not url:
            return b""
        cache = getattr(self, "_avatar_bytes_cache", None)
        if cache is None:
            cache = {}
            self._avatar_bytes_cache = cache
        now = time.time()
        hit = cache.get(url)
        if hit and now - hit[0] < 6 * 3600:
            return hit[1]
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return b""
                    raw = await resp.read()
        except Exception as exc:
            logger.debug("[GameCompanion] 战报头像拉取失败: %s", exc)
            return b""
        if not raw or len(raw) > 512 * 1024:
            return b""
        cache[url] = (now, raw)
        return raw

    async def _soup_poster_avatars(self, payload: dict[str, Any]) -> dict[int, bytes]:
        """并发预取本局玩家头像（最多 8 张，单张 4 秒超时）。"""
        targets: list[tuple[int, str]] = []
        for entry in payload.get("players") or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("avatar_url") or "")
            if url:
                targets.append((int(entry.get("number") or 0), url))
        if not targets:
            return {}
        results = await asyncio.gather(
            *[self._soup_avatar_bytes(url) for _number, url in targets],
            return_exceptions=True,
        )
        avatars: dict[int, bytes] = {}
        for (number, _url), raw in zip(targets, results):
            if isinstance(raw, bytes) and raw:
                avatars[number] = raw
        return avatars

    async def _soup_announce_poster(self, room: GameRoom) -> str:
        """生成「海龟汤战报」海报，返回 base64 PNG。

        t2i（HTML 渲染）优先，不可用时回退 Pillow 手绘；两者都失败则抛异常，
        由调用方回退纯文字。注意：前端「分享战报」走的是浏览器 canvas，与本函数
        同款视觉但各自独立，改版时两边要一起看。
        """
        payload = self._soup_result_payload(room) or {}
        avatars = await self._soup_poster_avatars(payload)
        data = self._soup_poster_data(room, avatars)
        try:
            return await self._soup_poster_t2i(room, data)
        except Exception as exc:
            logger.warning("[GameCompanion] t2i 渲染海龟汤战报海报失败，回退 Pillow: %s", exc)
        return await asyncio.to_thread(self._soup_poster_pillow, room, avatars)

    async def _soup_poster_t2i(
        self, room: GameRoom, data: dict[str, Any] | None = None
    ) -> str:
        """用 AstrBot 的 t2i（HTML 渲染）生成海龟汤战报海报，返回 base64 PNG。"""
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        data = data or self._soup_poster_data(room)
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": 720,
            "device_scale_factor_level": "ultra",
            "timeout": 20000,
        }
        url = await asyncio.wait_for(
            render(_SOUP_POSTER_TMPL, data, options=options), timeout=8
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    def _soup_poster_pillow(
        self, room: GameRoom, avatars: dict[int, bytes] | None = None
    ) -> str:
        """Pillow 手绘「海龟汤战报」海报（t2i 不可用时的兜底），返回 base64 PNG。"""
        from PIL import Image, ImageDraw  # 可选依赖，缺失时回退纯文字

        import io

        data = self._soup_poster_data(room, avatars)
        S = 1.5
        W = int(720 * S)
        pad = int(46 * S)
        text_w = W - pad * 2
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        f_title = _pillow_font(True, int(38 * S))
        f_surface = _pillow_font(False, int(21 * S))
        f_stats = _pillow_font(False, int(16 * S))
        f_qa = _pillow_font(False, int(17 * S))
        f_small = _pillow_font(False, int(13 * S))
        f_ptitle = _pillow_font(False, int(15 * S))
        ef = _pillow_emoji_font(int(22 * S))
        title_lines = _pillow_wrap(probe, data["title"], text_w, f_title, 2, ef)
        surface_lines = _pillow_wrap(probe, data["surface"], text_w, f_surface, 8, ef)
        players = list(data.get("players") or [])
        qa_lines: list[str] = []
        for line in data["questions"]:
            qa_lines.extend(_pillow_wrap(probe, line, text_w - int(28 * S), f_qa, 2, ef))
        avatar_dia = int(54 * S)
        avatar_row_h = int(54 * S) + int(30 * S)  # 圆头像 + 昵称
        players_h = (int(26 * S) + avatar_row_h + int(16 * S)) if players else 0
        # 先用宽松的高度建画布，画完再裁到内容底部（省得手算每一段间距）
        est = 126 + len(title_lines) * 52 + 16 + len(surface_lines) * 32 + 34
        if qa_lines:
            est += 30 + len(qa_lines) * 26 + 40
        est += (players_h / S if players_h else 0) + 16
        H = int((est + 86 + 110) * S)
        img = Image.new("RGBA", (W, H), (20, 14, 9, 255))
        d = ImageDraw.Draw(img, "RGBA")
        top_c, bottom_c = (43, 29, 19), (20, 14, 9)

        def base_at(yy: float) -> tuple[int, int, int]:
            """渐变背景在某个高度上的底色（半透明色块要先混上去，见 _pillow_blend）。"""
            t = max(0.0, min(1.0, yy / max(1, H - 1)))
            return tuple(round(c + (b - c) * t) for c, b in zip(top_c, bottom_c))

        for y in range(H):
            d.line([(0, y), (W, y)], fill=base_at(y) + (255,))
        # 顶部横幅（题头 + 日期 + 金色分隔线）
        ef_h = _pillow_emoji_font(int(20 * S))
        _pillow_draw_text(
            d, pad, int(46 * S), "🥣 海龟汤 · 单局战报",
            _pillow_font(False, int(20 * S)), (229, 201, 154, 255), efont=ef_h,
        )
        _pillow_draw_text(
            d, W - pad, int(46 * S), data["date"], f_small,
            (229, 201, 154, 178), anchor="rs",
        )
        d.rectangle([pad, int(68 * S), W - pad, int(72 * S)], fill=(255, 209, 102, 255))
        # 标题与顶部金线之间留足呼吸感（贴太近会显得挤）
        y = int(126 * S)
        for line in title_lines:
            _pillow_draw_text(d, pad, y, line, f_title, (255, 255, 255, 255), efont=ef)
            y += int(52 * S)
        y += int(16 * S)
        for line in surface_lines:
            _pillow_draw_text(d, pad, y, line, f_surface, (240, 228, 210, 255), efont=ef)
            y += int(32 * S)
        y += int(26 * S)
        _pillow_draw_text(d, pad, y, data["stats"], f_stats, (192, 168, 135, 255), efont=ef)
        y += int(22 * S)
        if qa_lines:
            card_top = y
            card_h = int((30 + len(qa_lines) * 26 + 14) * S)
            d.rounded_rectangle(
                [pad, card_top, W - pad, card_top + card_h],
                radius=int(12 * S),
                fill=_pillow_blend(base_at(card_top), (255, 255, 255), 13),
                outline=_pillow_blend(base_at(card_top), (255, 209, 102), 56),
                width=max(1, int(1 * S)),
            )
            _pillow_draw_text(
                d, pad + int(14 * S), card_top + int(24 * S), "他们问过",
                _pillow_font(False, int(15 * S)), (229, 201, 154, 255), efont=ef,
            )
            qy = card_top + int(50 * S)
            for line in qa_lines:
                _pillow_draw_text(
                    d, pad + int(14 * S), qy, line, f_qa, (220, 205, 182, 255), efont=ef,
                )
                qy += int(26 * S)
            y = card_top + card_h + int(18 * S)
        # 玩家板块（结果条上方独立一张卡）：圆头像 + 昵称
        if players:
            card_top = y - int(4 * S)
            card_h = int((26 + 54 + 30 + 16) * S)
            d.rounded_rectangle(
                [pad, card_top, W - pad, card_top + card_h],
                radius=int(12 * S),
                fill=_pillow_blend(base_at(card_top), (255, 255, 255), 13),
                outline=_pillow_blend(base_at(card_top), (255, 209, 102), 56),
                width=max(1, int(1 * S)),
            )
            _pillow_draw_text(
                d, pad + int(14 * S), card_top + int(24 * S), "本局玩家",
                f_ptitle, (229, 201, 154, 255), efont=ef,
            )
            count = len(players)
            gap = int(10 * S)
            cell_w = min(
                int(96 * S),
                (text_w - int(28 * S) - gap * max(0, count - 1)) // max(1, count),
            )
            # 人数少时整行居中，避免头像挤在左边、右侧留一大片空
            total_w = count * cell_w + gap * max(0, count - 1)
            start_x = (W - total_w) // 2 + cell_w // 2
            cy = card_top + int(30 * S) + avatar_dia // 2
            f_init = _pillow_font(True, int(24 * S))
            for index, player in enumerate(players):
                cx = start_x + index * (cell_w + gap)
                _pillow_draw_avatar(
                    img,
                    d,
                    (avatars or {}).get(int(player.get("number") or 0)) or b"",
                    str(player.get("initial") or "？"),
                    str(player.get("color") or "#8c5a3c"),
                    cx,
                    cy,
                    avatar_dia,
                    f_init,
                    ef,
                )
                name = _pillow_truncate(
                    d, str(player.get("name") or ""), cell_w, f_small, ef
                )
                _pillow_draw_text(
                    d, cx, cy + avatar_dia // 2 + int(20 * S), name,
                    f_small, (220, 205, 182, 255), anchor="mm", efont=ef,
                )
            y = card_top + card_h + int(18 * S)
        # 结果条
        solved = bool(data["solved"])
        bar_h = int(62 * S)
        d.rounded_rectangle(
            [pad, y, W - pad, y + bar_h], radius=int(12 * S),
            fill=(
                _pillow_blend(base_at(y), (255, 209, 102), 41)
                if solved
                else _pillow_blend(base_at(y), (255, 255, 255), 15)
            ),
        )
        _pillow_draw_text(
            d, pad + int(16 * S), y + int(30 * S), data["result_text"],
            _pillow_font(True, int(22 * S)),
            (255, 209, 102, 255) if solved else (201, 189, 168, 255), efont=ef,
        )
        _pillow_draw_text(
            d, pad + int(16 * S), y + int(50 * S), data["result_sub"], f_small,
            (240, 228, 210, 140),
        )
        y += bar_h
        # 脚标 + 裁切（auto 高度）
        foot_y = y + int(36 * S)
        _pillow_draw_text(
            d, W / 2, foot_y, "花火陪你玩 · 想知道答案，就自己来试一局",
            _pillow_font(True, int(18 * S)), (255, 209, 102, 255), anchor="mm", efont=ef,
        )
        _pillow_draw_text(
            d, W / 2, foot_y + int(24 * S), data["date"], f_small,
            (229, 201, 154, 140), anchor="mm",
        )
        crop_bottom = min(H, foot_y + int(46 * S))
        img = img.crop((0, 0, W, crop_bottom))
        ImageDraw.Draw(img, "RGBA").rectangle(
            [0, crop_bottom - int(10 * S), W, crop_bottom], fill=(255, 209, 102, 255)
        )
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def soup_recap(self, room: GameRoom) -> str:
        """生成一局海龟汤的短复盘（结算卡「花火复盘」/「通知到群」共用）。

        按 completed_games 缓存，同一局只请求一次 LLM；输出**不得包含汤底**——
        这段文字会跟着群通知一起发出去，剧透会毁掉后来人的乐趣。
        """
        payload = self._soup_result_payload(room)
        if payload is None:
            return ""
        key = f"soup:{int(payload.get('completed_games') or 0)}"
        cache = getattr(room, "recap_cache", None) or {}
        if key in cache:
            return str(cache[key] or "")
        summary = self._soup_game_summary(room)
        fallback = (
            "这题的汤面看着平常，问起来才发现处处是坑；"
            "下次别急着交答案，先把「为什么偏偏是他」问出来。"
        )
        if not summary:
            text = fallback
        else:
            prompt = (
                "请为一局海龟汤生成简短复盘（中文，2~4 句，轻松俏皮，可以带一点 emoji，"
                "不要标题、不要编号、不要客套语）。\n"
                f"本局信息：\n{summary}\n\n"
                "硬性要求：\n"
                "1. 只能谈论汤面、玩家提问的路线、大家在哪个方向打转；\n"
                "2. 绝对不能写出、暗示或总结汤底内容，也不要说「其实就是……」，"
                "因为这段文字会发到群里，看到的人可能还要玩这道题；\n"
                "3. 可以调侃思路（比如一直围着某个细节问），但要善意，不要贬低玩家；\n"
                "4. 结尾留一句「想知道答案就自己来试试」这类钩子。"
            )
            text = await self._llm_gen_neutral(
                prompt,
                system_prompt=(
                    "你是古灵精怪但温和的复盘解说员「花火」，只聊推理过程、不剧透答案，"
                    "只用中文输出复盘正文。"
                ),
            )
            text = (text or fallback).strip()
        if key:
            room.recap_cache[key] = text
        return text

    async def soup_announce_result(
        self, room: GameRoom, image_data: str = ""
    ) -> str:
        """把一局海龟汤的结果发到开房群，返回已发送文案。

        走独立的 `turtle_soup.group_announce_*` 配置（与棋类 / 谁是卧底互不影响）。
        文案只露汤面与提问，不露汤底；复盘由 soup_recap 生成（同样不剧透）。
        """
        if not getattr(self, "turtle_soup_group_announce_enabled", False):
            return ""
        if getattr(room, "game_type", "") != "turtle_soup":
            return ""
        text = self._soup_announce_text(room)
        if not text:
            return ""
        recap = ""
        if getattr(self, "turtle_soup_group_announce_recap", True):
            try:
                recap = await self.soup_recap(room)
            except Exception:
                recap = ""
        style = (getattr(self, "turtle_soup_group_announce_style", "text") or "text").lower()
        want_image = style in {"image", "both"}
        want_text = style in {"text", "both"}
        if not want_text and not want_image:
            want_text = True
        if want_image and image_data:
            recap_with_link = recap
            if not want_text:
                # 只发图（style=image）时正文被省了，房间链接引导得跟着复盘走，
                # 否则群友看完复盘也不知道去哪看完整汤底。
                tail = self._soup_room_link_tail(room)
                if tail:
                    recap_with_link = (recap + "\n\n" + tail) if recap else tail
            sent = await self._send_to_origin_image(
                room, text if want_text else "", image_data, recap_with_link
            )
            return text if sent else ""
        sent = await self._send_to_origin(
            room, text + (f"\n📝 复盘：{recap}" if recap else "")
        )
        return text if sent else ""

    async def _auto_announce_soup(self, room: GameRoom) -> None:
        """局末自动播报（turtle_soup.group_announce_auto）。

        与棋类一致：图文模式下服务端没有前端，用 t2i / Pillow 现画一张海报；
        发送成功后写幂等标记，避免玩家再手动点「通知到群」时重复刷群。
        """
        try:
            # 播报关掉时直接返回：别为了一个不会发出去的图白跑一次 t2i 渲染。
            if not getattr(self, "turtle_soup_group_announce_enabled", False):
                return
            if getattr(room, "game_type", "") != "turtle_soup":
                return
            image_data = ""
            style = (
                getattr(self, "turtle_soup_group_announce_style", "text") or "text"
            ).lower()
            if style in {"image", "both"}:
                try:
                    image_data = await self._soup_announce_poster(room)
                except Exception as exc:
                    logger.warning(
                        "[GameCompanion] 自动播报生成本局海龟汤海报失败，改发纯文字: %s", exc
                    )
                    image_data = ""
            text = await self.soup_announce_result(room, image_data)
            if text:
                room.last_announced_uid = f"soup:{int(room.completed_games)}"
                room.last_announced_text = text
        except Exception as exc:
            logger.warning("[GameCompanion] 海龟汤自动播报失败: %s", exc)

    async def board_announce_result(
        self, room: GameRoom, image_data: str = ""
    ) -> str:
        """把棋类（五子棋/黑白棋/中国象棋/围棋）单局胜负发到开房群，返回已发送文案。

        使用独立的「棋类播报」配置（board.group_announce_enabled / group_announce_style /
        group_announce_recap）；复盘由 board_recap 生成（按局缓存）。
        幂等由 server._board_announce 按 completed_games 保证。
        """
        if not getattr(self, "board_group_announce_enabled", False):
            return ""
        if getattr(room, "game_type", "") not in {
            "gomoku", "othello", "xiangqi", "weiqi",
        }:
            return ""
        text = self._board_announce_text(room)
        if not text:
            return ""
        # 群通报附带「花火复盘」（board.group_announce_recap）
        recap = ""
        if getattr(self, "board_group_announce_recap", True):
            try:
                recap = await self.board_recap(room)
            except Exception:
                recap = ""
        style = (getattr(self, "board_group_announce_style", "text") or "text").lower()
        want_image = style in {"image", "both"}
        want_text = style in {"text", "both"}
        if not want_text and not want_image:
            want_text = True
        if want_image and image_data:
            sent = await self._send_to_origin_image(
                room, text if want_text else "", image_data, recap
            )
            return text if sent else ""
        # 纯文字 / 「仅图片」但缺图：一律发文字兜底，保证群里能收到战况
        sent = await self._send_to_origin(
            room, text + (f"\n📝 复盘：{recap}" if recap else "")
        )
        return text if sent else ""

    async def board_recap(self, room: GameRoom) -> str:
        """生成一局棋类的短复盘摘要（结算卡「复盘」/「通知到群」共用）。

        按 completed_games 缓存：同局第一次请求才调 LLM，之后直接复用。
        依据本局结果（谁赢、执子、手数、终局局面）生成 2-3 句要点；
        LLM 不可用或超时时返回本地兜底文案。
        """
        payload = self._board_result_payload(room)
        if payload is None:
            return ""
        # 按「快照里的局号」缓存：玩家立刻再战后仍能取回上一局的复盘
        key = f"board:{int(payload.get('completed_games') or 0)}"
        cache = getattr(room, "recap_cache", None) or {}
        if key in cache:
            return str(cache[key] or "")
        summary = self._board_game_summary(room)
        if not summary:
            text = "这局棋有点意思，输赢都在一手之间，下次再来一局见分晓。"
        else:
            prompt = (
                "请为一局棋类对局生成简短复盘（中文，2~3 句，轻松俏皮口吻，"
                "可以带点网络梗和适度 emoji，不要标题、不要编号、不要客套感谢语）。\n"
                f"对局情况：{summary}\n"
                "风格参考：像一起玩的朋友在茶余饭后聊这盘棋，轻松幽默、善意调侃，"
                "点一下这局的关键（谁赢了、下得顺不顺、有没有翻盘/绝杀），绝不攻击或贬低任何一方。"
            )
            text = await self._llm_gen_neutral(
                prompt,
                system_prompt=(
                    "你是古灵精怪但温和的复盘解说员「花火」，点评轻松俏皮、善意不伤人，"
                    "只用中文输出复盘正文，不要标题、不要编号。"
                ),
            )
            text = (text or "这局棋有点意思，输赢都在一手之间，下次再来一局见分晓。").strip()
        if key:
            room.recap_cache[key] = text
        return text

    def _board_result_payload(self, room: GameRoom) -> dict[str, object] | None:
        """棋类本局结果：优先读终局快照，没有快照再退回当前 room.game。

        快照由 room_manager._finish_game 在换局前冻结，专门解决「玩家秒点再来一局，
        异步播报读到新局 → 0 手」的问题。
        """
        snap = getattr(room, "last_board_result", None)
        if isinstance(snap, dict) and snap:
            return snap
        game = room.game
        if game is None or not isinstance(game, BOARD_GAME_CLASSES):
            return None
        raw_board = getattr(game, "board", None)
        board = raw_board() if callable(raw_board) else raw_board
        payload: dict[str, object] = {
            "game_uid": str(getattr(game, "game_uid", "") or ""),
            "completed_games": int(getattr(room, "completed_games", 0) or 0),
            "game_type": room.game_type,
            "moves": len(getattr(game, "history", None) or []),
            "draw": bool(getattr(game, "draw", False)),
            "winner_token": "",
            "winner_name": "",
            "winner_side": "",
            "board": [list(row) for row in board] if board else None,
        }
        winner_seat = self.manager._duel_winner_seat(room)
        if winner_seat is not None:
            payload["winner_token"] = winner_seat.visitor_token
            payload["winner_name"] = self.manager._seat_display(winner_seat)
            payload["winner_side"] = self.manager._seat_side(room, winner_seat)
            loser_seat = self.manager._duel_loser_seat(room)
            if loser_seat is not None:
                payload["loser_name"] = self.manager._seat_display(loser_seat)
                payload["loser_side"] = self.manager._seat_side(room, loser_seat)
        # 单人历史房间（未启用玩家席）的兜底胜负
        if not payload["draw"] and not payload["winner_name"]:
            if isinstance(game, XiangqiGame):
                payload["human_won"] = game.winner == getattr(
                    game, "human_side", None
                )
            else:
                winner = int(getattr(game, "winner", 0) or 0)
                human_color = int(getattr(game, "human_color", 0) or 0)
                if winner and human_color:
                    payload["human_won"] = winner == human_color
        return payload

    def _board_game_summary(self, room: GameRoom) -> str:
        """把一局棋类的关键信息拼成可读文本，供复盘使用。"""
        try:
            payload = self._board_result_payload(room)
            if payload is None:
                return ""
            label = self.manager._game_label(room.game_type)
            moves = int(payload.get("moves") or 0)
            name = str(payload.get("winner_name") or "")
            side_text = {"red": "红", "black": "黑", "white": "白"}.get(
                str(payload.get("winner_side") or ""), ""
            )
            if name:
                outcome = f"{name}获胜" + (f"（执{side_text}）" if side_text else "")
            elif payload.get("draw"):
                outcome = "平局"
            else:
                outcome = "胜负已定"
            return f"{label}，共 {moves} 手，{outcome}"
        except Exception:
            return ""

    def _board_announce_text(self, room: GameRoom) -> str:
        """棋类纯文字战报：游戏名 → 谁赢（执什么）→ 手数，供群通报使用。"""
        payload = self._board_result_payload(room)
        if payload is None:
            return ""
        label = self.manager._game_label(room.game_type)
        moves = int(payload.get("moves") or 0)
        name = str(payload.get("winner_name") or "")
        side_text = {"red": "红", "black": "黑", "white": "白"}.get(
            str(payload.get("winner_side") or ""), ""
        )
        if name:
            result = f"🏆 {name}获胜" + (f"（执{side_text}）" if side_text else "")
            loser = str(payload.get("loser_name") or "")
            if loser and loser != name:
                loser_side_text = {"red": "红", "black": "黑", "white": "白"}.get(
                    str(payload.get("loser_side") or ""), ""
                )
                result += f" · {loser}落败" + (
                    f"（执{loser_side_text}）" if loser_side_text else ""
                )
        elif payload.get("draw"):
            result = "🤝 平局"
        elif payload.get("human_won") is True:
            result = "🏆 玩家获胜 · 花火落败"
        elif payload.get("human_won") is False:
            result = "🏆 花火获胜 · 玩家落败"
        else:
            result = "🏁 本局结束"
        return f"♟️ {label}战报\n{result}\n⏱️ 共 {moves} 手"

    async def _board_announce_poster(self, room: GameRoom) -> str:
        """生成「棋类战报」海报，返回 base64 PNG。

        优先用 AstrBot 的 t2i（HTML/Jinja2 → 图片，样式精美、支持 emoji）；
        t2i 服务不可用或超时时回退 Pillow 手绘。两者都失败则抛出异常，
        由调用方回退纯文字播报。
        """
        try:
            return await self._board_poster_t2i(room)
        except Exception as exc:
            logger.warning("[GameCompanion] t2i 渲染棋类战报海报失败，回退 Pillow: %s", exc)
        return await asyncio.to_thread(self._board_poster_pillow, room)

    async def _board_poster_t2i(self, room: GameRoom) -> str:
        """用 AstrBot 的 t2i（HTML 渲染）生成棋类战报海报，返回 base64 PNG。"""
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        data = self._board_poster_data(room)
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": 720,
            # 自建 t2i 服务下渲染开销低：用高分屏 DPR（1.8x）出更清晰的海报。
            "device_scale_factor_level": "ultra",
            "timeout": 20000,
        }
        url = await asyncio.wait_for(
            render(_BOARD_POSTER_TMPL, data, options=options), timeout=8
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    def _board_poster_data(self, room: GameRoom) -> dict[str, Any]:
        """提取棋类战报海报所需数据（标题/副信息/内嵌棋盘 SVG），供 t2i 模板与 Pillow 共用。"""
        payload = self._board_result_payload(room) or {}
        gt = room.game_type
        label = self.manager._game_label(gt)
        icon = {"gomoku": "🎯", "othello": "⚪", "xiangqi": "♟️", "weiqi": "⛰️"}.get(
            gt, "♟️"
        )
        moves = int(payload.get("moves") or 0)
        # 黑白棋终局：副信息显示双方子数，比「共 N 手」更直观
        if gt == "othello":
            piece_count = payload.get("piece_count") or {}
            if piece_count.get("black") is not None and piece_count.get("white") is not None:
                count_text = f"黑 {int(piece_count['black'])} · 白 {int(piece_count['white'])}"
            else:
                count_text = f"共 {moves} 手"
        else:
            count_text = f"共 {moves} 手"
        name = str(payload.get("winner_name") or "")
        side = str(payload.get("winner_side") or "")
        draw = bool(payload.get("draw"))
        # 与花火（AI 座位）对局时，副信息里带上花火棋力（难度），让群友一眼看清对手水平
        ai_difficulty = (
            self.manager._difficulty_label(room.difficulty)
            if self.manager._ai_seat(room) is not None
            else ""
        )
        diff = f" · 花火（{ai_difficulty}）" if ai_difficulty else ""

        def _huahuo_label(raw_name: str) -> str:
            # 花火作为胜者/败者时，难度直接跟在名字后（如「花火（普通）」），避免与 diff 重复
            if ai_difficulty and raw_name == "花火":
                return f"花火（{ai_difficulty}）"
            return raw_name

        loser_line = ""
        if name:
            # 昵称过长时截断，避免战报标题/副信息溢出画布「显示不全」
            if len(name) > 10:
                name = name[:10] + "…"
            side_text = {"red": "红", "black": "黑", "white": "白"}.get(side, "")
            loser = str(payload.get("loser_name") or "")
            loser_side_text = {"red": "红", "black": "黑", "white": "白"}.get(
                str(payload.get("loser_side") or ""), ""
            )
            title = f"{name} 获胜"
            # 胜方信息在第一行；败方另起一行，避免整句挤成一条读不清
            sub = (
                f"{label} · {_huahuo_label(name)}执{side_text} · {count_text}"
                if side_text
                else f"{label} · {count_text}"
            )
            if loser and loser != name:
                if len(loser) > 10:
                    loser = loser[:10] + "…"
                loser_line = f"败方：{_huahuo_label(loser)}"
                if loser_side_text:
                    loser_line += f" · 执{loser_side_text}"
            badge, accent = "🏆", "#ffd166"
        elif draw:
            title, sub, badge, accent = (
                "平局",
                f"{label}{diff} · {count_text}",
                "🤝",
                "#9ccbff",
            )
        elif payload.get("human_won") is True:
            title, sub, badge, accent = (
                "玩家获胜",
                f"{label}{diff} · {count_text}",
                "🏆",
                "#ffd166",
            )
            # 单人局（玩家 vs 花火）：负方恒为花火，这里没有座位可查，直接写死
            loser_line = f"败方：{_huahuo_label('花火')}"
        elif payload.get("human_won") is False:
            title, sub, badge, accent = (
                "花火 获胜",
                f"{label}{diff} · {count_text}",
                "🏆",
                "#ffd166",
            )
            loser_line = "败方：玩家"
        else:
            title, sub, badge, accent = (
                "本局结束",
                f"{label}{diff} · {count_text}",
                "🏁",
                "#9ccbff",
            )
        return {
            "icon": icon,
            "label": label,
            "title": title,
            "sub": sub,
            # 副信息第二行（目前只用于「败方：xxx · 执黑」），空串时不渲染
            "sub2": loser_line,
            "badge": badge,
            "accent": accent,
            "board_svg": self._board_svg(room),
        }

    @staticmethod
    def _board_svg(room: GameRoom) -> str:
        """生成终局棋盘 SVG（服务端海报用），与前端 paintBoardThumb 视觉一致。

        盘面优先取终局快照，避免玩家立刻再战导致海报画成新开局。
        """
        payload = getattr(room, "last_board_result", None)
        board = payload.get("board") if isinstance(payload, dict) else None
        if not board:
            game = room.game
            if game is None:
                return ""
            raw = getattr(game, "board", None)
            board = raw() if callable(raw) else raw
        if not board or not isinstance(board, list) or not board:
            return ""
        winning_line = payload.get("winning_line") if isinstance(payload, dict) else None
        last_move = payload.get("last_move") if isinstance(payload, dict) else None
        gt = room.game_type
        if gt == "xiangqi":
            return GameCompanionPlugin._xiangqi_svg_from_board(board, last_move)
        n = len(board)
        size = 360
        if gt == "othello":
            cell = size / n
            parts = [f'<rect width="{size}" height="{size}" fill="#27754f"/>']
            line = 'stroke="rgba(14,51,34,.78)" stroke-width="1.5"'
            for i in range(n + 1):
                p = i * cell
                parts.append(f'<line x1="{p:.2f}" y1="0" x2="{p:.2f}" y2="{size}" {line}/>')
                parts.append(f'<line x1="0" y1="{p:.2f}" x2="{size}" y2="{p:.2f}" {line}/>')
            r = cell * 0.4
            for row in range(n):
                for col in range(n):
                    v = board[row][col]
                    if not v:
                        continue
                    cx, cy = col * cell + cell / 2, row * cell + cell / 2
                    if v == 1:
                        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="#1a1a1a"/>')
                    else:
                        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="#f4f4f2" stroke="#9aa09b" stroke-width="1"/>')
            # 高亮最后一步落子
            if last_move and last_move.get("to"):
                lr, lc = last_move["to"]
                cx, cy = lc * cell + cell / 2, lr * cell + cell / 2
                parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{cell * 0.46:.2f}" fill="none" stroke="#ffd166" stroke-width="2.5"/>')
            return f'<svg width="360" height="360" viewBox="0 0 360 360" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
        pal = {
            "gomoku": ("#d4a85f", "#5d472c", "#242724", "#f7f8f5", "#9da39e"),
            "weiqi": ("#dcc089", "#6b5636", "#22251f", "#fbfcf8", "#9aa19b"),
        }.get(gt, ("#d4a85f", "#5d472c", "#242724", "#f7f8f5", "#9da39e"))
        board_c, line_c, black_c, white_c, edge_c = pal
        m = size * 0.05
        gap = (size - size * 0.1) / (n - 1) if n > 1 else size
        parts = [f'<rect width="{size}" height="{size}" fill="{board_c}"/>']
        lw = max(1.0, size / 240)
        line = f'stroke="{line_c}" stroke-width="{lw:.2f}"'
        for i in range(n):
            p = m + i * gap
            parts.append(f'<line x1="{m:.2f}" y1="{p:.2f}" x2="{size - m:.2f}" y2="{p:.2f}" {line}/>')
            parts.append(f'<line x1="{p:.2f}" y1="{m:.2f}" x2="{p:.2f}" y2="{size - m:.2f}" {line}/>')
        rr = gap * 0.45
        for row in range(n):
            for col in range(n):
                v = board[row][col]
                if not v:
                    continue
                cx, cy = m + col * gap, m + row * gap
                if v == 1:
                    parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{rr:.2f}" fill="{black_c}"/>')
                else:
                    parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{rr:.2f}" fill="{white_c}" stroke="{edge_c}" stroke-width="1"/>')
        # 五子棋：给连成五的一串棋子画一条贯穿高亮线
        if winning_line and len(winning_line) >= 2:
            r0, c0 = winning_line[0]
            r1, c1 = winning_line[-1]
            x0, y0 = m + c0 * gap, m + r0 * gap
            x1, y1 = m + c1 * gap, m + r1 * gap
            parts.append(
                f'<line x1="{x0:.2f}" y1="{y0:.2f}" x2="{x1:.2f}" y2="{y1:.2f}" '
                f'stroke="#ffd166" stroke-width="{gap * 0.30:.2f}" stroke-linecap="round" opacity="0.40"/>'
            )
            parts.append(
                f'<line x1="{x0:.2f}" y1="{y0:.2f}" x2="{x1:.2f}" y2="{y1:.2f}" '
                f'stroke="#ffd166" stroke-width="{gap * 0.12:.2f}" stroke-linecap="round"/>'
            )
        return f'<svg width="360" height="360" viewBox="0 0 360 360" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'

    @staticmethod
    def _xiangqi_svg_from_board(
        board: list[list[str]], last_move: dict[str, object] | None = None
    ) -> str:
        """中国象棋终局棋盘 SVG（10x9，含棋子汉字），可高亮最后一手落子。"""
        rows, cols = len(board), len(board[0]) if board else 9
        size = 360
        m = size * 0.07
        gx = (size - 2 * m) / (cols - 1)
        gy = (size - 2 * m) / (rows - 1)
        parts = [f'<rect width="{size}" height="{size}" fill="#d7a85d"/>']
        line = 'stroke="#563c23" stroke-width="1.5"'
        for r in range(rows):
            y = m + r * gy
            parts.append(f'<line x1="{m:.2f}" y1="{y:.2f}" x2="{size - m:.2f}" y2="{y:.2f}" {line}/>')
        for col in range(cols):
            x = m + col * gx
            if col == 0 or col == cols - 1:
                parts.append(f'<line x1="{x:.2f}" y1="{m:.2f}" x2="{x:.2f}" y2="{size - m:.2f}" {line}/>')
            else:
                parts.append(f'<line x1="{x:.2f}" y1="{m:.2f}" x2="{x:.2f}" y2="{m + 4 * gy:.2f}" {line}/>')
                parts.append(f'<line x1="{x:.2f}" y1="{m + 5 * gy:.2f}" x2="{x:.2f}" y2="{size - m:.2f}" {line}/>')
        labels = {
            "K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
            "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒",
        }
        rad = min(gx, gy) * 0.38
        fsize = rad * 1.1
        for r in range(rows):
            for col in range(cols):
                piece = board[r][col]
                if piece == ".":
                    continue
                x, y = m + col * gx, m + r * gy
                is_red = piece == piece.upper()
                stroke = "#8b2b24" if is_red else "#3a3a3a"
                fill = "#8b2b24" if is_red else "#1e2220"
                label = labels.get(piece, piece)
                parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{rad:.2f}" fill="#f1d49a" stroke="{stroke}" stroke-width="1.5"/>')
                parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{fsize:.2f}" font-family="Noto Serif SC, Songti SC, serif" font-weight="700" fill="{fill}" text-anchor="middle" dominant-baseline="central">{label}</text>')
        # 高亮最后一手：from→to 淡色虚线 + 落子点金色外圈（杀棋/将军的那一手）
        if last_move:
            from_rc = last_move.get("from")
            to_rc = last_move.get("to")
            if (
                isinstance(from_rc, (list, tuple))
                and len(from_rc) == 2
                and isinstance(to_rc, (list, tuple))
                and len(to_rc) == 2
            ):
                fr, fc = from_rc
                tr, tc = to_rc
                x0, y0 = m + fc * gx, m + fr * gy
                x1, y1 = m + tc * gx, m + tr * gy
                parts.append(
                    f'<line x1="{x0:.2f}" y1="{y0:.2f}" x2="{x1:.2f}" y2="{y1:.2f}" '
                    f'stroke="#ffd166" stroke-width="1.6" stroke-dasharray="4 3" opacity="0.75"/>'
                )
                parts.append(
                    f'<circle cx="{x1:.2f}" cy="{y1:.2f}" r="{rad * 1.3:.2f}" '
                    f'fill="none" stroke="#ffd166" stroke-width="2.5"/>'
                )
        return f'<svg width="360" height="360" viewBox="0 0 360 360" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'

    def _board_poster_pillow(self, room: GameRoom) -> str:
        """Pillow 手绘「棋类战报」海报（t2i 不可用时的兜底），返回 base64 PNG。"""
        from PIL import Image, ImageDraw  # 可选依赖，缺失时回退纯文字

        import io

        data = self._board_poster_data(room)
        S = 1.5
        W, H = int(720 * S), int(720 * S)
        pad = int(46 * S)
        img = Image.new("RGBA", (W, H), (13, 31, 23, 255))
        d = ImageDraw.Draw(img, "RGBA")
        # 深绿渐变背景
        top_c, bottom_c = (30, 67, 52), (13, 31, 23)
        for y in range(H):
            t = y / max(1, H - 1)
            d.line(
                [(0, y), (W, y)],
                fill=(
                    round(top_c[0] + (bottom_c[0] - top_c[0]) * t),
                    round(top_c[1] + (bottom_c[1] - top_c[1]) * t),
                    round(top_c[2] + (bottom_c[2] - top_c[2]) * t),
                    255,
                ),
            )
        # 底部强调条
        accent_hex = str(data["accent"]).lstrip("#")
        accent = tuple(int(accent_hex[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
        d.rectangle([0, H - int(10 * S), W, H], fill=accent)
        ef_h = _pillow_emoji_font(int(22 * S))
        _pillow_draw_text(
            d, pad, int(56 * S), f"{data['icon']} {data['label']} · 战报",
            _pillow_font(True, int(22 * S)), (255, 224, 166, 255), efont=ef_h,
        )
        ef_b = _pillow_emoji_font(int(30 * S))
        _pillow_draw_text(
            d, W - pad, int(56 * S), data["badge"],
            _pillow_font(True, int(30 * S)), (255, 255, 255, 255), anchor="rs", efont=ef_b,
        )
        # 大字结果：昵称过长时逐档缩字号，仍超宽则截断，避免溢出画布「显示不全」
        title = data["title"]
        max_w = W - 2 * pad
        big_font = _pillow_font(True, int(58 * S))
        for px in (58, 50, 44, 38, 32):
            big_font = _pillow_font(True, int(px * S))
            if _pillow_font_length(d, title, big_font, ef_b) <= max_w:
                break
        title = _pillow_truncate(d, title, max_w, big_font, ef_b)
        _pillow_draw_text(d, W / 2, int(160 * S), title, big_font, accent, anchor="mm", efont=ef_b, stroke_width=int(2 * S), stroke_fill=(255, 255, 255))
        # 副信息：过长时截断（含玩家昵称）
        sub_font = _pillow_font(False, int(19 * S))
        sub_text = _pillow_truncate(d, data["sub"], max_w, sub_font, ef_b)
        _pillow_draw_text(
            d, W / 2, int(226 * S), sub_text, sub_font, (255, 255, 255, 204), anchor="mm", efont=ef_b,
        )
        # 副信息第二行（败方）：另起一行，别和胜方挤在一起
        sub2 = str(data.get("sub2") or "")
        if sub2:
            sub2_font = _pillow_font(False, int(17 * S))
            sub2_text = _pillow_truncate(d, sub2, max_w, sub2_font, ef_b)
            _pillow_draw_text(
                d, W / 2, int(254 * S), sub2_text, sub2_font,
                (255, 255, 255, 148), anchor="mm", efont=ef_b,
            )
        # 棋盘：必须整块落在画布内（以前 fixed 560*S 从 264*S 起算，底部会被裁掉）
        board = int(380 * S)
        bx, by = (W - board) // 2, int(292 * S)
        GameCompanionPlugin._pillow_draw_board(d, room, bx, by, board)
        # 脚标
        _pillow_draw_text(
            d, W - pad, H - int(34 * S), "由 花火 生成",
            _pillow_font(False, int(14 * S)), (255, 255, 255, 102), anchor="rs", efont=ef_b,
        )
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _pillow_draw_board(d: Any, room: GameRoom, x0: float, y0: float, size: float) -> None:
        """把终局棋盘画进 Pillow 海报指定方形区域（与 SVG / 前端视觉一致）。"""
        payload = getattr(room, "last_board_result", None)
        gt = room.game_type
        board = payload.get("board") if isinstance(payload, dict) else None
        if not board:
            game = room.game
            if game is None:
                return
            raw = getattr(game, "board", None)
            board = raw() if callable(raw) else raw
        if not board:
            return
        winning_line = payload.get("winning_line") if isinstance(payload, dict) else None
        last_move = payload.get("last_move") if isinstance(payload, dict) else None
        if gt == "xiangqi":
            rows, cols = len(board), len(board[0]) if board else 9
            m = size * 0.07
            gx = (size - 2 * m) / (cols - 1)
            gy = (size - 2 * m) / (rows - 1)
            d.rectangle([x0, y0, x0 + size, y0 + size], fill=(215, 168, 93))
            ln, lw = (86, 60, 35), max(1, int(size / 220))
            for r in range(rows):
                y = y0 + m + r * gy
                d.line([(x0 + m, y), (x0 + size - m, y)], fill=ln, width=lw)
            for col in range(cols):
                x = x0 + m + col * gx
                if col == 0 or col == cols - 1:
                    d.line([(x, y0 + m), (x, y0 + size - m)], fill=ln, width=lw)
                else:
                    d.line([(x, y0 + m), (x, y0 + m + 4 * gy)], fill=ln, width=lw)
                    d.line([(x, y0 + m + 5 * gy), (x, y0 + size - m)], fill=ln, width=lw)
            labels = {
                "K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
                "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒",
            }
            rad = min(gx, gy) * 0.38
            f = _pillow_font(True, max(1, int(rad * 1.1)))
            for r in range(rows):
                for col in range(cols):
                    piece = board[r][col]
                    if piece == ".":
                        continue
                    x, y = x0 + m + col * gx, y0 + m + r * gy
                    is_red = piece == piece.upper()
                    ec = (139, 43, 36) if is_red else (58, 58, 58)
                    tc = (139, 43, 36) if is_red else (30, 34, 32)
                    d.ellipse(
                        [x - rad, y - rad, x + rad, y + rad],
                        fill=(241, 212, 154), outline=ec, width=max(1, int(size / 200)),
                    )
                    d.text((x, y), labels.get(piece, piece), font=f, fill=tc, anchor="mm")
            # 高亮最后一手：from→to 淡线 + 落子点金色外圈（杀棋/将军的那一手）
            if last_move:
                from_rc = last_move.get("from")
                to_rc = last_move.get("to")
                if (
                    isinstance(from_rc, (list, tuple)) and len(from_rc) == 2
                    and isinstance(to_rc, (list, tuple)) and len(to_rc) == 2
                ):
                    fr, fc = from_rc
                    tr, tc = to_rc
                    fx, fy = x0 + m + fc * gx, y0 + m + fr * gy
                    tx, ty = x0 + m + tc * gx, y0 + m + tr * gy
                    d.line(
                        [(fx, fy), (tx, ty)],
                        fill=(255, 209, 102, 190),
                        width=max(1, int(size / 280)),
                    )
                    d.ellipse(
                        [tx - rad * 1.3, ty - rad * 1.3, tx + rad * 1.3, ty + rad * 1.3],
                        outline=(255, 209, 102, 255),
                        width=max(1, int(size / 160)),
                    )
            return
        n = len(board)
        if gt == "othello":
            cell = size / n
            d.rectangle([x0, y0, x0 + size, y0 + size], fill=(39, 117, 79))
            ln, lw = (14, 51, 34), max(1, int(size / 200))
            for i in range(n + 1):
                x = x0 + i * cell
                y = y0 + i * cell
                d.line([(x, y0), (x, y0 + size)], fill=ln, width=lw)
                d.line([(x0, y), (x0 + size, y)], fill=ln, width=lw)
            r = cell * 0.4
            for row in range(n):
                for col in range(n):
                    v = board[row][col]
                    if not v:
                        continue
                    cx, cy = x0 + col * cell + cell / 2, y0 + row * cell + cell / 2
                    if v == 1:
                        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(26, 26, 26))
                    else:
                        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(244, 244, 242), outline=(154, 160, 155), width=1)
            # 高亮最后一步落子
            if last_move and last_move.get("to"):
                lr, lc = last_move["to"]
                cx, cy = x0 + lc * cell + cell / 2, y0 + lr * cell + cell / 2
                d.ellipse(
                    [cx - cell * 0.46, cy - cell * 0.46, cx + cell * 0.46, cy + cell * 0.46],
                    outline=(255, 209, 102, 255),
                    width=max(1, int(size / 160)),
                )
            return
        pal = {
            "gomoku": ((212, 168, 95), (93, 71, 44), (36, 39, 36), (247, 248, 245), (157, 163, 158)),
            "weiqi": ((220, 192, 137), (107, 86, 54), (34, 37, 31), (251, 252, 248), (154, 161, 155)),
        }.get(gt, ((212, 168, 95), (93, 71, 44), (36, 39, 36), (247, 248, 245), (157, 163, 158)))
        board_c, line_c, black_c, white_c, edge_c = pal
        m = size * 0.05
        gap = (size - 2 * m) / (n - 1) if n > 1 else size
        d.rectangle([x0, y0, x0 + size, y0 + size], fill=board_c)
        lw = max(1, int(size / 240))
        for i in range(n):
            p = y0 + m + i * gap
            d.line([(x0 + m, p), (x0 + size - m, p)], fill=line_c, width=lw)
            p = x0 + m + i * gap
            d.line([(p, y0 + m), (p, y0 + size - m)], fill=line_c, width=lw)
        r = gap * 0.45
        for row in range(n):
            for col in range(n):
                v = board[row][col]
                if not v:
                    continue
                cx, cy = x0 + m + col * gap, y0 + m + row * gap
                if v == 1:
                    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=black_c)
                else:
                    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=white_c, outline=edge_c, width=1)
        # 五子棋：给连成五的一串棋子画一条贯穿高亮线
        if winning_line and len(winning_line) >= 2:
            r0, c0 = winning_line[0]
            r1, c1 = winning_line[-1]
            ax, ay = x0 + m + c0 * gap, y0 + m + r0 * gap
            bx, by = x0 + m + c1 * gap, y0 + m + r1 * gap
            d.line(
                [(ax, ay), (bx, by)],
                fill=(255, 209, 102, 120),
                width=max(2, int(gap * 0.30)),
            )
            d.line(
                [(ax, ay), (bx, by)],
                fill=(255, 209, 102, 255),
                width=max(1, int(gap * 0.12)),
            )

    @staticmethod
    def _undercover_announce_text(game: Any) -> str:
        """纯文字战报（标题 → 词条 → 胜利方 → 失败方），供群通报与战报测试指令共用。

        不写惩罚、不接复盘（复盘由调用方按配置追加）。
        """
        title = _undercover_title_text(game) or "本局已结束"
        winner = getattr(game, "winner", None) or {}
        winner_get = (
            (lambda key, _d=None: winner.get(key, _d))
            if isinstance(winner, dict)
            else (lambda key, _d=None: getattr(winner, key, _d))
        )
        camp = winner_get("camp")
        cw = winner_get("civilian_word") or ""
        uw = winner_get("undercover_word") or ""
        players = getattr(game, "players", None) or []
        lines = [f"🕵️ 谁是卧底战报：{title}"]
        if cw or uw:
            lines.append(f"📌 词条：平民「{cw}」/ 卧底「{uw}」")
        if camp:
            camp_text = {
                "civilian": "平民", "undercover": "卧底", "whiteboard": "白板",
            }.get(camp, camp)
            winners = [
                f"{getattr(p, 'number', '?')}号"
                + (f" {getattr(p, 'display_name', '')}" if getattr(p, 'display_name', '') else "")
                + f"（{camp_text}）"
                for p in players
                if getattr(p, "camp", None) == camp
            ]
            lines.append(
                f"🏆 胜利方：\n" + "\n".join(winners)
                if winners
                else f"🏆 胜利方：{camp_text}"
            )
        losers = [
            p for p in players
            if getattr(p, "camp", None) and p.camp != camp
        ]
        if losers:
            camp_text_of = {
                "civilian": "平民", "undercover": "卧底", "whiteboard": "白板",
            }
            names = "\n".join(
                f"{getattr(p, 'number', '?')}号"
                + (f" {getattr(p, 'display_name', '')}" if getattr(p, 'display_name', '') else "")
                + f"（{camp_text_of.get(getattr(p, 'camp', ''), getattr(p, 'camp', '') or '?')}）"
                for p in losers
            )
            lines.append(f"💔 失败方：\n{names}")
        return "\n".join(lines)

    @staticmethod
    def _undercover_sample_room(camp: str = "civilian") -> Any:
        """构造一局示例「谁是卧底」结果（含词条与四名玩家），供海报/战报测试指令使用。

        camp 支持 civilian/undercover/whiteboard，对应平民/卧底/白板获胜。
        """
        from types import SimpleNamespace

        alias = {
            "平民": "civilian", "civilian": "civilian",
            "卧底": "undercover", "undercover": "undercover",
            "白板": "whiteboard", "whiteboard": "whiteboard",
        }
        camp = alias.get((camp or "").strip().lower(), "civilian")
        words = {
            "civilian": ("苹果", "香蕉"),
            "undercover": ("月亮", "太阳"),
            "whiteboard": ("咖啡", "奶茶"),
        }[camp]
        players = [
            SimpleNamespace(number=1, display_name="张三", camp="civilian"),
            SimpleNamespace(number=2, display_name="李四", camp="civilian"),
            SimpleNamespace(number=3, display_name="王五", camp="undercover"),
            SimpleNamespace(number=4, display_name="赵六", camp="whiteboard"),
        ]
        game = SimpleNamespace(
            winner={
                "camp": camp,
                "civilian_word": words[0],
                "undercover_word": words[1],
            },
            players=players,
        )
        return SimpleNamespace(game=game, multiplayer=SimpleNamespace(seats=[]))

    def _board_sample_room(self, game_key: str = "") -> Any:
        """构造一局示例「棋类」终局，供棋类战报测试指令使用。

        game_key 支持「五子棋/黑白棋/中国象棋/围棋」（或其英文名），默认五子棋。
        构造后调用 manager 冻结终局快照，让战报海报带上对应的终局高亮效果：
        五子棋连五连线 / 象棋最后一手 / 黑白棋落子与子数。
        """
        from types import SimpleNamespace

        key = (game_key or "").strip().lower()
        alias = {
            "五子棋": "gomoku", "gomoku": "gomoku",
            "黑白棋": "othello", "黑白": "othello", "othello": "othello",
            "中国象棋": "xiangqi", "象棋": "xiangqi", "xiangqi": "xiangqi",
            "围棋": "weiqi", "weiqi": "weiqi", "go": "weiqi",
        }
        gt = alias.get(key, "gomoku")
        game = self._board_sample_game(gt)
        room = SimpleNamespace(
            game_type=gt,
            completed_games=1,
            difficulty="normal",
            status="finished",
            multiplayer=SimpleNamespace(seats=[]),
            game=game,
        )
        self.manager._snapshot_board_result(room)
        return room

    @staticmethod
    def _board_sample_game(game_type: str) -> Any:
        """构造一局示例终局（供 _board_sample_room 使用），每种棋类返回已分胜负的盘面。"""
        if game_type == "othello":
            from .othello import BLACK, OthelloGame

            game = OthelloGame(human_color=BLACK)
            game.board = [
                [1, 1, 1, 1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 2, 2],
                [1, 1, 1, 1, 2, 2, 2, 2],
                [1, 1, 1, 2, 2, 2, 2, 2],
                [1, 1, 1, 1, 2, 2, 2, 2],
                [1, 1, 1, 1, 1, 2, 2, 2],
                [1, 1, 1, 1, 1, 1, 1, 2],
            ]
            game.winner = BLACK
            game.draw = False
            game.history = [(2, 4, BLACK)]
            return game
        if game_type == "xiangqi":
            from .xiangqi import RED, XiangqiGame

            game = XiangqiGame(human_side=RED)
            # 中炮对屏风马开局的几步，最后一手（红方）作为海报高亮目标
            game.moves = ["h2e2", "h9g7", "h0g2", "i9h9", "c3c4", "g6g5"]
            game.winner = RED
            game.draw = False
            return game
        if game_type == "weiqi":
            from .weiqi import BLACK, WHITE, WeiqiGame

            game = WeiqiGame(human_color=BLACK, size=13)
            board = [[0] * 13 for _ in range(13)]
            for row, col in ((3, 3), (3, 5), (4, 4), (5, 3), (5, 5), (6, 6), (2, 6), (4, 2)):
                board[row][col] = BLACK
            for row, col in ((3, 4), (4, 3), (4, 5), (5, 4), (6, 4), (5, 6)):
                board[row][col] = WHITE
            game.board = board
            game.winner = BLACK
            game.draw = False
            game.history = [(5, 5, BLACK)]
            return game
        from .gomoku import BLACK, WHITE, GomokuGame

        game = GomokuGame(human_color=BLACK)
        for row, column, color in [
            (0, 0, BLACK), (1, 0, WHITE),
            (0, 1, BLACK), (1, 1, WHITE),
            (0, 2, BLACK), (1, 2, WHITE),
            (0, 3, BLACK), (1, 3, WHITE),
            (0, 4, BLACK),
        ]:
            game.place(row, column, color)
        return game

    async def _auto_announce_undercover(self, room: GameRoom) -> None:
        """局末自动播报（group_announce_auto）。

        与「群通报发送方式」配置保持一致：仅文字 → 纯文字；仅图片/文字+图片 → 服务端
        用 Pillow 生成「战况通知」海报随通报发送（无前端页面也能出图，缺 Pillow/中文字体
        时回退纯文字）。发送成功后写入与手动通知一致的幂等标记，避免随后手动点「通知到群」
        重复发送。
        """
        try:
            style = (getattr(self, "group_announce_style", "text") or "text").lower()
            image_data = ""
            if style in {"image", "both"}:
                try:
                    image_data = await self._undercover_announce_poster(room)
                except Exception as exc:
                    logger.warning("[GameCompanion] 自动播报生成海报失败，回退纯文字: %s", exc)
                    image_data = ""
            announce_text = await self.undercover_announce_result(room, image_data)
            game_obj = room.game
            uid = str(getattr(game_obj, "game_uid", "") or "") if game_obj else ""
            if uid and announce_text:
                room.last_announced_uid = uid
                room.last_announced_text = announce_text
        except Exception as exc:
            logger.warning("[GameCompanion] 谁是卧底自动播报失败: %s", exc)

    async def _auto_announce_board(self, room: GameRoom) -> None:
        """花火获胜自动播报（board.group_announce_auto）。

        与「群通报发送方式」配置保持一致：仅文字 → 纯文字；仅图片/文字+图片 → 服务端
        用 AstrBot t2i（回退 Pillow）生成「棋类战报」海报随通报发送，无前端页面也能出图
        （缺 t2i / Pillow / 中文字体时回退纯文字）。发送成功后写入与手动通知一致的幂等
        标记，避免随后手动点「通知到群」重复发送。

        节流分两层：
        1. 手数不足（board.group_announce_min_moves）→ 直接不发；
        2. 冷却窗口内（board.group_announce_cooldown_seconds）→ 不丢弃，先把本局攒进
           room.pending_board_announces。开启「玩家在房间内就攒着」时，等玩家离开
           玩家席或离线后再合并成一条「连战 N 局」（最多攒够兜底上限）；关闭时
           冷却窗口一过就发。
        """
        try:
            # 先读终局快照：玩家可能已点「再来一局」，room.game 已换成新局
            payload = self._board_result_payload(room)
            if payload is None:
                return
            # 节流：太短的速战不发，避免「0 手速战速决」刷群
            moves = int(payload.get("moves") or 0)
            if (
                self.board_group_announce_min_moves
                and moves < self.board_group_announce_min_moves
            ):
                return
            # 手动「通知到群」可能已经发过这一局：别重复刷群（两边共用同一幂等键）
            sent_key = f"board:{int(payload.get('completed_games') or 0)}"
            if room.last_announced_uid == sent_key:
                return
            entry = self._board_announce_entry(room)
            if entry is None:
                return
            cooldown = self.board_group_announce_cooldown
            if cooldown and room.last_board_announce_at:
                elapsed = time.time() - room.last_board_announce_at
                if elapsed < cooldown:
                    # 冷却窗口内：攒着，等玩家真的停下来（或退席）再合并成一条「连战 N 局」
                    self._queue_board_announce(room, entry)
                    self._schedule_board_flush(room, self._board_series_delay(elapsed))
                    return
            # 窗口刚好过去：把之前攒下的连战和本局并成一条发
            pending = self._take_board_pending(room)
            entry_key = (entry.get("completed_games"), entry.get("game_uid"))
            entries = [
                item
                for item in pending
                if (item.get("completed_games"), item.get("game_uid")) != entry_key
            ]
            entries.append(entry)
            if len(entries) > 1:
                await self._send_board_series(room, entries)
                return
            style = (getattr(self, "board_group_announce_style", "text") or "text").lower()
            image_data = ""
            if style in {"image", "both"}:
                try:
                    image_data = await self._board_announce_poster(room)
                except Exception as exc:
                    logger.warning("[GameCompanion] 棋类自动播报生成海报失败，回退纯文字: %s", exc)
                    image_data = ""
            announce_text = await self.board_announce_result(room, image_data)
            if announce_text:
                room.last_announced_uid = (
                    f"board:{int(getattr(room, 'completed_games', 0) or 0)}"
                )
                room.last_announced_text = announce_text
                room.last_board_announce_at = time.time()
        except Exception as exc:
            logger.warning("[GameCompanion] 棋类自动播报失败: %s", exc)

    def _board_announce_entry(self, room: GameRoom) -> dict[str, object] | None:
        """把本局结果压成一条「连战」摘要，供冷却窗口内合并播报。"""
        payload = self._board_result_payload(room)
        if payload is None:
            return None
        winner_token = str(payload.get("winner_token") or "")
        try:
            ai_seat = self.manager._ai_seat(room)
        except Exception:
            ai_seat = None
        winner_is_ai = bool(
            winner_token
            and ai_seat is not None
            and winner_token == getattr(ai_seat, "visitor_token", "")
        )
        human_won = payload.get("human_won")
        return {
            "game_type": str(payload.get("game_type") or room.game_type),
            "game_uid": str(payload.get("game_uid") or ""),
            "completed_games": int(payload.get("completed_games") or 0),
            "moves": int(payload.get("moves") or 0),
            "draw": bool(payload.get("draw")),
            "winner_name": str(payload.get("winner_name") or ""),
            "winner_side": str(payload.get("winner_side") or ""),
            # 连战文案要写清「谁输给了谁」，负方信息随条目一起存下来
            "loser_name": str(payload.get("loser_name") or ""),
            "loser_side": str(payload.get("loser_side") or ""),
            "winner_is_ai": winner_is_ai,
            "human_won": None if human_won is None else bool(human_won),
        }

    @staticmethod
    def _queue_board_announce(room: GameRoom, entry: dict[str, object]) -> None:
        """冷却窗口内结束的一局先入库（同局去重，最多保留 20 条）。"""
        pending = getattr(room, "pending_board_announces", None)
        if not isinstance(pending, list):
            pending = []
            room.pending_board_announces = pending
        key = (entry.get("completed_games"), entry.get("game_uid"))
        if any(
            (item.get("completed_games"), item.get("game_uid")) == key
            for item in pending
        ):
            return
        pending.append(entry)
        del pending[:-20]

    @staticmethod
    def _take_board_pending(room: GameRoom) -> list[dict[str, object]]:
        """取出并清空待合并队列（切过玩法/换了房间的旧条目按 game_type 丢掉）。"""
        pending = list(getattr(room, "pending_board_announces", None) or [])
        room.pending_board_announces = []
        return [
            item
            for item in pending
            if str(item.get("game_type") or room.game_type) == room.game_type
        ]

    @classmethod
    def _requeue_board_pending(
        cls, room: GameRoom, entries: list[dict[str, object]]
    ) -> None:
        """把没发出去的连战条目放回队首（取消 / 发送失败时用）。

        以前 `_flush_board_announces` 先出队再 await 发送：如果这个 await 期间
        被防抖重排 cancel 掉（玩家又开了一局），或群发送失败，这几局的战报就
        凭空消失了。现在放回队首，下一轮合并会一起带上。
        """
        if not entries:
            return
        for entry in reversed(entries):
            cls._queue_board_announce(room, entry)
        # _queue_board_announce 是 append + 截断，这里重排一次保证顺序（旧 -> 新）
        pending = getattr(room, "pending_board_announces", None)
        if isinstance(pending, list):
            pending.sort(key=lambda item: int(item.get("completed_games") or 0))

    def _board_series_delay(self, elapsed: float) -> float:
        """「攒着」关闭时的合并节奏：冷却窗口还需多久关闭。

        开启「玩家在房间内就攒着」时此值仅作调度占位，实际等待由
        _flush_board_announces 内的状态检查驱动（离席/离线立即发，兜底上限强制发）；
        关闭时退化为「冷却窗口一过就发」。
        """
        cooldown = getattr(self, "board_group_announce_cooldown", 30)
        return max(0.5, float(cooldown) - max(0.0, float(elapsed or 0)))

    def _board_wait_while_active(self) -> bool:
        """是否「玩家在房间内就攒着」（默认开）。"""
        return bool(getattr(self, "board_group_announce_wait_while_active", True))

    def _board_humans_all_gone(self, room: GameRoom) -> bool:
        """真人全部离开玩家席、或全部离线，即可立即发连战汇总。"""
        try:
            seats = self.manager._human_seats(room)
        except Exception:
            return False
        if not seats:
            return True
        now = time.time()
        offline = float(getattr(self, "board_group_offline_seconds", None) or BOARD_SERIES_OFFLINE_SECONDS)
        for seat in seats:
            visitor = room.visitors.get(seat.visitor_token)
            if visitor is None:
                continue
            if visitor.connected and now - visitor.last_seen_at < offline:
                return False  # 还有活跃真人
        return True

    async def _board_hold_until_humans_gone(self, room: GameRoom) -> None:
        """「攒着」模式下的等待：真人离席/离线立即返回，否则攒够兜底上限再返回。"""
        max_wait = float(getattr(self, "board_group_announce_series_idle", 1800) or 0)
        if max_wait <= 0:
            max_wait = float(getattr(self, "board_group_announce_cooldown", 30) or 30)
        deadline = time.time() + max_wait
        while True:
            if self._board_humans_all_gone(room):
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1.0, remaining))

    def _schedule_board_flush(self, room: GameRoom, delay: float) -> None:
        """安排一次「连战合并播报」；新对局进来时重新计时（防抖）。"""
        tasks = getattr(self, "_board_flush_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._board_flush_tasks = tasks
        existing = tasks.get(room.room_id)
        if existing is not None and not existing.done():
            # 还在连战：把旧的等待任务取消，按最新一局重新计时
            existing.cancel()
        room.pending_board_flush_at = time.time() + max(0.0, delay)
        tasks[room.room_id] = self._spawn(self._flush_board_announces(room, delay))

    def _flush_board_series_when_seats_empty(self, room: GameRoom) -> None:
        """真人全部离开玩家席后，连战汇总不必再等空闲计时，立即发。"""
        if not getattr(room, "pending_board_announces", None):
            return
        try:
            if self.manager._human_seats(room):
                return
        except Exception:
            return
        tasks = getattr(self, "_board_flush_tasks", None)
        if isinstance(tasks, dict):
            task = tasks.get(room.room_id)
            if task is not None and not task.done():
                task.cancel()
        self._spawn(self._flush_board_announces(room, 0.0))

    async def _flush_board_series_on_game_switch(
        self, room: GameRoom, old_game_type: str
    ) -> None:
        """换游戏时先把旧玩法的连战发出去，新玩法从第一局重新起算。

        旧玩法的 pending 条目带自己的 game_type，若不在这里清掉，下一轮
        `_take_board_pending` 会按「新 game_type」过滤把它们直接丢掉，群里
        永远看不到那几局的结果；而且新旧玩法挤进一条「连战 N 局」会张冠李戴。
        """
        pending = list(getattr(room, "pending_board_announces", None) or [])
        if not pending:
            return
        old_entries = [
            entry
            for entry in pending
            if str(entry.get("game_type") or "") == old_game_type
        ]
        room.pending_board_announces = [
            entry
            for entry in pending
            if str(entry.get("game_type") or "") != old_game_type
        ]
        # 取消还在等的 flush 任务，避免它读到切换后的状态重复发
        tasks = getattr(self, "_board_flush_tasks", None)
        if isinstance(tasks, dict):
            task = tasks.get(room.room_id)
            if task is not None and not task.done():
                task.cancel()
        if not old_entries:
            return
        try:
            await self._send_board_series(room, old_entries)
        except Exception as exc:
            logger.warning(
                "[GameCompanion] 换游戏时旧玩法连战播报失败: %s", exc
            )
        # 新玩法第一局要按「单局」正常通报，不能接上旧玩法的冷却窗口
        room.last_board_announce_at = 0.0
        room.last_announced_uid = ""

    async def _flush_board_announces(
        self, room: GameRoom, delay: float = 0.0
    ) -> None:
        """冷却窗口结束后，把攒下的对局合并成一条「连战 N 局」发到群里。"""
        entries: list[dict[str, object]] = []
        try:
            if delay > 0:
                if self._board_wait_while_active():
                    # 状态驱动：玩家还在房间里就一直攒着，离席/离线（或攒够兜底）才发
                    await self._board_hold_until_humans_gone(room)
                else:
                    await asyncio.sleep(delay)
            entries = self._take_board_pending(room)
            if not entries:
                return
            if await self._send_board_series(room, entries):
                entries = []
            else:
                # 群发失败（被踢/禁言/网络）不能吞掉这几局的战报：放回队里等下一轮
                self._requeue_board_pending(room, entries)
                entries = []
        except asyncio.CancelledError:
            # 睡着了被防抖重排取消：还没出队，entries 为空，无需回填
            if entries:
                self._requeue_board_pending(room, entries)
            raise
        except Exception as exc:
            logger.warning("[GameCompanion] 连战合并播报失败: %s", exc)
            self._requeue_board_pending(room, entries)
        finally:
            tasks = getattr(self, "_board_flush_tasks", None)
            if isinstance(tasks, dict):
                current = asyncio.current_task()
                if tasks.get(room.room_id) is current:
                    tasks.pop(room.room_id, None)
                    # 只有自己还是队首时才清时间戳：防抖重排时旧任务不能覆盖新计划
                    room.pending_board_flush_at = 0.0

    @staticmethod
    def _board_entry_result(entry: dict[str, object]) -> str:
        """单局胜负短句（不含手数）：连战列表与单局合并战报共用。"""
        side = {"red": "红", "black": "黑", "white": "白"}.get(
            str(entry.get("winner_side") or ""), ""
        )
        side_text = f"（执{side}）" if side else ""
        loser_side = {"red": "红", "black": "黑", "white": "白"}.get(
            str(entry.get("loser_side") or ""), ""
        )
        if entry.get("draw"):
            return "🤝 平局"
        if entry.get("winner_is_ai"):
            winner = f"花火{side_text}"
            loser = str(entry.get("loser_name") or "")
            if loser and loser_side:
                return f"🏆 {winner}胜 {loser}（执{loser_side}）"
            if loser:
                return f"🏆 {winner}胜 {loser}"
            return f"🏆 花火获胜{side_text} · 玩家落败"
        name = str(entry.get("winner_name") or "")
        loser = str(entry.get("loser_name") or "")
        if name and loser:
            loser_text = f"{loser}（执{loser_side}）" if loser_side else loser
            return f"🏆 {name}{side_text}胜 {loser_text}"
        if name:
            return f"🏆 {name}获胜{side_text}"
        # 单人局兜底（无座位可查胜者）：按 human_won 判负方，负方必是玩家或花火之一
        if entry.get("human_won") is False:
            return f"🏆 花火获胜{side_text} · 玩家落败"
        return f"🏆 玩家获胜{side_text} · 花火落败"

    def _board_series_text(
        self, room: GameRoom, entries: list[dict[str, object]]
    ) -> str:
        """连战合并文案：逐局胜负 + 手数，末尾总比分；单局按普通战报格式。

        以前单局也会被写成「连战 1 局」——玩家刚续战一场、还没停下来，冷却窗口内
        只有这一局，等它被 flush 时就冒出「连战 1 局」，读起来像系统误判了连战。
        """
        if not entries:
            return ""
        # 玩法名取条目自带的 game_type：换游戏时旧玩法的连战要赶在新玩法之前
        # 发出去，此时 room.game_type 已经是新玩法，直接读会把旧战报标成新玩法。
        label = self.manager._game_label(
            str(entries[0].get("game_type") or room.game_type)
        )
        if len(entries) == 1:
            entry = entries[0]
            moves = int(entry.get("moves") or 0)
            return f"♟️ {label} · {self._board_entry_result(entry)} · {moves} 手"
        shown = entries[:10]
        lines = [f"♟️ {label} · 连战 {len(entries)} 局"]
        for index, entry in enumerate(shown, 1):
            moves = int(entry.get("moves") or 0)
            lines.append(f"{index}. {self._board_entry_result(entry)} · {moves} 手")
        if len(entries) > len(shown):
            lines.append(f"…另有 {len(entries) - len(shown)} 局略")
        ai_wins = sum(1 for e in entries if e.get("winner_is_ai"))
        draws = sum(1 for e in entries if e.get("draw"))
        human_wins = sum(
            1 for e in entries if not e.get("draw") and not e.get("winner_is_ai")
        )
        tally = []
        if ai_wins:
            tally.append(f"花火 {ai_wins} 胜")
        if human_wins:
            tally.append(f"玩家 {human_wins} 胜")
        if draws:
            tally.append(f"{draws} 平")
        if tally:
            lines.append("📊 " + " · ".join(tally))
        return "\n".join(lines)

    async def _send_board_series(
        self, room: GameRoom, entries: list[dict[str, object]]
    ) -> bool:
        """发送「连战 N 局」合并战报（多局摘要只走文字，不再逐局出图）。"""
        text = self._board_series_text(room, entries)
        if not text:
            return False
        recap = ""
        if getattr(self, "board_group_announce_recap", True):
            try:
                recap = await self._board_series_recap(room, entries)
            except Exception:
                recap = ""
        sent = await self._send_to_origin(
            room, text + (f"\n📝 复盘：{recap}" if recap else "")
        )
        if not sent:
            return False
        latest = max(int(e.get("completed_games") or 0) for e in entries)
        room.last_announced_uid = f"board:{latest}"
        room.last_announced_text = text
        room.last_board_announce_at = time.time()
        return True

    async def _board_series_recap(
        self, room: GameRoom, entries: list[dict[str, object]]
    ) -> str:
        """为一段连战生成简短复盘（合并播报附带），LLM 不可用则返回空串。"""
        summary = self._board_series_text(room, entries).replace("\n", "；")
        if not summary:
            return ""
        single = len(entries) == 1
        topic = "这盘棋" if single else "这一串连续进行的棋类对局（连战）"
        prompt = (
            f"请为{topic}生成简短复盘（中文，2~3 句，"
            "轻松俏皮口吻，可以带点网络梗和适度 emoji，不要标题、不要编号、不要客套感谢语）。\n"
            f"对局情况：{summary}\n"
            "风格参考：像一起玩的朋友在茶余饭后聊这几盘棋，点一下整体走势"
            "（谁占上风、有没有被翻盘），绝不攻击或贬低任何一方。"
        )
        try:
            text = await self._llm_gen_neutral(
                prompt,
                system_prompt=(
                    "你是古灵精怪但温和的复盘解说员「花火」，点评轻松俏皮、善意不伤人，"
                    "只用中文输出复盘正文，不要标题、不要编号。"
                ),
            )
        except Exception:
            return ""
        return (text or "").strip()

    async def _undercover_announce_poster(self, room: GameRoom) -> str:
        """生成「战况通知」海报，返回 base64 PNG。

        优先用 AstrBot 的 t2i（HTML/Jinja2 → 图片，样式精美、支持 emoji）；
        t2i 服务不可用或超时时回退 Pillow 手绘。两者都失败则抛出异常，
        由调用方回退纯文字播报。
        """
        try:
            return await self._undercover_poster_t2i(room)
        except Exception as exc:
            logger.warning("[GameCompanion] t2i 渲染战况海报失败，回退 Pillow: %s", exc)
        return await asyncio.to_thread(self._undercover_poster_pillow, room)

    async def _undercover_poster_t2i(self, room: GameRoom) -> str:
        """用 AstrBot 的 t2i（HTML 渲染）生成战况海报，返回 base64 PNG。

        需要 AstrBot 配置可用的文转图服务（远程或本地，AstrBot 外观→文本转图像）；
        未提供 html_render / 服务不可用 / 超时均抛异常，由调用方回退 Pillow。
        """
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        data = self._undercover_poster_data(room)
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": 720,
            # 自建 t2i 服务下渲染开销低：用高分屏 DPR（1.8x）出更清晰的海报。
            # timeout 单位为毫秒（Playwright），传 20000ms；外层 wait_for 按秒兜底
            "device_scale_factor_level": "ultra",
            "timeout": 20000,
        }
        url = await asyncio.wait_for(
            render(_UNDERCOVER_POSTER_TMPL, data, options=options), timeout=8
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    @staticmethod
    async def _download_image_base64(url: str) -> str:
        """下载远程图片并转成 base64（避免依赖远程 URL 的时效性）。"""
        import aiohttp
        import base64

        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                raw = await response.read()
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _undercover_poster_data(room: GameRoom) -> dict[str, Any]:
        """提取海报所需数据（标题/阵营/词条/胜方/败方），供 t2i HTML 模板与 Pillow 共用。"""
        game = room.game
        winner = getattr(game, "winner", None) or {}
        winner_get = (
            (lambda key, _d=None: winner.get(key, _d))
            if isinstance(winner, dict)
            else (lambda key, _d=None: getattr(winner, key, _d))
        )
        camp = winner_get("camp")
        title = _undercover_title_text(game) or "本局已结束"
        cw = winner_get("civilian_word") or ""
        uw = winner_get("undercover_word") or ""
        players = getattr(game, "players", None) or []
        seats = getattr(room.multiplayer, "seats", None) or []
        seat_names = {
            int(s.number): str(getattr(s, "display_name", "") or "").strip()
            for s in seats
        }
        names = [
            (
                int(getattr(p, "number", 0)),
                str(getattr(p, "display_name", "") or "").strip()
                or seat_names.get(int(getattr(p, "number", 0)), ""),
                getattr(p, "camp", "") or "",
            )
            for p in players
        ]
        camp_cn_of = {"civilian": "平民", "undercover": "卧底", "whiteboard": "白板"}
        winners = [
            f"{num}号" + (f" {name}" if name else "")
            for num, name, p_camp in names if p_camp == camp
        ]
        losers = [
            f"{num}号" + (f" {name}" if name else "")
            + (f"（{camp_cn_of.get(p_camp, p_camp or '?')}）" if p_camp else "")
            for num, name, p_camp in names
            if p_camp and p_camp != camp
        ]
        return {
            "title": title,
            "camp_cn": camp_cn_of.get(camp, ""),
            "camp_color": {
                "civilian": "#7dffb0",
                "undercover": "#dda9ff",
                "whiteboard": "#9ccbff",
            }.get(camp, "#ffd166"),
            "winners": winners,
            "cw": cw,
            "uw": uw,
            "losers": losers,
        }

    def _undercover_poster_pillow(self, room: GameRoom) -> str:
        """Pillow 手绘「战况通知」海报（t2i 服务不可用时的兜底），返回 base64 PNG。

        仅文字/纯 ASCII 内容（无彩色 emoji，规避 Pillow 缺 emoji 字体时的方框）；缺 Pillow
        或找不到中文字体时抛出异常，由调用方回退纯文字播报。
        """
        from PIL import Image, ImageDraw, ImageFont  # 可选依赖，缺失时回退纯文字

        game = room.game
        winner = getattr(game, "winner", None) or {}
        winner_get = (
            (lambda key, _d=None: winner.get(key, _d))
            if isinstance(winner, dict)
            else (lambda key, _d=None: getattr(winner, key, _d))
        )
        camp = winner_get("camp")
        title = _undercover_title_text(game) or "本局已结束"
        cw = winner_get("civilian_word") or ""
        uw = winner_get("undercover_word") or ""
        players = getattr(game, "players", None) or []
        seats = getattr(room.multiplayer, "seats", None) or []
        seat_names = {
            int(s.number): str(getattr(s, "display_name", "") or "").strip()
            for s in seats
        }
        names = [
            (
                int(getattr(p, "number", 0)),
                str(getattr(p, "display_name", "") or "").strip()
                or seat_names.get(int(getattr(p, "number", 0)), ""),
                getattr(p, "camp", "") or "",
            )
            for p in players
        ]

        camp_cn = {"civilian": "平民", "undercover": "卧底", "whiteboard": "白板"}.get(
            camp, ""
        )
        camp_col = {
            "civilian": (125, 255, 176),
            "undercover": (221, 169, 255),
            "whiteboard": (156, 203, 255),
        }.get(camp, (255, 209, 102))

        # 文字绘制统一使用模块级 Pillow 助手（_pillow_*），支持中文/emoji 混排
        S = 1.5
        W, H, M = int(720 * S), int(720 * S), int(46 * S)
        img = Image.new("RGBA", (W, H), (32, 9, 7, 255))
        d = ImageDraw.Draw(img, "RGBA")
        # 背景渐变（深红「大字报」暖调，与分享战报的深蓝区分）
        top = (90, 28, 22)
        bottom = (32, 9, 7)
        for y in range(H):
            t = y / max(1, H - 1)
            d.line(
                [(0, y), (W, y)],
                fill=(
                    round(top[0] + (bottom[0] - top[0]) * t),
                    round(top[1] + (bottom[1] - top[1]) * t),
                    round(top[2] + (bottom[2] - top[2]) * t),
                    255,
                ),
            )
        # 斜纹装饰（半透明白色需手动预混，本环境形状填充不混合 alpha）
        stripe = _pillow_blend((61, 18, 14), (255, 255, 255), 12)
        for x in range(-H, W + H, int(36 * S)):
            d.line([(x, 0), (x + H, H)], fill=stripe)
        # 底部阵营色强调条
        d.rectangle([0, H - int(10 * S), W, H], fill=camp_col + (255,))

        # 顶部标题行
        title_font = _pillow_font(True, int(22 * S))
        ef_title = _pillow_emoji_font(int(22 * S))
        _pillow_draw_text(d, M, int(64 * S), "📢 谁是卧底 · 战况通知", title_font, (255, 217, 160, 255), efont=ef_title)
        right_font = _pillow_font(False, int(16 * S))
        ef_right = _pillow_emoji_font(int(16 * S))
        _pillow_draw_text(
            d, W - M, int(64 * S),
            _pillow_truncate(d, title, W - 2 * M - int(200 * S), right_font, ef_right),
            right_font, (255, 255, 255, 153), anchor="rs", efont=ef_right,
        )

        # 中央大字：获胜阵营（描边 + 阵营色，大字报醒目感）
        big_text = f"{camp_cn or '本局'} 获胜"
        big_font = _pillow_font(True, int(88 * S))
        if _pillow_font_length(d, big_text, big_font) > W - 2 * M:
            big_font = _pillow_font(True, int(72 * S))
        big_y = int(215 * S)
        d.text(
            (W / 2, big_y),
            big_text,
            font=big_font,
            fill=camp_col + (255,),
            anchor="mm",
            stroke_width=int(12 * S),
            stroke_fill=(255, 255, 255),
        )

        # 获胜玩家名单（金色，最多 3 行）
        winners = [
            f"{num}号" + (f" {name}" if name else "")
            for num, name, p_camp in names if p_camp == camp
        ]
        w_line_count = 0
        if winners:
            w_font = _pillow_font(True, int(26 * S))
            ef_w = _pillow_emoji_font(int(26 * S))
            lines = _pillow_wrap(d, "🎉 " + "\n".join(winners), W - 2 * M, w_font, 3, ef_w)
            w_line_count = len(lines)
            for i, line in enumerate(lines):
                _pillow_draw_text(
                    d, W / 2, int(330 * S) + i * int(40 * S),
                    line, w_font, (255, 227, 176, 255), anchor="mm", efont=ef_w,
                )

        # 词条双药丸
        def pill(text: str, x: int, y: int, w: int, fill_col: tuple[int, int, int, int], f: Any, ef: Any = None) -> None:
            d.rounded_rectangle([x, y, x + w, y + int(46 * S)], radius=int(23 * S), fill=fill_col)
            _pillow_draw_text(
                d, x + w / 2, y + int(23 * S), text, f, (255, 255, 255, 255),
                anchor="mm", efont=ef,
            )

        if cw or uw:
            pill_top = int(356 * S) + max(1, w_line_count) * int(40 * S)
            pill_font = _pillow_font(True, int(20 * S))
            ef_p = _pillow_emoji_font(int(20 * S))
            cw_t = _pillow_truncate(d, f"🛡️ 平民「{cw}」", int(300 * S), pill_font, ef_p)
            uw_t = _pillow_truncate(d, f"🕵️ 卧底「{uw}」", int(300 * S), pill_font, ef_p)
            cw_w = int(_pillow_font_length(d, cw_t, pill_font, ef_p)) + int(34 * S)
            uw_w = int(_pillow_font_length(d, uw_t, pill_font, ef_p)) + int(34 * S)
            gap = int(20 * S)
            x0 = int((W - (cw_w + gap + uw_w)) / 2)
            if x0 >= M:
                pill(cw_t, x0, pill_top, cw_w, _pillow_blend((61, 18, 14), (125, 255, 176), 41), pill_font, ef_p)
                pill(uw_t, x0 + cw_w + gap, pill_top, uw_w, _pillow_blend((61, 18, 14), (221, 169, 255), 41), pill_font, ef_p)
            else:
                small = _pillow_font(True, int(18 * S))
                ef_s = _pillow_emoji_font(int(18 * S))
                cw2 = _pillow_truncate(d, f"🛡️ 平民「{cw}」", int(440 * S), small, ef_s)
                uw2 = _pillow_truncate(d, f"🕵️ 卧底「{uw}」", int(440 * S), small, ef_s)
                w1 = int(_pillow_font_length(d, cw2, small, ef_s)) + int(34 * S)
                w2 = int(_pillow_font_length(d, uw2, small, ef_s)) + int(34 * S)
                pill(cw2, int((W - w1) / 2), pill_top, w1, _pillow_blend((61, 18, 14), (125, 255, 176), 41), small, ef_s)
                pill(uw2, int((W - w2) / 2), pill_top + int(56 * S), w2, _pillow_blend((61, 18, 14), (221, 169, 255), 41), small, ef_s)

        # 底部失败方
        camp_cn_of = {"civilian": "平民", "undercover": "卧底", "whiteboard": "白板"}
        losers = [
            f"{num}号" + (f" {name}" if name else "")
            + (f"（{camp_cn_of.get(p_camp, p_camp or '?')}）" if p_camp else "")
            for num, name, p_camp in names
            if p_camp and p_camp != camp
        ]
        if losers:
            l_font = _pillow_font(False, int(17 * S))
            ef_l = _pillow_emoji_font(int(17 * S))
            lines = _pillow_wrap(d, "💔 败方：" + "\n".join(losers), W - 2 * M - int(150 * S), l_font, 2, ef_l)
            for i, line in enumerate(lines):
                _pillow_draw_text(
                    d, M, int(636 * S) + i * int(26 * S),
                    line, l_font, (255, 255, 255, 140), anchor="ls", efont=ef_l,
                )

        # 右下角水印
        d.text(
            (W - M, H - int(32 * S)),
            "由 花火 监督生成",
            font=_pillow_font(False, int(14 * S)),
            fill=(255, 255, 255, 102),
            anchor="rs",
        )

        import base64
        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _undercover_report_data(
        self, limit: int = 20, subtitle: str = "日报"
    ) -> dict[str, Any]:
        """提取战绩榜数据（全局榜前 limit，前 3 名金银铜），供日报海报 t2i/Pillow 共用。"""
        rows = self.manager.global_leaderboard("undercover", limit=limit)
        medals = ["🥇", "🥈", "🥉"]
        items = [
            {
                "rank": medals[index] if index < 3 else f"#{index + 1}",
                "name": str(row.get("name") or "").strip() or f"玩家{index + 1}",
                "wins": int(row.get("wins") or 0),
            }
            for index, row in enumerate(rows)
        ]
        max_wins = max((item["wins"] for item in items), default=0)
        for item in items:
            item["pct"] = round(item["wins"] / max_wins * 100) if max_wins else 0
        return {"subtitle": subtitle, "rows": items, "max_wins": max_wins}

    async def _undercover_report_poster(self, data: dict[str, Any]) -> str:
        """生成战绩日报/周报排行榜海报，返回 base64 PNG。

        优先 AstrBot t2i（HTML 渲染），失败回退 Pillow；都失败时抛出异常，
        由调用方回退纯文字榜单。
        """
        try:
            return await self._undercover_report_poster_t2i(data)
        except Exception as exc:
            logger.warning("[GameCompanion] t2i 渲染战绩海报失败，回退 Pillow: %s", exc)
        return await asyncio.to_thread(self._undercover_report_poster_pillow, data)

    async def _undercover_report_poster_t2i(self, data: dict[str, Any]) -> str:
        """用 AstrBot 的 t2i（HTML 渲染）生成战绩排行榜海报，返回 base64 PNG。"""
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": 720,
            # 自建 t2i 服务下渲染开销低：用高分屏 DPR（1.8x）出更清晰的海报。
            # timeout 单位为毫秒（Playwright），传 20000ms；外层 wait_for 按秒兜底
            "device_scale_factor_level": "ultra",
            "timeout": 20000,
        }
        url = await asyncio.wait_for(
            render(_UNDERCOVER_REPORT_TMPL, data, options=options), timeout=8
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    @staticmethod
    def _undercover_report_poster_pillow(data: dict[str, Any]) -> str:
        """Pillow 手绘战绩排行榜海报（t2i 不可用时的兜底，与 HTML 版同款榜单卡风格）。"""
        from PIL import Image, ImageDraw

        rows = data.get("rows") or []
        subtitle = str(data.get("subtitle") or "")
        S = 1.5
        W = int(720 * S)
        pad = int(44 * S)
        header_top = int(40 * S)
        icon_h = int(54 * S)
        rule_gap = int(26 * S)
        row_h = int(56 * S)
        row_gap = int(10 * S)
        accent_h = int(6 * S)
        rows_h = max(1, len(rows)) * (row_h + row_gap)
        H = (
            header_top + icon_h + rule_gap + rows_h
            + int(30 * S) + int(22 * S) + accent_h + int(26 * S)
        )
        img = Image.new("RGBA", (W, H), (12, 21, 38, 255))
        d = ImageDraw.Draw(img, "RGBA")
        # 深蓝渐变背景
        top_c = (28, 43, 78)
        bottom_c = (12, 21, 38)
        for y in range(H):
            t = y / max(1, H - 1)
            d.line(
                [(0, y), (W, y)],
                fill=(
                    round(top_c[0] + (bottom_c[0] - top_c[0]) * t),
                    round(top_c[1] + (bottom_c[1] - top_c[1]) * t),
                    round(top_c[2] + (bottom_c[2] - top_c[2]) * t),
                    255,
                ),
            )
        ef_h = _pillow_emoji_font(int(30 * S))
        ef_t = _pillow_emoji_font(int(26 * S))
        base = (18, 30, 52)  # 榜单卡背景代表色（用于手动预混半透明形状填充）
        # 标题区：图标块 + 标题 + 副标题 + 右上角药丸
        icon_x = pad
        icon_y = header_top
        d.rounded_rectangle(
            [icon_x, icon_y, icon_x + icon_h, icon_y + icon_h],
            radius=int(14 * S), fill=_pillow_blend(base, (255, 215, 130), 41),
            outline=_pillow_blend(base, (255, 215, 130), 89), width=int(2 * S),
        )
        _pillow_draw_text(d, icon_x + icon_h / 2, icon_y + icon_h / 2, "📊", _pillow_font(True, int(30 * S)), (255, 224, 150, 255), anchor="mm", efont=ef_h)
        title_f = _pillow_font(True, int(26 * S))
        _pillow_draw_text(d, icon_x + icon_h + int(16 * S), icon_y + int(6 * S), "谁是卧底 · 战绩报告", title_f, (255, 224, 150, 255), efont=ef_t)
        sub_f = _pillow_font(False, int(13 * S))
        _pillow_draw_text(d, icon_x + icon_h + int(16 * S), icon_y + int(6 * S) + int(36 * S), f"跨房间全局战绩榜 · 前 {len(rows)} 名", sub_f, (255, 255, 255, 128), efont=ef_t)
        badge_f = _pillow_font(True, int(15 * S))
        bw = int(_pillow_font_length(d, subtitle, badge_f)) + int(36 * S)
        bh = int(30 * S)
        bx = W - pad - bw
        by = icon_y + (icon_h - bh) // 2
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(255, 215, 130, 255))
        _pillow_draw_text(d, bx + bw / 2, by + bh / 2, subtitle, badge_f, (58, 42, 8, 255), anchor="mm", efont=ef_t)
        # 分隔线
        rule_y = header_top + icon_h + rule_gap
        d.line([(pad, rule_y), (W - pad, rule_y)], fill=_pillow_blend(base, (255, 255, 255), 46), width=int(2 * S))
        # 榜单行
        row_x0 = pad
        row_x1 = W - pad
        rank_r = int(20 * S)
        rank_cx = row_x0 + int(20 * S)
        name_x = rank_cx + rank_r + int(16 * S)
        bar_w = int(120 * S)
        bar_h = int(8 * S)
        wins_f = _pillow_font(True, int(19 * S))
        ef_w = _pillow_emoji_font(int(19 * S))
        bar_right = W - pad - int(90 * S)
        name_max_w = bar_right - name_x - bar_w - int(34 * S)
        row_top = rule_y + int(20 * S)
        if not rows:
            nf = _pillow_font(False, int(20 * S))
            _pillow_draw_text(d, W / 2, row_top + row_h, "当前暂无卧底战绩记录。", nf, (255, 255, 255, 140), anchor="mm", efont=ef_t)
        else:
            for index, row in enumerate(rows):
                y = row_top + index * (row_h + row_gap)
                is_top = index < 3
                if is_top:
                    d.rounded_rectangle([row_x0, y, row_x1, y + row_h], radius=int(14 * S), fill=_pillow_blend(base, (255, 215, 130), 28))
                    d.rounded_rectangle([row_x0, y, row_x1, y + row_h], radius=int(14 * S), outline=_pillow_blend(base, (255, 215, 130), 66), width=int(2 * S))
                else:
                    d.rounded_rectangle([row_x0, y, row_x1, y + row_h], radius=int(14 * S), fill=_pillow_blend(base, (255, 255, 255), 12))
                cy = y + row_h / 2
                # 排名圆章
                if is_top:
                    d.ellipse([rank_cx - rank_r, cy - rank_r, rank_cx + rank_r, cy + rank_r], fill=(255, 215, 130, 255))
                    rank_f = _pillow_font(True, int(20 * S))
                    ef_r = _pillow_emoji_font(int(22 * S))
                    _pillow_draw_text(d, rank_cx, cy, str(row.get("rank") or ""), rank_f, (58, 42, 8, 255), anchor="mm", efont=ef_r)
                else:
                    d.ellipse([rank_cx - rank_r, cy - rank_r, rank_cx + rank_r, cy + rank_r], fill=_pillow_blend(base, (255, 255, 255), 20))
                    rank_f = _pillow_font(True, int(17 * S))
                    ef_r = _pillow_emoji_font(int(17 * S))
                    _pillow_draw_text(d, rank_cx, cy, str(row.get("rank") or ""), rank_f, (255, 255, 255, 191), anchor="mm", efont=ef_r)
                # 名字
                name_f = _pillow_font(False, int(20 * S))
                name = _pillow_truncate(d, str(row.get("name") or ""), name_max_w, name_f, ef_r)
                _pillow_draw_text(d, name_x, cy, name, name_f, (255, 255, 255, 240), anchor="lm", efont=ef_r)
                # 进度条
                bar_left = bar_right - bar_w
                bar_y = cy - bar_h / 2
                d.rounded_rectangle([bar_left, bar_y, bar_right, bar_y + bar_h], radius=bar_h // 2, fill=_pillow_blend(base, (0, 0, 0), 40) if is_top else _pillow_blend(base, (255, 255, 255), 26))
                pct = max(0, min(100, int(row.get("pct") or 0)))
                if pct > 0:
                    fill_w = max(int(bar_w * pct / 100), int(4 * S))
                    fill_col = (245, 166, 35, 255) if is_top else _pillow_blend(base, (255, 255, 255), 92)
                    d.rounded_rectangle([bar_left, bar_y, bar_left + fill_w, bar_y + bar_h], radius=bar_h // 2, fill=fill_col)
                # 胜场
                wins_text = f"{int(row.get('wins') or 0)} 胜"
                _pillow_draw_text(d, W - pad - int(16 * S), cy, wins_text, wins_f, (255, 224, 150, 255), anchor="rm", efont=ef_w)
        # 底部与强调条
        ff = _pillow_font(False, int(13 * S))
        _pillow_draw_text(d, W / 2, H - accent_h - int(30 * S), "数据来自跨房间全局战绩榜 · 由 花火 监督生成", ff, (255, 255, 255, 115), anchor="mm", efont=ef_t)
        d.rectangle([0, H - accent_h, W, H], fill=(255, 190, 92, 255))
        import base64
        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def _send_to_origin_image(
        self, room: GameRoom, text: str, image_data_url: str, recap: str = ""
    ) -> bool:
        """把文字（可选）、一张战报图与复盘（可选）发到开房群，复盘放在图片后面。

        图片上方空一行（文字末尾补空行）、下方也空一行再接复盘，视觉更透气。
        """
        try:
            from astrbot.api.message_components import Image

            parts: list[Any] = []
            if text:
                parts.append(Plain(text.rstrip() + "\n\n"))  # 失败方后空一行再接图片
            b64 = str(image_data_url or "").split(",", 1)[-1]
            if b64:
                parts.append(Image(file=f"base64://{b64}"))
            if recap:
                parts.append(Plain(f"\n\n📝 复盘：{recap}"))  # 图片下方空一行再接复盘
            if parts:
                target = self.announce_target(room)
                if not target:
                    return False
                if await self._deliver(target, MessageChain(parts), what="战报图"):
                    return True
        except Exception as exc:
            logger.warning("[GameCompanion] 发送战报图片失败，回退纯文字: %s", exc)
        if text or recap:
            return await self._send_to_origin(
                room, (text + (f"\n📝 复盘：{recap}" if recap else "")).strip()
            )
        return False

    def _register_report_scheduler(self) -> None:
        """用 AstrBot 自带的 APScheduler（context.cron_manager.scheduler）注册卧底日报/周报定时任务。

        参考社区插件做法：用 CronTrigger 注册到 cron_manager，由框架调度，插件无需自建轮询。
        开关关闭或白名单为空时则不注册。
        """
        self._unregister_report_scheduler()
        if not getattr(self, "undercover_report_enabled", False):
            return
        groups = [g.strip() for g in re.split(r"[\s,，;；]+", str(getattr(self, "undercover_report_groups", "") or "")) if g.strip()]
        if not groups:
            return
        try:
            from apscheduler.triggers.cron import CronTrigger

            scheduler = getattr(getattr(self.context, "cron_manager", None), "scheduler", None)
            if scheduler is None or not callable(getattr(scheduler, "add_job", None)):
                logger.warning("[GameCompanion] AstrBot 未提供 cron_manager，日报/周报定时不可用")
                return
            hour, minute = self._parse_report_time()
            mode = (getattr(self, "undercover_report_mode", "daily") or "daily").lower()
            weekday = max(0, min(6, int(getattr(self, "undercover_report_weekday", 1) or 1)))
            trigger = (
                CronTrigger(hour=hour, minute=minute)
                if mode != "weekly"
                else CronTrigger(day_of_week=weekday, hour=hour, minute=minute)
            )
            scheduler.add_job(
                self._run_scheduled_report,
                trigger=trigger,
                id="astrbot_plugin_game_companion_undercover_report",
                replace_existing=True,
                misfire_grace_time=120,
            )
            logger.info(
                "[GameCompanion] 已注册卧底报表定时任务（%s %02d:%02d，白名单 %d 个）",
                "周" if mode != "daily" else "日", hour, minute, len(groups),
            )
        except Exception as exc:
            logger.warning("[GameCompanion] 注册卧底报表定时任务失败: %s", exc)

    def _unregister_report_scheduler(self) -> None:
        try:
            scheduler = getattr(getattr(self.context, "cron_manager", None), "scheduler", None)
            if scheduler is not None and callable(getattr(scheduler, "get_job", None)):
                job_id = "astrbot_plugin_game_companion_undercover_report"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
        except Exception:
            pass

    def _parse_report_time(self) -> tuple[int, int]:
        try:
            h, m = str(getattr(self, "undercover_report_time", "09:00") or "09:00").split(":")
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        except Exception:
            return 9, 0

    async def _run_scheduled_report(self) -> None:
        """APScheduler 定时触发：把卧底战绩报告发到白名单群。"""
        if not getattr(self, "undercover_report_enabled", False):
            return
        groups = [g.strip() for g in re.split(r"[\s,，;；]+", str(getattr(self, "undercover_report_groups", "") or "")) if g.strip()]
        if not groups:
            return
        text = self._build_undercover_report()
        for umoid in groups:
            self._spawn(self._send_report_piece(umoid, text))
        logger.info("[GameCompanion] 已触发卧底战绩报表推送")

    async def _send_report_piece(self, umoid: str, text: str) -> None:
        # 白名单里的会话串可能写的是旧的平台 id，发送前统一校正一遍
        await self._deliver(umoid, MessageChain([Plain(text)]), what="战绩报表")

    @staticmethod
    def _report_window_days(mode: str) -> int:
        """报表统计窗口：日报=最近 1 天，周报=最近 7 天，其它/未知=累计(0)。"""
        return {"daily": 1, "weekly": 7}.get(str(mode or "").lower(), 0)

    def _build_undercover_report(self) -> str:
        """拼一份谁是卧底战绩日报/周报文本（按时间窗口取全局胜场榜，按名合并）。"""
        mode = str(getattr(self, "undercover_report_mode", "daily") or "daily")
        days = self._report_window_days(mode)
        rows = self.manager.global_leaderboard("undercover", limit=20, days=days or None)
        scope = "累计" if not days else ("最近 1 天" if days == 1 else f"最近 {days} 天")
        fallback = False
        if days and not rows:
            # 升级后前几天可能还没有按天数据：不要发一张空榜，退回累计并注明
            rows = self.manager.global_leaderboard("undercover", limit=20)
            scope = "累计（该时段暂无新对局）"
            fallback = True
        lines = ["📊 谁是卧底 · 战绩报告", f"统计范围：{scope}", "——————"]
        if not rows:
            lines.append("当前暂无卧底战绩记录。")
            return "\n".join(lines)
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            rank = medals[i] if i < 3 else f"#{i + 1}"
            lines.append(f"{rank} {row['name']}：{int(row['wins'] or 0)} 胜")
        lines.append("——————")
        lines.append(
            "数据来自跨房间全局战绩榜。" if not fallback else "数据来自跨房间全局战绩榜（累计）。"
        )
        return "\n".join(lines)

    def _register_board_report_scheduler(self) -> None:
        """注册棋类战绩日报/周报定时任务（与卧底同构，用 board.* 配置段）。"""
        self._unregister_board_report_scheduler()
        if not getattr(self, "board_report_enabled", False):
            return
        groups = [g.strip() for g in re.split(r"[\s,，;；]+", str(getattr(self, "board_report_groups", "") or "")) if g.strip()]
        if not groups:
            return
        try:
            from apscheduler.triggers.cron import CronTrigger

            scheduler = getattr(getattr(self.context, "cron_manager", None), "scheduler", None)
            if scheduler is None or not callable(getattr(scheduler, "add_job", None)):
                logger.warning("[GameCompanion] AstrBot 未提供 cron_manager，棋类日报/周报定时不可用")
                return
            hour, minute = self._parse_board_report_time()
            mode = (getattr(self, "board_report_mode", "daily") or "daily").lower()
            weekday = max(0, min(6, int(getattr(self, "board_report_weekday", 1) or 1)))
            trigger = (
                CronTrigger(hour=hour, minute=minute)
                if mode != "weekly"
                else CronTrigger(day_of_week=weekday, hour=hour, minute=minute)
            )
            scheduler.add_job(
                self._run_board_report,
                trigger=trigger,
                id="astrbot_plugin_game_companion_board_report",
                replace_existing=True,
                misfire_grace_time=120,
            )
            logger.info(
                "[GameCompanion] 已注册棋类报表定时任务（%s %02d:%02d，白名单 %d 个）",
                "周" if mode != "daily" else "日", hour, minute, len(groups),
            )
        except Exception as exc:
            logger.warning("[GameCompanion] 注册棋类报表定时任务失败: %s", exc)

    def _unregister_board_report_scheduler(self) -> None:
        try:
            scheduler = getattr(getattr(self.context, "cron_manager", None), "scheduler", None)
            if scheduler is not None and callable(getattr(scheduler, "get_job", None)):
                job_id = "astrbot_plugin_game_companion_board_report"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
        except Exception:
            pass

    def _parse_board_report_time(self) -> tuple[int, int]:
        try:
            h, m = str(getattr(self, "board_report_time", "09:00") or "09:00").split(":")
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        except Exception:
            return 9, 0

    async def _run_board_report(self) -> None:
        """APScheduler 定时触发：把棋类战绩报告发到白名单群。"""
        if not getattr(self, "board_report_enabled", False):
            return
        groups = [g.strip() for g in re.split(r"[\s,，;；]+", str(getattr(self, "board_report_groups", "") or "")) if g.strip()]
        if not groups:
            return
        text = self._build_board_report()
        for umoid in groups:
            self._spawn(self._send_report_piece(umoid, text))
        logger.info("[GameCompanion] 已触发棋类战绩报表推送")

    def _board_report_data(
        self, subtitle: str = "日报", limit_per_game: int = 3
    ) -> dict[str, Any]:
        """提取棋类战绩榜数据（四种棋类各取前几名），供棋类日报海报 t2i/Pillow 共用。"""
        game_types = ["gomoku", "othello", "xiangqi", "weiqi"]
        labels = {
            "gomoku": "五子棋", "othello": "黑白棋",
            "xiangqi": "中国象棋", "weiqi": "围棋",
        }
        icons = {"gomoku": "🎯", "othello": "⚪", "xiangqi": "♟️", "weiqi": "⛰️"}
        mode = str(getattr(self, "board_report_mode", "daily") or "daily")
        days = self._report_window_days(mode)
        scope = "累计" if not days else ("最近 1 天" if days == 1 else f"最近 {days} 天")
        medals = ["🥇", "🥈", "🥉"]
        sections: list[dict[str, Any]] = []
        for gt in game_types:
            rows = self.manager.global_leaderboard(
                gt, limit=limit_per_game, days=days or None
            )
            if not rows and days:
                rows = self.manager.global_leaderboard(gt, limit=limit_per_game)
            if not rows:
                continue
            items = [
                {
                    "rank": medals[index] if index < 3 else f"#{index + 1}",
                    "name": str(row.get("name") or "").strip() or f"玩家{index + 1}",
                    "wins": int(row.get("wins") or 0),
                    "winrate": int(round(float(row.get("winrate") or 0))),
                }
                for index, row in enumerate(rows)
            ]
            sections.append({"icon": icons[gt], "label": labels[gt], "rows": items})
        return {"subtitle": subtitle, "scope": scope, "sections": sections}

    async def _board_report_poster(self, data: dict[str, Any]) -> str:
        """生成棋类战绩日报/周报排行榜海报，返回 base64 PNG。

        优先 AstrBot t2i（HTML 渲染），失败回退 Pillow；都失败时抛出异常，
        由调用方回退纯文字榜单。
        """
        try:
            return await self._board_report_poster_t2i(data)
        except Exception as exc:
            logger.warning("[GameCompanion] t2i 渲染棋类日报海报失败，回退 Pillow: %s", exc)
        return await asyncio.to_thread(self._board_report_poster_pillow, data)

    async def _board_report_poster_t2i(self, data: dict[str, Any]) -> str:
        """用 AstrBot 的 t2i（HTML 渲染）生成棋类排行榜海报，返回 base64 PNG。"""
        render = getattr(self, "html_render", None)
        if not callable(render):
            raise RuntimeError("当前 AstrBot 未提供 html_render（t2i）能力")
        options = {
            "type": "png",
            "full_page": True,
            "viewport_width": 720,
            "device_scale_factor_level": "ultra",
            "timeout": 20000,
        }
        url = await asyncio.wait_for(
            render(_BOARD_REPORT_TMPL, data, options=options), timeout=8
        )
        if not url:
            raise RuntimeError("t2i 渲染返回空结果")
        return await self._download_image_base64(url)

    @staticmethod
    def _board_report_poster_pillow(data: dict[str, Any]) -> str:
        """Pillow 手绘棋类排行榜海报（t2i 不可用时的兜底，与 HTML 版同款榜单卡风格）。"""
        from PIL import Image, ImageDraw

        sections = data.get("sections") or []
        subtitle = str(data.get("subtitle") or "")
        scope = str(data.get("scope") or "")
        S = 1.5
        W = int(720 * S)
        pad = int(44 * S)
        header_top = int(40 * S)
        icon_h = int(54 * S)
        rule_gap = int(26 * S)
        card_gap = int(14 * S)
        card_pad_y = int(14 * S)
        card_pad_x = int(18 * S)
        card_head_h = int(32 * S)
        row_h = int(38 * S)
        row_gap = int(4 * S)
        accent_h = int(6 * S)
        cards_top = int(20 * S)
        footer_h = int(30 * S)

        def card_height(n_rows: int) -> int:
            return card_pad_y * 2 + card_head_h + max(1, n_rows) * (row_h + row_gap)

        if sections:
            cards_h = sum(card_height(len(s.get("rows") or [])) for s in sections)
            cards_h += max(0, len(sections) - 1) * card_gap
            body_h = cards_top + cards_h
        else:
            body_h = cards_top + int(96 * S)
        H = (
            header_top + icon_h + rule_gap + body_h + footer_h
            + int(22 * S) + accent_h + int(26 * S)
        )
        img = Image.new("RGBA", (W, H), (12, 21, 38, 255))
        d = ImageDraw.Draw(img, "RGBA")
        top_c = (28, 43, 78)
        bottom_c = (12, 21, 38)
        for y in range(H):
            t = y / max(1, H - 1)
            d.line(
                [(0, y), (W, y)],
                fill=(
                    round(top_c[0] + (bottom_c[0] - top_c[0]) * t),
                    round(top_c[1] + (bottom_c[1] - top_c[1]) * t),
                    round(top_c[2] + (bottom_c[2] - top_c[2]) * t),
                    255,
                ),
            )
        ef_h = _pillow_emoji_font(int(30 * S))
        ef_t = _pillow_emoji_font(int(26 * S))
        base = (18, 30, 52)
        # 标题区
        icon_x = pad
        icon_y = header_top
        d.rounded_rectangle(
            [icon_x, icon_y, icon_x + icon_h, icon_y + icon_h],
            radius=int(14 * S), fill=_pillow_blend(base, (255, 215, 130), 41),
            outline=_pillow_blend(base, (255, 215, 130), 89), width=int(2 * S),
        )
        _pillow_draw_text(
            d, icon_x + icon_h / 2, icon_y + icon_h / 2, "♟️",
            _pillow_font(True, int(30 * S)), (255, 224, 150, 255), anchor="mm", efont=ef_h,
        )
        title_f = _pillow_font(True, int(26 * S))
        _pillow_draw_text(
            d, icon_x + icon_h + int(16 * S), icon_y + int(6 * S),
            "棋类对局 · 战绩报告", title_f, (255, 224, 150, 255), efont=ef_t,
        )
        sub_f = _pillow_font(False, int(13 * S))
        _pillow_draw_text(
            d, icon_x + icon_h + int(16 * S), icon_y + int(6 * S) + int(36 * S),
            f"跨房间全局战绩榜 · {scope}", sub_f, (255, 255, 255, 128), efont=ef_t,
        )
        badge_f = _pillow_font(True, int(15 * S))
        bw = int(_pillow_font_length(d, subtitle, badge_f)) + int(36 * S)
        bh = int(30 * S)
        bx = W - pad - bw
        by = icon_y + (icon_h - bh) // 2
        d.rounded_rectangle(
            [bx, by, bx + bw, by + bh], radius=bh // 2, fill=(255, 215, 130, 255)
        )
        _pillow_draw_text(
            d, bx + bw / 2, by + bh / 2, subtitle, badge_f, (58, 42, 8, 255),
            anchor="mm", efont=ef_t,
        )
        # 分隔线
        rule_y = header_top + icon_h + rule_gap
        d.line([(pad, rule_y), (W - pad, rule_y)], fill=_pillow_blend(base, (255, 255, 255), 46), width=int(2 * S))
        # 分区卡片
        y = rule_y + cards_top
        if not sections:
            nf = _pillow_font(False, int(20 * S))
            _pillow_draw_text(
                d, W / 2, y + int(48 * S), "当前暂无棋类战绩记录。",
                nf, (255, 255, 255, 140), anchor="mm", efont=ef_t,
            )
        head_f = _pillow_font(True, int(18 * S))
        name_f = _pillow_font(False, int(18 * S))
        stat_f = _pillow_font(True, int(15 * S))
        ef_r = _pillow_emoji_font(int(16 * S))
        ef_s = _pillow_emoji_font(int(15 * S))
        rank_r = int(13 * S)
        name_max_w = int(340 * S)
        for section in sections:
            rows = section.get("rows") or []
            card_h = card_height(len(rows))
            d.rounded_rectangle(
                [pad, y, W - pad, y + card_h], radius=int(16 * S),
                fill=_pillow_blend(base, (255, 255, 255), 12),
                outline=_pillow_blend(base, (255, 255, 255), 24), width=int(2 * S),
            )
            cy = y + card_pad_y + int(16 * S)
            # 图标与文字分开绘制，避免 emoji 宽度差异导致各区标题错位
            _pillow_draw_text(
                d, pad + card_pad_x + int(16 * S), cy,
                str(section.get("icon") or "♟️"),
                head_f, (255, 224, 150, 255), anchor="lm", efont=ef_t,
            )
            _pillow_draw_text(
                d, pad + card_pad_x + int(48 * S), cy,
                str(section.get("label") or ""),
                head_f, (255, 224, 150, 255), anchor="lm", efont=ef_t,
            )
            row_y = y + card_pad_y + card_head_h
            for index, row in enumerate(rows):
                rcy = row_y + row_h / 2
                rank_cx = pad + card_pad_x + rank_r
                is_top = index < 3
                if is_top:
                    d.ellipse(
                        [rank_cx - rank_r, rcy - rank_r, rank_cx + rank_r, rcy + rank_r],
                        fill=(255, 215, 130, 255),
                    )
                    _pillow_draw_text(
                        d, rank_cx, rcy, str(row.get("rank") or ""),
                        _pillow_font(True, int(16 * S)), (58, 42, 8, 255),
                        anchor="mm", efont=ef_r,
                    )
                else:
                    d.ellipse(
                        [rank_cx - rank_r, rcy - rank_r, rank_cx + rank_r, rcy + rank_r],
                        fill=_pillow_blend(base, (255, 255, 255), 20),
                    )
                    _pillow_draw_text(
                        d, rank_cx, rcy, str(row.get("rank") or ""),
                        _pillow_font(True, int(14 * S)), (255, 255, 255, 191),
                        anchor="mm", efont=ef_r,
                    )
                name_x = rank_cx + rank_r + int(16 * S)
                name = _pillow_truncate(
                    d, str(row.get("name") or ""), name_max_w, name_f, ef_t
                )
                _pillow_draw_text(
                    d, name_x, rcy, name, name_f, (255, 255, 255, 240),
                    anchor="lm", efont=ef_t,
                )
                stat_text = f"{int(row.get('wins') or 0)} 胜 · {int(row.get('winrate') or 0)}%"
                _pillow_draw_text(
                    d, W - pad - card_pad_x, rcy, stat_text, stat_f,
                    (255, 224, 150, 255), anchor="rm", efont=ef_s,
                )
                row_y += row_h + row_gap
            y += card_h + card_gap
        # 底部与强调条
        ff = _pillow_font(False, int(13 * S))
        _pillow_draw_text(
            d, W / 2, H - accent_h - int(30 * S),
            "数据来自跨房间全局战绩榜 · 由 花火 监督生成", ff,
            (255, 255, 255, 115), anchor="mm", efont=ef_t,
        )
        d.rectangle([0, H - accent_h, W, H], fill=(255, 190, 92, 255))
        import base64
        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _build_board_report(self) -> str:
        """拼一份棋类战绩日报/周报文本（按时间窗口汇总四种棋类榜，含胜率）。"""
        game_types = ["gomoku", "othello", "xiangqi", "weiqi"]
        labels = {"gomoku": "五子棋", "othello": "黑白棋", "xiangqi": "中国象棋", "weiqi": "围棋"}
        mode = str(getattr(self, "board_report_mode", "daily") or "daily")
        days = self._report_window_days(mode)
        scope = "累计" if not days else ("最近 1 天" if days == 1 else f"最近 {days} 天")
        lines = ["📊 棋类对局 · 战绩报告", f"统计范围：{scope}", "——————"]
        total_entries = 0
        for gt in game_types:
            rows = self.manager.global_leaderboard(gt, limit=10, days=days or None)
            if not rows and days:
                rows = self.manager.global_leaderboard(gt, limit=10)
            if not rows:
                continue
            total_entries += 1
            lines.append(f"♟️ {labels[gt]}")
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(rows):
                rank = medals[i] if i < 3 else f"#{i + 1}"
                games = int(row["games"] or 0)
                wins = int(row["wins"] or 0)
                rate = int(round(float(row["winrate"] or 0)))
                suffix = f"{wins} 胜 · {rate}%" if games else f"{wins} 胜"
                lines.append(f"  {rank} {row['name']}：{suffix}")
        if not total_entries:
            lines.append("当前暂无棋类战绩记录。")
        lines.append("——————")
        lines.append("数据来自跨房间全局战绩榜。")
        return "\n".join(lines)

    def _remember_bot_ctx(self, room: GameRoom, event) -> None:
        """从 QQ 事件里缓存该会话的 aiocqhttp bot/群号/自身QQ，供后续群里禁言使用。"""
        try:
            bot = getattr(event, "bot", None)
            gid = event.get_group_id() if hasattr(event, "get_group_id") else None
            self_id = event.get_self_id() if hasattr(event, "get_self_id") else None
            if bot is not None and gid is not None:
                self._session_bot_ctx[room.session_id] = {
                    "bot": bot,
                    "group_id": int(gid),
                    "self_id": None if self_id is None else int(self_id),
                }
        except Exception as exc:
            logger.debug("[GameCompanion] 缓存会话群禁言句柄失败: %s", exc)

    async def _mute_room_qq(self, room: GameRoom, qq: str, seconds: int, reason: str) -> bool:
        """在房间对应群聊里把指定 QQ 禁言 seconds 秒（向 60 取整）。

        采用与群管插件一致的 aiocqhttp `bot.set_group_ban`，不依赖 LLM 工具；
        拿不到句柄或调用失败只记日志，绝不阻塞游戏主流程。
        """
        if not seconds or not qq:
            return False
        ctx = self._session_bot_ctx.get(room.session_id)
        bot = ctx.get("bot") if ctx else None
        group_id = ctx.get("group_id") if ctx else None
        if bot is None or group_id is None:
            logger.info("[GameCompanion] 未取到群禁言句柄，跳过禁言(%s: %s)", reason, qq)
            return False
        duration = (max(0, int(seconds)) // 60) * 60
        if duration <= 0:
            return False
        try:
            await bot.set_group_ban(
                group_id=group_id,
                user_id=int(qq),
                duration=duration,
                self_id=ctx.get("self_id") if ctx.get("self_id") else None,
            )
            logger.info("[GameCompanion] %s：已将 QQ %s 禁言 %s 秒", reason, qq, duration)
            return True
        except Exception as exc:
            logger.error(
                "[GameCompanion] 群禁言失败(%s: %s): %s", reason, qq, exc
            )
            return False

    async def _deliver_room_link(
        self,
        room: GameRoom,
        url: str,
        *,
        reused: bool,
        restarted: bool,
        switched: bool = False,
    ) -> bool:
        if restarted:
            title = "新一局已在原游戏房间开始："
        elif switched:
            title = f"游戏已切换为{self._game_label(room.game_type)}房间："
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
        if not await self._deliver(
            room.session_id,
            MessageChain([Plain("\n".join(lines))]),
            what="房间链接",
        ):
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
        """每 2 秒扫一遍房间：判负、超时、销毁、隧道自愈。

        try 必须在 while 里面：以前包在循环外，任意一个房间 tick 抛一次异常
        就会让整个看门狗永久退出——之后所有房间不再判负、不再自动销毁，
        而且没有任何提示（插件看起来一切正常，其实定时逻辑已经死了）。
        """
        while True:
            try:
                await asyncio.sleep(2)
                await self.manager.sweep_expired()
                self._schedule_tunnel_recovery()
                self._register_companion_invite_ability()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 看门狗必须活到插件卸载
                logger.warning("[GameCompanion] 房间看门狗本轮执行失败，稍后重试: %s", exc)
                await asyncio.sleep(1)

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
        # 隧道重建后地址变了，清掉 base 缓存，让 _room_url 用新地址重新生成链接
        self._access_base_cache = ""
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
        # 管理台开的「链接房间」不会自动销毁，别给访客编造一个「X 秒内入席」的倒计时；
        # 普通房间现在按 idle_timeout（无有效操作）兜底清理，也不再是「空房 60 秒即毁」。
        if not room.admin_room:
            timeout = self.manager.idle_timeout
            if timeout:
                instruction += (
                    f"说明房间在长时间无人操作（约 {timeout} 秒）后会自动销毁，"
                    "提醒玩家及时进入。"
                )
        return instruction

    def _register_page_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        register_api(f"{PAGE_API_PREFIX}/rooms", self.page_rooms, ["GET"], "Game rooms")
        register_api(
            f"{PAGE_API_PREFIX}/room/create",
            self.page_create_room,
            ["POST"],
            "Create a room from the console",
        )
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
            f"{PAGE_API_PREFIX}/leaderboard/clear",
            self.page_clear_leaderboard,
            ["POST"],
            "Clear global leaderboard",
        )
        register_api(
            f"{PAGE_API_PREFIX}/leaderboard",
            self.page_leaderboard,
            ["POST"],
            "Read global leaderboard",
        )
        register_api(
            f"{PAGE_API_PREFIX}/leaderboard/set_wins",
            self.page_set_wins,
            ["POST"],
            "Edit a player's win count",
        )
        register_api(
            f"{PAGE_API_PREFIX}/operation_log",
            self.page_operation_log,
            ["POST"],
            "Read management operation log",
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
        register_api(
            f"{PAGE_API_PREFIX}/soup_library",
            self.page_soup_library,
            ["GET"],
            "Read turtle soup library stats",
        )
        register_api(
            f"{PAGE_API_PREFIX}/soup_library_reset",
            self.page_soup_library_reset,
            ["POST"],
            "Reset turtle soup usage records",
        )
        register_api(
            f"{PAGE_API_PREFIX}/soup_custom_delete",
            self.page_soup_custom_delete,
            ["POST"],
            "Delete a custom turtle soup puzzle",
        )
        register_api(
            f"{PAGE_API_PREFIX}/soup_library_action",
            self.page_soup_library_action,
            ["POST"],
            "Disable / restore / edit a builtin turtle soup puzzle",
        )
        register_api(
            f"{PAGE_API_PREFIX}/soup_reveal",
            self.page_soup_reveal,
            ["POST"],
            "Reveal one turtle soup puzzle's solution",
        )
        register_api(
            f"{PAGE_API_PREFIX}/soup_custom_update",
            self.page_soup_custom_update,
            ["POST"],
            "Edit a custom turtle soup puzzle",
        )

    async def page_undercover_words(self) -> dict[str, Any]:
        store = self.undercover_word_store
        items = store.list_all() if store is not None else []
        return {"status": "ok", "data": {"items": items}}

    async def page_undercover_words_add(self) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        # register_web_api 不向 handler 传参，必须自己从全局 request 取 JSON body
        payload = await request.json(default={}) or {}
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

    async def page_undercover_words_delete(self) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = await request.json(default={}) or {}
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

    @staticmethod
    def _extract_uc_word_pairs(text: str) -> list[Any]:
        """从 LLM 输出中提取词对列表。

        LLM 通常按要求返回裸 JSON 数组 ``[{...}, ...]``，而 ``extract_json_object``
        只认对象，会把数组里的第一个元素当成整个结果（导致一条都收不到）。这里
        先按数组解析，再兼容 ``{"items": [...]}`` 包装或单个词对对象。
        """
        raw = str(text or "").strip()
        if not raw:
            return []
        decoder = json.JSONDecoder()
        for index, character in enumerate(raw):
            if character != "[":
                continue
            try:
                parsed, _end = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        data = extract_json_object(raw)
        if isinstance(data, dict):
            for key in ("items", "pairs", "words", "result", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if data.get("word1") and data.get("word2"):
                return [data]
        return []

    @staticmethod
    def _parse_uc_word_lines(text: str) -> tuple[list[tuple[str, str]], list[str]]:
        """按「每行一对词条」解析批量文本。

        Returns:
            ``(pairs, malformed)``：成功解析的词对列表，以及词条数不是 2 的原始行。
            一行内用空格 / 逗号（中英文）/ 冒号 / 顿号 / 制表符分隔两个词条。
        """
        import re

        pairs: list[tuple[str, str]] = []
        malformed: list[str] = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            tokens = [token for token in re.split(r"[\s,，:：、\t]+", line) if token]
            if len(tokens) != 2:
                malformed.append(line)
                continue
            pairs.append((tokens[0], tokens[1]))
        return pairs, malformed

    async def page_undercover_words_batch_import(self) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = await request.json(default={}) or {}
        pairs, malformed = self._parse_uc_word_lines(str(payload.get("text") or ""))
        added = 0
        skipped: list[str] = [f"格式错误：{line}" for line in malformed]
        for w1, w2 in pairs:
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

    async def page_undercover_words_llm_generate_batch(self) -> dict[str, Any]:
        store = self.undercover_word_store
        if store is None:
            return {"status": "error", "message": "词库不可用", "data": {"items": []}}
        payload = await request.json(default={}) or {}
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
            pairs_src = self._extract_uc_word_pairs(text)
        except Exception as exc:
            logger.warning("[GameCompanion] 词库 LLM 生成失败: %s", exc)
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
                    {**room.admin_snapshot(), "url": self._room_url_safe(room)}
                    for room in self.manager.rooms.values()
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

    @staticmethod
    def _normalize_group_id(value: Any) -> str:
        """管理台输入的播报群号：只收数字，长度做个基本校验。

        允许留空（不绑定）；填了就必须是 5~12 位数字，否则后面拼出来的
        会话 ID 一定发不出去，不如在这里就说清楚。
        """
        text = str(value or "").strip().lstrip("#")
        if not text:
            return ""
        if not text.isdigit():
            raise ValueError("群号只能是数字")
        if not 5 <= len(text) <= 12:
            raise ValueError("群号应该是 5~12 位数字")
        return text

    async def page_create_room(self) -> dict[str, Any]:
        """管理台手动开房：不用发 QQ 指令，直接建一个链接房间并回传访问网址。

        建出来的是「管理员房间」（和群里由管理员创建的房间同一种）：
        访客打开链接后由管理员在管理台「安排玩家」入座，不需要再走绑定码流程。
        """
        payload = await request.json(default={}) or {}
        try:
            game_type = self._game_type(payload.get("game_type") or "gomoku")
        except (TypeError, ValueError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        difficulty = self._difficulty_for_game(game_type, payload.get("difficulty"))
        try:
            group_id = self._normalize_group_id(payload.get("group_id"))
        except ValueError as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        try:
            await self._ensure_public_access()
            room = await self.manager.create_room(
                source="private",
                session_id=ADMIN_CONSOLE_SESSION,
                platform="admin",
                group_id=group_id,
                creator_qq="",
                creator_name="游戏管理台",
                admin_room=True,
                game_type=game_type,
                difficulty=difficulty,
                bypass_limit=True,
            )
        except (ValueError, PermissionError, RuntimeError, OSError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        room.add_message(
            "system", "管理员在游戏管理台创建了这个房间，把访问网址发给朋友即可开局。"
        )
        if group_id:
            room.add_message("system", f"战报会发到群 {group_id}。")
        url = self._room_url_safe(room)
        self.manager.record_operation(
            "create_room",
            f"管理台开房：{room.room_id}（{self._game_label(game_type)}）",
            operator="游戏管理台",
            room_id=room.room_id,
            game_type=room.game_type,
        )
        return {
            "status": "ok",
            "message": "" if url else "房间已创建，但外部访问地址还没就绪",
            "data": {"room": room.admin_snapshot(), "url": url},
        }

    def _room_url_safe(self, room: GameRoom) -> str:
        """取房间访问网址；地址还没就绪时返回空串而不是抛异常（管理台列表容忍缺失）。"""
        try:
            return self._room_url(room)
        except (RuntimeError, ValueError):
            return ""

    async def page_room_action(self) -> dict[str, Any]:
        payload = await request.json(default={}) or {}
        room = self.manager.rooms.get(str(payload.get("room_id") or ""))
        if room is None:
            return {"status": "error", "message": "房间不存在或已经结束", "data": {}}
        action = str(payload.get("action") or "").strip().lower()
        try:
            if action == "bind_group":
                group_id = self._normalize_group_id(payload.get("group_id"))
                self.manager.bind_announce_group(room, group_id)
                self.manager.record_operation(
                    "bind_group",
                    f"房间 {room.room_id}：播报群改为"
                    f"{group_id or '（解除绑定）'}",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "assign":
                visitor_number = int(payload.get("visitor_number") or 0)
                await self.manager.assign_player(
                    room,
                    visitor_number,
                    str(payload.get("player_qq") or ""),
                )
                self.manager.record_operation(
                    "assign",
                    f"房间 {room.room_id}：将 {visitor_number} 号安排为玩家",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "demote":
                visitor_number = int(payload.get("visitor_number") or 0)
                await self.manager.remove_player(room, visitor_number)
                self.manager.record_operation(
                    "demote",
                    f"房间 {room.room_id}：将 {visitor_number} 号移回观众席",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "kick":
                visitor_number = int(payload.get("visitor_number") or 0)
                await self.manager.kick_visitor(room, visitor_number)
                self.manager.record_operation(
                    "kick",
                    f"房间 {room.room_id}：将 {visitor_number} 号踢出房间",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "set_difficulty":
                difficulty = self._difficulty(payload.get("difficulty"))
                await self.manager.set_difficulty(room, difficulty)
                self.manager.record_operation(
                    "set_difficulty",
                    f"房间 {room.room_id}：棋力改为"
                    f"{self.manager._difficulty_label(difficulty)}",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "pause":
                await self.manager.pause(room)
                self.manager.record_operation(
                    "pause_room",
                    f"房间 {room.room_id}：暂停对局",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "resume":
                await self.manager.resume(room)
                self.manager.record_operation(
                    "resume_room",
                    f"房间 {room.room_id}：继续对局",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "switch_game":
                await self.manager.switch_game(
                    room,
                    self._game_type(payload.get("game_type")),
                    force=self._value_bool(payload.get("confirm_abandon")),
                )
                self.manager.record_operation(
                    "switch_game",
                    f"房间 {room.room_id}：切换为《{payload.get('game_type')}》",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            elif action == "close":
                await self.manager.destroy(room.room_id, "管理员关闭了房间")
                self.manager.record_operation(
                    "close_room",
                    f"关闭房间 {room.room_id}",
                    operator="游戏管理台",
                    room_id=room.room_id,
                    game_type=room.game_type,
                )
            else:
                raise ValueError("不支持的管理操作")
        except (ValueError, RuntimeError, PermissionError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {"status": "ok", "data": {"room_id": room.room_id, "action": action}}

    async def page_clear_leaderboard(self) -> dict[str, Any]:
        """清空战绩排行榜（按玩法）。payload: {"game_type": "undercover"}，留空则清空全部玩法。"""
        payload = await request.json(default={}) or {}
        game_type = str(payload.get("game_type") or "").strip() or None
        # 指定玩法时严格只清该玩法；即使该玩法当前无数据，也不回退到清空全部，避免误操作
        cleared = self.manager.clear_global_stats(game_type)
        if cleared:
            self.manager.record_operation(
                "clear_leaderboard",
                f"清空战绩排行榜（{'全部玩法' if not game_type else self._game_label(game_type)}）",
                operator="游戏管理台",
                game_type=game_type or "",
            )
        return {
            "status": "ok" if cleared else "error",
            "message": (
                ""
                if cleared
                else (
                    "该玩法暂无排行榜数据"
                    if game_type
                    else "没有可清空的排行榜数据（持久化未启用或为空）"
                )
            ),
            "data": {"cleared": bool(cleared)},
        }

    async def page_set_wins(self) -> dict[str, Any]:
        """手动修正某玩家胜场。payload: {"game_type","name","wins"}。"""
        payload = await request.json(default={}) or {}
        try:
            changed = self.manager.set_global_wins(
                str(payload.get("game_type") or ""),
                str(payload.get("name") or ""),
                int(payload.get("wins") or 0),
            )
        except (ValueError, TypeError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        if not changed:
            return {"status": "error", "message": "未找到该玩家在对应玩法的记录", "data": {}}
        return {"status": "ok", "data": {"changed": changed}}

    async def page_operation_log(self) -> dict[str, Any]:
        """返回最近的操作日志。payload: {"limit": int}。"""
        payload = await request.json(default={}) or {}
        limit = int(payload.get("limit") or 200)
        return {"status": "ok", "data": {"rows": self.manager.operation_log(limit)}}

    async def page_leaderboard(self) -> dict[str, Any]:
        """返回各玩法全局战绩排行（用于管理台导出海报/浏览）。

        payload 可选 {"game_type": "undercover"}；不传则返回全部玩法。每个玩法最多 limit 条。
        """
        payload = await request.json(default={}) or {}
        target = str(payload.get("game_type") or "").strip() or None
        limit = max(0, int(payload.get("limit") or 100))
        game_types = (
            [target] if target and target in SUPPORTED_GAMES else list(SUPPORTED_GAMES)
        )
        result: dict[str, object] = {}
        for gtype in game_types:
            rows = self.manager.global_leaderboard(gtype, limit=limit)
            if rows:
                result[gtype] = rows
        return {"status": "ok", "data": {"leaderboard": result}}

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

    def _soup_library_snapshot(self) -> dict[str, Any]:
        """题库管理的完整数据：内置题（逐条）、玩家投稿、出题账本。

        汤底一律只给 `solution_preview`（打码），想看全文走单条「查看答案」。
        一次把两百多条答案铺在页面上，管理员自己就先被剧透了。
        """
        library = self._soup_library()
        deck = self._soup_deck()
        custom = self._soup_custom()
        recent = deck.known_surfaces()
        builtin_items: list[dict[str, Any]] = []
        for entry in library.entries:
            record = library.override_of(entry.index)
            builtin_items.append(
                {
                    "index": entry.index,
                    "surface": entry.surface,
                    "content_level": entry.content_level,
                    "solution_preview": SoupLibrary.mask_solution(entry.solution),
                    "disabled": entry.disabled,
                    "edited": bool(
                        record.get("surface")
                        or record.get("solution")
                        or record.get("content_level")
                    ),
                    "used": normalize_text(entry.surface) in recent,
                }
            )
        return {
            "builtin": {**library.stats(), "items": builtin_items},
            "used": deck.stats(),
            "custom": {
                "total": len(custom),
                "limit": custom.limit,
                "items": [
                    {
                        "index": index,
                        "surface": entry.surface,
                        "content_level": entry.content_level,
                        "created_at": entry.created_at,
                        "owner": entry.owner,
                        "solution_preview": SoupLibrary.mask_solution(entry.solution),
                    }
                    for index, entry in enumerate(custom.entries, start=1)
                ],
            },
            "generated": {
                **self._soup_generated().stats(),
                "items": [
                    {
                        "index": row["index"],
                        "title": row["title"],
                        "surface": row["surface"],
                        "content_level": row["content_level"],
                        "created_at": row["created_at"],
                        "last_used_at": row["last_used_at"],
                        "disabled": row["disabled"],
                        "solution_preview": SoupLibrary.mask_solution(row["solution"]),
                    }
                    for row in self._soup_generated().listing()
                ],
            },
        }

    async def page_soup_library(self) -> dict[str, Any]:
        """题库概况：内置题量、出题账本、玩家投稿（管理台「题库管理」用）。"""
        return {"status": "ok", "data": self._soup_library_snapshot()}

    async def page_soup_library_action(self) -> dict[str, Any]:
        """内置题的停用 / 恢复 / 删除 / 改回 / 编辑，全部只写覆盖层。"""
        payload = await request.json(default={}) or {}
        action = str(payload.get("action") or "").strip().lower()
        kind = str(payload.get("kind") or "builtin").strip().lower()
        library = self._soup_library()
        try:
            if kind == "generated":
                store = self._soup_generated()
                if action == "restore_all":
                    raise ValueError("生成题没有「恢复」，删掉的无法找回")
                index = int(payload.get("index") or 0)
                if index <= 0:
                    raise ValueError("缺少题目序号")
                if action == "disable":
                    store.set_disabled(index, True)
                    message = "已停用这道题（保留存档，不再被抽到）"
                elif action == "enable":
                    store.set_disabled(index, False)
                    message = "已恢复这道题"
                elif action == "delete":
                    store.remove(index)
                    message = "已删除这道题"
                elif action == "edit":
                    store.update(
                        index,
                        title=payload.get("title"),
                        surface=payload.get("surface"),
                        solution=payload.get("solution"),
                        content_level=payload.get("content_level"),
                    )
                    message = "已保存修改"
                else:
                    raise ValueError("不支持的操作")
            elif action == "restore_all":
                count = library.restore_all()
                message = f"已恢复 {count} 处改动，题库回到内置版本"
            else:
                index = int(payload.get("index") or 0)
                if index <= 0:
                    raise ValueError("缺少题目序号")
                if action == "disable":
                    library.set_disabled(index, True)
                    message = "已停用这道题（不参与抽题，可随时恢复）"
                elif action == "enable":
                    library.set_disabled(index, False)
                    message = "已恢复这道题"
                elif action == "delete":
                    library.remove_entry(index)
                    message = "已删除这道题（可用「恢复全部」找回）"
                elif action == "restore":
                    library.restore_entry(index)
                    message = "已恢复成内置版本"
                elif action == "edit":
                    library.edit_entry(
                        index,
                        surface=payload.get("surface"),
                        solution=payload.get("solution"),
                        content_level=payload.get("content_level"),
                    )
                    message = "已保存修改"
                else:
                    raise ValueError("不支持的操作")
            self.manager.record_operation(
                "soup_library", f"题库管理：{message}", operator="游戏管理台"
            )
        except (ValueError, OSError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        return {
            "status": "ok",
            "message": message,
            "data": self._soup_library_snapshot(),
        }

    async def page_soup_reveal(self) -> dict[str, Any]:
        """看某一道题的完整汤底：一次只看一条，避免整库剧透。"""
        payload = await request.json(default={}) or {}
        kind = str(payload.get("kind") or "builtin").strip().lower()
        try:
            index = int(payload.get("index") or 0)
        except (TypeError, ValueError):
            return {"status": "error", "message": "序号不合法", "data": {}}
        if kind == "generated":
            row = self._soup_generated().get(index)
            if row is None:
                return {"status": "error", "message": "没有这个序号", "data": {}}
            mapping = row.get("puzzle") or {}
            return {
                "status": "ok",
                "data": {
                    "title": str(mapping.get("title") or ""),
                    "surface": str(mapping.get("surface") or ""),
                    "solution": str(mapping.get("solution") or ""),
                    "content_level": normalize_content_level(
                        mapping.get("content_level")
                    ),
                },
            }
        if kind == "custom":
            store = self._soup_custom()
            if not 1 <= index <= len(store.entries):
                return {"status": "error", "message": "没有这个序号", "data": {}}
            entry = store.entries[index - 1]
            return {
                "status": "ok",
                "data": {
                    "surface": entry.surface,
                    "solution": entry.solution,
                    "content_level": entry.content_level,
                },
            }
        entry = self._soup_library().find(index)
        if entry is None:
            return {"status": "error", "message": "没有这个序号", "data": {}}
        return {
            "status": "ok",
            "data": {
                "surface": entry.surface,
                "solution": entry.solution,
                "content_level": entry.content_level,
            },
        }

    async def page_soup_custom_update(self) -> dict[str, Any]:
        """管理台改一条玩家投稿：payload {index, surface?, solution?, content_level?}"""
        payload = await request.json(default={}) or {}
        try:
            index = int(payload.get("index") or 0)
        except (TypeError, ValueError):
            return {"status": "error", "message": "序号不合法", "data": {}}
        try:
            updated = self._soup_custom().update(
                index,
                surface=payload.get("surface"),
                solution=payload.get("solution"),
                content_level=payload.get("content_level"),
            )
        except (ValueError, OSError) as exc:
            return {"status": "error", "message": str(exc), "data": {}}
        if updated is None:
            return {"status": "error", "message": "没有这个序号", "data": {}}
        self.manager.record_operation(
            "soup_custom", f"题库管理：修改投稿 #{index}", operator="游戏管理台"
        )
        return {
            "status": "ok",
            "message": "投稿已更新",
            "data": self._soup_library_snapshot(),
        }

    async def page_soup_library_reset(self) -> dict[str, Any]:
        """清空出题账本（只重置「出过的题」记录，不动题库文件与补全缓存）。"""
        removed = self._soup_deck().clear()
        return {
            "status": "ok",
            "message": f"已重置出题记录（清掉 {removed} 条），题库本身没有改动",
            "data": {"cleared": removed},
        }

    async def page_soup_custom_delete(self) -> dict[str, Any]:
        """删除一条玩家投稿：payload {"index": 1}。"""
        payload = await request.json(default={}) or {}
        try:
            index = int(payload.get("index") or 0)
        except (TypeError, ValueError):
            return {"status": "error", "message": "序号不合法", "data": {}}
        removed = self._soup_custom().remove(index)
        if removed is None:
            return {"status": "error", "message": "没有这个序号", "data": {}}
        return {
            "status": "ok",
            "message": "已删除该投稿",
            "data": {"surface": removed.surface},
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
            "notice": (
                "设置立即用于新房间和新一局；正在进行中的对局保持原参数。"
                "棋类的每步时间 / 掉线等待 / 备战挂机让座是全局参数，"
                "已统一放到插件配置的「多人席位」分组里调整。"
            ),
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

    def _revert_undercover_chinese_config_keys(self) -> None:
        """兼容 0.5.12 短暂使用的中文键：把已写入配置的中文键回迁为英文键。

        0.5.12 曾把谁是卧底对局参数写成中文键（undercover.最大玩家席 等）；
        若插件加载过该版本，配置里已存在中文键，这里把值搬回英文键并删除中文键，
        与全插件其余配置保持一致的英文点分键风格，已保存的参数不会丢失。
        """
        try:
            config = self.config
            if not isinstance(config, dict):
                return
            changed = False
            # 嵌套存储：config["undercover"] = {"最大玩家席": ...}
            section = config.get("undercover")
            if isinstance(section, dict):
                for chinese, (english, default) in _UNDERCOVER_CN_TO_EN_KEYS.items():
                    cn_key = chinese.split(".", 1)[1]
                    en_key = english.split(".", 1)[1]
                    if cn_key not in section:
                        continue
                    current = section.get(en_key)
                    if current is None or current == default:
                        section[en_key] = section[cn_key]
                    section.pop(cn_key, None)
                    changed = True
            # 平铺存储：config["undercover.最大玩家席"] = ...
            for chinese, (english, default) in _UNDERCOVER_CN_TO_EN_KEYS.items():
                if chinese not in config:
                    continue
                current = config.get(english)
                if current is None or current == default:
                    config[english] = config[chinese]
                config.pop(chinese, None)
                changed = True
            if changed:
                save = getattr(config, "save_config", None)
                if callable(save):
                    save()
        except Exception as exc:
            logger.warning("[GameCompanion] 谁是卧底中文键回迁失败（不影响运行）: %s", exc)

    def _apply_game_settings_runtime(self) -> None:
        self.enabled_games = {
            game_type: self._cfg_bool(f"{game_type}.enabled", True)
            for game_type in SUPPORTED_GAMES
        }
        self.manager.enabled_games.update(self.enabled_games)
        self.turtle_soup_max_hints = self._cfg_int(
            "turtle_soup.max_hints", 3, minimum=0, maximum=20
        )
        self.turtle_soup_content_level = normalize_content_level(
            self._cfg("turtle_soup.content_level", "normal")
        )
        self.turtle_soup_max_players = self._cfg_int(
            "turtle_soup.max_players", 0, minimum=0, maximum=100
        )
        self.turtle_soup_max_questions = self._cfg_int(
            "turtle_soup.max_questions", 15, minimum=0, maximum=999
        )
        self.turtle_soup_max_answers = self._cfg_int(
            "turtle_soup.max_answers", 8, minimum=0, maximum=999
        )
        self.turtle_soup_difficulty = self._soup_difficulty(
            self._cfg_str("turtle_soup.difficulty", "normal")
        )
        self.turtle_soup_follow_difficulty = self._cfg_bool(
            "turtle_soup.follow_difficulty", True
        )
        self.turtle_soup_library_priority = (
            self._cfg_str("turtle_soup.library_priority", "builtin_first")
            .strip()
            .lower()
            or "builtin_first"
        )
        if self.turtle_soup_library_priority not in {
            "builtin_first",
            "online_first",
            "generated_first",
        }:
            self.turtle_soup_library_priority = "builtin_first"
        self.turtle_soup_prefetch_target = self._cfg_int(
            "turtle_soup.prefetch_target", 1, minimum=0, maximum=3
        )
        self.turtle_soup_free_questioning = self._cfg_bool(
            "turtle_soup.free_questioning", True
        )
        self.turtle_soup_free_question_cooldown = self._cfg_int(
            "turtle_soup.free_question_cooldown", 1, minimum=0, maximum=30
        )
        self.turtle_soup_auto_reveal_seconds = self._cfg_int(
            "turtle_soup.auto_reveal_seconds", 1800, minimum=0, maximum=7200
        )
        self.turtle_soup_bot_join = self._cfg_bool(
            "turtle_soup.bot_join_player_host", True
        )
        self.turtle_soup_generation_provider_id = self._cfg_str(
            "turtle_soup.generation_provider_id", ""
        ).strip()
        self.turtle_soup_judge_provider_id = self._cfg_str(
            "turtle_soup.judge_provider_id", ""
        ).strip()
        self.multiplayer_turn_timeout = self._cfg_int(
            "multiplayer.turn_timeout_seconds", 60, minimum=0, maximum=3600
        )
        self.duel_turn_timeout = self._cfg_int(
            "multiplayer.duel_turn_timeout_seconds", 180, minimum=0, maximum=3600
        )
        self.duel_offline_grace = self._cfg_int(
            "multiplayer.duel_offline_grace_seconds", 90, minimum=0, maximum=3600
        )
        self.duel_afk_seat_timeout = self._cfg_int(
            "multiplayer.duel_afk_seat_timeout_seconds",
            60,
            minimum=0,
            maximum=3600,
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
        self.manager.turtle_soup_max_questions = self.turtle_soup_max_questions
        self.manager.turtle_soup_max_answers = self.turtle_soup_max_answers
        self.manager.turtle_soup_follow_difficulty = self.turtle_soup_follow_difficulty
        self.manager.turtle_soup_free_questioning = self.turtle_soup_free_questioning
        self.manager.turtle_soup_free_question_cooldown = self.turtle_soup_free_question_cooldown
        self.manager.turtle_soup_auto_reveal_seconds = (
            self.turtle_soup_auto_reveal_seconds
        )
        self.manager.turtle_soup_bot_join = self.turtle_soup_bot_join
        # 已经在房间里的对局也要跟上，否则页面提示还写着「花火也会一起猜」
        for room in self.manager.rooms.values():
            if room.game_type == "turtle_soup":
                room.soup_bot_join = self.turtle_soup_bot_join
        self.manager.multiplayer_turn_timeout = self.multiplayer_turn_timeout
        self.manager.duel_turn_timeout = self.duel_turn_timeout
        self.manager.duel_offline_grace = self.duel_offline_grace
        self.manager.duel_afk_seat_timeout = self.duel_afk_seat_timeout
        # ---- 谁是卧底：管理台里能改，就必须在保存后同步到 manager ----
        # 以前整段漏掉了，管理台显示已保存、实际对局仍用启动时的旧值。
        self.undercover_max_players = self._cfg_int(
            "undercover.max_players", 10, minimum=2, maximum=20
        )
        self.undercover_min_players = self._cfg_int(
            "undercover.min_players", 2, minimum=2, maximum=10
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
        self.undercover_allow_host_customize_camp_scales = self._cfg_bool(
            "undercover.allow_host_customize_camp_scales", True
        )
        self.undercover_ai_fill_enabled = self._cfg_bool(
            "undercover.ai_fill_enabled", True
        )
        self.undercover_ai_fill_min_players = self._cfg_int(
            "undercover.ai_fill_min_players", 3, minimum=2, maximum=8
        )
        self.undercover_camp_scales_default = (
            self._cfg_str("undercover.camp_scales_default", "4 1 0") or "4 1 0"
        )
        self.manager.undercover_max_players = self.undercover_max_players
        self.manager.undercover_min_players = self.undercover_min_players
        self.manager.undercover_match_seconds = self.undercover_match_seconds
        self.manager.undercover_prepare_seconds = self.undercover_prepare_seconds
        self.manager.undercover_speaking_seconds = self.undercover_speaking_seconds
        self.manager.undercover_voting_seconds = self.undercover_voting_seconds
        self.manager.undercover_first_round_non_voting = (
            self.undercover_first_round_non_voting
        )
        self.manager.undercover_send_identity_in_card = (
            self.undercover_send_identity_in_card
        )
        self.manager.undercover_show_voters = self.undercover_show_voters
        self.manager.undercover_similarity = self.undercover_similarity
        self.manager.undercover_allow_host_customize_camp_scales = (
            self.undercover_allow_host_customize_camp_scales
        )
        self.manager.undercover_ai_fill_enabled = self.undercover_ai_fill_enabled
        self.manager.undercover_ai_fill_min_players = max(
            self.manager.undercover_min_players,
            min(self.undercover_ai_fill_min_players, 12),
        )
        self.manager.undercover_camp_scales_default = _parse_camp_scales(
            self.undercover_camp_scales_default
        )
        self.weiqi_board_size = self._cfg_int(
            "weiqi.board_size", 13, minimum=9, maximum=19
        )
        self.weiqi_komi = self._safe_float(self._cfg("weiqi.komi", 7.5), 7.5)
        self.manager.weiqi_board_size = self.weiqi_board_size
        self.manager.weiqi_komi = self.weiqi_komi
        self.manager.swap_request_cooldown = self.swap_request_cooldown
        self.manager.swap_request_expiry = self.swap_request_expiry
        self.manager.duel_side_rotation = bool(
            getattr(self, "duel_side_rotation", True)
        )
        self.manager.ai_think_delay = max(
            0.0, min(5.0, self._safe_float(getattr(self, "ai_think_delay", 0.8), 0.8))
        )
        self.commentary_provider_id = self._cfg_str("commentary.provider_id", "")
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
        # 手动重启通道后地址可能变化，清缓存让后续链接用新地址
        self._access_base_cache = ""
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
        # 同房间同一时刻只允许一个 AI 动作在途：大幅避免 AI 玩家较多时对模型供应商
        # 并发/高频请求被限流（429），也是“最后一名 AI 一直卡住”的兜底之一。
        lock = self._uc_ai_lock.setdefault(room.room_id, asyncio.Lock())
        async with lock:
            try:
                # 1) 发言阶段：当前是 AI 玩家就自动发言
                if game.phase in ("speech", "pk"):
                    exp = game.expected_speaker_number
                    seat = self._uc_seat_for_number(room, exp) if exp else None
                    if seat is None or not getattr(seat, "is_ai", False):
                        return
                    # 小延时避免栈过深
                    await asyncio.sleep(0.6)
                    await self._undercover_ai_do_speech(room, seat)
                    return
                # 2) 投票阶段：还有 AI 没投就自动投（串行逐个推进，直到全投完）
                if game.phase == "voting":
                    all_live_nums = sorted([
                        p.number for p in game.players if not p.is_out
                    ])
                    voted = set(game.snapshot(None).get("voted_this_round_player_numbers") or [])
                    for n in all_live_nums:
                        if n in voted: continue
                        seat = self._uc_seat_for_number(room, n)
                        if seat and getattr(seat, "is_ai", False):
                            await asyncio.sleep(0.5)
                            await self._undercover_ai_do_vote(room, seat)
                            # 递归推进（可能下一个还是 AI）：新任务会等本任务释放锁后再串行执行
                            self._spawn(self._undercover_ai_step_if_needed(room))
                            return
            except Exception as exc:
                # AI 出错不影响真人玩家流程，但记录日志便于排查“AI 卡住”问题
                logger.warning(
                    "[GameCompanion] 谁是卧底 AI 自动驱动异常（已忽略，不影响真人）：%s",
                    exc,
                )

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
            # 座位号会在中途重编，先用开局冻结的号码找到局内玩家，再按稳定 uid 找座位
            seat = self._uc_seat_for_number(room, exp)
            if seat is None:
                # 当前发言人的座位已不存在（掉线被移出玩家席等）：
                # 直接跳过，避免整局卡在“等一个不在场的人发言”。
                game.skip_current_speaker()
                await self._undercover_ai_step_if_needed(room)
                return
            if getattr(seat, "is_ai", False):
                # 发言超时且是 AI：供应商故障时不再等模型，直接提交本地兜底文案，保证轮次推进
                await self._undercover_ai_speak_fallback(room, seat)
                return
            # 真人在限定时间内未发言：直接跳过（只推进指针，不生成占位发言，避免污染时间线与相似度池）
            await self.manager.skip_undercover_speaker(room, seat.visitor_token)
            # 下一位可能是 AI，顺手驱动
            await self._undercover_ai_step_if_needed(room)
            return
        if game.phase == "voting":
            # 投票轮超时应急兜底：别再依赖 AI 调模型，直接给所有未投玩家（含真人）补一次
            # 合法的本地票，保证即使供应商故障（429/断连）本轮也能收敛、不会永久卡在“投票进行中”。
            await self._undercover_force_all_votes(room)

    def _uc_seat_detached(self, room: GameRoom, seat) -> bool:
        """该座位是否「没人操作」：托管中、访客已离开、心跳超时、或在线挂机。"""
        return self.manager._undercover_seat_detached(room, seat, time.time())

    async def _undercover_autopilot_tick(self, room: GameRoom) -> None:
        """托管代打：离线玩家正卡着发言/投票时，立刻让 LLM 接管，不等整轮超时。

        和整轮超时兜底的区别：这里只处理「已经不在的人」，而且在轮到他的那一刻
        就动手，不会让在线玩家干等 60 秒；投票也走 LLM（按被托管人的词条与阵营
        判断），而不是机械地投最高票。
        """
        game = room.game
        if (
            not isinstance(game, UndercoverGame)
            or game.finished
            or room.status != "active"
        ):
            return
        if game.phase in ("speech", "pk"):
            expected = game.expected_speaker_number
            if expected is None:
                return
            seat = self._uc_seat_for_number(room, int(expected))
            if seat is None or not self._uc_seat_detached(room, seat):
                return
            # 和 AI 座位共用一个「同房间只允许一个动作在途」的锁，避免托管
            # 与 AI 自动发言同时提交、互相把指针推乱。
            lock = self._uc_ai_lock.setdefault(room.room_id, asyncio.Lock())
            async with lock:
                await asyncio.sleep(0.6)
                # 等锁期间局面可能已经变化（玩家重连自己说了、或别人推进了轮次）
                if room.game is not game or game.finished:
                    return
                if game.phase not in ("speech", "pk"):
                    return
                if game.expected_speaker_number != int(expected):
                    return
                await self._undercover_ai_do_speech(room, seat, autopilot=True)
            return
        if game.phase == "voting":
            pending = game.pending_round
            if pending is None:
                return
            for number in list(pending.vote_player_numbers):
                if pending.has_voted(int(number)):
                    continue
                seat = self._uc_seat_for_number(room, int(number))
                if seat is None or not self._uc_seat_detached(room, seat):
                    continue
                await self._undercover_ai_do_vote(room, seat, autopilot=True)

    @staticmethod
    def _uc_player_for_seat(room: GameRoom, seat) -> Any:
        """按稳定 uid 找出座位对应的卧底玩家（座位号中途会重编，不能用来反查）。"""
        game = room.game
        token = str(getattr(seat, "visitor_token", "") or "") if seat else ""
        if not token or not isinstance(game, UndercoverGame):
            return None
        return game.player_for_uid(_visitor_uid(token))

    @staticmethod
    def _uc_number_for_seat(room: GameRoom, seat) -> int | None:
        """座位在对局内的冻结号码；不在局内（中途入座/已移出）返回 None。"""
        player = GameCompanionPlugin._uc_player_for_seat(room, seat)
        return int(player.number) if player is not None else None

    @staticmethod
    def _uc_number_for_visitor(room: GameRoom, visitor) -> int | None:
        """访客在对局内的冻结号码；不是本局玩家时返回 None。"""
        game = room.game
        token = str(getattr(visitor, "token", "") or "") if visitor else ""
        if not token or not isinstance(game, UndercoverGame):
            return None
        player = game.player_for_uid(_visitor_uid(token))
        return int(player.number) if player is not None else None

    @classmethod
    def _uc_seat_for_number(cls, room: GameRoom, number: int) -> Any:
        """按局内冻结号码找回当前座位（座位号可能已被重编，优先按稳定 uid 匹配）。"""
        game = room.game
        if not isinstance(game, UndercoverGame):
            return None
        player = game.player_for_number(int(number))
        if player is not None and player.uid:
            seat = next(
                (
                    s
                    for s in room.multiplayer.seats
                    if _visitor_uid(s.visitor_token) == player.uid
                ),
                None,
            )
            if seat is not None:
                return seat
        return next(
            (s for s in room.multiplayer.seats if s.number == int(number)), None
        )

    async def _undercover_ai_speak_fallback(self, room: GameRoom, seat) -> None:
        """AI 发言兜底：绕过模型，直接按轮次提交一句本地文案，保证发言轮在供应商故障时也能推进。"""
        game = room.game
        if not isinstance(game, UndercoverGame) or getattr(game, "phase", "") not in ("speech", "pk"):
            return
        ai_player = self._uc_player_for_seat(room, seat)
        my_number = int(ai_player.number) if ai_player is not None else int(seat.number)
        if game.expected_speaker_number != my_number:
            return
        camp = str(ai_player.camp or "") if ai_player else ""
        if camp == "none":
            camp = ""
        round_no = max(1, int(game.current_round_number or 0))
        used = [
            str(item.get("content") or "")
            for item in game.rounds_speeches()
            if str(item.get("content") or "").strip()
        ]
        try:
            await self.manager.player_undercover_speech(
                room,
                seat.visitor_token,
                _uc_ai_fallback(camp, round_no, my_number, used=used),
            )
        except Exception as exc:
            # 本地兜底提交仍失败（相似度命中 / 座椅 token 失效等）：至少推进发言指针，
            # 避免“最后一名 AI 发言卡住整局”的问题
            logger.warning(
                "[GameCompanion] AI 发言本地兜底提交失败（将尝试跳过该玩家）：%s", exc
            )
            try:
                await self.manager.skip_undercover_speaker(room, seat.visitor_token)
            except Exception as exc2:
                logger.warning(
                    "[GameCompanion] AI 发言超时后跳过也失败：%s", exc2
                )

    async def _undercover_force_all_votes(self, room: GameRoom) -> None:
        """为投票轮中所有尚未投票的玩家补投一张合法的本地兜底票。

        选择目标优先看“当前得票最多的候选”（加速收敛），否则取第一个合法候选；
        PK 子轮只会投平票候选人，避免投出无效票。
        """
        game = room.game
        if not isinstance(game, UndercoverGame) or getattr(game, "phase", "") != "voting":
            return
        pending = getattr(game, "pending_round", None)
        if pending is None:
            return
        # 按局内冻结号码建索引（座位号在中途可能已被重编，不能直接用 seat.number）
        seats_by_num: dict[int, Any] = {}
        for seat in getattr(getattr(room, "multiplayer", None), "seats", []) or []:
            number = self._uc_number_for_seat(room, seat)
            if number is not None:
                seats_by_num[number] = seat
        for voter_num in list(pending.vote_player_numbers):
            if pending.has_voted(voter_num):
                continue
            seat = seats_by_num.get(voter_num)
            if seat is None:
                continue
            # 托管/离线玩家：让 LLM 按他的词条与当局局面接管这一票，
            # 而不是机械地投当前最高票（最高票只留给「在线但挂机」的真人兜底）。
            if self._uc_seat_detached(room, seat):
                await self._undercover_ai_do_vote(room, seat, autopilot=True)
                continue
            eligible = _uc_eligible_vote_targets(game, voter_num)
            if not eligible:
                continue
            tally = pending.votes_by_target()
            target = max(eligible, key=lambda n: tally.get(n, 0))
            try:
                await self.manager.player_undercover_vote(
                    room, seat.visitor_token, int(target)
                )
            except Exception:
                # 最后一张票会让本轮进入下一轮/结束，后续 submit 自然失败，忽略即可
                continue

    async def _undercover_ai_do_speech(
        self, room: GameRoom, seat, *, autopilot: bool = False
    ) -> None:
        game: UndercoverGame = room.game  # type: ignore[assignment]
        ai_player = self._uc_player_for_seat(room, seat)
        my_number = (
            int(ai_player.number)
            if ai_player is not None
            else int(getattr(seat, "number", 0) or 0)
        )
        snap = game.snapshot(visitor_uid=_visitor_uid(seat.visitor_token))
        # 注意：不能用快照里的 my.camp/my.word——「告知身份」关闭时快照会隐去阵营，
        # AI 必须直接从游戏内部拿到自己的真实阵营与词条，否则会因 camp 为空而沉默卡局。
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
        # 收集 AI 自己之前说过的话（这是“连续两轮一模一样”的根因之一）
        own_history = [
            str(s["content"])
            for s in game.rounds_speeches()
            if int(s.get("player_number", 0)) == int(my_number)
        ]
        # 全场历史（含别人）只用于兜底挑句：躲开自己时别撞上别人刚说过的话
        all_history = [
            str(item.get("content") or "")
            for item in game.rounds_speeches()
            if str(item.get("content") or "").strip()
        ]
        round_no = max(1, int(game.current_round_number or 0))
        is_pk = bool(getattr(getattr(game, "pending_round", None), "pk_reason", ""))
        # 每次发言换一个「角度」：同一个词条 + 同一套提示词，模型很容易原样复述；
        # 强制换角度是最省事也最有效的去重手段（平票 PK 加赛尤其明显）。
        angles = list(UC_SPEECH_ANGLES)
        random.shuffle(angles)
        # 托管代打：用被托管玩家本人的名义说话，别让其他人看出是花火在替
        speaker_label = (
            (seat.display_name or f"{my_number}号") if autopilot else "花火"
        )
        autopilot_hint = (
            f"（{speaker_label} 暂时掉线，你正在替他托管代打，"
            "必须完全代入他的身份与词条。）"
            if autopilot
            else ""
        )

        def _build_prompt(angle: str, rejected: str = "") -> str:
            """按「随机角度 + 禁用历史原句」拼提示词；rejected 是被判重复的上一版输出。"""
            if own_history:
                anti_repeat = (
                    f"这是第{round_no}轮发言{'（平票加赛）' if is_pk else ''}，"
                    f"你之前已经说过：{'；'.join(own_history[-3:])}。"
                    "这些句子一律算重复，禁止原句复用，也禁止只换几个词说同一件事；"
                    f"这一轮必须换成【{angle}】这个角度来说。"
                )
            else:
                anti_repeat = f"这是第{round_no}轮发言，请从【{angle}】这个角度说。"
            if is_pk:
                anti_repeat += (
                    "本轮是平票加赛，所有人上一轮刚听过你说的话，"
                    "再说一遍会立刻被当成复读、被投出去。"
                )
            if rejected:
                anti_repeat += (
                    f"你刚才那句『{rejected}』已被判定与历史重复，这次必须完全不同。"
                )
            if camp == "whiteboard":
                return (
                    "你正在玩中文「谁是卧底」，你抽到的是【白板】（没有词条，只能靠猜）。"
                    f"{autopilot_hint}{anti_repeat}。{others_context}。"
                    f"请以「{speaker_label}」的身份，像真人玩家一样，顺着前一位玩家的语气和话题方向，"
                    "用一句 20-45 字的话随口圆一个合群的描述，让人感觉你手里确实有词条。"
                    "绝对不要直接说出或试探性说出平民或卧底的词条，也不要问『你们说的是不是XX』；"
                    f"别说得太空泛，就聊【{angle}】这一件具体的小事；"
                    "可以有语气词、口头禅或小犹豫，越自然越好；"
                    "不要输出任何引号或说明；只输出你要说的那一句话。"
                )
            role = "平民" if camp == "civilian" else "卧底"
            other = "卧底" if camp == "civilian" else "平民"
            return (
                f"你正在玩中文「谁是卧底」，你是{role}，抽到的词条是【{word}】。"
                f"{autopilot_hint}{anti_repeat}。{others_context}。"
                f"请以「{speaker_label}」的身份，像真人玩家闲聊一样，用一句 20-45 字的话，"
                f"只从【{word}】的一个具体生活侧面入手，这一轮限定在【{angle}】这个角度上，"
                "说得自然、有画面感，像在聊天而不是在做谜题描述。"
                "硬性要求："
                f"话里绝不能出现【{word}】本身、它的同义词或它含有的字眼；"
                "不要用『我抽到/我的词条/它是一款』这类暴露句式；"
                f"不要列点罗列特征（颜色、形状、用途、价格逐项说），那一眼就会被{other}锁定；"
                "可以带语气词和日常感，但别机械、别每句都用『嗯嗯』开头；"
                "不要输出任何引号或说明；只输出你要说的那一句话。"
            )

        if camp not in ("whiteboard", "civilian", "undercover"):
            return
        # 注入性格人设（出自「火花·调皮(AI)」中的形容词）+ 阶段性措辞 + 长度约束
        m = re.search(r"·([^·()（）]+)\(AI\)", seat.display_name or "")
        persona = f"你的性格是【{m.group(1)}】，用词和口吻要贴合这个性格。" if m else ""
        if round_no <= 1:
            stage_hint = "现在是第1轮：所有人都还在试探。你的描述必须非常非常笼统、抽象，让谁都听不出、也联想不到具体是什么东西（不要提任何形状、颜色、用途、场景、气味、触感）。只说一句放之四海皆可、谁都能认同的泛泛感受即可。"
        elif round_no <= 2:
            stage_hint = "现在是第2轮：仍然要很含蓄。最多只能给出一个非常模糊、无法指向特定物件的侧面，绝不能让卧底或白板据此联想到物品本身。"
        elif round_no <= 4:
            stage_hint = f"现在是第{round_no}轮：可以稍微具体一点点了，但仍要委婉合群，别把特征说得太实在，别点名词条。"
        else:
            stage_hint = f"现在是第{round_no}轮：局势拉开，可以适当说具体些争取信任，但仍绝不能点名词条。"
        suffix = (
            persona
            + stage_hint
            + "整句务必控制在20字以内、最多1个逗号，越简短利落越好。"
            "不要输出任何表情代码、括号提示或说明。"
        )
        # 复读拦截用的历史：自己说过的优先，再加上全场，保证“换一句”真的换得掉
        history_for_check = list(
            dict.fromkeys([item for item in (own_history + all_history) if item])
        )
        content = ""
        rejected = ""
        # 最多生成两次：第一次被判重复就换一个角度再来一次，仍重复则落本地兜底。
        for attempt in range(2):
            angle = angles[attempt % len(angles)]
            prompt = _build_prompt(angle, rejected) + suffix
            try:
                raw = await asyncio.wait_for(
                    self._generate_persona_text(room, prompt),
                    timeout=8,  # 供应商故障时尽快落本地兜底，避免发言轮停滞
                )
            except Exception as exc:
                logger.warning(
                    "[GameCompanion] AI 发言生成失败，改用本地兜底：%s", exc
                )
                break
            # 规整为「≤1 逗号、≤20 字」的短句
            candidate = _sanitize_uc_ai_speech(str(raw or "").strip())
            if len(candidate) < 4:
                rejected = candidate
                continue
            # 白板 AI：一旦说出的内容泄露了任一词条（含「刷牙」之于「牙刷」这类），
            # 立即换下一版，避免 AI 因“白板说词直接获胜”而莫名其妙结束整局。
            if camp == "whiteboard" and game.leaks_word(candidate):
                rejected = candidate
                continue
            # 本地去重：AI 发言不走服务端相似度拦截（本地兜底文案模板有限，会被自己卡死），
            # 所以「和自己上一轮一模一样」只能在这里拦——PK 加赛复读就是从这漏出去的。
            if _uc_speech_is_repeat(candidate, history_for_check):
                logger.info(
                    "[GameCompanion] AI 发言与历史重复，换角度重生成：%s", candidate
                )
                rejected = candidate
                continue
            content = candidate
            break
        if not content:
            content = _uc_ai_fallback(camp, round_no, my_number, used=history_for_check)
        try:
            await self.manager.player_undercover_speech(
                room, seat.visitor_token, content
            )
        except Exception as exc2:
            logger.warning(
                "[GameCompanion] AI 本地兜底发言提交也失败（将跳过，防卡局）：%s", exc2
            )
            try:
                await self.manager.skip_undercover_speaker(room, seat.visitor_token)
            except Exception as exc3:
                logger.warning(
                    "[GameCompanion] AI 发言失败后跳过也失败：%s", exc3
                )

    async def _undercover_ai_do_vote(
        self, room: GameRoom, seat, *, autopilot: bool = False
    ) -> None:
        game: UndercoverGame = room.game  # type: ignore[assignment]
        ai_vp = self._uc_player_for_seat(room, seat)
        my_number = (
            int(ai_vp.number)
            if ai_vp is not None
            else int(getattr(seat, "number", 0) or 0)
        )
        my_snap = game.snapshot(visitor_uid=_visitor_uid(seat.visitor_token))
        rounds = my_snap.get("rounds_public") or []
        live_players = my_snap.get("players_public") or []
        live_nums = [int(p["player_number"]) for p in live_players if not p.get("is_out")]
        if not rounds:
            # 没有发言记录：只从合法目标里选（PK 轮只能投平票候选人），避免非法票被拒导致 AI 卡投
            eligible = _uc_eligible_vote_targets(game, my_number) or [
                n for n in live_nums if n != my_number
            ]
            if not eligible:
                return
            try:
                await self.manager.player_undercover_vote(room, seat.visitor_token, eligible[0])
            except Exception:
                return
            return
        last = rounds[-1]
        round_speeches = last.get("speeches") or []
        # 组装提示：把每个存活玩家本轮的发言列出来
        lines = []
        for s in round_speeches:
            pn = int(s.get("player_number") or 0)
            if pn in live_nums and pn != my_number:
                lines.append(f"{pn}号玩家说：{s.get('content') or ''}")
        if not lines:
            eligible = _uc_eligible_vote_targets(game, my_number) or [
                n for n in live_nums if n != my_number
            ]
            if not eligible:
                return
            try:
                await self.manager.player_undercover_vote(room, seat.visitor_token, eligible[0])
            except Exception:
                return
            return
        my_camp = str((my_snap.get("my") or {}).get("camp") or "")
        my_word = str((my_snap.get("my") or {}).get("word") or "")
        # 同上：AI 投票也需用真实阵营/词条，避免「告知身份」关闭时拿到被隐去的空值
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
        autopilot_hint = (
            f"（{getattr(ai_vp, 'display_name', '') or f'{my_number}号'} 暂时掉线，"
            "由你托管代打：必须站在他/她的身份与词条立场上判断谁最可疑，"
            "不要机械地跟票。）"
            if autopilot
            else ""
        )
        prompt = (
            f"你正在玩「谁是卧底」。{autopilot_hint}{hint}"
            f"以下是本轮除你之外所有存活玩家的发言：\n"
            + "\n".join(lines[:8])
            + f"\n请判断谁最可疑（最不像自己阵营的人），只返回一个整数（玩家编号 1-{max(live_nums)}），"
            "不要返回任何其他文字或说明。"
        )
        # 合法目标（PK 轮只限平票候选人）
        eligible = _uc_eligible_vote_targets(game, my_number)
        target = None
        try:
            text = await asyncio.wait_for(
                self._generate_persona_text(room, prompt),
                timeout=8,  # 供应商故障时尽快走本地兜底，避免投票轮长时间停滞
            )
            m = __import__("re").search(r"\d+", str(text or ""))
            if m:
                n = int(m.group())
                if n in eligible:
                    target = n
        except Exception:
            target = None
        if target is None:
            # 本地兜底：稳定选一个合法目标（PK 只会选平票候选人）
            candidates = eligible or [n for n in live_nums if n != my_number]
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

    def _difficulty_for_game(self, game_type: str, value: Any) -> Difficulty:
        """海龟汤多一档「娱乐」，其余玩法仍是 easy/normal/hard。

        分开解析是因为棋类、骰子、黑杰克的难度表都是三档硬编码，把「娱乐」
        塞进去会直接 KeyError。
        """
        if str(game_type or "").strip().lower() == "turtle_soup":
            return self._soup_difficulty(value)  # type: ignore[return-value]
        return self._difficulty(value)

    def _soup_difficulty(self, value: Any) -> str:
        """海龟汤难度：空值用配置里的默认难度，认不出的按普通。"""
        text = str(value or "").strip().lower()
        if not text:
            text = str(getattr(self, "turtle_soup_difficulty", "normal") or "normal")
        aliases = {
            "娱乐": "entertainment",
            "休闲": "entertainment",
            "放松": "entertainment",
            "entertainment": "entertainment",
            "casual": "entertainment",
            "简单": "easy",
            "容易": "easy",
            "easy": "easy",
            "普通": "normal",
            "中等": "normal",
            "normal": "normal",
            "困难": "hard",
            "难": "hard",
            "hard": "hard",
        }
        return aliases.get(text, "normal")

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
            "weiqi": "weiqi",
            "go": "weiqi",
            "围棋": "weiqi",
            "圍棋": "weiqi",
            "tictactoe": "tictactoe",
            "tic-tac-toe": "tictactoe",
            "tic_tac_toe": "tictactoe",
            "井字棋": "tictactoe",
            "圈叉棋": "tictactoe",
            "othello": "othello",
            "reversi": "othello",
            "黑白棋": "othello",
            "奥赛罗": "othello",
            "奥赛罗棋": "othello",
            "黑白子": "othello",
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
            raise ValueError("目前只支持五子棋、中国象棋、围棋、井字棋、黑白棋、海龟汤、贪心骰子、你画我猜、二十一点和谁是卧底")
        return aliases[normalized]  # type: ignore[return-value]

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
    def _value_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on", "是", "确认"}
        return value is True

    # 「要开一局」的明确说法。必须是请求动作，不能是单个「玩」「棋」这种泛词，
    # 否则群里一句「我刚玩完」就会被判成开局请求。
    _ROOM_REQUEST_PHRASES: tuple[str, ...] = (
        "来一局",
        "来一把",
        "来一盘",
        "来局",
        "玩一局",
        "玩一把",
        "玩一盘",
        "开一局",
        "开一把",
        "开一盘",
        "开个局",
        "开个房",
        "开房间",
        "建个房",
        "建房间",
        "开个房间",
        "新建房间",
        "陪我玩",
        "陪你玩",
        "一起玩",
        "和我玩",
        "跟我玩",
        "带我玩",
        "玩游戏",
        "玩个游戏",
        "下棋",
        "下盘棋",
        "来盘棋",
        "打牌",
        "来把牌",
        "打一局",
        "房间链接",
        "打开房间",
        "进房间",
        "发我链接",
        "发个链接",
    )

    # 口语化的泛请求：单看这两个词不足以判断意图（「来个笑话」「开个玩笑」），
    # 所以只在没有离题词时才认，见 _OFF_TOPIC_PHRASES。
    _ROOM_REQUEST_LOOSE: tuple[str, ...] = (
        "来个",
        "开个",
        "整一局",
        "搞一局",
        "整一把",
        "搞一把",
    )

    # 命中这些词说明「来个 / 开个」后面接的不是游戏，不能算开局请求。
    _OFF_TOPIC_PHRASES: tuple[str, ...] = (
        "笑话",
        "段子",
        "玩笑",
        "梗",
        "表情包",
        "开会",
        "开个头",
        "开个价",
        "开个口",
        "开个店",
        "开个号",
        "开个会",
        "来个图",
        "来张图",
        "来首歌",
        "来首",
        "来段",
        "来个头像",
        "来个名字",
        "来个昵称",
        "来个外号",
        "开个新坑",
        "开个场",
        "开个白",
        "推荐",
        "介绍一下",
        "举个例子",
        "总结",
    )

    # 各玩法在自然语言里的叫法，用于判断这条消息是不是点了具体游戏。
    _GAME_NAME_HINTS: dict[str, tuple[str, ...]] = {
        "gomoku": ("五子棋", "连五"),
        "xiangqi": ("象棋",),
        "weiqi": ("围棋",),
        "tictactoe": ("井字棋", "井字", "圈圈叉叉"),
        "othello": ("黑白棋", "翻转棋", "奥赛罗", "othello"),
        "turtle_soup": ("海龟汤", "汤面", "汤底", "猜汤"),
        "pig_dice": ("贪心骰", "骰子"),
        "draw_guess": ("你画我猜", "画猜", "画画猜"),
        "blackjack": ("二十一点", "21点", "黑杰克", "blackjack"),
        "undercover": ("卧底",),
    }

    # 本插件没做的游戏：消息里出现这些词时，即使带了「来一局」也不开房，
    # 免得用户说「来把王者」结果蹦出一个五子棋房间。
    _UNSUPPORTED_GAME_HINTS: tuple[str, ...] = (
        "王者",
        "原神",
        "和平精英",
        "吃鸡",
        "英雄联盟",
        "lol",
        "金铲铲",
        "蛋仔",
        "迷你世界",
        "我的世界",
        "斗地主",
        "麻将",
        "狼人杀",
        "扫雷",
        "飞行棋",
    )

    @staticmethod
    def _event_group_id(event: Any) -> str:
        """取消息所属群号；私聊返回空串。兼容测试里的简易 event 对象。"""
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                return ""
        return str(getattr(event, "group_id", "") or "").strip()

    @staticmethod
    def _normalize_intent_text(message: str) -> str:
        """把消息洗成可比对的正文：去掉 CQ 码、@、空白与标点。

        @ 分两种：「@昵称 正文」整段去掉；紧贴正文的「@花火？来局五子棋」
        只去掉 @ 本身，否则整句会被吃掉，正常请求反而被拦。
        """
        text = re.sub(r"\[CQ:[^\]]*\]", "", str(message or ""))
        text = re.sub(r"@\S+\s", "", text).replace("@", "")
        return re.sub(
            r"[\s，。！？!?、,.~～…·:：;；\"'“”‘’()（）\[\]【】<>《》]",
            "",
            text,
        ).lower()

    @classmethod
    def _room_request_intent(cls, message: str, game_type: str) -> tuple[bool, str]:
        """判断这条消息是不是真的在要求开一局，返回 (是否放行, 判定依据)。

        建房并往群里发链接是副作用很大的动作（会打扰整个群，还会占住房间名额），
        以前完全交给模型自由裁量：群里只是被 @ 唤醒、随口提了句游戏名，模型也可能
        直接调建房工具，于是「没人说要玩，花火却发了房间链接」。这里把闸门放在代码里，
        模型再怎么想调也必须先过这一关。

        判定依据会写进日志，下次再出现类似情况可以直接查是哪句话触发的。
        """
        text = cls._normalize_intent_text(message)
        if not text:
            return False, "空消息"
        names = cls._GAME_NAME_HINTS.get(str(game_type or ""), ())
        if any(name in text for name in names):
            return True, "点了具体游戏名"
        if any(bad in text for bad in cls._UNSUPPORTED_GAME_HINTS):
            return False, "提到了插件不支持的其它游戏"
        if any(phrase in text for phrase in cls._ROOM_REQUEST_PHRASES):
            return True, "带有开局请求"
        if any(word in text for word in cls._ROOM_REQUEST_LOOSE) and not any(
            off_topic in text for off_topic in cls._OFF_TOPIC_PHRASES
        ):
            return True, "口语化的开局请求"
        return False, "只是闲聊，没有开局请求"

    # 被闸门拦下之后，花火通常会追问一句「要不要来一局」；用户在几分钟内
    # 回一句「好 / 来吧」也应该能开局，否则追问就成了死路（用户答应了却开不出来）。
    _ROOM_CONFIRM_REPLIES: frozenset[str] = frozenset(
        {
            "好",
            "好呀",
            "好的",
            "好啊",
            "行",
            "可以",
            "来吧",
            "来",
            "要",
            "嗯",
            "嗯嗯",
            "愿意",
            "那就来",
            "走",
            "冲",
            "开吧",
            "ok",
            "okay",
        }
    )
    _ROOM_INTENT_BLOCK_TTL = 300.0

    def _remember_room_intent_block(self, session_id: str) -> None:
        if not session_id:
            return
        store = self.__dict__.setdefault("_room_intent_blocked_at", {})
        store[session_id] = time.monotonic()

    def _room_followup_confirmed(self, session_id: str, message: str) -> bool:
        """这条消息是不是「刚被拦下后，用户对追问说的那句肯定回应」。"""
        if not session_id:
            return False
        store = self.__dict__.get("_room_intent_blocked_at") or {}
        blocked_at = store.get(session_id)
        if not blocked_at:
            return False
        if time.monotonic() - blocked_at > self._ROOM_INTENT_BLOCK_TTL:
            store.pop(session_id, None)
            return False
        text = self._normalize_intent_text(message)
        return bool(text) and text in self._ROOM_CONFIRM_REPLIES

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
        # 用 setdefault 兜底：测试里常用 __new__ 直接造实例（不走 __init__），
        # 没有这个字段时也要能正常派发后台任务。
        tasks = self.__dict__.setdefault("_background_tasks", set())
        tasks.add(task)

        def finish(finished: asyncio.Task) -> None:
            tasks.discard(finished)
            try:
                finished.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("[GameCompanion] 后台任务失败: %s", exc)

        task.add_done_callback(finish)
        return task
