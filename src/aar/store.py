"""研究の状態・結果・費用・通知の保存。

再試行されても結果が重複しないよう、書き込みはすべて一意キーつきの upsert にする。
"""

from __future__ import annotations

import secrets
import subprocess
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aar.db.core import fetch_all, fetch_one
from aar.models import AgentResult, AuditReport, GateResult

# ------------------------------------------------------------------ 研究


def new_project_id() -> str:
    return f"RP-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"


def select_proposal(conn: psycopg.Connection, proposal_id: str, *, budget_usd: float, policy_sha256: str,
                    engine: str, execution_mode: str = "real") -> dict[str, Any]:
    """企画を選んで研究として登録する。同じ企画を2回選んでも研究は1つだけ（既存を返す）。"""
    existing = fetch_one(conn, "SELECT * FROM research_projects WHERE proposal_id = %s", (proposal_id,))
    if existing:
        return existing
    prop = fetch_one(conn, "SELECT id, title FROM research_proposals WHERE id = %s FOR UPDATE", (proposal_id,))
    if not prop:
        raise KeyError(f"企画が見つからない: {proposal_id}")
    pid = new_project_id()
    row = fetch_one(
        conn,
        """INSERT INTO research_projects (id, proposal_id, title, workflow_id, budget_usd, policy_sha256,
                                          engine, execution_mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (proposal_id) DO NOTHING RETURNING *""",
        (pid, proposal_id, prop["title"], pid, budget_usd, policy_sha256, engine, execution_mode),
    )
    if row is None:  # 同時に選ばれた
        return fetch_one(conn, "SELECT * FROM research_projects WHERE proposal_id = %s", (proposal_id,))  # type: ignore[return-value]
    conn.execute("UPDATE research_proposals SET status = 'selected', selected_at = now() WHERE id = %s", (proposal_id,))
    conn.execute("INSERT INTO human_interventions (project_id, kind) VALUES (%s, 'select')", (pid,))
    return row


def get_project(conn: psycopg.Connection, project_id: str) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT * FROM research_projects WHERE id = %s", (project_id,))
    if not row:
        raise KeyError(f"研究が見つからない: {project_id}")
    return row


def get_proposal_for_project(conn: psycopg.Connection, project_id: str) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """SELECT p.*, pa.title AS paper_title, pa.abstract AS paper_abstract, pa.arxiv_id, pa.doi, pa.url AS paper_url,
                  pa.published_at AS paper_published_at, pa.fulltext_status
           FROM research_projects r JOIN research_proposals p ON p.id = r.proposal_id
           LEFT JOIN papers pa ON pa.id = p.paper_id WHERE r.id = %s""",
        (project_id,),
    )
    if not row:
        raise KeyError(project_id)
    return row


def set_project(conn: psycopg.Connection, project_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(f"UPDATE research_projects SET {cols} WHERE id = %s", (*fields.values(), project_id))  # noqa: S608


def log_event(conn: psycopg.Connection, project_id: str | None, workflow_id: str, event: str,
              detail: dict[str, Any] | None = None, key: str | None = None) -> None:
    conn.execute(
        """INSERT INTO workflow_events (project_id, workflow_id, event, detail, idempotency_key)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
        (project_id, workflow_id, event, Jsonb(detail or {}), key),
    )


# ------------------------------------------------------------------ 費用


def record_agent_cost(conn: psycopg.Connection, project_id: str | None, purpose: str, key: str,
                      res: AgentResult) -> None:
    """エージェント呼び出し1回の費用を記録する。同じキーの2回目は無視する。"""
    category = {"claude": "claude", "codex": "openai"}.get(res.agent, "other_model")
    conn.execute(
        """INSERT INTO cost_ledger (project_id, category, billing, provider, model, purpose, amount_usd,
                                    is_estimate, usage, idempotency_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
        (project_id, category, res.billing, res.agent, res.model, purpose, res.cost_usd, res.cost_is_estimate,
         Jsonb(res.usage | {"duration_s": res.duration_s}), key),
    )


def spent_usd(conn: psycopg.Connection, project_id: str) -> float:
    row = fetch_one(conn, "SELECT coalesce(sum(amount_usd), 0) AS s FROM cost_ledger WHERE project_id = %s", (project_id,))
    return float(row["s"]) if row else 0.0


def budget_state(conn: psycopg.Connection, project_id: str) -> dict[str, float]:
    proj = get_project(conn, project_id)
    spent = spent_usd(conn, project_id)
    budget = float(proj["budget_usd"])
    return {"budget": budget, "spent": spent, "remaining": max(budget - spent, 0.0),
            "ratio": spent / budget if budget > 0 else 1.0}


# ------------------------------------------------------------------ 研究の中身


def save_prereg(conn: psycopg.Connection, project_id: str, cycle: int, content: dict[str, Any], sha: str,
                commit: str) -> int:
    row = fetch_one(
        conn,
        """INSERT INTO preregistrations (project_id, cycle, content, content_sha256, git_commit)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (project_id, cycle) DO UPDATE SET project_id = EXCLUDED.project_id RETURNING id""",
        (project_id, cycle, Jsonb(content), sha, commit),
    )
    prereg_id = row["id"]  # type: ignore[index]
    for h in content.get("hypotheses", []):
        conn.execute(
            """INSERT INTO hypotheses (project_id, label, cycle, statement, decision_rule, kind, preregistration_id)
               VALUES (%s, %s, %s, %s, %s, 'preregistered', %s) ON CONFLICT (project_id, cycle, label) DO NOTHING""",
            (project_id, h["label"], cycle, h["statement"], h.get("decision_rule", ""), prereg_id),
        )
    return prereg_id


def get_prereg(conn: psycopg.Connection, project_id: str, cycle: int) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT * FROM preregistrations WHERE project_id = %s AND cycle = %s", (project_id, cycle))


def upsert_experiment(conn: psycopg.Connection, project_id: str, cycle: int, kind: str, name: str, *,
                      status: str, design: str = "", config: dict[str, Any] | None = None,
                      duration_s: float | None = None, log_uri: str | None = None, git_commit: str | None = None,
                      execution_mode: str = "real") -> str:
    key = f"{project_id}:exp:{cycle}:{kind}:{name}"
    row = fetch_one(
        conn,
        """INSERT INTO experiments (project_id, cycle, kind, name, status, design, config, duration_s, log_uri,
                                    git_commit, execution_mode, idempotency_key, started_at, finished_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(),
                   CASE WHEN %s IN ('completed', 'failed') THEN now() END)
           ON CONFLICT (idempotency_key) DO UPDATE SET status = EXCLUDED.status,
               duration_s = coalesce(EXCLUDED.duration_s, experiments.duration_s),
               log_uri = coalesce(EXCLUDED.log_uri, experiments.log_uri),
               git_commit = coalesce(EXCLUDED.git_commit, experiments.git_commit),
               finished_at = coalesce(EXCLUDED.finished_at, experiments.finished_at)
           RETURNING id""",
        (project_id, cycle, kind, name, status, design, Jsonb(config or {}), duration_s, log_uri, git_commit,
         execution_mode, key, status),
    )
    return str(row["id"])  # type: ignore[index]


def save_results(conn: psycopg.Connection, project_id: str, experiment_id: str, cycle: int,
                 results: list[dict[str, Any]]) -> None:
    """分析結果を保存する。事前登録済みの仮説検証（confirmatory）と探索的分析は別の行として残す。"""
    for r in results:
        hyp = fetch_one(conn, "SELECT id FROM hypotheses WHERE project_id = %s AND cycle = %s AND label = %s",
                        (project_id, cycle, r.get("hypothesis") or ""))
        kind = "confirmatory" if r.get("analysis_kind") == "confirmatory" and hyp else "exploratory"
        conn.execute(
            """INSERT INTO evaluation_results (project_id, experiment_id, hypothesis_id, metric, condition, value,
                   ci_low, ci_high, effect_size, p_value, n, analysis_kind, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (experiment_id, metric, condition, analysis_kind) DO UPDATE SET
                   value = EXCLUDED.value, ci_low = EXCLUDED.ci_low, ci_high = EXCLUDED.ci_high,
                   effect_size = EXCLUDED.effect_size, p_value = EXCLUDED.p_value, n = EXCLUDED.n,
                   details = EXCLUDED.details""",
            (project_id, experiment_id, hyp["id"] if hyp else None, str(r.get("metric", "")),
             str(r.get("condition", "")), _num(r.get("value")), _num(r.get("ci_low")), _num(r.get("ci_high")),
             _num(r.get("effect_size")), _num(r.get("p_value")), _int(r.get("n")), kind,
             Jsonb({k: v for k, v in r.items() if k in ("note", "test", "verdict", "hypothesis")})),
        )


def set_hypothesis_status(conn: psycopg.Connection, project_id: str, cycle: int, label: str, status: str) -> None:
    if status not in ("supported", "not_supported", "inconclusive"):
        status = "inconclusive"
    conn.execute("UPDATE hypotheses SET status = %s WHERE project_id = %s AND cycle = %s AND label = %s",
                 (status, project_id, cycle, label))


def add_observation(conn: psycopg.Connection, project_id: str, content: str, origin: str,
                    experiment_id: str | None = None, data: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO observations (project_id, experiment_id, content, data, origin) VALUES (%s, %s, %s, %s, %s)",
                 (project_id, experiment_id, content, Jsonb(data or {}), origin))


def add_lesson(conn: psycopg.Connection, project_id: str | None, category: str, content: str,
               tags: list[str] | None = None, origin: str = "ai_inference") -> None:
    dup = fetch_one(conn, "SELECT 1 FROM lessons_learned WHERE project_id IS NOT DISTINCT FROM %s AND content = %s",
                    (project_id, content))
    if not dup:
        conn.execute("INSERT INTO lessons_learned (project_id, category, content, tags, origin) VALUES (%s, %s, %s, %s, %s)",
                     (project_id, category, content, tags or [], origin))


def register_artifact(conn: psycopg.Connection, project_id: str, kind: str, path: str, *, sha256: str | None = None,
                      size: int | None = None, git_commit: str | None = None, description: str = "",
                      reusable: bool = False) -> None:
    conn.execute(
        """INSERT INTO research_artifacts (project_id, kind, path, sha256, size_bytes, git_commit, description, reusable)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (project_id, path) DO UPDATE SET sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes,
               git_commit = EXCLUDED.git_commit, description = EXCLUDED.description, reusable = EXCLUDED.reusable""",
        (project_id, kind, path, sha256, size, git_commit, description, reusable),
    )


# ------------------------------------------------------------------ 監査とゲート


def save_audit(conn: psycopg.Connection, project_id: str, round_no: int, report: AuditReport, *,
               implementer: str, target_commit: str | None) -> str:
    """監査結果を保存する。同じ回の2回目の保存は元の行を返す。"""
    key = f"{project_id}:audit:{report.kind}:{round_no}"
    existing = fetch_one(conn, "SELECT id FROM audit_runs WHERE idempotency_key = %s", (key,))
    if existing:
        return str(existing["id"])
    row = fetch_one(
        conn,
        """INSERT INTO audit_runs (project_id, kind, round, auditor, implementer, skill, target_commit, status,
               research_verdict, publication_verdict, score, report_uri, summary, execution_mode, idempotency_key,
               finished_at, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s) RETURNING id""",
        (project_id, report.kind, round_no, report.auditor, implementer, report.skill, target_commit, report.status,
         report.research_verdict, report.publication_verdict, report.score, report.report_dir, report.summary,
         report.execution_mode, key, Jsonb(report.detail)),
    )
    audit_id = str(row["id"])  # type: ignore[index]
    for c in report.claims:
        conn.execute(
            """INSERT INTO audit_claims (audit_run_id, claim_id, claim_text, is_primary, status, recalculation_level, limitations)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (audit_id, c.claim_id, c.claim_text, c.is_primary, c.status, c.recalculation_level, c.limitations))
    for f in report.findings:
        conn.execute(
            """INSERT INTO audit_findings (audit_run_id, project_id, issue_id, severity, certainty, category, title, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (audit_id, project_id, f.issue_id, f.severity, f.certainty, f.category, f.title, Jsonb(f.detail)))
    for m in report.recomputed:
        within = (abs(m.difference) <= m.tolerance) if m.difference is not None and m.tolerance is not None else None
        conn.execute(
            """INSERT INTO audit_recomputed_metrics (audit_run_id, claim_id, metric, condition, original_value,
                   independent_value, difference, tolerance, within_tolerance, interpretation)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (audit_id, m.claim_id, m.metric, m.condition, m.original_value, m.independent_value, m.difference,
             m.tolerance, within, m.interpretation))
    # 再監査は毎回すべてを調べ直す。前の回の指摘は、同じ issue_id で再び挙がれば未解消、挙がらなければ解消とする
    # （再監査の担当には、残っている問題は元の issue_id のまま報告するよう指示している）。
    # ただし、担当に前回の指摘として渡しているのは Critical・Major だけなので、「解消」と言えるのもそれだけ。
    # 軽い指摘は、挙がらなくても解消とはせず not_rechecked にする（別の ID で挙がり直すことがある）
    if round_no > 1 and report.status != "failed":
        raised = [f.issue_id for f in report.findings]
        conn.execute(
            """UPDATE audit_findings f
               SET status = CASE WHEN f.issue_id = ANY(%s) THEN 'unresolved'
                                 WHEN f.severity IN ('Critical', 'Major') THEN 'resolved' ELSE 'not_rechecked' END,
                   resolved_in_round = CASE WHEN f.issue_id = ANY(%s) OR f.severity NOT IN ('Critical', 'Major') THEN NULL ELSE %s END
               FROM audit_runs a WHERE f.audit_run_id = a.id AND a.project_id = %s AND a.kind = %s AND a.round < %s
                 AND f.status IN ('open', 'fix_attempted', 'unresolved')""",
            (raised, raised, round_no, project_id, report.kind, round_no))
    return audit_id


def save_gate(conn: psycopg.Connection, project_id: str, round_no: int, result: GateResult) -> None:
    conn.execute(
        """INSERT INTO gate_evaluations (project_id, gate, round, passed, policy_sha256, checks)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (project_id, gate, round) DO UPDATE SET passed = EXCLUDED.passed, checks = EXCLUDED.checks""",
        (project_id, result.gate, round_no, result.passed, result.policy_sha256,
         Jsonb([c.model_dump() for c in result.checks])))


def latest_findings(conn: psycopg.Connection, project_id: str, kind: str) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """SELECT f.* FROM audit_findings f JOIN audit_runs a ON a.id = f.audit_run_id
           WHERE a.project_id = %s AND a.kind = %s
             AND a.round = (SELECT max(round) FROM audit_runs WHERE project_id = %s AND kind = %s)
           ORDER BY array_position(ARRAY['Critical','Major','Minor','Improvement'], f.severity), f.issue_id""",
        (project_id, kind, project_id, kind))


# ------------------------------------------------------------------ 通知


def notify(conn: psycopg.Connection, project_id: str | None, level: str, title: str, body: str, key: str,
           *, macos: bool = True) -> bool:
    """通知を保存し、初回だけ macOS の通知センターにも出す。"""
    row = fetch_one(
        conn,
        """INSERT INTO notifications (project_id, level, title, body, idempotency_key) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
        (project_id, level, title, body, key))
    if row and macos:
        try:
            script = 'display notification (item 2 of argv) with title (item 1 of argv)'
            subprocess.run(["osascript", "-e", f"on run argv\n{script}\nend run", f"自律研究: {title}", body[:200]],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    return bool(row)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
