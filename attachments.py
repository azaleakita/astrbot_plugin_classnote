"""Read only the platform-provided file component; never accept chat-supplied paths."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .core import TimetableError
from .ics import MAX_BYTES


def _local_bytes(location):
    path = Path(location)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise TimetableError("附件不存在、不是普通文件或超过2MiB。")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise TimetableError("日历文件超过2MiB。")
    return data


async def attachment_bytes(component, session):
    try:
        async with asyncio.timeout(25):
            location = await component.get_file(allow_return_url=True)
            if not location:
                raise TimetableError(
                    "平台没有提供可读取的附件，请重新发送原始 .ics 文件。"
                )
            if urlsplit(location).scheme in ("http", "https"):
                async with session.get(
                    location, timeout=aiohttp.ClientTimeout(total=20), max_redirects=3
                ) as response:
                    if response.status != 200:
                        raise TimetableError("日历附件下载失败，请重新发送文件。")
                    length = response.content_length
                    if length is not None and length > MAX_BYTES:
                        raise TimetableError("日历文件超过2MiB。")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise TimetableError("日历文件超过2MiB。")
                        chunks.append(chunk)
                    return b"".join(chunks)
            return await asyncio.to_thread(_local_bytes, location)
    except TimetableError:
        raise
    except Exception as exc:
        raise TimetableError(
            "无法读取日历附件，请检查 QQ 适配器的文件接收配置。"
        ) from exc
