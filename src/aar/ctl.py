"""基盤の起動・停止・状態確認（OS に依らない入口）。

Windows では PowerShell の `scripts/aarctl.ps1`、WSL・Linux では `python -m aar.cli ctl …` から呼ぶ。
旧環境（macOS）の `scripts/aarctl`（Bash、Docker Compose）は、そのまま残してある。

  PostgreSQL : Podman のコンテナ（Compose は使わない。`podman` を直接呼ぶ）。公開は 127.0.0.1 だけ。
  Temporal   : temporal CLI の単体サーバー（履歴は data_dir のファイル）。
  worker, ui : この Python のプロセス。

扱うこと: 多重起動（排他のロック）、古い PID（PID と起動時刻の両方で本人かを確かめる）、異常終了のあとの片付け、
ワーカーの入れ替え、ログの保存（data_dir/log）。Windows のサービスとしての登録はしない。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil

from aar import container, proc
from aar.config import Settings, get_settings

SERVICES = ("temporal", "worker", "ui")


def _run_dir(s: Settings) -> Path:
    d = s.data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_dir(s: Settings) -> Path:
    d = s.data_dir / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------- 排他と PID

@contextmanager
def control_lock(s: Settings, wait_s: float = 30) -> Iterator[None]:
    """起動・停止の操作を、同時に 1 つだけにする。持ち主が死んだロックは引き取る。"""
    path = _run_dir(s) / "ctl.lock"
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps(_identity(os.getpid())).encode())
            os.close(fd)
            break
        except FileExistsError:
            owner = _read_json(path)
            if not owner or not _same_process(owner):
                path.unlink(missing_ok=True)          # 持ち主がいない（異常終了の残り）
                continue
            if time.monotonic() > deadline:
                raise RuntimeError(f"別の起動・停止の操作が進行中（pid {owner.get('pid')}）") from None
            time.sleep(0.5)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _identity(pid: int) -> dict[str, Any]:
    return {"pid": pid, "create_time": psutil.Process(pid).create_time()}


def _same_process(rec: dict[str, Any]) -> bool:
    """記録した PID が、いまも同じプロセスを指しているか。PID は再利用されるので、起動時刻も比べる。"""
    try:
        p = psutil.Process(int(rec["pid"]))
        return abs(p.create_time() - float(rec["create_time"])) < 2 and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, KeyError, ValueError, TypeError):
        return False


def _pidfile(s: Settings, name: str) -> Path:
    return _run_dir(s) / f"{name}.pid.json"


def service_state(s: Settings, name: str) -> dict[str, Any]:
    """running / stopped / stale（記録はあるが、そのプロセスはもういない）。"""
    rec = _read_json(_pidfile(s, name))
    if rec is None:
        return {"name": name, "state": "stopped"}
    if _same_process(rec):
        return {"name": name, "state": "running", "pid": rec["pid"], "started_at": rec.get("started_at", "")}
    return {"name": name, "state": "stale", "pid": rec.get("pid"), "started_at": rec.get("started_at", "")}


def _service_env() -> dict[str, str]:
    return os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def launch(s: Settings, name: str, cmd: list[str]) -> str:
    st = service_state(s, name)
    if st["state"] == "running":
        return f"{name}: 起動済み (pid {st['pid']})"
    note = ""
    if st["state"] == "stale":
        note = f"（前回の pid {st['pid']} は異常終了していた。記録を片付けた）"
        _pidfile(s, name).unlink(missing_ok=True)
    log = (_log_dir(s) / f"{name}.log").open("ab")
    try:
        kwargs: dict[str, Any] = ({"creationflags": 0x08000000 | 0x00000200} if proc.IS_WINDOWS      # NO_WINDOW | NEW_PROCESS_GROUP
                                  else {"start_new_session": True})
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(s.data_dir.parent),
                             env=_service_env(), **kwargs)
    finally:
        log.close()
    rec = _identity(p.pid) | {"cmd": cmd, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp = _pidfile(s, name).with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, _pidfile(s, name))
    return f"{name}: 起動 (pid {p.pid}){note}"


def _kill_tree(pid: int, grace_s: float) -> None:
    """プロセスと子孫を止める。穏やかな終了を待ち、残れば強制的に止める。"""
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    procs = [*root.children(recursive=True), root]
    for p in procs:
        try:
            p.terminate()                 # POSIX は SIGTERM、Windows は TerminateProcess
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace_s)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)


def stop_one(s: Settings, name: str, grace_s: float = 30) -> str:
    st = service_state(s, name)
    pidfile = _pidfile(s, name)
    if st["state"] == "stopped":
        return f"{name}: 停止済み"
    if st["state"] == "stale":
        pidfile.unlink(missing_ok=True)
        return f"{name}: 記録だけが残っていた（pid {st['pid']} は既に終了）。片付けた"
    rec = _read_json(pidfile) or {}
    if name == "worker":
        # ワーカーには、停止の依頼をファイルで伝える（実行中の工程を Temporal に返してから終わる）
        from aar.temporal.worker import stop_request_path

        stop_request_path(s.data_dir).write_text("stop", encoding="utf-8", newline="\n")
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and _same_process(rec):
            time.sleep(0.5)
        stop_request_path(s.data_dir).unlink(missing_ok=True)
    if _same_process(rec):
        _kill_tree(int(rec["pid"]), grace_s if name != "worker" else 5)
    pidfile.unlink(missing_ok=True)
    return f"{name}: 停止"


# ---------------------------------------------------------------------- PostgreSQL（Podman）

def postgres_up(s: Settings, timeout_s: int = 180) -> str:
    engine = container.get_engine()
    name = s.postgres_container
    if engine.call("container", "exists", name).returncode == 0:
        r = engine.call("start", name)
        if r.returncode != 0:
            raise RuntimeError(f"PostgreSQL のコンテナを起動できない: {(r.stderr or r.stdout).strip()[-300:]}")
        how = "既存のコンテナを起動"
    else:
        if not container.image_exists(engine, s.postgres_image):
            raise RuntimeError(f"イメージ {s.postgres_image} がない。会社で承認された取得先から、先に取得しておく（podman pull）")
        if engine.call("volume", "exists", s.postgres_volume).returncode != 0:
            r = engine.call("volume", "create", s.postgres_volume)
            if r.returncode != 0:
                raise RuntimeError(f"volume を作れない: {(r.stderr or r.stdout).strip()[-300:]}")
        os.environ.setdefault("POSTGRES_PASSWORD", os.environ.get("AAR_PG_PASSWORD", "aar"))   # 値は引数に書かず、環境から渡す
        r = engine.call(
            "run", "--detach", "--name", name, "--label", "aar.service=postgres", "--pull", "never",
            "-e", "POSTGRES_USER=aar", "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=aar",
            "--publish", f"127.0.0.1:{s.postgres_port}:5432",                      # ローカルにだけ公開する
            "--volume", f"{s.postgres_volume}:/var/lib/postgresql/data", "--shm-size", "256m", "--memory", "512m",
            "--health-cmd", "pg_isready -U aar -d aar", "--health-interval", "5s", "--health-timeout", "3s", "--health-retries", "20",
            s.postgres_image, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"PostgreSQL のコンテナを作れない: {(r.stderr or r.stdout).strip()[-300:]}")
        how = "コンテナを作成"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # 定期の healthcheck はタイマーの仕組みに依存するので、ここから直接実行して待つ
        if engine.call("healthcheck", "run", name).returncode == 0 and _port_open("127.0.0.1", s.postgres_port):
            return f"postgres: {how}（127.0.0.1:{s.postgres_port}）"
        time.sleep(2)
    raise RuntimeError("PostgreSQL が時間内に応答しない（podman logs で確かめる）")


def postgres_stop(s: Settings) -> str:
    engine = container.get_engine()
    r = engine.call("stop", "--time", "30", "--ignore", s.postgres_container, timeout=90)
    return "postgres: 停止" if r.returncode == 0 else f"postgres: 停止に失敗: {(r.stderr or r.stdout).strip()[-200:]}"


def postgres_status(s: Settings) -> str:
    try:
        engine = container.get_engine()
        r = engine.call("ps", "--all", "--filter", f"name=^{s.postgres_container}$", "--format", "{{.Names}}: {{.Status}} {{.Ports}}")
    except (container.ContainerError, OSError, subprocess.SubprocessError) as e:
        return f"postgres: 確かめられない（{e}）"
    return r.stdout.strip() or "postgres: コンテナがない"


# ---------------------------------------------------------------------- 接続先と公開範囲

def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def exposure(ports: dict[str, int]) -> list[str]:
    """このマシンの、ループバック以外のアドレスからサービスに届いてしまわないかを、実際に接続して確かめる。

    確かめられるのは、この環境（Windows、WSL、仮想環境のどれか 1 つ）の中から見た範囲だけ。ほかの層は、それぞれで実行する。
    """
    problems = []
    addrs = sorted({a.address for addrs in psutil.net_if_addrs().values() for a in addrs
                    if a.family == socket.AF_INET and not a.address.startswith("127.")})
    for label, port in ports.items():
        for addr in addrs:
            if _port_open(addr, port, 0.5):
                problems.append(f"{label}（ポート {port}）が {addr} で待ち受けている。127.0.0.1 だけにする")
    return problems


def _ports(s: Settings) -> dict[str, int]:
    return {"PostgreSQL": s.postgres_port, "Temporal": int(s.temporal_address.rsplit(":", 1)[1]), "Temporal UI": 8233, "UI": s.ui_port}


# ---------------------------------------------------------------------- 入口

def _py() -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "aar.cli"]


def _temporal_cmd(s: Settings) -> list[str]:
    host, port = s.temporal_address.rsplit(":", 1)
    db = s.temporal_db or str(s.data_dir / "temporal.db")
    cmd = [*proc.resolve_command("temporal"), "server", "start-dev", "--db-filename", db, "--ip", host, "--port", port,
           "--ui-port", "8233", "--log-level", "warn"]
    if s.temporal_namespace != "default":
        cmd += ["--namespace", s.temporal_namespace]
    return cmd


def _migrate() -> None:
    subprocess.run([*_py(), "migrate"], check=True, env=_service_env())


def start() -> list[str]:
    s = get_settings()
    out = []
    with control_lock(s):
        out.append(postgres_up(s))
        out.append(launch(s, "temporal", _temporal_cmd(s)))
        host, port = s.temporal_address.rsplit(":", 1)
        for _ in range(60):
            if _port_open(host, int(port)):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Temporal が時間内に応答しない（aar ctl logs temporal）")
        _migrate()
        out.append(launch(s, "worker", [*_py(), "worker"]))
        out.append(launch(s, "ui", [*_py(), "ui"]))
    out.append(f"UI: http://{s.ui_host}:{s.ui_port}  /  Temporal: http://127.0.0.1:8233")
    return out + [f"注意: {p}" for p in exposure(_ports(s))]


def stop(*, everything: bool = False) -> list[str]:
    s = get_settings()
    with control_lock(s):
        out = [stop_one(s, n) for n in ("ui", "worker", "temporal")]
        if everything:
            out.append(postgres_stop(s))
    return out


def restart_worker() -> list[str]:
    """コードや設定を変えたあとに、ワーカーだけを入れ替える。実行中の工程は打ち切られ、続きから自動でやり直される。"""
    s = get_settings()
    with control_lock(s):
        out = [stop_one(s, "worker")]
        _migrate()
        out.append(launch(s, "worker", [*_py(), "worker"]))
    return out


def status() -> list[str]:
    s = get_settings()
    label = {"running": "稼働中", "stopped": "停止", "stale": "異常終了（記録だけが残っている。start か stop で片付く）"}
    out = []
    for n in SERVICES:
        st = service_state(s, n)
        out.append(f"{n}: {label[st['state']]}" + (f" (pid {st['pid']})" if st.get("pid") else ""))
    out.append(postgres_status(s))
    try:
        left = container.managed_containers(container.get_engine())
        out.append("実行用のコンテナ: 残りなし" if not left else "実行用のコンテナが残っている: " + ", ".join(left))
    except (container.ContainerError, OSError, subprocess.SubprocessError) as e:
        out.append(f"実行用のコンテナ: 確かめられない（{e}）")
    for label_, port in _ports(s).items():
        out.append(f"{label_}: 127.0.0.1:{port} {'応答あり' if _port_open('127.0.0.1', port) else '応答なし'}")
    return out + [f"注意: {p}" for p in exposure(_ports(s))]


def logs(name: str = "worker", lines: int = 50) -> str:
    path = _log_dir(get_settings()) / f"{name}.log"
    if not path.exists():
        return f"{path} がない"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
