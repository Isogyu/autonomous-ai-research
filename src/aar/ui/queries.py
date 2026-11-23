"""画面用の SQL。すべて読み取り専用（既読化だけが更新）。conn は aar.db.core.connect() の接続。"""

from __future__ import annotations

from typing import Any

from aar.db.core import fetch_all, fetch_one

Row = dict[str, Any]

SEVERITY_ORDER = {"Critical": 0, "Major": 1, "Minor": 2, "Improvement": 3}
PROJECT_STATUSES = ["registered", "running", "paused", "on_hold", "completed", "failed", "cancelled"]


# ---------------------------------------------------------------- 一覧画面


def latest_proposals(conn) -> list[Row]:
    """最新の batch_key の status='proposed' の企画（rank_score 降順）。"""
    batch = fetch_one(
        conn,
        "SELECT batch_key FROM research_proposals WHERE status = 'proposed'"
        " ORDER BY created_at DESC, batch_key DESC LIMIT 1",
    )
    if not batch:
        return []
    return fetch_all(
        conn,
        "SELECT id, title, body, rank_score, execution_mode, batch_key, created_at"
        " FROM research_proposals WHERE status = 'proposed' AND batch_key = %s"
        " ORDER BY rank_score DESC NULLS LAST, created_at DESC",
        (batch["batch_key"],),
    )


def unread_notifications(conn, limit: int = 5) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT id, project_id, level, title, body, created_at FROM notifications"
        " WHERE read_at IS NULL ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    )


def count_unread(conn) -> int:
    return fetch_one(conn, "SELECT count(*) AS n FROM notifications WHERE read_at IS NULL")["n"]  # type: ignore[index]


def running_count(conn) -> int:
    return fetch_one(conn, "SELECT count(*) AS n FROM research_projects WHERE status = 'running'")["n"]  # type: ignore[index]


def mark_all_read(conn) -> None:
    conn.execute("UPDATE notifications SET read_at = now() WHERE read_at IS NULL")


# ---------------------------------------------------------------- 企画の詳細


def get_proposal(conn, proposal_id: str) -> Row | None:
    return fetch_one(
        conn,
        "SELECT id, batch_key, paper_id, title, body, rank_score, status, execution_mode, created_at"
        " FROM research_proposals WHERE id = %s",
        (proposal_id,),
    )


def latest_score(conn, paper_id: str | None) -> Row | None:
    if not paper_id:
        return None
    return fetch_one(
        conn,
        "SELECT criteria, total, scorer, rubric_version, created_at FROM paper_scores"
        " WHERE paper_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
        (paper_id,),
    )


def project_of_proposal(conn, proposal_id: str) -> Row | None:
    return fetch_one(
        conn, "SELECT id, status FROM research_projects WHERE proposal_id = %s", (proposal_id,)
    )


def paper_url(conn, paper_id: str | None) -> str | None:
    if not paper_id:
        return None
    row = fetch_one(conn, "SELECT url FROM papers WHERE id = %s", (paper_id,))
    return row["url"] if row else None


# ---------------------------------------------------------------- 研究


def list_projects(conn, status: str | None = None) -> list[Row]:
    sql = (
        "SELECT p.id, p.title, p.status, p.phase, p.outcome, p.execution_mode, p.budget_usd,"
        " p.started_at, p.created_at,"
        " COALESCE((SELECT sum(amount_usd) FROM cost_ledger c WHERE c.project_id = p.id), 0) AS cost_usd"
        " FROM research_projects p"
    )
    params: tuple = ()
    if status:
        sql += " WHERE p.status = %s"
        params = (status,)
    sql += (
        " ORDER BY (p.status = 'running') DESC, (p.status IN ('paused', 'on_hold')) DESC,"
        " p.created_at DESC"
    )
    return fetch_all(conn, sql, params)


def get_project(conn, project_id: str) -> Row | None:
    return fetch_one(conn, "SELECT * FROM research_projects WHERE id = %s", (project_id,))


def project_costs(conn, project_id: str) -> tuple[Any, list[Row]]:
    rows = fetch_all(
        conn,
        "SELECT billing, sum(amount_usd) AS amount_usd, bool_or(is_estimate) AS has_estimate"
        " FROM cost_ledger WHERE project_id = %s GROUP BY billing ORDER BY billing",
        (project_id,),
    )
    return sum((r["amount_usd"] for r in rows), 0), rows


def project_hypotheses(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT label, cycle, statement, decision_rule, kind, status FROM hypotheses"
        " WHERE project_id = %s ORDER BY cycle, label",
        (project_id,),
    )


def project_preregistrations(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT cycle, content_sha256, git_commit, registered_at FROM preregistrations"
        " WHERE project_id = %s ORDER BY cycle",
        (project_id,),
    )


def project_experiments(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT cycle, kind, name, status, execution_mode, cost_usd, duration_s, started_at"
        " FROM experiments WHERE project_id = %s ORDER BY cycle, started_at NULLS LAST, name",
        (project_id,),
    )


def project_results(conn, project_id: str) -> dict[str, list[Row]]:
    rows = fetch_all(
        conn,
        "SELECT r.metric, r.condition, r.value, r.ci_low, r.ci_high, r.effect_size, r.p_value, r.n,"
        " r.analysis_kind, h.label AS hypothesis, e.name AS experiment"
        " FROM evaluation_results r"
        " LEFT JOIN hypotheses h ON h.id = r.hypothesis_id"
        " LEFT JOIN experiments e ON e.id = r.experiment_id"
        " WHERE r.project_id = %s ORDER BY r.analysis_kind, r.id",
        (project_id,),
    )
    out: dict[str, list[Row]] = {"confirmatory": [], "exploratory": []}
    for r in rows:
        out[r["analysis_kind"]].append(r)
    return out


def project_audits(conn, project_id: str) -> list[Row]:
    """監査を回ごとに。指摘は重大度順、主張の判定つき。"""
    runs = fetch_all(
        conn,
        "SELECT id, kind, round, auditor, status, research_verdict, publication_verdict, score,"
        " summary, execution_mode, started_at FROM audit_runs WHERE project_id = %s"
        " ORDER BY kind, round",
        (project_id,),
    )
    for run in runs:
        findings = fetch_all(
            conn,
            "SELECT issue_id, severity, certainty, category, title, status FROM audit_findings"
            " WHERE audit_run_id = %s",
            (run["id"],),
        )
        findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["issue_id"]))
        run["findings"] = findings
        run["claims"] = fetch_all(
            conn,
            "SELECT claim_id, claim_text, is_primary, status, limitations FROM audit_claims"
            " WHERE audit_run_id = %s ORDER BY claim_id",
            (run["id"],),
        )
    return runs


def project_gates(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT gate, round, passed, checks, created_at FROM gate_evaluations"
        " WHERE project_id = %s ORDER BY gate, round",
        (project_id,),
    )


def project_drafts(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn, "SELECT kind, path, status FROM publication_drafts WHERE project_id = %s ORDER BY kind",
        (project_id,),
    )


def project_artifacts(conn, project_id: str) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT kind, path, description, reusable FROM research_artifacts"
        " WHERE project_id = %s ORDER BY kind, path",
        (project_id,),
    )


def project_events(conn, project_id: str, limit: int = 50) -> list[Row]:
    return fetch_all(
        conn,
        "SELECT event, detail, created_at FROM workflow_events WHERE project_id = %s"
        " ORDER BY created_at DESC, id DESC LIMIT %s",
        (project_id, limit),
    )


def ping(conn) -> bool:
    return fetch_one(conn, "SELECT 1 AS ok")["ok"] == 1  # type: ignore[index]
