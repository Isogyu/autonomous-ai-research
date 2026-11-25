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
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from aar.config import display_tz, get_settings
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
            "until_local": fmt_local(until),
            "providers": [r["provider"] for r in rows], "source": rows[0]["source"] if rows else "backoff"}


def fmt_local(t: dt.datetime) -> str:
    """表示用の時刻（設定したタイムゾーン。未設定なら OS の設定）。保存する時刻は UTC のまま。"""
    return t.astimezone(display_tz()).strftime("%m-%d %H:%M")


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


# Windows: システムのイベントログ（Kernel-Power）。42 = スリープに入る、107 = スリープから復帰、
# 506 = モダンスタンバイに入る、507 = モダンスタンバイから出る。画面のロックや画面の消灯は、これらのイベントを出さない
_WIN_SLEEP_IDS, _WIN_WAKE_IDS = {42, 506}, {107, 507}


def parse_windows_power_events(xml_text: str, since: dt.datetime, until: dt.datetime) -> int:
    """`wevtutil qe System … /f:xml` の出力から、期間内にスリープしていた秒数を数える。"""
    import re

    events = []
    for block in re.findall(r"<Event\b.*?</Event>", xml_text, flags=re.S):
        eid = re.search(r"<EventID[^>]*>(\d+)</EventID>", block)
        at = re.search(r"<TimeCreated\s+SystemTime=['\"]([^'\"]+)['\"]", block)
        if not eid or not at:
            continue
        stamp = re.sub(r"(\.\d{6})\d*", r"\1", at.group(1)).replace("Z", "+00:00")
        try:
            events.append((dt.datetime.fromisoformat(stamp), int(eid.group(1))))
        except ValueError:
            continue
    asleep_from: dt.datetime | None = None
    total = 0.0
    for at, eid in sorted(events):
        if eid in _WIN_SLEEP_IDS:
            asleep_from = asleep_from or at
        elif eid in _WIN_WAKE_IDS and asleep_from is not None:
            total += max(0.0, (min(at, until) - max(asleep_from, since)).total_seconds())
            asleep_from = None
    if asleep_from is not None:
        total += max(0.0, (until - max(asleep_from, since)).total_seconds())
    return int(total)


@dataclass(frozen=True)
class SleepEvidence:
    """期間内にホストがスリープしていたかの証拠。調べられなかった場合は unknown で、「スリープなし」とは扱わない。"""

    status: str             # "slept"（スリープの記録がある）/ "awake"（記録を読めて、スリープがない）/ "unknown"（調べられない）
    seconds: int
    source: str
    detail: str = ""


def _is_wsl() -> bool:
    import platform

    return sys.platform == "linux" and "microsoft" in platform.release().lower()


def _windows_power_log(since: dt.datetime) -> tuple[str | None, str]:
    """Windows のイベントログから電源のイベントを読む。WSL の中からは、Windows 側の wevtutil.exe を呼ぶ。"""
    import shutil
    import subprocess

    exe = shutil.which("wevtutil.exe") or shutil.which("wevtutil")
    if not exe and _is_wsl():
        cand = "/mnt/c/Windows/System32/wevtutil.exe"
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return None, "wevtutil が見つからない"
    stamp = since.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ids = " or ".join(f"EventID={i}" for i in sorted(_WIN_SLEEP_IDS | _WIN_WAKE_IDS))
    query = f"*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and ({ids}) and TimeCreated[@SystemTime>='{stamp}']]]"
    try:
        r = subprocess.run([exe, "qe", "System", f"/q:{query}", "/f:xml", "/rd:false"], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"wevtutil を実行できない: {e}"
    if r.returncode != 0:
        return None, f"wevtutil が失敗（終了コード {r.returncode}）: {r.stderr.decode('utf-8', 'replace').strip()[:200]}"
    return r.stdout.decode("utf-8", "replace"), ""


def host_sleep_evidence(since: dt.datetime, until: dt.datetime | None = None) -> SleepEvidence:
    """期間内に、この PC（ホスト）がスリープしていたかを、OS の電源の記録で調べる。

    調べるのはホストのスリープだけ。処理の遅れや時刻の飛びからは推定しない。WSL や Podman の仮想環境だけが止まった場合は、
    ホストの電源の記録には現れない（別の事象として扱う）。電源の設定は読むだけで、変更しない。
    """
    import subprocess

    until = until or dt.datetime.now(dt.UTC)
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return SleepEvidence("unknown", 0, "pmset", str(e)[:200])
        if r.returncode != 0 or not r.stdout.strip():
            return SleepEvidence("unknown", 0, "pmset", f"終了コード {r.returncode}")
        seconds = parse_sleep_seconds(r.stdout, since, until)
        return SleepEvidence("slept" if seconds > 0 else "awake", seconds, "pmset")
    if sys.platform == "win32" or _is_wsl():
        source = "wevtutil（Kernel-Power 42/107/506/507）" + ("、WSL から Windows 側を参照" if _is_wsl() else "")
        text, problem = _windows_power_log(since)
        if text is None:
            return SleepEvidence("unknown", 0, source, problem)
        seconds = parse_windows_power_events(text, since, until)
        return SleepEvidence("slept" if seconds > 0 else "awake", seconds, source)
    return SleepEvidence("unknown", 0, "なし", "この OS では、ホストの電源の記録を読む方法を実装していない")


def host_sleep_seconds(since: dt.datetime, until: dt.datetime | None = None) -> int:
    """期間内に、この PC がスリープしていた秒数。調べられなかった場合も 0 を返すので、判定には host_sleep_evidence を使う。"""
    return host_sleep_evidence(since, until).seconds
