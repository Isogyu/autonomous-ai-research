"""情報源の共通部品: レート制限、再試行付きの HTTP、User-Agent。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """リクエスト間の最小間隔（秒）を守る。同時に1本しか通さない。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        # asyncio.Lock はイベントループに結び付くので、ループごとに作る
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        key = id(asyncio.get_running_loop())
        return self._locks.setdefault(key, asyncio.Lock())

    async def wait(self) -> None:
        async with self._lock():
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


def user_agent(contact_email: str = "") -> str:
    """連絡先が設定されているときだけ mailto を付ける。空なら個人情報は送らない。"""
    ua = "autonomous-ai-research/0.1"
    if contact_email:
        ua += f" (mailto:{contact_email})"
    return ua


def make_client(contact_email: str = "", timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact_email)}, timeout=timeout, follow_redirects=True
    )


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None  # HTTP 日付形式は使わず通常のバックオフに任せる


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    limiter: RateLimiter | None = None,
    max_attempts: int = 4,
    method: str = "GET",
    json: Any = None,
    backoff_base: float = 1.0,
    max_delay: float = 120.0,
) -> httpx.Response:
    """429/5xx とネットワーク例外を指数バックオフで再試行する。他の 4xx は即例外。"""
    for attempt in range(1, max_attempts + 1):
        if limiter:
            await limiter.wait()
        delay = min(backoff_base * 2 ** (attempt - 1), max_delay)
        try:
            resp = await client.request(method, url, params=params, json=json)
        except httpx.TransportError as e:
            if attempt == max_attempts:
                raise
            log.warning("通信エラー %s（%d/%d回目）: %s", url, attempt, max_attempts, e)
        else:
            if resp.status_code not in RETRY_STATUS:
                resp.raise_for_status()  # 4xx はここで例外
                return resp
            if attempt == max_attempts:
                resp.raise_for_status()
            ra = _retry_after(resp)
            if ra is not None:
                delay = min(ra, max_delay)
            log.warning("HTTP %d %s（%d/%d回目）", resp.status_code, url, attempt, max_attempts)
        await asyncio.sleep(delay)
    raise RuntimeError("unreachable")
