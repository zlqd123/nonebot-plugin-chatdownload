import asyncio
import re
from pathlib import Path

import httpx
from nonebot import logger

from .config import META
from .storage import log_error

_sem = asyncio.Semaphore(META["download_concurrency"])

_SAFE_NAME = re.compile(r"[^\w.\-]")


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


async def download_file(url: str, directory: Path, filename: str,
                        message_id: int, ftype: str):
    filename = sanitize(ensure_ext(filename, ".bin"))
    async with _sem:
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
            logger.info(f"[quest] 下载成功: {path}")
        except Exception as e:
            # 清掉半截文件，避免残留损坏文件
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            log_error(message_id, url, ftype, str(e))
            logger.warning(f"[quest] 下载失败: {url} - {e}")
