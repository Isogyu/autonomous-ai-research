"""実物の Podman での隔離の確認（AAR_PODMAN_TESTS=1 のときだけ実行）。

必要なもの: podman、取得済みのイメージ（AAR_TEST_IMAGE に、ダイジェストつきの Python のイメージを指定）。
AAR_REQUIRE_PODMAN=1 のときは、Podman やイメージが使えなければ skip ではなく失敗にする（skip を合格に数えないため）。
ここで確かめられるのは、テストを動かした環境の Podman についてだけ。別の環境（社内の Windows など）には当てはまらない。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from aar import container

pytestmark = pytest.mark.podman
IMAGE = os.environ.get("AAR_TEST_IMAGE", "")


def _unavailable(reason: str):
    if os.environ.get("AAR_REQUIRE_PODMAN") == "1":
        pytest.fail(f"Podman の実物の確認ができない: {reason}")
    pytest.skip(reason)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from aar import config
    from aar.improve import evalkit as ek

    if not shutil.which("podman"):
        _unavailable("podman がない")
    if "@sha256:" not in IMAGE:
        _unavailable("AAR_TEST_IMAGE に、ダイジェストつきのイメージを指定していない")
    data = tmp_path / "var データ"
    monkeypatch.setenv("AAR_DATA_DIR", str(data))
    monkeypatch.setenv("AAR_CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("AAR_IMPROVE_IMAGE", IMAGE)
    config.get_settings.cache_clear()
    engine = container.get_engine()
    if not container.image_exists(engine, IMAGE):
        _unavailable(f"イメージ {IMAGE} が取得されていない")

    class E:
        pass

    e = E()
    e.engine, e.data, e.ek, e.tmp = engine, data, ek, tmp_path
    yield e
    config.get_settings.cache_clear()


async def _verified(env, limits):
    v = await container.verify(env.engine, IMAGE, limits, scratch=env.data / "podman" / "scratch")
    container.save_verification(env.data, v)
    failed = [f"[{c.get('profile', '共通')}] {c['name']}: {c['detail']}" for c in v.checks if not c["passed"]]
    assert v.passed, "隔離の検証に通らない:\n" + "\n".join(failed) + "\n" + json.dumps(v.info, ensure_ascii=False)
    return v


async def test_isolation_is_effective_inside_the_container(env):
    """ネットワーク、読み取り専用、資源の上限、capability、権限昇格の禁止、非 root、マウント、時間切れ後の回収。"""
    v = await _verified(env, env.ek.PODMAN_LIMITS)
    names = {c["name"] for c in v.checks}
    for needed in ("ネットワークに出られない", "メモリの上限が効いている", "プロセス数の上限が効いている", "CPU の上限が効いている",
                   "有効な capability がない", "権限昇格の禁止が効いている", "root 以外で動いている", "読み取り専用のマウントに書けない",
                   "時間切れで止まり、コンテナもクライアントのプロセスも残らない", "コンテナが書いたファイルが、クライアント側から読める"):
        assert needed in names, needed
    assert container.managed_containers(env.engine) == []
    print("podman:", json.dumps(v.info, ensure_ascii=False))


PROBE_CANDIDATE = '''
import json, os, socket
def _try(fn):
    try:
        return fn()
    except BaseException as e:
        return "DENIED:" + type(e).__name__
def build_fix_context(findings, **kw):
    out = {
        "network": _try(lambda: socket.create_connection(("1.1.1.1", 53), 2) and "REACHED"),
        "write_root": _try(lambda: open("/usr/x", "w").write("x") and "WROTE"),
        "write_home": _try(lambda: open(os.path.expanduser("~root/x"), "w").write("x") and "WROTE"),
        "import_aar": _try(lambda: __import__("aar") and "IMPORTED"),
        "import_numpy": _try(lambda: __import__("numpy") and "IMPORTED"),
        "uid": os.getuid(),
        "kw": sorted(kw),
        "env": sorted(k for k in os.environ if k.endswith("_API_KEY") or "TOKEN" in k or "SECRET" in k or k.startswith(("AAR_", "GH_", "GITHUB", "ANTHROPIC", "OPENAI", "CLAUDE", "CODEX"))),
        "visible": sorted(p for p in ("/work", "/Users", "/home/runner", "/mnt/c", "/run/podman", "/var/run/docker.sock",
                                      "/run/user", "HOSTMARK") if os.path.exists(p.replace("HOSTMARK", "%s"))),
        "tmp_files": sorted(os.listdir("/tmp")),
    }
    return json.dumps(out)
'''


async def test_candidate_sees_only_its_own_inputs(env, monkeypatch):
    """候補のコードは、ネットワーク、外部の読み書き、基盤のコード、評価器、正解、認証情報のどれにも届かない。"""
    from aar.improve.targets import TARGETS

    await _verified(env, env.ek.PODMAN_LIMITS)
    mark = env.tmp / "host-secret.txt"                       # ホスト側にだけあるファイル（評価器や正解の代わり）
    mark.write_text("answer key", encoding="utf-8", newline="\n")
    monkeypatch.setenv("AAR_FAKE_API_KEY", "should-not-leak")
    cases = env.ek.synthetic_cases("t", 6000, 2)
    r = env.ek.evaluate(PROBE_CANDIDATE % str(mark), TARGETS["fix_context"], cases, isolation="podman", deny_read=[])
    assert r["status"] == "completed", r
    seen = json.loads(r["outputs"][cases[0]["id"]])
    assert str(seen["network"]).startswith("DENIED") and str(seen["write_root"]).startswith("DENIED"), seen
    assert str(seen["write_home"]).startswith("DENIED") and str(seen["import_aar"]).startswith("DENIED"), seen
    assert str(seen["import_numpy"]).startswith("DENIED"), seen                       # -S: 第三者のパッケージも読み込めない
    assert seen["uid"] == 65534 and seen["env"] == [] and seen["visible"] == [], seen
    assert seen["kw"] == ["budget_chars", "claims", "evidence"]                        # 正解（key）は渡らない
    assert seen["tmp_files"] == ["module.py"], seen                                    # 渡したのは候補のファイルだけ
    assert r["isolation"] == "podman" and r["isolation_detail"]["fingerprint"]
    # 現行方式と候補は、同じコンテナの条件で動く
    base = env.ek.evaluate("def build_fix_context(findings, **kw):\n    return '[]'\n", TARGETS["fix_context"], cases, isolation="podman", deny_read=[])
    assert base["isolation_detail"]["fingerprint"] == r["isolation_detail"]["fingerprint"]
    assert base["isolation_detail"]["image_id"] == r["isolation_detail"]["image_id"]
    assert container.managed_containers(env.engine) == []


async def test_second_target_is_evaluated_under_the_same_container_isolation(env):
    """2 つ目の対象（ログの抜粋）も、同じ隔離のコンテナで評価できる（入力は標準入力だけ）。"""
    from aar.improve.targets import TARGETS

    await _verified(env, env.ek.PODMAN_LIMITS)
    target, spec = TARGETS["log_excerpt"], env.ek.spec_for("log_excerpt")
    source = (Path(__file__).parents[1] / target.module_path).read_text(encoding="utf-8")
    cases = spec.synthetic_cases("t", spec.plan["budget_chars"], 7)
    r = env.ek.evaluate(source, target, cases, isolation="podman", deny_read=[])
    assert r["status"] == "completed" and r["isolation"] == "podman" and r["metrics"]["guard_failures"] == {}, r
    assert 0 < r["metrics"]["ref_resolution_micro"] < 1 and container.managed_containers(env.engine) == []


async def test_runaway_candidate_is_stopped_and_leaves_no_container(env):
    from aar.improve.targets import TARGETS

    await _verified(env, env.ek.PODMAN_LIMITS)
    loop = "def build_fix_context(findings, **kw):\n    while True:\n        pass\n"
    r = env.ek.run_variant(loop, TARGETS["fix_context"], env.ek.synthetic_cases("t", 6000, 1), isolation="podman", deny_read=[], timeout_s=5)
    assert r["status"] == "failed" and r["failure_kind"] == "timeout", r
    assert container.managed_containers(env.engine) == []
    fork = "import os\ndef build_fix_context(findings, **kw):\n    n = 0\n    while True:\n        if os.fork() == 0:\n            import time; time.sleep(30)\n        n += 1\n"
    r = env.ek.run_variant(fork, TARGETS["fix_context"], env.ek.synthetic_cases("t", 6000, 1), isolation="podman", deny_read=[], timeout_s=20)
    assert r["status"] != "completed" or json.loads(json.dumps(r))["results"][0]["error"], r      # プロセス数の上限で止まる
    assert container.managed_containers(env.engine) == []


async def test_experiment_runs_in_podman_with_only_the_workspace_writable(env):
    """研究の実験・分析・テストの経路（作業領域だけを書き込みでマウント）。"""
    from aar.config import SafetyPolicy
    from aar.sandbox import docker_image_tag, experiment_limits, run_in_sandbox

    safety = SafetyPolicy(docker_image=IMAGE, docker_memory="512m", docker_cpus=1, experiment_timeout_minutes=5)
    ws = env.tmp / "作業 領域" / "repo"
    ws.mkdir(parents=True)
    (ws / "run.py").write_text(
        "import json, os, socket, sys\n"
        "def t(fn):\n    try:\n        return fn()\n    except BaseException as e:\n        return 'DENIED:' + type(e).__name__\n"
        "out = {'net': t(lambda: socket.create_connection(('1.1.1.1', 53), 2) and 'REACHED'),\n"
        "       'outside': t(lambda: open('/usr/x', 'w').write('x') and 'WROTE'), 'uid': os.getuid(), 'arg': sys.argv[1]}\n"
        "open('結果.json', 'w', encoding='utf-8').write(json.dumps(out, ensure_ascii=False))\nprint('完了')\n", encoding="utf-8", newline="\n")
    # 検証していない構成（このイメージ）では、実行を拒否する
    with pytest.raises(container.IsolationUnavailable):
        await run_in_sandbox(["run.py", "日本語 引数"], workspace=ws, safety=safety)
    assert not (ws / "結果.json").exists()
    if not container.image_exists(env.engine, docker_image_tag(IMAGE, ws / "requirements.txt")):
        # 実験用のイメージの構築（pytest の導入）にはネットワークが要る。構築できない環境では、構築は別に確かめる
        from aar.sandbox import ensure_podman_image
        await ensure_podman_image(env.engine, safety, ws)
    tag = docker_image_tag(IMAGE, ws / "requirements.txt")
    v = await container.verify(env.engine, tag, experiment_limits(safety), scratch=env.data / "podman" / "scratch")
    container.save_verification(env.data, v)
    assert v.profiles["experiment"], [c for c in v.checks if not c["passed"]]
    r = await run_in_sandbox(["run.py", "日本語 引数"], workspace=ws, safety=safety)
    assert r.ok and r.sandbox == "podman" and "完了" in r.stdout, r
    out = json.loads((ws / "結果.json").read_text(encoding="utf-8"))
    assert out["net"].startswith("DENIED") and out["outside"].startswith("DENIED") and out["uid"] != 0 and out["arg"] == "日本語 引数", out
    t = await run_in_sandbox(["-c", "import time; time.sleep(120)"], workspace=ws, safety=safety, timeout_s=5)
    assert t.timed_out and not t.ok
    r = await run_in_sandbox(["-m", "pytest", "--version"], workspace=ws, safety=safety)            # テストの実行に使う pytest が入っている
    assert r.ok, r
    assert container.managed_containers(env.engine) == []


@pytest.mark.skipif(sys.platform == "win32", reason="PostgreSQL のコンテナの確認は、ctl の経路で別に行う")
def test_postgres_runs_under_podman_and_listens_on_loopback_only(env, monkeypatch):
    """PostgreSQL＋pgvector を Podman で起動する（Compose は使わない）。待機、接続、公開範囲、停止、volume。"""
    import psycopg

    from aar import config, ctl

    pg_image = os.environ.get("AAR_TEST_POSTGRES_IMAGE", "")
    if not pg_image or not container.image_exists(env.engine, pg_image):
        _unavailable("AAR_TEST_POSTGRES_IMAGE のイメージが取得されていない")
    monkeypatch.setenv("AAR_POSTGRES_IMAGE", pg_image)
    monkeypatch.setenv("AAR_POSTGRES_CONTAINER", "aar-postgres-test")
    monkeypatch.setenv("AAR_POSTGRES_VOLUME", "aar-pgdata-test")
    monkeypatch.setenv("AAR_POSTGRES_PORT", "55439")
    config.get_settings.cache_clear()
    s = config.get_settings()
    try:
        assert "作成" in ctl.postgres_up(s)
        url = "postgresql://aar:aar@127.0.0.1:55439/aar"
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("CREATE TABLE keep (v vector(3))")
            conn.execute("INSERT INTO keep VALUES ('[1,2,3]')")
            print("pgvector:", conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone())
        assert ctl.exposure({"PostgreSQL": 55439}) == []                                # ループバック以外からは届かない
        assert "停止" in ctl.postgres_stop(s) and not ctl._port_open("127.0.0.1", 55439)
        assert "既存" in ctl.postgres_up(s)                                              # 再起動しても、volume のデータが残る
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT count(*) FROM keep").fetchone()[0] == 1
    finally:
        env.engine.call("rm", "--force", "--volumes", "aar-postgres-test")
        env.engine.call("volume", "rm", "--force", "aar-pgdata-test")
