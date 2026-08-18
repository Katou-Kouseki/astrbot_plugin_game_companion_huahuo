from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_game_companion.tunnel import QuickTunnel


class _Output:
    def __init__(self, *lines: bytes) -> None:
        self._lines = list(lines) + [b""]

    async def readline(self) -> bytes:
        return self._lines.pop(0)


class _ExitedProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = _Output(b"https://temporary-game.trycloudflare.com\n")

    async def wait(self) -> int:
        self.returncode = 7
        return 7


@pytest.mark.asyncio
async def test_tunnel_records_an_exit_after_publishing_its_url() -> None:
    tunnel = QuickTunnel("http://127.0.0.1:42000")
    process = _ExitedProcess()
    tunnel._process = process

    await tunnel._read_output()

    assert tunnel.url == "https://temporary-game.trycloudflare.com"
    assert tunnel.started_at > 0
    assert not tunnel.running
    assert not tunnel.ready
    assert "代码 7" in tunnel.error


@pytest.mark.asyncio
async def test_tunnel_waits_for_two_public_health_checks(monkeypatch) -> None:
    tunnel = QuickTunnel("http://127.0.0.1:42000")
    process = SimpleNamespace(returncode=None)
    tunnel._process = process
    tunnel.url = "https://temporary-game.trycloudflare.com"
    probe = AsyncMock(side_effect=[False, True, True])
    monkeypatch.setattr(tunnel, "_probe_health", probe)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    reachable = await tunnel._wait_until_reachable(
        tunnel.url,
        process,
        timeout=5,
    )

    assert reachable is True
    assert tunnel.ready is True
    assert probe.await_count == 3


@pytest.mark.asyncio
async def test_tunnel_monitor_marks_live_process_unhealthy_after_failures() -> None:
    tunnel = QuickTunnel("http://127.0.0.1:42000")
    process = SimpleNamespace(returncode=None)
    tunnel._process = process
    tunnel.url = "https://temporary-game.trycloudflare.com"
    tunnel._reachable = True
    tunnel._probe_health = AsyncMock(return_value=False)

    await tunnel._monitor_reachability(
        tunnel.url,
        process,
        interval=0,
        failure_limit=2,
    )

    assert tunnel.running is True
    assert tunnel.ready is False
    assert "正在重新建立通道" in tunnel.error
