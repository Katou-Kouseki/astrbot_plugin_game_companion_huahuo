from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from astrbot_plugin_game_companion_huahuo.gomoku import BLACK, WHITE, GomokuGame
from astrbot_plugin_game_companion_huahuo.rapfi import RapfiService
from astrbot_plugin_game_companion_huahuo.room_manager import RoomManager

ROOT = Path(__file__).resolve().parents[1]

FAKE_ENGINE = r'''
import os
import sys
import time

board = {}
collecting = False
for raw in sys.stdin:
    line = raw.strip()
    if collecting:
        if line == "DONE":
            collecting = False
            if os.environ.get("FAKE_RAPFI_SLEEP") == "1":
                time.sleep(2)
            move = next(
                ((x, y) for y in range(15) for x in range(15) if (x, y) not in board),
                (0, 0),
            )
            print(f"{move[0]},{move[1]}", flush=True)
        else:
            try:
                x, y, field = line.split(",")
            except ValueError:
                print("ERROR bad board line", flush=True)
                continue
            if int(field) not in (1, 2):
                print("ERROR bad field", flush=True)
                continue
            board[(int(x), int(y))] = int(field)
        continue
    if line.startswith("START"):
        print("MESSAGE fake rapfi", flush=True)
        print("OK", flush=True)
    elif line == "RESTART":
        board = {}
    elif line == "BOARD":
        board = {}
        collecting = True
    elif line == "END":
        break
'''


def _write_fake_engine(tmp_path: Path) -> Path:
    script = tmp_path / "fake_rapfi.py"
    script.write_text(FAKE_ENGINE, encoding="utf-8")
    binary = tmp_path / "fake_rapfi"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    for name in ("config.toml", "model210901.bin", "mix9svqfreestyle_bsmix.bin.lz4"):
        (tmp_path / name).write_text("", encoding="utf-8")
    return binary


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    """让 RapfiService 的 subprocess 实际跑 python 假引擎。"""
    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        return await real(sys.executable, str(script), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
async def test_engine_move_uses_piskvork_protocol(tmp_path, monkeypatch) -> None:
    binary = _write_fake_engine(tmp_path)
    _patch_spawn(monkeypatch, tmp_path / "fake_rapfi.py")
    service = RapfiService(
        data_dir=tmp_path, configured_path=str(binary), think_ms=300
    )
    try:
        move = await service.choose_move([(7, 7, BLACK)], WHITE)
    finally:
        await service.close()

    assert move == (0, 0), "假引擎返回第一个空点，说明 BOARD/DONE 往返是通的"
    assert service.error == ""


@pytest.mark.asyncio
async def test_engine_timeout_raises_and_resets(tmp_path, monkeypatch) -> None:
    binary = _write_fake_engine(tmp_path)
    _patch_spawn(monkeypatch, tmp_path / "fake_rapfi.py")
    monkeypatch.setenv("FAKE_RAPFI_SLEEP", "1")
    monkeypatch.setattr(RapfiService, "MOVE_TIMEOUT_GRACE", 0.2)
    service = RapfiService(
        data_dir=tmp_path, configured_path=str(binary), think_ms=100
    )
    try:
        with pytest.raises(RuntimeError, match="超时"):
            await service.choose_move([(7, 7, BLACK)], WHITE)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_engine_reports_install_hint(tmp_path) -> None:
    service = RapfiService(data_dir=tmp_path)
    assert service.status()["available"] is False
    with pytest.raises(RuntimeError, match="游戏管理台安装"):
        await service.ensure_ready()


class _BoomEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def choose_move(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("引擎没装")


@pytest.mark.asyncio
async def test_nightmare_falls_back_to_builtin_when_engine_fails() -> None:
    engine = _BoomEngine()
    manager = RoomManager(max_private_rooms=0, gomoku_engine=engine)
    room = await manager.create_room(
        source="private",
        session_id="web:private:test",
        platform="web",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type="gomoku",
        difficulty="nightmare",
    )
    game = GomokuGame(human_color=BLACK, difficulty="nightmare")
    game.turn = WHITE
    room.game = game
    room.status = "active"

    plan = await manager._plan_gomoku_engine_move(room, game, ("marker",))

    assert engine.calls == 1
    assert plan["kind"] == "gomoku"
    row, column = plan["move"]
    assert game.board[row][column] == 0
    assert any("退回内置困难" in str(getattr(m, "content", m)) for m in room.messages)


@pytest.mark.asyncio
async def test_nightmare_is_gomoku_only() -> None:
    manager = RoomManager(max_private_rooms=0)
    gomoku = await manager.create_room(
        source="private",
        session_id="web:private:a",
        platform="web",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type="gomoku",
        difficulty="normal",
    )
    await manager.set_difficulty(gomoku, "nightmare")
    assert gomoku.difficulty == "nightmare"

    weiqi = await manager.create_room(
        source="private",
        session_id="web:private:b",
        platform="web",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type="weiqi",
        difficulty="normal",
    )
    with pytest.raises(ValueError, match="只用于五子棋"):
        await manager.set_difficulty(weiqi, "nightmare")


@pytest.mark.asyncio
async def test_switching_away_from_gomoku_resets_nightmare() -> None:
    manager = RoomManager(max_private_rooms=0)
    room = await manager.create_room(
        source="private",
        session_id="web:private:c",
        platform="web",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type="gomoku",
        difficulty="nightmare",
    )
    await manager.switch_game(room, "tictactoe")

    assert room.difficulty == "hard"
    assert any("退回困难" in str(getattr(m, "content", m)) for m in room.messages)


def test_frontend_and_schema_contracts() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    gomoku = schema["gomoku"]["items"]
    assert gomoku["allow_engine_download"]["default"] is True
    assert gomoku["auto_download_engine"]["default"] is False
    assert gomoku["nightmare_think_ms"]["default"] == 3000

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    room_html = (ROOT / "pages" / "游戏管理台" / "index.html").read_text(encoding="utf-8")
    manager = (ROOT / "pages" / "游戏管理台" / "manager.js").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'data-difficulty="nightmare"' in html
    assert 'id="turnThink"' in html
    assert 'd === "nightmare"' in app, "噩梦按钮必须只在五子棋里显示"
    assert "renderBotThink" in app
    assert ".turn-think" in (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="gomokuEngineAction"' in room_html
    assert "gomoku/install" in manager
    assert 'snapshot["bot_think"]' in server


@pytest.mark.asyncio
async def test_install_extracts_only_needed_members(tmp_path, monkeypatch) -> None:
    """托管安装只留引擎 + 配置 + 自由规则权重，不把四种规则权重全铺下去。"""
    import hashlib

    import py7zr

    service = RapfiService(data_dir=tmp_path, think_ms=500)
    member = service._binary_member()
    archive = tmp_path / "rapfi.7z"
    payload = {
        member: b"binary",
        "config.toml": b"[general]",
        "model210901.bin": b"model",
        "mix9svqfreestyle_bsmix.bin.lz4": b"freestyle",
        "AUTHORS": b"authors",
        "mix9svqstandard_bs15.bin.lz4": b"standard-should-not-be-extracted",
    }
    with py7zr.SevenZipFile(archive, "w") as writer:
        for name, data in payload.items():
            path = tmp_path / name
            path.write_bytes(data)
            writer.write(path, name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    async def fake_release():
        return {
            "tag_name": "250615",
            "assets": [
                {
                    "name": "Rapfi-engine.7z",
                    "browser_download_url": "https://example.invalid/rapfi.7z",
                    "digest": f"sha256:{digest}",
                }
            ],
        }

    async def fake_download(url, target):
        target.write_bytes(archive.read_bytes())

    async def fake_ready():
        return None

    monkeypatch.setattr(service, "_latest_release", fake_release)
    monkeypatch.setattr(service, "_download", fake_download)
    monkeypatch.setattr(service, "ensure_ready", fake_ready)

    status = await service.install_latest()

    assert status["available"] is True
    assert status["version"] == "250615"
    installed = {p.name for p in service.install_root.iterdir()}
    assert "mix9svqfreestyle_bsmix.bin.lz4" in installed
    assert "mix9svqstandard_bs15.bin.lz4" not in installed
    assert (service.install_root / "install.json").is_file()


class _BlockingEngine:
    """卡在思考里的引擎：用来观察房间的「思考中」标记。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def choose_move(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return (7, 7)


@pytest.mark.asyncio
async def test_room_flags_thinking_while_bot_calculates() -> None:
    """算棋期间房间要亮着「思考中」，落子后立刻熄掉（前端秒表只认这个）。"""
    engine = _BlockingEngine()
    manager = RoomManager(max_private_rooms=0, gomoku_engine=engine)
    room = await manager.create_room(
        source="private",
        session_id="web:private:think",
        platform="web",
        group_id="",
        creator_qq="10001",
        creator_name="房主",
        admin_room=False,
        game_type="gomoku",
        difficulty="nightmare",
    )
    game = GomokuGame(human_color=BLACK, difficulty="nightmare")
    game.turn = WHITE
    room.game = game
    room.status = "active"
    # 单人对局：不走「两名玩家抢座」那套，花火就是对手
    room.multiplayer.enabled = False
    manager.bot_think_delay = lambda _room: 0.0

    task = asyncio.create_task(manager._bot_turn(room))
    try:
        await asyncio.wait_for(engine.started.wait(), timeout=5)
        assert room.bot_think_active is True
        assert room.bot_think_started_at > 0
    finally:
        engine.release.set()
        await asyncio.wait_for(task, timeout=5)

    assert room.bot_think_active is False
    assert room.bot_think_started_at == 0.0
    assert room.last_bot_think_ms >= 0


def test_thinking_flag_and_snapshot_contracts() -> None:
    """快照要带 active/elapsed_ms，前端要有本地 ticker，否则秒表只会跳一帧。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    models = (ROOT / "models.py").read_text(encoding="utf-8")

    assert "bot_think_active" in models
    assert '"active": thinking' in server
    assert '"elapsed_ms"' in server
    assert "思考中 ${" in app
    assert "setInterval(paintBotThink" in app
    assert ".turn-think.is-live" in css
    # 秒表每天都在涨，不能进内容指纹，否则思考期间每轮都传 20KB 全量
    assert 'if key != "elapsed_ms"' in server
    # 思考时轮询提速，落子那一帧才不会从 4 秒跳回 3 秒
    assert "Boolean(room?.bot_think?.active)" in app
