"""実験コードの隔離実行。

研究エージェントが書いたコードは信頼しない。次のどれかで動かす（config/policy.yaml の safety.sandbox）。
  docker   : コンテナ内で実行。ネットワークなし、ルートは読み取り専用、作業領域だけ書き込み可。
             方針ファイルの値の名前は歴史的なもので、意味は「コンテナ」。どのエンジンで動かすかは環境の設定
             （AAR_CONTAINER_ENGINE）で決まる。既定は podman。docker は旧環境（macOS）専用で、明示したときだけ使う。
             podman では、その構成で隔離の検証（aar podman verify）に通っていなければ実行を拒否する。
  seatbelt : macOS の sandbox-exec で実行（旧環境専用）。ネットワークを禁じ、書き込みを作業領域と一時領域に限る。
             ファイルの読み取りは制限しきれない。macOS 以外では使えず、ほかの方式に切り替えもしない。
  none     : 隔離なし。テスト専用。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from aar import container, proc
from aar.config import SafetyPolicy, get_settings


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
    return proc.base_env() | {
        "HOME": str(workspace / ".home"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TMPDIR": "/private/tmp/aar-sbx",
        "MPLBACKEND": "Agg",
    }


def docker_image_tag(base: str, requirements: Path) -> str:
    req = requirements.read_bytes() if requirements.exists() else b""
    return "aar-exp:" + hashlib.sha256(base.encode() + b"\n" + req).hexdigest()[:16]


async def _communicate(cmd: list[str], cwd: Path, env: dict[str, str] | None, timeout_s: int,
                       heartbeat: Callable[[], None] | None) -> tuple[int, str, str, bool, float]:
    r = await proc.run(cmd, cwd=cwd, env=env, timeout_s=timeout_s, heartbeat=heartbeat, grace_s=5)
    return r.returncode, r.stdout, r.stderr, r.timed_out, r.duration_s


def _build_context(base_image: str, workspace: Path, build_dir: Path, filename: str) -> None:
    req = workspace / "requirements.txt"
    spec = f"FROM {base_image}\nRUN pip install --no-cache-dir pytest\n"
    if req.exists() and req.read_text(encoding="utf-8").strip():
        spec += "COPY requirements.txt /tmp/requirements.txt\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
    (build_dir / filename).write_text(spec, encoding="utf-8", newline="\n")
    if req.exists():
        shutil.copy(req, build_dir / "requirements.txt")


def experiment_limits(safety: SafetyPolicy) -> container.Limits:
    return container.Limits(memory=safety.docker_memory, cpus=safety.docker_cpus, pids=512, tmpfs_mb=512, network=safety.docker_network)


async def ensure_podman_image(engine: container.Engine, safety: SafetyPolicy, workspace: Path) -> str:
    """依存関係（requirements.txt）を入れた実験用イメージを Podman で用意する。導入時だけネットワークを使う。"""
    tag = docker_image_tag(safety.docker_image, workspace / "requirements.txt")
    if container.image_exists(engine, tag):
        return tag
    with tempfile.TemporaryDirectory(prefix="aar-build-") as td:          # 構築の文脈は作業領域の外に置く
        build_dir = Path(td)
        _build_context(safety.docker_image, workspace, build_dir, "Containerfile")
        code, out, err, _, _ = await _communicate(
            [*engine.base, "build", "--quiet", "--tag", tag, "--file", str(build_dir / "Containerfile"), str(build_dir)],
            workspace, None, 1200, None)
    if code != 0:
        raise RuntimeError(f"実験用イメージを作れない: {(err or out)[-500:]}")
    return tag


async def ensure_docker_image(safety: SafetyPolicy, workspace: Path) -> str:
    """依存関係（requirements.txt）を入れた実験用イメージを用意する。導入時だけネットワークを使う。"""
    req = workspace / "requirements.txt"
    tag = docker_image_tag(safety.docker_image, req)
    code, *_ = await _communicate(["docker", "image", "inspect", tag], workspace, None, 60, None)
    if code == 0:
        return tag
    dockerfile = f"FROM {safety.docker_image}\nRUN pip install --no-cache-dir pytest\n"
    if req.exists() and req.read_text(encoding="utf-8").strip():
        dockerfile += "COPY requirements.txt /tmp/requirements.txt\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
    build_dir = workspace / ".aar-build"
    build_dir.mkdir(exist_ok=True)
    try:
        (build_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8", newline="\n")
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
        mode = get_settings().container_engine           # 方針の「docker」は「コンテナ」の意味。エンジンは環境の設定で決める
        if mode not in ("podman", "docker"):
            raise ValueError(f"未知のコンテナエンジン: {mode}")
    if mode == "podman":
        s = get_settings()
        engine = container.get_engine()
        limits = experiment_limits(safety)
        tag = await ensure_podman_image(engine, safety, workspace)
        # 検証に通っていない構成では実行しない（IsolationUnavailable）。弱い方式には切り替えない
        info = container.require_verified(engine, tag, limits, "experiment", data_dir=s.data_dir, rootful_user=s.podman_rootful_user)
        flags = [*container.hardening_flags(limits, container.user_flags(info, s.podman_rootful_user)),
                 "-v", f"{workspace}:/work:rw", "-w", "/work"]
        res = await container.run_container(engine, tag, ["python", *args], flags=flags, cwd=workspace, timeout_s=timeout_s,
                                            heartbeat=heartbeat, kind="exp")
        err = res.stderr if res.removed else res.stderr + f"\n実行のあとにコンテナ {res.name} が残った: {res.cleanup_note}"
        return RunResult(ok=res.returncode == 0 and not res.timed_out and res.removed, returncode=res.returncode, stdout=res.stdout,
                         stderr=err, duration_s=res.duration_s, timed_out=res.timed_out, sandbox="podman")
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
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            raise container.IsolationUnavailable("seatbelt は macOS 専用。この環境では使えない（別の方式へは切り替えない）")
        Path("/private/tmp/aar-sbx").mkdir(exist_ok=True)
        (workspace / ".home").mkdir(exist_ok=True)
        cmd = ["sandbox-exec", "-p", seatbelt_profile(workspace), python or sys.executable, *args]
        env = _clean_env(workspace)
    elif mode == "none":
        cmd = [python or sys.executable, *args]
        env = _clean_env(workspace) | {"TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir()}
    else:
        raise ValueError(f"未知のサンドボックス: {mode}")
    code, out, err, timed_out, dur = await _communicate(cmd, workspace, env, timeout_s, heartbeat)
    return RunResult(ok=code == 0 and not timed_out, returncode=code, stdout=out, stderr=err,
                     duration_s=dur, timed_out=timed_out, sandbox=mode)
