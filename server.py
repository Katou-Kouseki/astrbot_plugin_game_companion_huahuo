from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from astrbot.api import logger

from .models import GameRoom, soup_reveal_required
from .room_manager import RoomManager


class GameRoomServer:
    """Serve the mobile game UI without sharing AstrBot's dashboard port."""

    ASSETS = {"index.html", "app.css", "app.js", "lucide.min.js", "background.webp"}
    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}")
    TRUSTED_BROWSER_COOKIE = "game_companion_device"

    def __init__(
        self,
        plugin: Any,
        manager: RoomManager,
        *,
        host: str,
        port: int,
        web_root: Path,
    ) -> None:
        self.plugin = plugin
        self.manager = manager
        self.host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self.requested_port = max(1, min(int(port), 65535))
        self.port = self.requested_port
        self.web_root = Path(web_root)
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and self._site is not None

    @property
    def local_base_url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::", "[::]"} else self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        """Start on the configured port or one of the next ten ports."""
        if self.running:
            return
        missing = [name for name in self.ASSETS if not (self.web_root / name).is_file()]
        if missing:
            raise RuntimeError("游戏页面静态资源不完整：" + "、".join(sorted(missing)))
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        last_error: OSError | None = None
        for candidate in range(
            self.requested_port, min(65535, self.requested_port + 10) + 1
        ):
            site = web.TCPSite(self._runner, self.host, candidate)
            try:
                await site.start()
            except OSError as exc:
                last_error = exc
                continue
            self._site = site
            self.port = candidate
            return
        await self.stop()
        raise RuntimeError(
            f"无法监听游戏端口 {self.requested_port}-{min(65535, self.requested_port + 10)}：{last_error}"
        )

    def _build_app(self) -> web.Application:
        """Build the room application for the real server and isolated tests."""
        app = web.Application(
            # 通知到群的海报图以 base64 放进 JSON 请求体（PNG 越漂亮越大，
            # base64 再膨胀 1/3）。给到 8MB：2MB 时 1080 宽的战报图会直接撞
            # 「Maximum request body size exceeded」，而报错发生在路由之前，
            # 玩家只会看到一句看不懂的英文。
            client_max_size=8 * 1024 * 1024,
            middlewares=[self._error_middleware],
        )
        app.router.add_get("/", self._serve_index)
        app.router.add_get("/room/{access_token}", self._serve_index)
        app.router.add_get("/assets/{name}", self._serve_asset)
        app.router.add_get("/health", self._health)
        app.router.add_post("/api/room/{access_token}/join", self._join)
        app.router.add_get("/api/room/{access_token}/state", self._state)
        app.router.add_post(
            "/api/room/{access_token}/identity/forget", self._forget_identity
        )
        app.router.add_post("/api/room/{access_token}/claim", self._claim)
        app.router.add_post("/api/room/{access_token}/start", self._start_game)
        app.router.add_post("/api/room/{access_token}/move", self._move)
        # 围棋停一手：双方连续停手即按数子法结算
        app.router.add_post("/api/room/{access_token}/weiqi/pass", self._weiqi_pass)
        app.router.add_post("/api/room/{access_token}/weiqi/resign", self._weiqi_resign)
        app.router.add_post("/api/room/{access_token}/weiqi/board", self._weiqi_board)
        app.router.add_post("/api/room/{access_token}/dice/action", self._dice_action)
        app.router.add_post(
            "/api/room/{access_token}/blackjack/action", self._blackjack_action
        )
        app.router.add_post(
            "/api/room/{access_token}/draw/strokes", self._draw_strokes
        )
        app.router.add_post(
            "/api/room/{access_token}/draw/guess", self._draw_guess
        )
        app.router.add_post(
            "/api/room/{access_token}/soup/question", self._soup_question
        )
        app.router.add_post("/api/room/{access_token}/soup/answer", self._soup_answer)
        app.router.add_post("/api/room/{access_token}/soup/hint", self._soup_hint)
        app.router.add_post("/api/room/{access_token}/soup/reverse", self._soup_reverse)
        app.router.add_post("/api/room/{access_token}/soup/correct", self._soup_correct)
        app.router.add_post("/api/room/{access_token}/soup/reveal", self._soup_reveal)
        app.router.add_post("/api/room/{access_token}/soup/host", self._soup_host)
        app.router.add_post("/api/room/{access_token}/soup/judge", self._soup_judge)
        app.router.add_post(
            "/api/room/{access_token}/soup/difficulty", self._soup_difficulty
        )
        app.router.add_post("/api/room/{access_token}/soup/announce", self._soup_announce)
        app.router.add_post("/api/room/{access_token}/soup/recap", self._soup_recap)
        app.router.add_post(
            "/api/room/{access_token}/undercover/speech", self._undercover_speech
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/vote", self._undercover_vote
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/pk", self._undercover_pk
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/camp_scales",
            self._undercover_set_camp_scales,
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/reveal",
            self._undercover_set_reveal_identity,
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/recap",
            self._undercover_recap,
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/announce",
            self._undercover_announce,
        )
        app.router.add_post(
            "/api/room/{access_token}/board/announce",
            self._board_announce,
        )
        app.router.add_post(
            "/api/room/{access_token}/board/recap",
            self._board_recap,
        )
        app.router.add_post("/api/room/{access_token}/ready", self._ready)
        app.router.add_post(
            "/api/room/{access_token}/undercover/add_ai",
            self._undercover_add_ai_seat,
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/remove_ai",
            self._undercover_remove_ai_seat,
        )
        app.router.add_post(
            "/api/room/{access_token}/undercover/batch_words",
            self._undercover_batch_words,
        )
        app.router.add_post(
            "/api/room/{access_token}/seat/swap/request", self._seat_swap_request
        )
        app.router.add_post(
            "/api/room/{access_token}/seat/swap/respond", self._seat_swap_respond
        )
        app.router.add_post("/api/room/{access_token}/seat/leave", self._seat_leave)
        # 两人对弈：和花火对局（add）/ 等对手来（remove）
        app.router.add_post("/api/room/{access_token}/seat/ai", self._seat_ai)
        # 棋类房间：玩家自选花火棋力（仅落座玩家、对手非真人）
        app.router.add_post(
            "/api/room/{access_token}/difficulty", self._set_difficulty
        )
        # 棋类房间：玩家自选「再来一局」换边还是保持执色（仅人机对局）
        app.router.add_post(
            "/api/room/{access_token}/side-rotation", self._set_side_rotation
        )
        app.router.add_post("/api/room/{access_token}/rematch", self._rematch)
        # 房间内投票换游戏：发起 / 投票 / 撤回
        app.router.add_post(
            "/api/room/{access_token}/game/vote/propose", self._game_vote_propose
        )
        app.router.add_post(
            "/api/room/{access_token}/game/vote/cast", self._game_vote_cast
        )
        app.router.add_post(
            "/api/room/{access_token}/game/vote/cancel", self._game_vote_cancel
        )
        app.router.add_post("/api/room/{access_token}/chat", self._chat)
        app.router.add_post("/api/room/{access_token}/leave", self._leave)
        # QQ 头像代理：qlogo 不带 CORS 头，Canvas 无法直接绘制；服务端拉取后补 ACAO
        app.router.add_get(
            "/api/room/{access_token}/avatar", self._avatar_proxy
        )
        return app

    async def stop(self) -> None:
        """Stop only the server instance owned by this plugin."""
        site, runner = self._site, self._runner
        self._site = None
        self._runner = None
        if site is not None:
            await site.stop()
        if runner is not None:
            await runner.cleanup()

    async def _serve_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            self.web_root / "index.html",
            headers=self._headers("text/html; charset=utf-8"),
        )

    async def _serve_asset(self, request: web.Request) -> web.FileResponse:
        name = str(request.match_info.get("name") or "")
        if name not in self.ASSETS - {"index.html"}:
            raise web.HTTPNotFound()
        path = self.web_root / name
        return web.FileResponse(
            path,
            headers=self._headers(
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            ),
        )

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "plugin": "astrbot_plugin_game_companion_huahuo",
                "rooms": len(self.manager.rooms),
            },
            headers=self._headers("application/json"),
        )

    async def _avatar_proxy(self, request: web.Request) -> web.StreamResponse:
        """QQ 头像代理：qlogo 即使带 Origin 也不返回 Access-Control-Allow-Origin，
        浏览器无法把跨域头像绘入 Canvas（分享/通知海报会画成首字占位）。

        这里由服务端拉取头像后补上 ACAO 头返回，前端以 same-origin 加载即可绘制。
        仅允许 q1/q2.qlogo.cn，且要求房间 access_token 有效，避免成为开放代理。
        """
        self._room(request)  # 无效/已关闭房间直接 404/410
        url = str(request.query.get("url") or "").strip()
        if not re.match(r"^https://q[12]\.qlogo\.cn/", url):
            raise web.HTTPBadRequest(text="头像地址不受支持")
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return web.Response(status=502, text="头像拉取失败")
                    data = await resp.read()
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
        except Exception as exc:
            logger.debug("[GameCompanion] QQ 头像代理拉取失败: %s", exc)
            return web.Response(status=502, text="头像拉取失败")
        return web.Response(
            body=data,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": ctype,
                "Cache-Control": "public, max-age=86400",
            },
        )

    async def _join(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        trusted_identity = await self._resolve_trusted_identity(request)
        visitor = await self.manager.join(
            room,
            str(payload.get("visitor_token") or ""),
            trusted_identity=trusted_identity,
        )
        return await self._identity_response(
            request,
            room,
            visitor,
            {
                "visitor_token": visitor.token,
            },
            remember=payload.get("remember_identity") is True,
            trusted_identity=trusted_identity,
        )

    async def _state(self, request: web.Request) -> web.Response:
        room = self._room(request)
        visitor_token = str(request.query.get("visitor_token") or "")
        visitor = await self.manager.heartbeat(room, visitor_token)
        remember = str(request.query.get("remember_identity") or "") == "1"
        client_rev = str(request.query.get("rev") or "")
        snapshot = self._room_payload(room, visitor.token)
        # 每秒一次的轮询里绝大部分时间内容根本没变：这时只回一个小包，
        # 把 20KB 的全量快照换成 ~200B（稳态流量约省 99%）。
        # 心跳已经在上面刷过了，所以「在线状态」不会因为走这条短路而停更。
        if (
            client_rev
            and client_rev == snapshot.get("rev")
            and not getattr(visitor, "trusted_browser_enrollment_pending", False)
        ):
            return self._response(
                {
                    "unchanged": True,
                    "rev": snapshot.get("rev"),
                    # 时钟和「正在打字」标记必须照发：前者用于倒计时校准，
                    # 后者决定前端是否把轮询提速到 300ms（聊天回复靠它推送）。
                    "server_time": snapshot.get("server_time"),
                    "bot_typing": bool(snapshot.get("bot_typing")),
                    "bot_chat_typing": bool(snapshot.get("bot_chat_typing")),
                }
            )
        return await self._identity_response(
            request,
            room,
            visitor,
            {},
            remember=remember,
            snapshot=snapshot,
        )

    async def _forget_identity(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.heartbeat(room, visitor_token)
        if payload.get("unbind") is True:
            # 解绑玩家：清空 QQ 身份绑定，回到绑定引导界面
            await self.manager.unbind_identity(room, visitor_token)
        raw_token = str(request.cookies.get(self.TRUSTED_BROWSER_COOKIE) or "")
        store = getattr(self.plugin, "trusted_identity_store", None)
        if store is not None and raw_token:
            await store.revoke_token(raw_token)
        snapshot = self._room_payload(room, visitor_token)
        snapshot.update(self._trusted_browser_snapshot(active=False, expires_at=0))
        response = self._response({"room": snapshot, "forgotten": True})
        response.del_cookie(
            self.TRUSTED_BROWSER_COOKIE,
            path=self._trusted_cookie_path(),
        )
        return response

    async def _claim(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.require_visitor_identity(room, visitor_token)
        result = await self.manager.claim_and_start(
            room,
            visitor_token,
            str(payload.get("side") or "human_black"),
            takeover=bool(payload.get("takeover")),
        )
        if result.get("need_confirm"):
            # 对手长时间没操作：先回给前端弹确认，这一步不做任何座位变更
            return self._response(result)
        self._log_room_op(room, "room_claim", "落座玩家席", visitor_token)
        return self._response(
            {"room": self._room_payload(room, visitor_token)}
        )

    async def _start_game(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.require_visitor_identity(room, visitor_token)
        await self.manager.start_game(
            room, visitor_token, str(payload.get("side") or "human_black")
        )
        self._log_room_op(room, "room_start", "开始对局", visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _move(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_move(
            room,
            visitor_token,
            row=int(payload.get("row", -1)),
            column=int(payload.get("column", -1)),
            from_row=int(payload.get("from_row", -1)),
            from_column=int(payload.get("from_column", -1)),
            to_row=int(payload.get("to_row", -1)),
            to_column=int(payload.get("to_column", -1)),
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _weiqi_pass(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_pass(room, visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _weiqi_resign(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            await self.manager.resign(room, visitor_token=visitor_token)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        self._log_room_op(room, "room_resign", "认输", visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _weiqi_board(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.set_weiqi_board(
            room,
            visitor_token,
            int(payload.get("size") or 0),
            None if payload.get("komi") in (None, "") else float(payload["komi"]),
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _rematch(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        if room.game_type == "turtle_soup":
            # 海龟汤不走「模型决定要不要再来一局」：直接退回准备状态，
            # 让玩家有机会点「我来出题」，或者等人齐了再开始。
            await self.manager.prepare_soup_next_round(room, visitor_token)
            self._log_room_op(
                room, "room_prepare_round", "海龟汤回到准备状态", visitor_token
            )
            return self._response(
                {"prepared": True, "room": self._room_payload(room, visitor_token)}
            )
        await self.manager.request_rematch(room, visitor_token)
        self._log_room_op(room, "room_rematch", "申请再来一局", visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _game_vote_propose(self, request: web.Request) -> web.Response:
        """房间内发起「换游戏」投票。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        game_type = str(payload.get("game_type") or "")
        try:
            await self.manager.propose_game_vote(room, visitor_token, game_type)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        self._log_room_op(
            room,
            "game_vote",
            f"发起换游戏投票 → {self.manager._game_label(game_type)}",
            visitor_token,
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _game_vote_cast(self, request: web.Request) -> web.Response:
        """对当前换游戏提案投赞成/反对。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        approve = payload.get("approve")
        if not isinstance(approve, bool):
            approve = str(approve or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            await self.manager.cast_game_vote(room, visitor_token, approve)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _game_vote_cancel(self, request: web.Request) -> web.Response:
        """发起人撤回换游戏提案。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            await self.manager.cancel_game_vote(room, visitor_token)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        self._log_room_op(room, "game_vote", "撤回换游戏投票", visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _chat(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_room_chat(
            room,
            str(payload.get("text") or ""),
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _dice_action(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_dice_action(
            room, visitor_token, str(payload.get("action") or "")
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _blackjack_action(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.player_blackjack_action(
            room, visitor_token, str(payload.get("action") or "")
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _draw_strokes(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.update_drawing(
            room, visitor_token, payload.get("strokes")
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _draw_guess(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_draw_guess(
            room,
            visitor_token=visitor_token,
            image_data_url=str(payload.get("image_data_url") or ""),
        )
        return self._response(
            {**result, "room": self._room_payload(room, visitor_token)}
        )

    async def _soup_question(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_turtle_soup_question(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _soup_answer(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_turtle_soup_answer(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _soup_hint(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        hint = await self.manager.request_turtle_soup_hint(
            room,
            source="web",
            visitor_token=visitor_token,
        )
        return self._response(
            {
                "hint": hint,
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _soup_reverse(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.plugin.submit_reverse_turtle_soup_turn(
            room,
            str(payload.get("text") or ""),
            source="web",
            visitor_token=visitor_token,
        )
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _soup_correct(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.confirm_reverse_turtle_soup_guess(
            room, source="web", visitor_token=visitor_token
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _soup_reveal(self, request: web.Request) -> web.Response:
        """海龟汤「揭晓汤底」：action=request 发起、vote 表态、status 查票数。

        与「投降」的区别：多人房要过半玩家同意，单人房直接生效；发起人第一票自动算同意。
        """
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        action = str(payload.get("action") or "request").strip().lower()
        if action == "vote":
            agree = bool(payload.get("agree", True))
            result = await self.manager.vote_soup_reveal(
                room,
                agree=agree,
                source="web",
                visitor_token=visitor_token,
            )
            self._log_room_op(
                room,
                "soup_reveal_vote",
                "投票揭晓汤底" if agree else "不同意揭晓汤底",
                visitor_token,
            )
        elif action == "status":
            game = room.game
            result = {
                "revealed": bool(getattr(game, "finished", False)),
                "votes": sum(
                    1
                    for value in getattr(game, "reveal_votes", {}).values()
                    if value
                ),
                "required": soup_reveal_required(room),
                "text": "",
            }
        else:
            result = await self.manager.request_soup_reveal(
                room,
                source="web",
                visitor_token=visitor_token,
            )
            self._log_room_op(
                room, "soup_reveal_request", "发起揭晓汤底", visitor_token
            )
        return self._response(
            {**result, "room": self._room_payload(room, visitor_token)}
        )

    async def _soup_announce(self, request: web.Request) -> web.Response:
        """结算卡「通知到群」：把本局汤面、提问与结果发到开房群（不含汤底）。

        按 room.completed_games 幂等：同一局只通知一次，重复点击返回已通知。
        """
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        if getattr(room, "game_type", "") != "turtle_soup":
            return web.json_response(
                {"status": "error", "message": "当前玩法不支持通知到群"},
                status=400,
            )
        try:
            self.manager.require_seated_player(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        problem = self.plugin.announce_problem(room)
        if problem:
            return web.json_response(
                {"status": "error", "message": problem},
                status=400,
            )
        key = f"soup:{int(getattr(room, 'completed_games', 0) or 0)}"
        if room.last_announced_uid == key:
            return self._response({
                "announced": bool(room.last_announced_text),
                "already": True,
                "text": room.last_announced_text,
            })
        image_data = self._image_payload(payload)
        try:
            text = await self.plugin.soup_announce_result(room, image_data)
        except Exception as exc:
            logger.warning("[GameCompanion] 海龟汤通知失败: %s", exc)
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        if text:
            room.last_announced_uid = key
            room.last_announced_text = text
            self._log_room_op(
                room,
                "room_announce",
                "手动把海龟汤战报发送到群",
                str(payload.get("visitor_token") or ""),
            )
        return self._response({
            "announced": bool(text),
            "already": False,
            "text": text,
            "disabled": not bool(
                getattr(self.plugin, "turtle_soup_group_announce_enabled", False)
            ),
        })

    async def _soup_host(self, request: web.Request) -> web.Response:
        """玩家出题人：申请 / 撤回 / 提交题目（汤面 + 汤底）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        action = str(payload.get("action") or "apply").strip().lower()
        if action == "cancel":
            await self.manager.cancel_soup_host(room, visitor_token)
            return self._response(
                {
                    "host_number": 0,
                    "cancelled": True,
                    "room": self._room_payload(room, visitor_token),
                }
            )
        if action == "submit":
            hints = payload.get("hints") or []
            if isinstance(hints, str):
                hints = [line for line in hints.splitlines() if line.strip()]
            if not isinstance(hints, list):
                hints = []
            game = await self.manager.start_soup_host_game(
                room,
                visitor_token,
                title=str(payload.get("title") or ""),
                surface=str(payload.get("surface") or ""),
                solution=str(payload.get("solution") or ""),
                hints=[str(item) for item in hints][:3],
            )
            self._log_room_op(
                room, "room_announce", "玩家出题并开局", visitor_token
            )
            return self._response(
                {
                    "started": True,
                    "host_number": game.host_number,
                    "room": self._room_payload(room, visitor_token),
                }
            )
        result = await self.manager.apply_soup_host(room, visitor_token)
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _soup_judge(self, request: web.Request) -> web.Response:
        """出题人裁决：问题给四选一，推理给「猜对了 / 不对」。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        result = await self.manager.judge_soup_pending(
            room, visitor_token, verdict=str(payload.get("verdict") or "")
        )
        return self._response(
            {**result, "room": self._room_payload(room, visitor_token)}
        )

    async def _soup_difficulty(self, request: web.Request) -> web.Response:
        """房间内实时切换海龟汤难度：次数与猜中条件当场跟着变。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        difficulty = self.plugin._soup_difficulty(payload.get("difficulty"))
        await self.manager.player_set_soup_difficulty(
            room, visitor_token, difficulty
        )
        self._log_room_op(
            room,
            "soup_difficulty",
            f"海龟汤难度改为{self.manager._difficulty_label(difficulty)}",
            visitor_token,
        )
        return self._response(
            {
                "difficulty": difficulty,
                "limits": self.manager.soup_limits(room),
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _soup_recap(self, request: web.Request) -> web.Response:
        """结算卡「花火复盘」：按局（completed_games）缓存，同局只请求一次 LLM。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        try:
            self.manager.require_visitor(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        try:
            recap = await self.plugin.soup_recap(room)
        except Exception as exc:
            logger.warning("[GameCompanion] 生成海龟汤复盘失败: %s", exc)
            recap = ""
        return self._response({"recap": (recap or "").strip()})

    async def _seat_swap_request(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        target_number = int(payload.get("target_number") or 0)
        request_id = await self.manager.request_seat_swap(
            room,
            visitor_token,
            target_number,
        )
        self._log_room_op(
            room, "room_seat_swap", f"请求与 {target_number} 号交换座位", visitor_token
        )
        return self._response(
            {
                "request_id": request_id,
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _seat_swap_respond(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        accepted = payload.get("accepted") is True
        await self.manager.resolve_seat_swap(
            room,
            visitor_token,
            str(payload.get("request_id") or ""),
            accepted=accepted,
        )
        self._log_room_op(
            room,
            "room_seat_swap",
            "接受换座请求" if accepted else "拒绝换座请求",
            visitor_token,
        )
        return self._response(
            {"accepted": accepted, "room": self._room_payload(room, visitor_token)}
        )

    async def _leave(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.leave(room, visitor_token)
        self._log_room_op(room, "room_leave", "离开房间", visitor_token)
        return self._response({"left": True})

    async def _undercover_speech(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        content = str(payload.get("content") or "")
        await self.manager.player_undercover_speech(room, visitor_token, content)
        resp = {"room": self._room_payload(room, visitor_token)}
        if getattr(room.game, "last_speech_masked", False):
            resp["notice"] = "发言中包含你的词条，已自动打码为「***」并发出。"
        return self._response(resp)

    async def _undercover_vote(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            target = int(payload.get("target_number"))
        except (TypeError, ValueError):
            return web.json_response(
                {"status": "error", "message": "target_number 必须是整数"},
                status=400,
            )
        result = await self.manager.player_undercover_vote(
            room, visitor_token, target
        )
        return self._response(
            {
                **result,
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _undercover_pk(self, request: web.Request) -> web.Response:
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        raw = payload.get("pk_targets") or []
        try:
            targets = [int(x) for x in raw]
        except (TypeError, ValueError):
            return web.json_response(
                {"status": "error", "message": "pk_targets 必须是整数数组"},
                status=400,
            )
        await self.manager.continue_undercover_pk(room, visitor_token, targets)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _undercover_set_camp_scales(self, request: web.Request) -> web.Response:
        """房主（1号玩家）在游戏开始前自定义阵营比例。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        scales_str = str(payload.get("camp_scales") or "").strip()
        try:
            parsed = await self.manager.set_undercover_host_camp_scales(
                room, visitor_token, scales_str
            )
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(
            room,
            "room_uc_scales",
            f"阵营比例改为 平民{parsed[0]} / 卧底{parsed[1]} / 白板{parsed[2]}",
            visitor_token,
        )
        return self._response(
            {
                "camp_scales": {
                    "civilian": parsed[0],
                    "undercover": parsed[1],
                    "whiteboard": parsed[2],
                    "raw": "{} {} {}".format(*parsed),
                },
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _undercover_set_reveal_identity(
        self, request: web.Request
    ) -> web.Response:
        """房主在集结阶段切换「告知身份」开关。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        reveal_identity = payload.get("reveal_identity") is True
        try:
            await self.manager.set_undercover_reveal_identity(
                room, visitor_token, reveal_identity
            )
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(
            room,
            "room_uc_reveal_identity",
            "开启「告知身份」" if reveal_identity else "关闭「告知身份」",
            visitor_token,
        )
        return self._response(
            {
                "reveal_identity": room.undercover_reveal_identity,
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _ready(self, request: web.Request) -> web.Response:
        """谁是卧底集结阶段：玩家点击准备/取消准备。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        ready = payload.get("ready") is True
        try:
            await self.manager.set_player_ready(room, visitor_token, ready)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _undercover_recap(self, request: web.Request) -> web.Response:
        """结算卡「花火复盘」：按局缓存，同局只请求一次 LLM，点击即取缓存。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        try:
            self.manager.require_visitor(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        game = getattr(room, "game", None)
        uid = str(getattr(game, "game_uid", "") or "") if game else ""
        cache = room.recap_cache or {}
        if uid and cache.get(uid):
            return self._response({"recap": cache[uid], "cached": True})
        try:
            recap = await self.plugin.undercover_recap(room)
        except Exception as exc:
            logger.warning("[GameCompanion] 生成复盘失败: %s", exc)
            recap = ""
        recap = (recap or "").strip()
        if uid:
            room.recap_cache[uid] = recap
        return self._response({"recap": recap, "cached": False})

    async def _undercover_announce(self, request: web.Request) -> web.Response:
        """结算卡「通知到群」：把本局胜负(与惩罚、复盘)发到开房群。

        同局只允许通知一次（按 game_uid），重复点击返回已通知文案而不重复发送。
        仅在插件配置开启群通报时才会真正发送。
        """
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        game = getattr(room, "game", None)
        uid = str(getattr(game, "game_uid", "") or "") if game else ""
        if uid and room.last_announced_uid == uid:
            # 本局已通知过 → 幂等返回，不重复发
            return self._response({
                "announced": bool(room.last_announced_text),
                "already": True,
                "text": room.last_announced_text,
            })
        try:
            # 只允许玩家席上的玩家触发：这个接口会往开房群发消息，仅凭链接即可调用
            # 等于把群发能力开放给任何拿到链接的人（还能写死幂等标记卡死真玩家）
            self.manager.require_seated_player(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        image_data = self._image_payload(payload)
        problem = self.plugin.announce_problem(room)
        if problem:
            return web.json_response(
                {"status": "error", "message": problem},
                status=400,
            )
        try:
            text = await self.plugin.undercover_announce_result(room, image_data)
        except Exception as exc:
            logger.warning("[GameCompanion] 通知失败: %s", exc)
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        if uid and text:
            room.last_announced_uid = uid
            room.last_announced_text = text
            self._log_room_op(
                room,
                "room_announce",
                "手动把谁是卧底战报发送到群",
                str(payload.get("visitor_token") or ""),
            )
        # announced=False 有两种原因：开关没开 / 发送失败。
        # 前端要能区分提示，否则发送失败会被说成「去开开关」。
        return self._response({
            "announced": bool(text),
            "already": False,
            "text": text,
            "disabled": not bool(getattr(self.plugin, "group_announce_enabled", False)),
        })

    async def _board_announce(self, request: web.Request) -> web.Response:
        """棋类结算卡「通知到群」：把本局胜负（谁赢了、执子、手数）发到开房群。

        按 room.completed_games 幂等：同一局只通知一次，重复点击返回已通知而不重复发。
        复用「通知到群」总开关与样式配置；只有棋类（五子棋/黑白棋/象棋/围棋）才支持。
        """
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        if getattr(room, "game_type", "") not in {
            "gomoku", "othello", "xiangqi", "weiqi",
        }:
            return web.json_response(
                {"status": "error", "message": "当前玩法不支持通知到群"},
                status=400,
            )
        key = f"board:{int(getattr(room, 'completed_games', 0) or 0)}"
        try:
            self.manager.require_seated_player(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        problem = self.plugin.announce_problem(room)
        if problem:
            return web.json_response(
                {"status": "error", "message": problem},
                status=400,
            )
        if room.last_announced_uid == key:
            return self._response({
                "announced": bool(room.last_announced_text),
                "already": True,
                "text": room.last_announced_text,
            })
        image_data = self._image_payload(payload)
        try:
            text = await self.plugin.board_announce_result(room, image_data)
        except Exception as exc:
            logger.warning("[GameCompanion] 棋类通知失败: %s", exc)
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        if text:
            room.last_announced_uid = key
            room.last_announced_text = text
            self._log_room_op(
                room,
                "room_announce",
                "手动把棋类战报发送到群",
                str(payload.get("visitor_token") or ""),
            )
        return self._response({
            "announced": bool(text),
            "already": False,
            "text": text,
            "disabled": not bool(
                getattr(self.plugin, "board_group_announce_enabled", False)
            ),
        })

    async def _board_recap(self, request: web.Request) -> web.Response:
        """棋类结算卡「花火复盘」：按局（completed_games）缓存，同局只请求一次 LLM。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        try:
            self.manager.require_visitor(
                room, str(payload.get("visitor_token") or "")
            )
        except PermissionError as exc:
            return self._forbidden(exc)
        try:
            recap = await self.plugin.board_recap(room)
        except Exception as exc:
            logger.warning("[GameCompanion] 生成棋类复盘失败: %s", exc)
            recap = ""
        return self._response({"recap": (recap or "").strip()})

    async def _seat_leave(self, request: web.Request) -> web.Response:
        """玩家主动从玩家席退到观众席（仅本人可操作）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            await self.manager.leave_player_seat(room, visitor_token)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(room, "room_seat_leave", "离开玩家席", visitor_token)
        return self._response({"room": self._room_payload(room, visitor_token)})

    async def _seat_ai(self, request: web.Request) -> web.Response:
        """两人对弈：和花火对局（action=add）/ 等对手来（action=remove）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        await self.manager.require_visitor_identity(room, visitor_token)
        action = str(payload.get("action") or "add").strip().lower()
        try:
            if action == "remove":
                result = await self.manager.remove_ai_seat(room, visitor_token)
                self._log_room_op(room, "room_ai_seat", "让花火离座", visitor_token)
            else:
                result = await self.manager.add_ai_seat(
                    room,
                    visitor_token,
                    side=str(payload.get("side") or ""),
                )
                self._log_room_op(room, "room_ai_seat", "让花火补位对局", visitor_token)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        return self._response(
            {
                **result,
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _set_difficulty(self, request: web.Request) -> web.Response:
        """玩家在房间内自选花火棋力（仅棋类、仅落座玩家、对手非真人）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        difficulty = str(payload.get("difficulty") or "").strip().lower()
        try:
            await self.manager.player_set_difficulty(
                room, visitor_token, difficulty
            )
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(
            room,
            "room_difficulty",
            f"花火棋力调整为{self.manager._difficulty_label(difficulty)}",
            visitor_token,
        )
        return self._response(
            {
                "room": self._room_payload(room, visitor_token),
            }
        )

    async def _set_side_rotation(self, request: web.Request) -> web.Response:
        """玩家在房间内自选「再来一局」换边方式（仅棋类、仅人机对局）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        mode = str(payload.get("mode") or "").strip().lower()
        try:
            await self.manager.player_set_side_rotation(room, visitor_token, mode)
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(
            room, "room_side_rotation", f"再来一局换先方式改为「{mode}」", visitor_token
        )
        return self._response({"room": self._room_payload(room, visitor_token)})

    @staticmethod
    def _forbidden(exc: Exception) -> web.Response:
        """身份校验失败：403 + 可读提示（不回显任何内部细节）。"""
        return web.json_response(
            {"status": "error", "message": str(exc) or "没有权限执行该操作"},
            status=403,
        )

    @staticmethod
    def _image_payload(payload: dict[str, Any], field: str = "image") -> str:
        """校验前端传来的战报图 data URL：只接受 png/jpeg/webp，且体积有上限。

        这个值会被原样发到开房群（`base64://…`），不过滤等于让任何拿到链接的人
        往群里塞任意二进制内容，顺带把 `last_announced_uid` 写死让真玩家发不出去。
        """
        raw = payload.get(field)
        if not isinstance(raw, str):
            return ""
        text = raw.strip()
        if not text.lower().startswith("data:image/"):
            return ""
        header, sep, b64 = text.partition(",")
        if not sep:
            return ""
        if header.lower() not in {
            "data:image/png;base64",
            "data:image/jpeg;base64",
            "data:image/jpg;base64",
            "data:image/webp;base64",
        }:
            return ""
        # base64 长度上限 ≈ 解码后 2.5MB：战报海报实测 200~600KB，够用且能挡住滥用
        if len(b64) > 3_500_000:
            return ""
        try:
            import base64 as _b64
            import binascii

            head = _b64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4), validate=True)
            _b64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return ""
        if not (
            head.startswith(b"\x89PNG\r\n\x1a\n")
            or head.startswith(b"\xff\xd8\xff")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        ):
            return ""
        return text

    def _room_payload(self, room: GameRoom, visitor_token: str) -> dict[str, Any]:
        """房间快照 + 前端需要的实时配置（换边状态、动态提醒开关）。

        所有返回房间的接口都走这里，避免「点完按钮那一帧按钮状态跳回默认、
        要等下一次轮询才对」的闪烁。
        """
        snapshot = room.public_snapshot(
            visitor_token,
            global_leaderboard=self.manager.global_leaderboard(room.game_type),
            switchable_games=self.manager.switchable_games(room),
        )
        # 提案过期是惰性的：这里顺手清一次，前端下一次轮询就看不到僵尸倒计时
        if self.manager.prune_game_vote(room):
            snapshot["game_vote"] = None
        snapshot["game_vote_enabled"] = bool(self.manager.game_vote_enabled)
        snapshot["game_vote_allow_while_playing"] = bool(
            self.manager.game_vote_allow_while_playing
        )
        snapshot["game_vote_expiry_seconds"] = int(self.manager.game_vote_expiry)
        snapshot["game_vote_cooldown_until"] = self.manager.game_vote_cooldown_until(room)
        try:
            snapshot["side_rotation"] = self.manager.side_rotation_state(
                room, visitor_token
            )
        except Exception:
            snapshot["side_rotation"] = {
                "configurable": False,
                "mode": "auto",
                "explicit": False,
            }
        snapshot["activity_toast"] = self._activity_toast_snapshot()
        # 海龟汤自由提问：前端据此把「当前轮到 X 号」换成「谁都可以问」。
        # 自由模式下压根没有轮次概念，再显示倒计时只会让人以为还要等。
        snapshot["soup_free_questioning"] = bool(
            room.game_type == "turtle_soup"
            and getattr(self.manager, "turtle_soup_free_questioning", True)
        )
        # 本次对局的三项额度：跟难度走（娱乐/简单/普通/困难各不同），
        # 前端据此拼「已问 12/35 · 提示剩 2」，也用来显示难度对应的次数说明。
        soup_limits = (
            self.manager.soup_limits(room)
            if room.game_type == "turtle_soup"
            else {"questions": 0, "hints": 0, "answers": 0}
        )
        # 每局提问上限（0 = 不限）：前端拼「已问 12/30 · 提示剩 2」
        snapshot["soup_question_limit"] = (
            int(soup_limits["questions"]) if room.game_type == "turtle_soup" else 0
        )
        # 每局推理上限（0 = 不限）：用完还没人答对就直接揭晓，前端要能提前提醒
        snapshot["soup_answer_limit"] = (
            int(soup_limits["answers"]) if room.game_type == "turtle_soup" else 0
        )
        snapshot["soup_hint_limit"] = (
            int(soup_limits["hints"]) if room.game_type == "turtle_soup" else 0
        )
        # 快照指纹：前端下一次轮询带上它，内容没变就只回一个几十字节的小包。
        # 必须放在最后——指纹覆盖这里之前的所有字段。
        snapshot["rev"] = self._snapshot_rev(snapshot)
        return snapshot

    @staticmethod
    def _snapshot_rev(snapshot: dict[str, Any]) -> str:
        """给房间快照算内容指纹，用来判断「这一秒的内容和上一秒是否一样」。

        `server_time` 每轮都不同，必须排除（但真正返回时仍要带上，前端靠它算倒计时）；
        `rev` 自己也要排除，否则会自己套自己。
        指纹就是快照内容本身，不依赖「哪些地方要记得 +1」，所以不会漏更新。
        """
        stable = {
            key: value
            for key, value in snapshot.items()
            if key not in ("server_time", "rev")
        }
        try:
            raw = json.dumps(
                stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            # 极端情况：快照里混进不可 JSON 序列化的对象。退化成「每次都不同」，
            # 前端就会一直拿全量快照——功能不受影响，只是省不到带宽。
            return f"fallback-{time.time():.6f}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    async def _undercover_add_ai_seat(self, request: web.Request) -> web.Response:
        """房主/管理员手动追加一位 AI 玩家（计入总数，不可超容量）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            result = await self.manager.add_undercover_ai_seat(
                room, visitor_token
            )
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(room, "room_uc_ai", "追加一位花火玩家", visitor_token)
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _undercover_remove_ai_seat(self, request: web.Request) -> web.Response:
        """房主/管理员手动移除一位 AI 玩家（每次一个，编号最大的优先）。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        try:
            result = await self.manager.remove_undercover_ai_seat(
                room, visitor_token
            )
        except (ValueError, PermissionError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        self._log_room_op(room, "room_uc_ai", "移除一位花火玩家", visitor_token)
        return self._response({**result, "room": self._room_payload(room, visitor_token)})

    async def _undercover_batch_words(self, request: web.Request) -> web.Response:
        """通过管理台快捷批量导入词条（每行一对 词1 词2），普通玩家调用会失败。"""
        self._require_origin(request)
        room = self._room(request)
        payload = await self._payload(request)
        visitor_token = str(payload.get("visitor_token") or "")
        # 必须是管理员房间，或玩家是房主(1号)，才允许批量加词
        if not room.admin_room:
            seat = room.multiplayer.seat_for_token(visitor_token)
            if seat is None or seat.number != 1:
                return web.json_response(
                    {"status": "error", "message": "仅管理员房间或房主能批量导入词条"},
                    status=403,
                )
        raw_text = str(payload.get("text") or "")
        store = getattr(self.manager, "undercover_word_store", None)
        if store is None:
            return web.json_response(
                {"status": "error", "message": "词库未初始化，无法批量添加"},
                status=500,
            )
        # 与插件指令 / 管理台共用同一套解析（空格、中英文逗号、冒号、顿号、制表符）
        pairs, malformed = self.plugin._parse_uc_word_lines(raw_text)
        added: list[dict] = []
        skipped: list[str] = [f"格式错误：{line}" for line in malformed]
        for word1, word2 in pairs:
            record = store.add(word1, word2)
            if record:
                added.append(record)
            else:
                skipped.append(f"重复/非法：{word1} vs {word2}")
        self._log_room_op(
            room,
            "room_uc_words",
            f"房间内批量导入词条：新增 {len(added)} 对，跳过 {len(skipped)} 对",
            visitor_token,
        )
        return self._response(
            {
                "added_count": len(added),
                "skipped_count": len(skipped),
                "skipped": skipped,
                "total_now": len(store.list_all()),
            }
        )

    async def _resolve_trusted_identity(self, request: web.Request) -> Any | None:
        if not self._trusted_browser_enabled():
            return None
        store = getattr(self.plugin, "trusted_identity_store", None)
        if store is None:
            return None
        token = str(request.cookies.get(self.TRUSTED_BROWSER_COOKIE) or "")
        return await store.resolve(token)

    async def _identity_response(
        self,
        request: web.Request,
        room: GameRoom,
        visitor: Any,
        data: dict[str, Any],
        *,
        remember: bool,
        trusted_identity: Any | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> web.Response:
        issued_token = ""
        identity = (
            trusted_identity
            if trusted_identity is not None
            else await self._resolve_trusted_identity(request)
        )
        active = bool(
            identity is not None
            and visitor.identity_confirmed
            and identity.qq == visitor.qq
        )
        enrollment_pending = bool(
            getattr(visitor, "trusted_browser_enrollment_pending", False)
        )
        if (
            self._trusted_browser_enabled()
            and remember
            and not room.admin_room
            and enrollment_pending
            and visitor.identity_confirmed
            and visitor.qq
            and not active
        ):
            store = getattr(self.plugin, "trusted_identity_store", None)
            if store is not None:
                old_token = str(
                    request.cookies.get(self.TRUSTED_BROWSER_COOKIE) or ""
                )
                if old_token:
                    await store.revoke_token(old_token)
                issued_token, identity = await store.issue(
                    visitor.qq, visitor.display_name
                )
                active = True
        if enrollment_pending:
            visitor.trusted_browser_enrollment_pending = False
        if snapshot is None:
            snapshot = self._room_payload(room, visitor.token)
        snapshot.update(
            self._trusted_browser_snapshot(
                active=active,
                expires_at=float(getattr(identity, "expires_at", 0) or 0)
                if active
                else 0,
            )
        )
        response = self._response({**data, "room": snapshot})
        if issued_token:
            ttl_days = int(
                getattr(self.plugin, "trusted_browser_ttl_days", 30) or 30
            )
            response.set_cookie(
                self.TRUSTED_BROWSER_COOKIE,
                issued_token,
                max_age=max(1, min(ttl_days, 365)) * 86400,
                path=self._trusted_cookie_path(),
                secure=True,
                httponly=True,
                samesite="Lax",
            )
        return response

    def _activity_toast_snapshot(self) -> dict[str, object]:
        """房间动态提醒的前端开关（web_ui 段，实时跟随配置）。

        前端每次轮询都会拿到最新值：关掉总开关后旧页面也会立刻停止弹窗，
        不需要刷新房间链接。读配置失败时按默认值全开，不能因此让房间状态接口挂掉。
        """
        defaults = {"enabled": True, "spectators": True, "cooldown_seconds": 15}
        plugin = self.plugin
        read_bool = getattr(plugin, "_cfg_bool", None)
        read_int = getattr(plugin, "_cfg_non_negative", None)
        if not callable(read_bool) or not callable(read_int):
            return defaults
        try:
            return {
                "enabled": bool(read_bool("web_ui.activity_toast_enabled", True)),
                "spectators": bool(
                    read_bool("web_ui.activity_toast_spectators", True)
                ),
                "cooldown_seconds": max(
                    0, int(read_int("web_ui.activity_toast_cooldown_seconds", 15))
                ),
            }
        except Exception:
            return defaults

    def _trusted_browser_snapshot(
        self, *, active: bool, expires_at: float
    ) -> dict[str, object]:
        enabled = self._trusted_browser_enabled()
        return {
            "trusted_browser_available": enabled,
            "trusted_browser_active": bool(enabled and active),
            "trusted_browser_expires_at": expires_at if enabled and active else 0,
            "trusted_browser_ttl_days": int(
                getattr(self.plugin, "trusted_browser_ttl_days", 30) or 30
            ),
        }

    def _trusted_browser_enabled(self) -> bool:
        external_base = str(
            getattr(self.plugin, "external_base_url", "") or ""
        )
        return bool(
            getattr(self.plugin, "trusted_browser_enabled", False)
            and (
                getattr(self.plugin, "public_base_url", "")
                or external_base
            )
            and getattr(self.plugin, "trusted_identity_store", None) is not None
        )

    def _trusted_cookie_path(self) -> str:
        path = str(getattr(self.plugin, "trusted_browser_cookie_path", "/") or "/")
        return path if path.startswith("/") else "/"

    @web.middleware
    async def _error_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if request.path.startswith("/api/"):
                return web.json_response(
                    {"status": "error", "message": exc.text or exc.reason},
                    status=exc.status,
                    headers=self._headers("application/json"),
                )
            raise
        except PermissionError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=403,
                headers=self._headers("application/json"),
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
                headers=self._headers("application/json"),
            )
        except RuntimeError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=503,
                headers=self._headers("application/json"),
            )
        except Exception as exc:
            logger.warning("[GameCompanion] 房间请求处理失败: %s", exc, exc_info=True)
            return web.json_response(
                {"status": "error", "message": "房间服务处理请求失败"},
                status=500,
                headers=self._headers("application/json"),
            )

    def _room(self, request: web.Request) -> GameRoom:
        token = str(request.match_info.get("access_token") or "")
        if not self.TOKEN_PATTERN.fullmatch(token):
            raise web.HTTPNotFound(text="房间链接无效")
        room = self.manager.by_access_token(token)
        if room is None:
            reason = self.manager.closed_reason_by_access_token(token)
            raise web.HTTPGone(text=reason or "房间已结束或链接已经失效")
        return room

    async def _payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise web.HTTPBadRequest(text="请求内容不是有效 JSON") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="请求内容格式无效")
        return payload

    @staticmethod
    def _visitor_label(room: GameRoom, visitor_token: str) -> str:
        """把访客 token 转成「昵称（N号）」用于日志；查不到返回空串。"""
        visitor = room.visitors.get(str(visitor_token or ""))
        if visitor is None:
            return ""
        name = str(getattr(visitor, "display_name", "") or "").strip()
        number = int(getattr(visitor, "number", 0) or 0)
        if name and number:
            return f"{name}（{number}号）"
        if name:
            return name
        return f"{number}号" if number else ""

    def _log_room_op(
        self,
        room: GameRoom,
        action: str,
        detail: str,
        visitor_token: str = "",
    ) -> None:
        """记录房间内的关键操作到管理台操作日志。

        只记「结构性 / 低频率」动作（落座、开局、离席、改难度、认输等），
        不记每一步落子、发言、掷骰等高频动作，避免日志被刷爆。
        """
        who = self._visitor_label(room, visitor_token)
        suffix = f" · {who}" if who else ""
        self.manager.record_operation(
            action,
            f"房间 {room.room_id}：{detail}{suffix}",
            operator=who,
            room_id=room.room_id,
            game_type=str(getattr(room, "game_type", "") or ""),
        )

    def _require_origin(self, request: web.Request) -> None:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="房间来源校验失败")

    def _origin_allowed(self, request: Any) -> bool:
        origin = str(request.headers.get("Origin") or "").strip()
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return False
            origin_value = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            request_value = (
                f"{str(request.scheme).lower()}://{str(request.host).lower()}"
            )
        except Exception:
            return False
        if origin_value == request_value:
            return True
        public_urls = (
            str(getattr(self.plugin, "public_base_url", "") or ""),
            str(getattr(self.plugin, "external_base_url", "") or ""),
            str(getattr(getattr(self.plugin, "quick_tunnel", None), "url", "") or ""),
        )
        for public_url in public_urls:
            try:
                public = urlsplit(public_url)
                if (
                    public.scheme
                    and public.netloc
                    and origin_value
                    == f"{public.scheme.lower()}://{public.netloc.lower()}"
                ):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _response(
        data: dict[str, Any],
        *,
        status: int = 200,
    ) -> web.Response:
        return web.json_response({"status": "ok", "data": data}, status=status)

    @staticmethod
    def _headers(content_type: str) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: https://q1.qlogo.cn https://q.qlogo.cn; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        return headers
