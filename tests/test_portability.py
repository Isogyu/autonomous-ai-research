"""OS に依らない部分の確認: プロセスの回収、Podman の呼び出しの組み立て（代役）、検証の記録、パスの漏れ、電源の記録、起動・停止。

ここのテストは、実物の Podman も Windows も使わない。Podman の実物での隔離は tests/test_podman_real.py、
Windows でだけ意味のある確認には windows_only の印が付いている。
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from aar import container, proc
from aar import workspace as wsmod

PY = sys.executable
FAKE = str(Path(__file__).with_name("fake_podman.py"))


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def _wait_gone(pids: list[int], timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_gone(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------- プロセスの回収

_SPAWNER = """
import subprocess, sys, time, os
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(os.getpid()) + " " + str(child.pid))
AFTER
"""


async def test_run_passes_arguments_stdin_and_unicode_without_a_shell(tmp_path):
    # 引数は並びのまま渡る（空白、引用符、% や & を含んでも、シェルに解釈されない）
    tricky = ['a b', '"q"', "%PATH%", "&& echo x", "日本語 の引数", "back\\slash"]
    code = "import sys, json; sys.stdout.buffer.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8')}).encode()); sys.stderr.write('e'); sys.exit(3)"
    work = tmp_path / "作業 領域"
    work.mkdir()
    r = await proc.run([PY, "-c", code, *tricky], cwd=work, stdin_data="入力 テキスト".encode(), timeout_s=60, poll_s=0.2)
    out = json.loads(r.stdout)
    assert out == {"argv": tricky, "stdin": "入力 テキスト"} and r.returncode == 3 and r.stderr == "e" and not r.timed_out
    assert r.survivors == 0


async def test_timeout_stops_the_child_and_the_grandchild(tmp_path):
    pidfile = tmp_path / "pids"
    r = await proc.run([PY, "-c", _SPAWNER.replace("AFTER", "time.sleep(120)"), str(pidfile)], cwd=tmp_path, timeout_s=3, poll_s=0.2, grace_s=2)
    pids = [int(x) for x in pidfile.read_text().split()]
    assert r.timed_out and r.returncode == -1
    assert _wait_gone(pids), "時間切れのあとに、子か孫のプロセスが残っている"
    assert r.survivors == 0


async def test_cancellation_stops_the_child_and_the_grandchild(tmp_path):
    pidfile = tmp_path / "pids"
    task = asyncio.ensure_future(proc.run([PY, "-c", _SPAWNER.replace("AFTER", "time.sleep(120)"), str(pidfile)], cwd=tmp_path,
                                          timeout_s=120, poll_s=0.2, grace_s=2))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if pidfile.exists() and len(pidfile.read_text().split()) == 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _wait_gone([int(x) for x in pidfile.read_text().split()]), "キャンセルのあとに、子か孫のプロセスが残っている"


async def test_grandchild_left_behind_by_a_finished_parent_is_stopped(tmp_path):
    pidfile = tmp_path / "pids"
    r = await proc.run([PY, "-c", _SPAWNER.replace("AFTER", "sys.exit(0)"), str(pidfile)], cwd=tmp_path, timeout_s=60, poll_s=0.2)
    assert r.returncode == 0 and not r.timed_out
    assert _wait_gone([int(x) for x in pidfile.read_text().split()]), "親が終わったあとに、孫のプロセスが残っている"


def test_batch_launchers_are_not_started_directly(tmp_path):
    # npm の定型のランチャー: 先の node のスクリプトを直接起動する
    bin_dir = tmp_path / "npm bin"
    script = bin_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// js")
    node = bin_dir / "node.exe"
    node.write_text("")
    shim = bin_dir / "codex.cmd"
    shim.write_text('@ECHO off\r\nSETLOCAL\r\nSET dp0=%~dp0\r\n"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    for f in (shim, node):
        f.chmod(0o755)
    assert proc.resolve_command("codex", str(shim)) == [str(node), str(script)]
    # 定型でないバッチファイルは、推測せずに拒否する（cmd.exe 経由では引数の分離を保てない）
    odd = bin_dir / "claude.cmd"
    odd.write_text("@echo off\r\nsomething %*\r\n")
    odd.chmod(0o755)
    with pytest.raises(proc.LauncherError):
        proc.resolve_command("claude", str(odd))
    # 同じ名前の .exe があれば、それを使う
    exe = bin_dir / "claude.exe"
    exe.write_text("")
    assert proc.resolve_command("claude", str(odd)) == [str(exe)]
    with pytest.raises(proc.LauncherError):
        proc.resolve_command("no-such-tool-aar")


# ---------------------------------------------------------------------- Podman の呼び出し（代役）

@pytest.fixture
def fake(tmp_path, monkeypatch):
    state = tmp_path / "fake state"
    state.mkdir()
    monkeypatch.setenv("FAKE_PODMAN_STATE", str(state))

    class F:
        engine = container.Engine((PY, FAKE), "社内 接続")
        dir = state

        def calls(self):
            p = state / "calls.jsonl"
            return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    return F()


LIMITS = container.Limits(memory="512m", cpus=1, pids=64, tmpfs_mb=64)


def test_hardening_flags_request_every_restriction():
    flags = container.hardening_flags(LIMITS, container.EVAL_USER)
    joined = " ".join(flags)
    for needed in ("--network none", "--read-only", "--cap-drop all", "--security-opt no-new-privileges", "--memory 512m",
                   "--memory-swap 512m", "--cpus 1", "--pids-limit 64", "--user 65534:65534", "--pull never", "--tmpfs /tmp:rw,size=64m"):
        assert needed in joined, needed
    assert "--privileged" not in joined and "--volume" not in joined and "-v " not in joined
    assert container.user_flags({"rootless": True}, "1000:1000") == ["--userns", "keep-id"]
    assert container.user_flags({"rootless": False}, "1000:1000") == ["--user", "1000:1000"]


async def test_container_is_named_removed_and_checked_after_every_run(fake, tmp_path, monkeypatch):
    res = await container.run_container(fake.engine, "img@sha256:x", ["python", "-c", "pass"], flags=container.hardening_flags(LIMITS, container.EVAL_USER),
                                        cwd=tmp_path, stdin_data="入力".encode(), timeout_s=30, poll_s=0.2)
    assert res.stdout == "入力" and res.removed and res.container_id == "c" * 64
    run, rm, ps = (next(c for c in fake.calls() if c[2] == verb) for verb in ("run", "rm", "ps"))
    assert run[:2] == ["--connection", "社内 接続"] and "podman" not in run            # 接続先を明示して呼ぶ
    assert run.index("--name") < run.index("img@sha256:x") and run[run.index("--name") + 1] == res.name
    assert res.name in rm and "--force" in rm and f"name=^{res.name}$" in ps
    # 時間切れ: クライアントを止めたあとも、同じ名前で削除して、残りを確かめる
    monkeypatch.setenv("FAKE_PODMAN_RUN", "sleep")
    t = await container.run_container(fake.engine, "img", ["x"], flags=[], cwd=tmp_path, timeout_s=2, poll_s=0.2)
    assert t.timed_out and t.removed and t.client_survivors == 0
    assert any(c[2] == "rm" and t.name in c for c in fake.calls())
    # 残っていたら、残ったと報告する
    monkeypatch.setenv("FAKE_PODMAN_RUN", "echo")
    (fake.dir / "leftover").write_text("deadbeef")
    left = await container.run_container(fake.engine, "img", ["x"], flags=[], cwd=tmp_path, timeout_s=30, poll_s=0.2)
    assert not left.removed


def _verification(engine, image, limits, profiles, checks=()):
    info = container.engine_info(engine)
    iid = container.image_id(engine, image)
    return container.Verification(container.fingerprint(info, image, iid, limits, "1000:1000"), all(profiles.values()), profiles, list(checks), info, image, iid)


def test_unverified_or_changed_configuration_is_refused(fake, tmp_path):
    image = "python@sha256:" + "1" * 64
    with pytest.raises(container.IsolationUnavailable, match="まだ行っていない"):
        container.require_verified(fake.engine, image, LIMITS, "eval", data_dir=tmp_path)
    container.save_verification(tmp_path, _verification(fake.engine, image, LIMITS, {"eval": True, "experiment": False},
                                                        [{"name": "出力領域に書ける", "passed": False, "detail": "", "profile": "experiment"}]))
    ok = container.require_verified(fake.engine, image, LIMITS, "eval", data_dir=tmp_path)
    assert ok["rootless"] is True and ok["engine"] == "podman" and ok["connection"] == "社内 接続"
    with pytest.raises(container.IsolationUnavailable, match="出力領域に書ける"):       # 通っていない profile は使えない
        container.require_verified(fake.engine, image, LIMITS, "experiment", data_dir=tmp_path)
    with pytest.raises(container.IsolationUnavailable):                                # 隔離の指定が変われば、検証し直す
        container.require_verified(fake.engine, image, container.Limits(memory="4g"), "eval", data_dir=tmp_path)
    info = {"version": {"Version": "5.4.0"}, "host": {"security": {"rootless": False}, "cgroupVersion": "v2", "os": "linux", "arch": "amd64"}}
    (fake.dir / "info.json").write_text(json.dumps(info))
    assert container.engine_info(fake.engine)["rootless"] is False                     # rootless かどうかは、接続先に問い合わせた値
    with pytest.raises(container.IsolationUnavailable):                                # 構成が変われば、検証し直す
        container.require_verified(fake.engine, image, LIMITS, "eval", data_dir=tmp_path)


def test_probe_results_are_judged_strictly():
    good = {"uid": 65534, "cap_eff": "0000000000000000", "no_new_privs": "1", "interfaces": ["lo"], "network_reachable": False,
            "rootfs_writable": False, "tmp_writable": True, "memory_max": str(512 * 1024**2), "pids_max": "64", "cpu_max": "100000 100000",
            "stdin_echo": "t"}
    judge = lambda p: {c["name"]: c["passed"] for c in container._judge(p, LIMITS, expect_nonroot=True, mounts=False, token="t")}  # noqa: E731
    assert all(judge(good).values())
    assert not judge(good | {"memory_max": "max"})["メモリの上限が効いている"]            # 指定を受け付けても、効いていなければ不合格
    assert not judge(good | {"pids_max": "max"})["プロセス数の上限が効いている"]
    assert not judge(good | {"cpu_max": "max 100000"})["CPU の上限が効いている"]
    assert not judge(good | {"network_reachable": True})["ネットワークに出られない"]
    assert not judge(good | {"interfaces": ["eth0", "lo"]})["ネットワークに出られない"]
    assert not judge(good | {"cap_eff": "00000000a80425fb"})["有効な capability がない"]
    assert not judge(good | {"no_new_privs": "0"})["権限昇格の禁止が効いている"]
    assert not judge(good | {"uid": 0})["root 以外で動いている"]
    assert not judge(good | {"rootfs_writable": True})["ルートのファイルシステムに書けない"]
    assert not any(judge({}).values())                                                  # 調べられなかった項目は、合格にしない


@pytest.fixture
def podman_settings(fake, tmp_path, monkeypatch):
    from aar import config

    monkeypatch.setenv("AAR_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("AAR_CONTAINER_ENGINE", "podman")
    monkeypatch.setattr(container, "get_engine", lambda *a, **k: fake.engine)
    config.get_settings.cache_clear()
    yield tmp_path / "var"
    config.get_settings.cache_clear()


async def test_experiments_do_not_fall_back_when_podman_is_not_verified(podman_settings, fake, tmp_path):
    from aar.config import SafetyPolicy
    from aar.sandbox import run_in_sandbox

    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(container.IsolationUnavailable):
        await run_in_sandbox(["-c", "print(1)"], workspace=ws, safety=SafetyPolicy())    # 方針の sandbox は docker（＝コンテナ）
    assert not any(c[2] == "run" for c in fake.calls())                                  # 検証なしでは、コンテナを起動していない
    with pytest.raises(container.IsolationUnavailable):                                  # seatbelt は macOS 以外で使えず、切り替えもしない
        if sys.platform == "darwin":
            raise container.IsolationUnavailable("macOS では対象外")
        await run_in_sandbox(["-c", "print(1)"], workspace=ws, safety=SafetyPolicy(sandbox="seatbelt"))


def test_candidate_evaluation_is_refused_without_a_pinned_verified_image(podman_settings, fake, monkeypatch):
    from aar import config
    from aar.improve import decide as dec
    from aar.improve import evalkit as ek
    from aar.improve.targets import TARGETS

    src = "def build_fix_context(findings, **kw):\n    return '[]'\n"
    cases = ek.synthetic_cases("t", 6000, 2)
    r = ek.evaluate(src, TARGETS["fix_context"], cases, isolation="podman", deny_read=[])
    assert r["status"] == "failed" and r["failure_kind"] == "isolation_unavailable" and "ダイジェスト" in r["error"]
    monkeypatch.setenv("AAR_IMPROVE_IMAGE", "python:3.12-slim")                         # タグだけの指定は、内容が固定されない
    config.get_settings.cache_clear()
    assert ek.evaluate(src, TARGETS["fix_context"], cases, isolation="podman", deny_read=[])["failure_kind"] == "isolation_unavailable"
    image = "python@sha256:" + "2" * 64
    monkeypatch.setenv("AAR_IMPROVE_IMAGE", image)
    config.get_settings.cache_clear()
    r = ek.evaluate(src, TARGETS["fix_context"], cases, isolation="podman", deny_read=[])
    assert r["failure_kind"] == "isolation_unavailable" and "検証" in r["error"]         # 検証していない構成では動かさない
    assert not any(c[2] == "run" for c in fake.calls())
    assert ek.isolation_available() != "podman"
    # 検証済みなら、候補のファイル・child.py・課題の入力を標準入力で渡し、マウントなしで起動する
    container.save_verification(podman_settings, _verification(fake.engine, image, ek.PODMAN_LIMITS, {"eval": True, "experiment": True}))
    monkeypatch.setenv("FAKE_PODMAN_STDOUT", json.dumps({"error": "", "results": [{"id": c["id"], "output": "[]", "again": "[]", "error": "", "ms": 1.0} for c in cases]}))
    r = ek.evaluate(src, TARGETS["fix_context"], cases, isolation="podman", deny_read=[])
    assert r["status"] == "completed" and r["isolation"] == "podman" and r["isolation_detail"]["image_id"].startswith("sha256:")
    run = next(c for c in fake.calls() if c[2] == "run")
    assert not any(a in ("-v", "--volume", "--mount") for a in run) and run[run.index("--network") + 1] == "none"
    assert "key" not in json.dumps(run) and "aar" not in run[run.index(image) + 1:][-1]  # 正解も基盤のコードも、引数に入れていない
    assert ek.isolation_available() == "podman"
    # 計画が求める隔離は podman。別プロセスだけの実行では、採否を決めない
    assert dec.DEFAULT_PLAN["required_isolation"] == "podman" and dec.DEFAULT_PLAN["max_mean_chars_ratio"] == 4.0
    assert ek.run_variant(src, TARGETS["fix_context"], cases, isolation="なし", deny_read=[])["failure_kind"] == "isolation_unavailable"


# ---------------------------------------------------------------------- パスの漏れ

LEAKS = ["see /Users/someone/project", "/home/taro/aar/x", r"C:\Users\山田 太郎\aar\x.py", "C:/Users/taro/x", r'{"p": "C:\\Users\\taro\\x"}',
         "/mnt/c/Users/taro/x", r"\\wsl.localhost\Ubuntu\home\taro\x", r"\\wsl$\Ubuntu\x", r"\\fileserver01\研究 共有\x",
         r"\\fs.corp.example\share$\x", "d:\\users\\TARO\\x", r'"\\\\fs01\\share\\x"']
CLEAN = ["/work/experiments/run.py", "~/x", r"a \\ b", r"\\hline", r"x \\\hline y", "http://example.com/Users", "C:\\Program Files\\x",
         "s3://bucket/home/", "https://x.org/home/page/", "x \\\\\ny \\\\\n", r"\\textbf{a}\\textit{b}\\"]


def test_local_paths_of_every_environment_are_detected_and_redacted():
    for text in LEAKS:
        assert wsmod.find_local_path(text), text
        red = wsmod.redact_local_paths(text)
        assert not wsmod.find_local_path(red), (text, red)
        for name in ("someone", "taro", "TARO", "山田", "fileserver01", "fs.corp", "fs01", "Ubuntu"):
            assert name not in red, (text, red)
    for text in CLEAN:
        assert not wsmod.find_local_path(text), text
        assert wsmod.redact_local_paths(text) == text


def test_secret_scan_reports_windows_and_wsl_paths(tmp_path):
    ws = tmp_path / "研究 ws"
    wsmod.init_workspace(ws, author_name="t", author_email="t@example.invalid")
    (ws / "notes.md").write_text("ログ: C:\\Users\\taro\\aar\\run.log\n", encoding="utf-8", newline="\n")
    (ws / "unc.md").write_text("置き場: \\\\fs01\\研究\\data\n", encoding="utf-8", newline="\n")
    wsmod.git(ws, "add", "-A")
    wsmod.git(ws, "commit", "-m", "x")
    hits = {h["path"].split(":")[0] for h in wsmod.scan_secrets(ws) if h["kind"] == "利用者のホームのパス"}
    assert hits == {"notes.md", "unc.md"}


def test_workspace_files_keep_lf_and_utf8_on_every_os(tmp_path):
    ws = tmp_path / "日本語 と 空白" / "ws"
    wsmod.init_workspace(ws, author_name="t", author_email="t@example.invalid")
    assert b"\r" not in (ws / ".gitignore").read_bytes()
    (ws / "データ.json").write_text('{"値": "あ"}\n', encoding="utf-8", newline="\n")
    wsmod.git(ws, "add", "-A")
    wsmod.git(ws, "commit", "-m", "日本語のコミット")
    assert "データ.json" in wsmod.git(ws, "ls-files")                                     # 日本語のファイル名が、エスケープされずに返る
    assert wsmod.git(ws, "show", "HEAD:データ.json") == '{"値": "あ"}'                    # Git を通しても、改行と文字が変わらない
    assert (ws / "データ.json").read_bytes() == '{"値": "あ"}\n'.encode()
    assert wsmod.git(ws, "status", "--porcelain") == ""


def test_source_writes_text_as_utf8_with_lf():
    """基盤が書くファイルは、OS に依らず同じバイト列にする（Windows の既定は cp932 と CRLF）。"""
    bad = []
    for path in sorted((Path(__file__).parents[1] / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("write_text", "read_text"):
                kws = {k.arg for k in node.keywords}
                if "encoding" not in kws and not (len(node.args) >= (2 if node.func.attr == "write_text" else 1)):
                    bad.append(f"{path.name}:{node.lineno} encoding")
                if node.func.attr == "write_text" and "newline" not in kws:
                    bad.append(f"{path.name}:{node.lineno} newline")
    assert not bad, bad


def test_repository_does_not_convert_line_endings():
    root = Path(__file__).parents[1]
    assert "* -text" in (root / ".gitattributes").read_text(encoding="utf-8")
    for rel in ("src/aar/improve/child.py", "src/aar/db/migrations/0001_initial.sql", "config/policy.yaml"):
        assert b"\r\n" not in (root / rel).read_bytes(), rel                              # ハッシュで固定するファイルに CRLF がない


# ---------------------------------------------------------------------- 電源の記録

_EVENT = ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-Kernel-Power'/>"
          "<EventID>{id}</EventID><TimeCreated SystemTime='{at}'/></System></Event>")


def test_windows_power_events_are_counted_and_missing_evidence_is_unknown(monkeypatch):
    from aar import limits

    at = lambda h, m: dt.datetime(2026, 11, 26, h, m, tzinfo=dt.UTC)  # noqa: E731
    xml = "".join(_EVENT.format(id=i, at=t) for i, t in (
        (42, "2026-11-26T12:00:00.0000000Z"), (107, "2026-11-26T12:16:00.0000000Z"),       # スリープ 16 分
        (506, "2026-11-26T12:30:00.000Z"), (507, "2026-11-26T12:40:00.000Z"),              # モダンスタンバイ 10 分
        (41, "2026-11-26T12:45:00.000Z"),                                                  # 関係のないイベントは数えない
        (42, "2026-11-26T13:30:00.000Z")))                                                 # まだ復帰していない
    f = limits.parse_windows_power_events
    assert f(xml, at(11, 0), at(13, 0)) == 26 * 60
    assert f(xml, at(12, 10), at(12, 35)) == 6 * 60 + 5 * 60                               # 期間の端で切る
    assert f(xml, at(13, 0), at(14, 0)) == 30 * 60
    assert f("", at(10, 0), at(11, 0)) == 0
    # 記録を読めないときは unknown。「スリープなし」とは扱わない
    monkeypatch.setattr(limits.sys, "platform", "win32")
    monkeypatch.setattr(limits, "_windows_power_log", lambda since: (None, "アクセスが拒否された"))
    ev = limits.host_sleep_evidence(at(11, 0), at(13, 0))
    assert ev.status == "unknown" and "拒否" in ev.detail
    monkeypatch.setattr(limits, "_windows_power_log", lambda since: (xml, ""))
    assert limits.host_sleep_evidence(at(11, 0), at(13, 0)) == limits.SleepEvidence("slept", 26 * 60, ev.source)
    monkeypatch.setattr(limits, "_windows_power_log", lambda since: ("", ""))
    assert limits.host_sleep_evidence(at(11, 0), at(13, 0)).status == "awake"
    monkeypatch.setattr(limits.sys, "platform", "freebsd")
    assert limits.host_sleep_evidence(at(11, 0)).status == "unknown"                       # 調べる方法のない OS


@pytest.mark.windows_only
def test_windows_power_log_is_readable_and_macos_tools_are_not_called():
    import shutil

    from aar import limits

    assert not shutil.which("pmset") and not shutil.which("caffeinate")
    ev = limits.host_sleep_evidence(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10))
    assert ev.status in ("awake", "slept"), ev                                             # この Windows では、電源の記録を読める


@pytest.mark.windows_only
def test_windows_event_loop_supports_subprocesses():
    loop = asyncio.new_event_loop()
    try:
        assert type(loop).__name__ == "ProactorEventLoop"
    finally:
        loop.close()


# ---------------------------------------------------------------------- 起動・停止

@pytest.fixture
def ctl_settings(tmp_path, monkeypatch):
    from aar import config

    monkeypatch.setenv("AAR_DATA_DIR", str(tmp_path / "var 領域"))
    config.get_settings.cache_clear()
    s = config.get_settings()
    s.data_dir.mkdir(parents=True)
    yield s
    config.get_settings.cache_clear()


def test_services_are_started_once_and_stale_pids_are_not_trusted(ctl_settings):
    from aar import ctl

    s = ctl_settings
    cmd = [PY, "-c", _SPAWNER.replace("AFTER", "print('起動した', flush=True); time.sleep(120)"), str(s.data_dir / "pids")]
    assert "起動 (pid" in ctl.launch(s, "ui", cmd)
    assert "起動済み" in ctl.launch(s, "ui", cmd)                                          # 多重起動しない
    st = ctl.service_state(s, "ui")
    assert st["state"] == "running"
    for _ in range(100):
        if (s.data_dir / "pids").exists() and len((s.data_dir / "pids").read_text().split()) == 2:
            break
        time.sleep(0.1)
    pids = [int(x) for x in (s.data_dir / "pids").read_text().split()]
    assert "起動した" in ctl.logs("ui")                                                    # 出力はログに残る
    assert ctl.stop_one(s, "ui", grace_s=5) == "ui: 停止"
    assert _wait_gone(pids), "停止のあとに、サービスの子孫のプロセスが残っている"
    assert ctl.service_state(s, "ui")["state"] == "stopped" and ctl.stop_one(s, "ui") == "ui: 停止済み"
    # 古い PID: 同じ番号の別のプロセスが動いていても、本人とはみなさない（止めもしない）
    me = psutil.Process(os.getpid())
    ctl._pidfile(s, "worker").write_text(json.dumps({"pid": me.pid, "create_time": me.create_time() - 3600}))
    assert ctl.service_state(s, "worker")["state"] == "stale"
    assert "片付けた" in ctl.stop_one(s, "worker") and psutil.pid_exists(me.pid)
    ctl._pidfile(s, "worker").write_text(json.dumps({"pid": me.pid, "create_time": me.create_time() - 3600}))
    out = ctl.launch(s, "worker", [PY, "-c", "import time; time.sleep(60)"])
    assert "異常終了していた" in out
    ctl.stop_one(s, "worker", grace_s=2)


def test_control_lock_is_exclusive_and_recovers_from_a_dead_owner(ctl_settings):
    from aar import ctl

    s = ctl_settings
    with ctl.control_lock(s):
        with pytest.raises(RuntimeError, match="進行中"):
            with ctl.control_lock(s, wait_s=0.5):
                pass
    (ctl._run_dir(s) / "ctl.lock").write_text(json.dumps({"pid": 2**22 + 12345, "create_time": 1.0}))   # 持ち主がいないロック
    with ctl.control_lock(s, wait_s=1):
        pass
