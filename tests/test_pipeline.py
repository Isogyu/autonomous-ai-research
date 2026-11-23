"""パイプライン全体のテスト（モック実行）。

実際の Temporal（開発用サーバー）と PostgreSQL＋pgvector を使い、エージェントだけを台本に置き換える。
ここで確かめるのは流れと記録であり、研究の中身の質ではない。DB 上もモック実行として記録される。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def stack(db_url, tmp_path, monkeypatch):
    """Temporal の開発用サーバーとワーカーを立て、設定をテスト用に向ける。ワーカーは止めて起動し直せる。"""
    from datetime import timedelta

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from aar import config

    policy = tmp_path / "policy.yaml"
    policy.write_text((ROOT / "config" / "policy.yaml").read_text(encoding="utf-8")
                      .replace("sandbox: docker ", "sandbox: none ").replace("macos: true", "macos: false"), encoding="utf-8")
    skill = tmp_path / "skills" / "independent-research-audit"
    (skill / "assets").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# テスト用の監査 Skill の代わり\n", encoding="utf-8")
    pub_skill = tmp_path / "skills" / "repo-publication-audit"
    install_publication_skill(pub_skill)
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which("temporal"))
    for k, v in {"AAR_DATABASE_URL": db_url, "AAR_MODE": "mock", "AAR_POLICY_PATH": str(policy),
                 "AAR_WORKSPACES_DIR": str(tmp_path / "ws"), "AAR_DATA_DIR": str(tmp_path / "var"),
                 "AAR_RESEARCH_AUDIT_SKILL_DIR": str(skill), "AAR_PUBLICATION_AUDIT_SKILL_DIR": str(pub_skill),
                 "AAR_EMBEDDING_MODEL": "hash", "AAR_TEMPORAL_ADDRESS": env.client.service_client.config.target_host,
                 "AAR_TEMPORAL_NAMESPACE": env.client.namespace, "AAR_TASK_QUEUE": "aar-test"}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    (tmp_path / "var").mkdir()

    from aar.temporal.activities import ALL_ACTIVITIES
    from aar.temporal.workflows import ALL_WORKFLOWS

    state: dict = {"env": env, "policy": policy, "tmp": tmp_path, "pub_skill": pub_skill, "task": None, "worker": None}

    async def start_worker() -> None:
        state["worker"] = Worker(env.client, task_queue="aar-test", workflows=ALL_WORKFLOWS, activities=ALL_ACTIVITIES,
                                 graceful_shutdown_timeout=timedelta(seconds=0))
        state["task"] = asyncio.create_task(state["worker"].run())

    async def stop_worker() -> None:
        if state["worker"] is not None:
            await state["worker"].shutdown()
            await asyncio.gather(state["task"], return_exceptions=True)
            state["worker"] = None

    state["start_worker"], state["stop_worker"] = start_worker, stop_worker
    await start_worker()
    yield state
    await stop_worker()
    await env.shutdown()
    config.get_settings.cache_clear()


def install_publication_skill(path: Path) -> None:
    """公開品質監査 Skill の代わり（ファイルの有無だけを見るため、中身は空に近い）。"""
    (path / "references").mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text("# テスト用の公開品質監査 Skill の代わり\n", encoding="utf-8")
    for name in ("rubric.md", "security.md", "report.md"):
        (path / "references" / name).write_text("# テスト用\n", encoding="utf-8")


async def _proposal(stack) -> str:
    from aar import service
    from aar.db.core import connect, fetch_all

    ing = await service.run_now("ingest", "2026-11-23")
    assert ing["inserted"] == 2 and ing["scored"] == 2
    again = await service.run_now("ingest", "2026-11-23")
    assert again["inserted"] == 0                                        # 同じ日の再収集で重複しない
    out = await service.run_now("proposals", "2026-W47")
    assert out["created"] == 2
    assert (await service.run_now("proposals", "2026-W47"))["created"] == 0   # 同じ週の再生成で重複しない
    with connect() as conn:
        rows = fetch_all(conn, "SELECT id, execution_mode, body FROM research_proposals ORDER BY created_at")
    assert all(r["execution_mode"] == "mock" for r in rows)
    assert {"hypotheses", "value_if_negative", "risks", "story"} <= set(rows[0]["body"])
    return str(rows[0]["id"])


async def _wait(project_id: str, states: tuple[str, ...], timeout: float = 120) -> dict:
    from aar import store
    from aar.db.core import connect

    for _ in range(int(timeout * 2)):
        with connect() as conn:
            p = store.get_project(conn, project_id)
        if p["status"] in states:
            return p
        await asyncio.sleep(0.5)
    raise AssertionError(f"{project_id} が {states} にならない（現在 {p['status']} / {p['phase']} / {p['outcome_reason']}）")


async def test_end_to_end_with_fix_and_reaudit(stack):
    from aar import service, workspace
    from aar.db.core import connect, fetch_all, fetch_one

    proposal_id = await _proposal(stack)
    project = await service.select_and_start(proposal_id)
    assert (await service.select_and_start(proposal_id))["id"] == project["id"]   # 2回選んでも研究は1つ
    pid = project["id"]
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable", p["outcome_reason"]
    assert p["execution_mode"] == "mock"                                  # モックの成功は実際の成功と区別される

    with connect() as conn:
        audits = fetch_all(conn, "SELECT kind, round, auditor, implementer, execution_mode FROM audit_runs WHERE project_id = %s ORDER BY started_at", (pid,))
        gates = fetch_all(conn, "SELECT gate, round, passed FROM gate_evaluations WHERE project_id = %s ORDER BY id", (pid,))
        finding = fetch_one(conn, "SELECT severity, status, resolved_in_round FROM audit_findings WHERE project_id = %s AND issue_id = 'I001'", (pid,))
        hyp = fetch_one(conn, "SELECT status, kind FROM hypotheses WHERE project_id = %s", (pid,))
        results = fetch_all(conn, "SELECT analysis_kind FROM evaluation_results WHERE project_id = %s", (pid,))
        mains = fetch_all(conn, "SELECT name, status FROM experiments WHERE project_id = %s AND kind = 'main' ORDER BY name", (pid,))
        prereg = fetch_one(conn, "SELECT * FROM preregistrations WHERE project_id = %s", (pid,))
        drafts = {r["kind"]: r["status"] for r in fetch_all(conn, "SELECT kind, status FROM publication_drafts WHERE project_id = %s", (pid,))}
        events = fetch_all(conn, "SELECT detail->>'key' AS k, count(*) AS n FROM workflow_events WHERE project_id = %s AND event = 'phase_completed' GROUP BY 1", (pid,))
        lessons = fetch_one(conn, "SELECT count(*) AS n FROM lessons_learned WHERE project_id = %s", (pid,))
        reusable = fetch_one(conn, "SELECT count(*) AS n FROM research_artifacts WHERE project_id = %s AND reusable", (pid,))
        notes = fetch_all(conn, "SELECT title FROM notifications WHERE project_id = %s", (pid,))
        approval = fetch_one(conn, "SELECT decision FROM publication_approvals WHERE project_id = %s", (pid,))

    # 1回目の監査が分析の誤りを再計算で見つけ、修正・再実験・再監査で解消された
    assert [(a["kind"], a["round"]) for a in audits] == [("research", 1), ("research", 2), ("publication_quality", 1),
                                                          ("publication_quality", 2)]
    assert all(a["auditor"].split(":")[0] != a["implementer"].split(":")[0] and a["execution_mode"] == "mock" for a in audits)
    assert [(g["gate"], g["round"], g["passed"]) for g in gates] == [
        ("research", 1, False), ("research", 2, True), ("publication", 1, False), ("publication", 2, True)]
    assert finding == {"severity": "Major", "status": "resolved", "resolved_in_round": 2}

    # 公開品質監査は正式な Skill で行われ、Major の指摘 → 修正 → 再監査で解消された。証跡が残っている
    with connect() as conn:
        pubs = fetch_all(conn, "SELECT round, skill, status, score, publication_verdict, target_commit, report_uri, detail "
                               "FROM audit_runs WHERE project_id = %s AND kind = 'publication_quality' ORDER BY round", (pid,))
        p101 = fetch_one(conn, "SELECT severity, status, resolved_in_round FROM audit_findings WHERE project_id = %s AND issue_id = 'P101'", (pid,))
        fix = fetch_one(conn, "SELECT result FROM side_effects WHERE idempotency_key = %s", (f"{pid}:fix:publication_quality:1",))
    assert [(a["publication_verdict"], a["status"]) for a in pubs] == [("C", "completed"), ("A", "completed")]
    assert all(a["skill"].startswith("repo-publication-audit@") and a["detail"]["valid"] for a in pubs)
    assert pubs[0]["target_commit"] != pubs[1]["target_commit"] and len(pubs[1]["target_commit"]) == 40
    assert len(pubs[1]["detail"]["categories"]) == 7 and pubs[1]["detail"]["total_confirmed"] == float(pubs[1]["score"]) == 98
    assert pubs[1]["detail"]["total_pending"] == 1 and pubs[1]["detail"]["linkedin_verdict"] == "投稿可"
    record = Path(pubs[1]["report_uri"])
    assert {"prompt.md", "skill_report.md", "structured_report.json", "publication_quality_audit.md"} <= {f.name for f in record.iterdir()}
    assert p101 == {"severity": "Major", "status": "resolved", "resolved_in_round": 2}
    assert fix["result"]["research_changed"] is False                      # 研究内容に及ばない修正では、研究監査へ戻らない
    assert [m["name"] for m in mains] == ["main-1", "main-2"]
    assert hyp == {"status": "supported", "kind": "preregistered"}
    assert {r["analysis_kind"] for r in results} == {"confirmatory", "exploratory"}   # 仮説検証と探索的分析を区別して保存
    assert all(e["n"] == 1 for e in events)                                # 各工程の完了は1回だけ
    assert set(drafts) >= {"report_md", "report_html", "readme", "talk_script", "future_work", "audit_report"}
    assert set(drafts.values()) == {"gate_passed"}
    assert lessons["n"] >= 2 and reusable["n"] == 1 and len(notes) == 1
    assert approval["decision"] == "pending"                               # モックの結果は GitHub に出さない

    ws = Path(p["workspace_path"])
    assert workspace.verify_prereg(ws, 1, prereg["content_sha256"], prereg["git_commit"])[0]
    assert workspace.git(ws, "rev-parse", "--abbrev-ref", "HEAD") == f"research/{pid}"   # main ではなく専用ブランチ
    assert not workspace.scan_secrets(ws)
    report = (ws / "report/report.md").read_text(encoding="utf-8")
    assert "事前登録した仮説の検証結果" in report and "探索的分析" in report
    assert (ws / "audit/research-round2/recomputed_metrics.csv").exists() and (ws / "LICENSE").exists()

    # 研究履歴を検索・再利用できる
    hits = service.search_memory("平均", ["hypothesis", "lesson", "finding"])
    assert any(h["project_id"] == pid for h in hits)
    metrics = service.self_metrics()
    assert metrics["研究（件）"] == 0 and metrics["（参考）モック実行の研究（件、上の指標には含めない）"] == 1


async def test_policy_change_puts_project_on_hold_until_restored(stack):
    """品質ゲートを含む方針を研究の途中で書き換えると、研究は保留になる。戻して再開すれば続きから進む。"""
    from aar import service
    from aar.db.core import connect, fetch_all

    proposal_id = await _proposal(stack)
    original = stack["policy"].read_text(encoding="utf-8")
    from aar.config import load_policy
    pinned = load_policy().sha256
    project = await _select_with_tampered_policy(stack, proposal_id, original)
    pid = project["id"]
    assert project["policy_sha256"] == pinned
    p = await _wait(pid, ("on_hold", "completed", "failed"))
    assert p["status"] == "on_hold" and "policy_changed" in p["outcome_reason"]
    stack["policy"].write_text(original, encoding="utf-8")
    await service.control_project(pid, "resume")
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable", p["outcome_reason"]
    with connect() as conn:
        kinds = [r["event"] for r in fetch_all(conn, "SELECT event FROM workflow_events WHERE project_id = %s ORDER BY id", (pid,))]
    assert "on_hold" in kinds and "resumed" in kinds


async def _select_with_tampered_policy(stack, proposal_id: str, original: str) -> dict:
    from aar import store
    from aar.config import load_policy
    from aar.db.core import connect

    policy = load_policy()
    with connect() as conn:
        project = store.select_proposal(conn, proposal_id, budget_usd=5, policy_sha256=policy.sha256, engine="native",
                                        execution_mode="mock")
    # 登録のあと、開始の前に基準を緩める
    stack["policy"].write_text(original.replace("max_open_major: 0", "max_open_major: 99"), encoding="utf-8")
    from aar import service
    await service.start_workflow_for(await service.get_client(), project)
    return project


async def test_cancel_stops_project_and_keeps_record(stack, monkeypatch):
    from aar import service

    monkeypatch.setenv("AAR_MOCK_DELAY_S", "3")
    proposal_id = await _proposal(stack)
    project = await service.select_and_start(proposal_id)
    await asyncio.sleep(2)
    await service.control_project(project["id"], "cancel")
    p = await _wait(project["id"], ("cancelled", "completed", "failed"))
    assert p["status"] == "cancelled" and p["outcome"] == "cancelled"


async def test_reopen_continues_from_audit_with_same_gates(stack):
    """監査の上限回数で「公開不可」になった研究を、利用者の判断で監査からやり直せる。基準と記録はそのまま。"""
    from aar import config, service
    from aar.db.core import connect, fetch_all

    stack["policy"].write_text(stack["policy"].read_text(encoding="utf-8").replace(
        "max_audit_rounds: 3        # 修正と再監査の上限", "max_audit_rounds: 1        # 修正と再監査の上限"), encoding="utf-8")
    assert config.load_policy().gates.research.max_audit_rounds == 1
    proposal_id = await _proposal(stack)
    project = await service.select_and_start(proposal_id)
    pid = project["id"]
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "not_publishable" and "Major" in p["outcome_reason"]   # 1回目の監査で止まり、成功扱いにならない
    info = await service.reopen_project(pid)
    assert info["fix_kind"] == "research" and info["workflow_id"] == f"{pid}-reopen-1"
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable", p["outcome_reason"]
    with connect() as conn:
        gates = fetch_all(conn, "SELECT gate, round, passed FROM gate_evaluations WHERE project_id = %s ORDER BY id", (pid,))
        kinds = [r["kind"] for r in fetch_all(conn, "SELECT kind FROM human_interventions WHERE project_id = %s ORDER BY id", (pid,))]
    assert [(g["gate"], g["round"], g["passed"]) for g in gates] == [
        ("research", 1, False), ("research", 2, True), ("publication", 1, False), ("publication", 2, True)]
    assert kinds == ["select", "reopen"]                                    # 人の介入として記録される
    with pytest.raises(ValueError):
        await service.reopen_project(pid)                                    # 公開可で終わった研究は再開できない


async def test_missing_publication_skill_holds_instead_of_passing(stack):
    """公開品質監査 Skill が無ければ、代用の監査で通さずに保留にする。Skill を置いて再開すれば続きから進む。"""
    from aar import service
    from aar.db.core import connect, fetch_all

    shutil.rmtree(stack["pub_skill"])
    proposal_id = await _proposal(stack)
    pid = (await service.select_and_start(proposal_id))["id"]
    p = await _wait(pid, ("on_hold", "completed", "failed"))
    assert p["status"] == "on_hold" and "[setup]" in p["outcome_reason"] and "Skill" in p["outcome_reason"]
    with connect() as conn:
        assert not fetch_all(conn, "SELECT 1 FROM audit_runs WHERE project_id = %s AND kind = 'publication_quality'", (pid,))
        assert not fetch_all(conn, "SELECT 1 FROM gate_evaluations WHERE project_id = %s AND gate = 'publication'", (pid,))
    install_publication_skill(stack["pub_skill"])
    await service.control_project(pid, "resume")
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable", p["outcome_reason"]


async def test_invalid_skill_result_never_counts_as_passed(stack, monkeypatch):
    """Skill の結果が決まりに合っていなければ（Critical があるのに判定 A）、やり直し、それでも駄目なら失敗で確定する。"""
    from aar import service
    from aar.db.core import connect, fetch_all

    monkeypatch.setenv("AAR_MOCK_PUB", "inconsistent")
    proposal_id = await _proposal(stack)
    pid = (await service.select_and_start(proposal_id))["id"]
    p = await _wait(pid, ("completed", "failed", "on_hold"), timeout=180)
    assert p["status"] == "failed" and p["outcome"] == "failed" and "正常な結果" in p["outcome_reason"]
    with connect() as conn:
        invalid = fetch_all(conn, "SELECT detail FROM workflow_events WHERE project_id = %s AND event = 'publication_audit_invalid' ORDER BY id", (pid,))
        gates = fetch_all(conn, "SELECT 1 FROM gate_evaluations WHERE project_id = %s AND gate = 'publication'", (pid,))
        drafts = fetch_all(conn, "SELECT status FROM publication_drafts WHERE project_id = %s", (pid,))
    assert len(invalid) == 3 and "Critical" in invalid[0]["detail"]["problems"][0]   # 3 回試して、毎回記録が残る
    assert not gates and all(d["status"] == "draft" for d in drafts)                   # ゲートは判定されず、成果物は通過扱いにならない
    record = Path(invalid[-1]["detail"]["record"])
    assert len(list(record.parent.glob("attempt-*"))) == 2 and (record / "structured_report.json").exists()   # 過去の試行も残す


async def test_worker_restart_during_publication_audit_resumes(stack, monkeypatch):
    """公開品質監査の途中でワーカーが落ちても、起動し直せば同じ工程からやり直して完了する。完了済みの工程は繰り返さない。"""
    from aar import service
    from aar.db.core import connect, fetch_all

    monkeypatch.setenv("AAR_MOCK_DELAY_S", "2")
    proposal_id = await _proposal(stack)
    pid = (await service.select_and_start(proposal_id))["id"]
    for _ in range(400):
        with connect() as conn:
            started = fetch_all(conn, "SELECT 1 FROM workflow_events WHERE project_id = %s AND event = 'phase_started' "
                                      "AND detail->>'key' = 'audit:publication:1'", (pid,))
        if started:
            break
        await asyncio.sleep(0.25)
    assert started
    await stack["stop_worker"]()                    # 監査の途中で止める
    monkeypatch.setenv("AAR_MOCK_DELAY_S", "0")
    await stack["start_worker"]()
    p = await _wait(pid, ("completed", "failed"), timeout=180)
    assert p["outcome"] == "publishable", p["outcome_reason"]
    with connect() as conn:
        done = fetch_all(conn, "SELECT detail->>'key' AS k, count(*) AS n FROM workflow_events WHERE project_id = %s "
                               "AND event = 'phase_completed' GROUP BY 1", (pid,))
        tries = fetch_all(conn, "SELECT detail->>'attempt' AS a FROM workflow_events WHERE project_id = %s "
                                "AND event = 'phase_started' AND detail->>'key' = 'audit:publication:1'", (pid,))
        rows = fetch_all(conn, "SELECT round FROM audit_runs WHERE project_id = %s AND kind = 'publication_quality' ORDER BY round", (pid,))
    assert all(d["n"] == 1 for d in done) and len(tries) >= 2          # やり直したのは止められた工程だけ
    assert [r["round"] for r in rows] == [1, 2]                         # 監査の記録は重複しない


async def test_reopen_from_publication_keeps_history_and_requires_explicit_policy_adoption(stack):
    """公開可で終わった研究を、公開品質監査からやり直せる（Skill を入れ替えたあとの再確認など）。記録と成果物は残る。"""
    from aar import service, workspace
    from aar.db.core import connect, fetch_all

    proposal_id = await _proposal(stack)
    pid = (await service.select_and_start(proposal_id))["id"]
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable"
    ws = Path(p["workspace_path"])
    head_before = workspace.head(ws)

    stack["policy"].write_text(stack["policy"].read_text(encoding="utf-8") + "\n# 方針を変えた\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adopt-current-policy"):
        await service.reopen_project(pid, from_="publication")                     # 黙って新しい方針にはならない
    with pytest.raises(ValueError):
        await service.reopen_project(pid, from_="audit", adopt_current_policy=True)   # 公開可の研究を独立監査からはやり直せない
    info = await service.reopen_project(pid, from_="publication", adopt_current_policy=True)
    assert info["fix_kind"] == "" and info["pr"] == 2
    p = await _wait(pid, ("completed", "failed"))
    assert p["outcome"] == "publishable", p["outcome_reason"]
    with connect() as conn:
        rounds = [(a["kind"], a["round"]) for a in fetch_all(conn, "SELECT kind, round FROM audit_runs WHERE project_id = %s ORDER BY started_at", (pid,))]
        kinds = [r["kind"] for r in fetch_all(conn, "SELECT kind FROM human_interventions WHERE project_id = %s ORDER BY id", (pid,))]
    assert rounds == [("research", 1), ("research", 2), ("publication_quality", 1), ("publication_quality", 2), ("publication_quality", 3)]
    assert kinds == ["select", "adopt_policy", "reopen"]                             # 方針の固定し直しは人の判断として残る
    assert workspace.git(ws, "rev-parse", "before-reopen-1") == head_before          # やり直し前の状態にタグが付いている
    assert workspace.git(ws, "merge-base", "--is-ancestor", head_before, "HEAD", check=False) == ""   # 履歴は書き換えない
