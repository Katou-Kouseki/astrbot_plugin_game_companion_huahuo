from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp

QUICK_TUNNEL_PATTERN = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE
)


class QuickTunnel:
    """Manage an optional Cloudflare Quick Tunnel subprocess."""

    def __init__(self, local_url: str, search_paths: list[Path] | None = None) -> None:
        self.local_url = str(local_url or "").rstrip("/")
        self.search_paths = [Path(path) for path in search_paths or []]
        self.url = ""
        self.started_at = 0.0
        self.error = ""
        self._reachable = False
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.returncode is None and self.url)

    @property
    def ready(self) -> bool:
        return self.running and self._reachable

    def binary_path(self) -> Path | None:
        """Find cloudflared without downloading or modifying the host."""
        command = shutil.which("cloudflared")
        if command:
            return Path(command)
        names = (
            ("cloudflared.exe", "cloudflared") if os.name == "nt" else ("cloudflared",)
        )
        for root in self.search_paths:
            for name in names:
                candidate = root / name
                if candidate.is_file():
                    return candidate
        return None

    def status(self) -> dict[str, object]:
        """Return a dashboard-safe tunnel status."""
        return {
            "installed": self.binary_path() is not None,
            "running": self.running,
            "ready": self.ready,
            "url": self.url if self.running else "",
            "started_at": self.started_at if self.running else 0.0,
            "error": self.error,
        }

    async def start(self, timeout: float = 40.0) -> str:
        """Start cloudflared and wait for its temporary HTTPS URL."""
        async with self._start_lock:
            if self.ready:
                return self.url
            binary = self.binary_path()
            if binary is None:
                raise RuntimeError(
                    "未找到 cloudflared，请先安装 Cloudflare Tunnel 客户端"
                )
            await self.stop()
            self.url = ""
            self.started_at = 0.0
            self.error = ""
            self._reachable = False
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self._process = await asyncio.create_subprocess_exec(
                    str(binary),
                    "tunnel",
                    "--no-autoupdate",
                    "--url",
                    self.local_url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise RuntimeError(f"无法启动 cloudflared：{exc}") from exc
            self._reader_task = asyncio.create_task(self._read_output())
            deadline = asyncio.get_running_loop().time() + max(2.0, timeout)
            while not self.url and asyncio.get_running_loop().time() < deadline:
                if self._process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if not self.url or self._process.returncode is not None:
                message = self.error or "cloudflared 未返回临时公网地址"
                await self.stop()
                raise RuntimeError(message)
            remaining = max(2.0, deadline - asyncio.get_running_loop().time())
            if not await self._wait_until_reachable(
                self.url,
                self._process,
                timeout=remaining,
            ):
                message = self.error or "临时公网地址尚未生效，请稍后重试"
                await self.stop()
                raise RuntimeError(message)
            self._probe_task = asyncio.create_task(
                self._monitor_reachability(self.url, self._process)
            )
            return self.url

    async def stop(self) -> None:
        """Stop the subprocess and clear the temporary public URL."""
        process = self._process
        reader = self._reader_task
        probe = self._probe_task
        self._process = None
        self._reader_task = None
        self._probe_task = None
        self.url = ""
        self.started_at = 0.0
        self._reachable = False
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if (
            probe is not None
            and probe is not asyncio.current_task()
            and not probe.done()
        ):
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)

    async def _probe_health(self, public_url: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=4.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{public_url.rstrip('/')}/health",
                    allow_redirects=False,
                ) as response:
                    return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def _wait_until_reachable(
        self,
        public_url: str,
        process: asyncio.subprocess.Process | None,
        *,
        timeout: float,
    ) -> bool:
        """Require two successful public probes before a room URL is issued."""
        deadline = asyncio.get_running_loop().time() + max(2.0, timeout)
        consecutive_successes = 0
        while (
            self._process is process
            and process is not None
            and process.returncode is None
            and self.url == public_url
            and asyncio.get_running_loop().time() < deadline
        ):
            if await self._probe_health(public_url):
                consecutive_successes += 1
                if consecutive_successes >= 2:
                    self._reachable = True
                    self.error = ""
                    return True
            else:
                consecutive_successes = 0
            await asyncio.sleep(1.0)
        self.error = "临时公网地址未通过连通性检查，已停止签发该链接"
        return False

    async def _monitor_reachability(
        self,
        public_url: str,
        process: asyncio.subprocess.Process | None,
        *,
        interval: float = 15.0,
        failure_limit: int = 3,
    ) -> None:
        """Mark a live subprocess unhealthy when its public hostname stops working."""
        failures = 0
        try:
            while (
                self._process is process
                and process is not None
                and process.returncode is None
                and self.url == public_url
            ):
                await asyncio.sleep(max(0.0, interval))
                if await self._probe_health(public_url):
                    failures = 0
                    self._reachable = True
                    self.error = ""
                    continue
                failures += 1
                if failures >= max(1, failure_limit):
                    self._reachable = False
                    self.error = "临时公网地址已失去连通性，正在重新建立通道"
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reachable = False
            self.error = f"公网地址连通性检查失败：{exc}"

    async def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self.error = "无法读取 cloudflared 输出"
            return
        lines: list[str] = []
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)
                    del lines[:-8]
                match = QUICK_TUNNEL_PATTERN.search(line)
                if match and not self.url:
                    self.url = match.group(0).rstrip("/")
                    self.started_at = time.time()
            await process.wait()
            if self._process is process:
                self._reachable = False
                detail = lines[-1][-300:] if lines else "未提供错误信息"
                self.error = f"cloudflared 已退出（代码 {process.returncode}）：{detail}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"读取 cloudflared 状态失败：{exc}"
