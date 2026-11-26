"""Rapfi 五子棋引擎：托管安装 + Piskvork 协议进程。

只做「噩梦」档使用引擎，其它档位仍走 gomoku.py 的内置启发式。引擎不可用时
由上层回退到内置搜索，所以这里的失败必须抛异常而不是静默返回坐标。

协议要点（Gomocup Piskvork，坐标 0-based）：
- ``START 15`` 初始化棋盘，回 ``OK``
- ``INFO timeout_turn <ms>`` / ``INFO game_type 1`` 只发不收
- ``BOARD`` + 若干 ``x,y,field`` + ``DONE``：field 1=引擎自己的子、2=对手的子，
  收到 DONE 后引擎直接回一行 ``x,y``
- ``END`` 无回复，进程退出
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

BOARD_SIZE = 15
MOVE_PATTERN = r"^(\d{1,2}),(\d{1,2})$"
RAPFI_REPOSITORY = "dotb1ue/rapfi"
RAPFI_RELEASE_API = f"https://api.github.com/repos/{RAPFI_REPOSITORY}/releases/latest"
# 引擎包内必须一起安装的文件：配置 + 主模型 + 自由规则权重 + 许可说明
REQUIRED_MEMBERS = ("config.toml", "model210901.bin")
WEIGHT_MEMBER = "mix9svqfreestyle_bsmix.bin.lz4"
LICENSE_MEMBERS = ("AUTHORS",)
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


class RapfiService:
    """Install and run one serialized Rapfi process for all rooms."""

    # 引擎读完 DONE 到吐出坐标之间的额外宽限（秒）。引擎自己会按 timeout_turn
    # 控时，这里只是防止进程卡死时把整间房一起拖住。
    MOVE_TIMEOUT_GRACE = 6.0

    def __init__(
        self,
        *,
        data_dir: Path,
        configured_path: str = "",
        download_proxy: str = "",
        allow_download: bool = True,
        auto_download: bool = False,
        think_ms: int = 3000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.install_root = self.data_dir / "rapfi"
        self.configured_path = str(configured_path or "").strip()
        self.download_proxy = str(download_proxy or "").strip()
        self.allow_download = bool(allow_download)
        self.auto_download = bool(auto_download)
        self.think_ms = max(100, int(think_ms))
        self.error = ""
        self.version = ""
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._lifecycle_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._process and self._process.returncode is None)

    def binary_path(self) -> Path | None:
        if self.configured_path:
            configured = Path(self.configured_path).expanduser()
            if configured.is_file():
                return configured.resolve()
        metadata = self._install_metadata()
        candidate = self.install_root / str(metadata.get("binary") or "")
        return candidate.resolve() if candidate.is_file() else None

    def status(self) -> dict[str, object]:
        binary = self.binary_path()
        metadata = self._install_metadata()
        missing: list[str] = []
        if binary is not None:
            for name in (*REQUIRED_MEMBERS, WEIGHT_MEMBER):
                if not (binary.parent / name).is_file():
                    missing.append(name)
        available = bool(binary and not missing)
        status_error = self.error
        if binary and missing and not status_error:
            status_error = "缺少引擎文件：" + "、".join(missing)
        return {
            "available": available,
            "running": self.running,
            "version": self.version or str(metadata.get("version") or ""),
            "path": str(binary) if binary else "",
            "managed": bool(binary and self.install_root in binary.parents),
            "configured": bool(binary and self.configured_path),
            "allow_download": self.allow_download,
            "auto_download": self.auto_download,
            "think_ms": self.think_ms,
            "platform": self._platform_label(),
            "error": status_error,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def ensure_ready(self) -> None:
        if self.running:
            return
        if self.binary_path() is None and self.auto_download:
            await self.install_latest()
        binary = self.binary_path()
        if binary is None:
            raise RuntimeError(
                "五子棋引擎尚未安装，请在游戏管理台安装 Rapfi，或在插件配置中填写引擎路径"
            )
        async with self._lifecycle_lock:
            if self.running:
                return
            try:
                await self._start(binary)
            except Exception as exc:
                self.error = str(exc)
                raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_process()

    async def _start(self, binary: Path) -> None:
        workdir = binary.parent
        for name in (*REQUIRED_MEMBERS, WEIGHT_MEMBER):
            if not (workdir / name).is_file():
                raise RuntimeError(f"Rapfi 缺少运行文件：{workdir / name}")
        self.error = ""
        self._lines = asyncio.Queue()
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(binary),
                cwd=str(workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise RuntimeError(f"无法启动 Rapfi：{exc}") from exc
        self._reader_task = asyncio.create_task(self._read_output())
        try:
            lines = await self._run_until(
                f"START {BOARD_SIZE}", lambda line: line.strip() == "OK", timeout=15
            )
            del lines
            self.version = self.version or "Rapfi"
            await self._write(f"INFO max_memory {256 * 1024 * 1024}")
            await self._write("INFO game_type 1")
        except Exception:
            await self._stop_process()
            raise

    async def _stop_process(self) -> None:
        process, reader = self._process, self._reader_task
        self._process = None
        self._reader_task = None
        if process is not None and process.returncode is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"END\n")
                    await process.stdin.drain()
                await asyncio.wait_for(process.wait(), timeout=3)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    # ------------------------------------------------------------------
    # 走子
    # ------------------------------------------------------------------
    async def choose_move(
        self,
        stones: list[tuple[int, int, int]],
        bot_color: int,
        *,
        think_ms: int | None = None,
    ) -> tuple[int, int]:
        """按当前局面要一手。stones 为 (row, column, color)，color 1/2。"""
        await self.ensure_ready()
        timeout_ms = max(100, int(think_ms or self.think_ms))
        occupied = {(row, column) for row, column, _color in stones}
        try:
            async with self._command_lock:
                await self._write(f"INFO timeout_turn {timeout_ms}")
                await self._write("RESTART")
                await self._write("BOARD")
                for row, column, color in stones:
                    field = 1 if color == bot_color else 2
                    await self._write(f"{column},{row},{field}")
                await self._write("DONE")
                move = await self._read_move(
                    timeout=timeout_ms / 1000 + self.MOVE_TIMEOUT_GRACE
                )
        except Exception as exc:
            self.error = str(exc)
            await self.close()
            raise
        row, column = move
        if not (0 <= row < BOARD_SIZE and 0 <= column < BOARD_SIZE):
            raise RuntimeError("Rapfi 返回了棋盘外的坐标")
        if (row, column) in occupied:
            raise RuntimeError("Rapfi 返回了已经有棋子的位置")
        return move

    async def _read_move(self, *, timeout: float) -> tuple[int, int]:
        import re

        pattern = re.compile(MOVE_PATTERN)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError("Rapfi 思考超时")
            try:
                line = await asyncio.wait_for(self._lines.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Rapfi 思考超时") from exc
            if line is None:
                raise RuntimeError(self.error or "Rapfi 进程已退出")
            text = line.strip()
            if not text:
                continue
            if text.startswith("ERROR"):
                raise RuntimeError(f"Rapfi 报错：{text}")
            match = pattern.match(text)
            if match:
                return int(match.group(1)), int(match.group(2))

    async def _run_until(self, command: str, done: Any, *, timeout: float) -> list[str]:
        await self._write(command)
        lines: list[str] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(f"Rapfi 命令超时：{command}")
            try:
                line = await asyncio.wait_for(self._lines.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"Rapfi 命令超时：{command}") from exc
            if line is None:
                raise RuntimeError(self.error or "Rapfi 进程已退出")
            text = line.strip()
            lines.append(text)
            if done(text):
                return lines
            if text.startswith("ERROR"):
                raise RuntimeError(f"Rapfi 报错：{text}")

    async def _write(self, command: str) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeError(self.error or "Rapfi 尚未运行")
        process.stdin.write((command + "\n").encode("ascii"))
        await process.stdin.drain()

    async def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while raw := await process.stdout.readline():
                await self._lines.put(raw.decode("utf-8", errors="replace").strip())
            await process.wait()
            if self._process is process:
                self.error = f"Rapfi 已退出（代码 {process.returncode}）"
                await self._lines.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读失败统一转成引擎不可用
            self.error = f"读取 Rapfi 输出失败：{exc}"
            await self._lines.put(None)

    # ------------------------------------------------------------------
    # 安装
    # ------------------------------------------------------------------
    async def install_latest(self) -> dict[str, object]:
        if not self.allow_download:
            raise PermissionError("插件配置已禁止从管理台下载五子棋引擎")
        if self.configured_path and Path(self.configured_path).expanduser().is_file():
            raise ValueError(
                "当前使用插件配置中指定的 Rapfi；请先清空引擎路径再安装托管版本"
            )
        async with self._install_lock:
            release = await self._latest_release()
            version = str(release.get("tag_name") or "").strip()
            assets = release.get("assets")
            asset = None
            if isinstance(assets, list):
                for item in assets:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").lower()
                    if name.endswith(".7z") and "engine" in name:
                        asset = item
                        break
            if not version or asset is None:
                raise RuntimeError("Rapfi 最新发行信息不完整")
            url = str(asset.get("browser_download_url") or "")
            digest = str(asset.get("digest") or "")
            if not url or not digest.startswith("sha256:"):
                raise RuntimeError("Rapfi 发行资产缺少下载地址或 SHA-256 摘要")
            member = self._binary_member()
            install_members = [
                member,
                *REQUIRED_MEMBERS,
                WEIGHT_MEMBER,
                *LICENSE_MEMBERS,
            ]
            self.install_root.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="rapfi-install-", dir=self.install_root.parent
            ) as temporary:
                temporary_root = Path(temporary)
                archive_path = temporary_root / "rapfi.7z"
                await self._download(url, archive_path)
                actual = await asyncio.to_thread(self._sha256, archive_path)
                expected = digest.removeprefix("sha256:").lower()
                if actual != expected:
                    raise RuntimeError("Rapfi 下载文件校验失败，已拒绝安装")
                extracted = temporary_root / "archive"
                await asyncio.to_thread(
                    self._extract_archive, archive_path, extracted, install_members
                )
                staging = temporary_root / "managed"
                staging.mkdir()
                binary_name = "rapfi.exe" if os.name == "nt" else "rapfi"
                shutil.copy2(extracted / member, staging / binary_name)
                for name in (*REQUIRED_MEMBERS, WEIGHT_MEMBER, *LICENSE_MEMBERS):
                    source = extracted / name
                    if source.is_file():
                        shutil.copy2(source, staging / name)
                if os.name != "nt":
                    (staging / binary_name).chmod(
                        (staging / binary_name).stat().st_mode | stat.S_IXUSR
                    )
                metadata = {
                    "version": version,
                    "binary": binary_name,
                    "sha256": actual,
                    "asset": str(asset.get("name") or ""),
                    "source": url,
                    "platform": self._platform_label(),
                }
                (staging / "install.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                async with self._command_lock:
                    await self.close()
                    backup = self.install_root.with_name("rapfi.previous")
                    if backup.exists():
                        shutil.rmtree(backup)
                    had_previous = self.install_root.exists()
                    if had_previous:
                        self.install_root.rename(backup)
                    try:
                        staging.rename(self.install_root)
                    except Exception:
                        if (
                            had_previous
                            and backup.exists()
                            and not self.install_root.exists()
                        ):
                            backup.rename(self.install_root)
                        raise
                    try:
                        self.error = ""
                        self.version = version
                        await self.ensure_ready()
                    except Exception as exc:
                        await self.close()
                        if self.install_root.exists():
                            shutil.rmtree(self.install_root)
                        if backup.exists():
                            backup.rename(self.install_root)
                        self.version = ""
                        self.error = str(exc)
                        raise
                    else:
                        if backup.exists():
                            shutil.rmtree(backup)
            return self.status()

    async def _latest_release(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AstrBot-GameCompanion",
        }
        proxy = self._effective_proxy()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(RAPFI_RELEASE_API, proxy=proxy) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"读取 Rapfi 发行信息失败：HTTP {response.status}"
                        )
                    payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RuntimeError(f"读取 Rapfi 发行信息失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Rapfi 发行信息格式无效")
        return payload

    async def _download(self, url: str, target: Path) -> None:
        timeout = aiohttp.ClientTimeout(total=300)
        proxy = self._effective_proxy()
        headers = {"User-Agent": "AstrBot-GameCompanion"}
        size = 0
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, proxy=proxy, allow_redirects=True) as response:
                    if response.status != 200:
                        raise RuntimeError(f"下载 Rapfi 失败：HTTP {response.status}")
                    with target.open("wb") as stream:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_ARCHIVE_BYTES:
                                raise RuntimeError("Rapfi 下载文件超过安全大小限制")
                            stream.write(chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RuntimeError(f"下载 Rapfi 失败：{exc}") from exc
        if size < 1024 * 1024:
            raise RuntimeError("Rapfi 下载文件异常过小")

    def _binary_member(self) -> str:
        system = platform.system().lower()
        machine = platform.machine().lower()
        if system == "windows" and machine in {"x86_64", "amd64"}:
            return "pbrain-rapfi-windows-avx2.exe"
        if system == "linux" and machine in {"x86_64", "amd64"}:
            return "pbrain-rapfi-linux-clang-avx2"
        if system == "darwin" and machine in {"arm64", "aarch64"}:
            return "pbrain-rapfi-macos-apple-silicon"
        raise RuntimeError(
            f"当前平台暂无 Rapfi 自动安装包：{platform.system()} {platform.machine()}"
        )

    def _platform_label(self) -> str:
        try:
            return self._binary_member()
        except RuntimeError:
            return f"{platform.system()} {platform.machine()}"

    def _install_metadata(self) -> dict[str, Any]:
        path = self.install_root / "install.json"
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _effective_proxy(self) -> str | None:
        return (
            self.download_proxy
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or None
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _extract_archive(
        archive_path: Path, target: Path, members: list[str]
    ) -> None:
        try:
            import py7zr
        except ImportError as exc:
            raise RuntimeError("缺少 py7zr，无法解压 Rapfi 安装包") from exc
        target.mkdir(parents=True, exist_ok=True)
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            names = set(archive.getnames())
            missing = [
                member
                for member in (members[0], *REQUIRED_MEMBERS, WEIGHT_MEMBER)
                if member not in names
            ]
            if missing:
                raise RuntimeError("Rapfi 安装包缺少必要文件：" + "、".join(missing))
            archive.extract(path=target, targets=[m for m in members if m in names])
