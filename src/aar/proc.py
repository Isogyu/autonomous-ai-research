"""子プロセスの起動と回収（POSIX と Windows の両方）。

実行担当の CLI、コンテナのクライアント、実験のプロセスは、どれも孫プロセスを作る。時間切れやキャンセルのときは、
孫まで含めて止め、残っていないことを確かめる。

  POSIX   : 新しいセッション（＝プロセスグループ）で起動し、グループへ SIGTERM、応じなければ SIGKILL。
  Windows : 停止した状態で起動して Job Object に入れてから再開する。止めるときは Job ごと終了させる。
            Job Object はプロセスの管理であって、コードの隔離ではない（ファイルやネットワークは制限しない）。

Windows では `os.killpg` も POSIX のシグナルも使わない。イベントループは既定（Proactor）のままにする
（Selector のループは子プロセスを扱えない）。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Windows でプロセスの起動に要る環境変数。環境を絞って渡すときも、これは残す
_WINDOWS_ENV_KEYS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "NUMBER_OF_PROCESSORS",
                     "PROCESSOR_ARCHITECTURE")


def base_env() -> dict[str, str]:
    """絞った環境で子プロセスを起動するときの土台。PATH と、OS が起動に必要とする変数だけを持つ。"""
    if IS_WINDOWS:
        env = {k: v for k, v in os.environ.items() if k.upper() in _WINDOWS_ENV_KEYS}
        env["PATH"] = os.environ.get("PATH", "")
        return env
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


# ---------------------------------------------------------------------- 実行ファイルの解決

class LauncherError(RuntimeError):
    """CLI の実行ファイルを、安全に起動できる形で特定できない。"""


_NPM_SHIM_SCRIPT = re.compile(r'"%(?:~dp0|dp0%)\\?(node_modules\\[^"\r\n]+?\.(?:js|mjs|cjs))"', re.IGNORECASE)


def resolve_command(name: str, override: str = "") -> list[str]:
    """CLI の名前を、実際に起動する実行ファイル（と前置きの引数）にする。

    Windows では、npm が作る `.cmd` のランチャーを直接は起動しない。バッチファイルは cmd.exe を経由し、
    引数が cmd.exe の規則で解釈し直される（引数の分離が保てない）。`.cmd` の中身が npm の定型なら、
    その先の `node <script.js>` を直接起動する。定型でなければ、推測せずに失敗させる。
    """
    target = override or name
    found = shutil.which(target)
    if not found:
        raise LauncherError(f"{target} が見つからない")
    path = Path(found)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return [str(path)]
    exe = path.with_suffix(".exe")
    if exe.exists():
        return [str(exe)]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise LauncherError(f"{path} を読めない: {e}") from e
    m = _NPM_SHIM_SCRIPT.search(text)
    script = (path.parent / m.group(1).replace("\\", os.sep)) if m else None
    if not script or not script.exists():
        raise LauncherError(f"{path} はバッチファイルのランチャーで、起動先を特定できない。"
                            "実行ファイル（.exe）の場所を設定で指定する")
    node = path.parent / "node.exe"
    node_cmd = str(node) if node.exists() else shutil.which("node")
    if not node_cmd:
        raise LauncherError(f"{path} は node のスクリプトを起動するが、node が見つからない")
    return [node_cmd, str(script)]


# ---------------------------------------------------------------------- Windows の Job Object

if IS_WINDOWS:  # pragma: no cover - Windows でだけ実行される（CI の windows の経路で確かめる）
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_INFO, _BASIC_ACCOUNTING_INFO = 9, 1
    _PROCESS_ACCESS = 0x0001 | 0x0100 | 0x0800 | 0x1000      # TERMINATE | SET_QUOTA | SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
    CREATE_SUSPENDED, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x4, 0x200, 0x08000000

    def _winerr(what: str) -> OSError:
        return ctypes.WinError(ctypes.get_last_error(), what)

    class _Job:
        """終了時に中のプロセスをすべて止める Job Object。"""

        def __init__(self) -> None:
            self.handle = _k32.CreateJobObjectW(None, None)
            if not self.handle:
                raise _winerr("CreateJobObject")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(self.handle, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
                err = _winerr("SetInformationJobObject")
                self.close()
                raise err

        def adopt_and_resume(self, pid: int) -> None:
            """停止した状態で起動したプロセスを Job に入れてから再開する。入れる前に孫を作らせない。"""
            h = _k32.OpenProcess(_PROCESS_ACCESS, False, pid)
            if not h:
                raise _winerr("OpenProcess")
            try:
                if not _k32.AssignProcessToJobObject(self.handle, h):
                    raise _winerr("AssignProcessToJobObject")
                status = _ntdll.NtResumeProcess(h)
                if status != 0:
                    raise OSError(f"NtResumeProcess が失敗（NTSTATUS {status & 0xFFFFFFFF:#x}）")
            finally:
                _k32.CloseHandle(h)

        def active(self) -> int:
            info = _BasicAccounting()
            if not _k32.QueryInformationJobObject(self.handle, _BASIC_ACCOUNTING_INFO, ctypes.byref(info), ctypes.sizeof(info), None):
                raise _winerr("QueryInformationJobObject")
            return int(info.ActiveProcesses)

        def terminate(self) -> None:
            if self.handle:
                _k32.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            if self.handle:
                _k32.CloseHandle(self.handle)      # KILL_ON_JOB_CLOSE: 残っているプロセスがあれば、ここで止まる
                self.handle = None


# ---------------------------------------------------------------------- 起動と回収

@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    survivors: int = 0          # 回収のあとに残っていたプロセスの数（0 が正常。調べられなければ -1）


class ProcessTree:
    """起動した子プロセスと、その子孫。"""

    def __init__(self, proc: asyncio.subprocess.Process, job: Any = None) -> None:
        self.proc = proc
        self._job = job

    @classmethod
    async def start(cls, cmd: list[str], *, cwd: Path | str, env: dict[str, str] | None, stdin: int, stdout: int,
                    stderr: int) -> ProcessTree:
        if not IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                                        start_new_session=True)
            return cls(proc)
        job = _Job()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd), env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                creationflags=CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
        except BaseException:
            job.close()
            raise
        try:
            job.adopt_and_resume(proc.pid)
        except BaseException:
            proc.kill()                     # Job に入れられなかったプロセスを、再開せずに止める
            job.close()
            raise
        return cls(proc, job)

    def signal_stop(self, *, force: bool) -> None:
        """子孫ごと止める。POSIX は force=False で SIGTERM、True で SIGKILL。Windows は常に Job の終了。"""
        if IS_WINDOWS:
            self._job.terminate()
            return
        try:
            os.killpg(self.proc.pid, 9 if force else 15)
        except (ProcessLookupError, PermissionError):
            pass

    def survivors(self) -> int:
        """まだ動いている子孫の数（POSIX ではグループが残っていれば 1）。"""
        if IS_WINDOWS:
            try:
                return self._job.active()
            except OSError:
                return -1
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return -1
        return 1

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


async def run(cmd: list[str], *, cwd: Path | str, env: dict[str, str] | None = None, stdin_data: bytes | None = None,
              timeout_s: float, heartbeat: Callable[[], None] | None = None, poll_s: float = 10, grace_s: float = 10,
              merge_stderr: bool = False) -> ProcResult:
    """コマンドを実行して結果を返す。時間切れ・キャンセルでは、子孫ごと止めてから戻る（または例外を通す）。

    cmd は引数の並びで渡す。シェルは経由しない（文字列の連結で命令を組み立てない）。
    """
    started = time.monotonic()
    tree = await ProcessTree.start(
        cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE)
    comm = asyncio.ensure_future(tree.proc.communicate(stdin_data))
    timed_out = False
    last_beat = started
    try:
        while True:
            now = time.monotonic()
            done, _ = await asyncio.wait({comm}, timeout=max(0.05, min(poll_s, 1.0, timeout_s - (now - started))))
            if done:
                break
            if tree.proc.returncode is not None:
                # 親は終わったが、出力のパイプを握ったままの孫が残っている（パイプが閉じるまで communicate は戻らない）。
                # 孫を止めて、出力を受け取る
                tree.signal_stop(force=True)
                try:
                    await asyncio.wait_for(asyncio.shield(comm), timeout=10)
                except TimeoutError:
                    pass
                break
            now = time.monotonic()
            if heartbeat and now - last_beat >= poll_s:
                heartbeat()
                last_beat = now
            if now - started >= timeout_s:
                timed_out = True
                break
    finally:
        if not comm.done():
            for force in (False, True):
                tree.signal_stop(force=force)
                try:
                    await asyncio.wait_for(asyncio.shield(comm), timeout=grace_s if not force else 5)
                    break
                except (TimeoutError, asyncio.CancelledError):
                    continue
        else:
            tree.signal_stop(force=True)          # 親が終わっても、残した孫がいれば止める
        survivors = tree.survivors()
        for _ in range(20):                        # 終了の反映を少し待つ
            if survivors <= 0:
                break
            await _sleep_uncancelled(0.1)
            survivors = tree.survivors()
        tree.close()
    dur = time.monotonic() - started
    if timed_out or not comm.done() or comm.cancelled():
        return ProcResult(-1, "", "時間切れで停止した", True, dur, survivors)
    out, err = comm.result()
    return ProcResult(tree.proc.returncode or 0, (out or b"").decode("utf-8", errors="replace"),
                      (err or b"").decode("utf-8", errors="replace"), False, dur, survivors)


async def _sleep_uncancelled(seconds: float) -> None:
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        time.sleep(seconds)
