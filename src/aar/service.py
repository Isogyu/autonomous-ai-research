"""UI と CLI から使う操作。Temporal と DB の両方にまたがる処理をここにまとめる。"""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from aar import store
from aar.config import get_settings, load_policy
from aar.db.core import connect, fetch_all, fetch_one
from aar.memory import search as memsearch
from aar.memory.embed import get_embedder
from aar.temporal.workflows import ResearchInput


async def get_client() -> Client:
    s = get_settings()
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


async def start_workflow_for(client: Client, project: dict[str, Any]) -> None:
    """研究のワークフローを開始する。Workflow ID は研究 ID なので、何度呼んでも1つしか動かない。"""
    policy = load_policy()
    await client.start_workflow(
        "ResearchWorkflow",
        ResearchInput(project_id=project["id"], max_cycles=policy.research.max_cycles,
                      max_research_audit_rounds=policy.gates.research.max_audit_rounds,
                      max_publication_audit_rounds=policy.gates.publication.max_audit_rounds,
                      phase_timeout_minutes=policy.research.phase_timeout_minutes),
        id=project["workflow_id"], task_queue=get_settings().task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        execution_timeout=timedelta(hours=policy.research.max_wallclock_hours * 4),
    )
    with connect() as conn:
        conn.execute("UPDATE research_projects SET status = 'running' WHERE id = %s AND status = 'registered'", (project["id"],))
        store.log_event(conn, project["id"], project["workflow_id"], "workflow_started", key=f"{project['id']}:started")


async def select_and_start(proposal_id: str, *, budget_usd: float | None = None, engine: str | None = None,
                           remote_repo: str | None = None) -> dict[str, Any]:
    """企画を選び、研究として登録して開始する。同じ企画を2回選んでも研究は1つ。

    Temporal に接続できなくても登録は残り、ワーカーの起動時に開始される（start_pending）。
    """
    policy, settings = load_policy(), get_settings()
    if (engine or policy.research.default_engine) != "native":
        # AI Scientist v3 のアダプター（aar.engine.ai_scientist）は、まだワークフローにつないでいない
        raise NotImplementedError("現在使える研究エンジンは native だけ（ai_scientist_v3 は未接続。ADR-0003）")
    with connect() as conn:
        prop = fetch_one(conn, "SELECT execution_mode FROM research_proposals WHERE id = %s", (proposal_id,))
        if not prop:
            raise KeyError(f"企画が見つからない: {proposal_id}")
        mode = "mock" if settings.mode == "mock" or prop["execution_mode"] == "mock" else "real"
        project = store.select_proposal(conn, proposal_id, budget_usd=budget_usd or policy.research.budget_usd,
                                        policy_sha256=policy.sha256, engine=engine or policy.research.default_engine,
                                        execution_mode=mode)
        if remote_repo and not project.get("remote_repo"):
            store.set_project(conn, project["id"], remote_repo=remote_repo)
    try:
        await start_workflow_for(await get_client(), project)
    except Exception as e:  # noqa: BLE001 - 接続できない場合は登録だけ残す
        with connect() as conn:
            store.log_event(conn, project["id"], project["workflow_id"], "start_deferred", {"error": str(e)[:300]})
    return project


async def start_pending() -> list[str]:
    """登録済みでまだ始まっていない研究を開始する（ワーカーの起動時に呼ぶ）。"""
    with connect() as conn:
        pending = fetch_all(conn, "SELECT * FROM research_projects WHERE status = 'registered' ORDER BY created_at")
    if not pending:
        return []
    client = await get_client()
    for p in pending:
        await start_workflow_for(client, p)
    return [p["id"] for p in pending]


def _workflow_id(project_id: str) -> str:
    with connect() as conn:
        return store.get_project(conn, project_id)["workflow_id"]


async def reopen_project(project_id: str, *, from_: str = "audit", adopt_current_policy: bool = False) -> dict[str, Any]:
    """終わった研究を、監査からやり直す（利用者の判断による追加の修正・再監査）。

    from_ が audit なら独立監査から（公開不可・失敗で終わった研究）、publication なら公開品質監査から
    （公開可で終わった研究も対象。たとえば監査の Skill を入れ替えたあとの再確認）。
    監査の回は続きの番号になり、これまでの記録と成果物は残る。やり直しの前の状態には Git のタグを付ける。

    品質ゲートの基準は、研究の開始時に固定したもののまま。方針ファイルが変わっている場合は、
    adopt_current_policy を明示したときだけ現在の方針に固定し直す（人の判断として記録する。エージェントからは呼べない）。
    """
    if from_ not in ("audit", "publication"):
        raise ValueError("from_ は audit か publication")
    policy = load_policy()
    allowed = ("not_publishable", "failed") if from_ == "audit" else ("publishable", "publishable_with_revisions", "not_publishable", "failed")
    with connect() as conn:
        p = store.get_project(conn, project_id)
        if p["status"] not in ("completed", "failed") or p["outcome"] not in allowed:
            raise ValueError(f"この研究（{p['status']} / {p['outcome']}）は {from_} からは再開できない")
        if not fetch_one(conn, "SELECT 1 FROM experiments WHERE project_id = %s AND kind = 'main' AND status = 'completed'", (project_id,)):
            raise ValueError("本実験が完了していない研究は、監査からは再開できない")
        if p["policy_sha256"] != policy.sha256:
            if not adopt_current_policy:
                raise ValueError("方針ファイルが研究の開始時から変わっている。現在の方針で再開するなら --adopt-current-policy を付ける")
            from psycopg.types.json import Jsonb
            conn.execute("INSERT INTO human_interventions (project_id, kind, detail) VALUES (%s, 'adopt_policy', %s)",
                         (project_id, Jsonb({"from": p["policy_sha256"], "to": policy.sha256})))
            store.set_project(conn, project_id, policy_sha256=policy.sha256)
        r = fetch_one(conn, """SELECT
              coalesce((SELECT max(round) FROM audit_runs WHERE project_id = %(p)s AND kind = 'research'), 0) AS rr,
              coalesce((SELECT max(round) FROM audit_runs WHERE project_id = %(p)s AND kind = 'publication_quality'), 0) AS pr,
              coalesce((SELECT max(cycle) FROM experiments WHERE project_id = %(p)s AND kind = 'main' AND status = 'completed'), 1) AS cycle,
              (SELECT count(*) FROM experiments WHERE project_id = %(p)s AND kind = 'main') AS runs,
              (SELECT count(*) FROM human_interventions WHERE project_id = %(p)s AND kind = 'reopen') AS reopens,
              (SELECT CASE gate WHEN 'research' THEN 'research' ELSE 'publication_quality' END FROM gate_evaluations
                WHERE project_id = %(p)s ORDER BY id DESC LIMIT 1) AS last_kind,
              (SELECT round FROM gate_evaluations WHERE project_id = %(p)s ORDER BY id DESC LIMIT 1) AS last_round,
              (SELECT passed FROM gate_evaluations WHERE project_id = %(p)s ORDER BY id DESC LIMIT 1) AS last_passed""",
                      {"p": project_id})
        r = dict(r)  # type: ignore[arg-type]
        # 最後のゲートが通っていなければ、その指摘の修正から始める
        unfixed = r["last_passed"] is False
        r["fix_kind"] = r["last_kind"] if unfixed else ""
        r["fix_round"] = r["last_round"] if unfixed else 0
        if p.get("workspace_path"):
            from pathlib import Path

            from aar import workspace as wsmod
            wsmod.git(Path(p["workspace_path"]), "tag", "-f", f"before-reopen-{r['reopens'] + 1}", check=False)
        workflow_id = f"{project_id}-reopen-{r['reopens'] + 1}"  # type: ignore[index]
        store.set_project(conn, project_id, status="running", outcome=None, outcome_reason=None, phase="reopened",
                          finished_at=None, workflow_id=workflow_id)
        conn.execute("INSERT INTO human_interventions (project_id, kind) VALUES (%s, 'reopen')", (project_id,))
        store.log_event(conn, project_id, workflow_id, "reopened", r | {"from": from_})
    client = await get_client()
    await client.start_workflow(
        "ResearchWorkflow",
        ResearchInput(project_id=project_id, max_cycles=policy.research.max_cycles,
                      max_research_audit_rounds=policy.gates.research.max_audit_rounds,
                      max_publication_audit_rounds=policy.gates.publication.max_audit_rounds,
                      phase_timeout_minutes=policy.research.phase_timeout_minutes, resume_from=from_,
                      cycle=int(r["cycle"]), run_no=int(r["runs"]), start_research_round=int(r["rr"]) + 1,  # type: ignore[index]
                      start_publication_round=int(r["pr"]) + 1, fix_kind=r["fix_kind"] or "",  # type: ignore[index]
                      fix_round=int(r["fix_round"] or 0)),  # type: ignore[index]
        id=workflow_id, task_queue=get_settings().task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        execution_timeout=timedelta(hours=policy.research.max_wallclock_hours * 2))
    return {"project_id": project_id, "workflow_id": workflow_id, "from": from_, **r}


def adopt_policy(project_id: str) -> dict[str, str]:
    """保留中の研究を、現在の方針ファイルに固定し直す（方針を意図して変えた場合の、人の判断としての取り込み）。

    対象は「方針の変更」で保留になっている研究だけ。前後のハッシュを人の介入として記録する。
    このあと resume で再開する。エージェントからは呼べない（CLI からの操作だけ）。
    """
    policy = load_policy()
    with connect() as conn:
        p = store.get_project(conn, project_id)
        if p["status"] != "on_hold" or "[policy_changed]" not in (p["outcome_reason"] or ""):
            raise ValueError("方針の変更で保留になっている研究だけが対象")
        from psycopg.types.json import Jsonb
        conn.execute("INSERT INTO human_interventions (project_id, kind, detail) VALUES (%s, 'adopt_policy', %s)",
                     (project_id, Jsonb({"from": p["policy_sha256"], "to": policy.sha256})))
        store.set_project(conn, project_id, policy_sha256=policy.sha256)
        store.log_event(conn, project_id, p["workflow_id"], "policy_adopted", {"from": p["policy_sha256"], "to": policy.sha256})
    return {"from": p["policy_sha256"], "to": policy.sha256}


async def control_project(project_id: str, action: str) -> None:
    if action not in ("pause", "resume", "cancel"):
        raise ValueError(f"未知の操作: {action}")
    client = await get_client()
    await client.get_workflow_handle(_workflow_id(project_id)).signal(action)
    with connect() as conn:
        conn.execute("INSERT INTO human_interventions (project_id, kind) VALUES (%s, %s)", (project_id, action))


async def change_budget(project_id: str, budget_usd: float) -> None:
    client = await get_client()
    await client.get_workflow_handle(_workflow_id(project_id)).execute_update("change_budget", budget_usd)


async def ensure_schedules() -> list[str]:
    """日次の論文収集と週次の企画生成を登録する。登録済みなら何もしない。

    PC が止まっていて逃した実行は、収集は1日分、企画は3日分までさかのぼって1回だけ実行する。
    """
    client = await get_client()
    queue = get_settings().task_queue
    specs = {
        "aar-daily-ingest": ("IngestWorkflow", ScheduleCalendarSpec(hour=[ScheduleRange(6)], minute=[ScheduleRange(30)]),
                             timedelta(days=1)),
        "aar-weekly-proposals": ("ProposalWorkflow",
                                 ScheduleCalendarSpec(day_of_week=[ScheduleRange(1)], hour=[ScheduleRange(7)], minute=[ScheduleRange(30)]),
                                 timedelta(days=3)),
    }
    created = []
    for sid, (wf, cal, catchup) in specs.items():
        try:
            await client.create_schedule(sid, Schedule(
                action=ScheduleActionStartWorkflow(wf, id=sid, task_queue=queue, execution_timeout=timedelta(hours=3)),
                spec=ScheduleSpec(calendars=[cal], time_zone_name="Asia/Tokyo"),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=catchup)))
            created.append(sid)
        except ScheduleAlreadyRunningError:
            pass
    return created


async def run_now(kind: str, arg: str = "", *, prefer_public_data: bool = False, paper_ids: list[str] | None = None) -> dict[str, Any]:
    """収集または企画生成を今すぐ1回実行して結果を待つ。"""
    client = await get_client()
    wf = {"ingest": "IngestWorkflow", "proposals": "ProposalWorkflow"}[kind]
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    args: list[Any] = [arg, prefer_public_data, paper_ids] if kind == "proposals" and (prefer_public_data or paper_ids) else [arg]
    return await client.execute_workflow(wf, args=args, id=f"aar-{kind}-manual-{stamp}", task_queue=get_settings().task_queue,
                                         execution_timeout=timedelta(hours=3))


def search_memory(query: str, kinds: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        return memsearch.hybrid_search(conn, get_embedder(get_settings().embedding_model), query, kinds=kinds, limit=limit)


def self_metrics() -> dict[str, Any]:
    """システムの自己評価。モック実行は数えない。基準を緩めるためではなく、傾向を見るための指標。"""
    q = """
    WITH p AS (SELECT * FROM research_projects WHERE execution_mode = 'real'),
         done AS (SELECT * FROM p WHERE status IN ('completed', 'failed', 'cancelled')),
         cost AS (SELECT project_id, sum(amount_usd) usd FROM cost_ledger GROUP BY project_id),
         r1 AS (SELECT a.project_id, bool_or(g.passed) AS first_pass FROM audit_runs a
                JOIN gate_evaluations g ON g.project_id = a.project_id AND g.gate = 'research' AND g.round = 1
                WHERE a.kind = 'research' AND a.round = 1 AND a.execution_mode = 'real' GROUP BY a.project_id)
    SELECT
      (SELECT count(*) FROM papers) AS papers,
      (SELECT count(*) FROM research_proposals WHERE execution_mode = 'real') AS proposals,
      (SELECT round(100.0 * count(*) FILTER (WHERE status = 'selected') / nullif(count(*), 0), 1)
         FROM research_proposals WHERE execution_mode = 'real') AS adoption_pct,
      (SELECT count(*) FROM p) AS projects,
      (SELECT round(100.0 * count(*) FILTER (WHERE status = 'completed') / nullif(count(*), 0), 1) FROM done) AS completion_pct,
      (SELECT round(100.0 * count(*) FILTER (WHERE outcome IN ('publishable', 'publishable_with_revisions')) / nullif(count(*), 0), 1)
         FROM done) AS publishable_pct,
      (SELECT round(100.0 * count(*) FILTER (WHERE first_pass) / nullif(count(*), 0), 1) FROM r1) AS first_audit_pass_pct,
      (SELECT count(*) FROM audit_findings f JOIN p ON p.id = f.project_id WHERE f.severity IN ('Critical', 'Major')) AS major_findings,
      (SELECT round(100.0 * count(DISTINCT e.project_id) FILTER (WHERE e.name ~ '^main-([2-9]|[1-9][0-9])')
                    / nullif(count(DISTINCT e.project_id), 0), 1)
         FROM experiments e JOIN p ON p.id = e.project_id WHERE e.kind = 'main') AS rerun_pct,
      (SELECT round(avg(c.usd)::numeric, 2) FROM done d JOIN cost c ON c.project_id = d.id) AS avg_cost,
      (SELECT round((avg(extract(epoch FROM finished_at - started_at)) / 3600)::numeric, 2) FROM done
         WHERE finished_at IS NOT NULL AND started_at IS NOT NULL) AS avg_hours,
      (SELECT round(count(*)::numeric / nullif((SELECT count(*) FROM p), 0), 2) FROM human_interventions h
         JOIN p ON p.id = h.project_id WHERE h.kind <> 'select') AS interventions,
      (SELECT round(100.0 * count(*) FILTER (WHERE within_tolerance) / nullif(count(*) FILTER (WHERE within_tolerance IS NOT NULL), 0), 1)
         FROM audit_recomputed_metrics m JOIN audit_runs a ON a.id = m.audit_run_id WHERE a.execution_mode = 'real') AS recompute_match_pct,
      (SELECT count(*) FROM research_projects WHERE execution_mode = 'mock') AS mock_projects
    """
    with connect() as conn:
        r = fetch_one(conn, q) or {}
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "収集した論文（件）": r.get("papers"),
        "生成した検証企画（件）": r.get("proposals"),
        "検証企画の採用率（%）": f(r.get("adoption_pct")),
        "研究（件）": r.get("projects"),
        "研究完了率（%、終了した研究のうち最後まで進んだ割合）": f(r.get("completion_pct")),
        "技術発信できる成果の割合（%）": f(r.get("publishable_pct")),
        "初回の研究監査の合格率（%）": f(r.get("first_audit_pass_pct")),
        "監査で見つかった重大な問題（Critical・Major、件）": r.get("major_findings"),
        "再実験が起きた研究の割合（%）": f(r.get("rerun_pct")),
        "研究1件当たりの費用（ドル、定額契約分は API 定価への換算）": f(r.get("avg_cost")),
        "研究1件当たりの時間（時間）": f(r.get("avg_hours")),
        "研究1件当たりの人の介入（回、企画の選択を除く）": f(r.get("interventions")),
        "独立再計算が元の値と一致した割合（%）": f(r.get("recompute_match_pct")),
        "（参考）モック実行の研究（件、上の指標には含めない）": r.get("mock_projects"),
    }
