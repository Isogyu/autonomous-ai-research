"""Podman によるコンテナの実行と、隔離が実際に効いているかの検証。

`podman` を名前で明示して呼ぶ（`docker` の別名には頼らない）。接続先は設定で指定できる
（Windows のクライアントから Podman の仮想環境へ、WSL から Podman Desktop の環境へ、など）。

隔離の指定（ネットワークなし、読み取り専用、資源の上限、capability の削除、権限昇格の禁止、非 root）は
`hardening_flags` の 1 か所で組み立てる。`verify` は、同じ指定でコンテナを動かし、制約がコンテナの中で
実際に効いていることを確かめて記録する。検証に通っていない環境では、`require_verified` が実行を拒否する
（指定を受け付けても効かない構成がある。たとえば cgroups v1 の rootless では資源の上限が使えない）。

コンテナには名前を付けて起動し、終了・時間切れ・キャンセルのどの場合も、その名前で強制的に削除したうえで、
残っていないことを確かめる（クライアントのプロセスを止めても、コンテナは止まらない）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aar import proc

VERIFY_VERSION = 1


class ContainerError(RuntimeError):
    pass


class IsolationUnavailable(ContainerError):
    """求める隔離を、この環境では確かめられていない。弱い方式には切り替えない。"""


@dataclass(frozen=True)
class Engine:
    command: tuple[str, ...]            # 実際に起動する podman の実行ファイル（と前置きの引数）
    connection: str = ""                # `podman --connection <名前>`。空なら既定の接続

    @property
    def base(self) -> list[str]:
        return [*self.command, *(["--connection", self.connection] if self.connection else [])]

    def call(self, *args: str, timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        """短い管理用の呼び出し（情報の取得、削除など）。"""
        return subprocess.run([*self.base, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, input=input_text, check=False,
                              stdin=subprocess.DEVNULL if input_text is None else None)


def get_engine(binary: str = "", connection: str = "") -> Engine:
    from aar.config import get_settings

    s = get_settings()
    try:
        cmd = proc.resolve_command("podman", binary or s.podman_bin)
    except proc.LauncherError as e:
        raise IsolationUnavailable(f"Podman を起動できない: {e}") from e
    return Engine(tuple(cmd), connection or s.podman_connection)


def engine_info(engine: Engine) -> dict[str, Any]:
    """接続先の Podman の構成。rootless かどうかなどは、推測せずにここで調べた値を使う。"""
    r = engine.call("info", "--format", "json")
    if r.returncode != 0:
        raise IsolationUnavailable(f"podman info が失敗: {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        raw = json.loads(r.stdout)
    except ValueError as e:
        raise IsolationUnavailable("podman info の出力を読めない") from e
    host = raw.get("host") or {}
    return {
        "version": (raw.get("version") or {}).get("Version", ""),
        "rootless": bool((host.get("security") or {}).get("rootless")),
        "remote": bool(host.get("serviceIsRemote")),
        "cgroup_version": host.get("cgroupVersion", ""),
        "cgroup_controllers": sorted(host.get("cgroupControllers") or []),
        "os": host.get("os", ""), "arch": host.get("arch", ""), "kernel": host.get("kernel", ""),
        "runtime": (host.get("ociRuntime") or {}).get("name", ""),
        "connection": engine.connection,
    }


def image_id(engine: Engine, image: str) -> str:
    r = engine.call("image", "inspect", "--format", "{{.Id}}", image)
    return r.stdout.strip() if r.returncode == 0 else ""


def image_exists(engine: Engine, image: str) -> bool:
    return engine.call("image", "exists", image).returncode == 0


# ---------------------------------------------------------------------- 隔離の指定

@dataclass(frozen=True)
class Limits:
    memory: str = "2g"
    cpus: float = 2
    pids: int = 512
    tmpfs_mb: int = 512
    network: str = "none"


def user_flags(info: dict[str, Any], rootful_user: str) -> list[str]:
    """作業領域を書き込みでマウントするコンテナを、root 以外で動かす指定。

    rootless では、コンテナ内の利用者を Podman を動かしている利用者に対応させる（マウント先に書ける）。
    rootful では、設定した UID:GID で動かす。どちらも、実際に効くかは verify が確かめる。
    """
    if info["rootless"]:
        return ["--userns", "keep-id"]
    return ["--user", rootful_user]


def hardening_flags(limits: Limits, user: list[str]) -> list[str]:
    return [
        "--pull", "never",                                   # 実行時にレジストリへ取りに行かない
        "--network", limits.network,
        "--memory", limits.memory, "--memory-swap", limits.memory,
        "--cpus", str(limits.cpus), "--pids-limit", str(limits.pids),
        "--read-only", "--tmpfs", f"/tmp:rw,size={limits.tmpfs_mb}m",
        "--cap-drop", "all", "--security-opt", "no-new-privileges",
        *user,
        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0", "-e", "MPLBACKEND=Agg",
        "-e", "PYTHONIOENCODING=utf-8", "-e", "LANG=C.UTF-8",
    ]


EVAL_USER = ["--user", "65534:65534"]       # マウントを持たないコンテナ（改善候補の実行）は、常に nobody で動かす


# ---------------------------------------------------------------------- 実行

@dataclass
class ContainerResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    name: str
    container_id: str = ""
    removed: bool = True                # 実行のあと、コンテナが残っていないことを確かめたか
    client_survivors: int = 0
    cleanup_note: str = ""


def remove_container(engine: Engine, name: str) -> tuple[bool, str]:
    """名前のコンテナを強制的に削除し、残っていないことを確かめる。"""
    note = ""
    try:
        r = engine.call("rm", "--force", "--time", "0", "--ignore", name, timeout=60)
        if r.returncode != 0:
            note = (r.stderr or r.stdout).strip()[-200:]
        left = engine.call("ps", "--all", "--quiet", "--filter", f"name=^{name}$", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"削除を確かめられない: {e}"
    if left.returncode != 0:
        return False, f"残りを確かめられない: {(left.stderr or left.stdout).strip()[-200:]}"
    return (not left.stdout.strip()), note


async def run_container(engine: Engine, image: str, command: list[str], *, flags: list[str], cwd: Path,
                        stdin_data: bytes | None = None, timeout_s: float, heartbeat: Callable[[], None] | None = None,
                        kind: str = "run", poll_s: float = 10) -> ContainerResult:
    name = f"aar-{kind}-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="aar-cid-") as td:
        cidfile = Path(td) / "cid"
        cmd = [*engine.base, "run", "--rm", "--name", name, "--label", "aar.managed=1", "--cidfile", str(cidfile),
               *(["--interactive"] if stdin_data is not None else []), *flags, image, *command]
        removed, note, cid = False, "", ""
        try:
            res = await proc.run(cmd, cwd=cwd, env=None, stdin_data=stdin_data, timeout_s=timeout_s, heartbeat=heartbeat,
                                 poll_s=poll_s, grace_s=5)
        finally:
            # 正常終了・時間切れ・キャンセルのどれでも、コンテナを名前で止めて削除する
            try:
                cid = cidfile.read_text(encoding="utf-8").strip()
            except OSError:
                cid = ""
            removed, note = remove_container(engine, name)
    return ContainerResult(res.returncode, res.stdout, res.stderr, res.timed_out, res.duration_s, name, cid, removed,
                           res.survivors, note)


def managed_containers(engine: Engine) -> list[str]:
    """この基盤が起動して、まだ残っている実行用のコンテナ（異常終了のあとの見回り用）。"""
    r = engine.call("ps", "--all", "--filter", "label=aar.managed=1", "--format", "{{.Names}}")
    return [x for x in r.stdout.split() if x] if r.returncode == 0 else []


# ---------------------------------------------------------------------- 検証

_PROBE = r'''
import json, os, socket, sys
out = {}
def status(key):
    for line in open("/proc/self/status"):
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return ""
def read(path):
    try:
        return open(path).read().strip()
    except OSError as e:
        return "ERR:" + type(e).__name__
def can_write(path):
    try:
        with open(path, "w") as f:
            f.write("x")
        return True
    except OSError:
        return False
def can_connect():
    try:
        socket.create_connection(("1.1.1.1", 53), 3).close()
        return True
    except OSError:
        return False
out["uid"] = os.getuid()
out["cap_eff"] = status("CapEff")
out["cap_bnd"] = status("CapBnd")
out["no_new_privs"] = status("NoNewPrivs")
out["interfaces"] = sorted(n for _, n in socket.if_nameindex())
out["network_reachable"] = can_connect()
out["rootfs_writable"] = can_write("/usr/aar-probe") or can_write("/aar-probe")
out["tmp_writable"] = can_write("/tmp/aar-probe")
out["memory_max"] = read("/sys/fs/cgroup/memory.max")
out["pids_max"] = read("/sys/fs/cgroup/pids.max")
out["cpu_max"] = read("/sys/fs/cgroup/cpu.max")
out["stdin_echo"] = sys.argv[1] if len(sys.argv) > 1 else ""
if os.path.isdir("/probe-ro"):
    out["ro_read"] = read("/probe-ro/入力 ファイル.txt")
    out["ro_writable"] = can_write("/probe-ro/x")
if os.path.isdir("/probe-rw"):
    out["rw_writable"] = can_write("/probe-rw/出力 結果.txt")
sys.stderr.write("stderr-ok\n")
print(json.dumps(out, ensure_ascii=False))
sys.exit(7)
'''


def _memory_bytes(spec: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    spec = spec.strip().lower().rstrip("b")
    return int(float(spec[:-1]) * units[spec[-1]]) if spec[-1] in units else int(spec)


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)}


def _judge(p: dict[str, Any], limits: Limits, *, expect_nonroot: bool, mounts: bool, token: str) -> list[dict[str, Any]]:
    cpu = p.get("cpu_max", "").split()
    checks = [
        _check("ネットワークに出られない", p.get("network_reachable") is False and p.get("interfaces") == ["lo"], p.get("interfaces")),
        _check("ルートのファイルシステムに書けない", p.get("rootfs_writable") is False, p.get("rootfs_writable")),
        _check("一時領域には書ける", p.get("tmp_writable") is True, p.get("tmp_writable")),
        _check("有効な capability がない", bool(p.get("cap_eff")) and int(p["cap_eff"], 16) == 0, p.get("cap_eff")),
        _check("権限昇格の禁止が効いている", p.get("no_new_privs") == "1", p.get("no_new_privs")),
        _check("メモリの上限が効いている", p.get("memory_max", "").isdigit() and int(p["memory_max"]) <= _memory_bytes(limits.memory),
               p.get("memory_max")),
        _check("プロセス数の上限が効いている", p.get("pids_max") == str(limits.pids), p.get("pids_max")),
        _check("CPU の上限が効いている", len(cpu) == 2 and cpu[0].isdigit() and int(cpu[0]) <= limits.cpus * int(cpu[1]) + 1,
               p.get("cpu_max")),
        _check("引数と標準出力で日本語が通る", p.get("stdin_echo") == token, p.get("stdin_echo")),
    ]
    if expect_nonroot:
        checks.append(_check("root 以外で動いている", p.get("uid") not in (0, None), p.get("uid")))
    if mounts:
        checks += [
            _check("読み取り専用のマウントを読める（空白・日本語のパス）", p.get("ro_read") == token, p.get("ro_read")),
            _check("読み取り専用のマウントに書けない", p.get("ro_writable") is False, p.get("ro_writable")),
            _check("出力領域に書ける", p.get("rw_writable") is True, p.get("rw_writable")),
        ]
    return checks


async def _probe(engine: Engine, image: str, flags: list[str], cwd: Path, token: str) -> tuple[dict[str, Any], ContainerResult]:
    res = await run_container(engine, image, ["python", "-I", "-S", "-B", "-", token], flags=flags, cwd=cwd,
                              stdin_data=_PROBE.encode(), timeout_s=120, kind="verify", poll_s=1)
    try:
        return json.loads(res.stdout.strip().splitlines()[-1]), res
    except (ValueError, IndexError):
        return {}, res


def fingerprint(info: dict[str, Any], image: str, image_id_: str, limits: Limits, rootful_user: str) -> str:
    """検証の結果が当てはまる構成。接続先・版・イメージ・隔離の指定のどれかが変われば、検証をやり直す。"""
    body = {"v": VERIFY_VERSION, "info": info, "image": image, "image_id": image_id_, "limits": asdict(limits),
            "eval_flags": hardening_flags(limits, EVAL_USER), "experiment_flags": hardening_flags(limits, user_flags(info, rootful_user))}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Verification:
    fingerprint: str
    passed: bool
    profiles: dict[str, bool]
    checks: list[dict[str, Any]]
    info: dict[str, Any]
    image: str
    image_id: str
    at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())


async def verify(engine: Engine, image: str, limits: Limits, *, scratch: Path, rootful_user: str = "1000:1000") -> Verification:
    """この環境で、隔離の指定が実際に効くことを、コンテナを動かして確かめる。

    profile "eval"       : マウントなし・nobody。改善候補の実行に使う。
    profile "experiment" : 作業領域を書き込みでマウント。研究の実験・分析・テストに使う。
    """
    info = engine_info(engine)
    iid = image_id(engine, image)
    if not iid:
        raise IsolationUnavailable(f"イメージ {image} がない（先に取得しておく。検証と実行では取得しない）")
    token = "検証 トークン"
    checks: list[dict[str, Any]] = [_check("資源の上限を扱える構成（cgroups v2）", info["cgroup_version"] == "v2", info["cgroup_version"])]
    scratch.mkdir(parents=True, exist_ok=True)

    def tag(profile: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [c | {"profile": profile} for c in items]

    # eval: マウントなし
    p, res = await _probe(engine, image, hardening_flags(limits, EVAL_USER), scratch, token)
    eval_checks = _judge(p, limits, expect_nonroot=True, mounts=False, token=token) + [
        _check("終了コードと標準エラーを受け取れる", res.returncode == 7 and "stderr-ok" in res.stderr, {"code": res.returncode, "stderr": res.stderr[-200:]}),
        _check("実行のあとにコンテナが残らない", res.removed, res.cleanup_note or res.name)]
    # 時間切れ: クライアントを止めたあと、コンテナも残らない
    t = await run_container(engine, image, ["python", "-c", "import time; time.sleep(300)"], flags=hardening_flags(limits, EVAL_USER),
                            cwd=scratch, timeout_s=5, kind="verify", poll_s=1)
    eval_checks.append(_check("時間切れで止まり、コンテナもクライアントのプロセスも残らない",
                              t.timed_out and t.removed and t.client_survivors == 0,
                              {"timed_out": t.timed_out, "removed": t.removed, "client_survivors": t.client_survivors}))
    checks += tag("eval", eval_checks)

    # experiment: クライアント側のパスを、そのままマウントできるとは限らない。小さな読み書きで確かめる
    base = scratch / f"マウント 検証-{uuid.uuid4().hex[:8]}"
    ro, rw = base / "ro", base / "rw"
    ro.mkdir(parents=True)
    rw.mkdir()
    (ro / "入力 ファイル.txt").write_text(token, encoding="utf-8", newline="\n")
    flags = [*hardening_flags(limits, user_flags(info, rootful_user)), "-v", f"{ro}:/probe-ro:ro", "-v", f"{rw}:/probe-rw:rw"]
    p, res = await _probe(engine, image, flags, scratch, token)
    exp_checks = _judge(p, limits, expect_nonroot=True, mounts=True, token=token)
    written = rw / "出力 結果.txt"
    exp_checks += [
        _check("コンテナが書いたファイルが、クライアント側から読める", written.exists() and written.read_text(encoding="utf-8") == "x",
               str(written.exists())),
        _check("実行のあとにコンテナが残らない", res.removed, res.cleanup_note or res.name)]
    if not p:
        exp_checks.append(_check("マウントつきのコンテナを起動できる", False, (res.stderr or res.stdout)[-300:]))
    checks += tag("experiment", exp_checks)

    common_ok = all(c["passed"] for c in checks if "profile" not in c)
    profiles = {name: common_ok and all(c["passed"] for c in checks if c.get("profile") == name) for name in ("eval", "experiment")}
    return Verification(fingerprint(info, image, iid, limits, rootful_user), all(profiles.values()), profiles, checks, info, image, iid)


def _record_path(data_dir: Path, fp: str) -> Path:
    return data_dir / "podman" / f"verify-{fp[:16]}.json"


def save_verification(data_dir: Path, v: Verification) -> Path:
    path = _record_path(data_dir, v.fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(v), ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return path


def require_verified(engine: Engine, image: str, limits: Limits, profile: str, *, data_dir: Path,
                     rootful_user: str = "1000:1000") -> dict[str, Any]:
    """いまの構成で、profile の検証に通った記録があることを確かめる。なければ実行を拒否する。"""
    info = engine_info(engine)
    iid = image_id(engine, image)
    if not iid:
        raise IsolationUnavailable(f"イメージ {image} がない")
    fp = fingerprint(info, image, iid, limits, rootful_user)
    path = _record_path(data_dir, fp)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IsolationUnavailable("この Podman の構成では、隔離の検証（aar podman verify）をまだ行っていない。"
                                   "構成・イメージ・隔離の指定が変わると、検証のやり直しが要る") from None
    if rec.get("fingerprint") != fp or not (rec.get("profiles") or {}).get(profile):
        failed = [c["name"] for c in rec.get("checks", []) if not c["passed"] and c.get("profile") in (None, profile)]
        raise IsolationUnavailable(f"隔離の検証に通っていない（{profile}）: " + "、".join(failed))
    return {"engine": "podman", "fingerprint": fp, "verified_at": rec.get("at", ""), "image": image, "image_id": iid, **info}
