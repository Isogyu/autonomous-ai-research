"""実験コードの隔離実行。

研究エージェントが書いたコードは信頼しない。次のどれかで動かす（config/policy.yaml の safety.sandbox）。
  docker   : コンテナ内で実行。ネットワークなし、ルートは読み取り専用、作業領域だけ書き込み可。
  seatbelt : macOS の sandbox-exec で実行。ネットワークを禁じ、書き込みを作業領域と一時領域に限る。
             Docker イメージを置く余裕がない環境向けの代替（ファイルの読み取りは制限しきれない）。
  none     : 隔離なし。テスト専用。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from aar.config import SafetyPolicy


class RunResult(BaseModel):
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    sandbox: str = ""

    def tail(self, n: int = 3000) -> str:
        return (self.stdout[-n:] + ("\n--- stderr ---\n" + self.stderr[-n:] if self.stderr.strip() else "")).strip()


# 秘密情報を読ませない場所（seatbelt 用）
_DENY_READ = [".ssh", ".aws", ".gnupg", ".codex", ".claude", ".config/gh", ".docker", ".netrc",
              "Library/Keychains", "Library/Application Support/Google", "Library/Cookies"]


def seatbelt_profile(workspace: Path) -> str:
    home = Path.home()
    deny = "\n".join(f'(deny file-read* (subpath "{home / p}"))' for p in _DENY_READ)
    ws = workspace.resolve()
    return f"""(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{ws}") (subpath "/private/tmp/aar-sbx")
       (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty"))
{deny}
"""


def _clean_env(workspace: Path) -> dict[str, str]:
    """実験に渡す環境変数。API キーやトークンは一切渡さない。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workspace / ".home"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TMPDIR": "/private/tmp/aar-sbx",
        "MPLBACKEND": "Agg",
    }


def docker_image_tag(base: str, requirements: Path) -> str:
    req = requirements.read_bytes() if requirements.exists() else b""
    return "aar-exp:" + hashlib.sha256(base.encode() + b"\n" + req).hexdigest()[:16]


async def _communicate(cmd: list[str], cwd: Path, env: dict[str, str] | None, timeout_s: int,
                       heartbeat: Callable[[], None] | None) -> tuple[int, str, str, bool, float]:
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    comm = asyncio.ensure_future(proc.communicate())
    timed_out = False
    try:
        while True:
            done, _ = await asyncio.wait({comm}, timeout=10)
            if done:
                break
            if heartbeat:
                heartbeat()
            if time.monotonic() - started > timeout_s:
                timed_out = True
                break
    finally:
        if not comm.done():
            for sig in (15, 9):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(comm), timeout=5)
                    break
                except (TimeoutError, asyncio.CancelledError):
                    continue
    dur = time.monotonic() - started
    if timed_out or not comm.done():
        return -1, "", "時間切れで停止した", True, dur
    out, err = comm.result()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"), False, dur


async def ensure_docker_image(safety: SafetyPolicy, workspace: Path) -> str:
    """依存関係（requirements.txt）を入れた実験用イメージを用意する。導入時だけネットワークを使う。"""
    req = workspace / "requirements.txt"
    tag = docker_image_tag(safety.docker_image, req)
    code, *_ = await _communicate(["docker", "image", "inspect", tag], workspace, None, 60, None)
    if code == 0:
        return tag
    dockerfile = f"FROM {safety.docker_image}\nRUN pip install --no-cache-dir pytest\n"
    if req.exists() and req.read_text().strip():
        dockerfile += "COPY requirements.txt /tmp/requirements.txt\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
    build_dir = workspace / ".aar-build"
    build_dir.mkdir(exist_ok=True)
    try:
        (build_dir / "Dockerfile").write_text(dockerfile)
        if req.exists():
            shutil.copy(req, build_dir / "requirements.txt")
        code, out, err, _, _ = await _communicate(["docker", "build", "-q", "-t", tag, str(build_dir)],
                                                  workspace, None, 1200, None)
        if code != 0:
            raise RuntimeError(f"実験用イメージを作れない: {(err or out)[-500:]}")
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    return tag


async def run_in_sandbox(
    args: list[str], *, workspace: Path, safety: SafetyPolicy, timeout_s: int | None = None,
    heartbeat: Callable[[], None] | None = None, python: str | None = None,
) -> RunResult:
    """作業領域で `python <args...>` を隔離して実行する。"""
    workspace = workspace.resolve()
    timeout_s = timeout_s or safety.experiment_timeout_minutes * 60
    mode = safety.sandbox
    if mode == "docker":
        tag = await ensure_docker_image(safety, workspace)
        cmd = ["docker", "run", "--rm", "--network", safety.docker_network,
               "--memory", safety.docker_memory, "--cpus", str(safety.docker_cpus),
               "--pids-limit", "512", "--read-only", "--tmpfs", "/tmp:rw,size=512m",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0", "-e", "MPLBACKEND=Agg",
               "-v", f"{workspace}:/work", "-w", "/work", tag, "python", *args]
        env = None
    elif mode == "seatbelt":
        Path("/private/tmp/aar-sbx").mkdir(exist_ok=True)
        (workspace / ".home").mkdir(exist_ok=True)
        cmd = ["sandbox-exec", "-p", seatbelt_profile(workspace), python or sys.executable, *args]
        env = _clean_env(workspace)
    elif mode == "none":
        cmd = [python or sys.executable, *args]
        env = _clean_env(workspace) | {"TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    else:
        raise ValueError(f"未知のサンドボックス: {mode}")
    code, out, err, timed_out, dur = await _communicate(cmd, workspace, env, timeout_s, heartbeat)
    return RunResult(ok=code == 0 and not timed_out, returncode=code, stdout=out, stderr=err,
                     duration_s=dur, timed_out=timed_out, sandbox=mode)
