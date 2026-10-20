"""A deliberately small compatibility adapter for WakeUp's legacy share endpoint."""

import asyncio
import json

import aiohttp

from .core import TimetableError, extract_share_key, parse_timetable, unwrap_api


async def import_share(session: aiohttp.ClientSession, text: str) -> dict:
    key = extract_share_key(text)
    try:
        async with session.get(
            "https://i.wakeup.fun/share_schedule/get",
            params={"key": key},
            headers={"User-Agent": "okhttp/4.12.0"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=18),
        ) as response:
            if response.status != 200:
                raise TimetableError(
                    f"WakeUp 请求未成功（HTTP {response.status}），原课表未被替换。"
                )
            # Size limit applies while reading, including decompressed chunks.
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise TimetableError("WakeUp 响应过大，拒绝导入。")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Do not include URLs/keys from exceptions in chat or logs.
        raise TimetableError("连接 WakeUp 超时或网络不可用，原课表未被替换。") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TimetableError(
            "WakeUp 未返回可识别的课表 JSON，原课表未被替换。"
        ) from exc
    return parse_timetable(unwrap_api(payload))
