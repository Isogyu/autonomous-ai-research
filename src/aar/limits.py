"""実行担当（Codex / Claude Code）の利用上限の扱い。

定額契約の利用枠には上限がある。上限に達したときの決まり:
  * 相手が解除の時刻を示したら、その時刻（＋余裕）まで待つ。示さなければ、待ち時間を 15 分から倍々に延ばす（最大 4 時間）。
  * 解除の見込み時刻までは、相手に問い合わせない。研究が何件あっても、確かめに行くのは解除の見込み時刻を過ぎてからの
    1回ずつで、上限を回避するための連打はしない。
  * 状態は DB（provider_limits）に置く。ワーカーを再起動しても、別の研究からも、同じ状態を見る。
  * 自動で待つ回数には上限がある（ワークフロー側）。それを超えたら、人が再開するまで止まる。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from aar.config import get_settings
from aar.db.core import connect, fetch_one
from aar.models import AgentResult

BACKOFF_FIRST = dt.timedelta(minutes=15)
BACKOFF_MAX = dt.timedelta(hours=4)
STREAK_RESET = dt.timedelta(hours=12)            # これだけ間が空いたら、連続回数を数え直す


def backoff(hits: int) -> dt.timedelta:
    """解除の時刻が分からないときの待ち時間（15分、30分、1時間、2時間、以後4時間）。"""
    return min(BACKOFF_FIRST * (2 ** max(hits - 1, 0)), BACKOFF_MAX)


def note_limit(conn: psycopg.Connection, provider: str, retry_at: dt.datetime | None, message: str,
               now: dt.datetime | None = None) -> dt.datetime:
    """上限に達したことを記録し、次に呼び出してよい時刻を返す。"""
    now = now or dt.datetime.now(dt.UTC)
    prev = fetch_one(conn, "SELECT * FROM provider_limits WHERE provider = %s", (provider,))
    hits = 1
    if prev and now - prev["last_hit_at"] < STREAK_RESET:
        hits = int(prev["consecutive_hits"]) + 1
    if retry_at is not None and retry_at > now:
        # 相手が示した時刻ちょうどには行かず、少し余裕を置く
        until, source = retry_at + dt.timedelta(seconds=get_settings().rate_limit_margin_s), "provider_message"
        # 示された時刻を過ぎても続けて上限に当たる場合は、間隔を延ばす
        if hits > 1 and prev and prev["source"] == "provider_message" and abs((prev["limited_until"] - until).total_seconds()) < 60:
            until = max(until, now + backoff(hits - 1))
    else:
        until, source = now + backoff(hits), "backoff"
    conn.execute(
        """INSERT INTO provider_limits (provider, limited_until, source, consecutive_hits, first_hit_at, last_hit_at, message)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (provider) DO UPDATE SET limited_until = EXCLUDED.limited_until, source = EXCLUDED.source,
             consecutive_hits = EXCLUDED.consecutive_hits, last_hit_at = EXCLUDED.last_hit_at, message = EXCLUDED.message,
             first_hit_at = CASE WHEN EXCLUDED.consecutive_hits = 1 THEN EXCLUDED.first_hit_at ELSE provider_limits.first_hit_at END""",
        (provider, until, source, hits, now, now, message[:500]))
    return until


def limited_until(conn: psycopg.Connection, provider: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """まだ呼び出してはいけない場合、その解除の見込み時刻。呼び出してよければ None。"""
    now = now or dt.datetime.now(dt.UTC)
    row = fetch_one(conn, "SELECT limited_until FROM provider_limits WHERE provider = %s", (provider,))
    return row["limited_until"] if row and row["limited_until"] > now else None


def clear_limit(conn: psycopg.Connection, provider: str) -> bool:
    """上限の記録を消す（呼び出しが成功したとき、または人が解除を確かめたとき）。"""
    return bool(conn.execute("DELETE FROM provider_limits WHERE provider = %s", (provider,)).rowcount)


def current_limits(conn: psycopg.Connection) -> list[dict[str, Any]]:
    from aar.db.core import fetch_all

    return fetch_all(conn, "SELECT * FROM provider_limits ORDER BY provider")


def wait_plan(conn: psycopg.Connection, project_id: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """利用上限で保留になった研究が、次に再開を試みるまでの待ち時間。

    上限に達している実行担当のうち、最も遅い解除の見込み時刻まで待つ。研究ごとに数十秒ずらし、同時に殺到させない。
    """
    now = now or dt.datetime.now(dt.UTC)
    rows = [r for r in current_limits(conn) if r["limited_until"] > now]
    until = max((r["limited_until"] for r in rows), default=now + BACKOFF_FIRST)
    stagger = sum(project_id.encode()) % 90
    floor = get_settings().rate_limit_min_wait_s
    # 見込み時刻より手前で起きると、もう1回待ち直すことになるので、切り上げて少し足す
    seconds = max(int((until - now).total_seconds()) + 2 + (stagger if floor >= 60 else 0), floor)
    return {"seconds": seconds, "until": until.isoformat(timespec="seconds"),
            "until_local": until.astimezone().strftime("%m-%d %H:%M"),
            "providers": [r["provider"] for r in rows], "source": rows[0]["source"] if rows else "backoff"}


def fmt_local(t: dt.datetime) -> str:
    return t.astimezone().strftime("%m-%d %H:%M")


class GuardedRunner:
    """実行担当を包み、利用上限の状態を見てから呼び出す。上限の間は呼び出さずに rate_limit を返す。"""

    def __init__(self, inner: Any, connect_fn: Callable[[], Any] = connect) -> None:
        self.inner = inner
        self.name = inner.name
        self._connect = connect_fn

    async def run(self, prompt: str, *, cwd: Path, **kw: Any) -> AgentResult:
        with self._connect() as conn:
            until = limited_until(conn, self.name)
        if until is not None:
            return AgentResult(ok=False, agent=self.name, model=kw.get("model", ""), error_kind="rate_limit", retry_at=until,
                               error=f"{self.name} は利用上限の解除待ち（見込み {fmt_local(until)}）。それまでは問い合わせない")
        res = await self.inner.run(prompt, cwd=cwd, **kw)
        with self._connect() as conn:
            if not res.ok and res.error_kind == "rate_limit":
                until = note_limit(conn, self.name, res.retry_at, res.error or "")
                res.retry_at = until
            elif res.ok:
                clear_limit(conn, self.name)
        return res


# ---------------------------------------------------------------------- 実行している PC のスリープ


def parse_sleep_seconds(log_text: str, since: dt.datetime, until: dt.datetime) -> int:
    """`pmset -g log` の出力から、期間内に PC がスリープしていた秒数を数える。"""
    import re

    asleep_from: dt.datetime | None = None
    total = 0.0
    for line in log_text.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ([+-]\d{4})\s+(Sleep|Wake|DarkWake) *\t", line)   # 「Wake Requests」などの別の種類の行は数えない
        if not m:
            continue
        at = dt.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d %H:%M:%S%z")
        if m.group(3) == "Sleep":
            asleep_from = asleep_from or at
        elif asleep_from is not None:
            total += max(0.0, (min(at, until) - max(asleep_from, since)).total_seconds())
            asleep_from = None
    if asleep_from is not None:
        total += max(0.0, (until - max(asleep_from, since)).total_seconds())
    return int(total)


def host_sleep_seconds(since: dt.datetime, until: dt.datetime | None = None) -> int:
    """期間内に、この PC がスリープしていた秒数（macOS だけ。調べられなければ 0）。"""
    import subprocess

    try:
        out = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return parse_sleep_seconds(out, since, until or dt.datetime.now(dt.UTC))
