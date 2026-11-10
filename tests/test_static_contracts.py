from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_config_defaults_match_product_contract() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    rooms = schema["rooms"]["items"]

    assert rooms["max_group_rooms"]["default"] == 1
    assert rooms["max_private_rooms"]["default"] == 1
    assert rooms["empty_player_timeout_seconds"]["default"] == 60
    assert rooms["idle_timeout_seconds"]["default"] == 300
    server = schema["server"]["items"]
    assert server["cloudflared_path"]["default"] == ""
    assert server["allow_cloudflared_download"]["default"] is True
    assert server["external_base_url"]["default"] == ""
    assert schema["logging"]["items"]["level"]["default"] == "inherit"
    assert schema["logging"]["items"]["level"]["type"] == "string"
    assert rooms["allow_non_admin_group_creation"]["default"] is False
    assert "默认创建普通房间" in rooms["allow_non_admin_group_creation"]["hint"]
    identity = schema["identity"]["items"]
    assert identity["enable_trusted_browser"]["default"] is False
    assert identity["trusted_browser_ttl_days"]["default"] == 30
    context = schema["context"]["items"]
    assert context["enable_private_qq_game_context"]["default"] is True
    assert context["recent_game_result_ttl_minutes"]["default"] == 30
    assert schema["xiangqi"]["items"]["allow_engine_download"]["default"] is True
    assert schema["xiangqi"]["items"]["auto_download_engine"]["default"] is False
    assert schema["turtle_soup"]["items"]["max_hints"]["default"] == 3
    assert schema["turtle_soup"]["items"]["content_level"]["default"] == "normal"
    # 提问与推理各有一份额度，都是整局共享；0 表示不限
    assert schema["turtle_soup"]["items"]["max_questions"]["default"] == 15
    assert schema["turtle_soup"]["items"]["max_answers"]["default"] == 8
    # 模型配置要能在面板里下拉选，别退回手填 Provider ID
    for key in ("generation_provider_id", "judge_provider_id"):
        assert (
            schema["turtle_soup"]["items"][key]["_special"] == "select_provider"
        ), f"{key} 应该是下拉选择（select_provider）"
    # 海龟汤不限人数（0）：群里参与的人越多越好，不该被座位数挡在门外
    assert schema["turtle_soup"]["items"]["max_players"]["default"] == 0
    assert schema["multiplayer"]["items"]["turn_timeout_seconds"]["default"] == 60
    assert schema["multiplayer"]["items"]["duel_turn_timeout_seconds"]["default"] == 180
    assert (
        schema["multiplayer"]["items"]["duel_offline_grace_seconds"]["default"] == 90
    )
    assert (
        schema["multiplayer"]["items"]["swap_request_cooldown_seconds"]["default"] == 30
    )
    assert (
        schema["multiplayer"]["items"]["swap_request_expiry_seconds"]["default"] == 20
    )
    integration = schema["companion_integration"]["items"]
    assert integration["enable_emotional_afterglow"]["default"] is False
    assert integration["enable_proactive_invites"]["default"] is False
    assert integration["proactive_invite_probability_percent"]["default"] == 18
    assert integration["proactive_invite_cooldown_hours"]["default"] == 24
    draw_guess = schema["draw_guess"]["items"]
    assert draw_guess["duration_seconds"]["default"] == 120
    assert draw_guess["max_guesses"]["default"] == 5
    assert draw_guess["vision_provider_id"]["_special"] == "select_provider"
    for game_type in (
        "gomoku",
        "xiangqi",
        "tictactoe",
        "othello",
        "turtle_soup",
        "pig_dice",
        "draw_guess",
        "blackjack",
    ):
        assert schema[game_type]["items"]["enabled"]["default"] is True
    assert schema["pig_dice"]["items"]["target_score"]["default"] == 50
    assert schema["blackjack"]["items"]["max_players"]["default"] == 1


def test_metadata_registers_default_management_page() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))

    assert metadata["astrbot_version"] == ">=4.24.2"
    assert metadata["repo"] == (
        "https://github.com/Katou-Kouseki/astrbot_plugin_game_companion_huahuo"
    )
    assert metadata["pages"] == [{"name": "游戏管理台", "title": "游戏管理台"}]
    assert metadata["version"] == "0.6.02"


def test_chat_messages_render_local_timestamp() -> None:
    """每条聊天消息都要带小字精确时间，且不能再把 meta 单独塞进 article。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert "function formatMessageTime(at)" in script
    assert "function messageTimeNode(at)" in script
    assert 'head.className = "message-head"' in script
    assert "time.classList.add(\"system-time\")" in script
    assert "at: Date.now() / 1000," in script, "本地乐观消息也要有时刻"
    assert ".message-time" in css and ".message-head" in css


def test_plugin_version_reads_metadata_instead_of_hardcoding() -> None:
    """版本号只保留在 metadata.yaml，避免升级后管理台还显示旧版本。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "PLUGIN_VERSION = _read_plugin_version()" in source
    assert re.search(r"PLUGIN_VERSION = \"", source) is None
    assert "def _read_plugin_version() -> str:" in source


def test_frontends_do_not_use_external_cdn_or_inline_scripts() -> None:
    for page in (
        ROOT / "web" / "index.html",
        ROOT / "pages" / "游戏管理台" / "index.html",
    ):
        content = page.read_text(encoding="utf-8")
        assert "https://" not in content
        assert "<script>" not in content


def test_mobile_room_can_resume_its_prebound_player_identity() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'new URLSearchParams(window.location.search).get("visitor_token")' in script
    assert "async def mobile_create_room" in source
    assert 'session_id = f"mobile:{normalized_user}"' in source


def test_trusted_browser_controls_are_registered_without_exposing_cookie() -> None:
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'id="rememberIdentity"' in page
    assert 'id="forgetIdentity"' in page
    assert 'request("POST", "identity/forget"' in script
    assert 'httponly=True' in server
    assert 'secure=True' in server


def test_player_move_is_rendered_before_waiting_for_bot_response() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    optimistic_render = script.index(
        'pendingMove = { kind: "gomoku", row, column, color: myColor() };'
    )
    move_request = script.index(
        'await request("POST", "move", { visitor_token: visitorToken, row, column })',
        optimistic_render,
    )

    assert optimistic_render < move_request
    assert "pendingMove = null;" in script[move_request:]


def test_xiangqi_webui_uses_server_legal_moves_without_game_switcher() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "room.game.legal_moves" in script
    assert 'room.game_type === "xiangqi"' in script
    assert "from_row" in script and "to_column" in script
    assert "switch_game" not in script
    assert "切换游戏" not in page


def test_tictactoe_webui_has_three_by_three_board_and_optimistic_move() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'room?.game_type === "tictactoe"' in script
    assert '[["human_x", "我执 X"], ["human_o", "我执 O"]' in script
    optimistic = script.index(
        'pendingMove = { kind: "tictactoe", row, column, mark: room.game.human_mark };'
    )
    request = script.index(
        'await request("POST", "move", { visitor_token: visitorToken, row, column })',
        optimistic,
    )
    assert optimistic < request
    assert "function drawTicTacToe()" in script
    assert ".board-stage.tictactoe" in style


def test_othello_webui_has_eight_by_eight_board_and_legal_move_feedback() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'room?.game_type === "othello"' in script
    assert '["human_black", "我执黑"], ["bot_black", "我执白"]' in script
    assert "function drawOthello()" in script
    assert "room.game.legal_moves" in script
    assert 'pendingMove = { kind: "othello", row, column, color: myColor() };' in script
    assert ".board-stage.othello" in style


def test_every_game_exposes_leave_seat_outside_the_undercover_ready_area() -> None:
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    # 退席按钮不能挂在「谁是卧底集结准备区」里：那个区域只有卧底 setup 阶段可见，
    # 其它玩法入座后就只剩「加入对局」，找不到退出入口。
    ready_area_at = page.index('id="ucReadyArea"')
    leave_row_at = page.index('id="seatLeaveRow"')
    leave_button_at = page.index('id="ucLeaveSeatButton"')
    assert leave_row_at > ready_area_at
    assert leave_button_at > leave_row_at
    assert "function renderSeatLeave()" in script
    assert "renderSeatLeave();" in script
    assert 'document.getElementById("seatLeaveRow")' in script
    # 对局中是否置灰由 seatLeaveLocked() 统一判断：
    # 两人对弈看「对面是不是真人」，其余上座玩法一律禁止中途退席；
    # 判断不再按玩法名硬编码，也不再依赖 player_mode。
    assert "function roomSupportsSeats(target = room)" in script
    assert "function seatLeaveLocked(target = room)" in script
    assert "if (isDuelGame(target)) return hasHumanOpponent(target);" in script


def test_solo_leave_during_active_game_requires_confirmation() -> None:
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    # 单人局对局中退席会让本局作废，必须先弹确认
    assert 'id="confirmOverlay"' in page
    assert 'id="confirmOk"' in page
    assert 'id="confirmCancel"' in page
    assert "function askConfirm(" in script
    assert "await askConfirm({" in script
    assert 'room.status === "active" || room.status === "paused"' in script
    assert "!seatLeaveLocked(room)" in script
    assert "退出后这一局立即作废" in script
    assert 'event.key === "Escape"' in script

    # 预留多人上座：snapshot 暴露 player_mode，后端用统一的能力集合判断席位策略
    assert '"player_mode": "multiplayer" if multiplayer.enabled else "solo_ai"' in models
    assert "SEAT_CAPABLE_GAMES: frozenset[GameType]" in manager
    assert "if room.game_type not in SEAT_CAPABLE_GAMES:" in manager
    assert "if room.game_type in SEAT_CAPABLE_GAMES:" in manager


def test_board_games_support_two_human_seats_and_ai_opponent() -> None:
    """五子棋 / 黑白棋：默认 2 席，可等真人，也可「和花火对局」。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    # 后端：两种棋类进入上座集合，并有独立的「两人对弈」集合与执色分配
    assert (
        '"turtle_soup",\n        "blackjack",\n        "undercover",\n        "gomoku",'
        '\n        "othello",\n        "xiangqi",\n        "weiqi",'
        in manager
    )
    assert 'frozenset(\n    {"gomoku", "othello", "xiangqi", "weiqi"}\n)' in manager
    assert "def _assign_duel_sides(" in manager
    assert "def _seat_color(" in manager
    assert "async def add_ai_seat(" in manager
    assert "async def remove_ai_seat(" in manager
    # 胜负按座位归属：只给赢家记胜，输家与 AI 座位不记
    assert "winner_tokens: set[str] | None = None" in manager
    assert "self._bump_seat_win(room, winner_seat)" in manager
    # 落子鉴权改成「当前座位」，两人对局不再只认主玩家 token
    assert "seat = room.multiplayer.seat_for_token(visitor.token)" in manager
    # 座位执色随快照下发，前端据此判断「我执黑还是执白」
    assert '"side": seat.side,' in models

    # 服务端新增 seat/ai（和花火对局 / 等对手来）
    assert '"/api/room/{access_token}/seat/ai"' in server
    assert "async def _seat_ai(" in server

    # 前端：座位面板新增「等对手来」，两人对弈不再用 human_color 判断轮次
    assert 'id="seatWaitRow"' in page
    assert 'id="seatWaitButton"' in page
    assert "function renderSeatWait()" in script
    assert "和花火对局" in script
    assert "等对手来" in script
    assert "function myColor(target = room)" in script
    assert "function hasHumanOpponent(target = room)" in script
    assert "room.game.turn !== myColor()" in script
    assert "room.game.turn === myColor()" in script


def test_xiangqi_supports_two_human_seats() -> None:
    """中国象棋：2 席、红黑分色、按座位视角下发可走法。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    xiangqi = (ROOT / "xiangqi.py").read_text(encoding="utf-8")

    # 后端：象棋进入上座集合与两人对弈集合，固定 2 席
    assert '"xiangqi": 2,' in manager
    assert '"weiqi": 2,' in manager
    assert (
        '"turtle_soup", "blackjack", "undercover", "gomoku", "othello", "xiangqi", "weiqi",'
        in " ".join(manager.split())
    )
    assert '{"gomoku", "othello", "xiangqi", "weiqi"}' in manager
    # 执色按玩法区分：象棋红/黑，棋类黑/白
    assert '("red", "black") if room.game_type == "xiangqi" else ("black", "white")' in manager
    assert "def _seat_side(" in manager
    assert "def _duel_seat_for_side(" in manager
    assert "def _duel_winner_seat(" in manager

    # 引擎：按执色落子 + 按视角下发合法着法
    assert "async def place_side(" in xiangqi
    assert '"viewer_side": viewer' in xiangqi
    assert "if self.turn == viewer and not self.finished" in xiangqi
    assert "viewer_side=my_seat.side if my_seat else None" in models

    # 前端：象棋属于两人对弈，轮次与棋盘朝向按座位执方判断
    assert 'const DUEL_GAMES = ["gomoku", "othello", "xiangqi", "weiqi"];' in script
    assert "function mySide(target = room)" in script
    assert 'room?.game_type === "xiangqi" && mySide() === "black"' in script
    assert "if (room.game.turn !== mySide()) return;" in script


def test_duel_turn_clock_pauses_on_disconnect_and_forfeits_on_timeout() -> None:
    """两位真人对弈：掉线先暂停等重连，超时判负；和花火练习局不计时。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "duel_turn_timeout: int = 180" in manager
    assert "duel_offline_grace: int = 90" in manager
    assert "def _duel_turn_owner(" in manager
    assert "def _duel_human_pair(" in manager
    assert "def _tick_duel_locked(" in manager
    assert "def _forfeit_duel_locked(" in manager
    # 掉线 -> 暂停 -> 宽限结束判负；在线超时直接判负
    assert "state.duel_paused = True" in manager
    assert 'return {"forfeit": seat, "label": label, "reason": "offline"}' in manager
    assert 'return {"forfeit": seat, "label": label, "reason": "timeout"}' in manager
    assert "game.winner = WHITE if loser_color == BLACK else BLACK" in manager

    # 快照下发暂停状态，前端据此把倒计时换成「等待重连」
    assert '"duel_paused": bool(multiplayer.enabled and multiplayer.duel_paused)' in models
    assert "function duelClockSuffix(" in script
    assert "duelClockSuffix()" in script
    assert "掉线暂停" in script


def test_management_page_can_switch_games_and_install_engine() -> None:
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")

    assert 'action: "switch_game"' in script
    assert '["tictactoe", "井字棋"]' in script
    assert '["othello", "黑白棋"]' in script
    assert '["turtle_soup", "海龟汤"]' in script
    assert "confirm_abandon: active" in script
    assert 'endpoint("POST", "xiangqi/install")' in script
    assert "window.confirm" not in script
    assert 'id="confirmDialog"' in page
    assert "confirmAction" in script
    assert 'endpoint("GET", "settings")' in script
    assert 'endpoint("POST", "settings/update"' in script
    assert 'id="gameSettingsGrid"' in page
    assert 'data-panel="settings"' in page


def test_webui_uses_one_room_chat_composer_for_turtle_soup_and_controls() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="soupStage"' in page
    assert 'id="chatComposer"' in page
    assert 'id="chatInput"' in page
    assert 'id="soupSolution"' in page
    assert 'request("POST", "chat"' in script
    assert '["turtle_soup", "pig_dice", "draw_guess", "blackjack", "undercover"].includes(room.game_type)' in script
    assert ".side-choice[hidden] { display: none; }" in styles
    assert 'chatInput.value = "";' in script
    assert "function submitChat" in script
    assert "switch_game" not in script
    assert "切换游戏" not in page
    assert 'request("POST", "seat/swap/request"' in script
    assert 'id="messages"' in page


def test_room_expiry_does_not_stop_the_shared_access_channel() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "_stop_idle_access" not in source
    assert "self._schedule_tunnel_recovery()" in source


def test_natural_language_rematch_is_routed_to_the_existing_room() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'action == "rematch"' in source
    assert "request_rematch" in source
    # 再来一局等局内操作仍留在 WebUI；但「切换游戏」在旧房间空闲时由建房工具自动完成
    assert "由用户进入 WebUI 后完成" in source
    assert "自动把旧房间切换成新玩法" in source


def test_room_creation_tool_exposes_explicit_admin_room_mode() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "admin_room(boolean)" in source
    assert "_admin_room_requested" in source
    assert "只有游戏管理员可以创建管理员房间" in source


def test_pig_dice_webui_and_qq_menu_are_registered() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert '@filter.command_group("game")' in source
    assert '@game_commands.command("游戏菜单"' in source
    assert '"贪心骰子": "pig_dice"' in source
    assert 'id="diceStage"' in page
    assert 'request("POST", "dice/action"' in script
    assert '["pig_dice", "贪心骰子"]' in manager


def test_draw_guess_webui_and_visual_endpoint_are_registered() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert '"你画我猜": "draw_guess"' in source
    assert 'id="drawCanvas"' in page
    assert 'request("POST", "draw/strokes"' in script
    assert 'request("POST", "draw/guess"' in script
    assert '"/api/room/{access_token}/draw/guess"' in server
    assert '["draw_guess", "你画我猜"]' in manager


def test_blackjack_webui_endpoint_and_management_registration() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert '"二十一点": "blackjack"' in source
    assert '"/api/room/{access_token}/blackjack/action"' in server
    assert 'id="blackjackStage"' in page
    assert 'request("POST", "blackjack/action"' in script
    assert '["blackjack", "二十一点"]' in manager


def test_draw_guess_queues_local_changes_without_accepting_stale_polling_state() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "let drawSyncPromise = null;" in script
    assert "let drawDirty = false;" in script
    assert "&& !drawDirty" in script
    assert "if (drawSyncPromise) return drawSyncPromise;" in script
    assert "while (drawDirty)" in script
    assert "drawDirty = true;" in script
    assert "if (!(await syncDrawing())) return;" in script


def test_fast_game_replies_do_not_pollute_normal_conversation_history() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "_sync_conversation_pair" not in source
    assert "add_message_pair" not in source
    assert "record_shared_experience" in source
    assert "submit_room_chat" in source
    assert "WebUI 的房间中与用户聊天" in source
    assert "_memory_context_for_visitor" in source


def test_tictactoe_is_available_to_natural_language_tools() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '"井字棋": "tictactoe"' in source
    assert '"圈叉棋": "tictactoe"' in source
    assert '"tictactoe": "井字棋"' in source


def test_othello_is_available_to_natural_language_tools() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '"黑白棋": "othello"' in source
    assert '"奥赛罗": "othello"' in source
    assert '"othello": "黑白棋"' in source


def test_every_game_has_rules_and_admin_label() -> None:
    """规则面板与管理台标签必须覆盖全部玩法，否则会回退成错误的游戏文案。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    start = script.index("const UC_GAME_META = {")
    meta_block = script[start : script.index("\n  };", start)]
    labels_start = manager.index("const GAME_LABELS = {")
    labels_block = manager[labels_start : manager.index("\n  };", labels_start)]

    games = (
        "gomoku",
        "xiangqi",
        "tictactoe",
        "othello",
        "turtle_soup",
        "pig_dice",
        "draw_guess",
        "blackjack",
        "undercover",
    )
    for game in games:
        assert f"\n    {game}: {{" in meta_block, f"UC_GAME_META 缺少 {game}"
        assert f"{game}:" in labels_block, f"GAME_LABELS 缺少 {game}"

    othello_start = meta_block.index("othello: {")
    othello_block = meta_block[othello_start : meta_block.index("\n    },", othello_start)]
    assert 'name: "黑白棋"' in othello_block
    assert "8×8" in othello_block
    assert "夹住" in othello_block
    assert 'othello: "黑白棋"' in labels_block


def test_room_link_is_delivered_outside_model_rewrite_with_fallback() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "link_delivered = await self._deliver_room_link" in source
    assert '"room_url": "" if link_delivered else url' in source
    assert 'MessageChain([Plain("\\n".join(lines))])' in source
    assert "将交由模型回复回退" in source


def test_browser_reports_departure_with_heartbeat_fallback() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert 'window.addEventListener("pagehide"' in script
    assert "window.navigator.sendBeacon" in script
    assert 'endpoint("leave")' in script
    assert "FINISHED_PLAYER_LEAVE_GRACE_SECONDS = 8" in manager
    assert "FINISHED_PLAYER_HEARTBEAT_TIMEOUT_SECONDS = 60" in manager


def test_commentary_narrates_duel_as_spectator_with_own_provider() -> None:
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    # 两位真人时花火只能旁观解说，不能把自己算成对局一方
    assert "def _commentary_duel_room(" in main
    assert "你不在棋盘上，也没有执任何一方" in main
    assert "不要说自己下了哪一步" in main
    # 解说提示词改用「谁执哪方 / 谁刚走完 / 轮到谁」，不再写死玩家 vs Bot
    assert "def _commentary_facts(" in main
    assert "刚走完这一步" in main
    # 走子事件带上座位信息，解说才知道这一步是谁下的
    assert "seat_name=self._seat_display(seat)" in manager
    assert 'seat_name="花火"' in manager
    # 解说只配一个 Provider：Provider 自带默认模型，不再单独填模型名
    assert '"commentary.provider_id"' in main
    assert '"commentary.model"' not in main
    assert schema["commentary"]["items"]["provider_id"]["default"] == ""
    assert "model" not in schema["commentary"]["items"]
    assert "get_provider_by_id" in main


def test_weiqi_ships_winrate_panel_and_admin_difficulty_switch() -> None:
    """围棋：棋盘 + 停一手 + 右侧实时胜率；管理台只给棋类房间开放棋力切换。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager_js = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(
        encoding="utf-8"
    )
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    weiqi = (ROOT / "weiqi.py").read_text(encoding="utf-8")

    # 规则引擎：提子 / 劫 / 停一手 / 数子 / 胜率
    assert "class WeiqiGame" in weiqi
    assert "def territories(" in weiqi
    assert "def area_score(" in weiqi
    assert "def evaluate(" in weiqi
    assert "def pass_turn(" in weiqi
    assert '"kind": "weiqi"' in weiqi

    # 后端：围棋进入对弈集合，停一手有独立接口
    assert '"weiqi",' in manager
    assert "async def player_pass(" in manager
    assert '"/api/room/{access_token}/weiqi/pass"' in server
    assert "async def _weiqi_pass(" in server
    assert '"/api/room/{access_token}/weiqi/resign"' in server
    assert "async def _weiqi_resign(" in server

    # 前端：围棋棋盘绘制、停一手按钮、右侧胜率面板
    assert "function drawWeiqi()" in script
    assert "function moveWeiqi(event)" in script
    assert "function passWeiqi()" in script
    assert "function resignWeiqi()" in script
    assert "function renderEvalPanel()" in script
    assert 'id="evalPanel"' in page
    assert 'id="weiqiPassAction"' in page
    assert 'id="weiqiResignAction"' in page
    assert 'id="evalBlackBar"' in page
    assert 'weiqi: "围棋"' in script

    # 解说：围棋会带上胜率与目差事实
    assert "_weiqi_commentary_prompt" in main
    assert "当前评估黑方胜率" in main

    # 管理台：只有棋类房间出现棋力下拉
    assert "DIFFICULTY_GAME_TYPES" in models
    assert '"difficulty_adjustable": self.game_type in DIFFICULTY_GAME_TYPES' in models
    assert "async def set_difficulty(" in manager
    assert 'action: "set_difficulty"' in manager_js
    assert 'className = "difficulty-select"' in manager_js
    assert 'room.difficulty_adjustable' in manager_js


def test_player_picks_huahuo_difficulty_inside_room() -> None:
    """棋类房间：落座玩家（对手非真人）可自选花火棋力。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")

    assert "difficulty_chosen_by_player" in models
    assert "async def player_set_difficulty(" in manager
    assert "和真人对局时不能调整花火棋力" in manager
    assert '"/api/room/{access_token}/difficulty"' in server
    assert "async def _set_difficulty(" in server
    assert 'id="difficultyPicker"' in page
    assert "function setDifficulty(" in script
    assert "canPickDifficulty" in script


def test_othello_reuses_the_same_winrate_panel_as_weiqi() -> None:
    """黑白棋复用围棋那块胜率面板：子数差换算，别再写第二套 DOM。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    othello = (ROOT / "othello.py").read_text(encoding="utf-8")

    # 引擎：黑白棋自己算胜率，并随快照下发
    assert "def evaluate(" in othello
    assert '"eval": self.evaluate(),' in othello
    assert '"empty": empty,' in othello

    # 前端：一块面板两个棋类，其余玩法保持隐藏
    assert 'id="evalPanel"' in page
    assert 'id="weiqiEval"' not in page
    assert "const isOthello = room?.game_type === \"othello\";" in script
    assert "const supported = isWeiqi || isOthello;" in script
    assert "子数差估算" in script
    assert "中国规则数子" in script


def test_console_create_room_and_ui_polish_contracts() -> None:
    """管理台手动开房 + 解说 provider 下拉 + 停一手按钮 + 引擎图标居中。"""
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    manager_js = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(
        encoding="utf-8"
    )
    manager_page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(
        encoding="utf-8"
    )
    manager_css = (ROOT / "pages" / "游戏管理台" / "manager.css").read_text(
        encoding="utf-8"
    )
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    # 解说模型：和新版插件一样用 select_provider 下拉，而不是手填 Provider ID
    commentary = schema["commentary"]["items"]
    assert commentary["provider_id"]["_special"] == "select_provider"
    assert "select_provider" in json.dumps(schema, ensure_ascii=False)

    # 管理台手动开房：后端有独立接口，且不受私聊并行上限约束
    assert '"/room/create"' not in main  # 走 PAGE_API_PREFIX，不能写死绝对路径
    assert 'f"{PAGE_API_PREFIX}/room/create"' in main
    assert "async def page_create_room(" in main
    assert "ADMIN_CONSOLE_SESSION" in main
    assert "bypass_limit: bool = False" in manager
    assert "not bypass_limit" in manager
    assert '"url": self._room_url_safe(room)' in main

    # 管理台 UI：玩法下拉 + 开新房间按钮 + 每行复制链接
    assert 'id="newRoomGame"' in manager_page
    assert 'id="createRoomAction"' in manager_page
    assert "async function createRoomFromConsole()" in manager_js
    assert 'endpoint("POST", "room/create"' in manager_js
    assert "async function copyText(text)" in manager_js
    assert "navigator.clipboard" in manager_js
    assert "复制访问网址" in manager_js

    # 停一手：挪进右侧操作面板，轮到自己时点亮
    assert 'class="pass-action"' in page
    assert page.index('id="weiqiActions"') > page.index('class="game-panel"')
    assert page.index('id="weiqiActions"') > page.index('class="board-stage"')
    assert ".board-actions.side-actions" in style
    assert ".pass-action.is-ready" in style
    assert 'passButton.classList.toggle("is-ready", myTurn)' in script

    # 引擎图标居中：定位规则不能再被「文字块 span」规则命中
    assert ".engine-identity > div > span" in manager_css
    assert ".engine-identity span {" not in manager_css
    assert "line-height: 0" in manager_css


def test_eval_panel_collapses_and_duel_seats_show_both_players() -> None:
    """胜率条默认收起（玩家）+ 数字取整，棋盘上方显示黑白双方头像。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    # C 方案：可折叠，玩家默认收起、观众默认展开，展开 20 秒后自动收回
    assert 'id="evalToggle"' in page
    assert 'id="evalBody"' in page
    assert 'class="eval-panel is-collapsed"' in page
    assert "function setEvalCollapsed(" in script
    assert "evalCollapsed = Boolean(room?.is_player)" in script
    assert "}, 20000);" in script
    assert ".eval-panel.is-collapsed .eval-body { display: none; }" in style

    # A 方案：百分比取整、条形图按整数走、动画放慢、只在明显领先时变色
    assert "const blackPct = Math.round(black);" in script
    assert "const whitePct = Math.round(white);" in script
    assert "transition: width .8s" in style
    assert "is-black-ahead" in style and "is-black-ahead" in script

    # 对局双方：棋盘上方的黑白（象棋红黑）座位卡，带头像与昵称
    assert 'id="duelSeats"' in page
    assert 'id="duelSeatAName"' in page and 'id="duelSeatBName"' in page
    assert "function renderDuelSeats()" in script
    assert "function duelSeatSideOrder()" in script
    assert "ucSeatAvatarNode(seat.number" in script
    assert page.index('id="duelSeats"') < page.index('id="weiqiActions"')
    assert ".duel-seat.is-active" in style


def test_opening_commentary_does_not_wait_on_memory_and_shows_typing() -> None:
    """开局解说：跳过记忆检索、人格并发、网页显示「正在看棋盘」并把轮询提速。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    # 记忆检索与人格读取并发，别串行叠加在模型调用前面
    assert "async def _generate_persona_text(" in main
    assert "with_memory: bool = True" in main
    assert "persona, memory = await asyncio.gather(" in main
    # 开场白只讲棋局事实，跳过记忆检索
    assert "with_memory=False," in main
    # 生成期间挂「正在输入」标记
    assert "room.bot_typing_pending += 1" in main
    assert "room.bot_typing_pending = max(0, room.bot_typing_pending - 1)" in main
    assert "bot_typing_pending: int = 0" in models
    assert '"bot_typing": self.bot_typing_pending > 0,' in models
    # 前端占位 + 快轮询 + 长句打字机加速
    assert "花火正在看着棋盘" in script
    assert ") || Boolean(room?.bot_typing) || Boolean(room?.bot_chat_typing);" in script
    assert "function typingPace(length)" in script


def test_weiqi_board_size_capture_fx_and_in_play_join_guard() -> None:
    """围棋盘路自选 + 提子残影 + 局中不给「加入对局」入口。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    weiqi = (ROOT / "weiqi.py").read_text(encoding="utf-8")

    # 后端：房间级盘路 + 首位落座玩家的调整接口
    assert "weiqi_size: int = 13" in models
    assert '"adjustable": bool(' in models
    assert "async def set_weiqi_board(" in manager
    assert "def _is_first_seated_human(" in manager
    assert "只有最先入座的玩家可以调整盘路" in manager
    assert '"/api/room/{access_token}/weiqi/board"' in server
    assert "async def _weiqi_board(" in server

    # 引擎：提子坐标随快照下发
    assert "last_captured_points" in weiqi
    assert '"last_captured_points": [list(point) for point in self.last_captured_points]' in weiqi

    # 前端：盘路选择器 + 空棋盘按房间设置重绘
    assert 'id="weiqiBoardPanel"' in page
    assert 'data-weiqi-size="19"' in page
    assert "function renderWeiqiBoard()" in script
    assert "async function changeWeiqiBoard(size)" in script
    assert 'request("POST", "weiqi/board"' in script
    assert "Number(room?.game?.size || room?.weiqi?.size || 13)" in script
    assert ".weiqi-board-choices button.is-active" in style

    # 前端：提子残影 + 「提 N 子」气泡
    assert "function syncCaptureFx()" in script
    assert "function drawCaptureFx(margin, gap)" in script
    assert "captureFx.points" in script
    assert "drawCaptureFx(margin, gap);" in script
    assert 'id="captureBlack"' in page and 'id="captureWhite"' in page
    assert ".capture-stone.black" in style

    # 局中按钮：观战者看到的是禁用的「本局进行中」，玩家侧入口直接隐藏
    assert 'const inPlay = room.status === "active" || room.status === "paused";' in script
    assert 'action.innerHTML = \'<i data-lucide="eye"></i><span>本局进行中</span>\';' in script
    assert "等这局结束后再加入玩家席" in script


def test_spectator_can_take_over_ai_seat_after_finished_duel() -> None:
    """对局结束后，观众席玩家可以接替花火座位（前端按钮 + 后端 claim 让位）。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    # 前端：花火占的座位不算「真满」，观众看到的是可点的「接替花火」
    assert "const vacatableAi = isDuelGame(room) && hasAiOpponent(room) && !inPlay;" in script
    assert '接替花火' in script
    assert "花火这局已结束，你可以接替花火的位置" in script
    # 后端：claim 在席位满且坐着花火时，先把花火请下座位再安进真人
    assert "vacated_ai = self._ai_seat(room)" in manager
    assert "把位置让给了" in manager


def test_board_report_uses_frozen_snapshot_and_throttles() -> None:
    """棋类播报读终局快照（避免再战后变 0 手），并带节流与解说防丢。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")

    # 终局快照
    assert "last_board_result" in models
    assert "last_board_announce_at" in models
    assert "_snapshot_board_result" in manager
    assert "def _board_result_payload" in main
    # 播报节流
    assert "board_group_announce_min_moves" in main
    assert "board_group_announce_cooldown" in main
    assert '"group_announce_cooldown_seconds"' in schema
    assert '"group_announce_min_moves"' in schema
    # 解说防丢
    assert "expect_game_uid" in main


def test_board_poster_highlights_and_gold_title() -> None:
    """战报海报：获胜方名称用金色大字 + 白色描边（text-shadow 模拟，不被 t2i 渲染器压暗），并带终局高亮。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    # 名称（大字结果）用金色 accent；描边用 text-shadow 白色（画在文字后面），
    # 不再用 -webkit-text-stroke（其半透明黑描边会被自建 t2i 渲染器画到填充之上，
    # 把文字压暗 45%：白→#8c8c8c、金→#8c7338）
    assert "color: {{ accent }};" in main
    assert "-webkit-text-stroke" not in main
    assert "5px 0 1px #fff" in main  # 白色描边（手动调粗后保留）
    assert "0 4px 18px rgba(0,0,0,.3)" in main  # 保留下投影
    # 五子棋连线
    assert "winning_line" in manager
    assert "_gomoku_winning_line" in manager
    # 象棋最后一手 + 黑白棋落子/子数
    assert '"last_move"' in manager
    assert '"piece_count"' in manager
    assert "last_move: dict[str, object] | None = None" in main


def test_board_report_commands_render_posters_for_testing() -> None:
    """棋类战报/日报测试指令能出海报：战报按棋类出终局高亮图，日报出排行榜海报。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    # 战报测试指令：生成文字 + 海报，并支持按棋类选样例
    assert '@filter.command("棋类战报", alias={"棋类战报样式测试"})' in main
    assert "async def board_report_test(self, event: AstrMessageEvent, game: str = \"\")" in main
    assert "self._board_announce_poster(room)" in main
    assert "def _board_sample_room(self, game_key: str = \"\") -> Any" in main
    assert "def _board_sample_game(game_type: str) -> Any" in main
    assert 'self.manager._snapshot_board_result(room)' in main

    # 日报测试指令：生成排行榜海报（t2i 优先、Pillow 兜底、文字回退）
    assert '@filter.command("棋类日报", alias={"棋类周报", "棋类战绩日报", "棋类战绩周报"})' in main
    assert "self._board_report_poster(data)" in main
    assert "def _board_report_data(" in main
    assert "_BOARD_REPORT_TMPL" in main
    assert "棋类对局 · 战绩报告" in main
    assert "def _board_report_poster_pillow" in main


def test_room_activity_toasts_are_compact_and_throttled() -> None:
    """房间动态提醒：细条浮层（鼠标穿透、长昵称省略）+ 同一访客 CD，避免刷屏。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="roomActivity"' in page
    assert ".room-activity {" in style
    # 浮层不能吃掉棋盘的点击
    activity_block = style[style.index(".room-activity {") : style.index(".room-activity-item {")]
    assert "pointer-events: none;" in activity_block
    # 长昵称自动省略
    assert ".room-activity-text strong" in style
    assert "text-overflow: ellipsis;" in style[style.index(".room-activity-text strong") :]

    # 五类事件都在（含离线回来的「回到了房间」）
    for kind in ('"join"', '"leave"', '"return"', '"seat-in"', '"seat-out"'):
        assert kind in script
    # 同一访客同类动作有 CD，且轮询后做 diff
    assert "ACTIVITY_COOLDOWN_MS = 15000" in script
    assert "activityAllowed(" in script
    load_state = script[script.index("async function loadState()") :]
    assert "handleRosterActivity(" in load_state
    # 不提醒自己与花火
    assert "key === selfKey || visitor.is_ai" in script

    # 服务端下发开关，前端按配置过滤（总开关 / 观众进出 / 冷却）
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    ui = schema["web_ui"]["items"]
    assert ui["activity_toast_enabled"]["default"] is True
    assert ui["activity_toast_spectators"]["default"] is True
    assert ui["activity_toast_cooldown_seconds"]["default"] == 15
    assert "_activity_toast_snapshot" in server
    assert '"web_ui.activity_toast_spectators"' in server
    assert 'snapshot["activity_toast"]' in server
    assert "config.enabled === false" in script
    assert "config.spectators !== false" in script

    # 窄屏贴屏幕正中下方悬浮，聊天页抬到输入框上方
    mobile = style[style.index("@media (max-width: 1180px)", style.index(".room-activity {")) :]
    assert "left: 50%;" in mobile
    assert "env(safe-area-inset-bottom, 0px)" in mobile
    assert "body.room-view-chat .room-activity" in mobile
    assert 'classList.toggle("room-view-chat"' in script


def test_room_activity_uses_stable_visitor_uid() -> None:
    """名册差量按服务端下发的 uid 认人：座位重编号 / 号码撞号都不会误报进出。"""
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "def _visitor_uid(" in models
    assert '"uid": _visitor_uid(self.token)' in models
    assert '"visitor_uid": _visitor_uid(visitor.token) if visitor else ""' in models
    assert "function rosterKey(" in script
    assert "rosterKey(visitor)" in script
    # 不能再拿 number 当身份
    assert "previousMap.get(number)" not in script
    assert "String(room?.visitor_uid || \"\")" in script


def test_board_series_waits_for_idle_and_flushes_on_seat_leave() -> None:
    """连战汇总：状态驱动（离席/离线立即发，兜底上限），真人退席立即发送。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    # 兜底上限默认 30 分钟（1800s）
    assert (
        schema["board"]["items"]["group_announce_series_idle_seconds"]["default"] == 1800
    )
    # 「玩家在房间内就攒着」默认开
    assert schema["board"]["items"]["group_announce_wait_while_active"]["default"] is True
    assert "board_group_announce_series_idle" in main
    assert "board_group_announce_wait_while_active" in main
    assert "def _board_series_delay(" in main
    assert "def _flush_board_series_when_seats_empty(" in main
    assert "def _board_humans_all_gone(" in main
    assert "def _board_hold_until_humans_gone(" in main
    assert "self._flush_board_series_when_seats_empty(room)" in main
    # 防抖：新对局要把旧的等待任务取消重排
    assert "existing.cancel()" in main
    # 离线判定阈值放宽到 3~5 分钟（240s）
    assert "BOARD_SERIES_OFFLINE_SECONDS = 240" in main


def test_duel_side_rotation_is_relative_and_configurable() -> None:
    """再来一局换先：就地互换（不是固定偏移），并可通过配置固定执色。"""
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert schema["multiplayer"]["items"]["duel_side_rotation"]["default"] is True
    assert 'self._cfg_bool(\n            "multiplayer.duel_side_rotation", True' in main
    assert "duel_side_rotation=self.duel_side_rotation" in main
    assert "def _prepare_duel_sides(" in manager
    assert "def _swap_duel_sides(" in manager
    assert "duel_seat_signature" in manager
    # 不能再靠「固定偏移」换先，否则每局算出来都一样
    assert "_assign_duel_sides(room, rotate=True)" not in manager


def test_side_rotation_override_is_ai_only_and_wired_to_ui() -> None:
    """房间内换边设置：仅人机对局可改，服务端下发状态，前端有独立开关。"""
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "ai_side_rotation: bool | None = None" in models
    assert "def _ai_side_rotation_active(" in manager
    assert "def side_rotation_state(" in manager
    assert "def player_set_side_rotation(" in manager
    # 真人对局里房间级设置必须被忽略
    assert "not self._duel_human_pair(room)" in manager
    assert "/api/room/{access_token}/side-rotation" in server
    assert 'snapshot["side_rotation"] = self.manager.side_rotation_state(' in server
    # 所有返回房间的接口都走同一个 helper，避免按钮状态闪一下
    assert "def _room_payload(" in server

    assert 'id="rematchChoice"' in page
    assert 'data-rotation="auto"' in page and 'data-rotation="fixed"' in page
    assert "function setSideRotation(" in script
    assert (
        'request("POST", "side-rotation", {' in script
    )


def test_side_banner_shows_color_at_game_start() -> None:
    """开局换边提醒：棋盘上方一条醒目胶囊，鼠标穿透、每局只弹一次。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="sideBanner"' in page
    assert 'id="sideBannerStone"' in page
    block = style[style.index(".side-banner {") : style.index(".side-banner[hidden]")]
    assert "position: absolute;" in block
    assert "pointer-events: none;" in block
    assert "transform: translate(-50%, 0)" in style

    assert "function showSideBanner(" in script
    assert "function maybeShowSideBanner(" in script
    assert "SIDE_BANNER_MS" in script
    assert "sideBannerPrimed" in script
    assert "你执${sideLabel(side)} · ${first ? \"先行\" : \"后行\"}" in script
    render = script[script.index("function render() {"):]
    assert "maybeShowSideBanner();" in render


def test_activity_leave_uses_explicit_left_at() -> None:
    """「离开房间」只认服务端下发的 left_at，心跳超时不再误报离开/回来。"""
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert '"left_at": float(self.left_at or 0.0),' in models
    collect = script[script.index("function collectRosterEvents("):]
    collect = collect[: collect.index("function handleRosterActivity(")]
    assert "Number(visitor.left_at) > 0" in collect
    assert "Number(before.left_at) > 0" in collect


def test_ai_think_delay_is_configurable() -> None:
    """花火整体思考延迟：一个配置项，难度只做小幅增减。"""
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert schema["game"]["items"]["ai_think_delay_seconds"]["default"] == 0.8
    assert "def bot_think_delay(" in manager
    assert "def _spawn_bot_turn(" in manager
    assert "def wait_bot_turns(" in manager
    assert 'self._cfg("game.ai_think_delay_seconds", 0.8)' in main
    assert "ai_think_delay=self.ai_think_delay" in main
    # 棋类 / 贪心骰子 / 二十一点庄家的 AI 回合一律后台执行（spawn），
    # 玩家不用等「花火想完 / 掷完 / 补完牌」才看到自己的操作结果，
    # 前端靠轮询逐帧看到 AI 的每一步（掷骰、补牌），观感更接近真人节奏。
    assert manager.count("await self._bot_turn(room)") == 0
    assert manager.count("await self._blackjack_dealer_turn(room)") == 0
    assert manager.count("self._spawn_blackjack_dealer_turn(room)") >= 1


def test_room_page_does_not_depend_on_seat_visitor_token() -> None:
    """座位快照只下发 uid：前端不能再用 player_seats[].visitor_token 判身份。"""
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert '"visitor_token": seat.visitor_token' not in models
    assert '"uid": _visitor_uid(seat.visitor_token)' in models
    assert "seat.visitor_token" not in script
    assert "s.visitor_token" not in script
    assert "function isMySeat(" in script
    # 比对的是自己的 room.visitor_uid
    assert "String(target?.visitor_uid || \"\")" in script


def test_announce_and_resign_endpoints_require_identity() -> None:
    """通知到群/复盘/认输都必须校验访客身份，且图片要过白名单。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "def require_seated_player(" in manager
    assert "self.require_seated_player(room, visitor_token)" in manager  # resign
    # 两处 announce 都要 seated 校验，两处 recap 至少要求是房间访客
    announce_block = server[
        server.index("async def _undercover_announce(") : server.index(
            "async def _board_recap("
        )
    ]
    assert announce_block.count("require_seated_player") == 2
    recap_block = server[
        server.index("async def _undercover_recap(") : server.index(
            "async def _undercover_announce("
        )
    ]
    assert "require_visitor(" in recap_block
    board_recap = server[
        server.index("async def _board_recap(") : server.index(
            "async def _seat_leave("
        )
    ]
    assert "require_visitor(" in board_recap
    # 前端必须带上令牌，否则会被 403 挡掉
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for action in ("undercover/recap", "board/recap", "undercover/announce", "board/announce"):
        call = script[script.index(f'"{action}"') :][:120]
        assert "visitor_token: visitorToken" in call

    assert "def _image_payload(" in server
    assert "_image_payload(payload)" in server
    assert "def _forbidden(" in server


def test_frontend_recovers_from_transient_errors() -> None:
    """轮询失败只弹临时遮罩，恢复后必须撤掉；请求要有超时；渲染异常不能锁死按钮。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function showTransientOverlay(" in script
    assert "function clearTransientOverlay(" in script
    assert 'overlay.dataset.transient = "1"' in script
    assert "clearTransientOverlay();" in script
    # 不在 catch 里直接写 hidden = false
    assert 'document.getElementById("boardOverlay").hidden = false;\n      pollTimer' not in script

    assert "async function timedFetch(" in script
    assert "AbortController" in script
    assert 'data?.status === "error"' in script

    # render 包一层 try，renderInner 才是真正实现
    assert "function renderInner()" in script
    render_block = script[script.index("  function render() {") :][:420]
    assert "try {" in render_block and "renderInner();" in render_block


def test_vote_card_nickname_is_html_escaped() -> None:
    """昵称会先被 sanitizeDisplayText 解码实体，拼进 innerHTML 前必须再转义。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function escapeHtml(" in script
    card = script[script.index('card.className = "uc-vote-card"') :][:1200]
    assert "escapeHtml(sanitizeDisplayText(p.display_name" in card


def test_poll_recovers_from_pruned_visitor() -> None:
    """访客被回收后旧令牌失效：前端要自动重新入房，而不是一直显示「房间不可用」。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    poll = script[script.index("async function poll() {") :]
    poll = poll[: poll.index("async function loadState()")]
    assert 'message.includes("访客身份")' in poll
    assert "await join();" in poll


def test_browser_tab_title_reminds_my_turn() -> None:
    """标签页标题提醒：聚合各玩法「轮到我」状态，render 末尾更新 document.title。"""
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function computeMyTurn()" in script
    assert "function updateDocumentTitle()" in script
    # 轮到我时标题带「轮到你」，未轮到时用静态「花火陪你玩」
    assert "🔔 轮到你" in script
    assert "花火陪你玩 · ${gameLabel()}" in script
    # 覆盖主要玩法的「轮到我」判定
    assert "isDuelGame(room)" in script  # 棋类对弈（turn === 我执方）
    assert "game.turn === mySide()" in script  # 象棋
    assert 'game.turn === "human"' in script  # 贪心骰子
    assert "room.is_current_player" in script  # 二十一点 / 海龟汤
    assert "ucPrevMyTurn" in script  # 卧底发言轮
    # render 末尾更新标题
    assert "updateDocumentTitle();" in script


def test_page_api_handlers_read_body_from_request_not_signature() -> None:
    """AstrBot 的 register_web_api 不向 handler 传参，POST handler 必须自己读 request.json。

    曾因词库 4 个 handler 写成 `(self, payload=None)`，payload 恒为 None，
    导致批量添加/新增/删除/AI 生成全部拿不到请求体（返回 0 条却不报错）。
    """
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "payload: Any = None" not in main
    assert main.count("await request.json(default={}) or {}") >= 11
    # 词库 4 个写接口都要按此方式取请求体
    for name in (
        "page_undercover_words_add",
        "page_undercover_words_delete",
        "page_undercover_words_batch_import",
        "page_undercover_words_llm_generate_batch",
    ):
        assert f"async def {name}(self) -> dict[str, Any]:" in main


def test_stats_poster_rows_start_below_header() -> None:
    """战绩海报的数据行必须从表头下方开始，否则第一名会压在表头上。"""
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    # 表头高度已计入画布总高，数据行起点也要带上它
    assert "const height = padTop + headerH + nRows * rowH + footerH;" in script
    assert "const rowTop = padTop + headerH;" in script
    assert "const y = rowTop + i * rowH;" in script
    # 旧写法（行起点等于表头起点）不能再出现
    assert "const y = padTop + i * rowH;" not in script


def test_manager_game_labels_cover_all_supported_games() -> None:
    """管理台玩法中文名必须覆盖全部支持玩法（曾漏 weiqi 导致海报显示英文键名）。"""
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")

    block = script.split("const GAME_LABELS = {", 1)[1].split("};", 1)[0]
    for game in (
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
    ):
        assert f"{game}:" in block, f"管理台缺少 {game} 的中文名"
    assert 'weiqi: "围棋"' in block
    # 后端 SUPPORTED_GAMES 里的每个玩法都要有中文名
    for game in ("gomoku", "xiangqi", "weiqi", "tictactoe", "othello"):
        assert f'"{game}"' in models


def test_game_vote_wiring_is_complete() -> None:
    """房间内投票换游戏：配置项、路由、快照字段、前端面板必须齐全。"""
    import json

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["multiplayer"]["items"]
    for key in (
        "game_vote_enabled",
        "game_vote_expiry_seconds",
        "game_vote_cooldown_seconds",
        "game_vote_allow_while_playing",
    ):
        assert key in items, f"缺少配置项 {key}"
    # 对局中发起默认关闭
    assert items["game_vote_allow_while_playing"]["default"] is False

    server = (ROOT / "server.py").read_text(encoding="utf-8")
    for path, handler in (
        ("game/vote/propose", "_game_vote_propose"),
        ("game/vote/cast", "_game_vote_cast"),
        ("game/vote/cancel", "_game_vote_cancel"),
    ):
        assert f'"/api/room/{{access_token}}/{path}"' in server
        assert f"async def {handler}(" in server
    assert "switchable_games=self.manager.switchable_games(room)" in server
    assert 'snapshot["game_vote_enabled"]' in server

    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    for name in (
        "def switchable_games(",
        "def game_vote_required(",
        "def prune_game_vote(",
        "async def propose_game_vote(",
        "async def cast_game_vote(",
        "async def cancel_game_vote(",
    ):
        assert name in manager
    # 看门狗顺带清过期提案
    assert "self.prune_game_vote(room, current)" in manager

    models = (ROOT / "models.py").read_text(encoding="utf-8")
    assert "class GameVoteRequest" in models
    assert '"switchable_games": list(switchable_games or [])' in models
    assert '"game_vote": self._game_vote_snapshot(visitor_token)' in models

    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "function renderGameVote()" in script
    assert "renderGameVote();" in script
    for action in (
        'request("POST", "game/vote/propose"',
        'request("POST", "game/vote/cast"',
        'request("POST", "game/vote/cancel"',
    ):
        assert action in script

    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="gameVotePanel"' in page
    assert 'id="gameVoteTarget"' in page


def test_board_poster_renders_loser_on_its_own_line() -> None:
    """战报海报：败方必须另起一行渲染（HTML 与 Pillow 两条路径都要有）。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    # 数据层：sub 只放胜方信息，败方单独走 sub2
    assert '"sub2": loser_line' in main
    assert 'loser_line = f"败方：' in main
    # HTML/t2i 模板：第二行可选渲染
    assert '{% if sub2 %}<div class="sub2">{{ sub2 }}</div>{% endif %}' in main
    assert ".sub2 {" in main
    # Pillow 兜底：同样画第二行
    assert 'sub2 = str(data.get("sub2") or "")' in main


def test_series_report_names_the_loser() -> None:
    """连战播报每一局都要写清「谁胜谁负」，不能只写胜方。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    entry = main.split("def _board_announce_entry", 1)[1].split("def _queue_board_announce", 1)[0]
    assert '"loser_name"' in entry
    assert '"loser_side"' in entry

    # 单局胜负短句抽成了 _board_entry_result，连战列表与单局战报都复用它
    result = main.split("def _board_entry_result", 1)[1].split("def _board_series_text", 1)[0]
    assert "loser_name" in result
    assert "胜 {loser_text}" in result or "胜 {loser}" in result


def test_state_polling_supports_unchanged_rev() -> None:
    """轮询带指纹：内容没变只回小包，但时钟与「正在打字」标记必须照发。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "def _snapshot_rev(" in server
    assert 'if key not in ("server_time", "rev")' in server
    assert 'snapshot["rev"] = self._snapshot_rev(snapshot)' in server
    # 短路响应必须带 clock 与 typing 标记，否则倒计时会冻住、回复会慢一拍
    assert '"unchanged": True,' in server
    assert '"server_time": snapshot.get("server_time"),' in server
    assert '"bot_chat_typing": bool(snapshot.get("bot_chat_typing")),' in server
    # 前端：带上上一条指纹，并处理小包
    assert "&rev=${encodeURIComponent(String(room.rev))}" in script
    assert "if (data?.data?.unchanged) {" in script
    assert "function applyUnchangedState(" in script


def test_turtle_soup_upgrade_contracts() -> None:
    """海龟汤升级的三块关键链路不能只做后端或只做前端。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    # 路由：揭晓 / 通知 / 复盘
    for path in ("soup/reveal", "soup/announce", "soup/recap"):
        assert path in server
    # 结束流程必须冻结海龟汤快照，否则「再来一道」后播报会读到新题
    assert "self._snapshot_soup_result(room)" in manager
    # 超时兜底揭晓必须在 tick 里触发，而不是只有玩家手动点
    assert 'await self.force_reveal_soup(need_soup_reveal, reason="timeout")' in manager
    # 快照不含汤底
    assert '"solution"' not in manager.split("def _snapshot_soup_result", 1)[1].split(
        "def ", 1
    )[0]
    # 前端结算面板与海报
    assert 'id="soupResultPanel"' in page
    assert 'id="soupRevealButton"' in page
    assert "function soupSharePosterUrl(" in script
    assert "function renderSoupResult(" in script
    assert "renderSoupResult(game);" in script


def test_qq_bridge_contracts() -> None:
    """群聊入口与前端来源角标不能被顺手删掉。

    这三处是分开的：main 里注册 handler、qq_bridge 里解析与执行、前端加角标。
    少任何一处，「网页 ↔ QQ 互通」都会静默失效（消息不报错，但没人回）。
    """
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    bridge = (ROOT / "qq_bridge.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert "def soup_qq_command" in main_src
    assert "handle_soup_command(self, event)" in main_src
    # @花火 直接说话也要能当提问，否则群里必须先写「汤 提问」
    assert "def soup_qq_at_say" in main_src
    assert "handle_soup_at_message(self, event)" in main_src
    assert "classify_at_message" in bridge
    # LLM 工具也要能真的答题，而不是只回「请去网页」
    assert "run_soup_action(" in main_src
    assert "soup_room_for_session(self, session_id)" in main_src

    for word in ("提问", "推理", "提示", "揭晓", "附议", "反对", "状态", "查看"):
        assert word in bridge
    # 群消息必须带 source="qq"：轮次与身份校验靠它分流
    assert 'source="qq"' in bridge
    # 群内回显历史要防剧透
    assert "redact_spoiler" in bridge

    assert "soup-qq-badge" in script
    assert ".soup-qq-badge" in style


def test_qq_bridge_switches_and_start_command() -> None:
    """互通总开关 / 开局指令 / 提问上限 / 剩余信息不能被顺手删掉。"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    bridge = (ROOT / "qq_bridge.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    soup = schema["turtle_soup"]["items"]

    # 总开关：关掉后群里完全不参与（指令、@、模型工具三处都要查）
    assert soup["qq_bridge_enabled"]["default"] is True
    assert main_src.count("turtle_soup_qq_bridge") >= 3
    # 开局指令
    assert "async def start_soup_room" in bridge
    assert '"开局": "start"' in bridge
    # 提问上限：只限制提问，推理不受影响
    assert soup["max_questions"]["default"] == 15
    assert "def _soup_question_limit" in main_src
    assert '"limited": True' in main_src
    # 剩余信息：网页与群里共用同一组数字
    assert "def _soup_progress" in main_src
    assert "def _progress_tail" in bridge


def test_soup_sync_uses_image_and_forward_as_fallbacks() -> None:
    """网页提问同步到群：图片与合并聊天记录互为兜底，最后才发纯文本。"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert schema["turtle_soup"]["items"]["qq_sync_questions"]["default"] is True
    assert "_SOUP_SYNC_TMPL" in main_src
    assert "def _queue_soup_group_sync" in main_src
    assert "def _flush_soup_group_sync" in main_src
    assert "def _soup_sync_t2i" in main_src
    assert "def _soup_sync_chain" in main_src
    # 图片与合并记录都失败时必须还有纯文本兜底
    assert "await self._send_to_origin(room, text)" in main_src


def test_custom_soup_commands_are_private_only() -> None:
    """自定义题库：私聊投稿指令 + 出题优先级 + 管理台入口。"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    custom = (ROOT / "soup_custom.py").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")

    assert "def soup_custom_command" in main_src
    assert "if self._event_group_id(event):" in main_src
    assert "handle_custom_command(self, event.message_str" in main_src
    # 自定义题必须排在内置题库之前，否则投稿永远抽不到
    assert main_src.index("自定义题目（玩家私聊投稿）") < main_src.index("内置题库（soup_library.json）")
    assert "class CustomSoupStore" in custom
    # 管理台题库页
    assert "def page_soup_library" in main_src
    assert "def page_soup_library_reset" in main_src
    assert "def page_soup_custom_delete" in main_src
    assert 'data-panel="library"' in page
    assert "libraryPanel" in page
    assert '"soup_library"' in manager


def test_soup_library_is_shipped() -> None:
    """内置题库必须与代码一起在：只有代码没有数据，等于没做这个功能。"""
    data = json.loads((ROOT / "soup_library.json").read_text(encoding="utf-8"))
    assert len(data["items"]) >= 200
    assert data["source"]
    for item in data["items"][:20]:
        assert item["surface"] and item["solution"]
        assert item["content_level"] in {"all_ages", "normal", "unrestricted"}

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    soup = schema["turtle_soup"]["items"]
    assert soup["library_enabled"]["default"] is True
    assert soup["qq_at_question"]["default"] is True
    assert soup["qq_auto_bind"]["default"] is True
    # 轮次默认关掉：群里人多时按顺位轮转，一轮要等二十次
    assert soup["free_questioning"]["default"] is True



def test_announce_poster_fits_in_the_request_body_limit() -> None:
    """通知到群把战报图放进 JSON 请求体：图要压、上限要够。

    2MB 上限 + PNG 时，1080 宽的战报图会先撞 aiohttp 的
    「Maximum request body size 2097152 exceeded」，报错发生在路由之前，
    玩家只看到一句英文，还以为是自己没绑群。
    """
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "8 * 1024 * 1024" in server
    assert "soupSharePosterUrl(buildSoupResult(), { jpeg: true })" in script


def test_turtle_soup_rematch_returns_to_setup() -> None:
    """海龟汤的「再来一局」必须回到准备状态，否则玩家抢不到「我来出题」。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "prepare_soup_next_round" in server
    assert "async def prepare_soup_next_round" in manager


def test_soup_opening_card_reaches_the_group() -> None:
    """群里开的局要能直接看到题目：开局卡（t2i，失败回退文字）。

    以前题目就绪只发一条房间链接，群里的人不点进去就不知道在玩什么。
    """
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert "_SOUP_OPEN_TMPL" in main
    assert "async def _announce_soup_opening" in main
    assert "_announce_soup_opening(room)" in main
    assert schema["turtle_soup"]["items"]["qq_opening_card"]["default"] is True


def test_soup_sync_report_has_no_truncation_and_shows_real_avatars() -> None:
    """速报图：问句不许截断，头像用玩家真实 QQ 头像。

    以前问句按 60 字砍断，图上一大半问题只剩半句；头像只有号码牌。
    """
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "prompt[:60]" not in main
    assert "async def _soup_sync_avatars" in main
    assert "number_value" in main
    assert "qlogo" not in main  # 头像 URL 统一走 models._qq_avatar_url
    assert "群里 @花火 直接问一句" in main


def test_soup_at_message_handles_pure_action_phrases() -> None:
    """`@我 提示` 是动作而不是提问；群里也要能直接开局。"""
    bridge = (ROOT / "qq_bridge.py").read_text(encoding="utf-8")

    assert "def classify_at_intent" in bridge
    assert "classify_at_intent(text)" in bridge
    assert "start_soup_room" in bridge
    assert "@我 说「来局海龟汤」" in bridge


def test_soup_announce_requires_a_target_group() -> None:
    """发不出去要先说清原因，别让玩家以为点了没反应。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    # 「没绑群」和「找不到 QQ 适配器」是两种原因，前端要能分开提示
    assert server.count("announce_problem") >= 3
    assert "def announce_target" in main
    assert "def announce_problem" in main


def test_announce_resolves_the_real_platform_id() -> None:
    """平台 id 不能写死 `aiocqhttp`。

    AstrBot 按 `platform.meta().id` 找适配器，`aiocqhttp` 只是适配器类型名
    (`meta().name`)。用户改过适配器 id 时，写死的会话串会让
    `context.send_message` 直接返回 False 并只打一行 warning
    （`cannot find platform for session ...`），管理台开台通知就是这么丢的。
    """
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def _resolve_platform_id" in main
    assert "def _session_with_platform" in main
    assert "def _deliver" in main
    # 所有主动发送都走统一出口，避免又冒出直接拼会话串的调用
    assert "await self.context.send_message(" in main
    assert main.count("context.send_message(") <= 2


def test_turtle_soup_prefetches_the_next_puzzle() -> None:
    """预生成：开局出好题后，后台顺手备好下一题，下一局直接命中存档。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert "async def _soup_prefetch(" in main
    assert "def _schedule_soup_prefetch(" in main
    assert "_prepare_turtle_soup_task" in main
    soup = schema["turtle_soup"]["items"]
    # 预生成只留一个「备用题数量」，0 即关闭：两个开关容易互相打架
    assert soup["prefetch_target"]["default"] == 1
    assert "prefetch_generated" not in soup
    assert "generated_first" in soup["library_priority"]["options"]


def test_soup_difficulty_and_library_admin_are_wired() -> None:
    """房间内改难度、管理台题库管理都要真的接上路由与按钮。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")

    assert "/soup/difficulty" in server
    for name in (
        "page_soup_library_action",
        "page_soup_reveal",
        "page_soup_custom_update",
    ):
        assert name in main
    assert 'id="soupDifficultyPicker"' in (
        ROOT / "web" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="builtinBody"' in page
    assert 'id="soupEditorDialog"' in page
    assert "mask_solution" in main
    assert "soup_library_action" in script
    # 难度行里的「提示 N」必须跟对局实际可用条数一致，否则同一屏会出现
    # 「提示 15」和计数「0 / 3」两套数字。
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "game?.hint_limit" in app


def test_soup_source_and_generated_library_are_wired() -> None:
    """题目出处、生成题库与管理台页签都要真的接上。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    manager_page = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(
        encoding="utf-8"
    )
    manager_script = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(
        encoding="utf-8"
    )
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    # 网页展示「题目来源：…」
    assert 'id="soupSource"' in page
    assert "题目来源：" in app
    assert "puzzle_source_label" in app

    # 对局中锁难度，不许中途换档
    assert "difficultyLocked" in app
    assert '{"active", "paused"}' in (
        ROOT / "room_manager.py"
    ).read_text(encoding="utf-8")

    # 管理台：三类题库用页签分开，模型出的题单独一页
    assert 'data-lib-tab="generated"' in manager_page
    assert 'id="generatedBody"' in manager_page
    assert "renderGeneratedTable" in manager_script
    assert "function soupLibraryAction(kind" in manager_script
    assert '"page/soup_library_action"' in manager_script

    # 后端：生成题库落盘、出题时复用
    assert (ROOT / "soup_generated.py").exists()
    assert "def _soup_generated" in main
    assert 'source="generated"' in main


def test_soup_timeline_preview_and_full_sheet() -> None:
    """时间线：默认只留最近几条，翻旧账走「查看全部」弹层，不再套娃滚动。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    # 主时间线只放最近几条，其余进弹层
    assert "SOUP_HISTORY_PREVIEW = 5" in app
    assert "soupHistoryAll" in page
    assert "soupHistorySheet" in page
    assert "function openSoupSheet" in app
    assert "function closeSoupSheet" in app
    # 高度不再钉死 330px，而是跟随视口；手机上取消内部滚动
    assert "max-height: clamp(" in css
    assert "max-height: 330px" not in css
    assert "overflow: visible" in css


def test_soup_timeline_shows_player_avatar_and_name() -> None:
    """时间线带上玩家头像与昵称：不再只有「1 号 / 2 号」认人。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function soupEntryNode" in app
    assert "ucSeatAvatarNode" in app
    assert "soup-entry-avatar" in app
    assert "function soupSeatLabel" in app
    # 提示 / 花火自己问的没有座位号，用金色小人兜底
    assert "soup-avatar-bot" in app


def test_soup_composer_inline_input() -> None:
    """常驻输入条：提问/推理二选一，直接提交，不必切到「聊天」页。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="soupComposer"' in page
    assert 'data-soup-mode="answer"' in page
    assert "function submitSoupComposer" in app
    assert "soup/question" in app
    assert "soup/answer" in app


def test_soup_direct_command_and_sync_style() -> None:
    """一句「汤」直接开局，同步到群默认纯文字、可切横条图。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    bridge = (ROOT / "qq_bridge.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    # AstrBot 指令页可见「汤」指令，解析层把单独「汤」当开局
    assert '@filter.command("汤"' in main
    assert 'return ("start", "")' in bridge
    # 同步形态：默认纯文字，可选横条图
    style = schema["turtle_soup"]["items"]["qq_sync_style"]
    assert style["default"] == "text"
    assert "image" in style["options"]
    assert "def _soup_sync_text" in main
    assert "def _flush_soup_sync_image" in main


def test_soup_sync_strip_and_result_link() -> None:
    """速报改成横条图消除底部空白；战报附房间链接；推理条目有独立样式。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    # 横条图：压扁、缩短、更清晰，用 viewport_width/viewport_height 消除大片空白
    assert "viewport_width=440" in main
    assert "viewport_height=60" in main
    assert 'scale="high"' in main
    # 判定简写：提问只给「是 / 否 / 无关」
    assert "verdict_label" in main
    # 战报末尾附房间链接 + 网页查看引导
    assert "房间链接：{url}" in main
    assert "想看完整汤底？点上面的链接进网页房间查看" in main
    # 前端：推理条目给独立样式（徽章 + 紫色左边框），发送按钮白字
    assert "soup-kind-badge" in app
    assert ".soup-entry.answer" in css
    assert "color: #ffffff" in css


def test_soup_reveal_vote_syncs_to_group() -> None:
    """网页发起揭晓/投票要同步到群，群友才知道能附议。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    rm = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "soup_reveal_requested" in main
    assert "soup_reveal_voted" in main
    assert "def _announce_soup_reveal_vote" in main
    # payload 带上 actor / agree，群里才知道谁在表决
    assert '"actor"' in rm
    assert '"agree"' in rm
    # 群里附议/反对的指引
    assert "汤 附议" in main


def test_soup_announce_image_keeps_room_link() -> None:
    """图片战报（只发图不发正文）也要带上房间链接引导。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def _soup_room_link_tail" in main
    assert "recap_with_link" in main


def test_soup_hint_syncs_and_reveal_solution_gated() -> None:
    """网页提示同步到群；汤底只在群内参与揭晓时发，其余路径也要有通知。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    room_manager = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    # 提示跟着问答同步（kind 纳入 hint，正文取 response）
    assert '"hint"' in main
    assert "申请了提示" in main
    # 揭晓汤底：按「本局群内参与过」判定，而不是最后一次操作者来源
    assert "def _announce_soup_reveal" in main
    assert "qq_participated" in main
    assert "with_solution=qq_participated" in main
    # 猜中、额度用尽、超时三条路径都要发揭晓事件，群里不能静默
    assert '"reason": "solved" if solved else "answer_limit"' in room_manager
    assert '"source": "system"' in room_manager
    assert "qq_reveal_interest" in room_manager
    # 收尾那次推理也要进群同步队列
    assert '"new_facts": 0 if solved or auto_reveal else len(newly_discovered)' in room_manager
    # 合成「结束」条目不再同步成「花火推理」
    assert "entry.verdict is None" in main
    # 夜间模式「提问」发送按钮背景改暗（不再用亮金色 var(--gold)）
    assert "body.dark .soup-composer-send" in css
    assert "background: #6b5a26" in css


def test_board_series_single_game_not_labelled_streak() -> None:
    """棋类连战：单局 flush 不写成「连战 1 局」，按普通单局格式发。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def _board_entry_result" in main
    assert "len(entries) == 1" in main


def test_board_result_snapshot_survives_seat_vacation() -> None:
    """玩家都退席后，新观众要靠终局快照看到真实胜者，不能再误判成「花火获胜」。"""
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert '"board_result"' in models
    assert '"winner_name"' in models
    assert '"loser_name"' in models
    # 前端结算与状态栏在查不到座位时读快照胜者
    assert "boardResult && boardResult.winner_name" in app
    assert "room.board_result?.winner_name" in app


def test_board_share_poster_shows_winner_and_loser() -> None:
    """前端分享战报要写出胜者与败者，不能再只有「你赢了」三个字。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "loser_name" in app
    assert "loser_side" in app
    assert "败方：" in app
    assert "result.winner_name ? `${result.winner_name} 获胜`" in app


def test_turtle_soup_emoji_uses_bowl_not_turtle() -> None:
    """海龟汤战报用 🥣，不再用乌龟 🐢。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "🥣 海龟汤" in main
    assert "🥣 海龟汤" in app
    assert "🐢" not in main
    assert "🐢" not in app


def test_soup_report_dedupes_identical_questions() -> None:
    """战报「他们问过」按去空格去标点的正文去重，同一问题不重复出现。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "seen" in main
    assert 'str.maketrans("", "", "？?！!，,。.、：:")' in main
    assert "seen.has(key)" in app


def test_board_series_flushes_old_game_on_switch() -> None:
    """换游戏时旧玩法连战先发出，新玩法从第一局重新起算。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    rm = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "def _flush_board_series_on_game_switch" in main
    assert "await self._flush_board_series_on_game_switch" in main
    # 连战文案的玩法名取条目自带的 game_type（换游戏后 room.game_type 已是新玩法）
    assert 'entries[0].get("game_type")' in main
    # 换玩法清掉旧终局快照
    assert "room.last_board_result = None" in rm
    assert "room.last_soup_result = None" in rm


def test_game_switch_resets_frontend_residual_state() -> None:
    """换玩法后前端清掉上一款游戏的结算弹窗与结果快照，不残留。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'boardResultShownKey = ""' in app
    assert "boardLastResult = null" in app
    assert 'soupResultShownKey = ""' in app


def test_game_stage_visibility_runs_on_every_render() -> None:
    """换游戏（投票/管理台/群指令）后棋盘要跟着新玩法切 stage，不能残留上一款棋盘。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function applyGameStageVisibility" in app
    # renderInner 每次渲染都重切 stage 可见性（不只在 game_type 变化时）
    body = app.split("function renderInner()", 1)[1].split("function renderSeat", 1)[0]
    assert "applyGameStageVisibility();" in body
    # 非棋类玩法不画棋盘
    assert '["turtle_soup", "pig_dice", "draw_guess", "blackjack", "undercover"].includes(room?.game_type)' in app


def test_blackjack_auto_stand_tolerates_short_heartbeat_gaps() -> None:
    """二十一点：主动离席立即托管，短暂心跳中断不抢回合，长时间断网才自动停牌。"""
    rm = (ROOT / "room_manager.py").read_text(encoding="utf-8")

    assert "offline = not previous.connected" in rm
    assert "BLACKJACK_OFFLINE_SECONDS = 120" in rm
    assert "heartbeat_gap >= self.BLACKJACK_OFFLINE_SECONDS" in rm
    # 15 秒抢回合的判定不再回来（仅海龟汤换人仍用 15s，属另一场景）
    assert "current - visitor.last_seen_at < 15" not in rm.split("def _advance_blackjack_turn", 1)[1].split("async def _blackjack_dealer_turn", 1)[0]


def test_chat_bubbles_distinguish_mine_other_and_bot() -> None:
    """聊天气泡：自己右侧、他人与花火左侧；三种气泡颜色。"""
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    # 消息带上发送者 uid，前端据此判「我 / 别人」
    assert '"sender_uid": _visitor_uid(visitor.token)' in models
    assert "sender_uid === room.visitor_uid" in app
    assert " mine" in app and " other" in app
    # 三种气泡底色
    assert "--chat-user-bg" in css and "--chat-other-bg" in css and "--chat-bot-bg" in css
    assert ".message.user.mine" in css and ".message.user.other" in css


def test_board_announce_names_loser_in_fallback() -> None:
    """棋类通报单人局兜底也要写清胜方与败方（玩家 vs 花火）。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "花火落败" in main
    assert "玩家落败" in main


def test_undercover_afk_autopilot_and_name_layout() -> None:
    """谁是卧底：在线挂机也交 LLM 托管；战报玩家名「1号 李思」带空格且逐行。"""
    rm = (ROOT / "room_manager.py").read_text(encoding="utf-8")
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    # 挂机托管：按 last_action_at 判断「在线但长时间无操作」
    assert "UNDERCOVER_AFK_SECONDS" in rm
    assert "now - last >= self.UNDERCOVER_AFK_SECONDS" in rm
    # 发言/投票刷新挂机判定
    assert "self._touch_seat_action(room, visitor.token)" in rm
    # 战报名「号」后带空格，不再「1号李思」
    assert 'f"{num}号" + (f" {name}" if name else "")' in main
    # 玩家逐行，不再顿号挤在一行
    assert '"\\n".join(winners)' in main
