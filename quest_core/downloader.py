"""文件下载 —— 与原 nonebot 版 downloader.py 等价，另增 base64 段落直接落盘。

MaiBot 的消息段里图片可能是 URL，也可能是 base64（取决于适配器），
两种都支持。
"""

import asyncio
import base64
import binascii
import re
from pathlib import Path

import httpx

from . import config, storage

_sem: asyncio.Semaphore | None = None

_SAFE_NAME = re.compile(r"[^\w.\-]")

logger = None  # 由 plugin.py 注入（ctx.logger）


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None or _sem._value != config.get("download_concurrency"):
        _sem = asyncio.Semaphore(config.get("download_concurrency"))
    return _sem


def sanitize(filename: str) -> str:
    """去掉路径分隔符和非法字符，防路径穿越。"""
    name = Path(filename).name
    name = _SAFE_NAME.sub("_", name)
    return name or "unnamed"


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        new_path = directory / f"{stem}_{i}{suffix}"
        if not new_path.exists():
            return new_path
        i += 1


def ensure_ext(filename: str, fallback: str) -> str:
    if Path(filename).suffix:
        return filename
    return filename + fallback


def _decode_base64(data: str) -> bytes:
    """兼容纯 base64 和 data URI 两种形式。"""
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


async def save_base64(data: str, directory: Path, filename: str,
                      message_id, ftype: str):
    """消息段自带 base64 内容时直接写文件，不经过网络。"""
    filename = sanitize(ensure_ext(filename, ".bin"))
    try:
        raw = _decode_base64(data)
    except (binascii.Error, ValueError) as e:
        storage.log_error(message_id, "", ftype, f"base64 decode failed: {e}")
        return
    path = unique_path(directory, filename)
    with open(path, "wb") as f:
        f.write(raw)
    if logger:
        logger.info(f"[quest] base64 落盘成功: {path}")


async def download_file(url: str, directory: Path, filename: str,
                        message_id, ftype: str):
    filename = sanitize(ensure_ext(filename, ".bin"))
    async with _get_sem():
        # QQ 图片文件名是内容 MD5，同名即同内容：已存在则跳过，避免重复下载。
        # 检查放在信号量内，保证并发下载同名文件时也只下载一次。
        target = directory / filename
        if ftype == "image" and target.exists():
            if target.stat().st_size > 0:
                if logger:
                    logger.info(f"[quest] 图片已存在，跳过: {target}")
                return
            # 0 字节残留（上次异常退出）→ 清掉重新下载
            target.unlink(missing_ok=True)
        path = None
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    path = unique_path(directory, filename)
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)
            if logger:
                logger.info(f"[quest] 下载成功: {path}")
        except Exception as e:
            # 清掉半截文件，避免残留损坏文件
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            storage.log_error(message_id, url, ftype, str(e))
            if logger:
                logger.warning(f"[quest] 下载失败: {url} - {e}")
