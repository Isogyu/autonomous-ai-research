"""コマンドライン。通常の操作は `aar proposals list` と `aar select <番号>` だけで足りる。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import subprocess
from typing import Annotated, Any

import typer

from aar.config import get_settings, load_policy
from aar.db.core import connect, fetch_all, fetch_one, migrate

app = typer.Typer(help="自律型 AI 研究パイプライン", no_args_is_help=True, add_completion=False)
proposals_app = typer.Typer(help="検証企画", no_args_is_help=True)
projects_app = typer.Typer(help="研究", no_args_is_help=True)
app.add_typer(proposals_app, name="proposals")
app.add_typer(projects_app, name="projects")


def _print(obj: Any) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


@app.command()
def doctor() -> None:
    """動作に必要なものが揃っているかを確かめる。"""
    s = get_settings()
    checks: list[tuple[str, bool, str]] = []

    def cmd(name: str, args: list[str]) -> tuple[bool, str]:
        if not shutil.which(args[0]):
            return False, "見つからない"
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else ""

    try:
        with connect() as conn:
            v = fetch_one(conn, "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        checks.append(("PostgreSQL + pgvector", bool(v), f"pgvector {v['extversion']}" if v else "拡張がない"))
    except Exception as e:  # noqa: BLE001
        checks.append(("PostgreSQL + pgvector", False, str(e)[:80]))
    try:
        from aar import service
        asyncio.run(asyncio.wait_for(service.get_client(), 5))
        checks.append(("Temporal", True, s.temporal_address))
    except Exception as e:  # noqa: BLE001
        checks.append(("Temporal", False, str(e)[:80]))
    for name, args in (("Claude Code", ["claude", "--version"]), ("Codex", ["codex", "--version"]),
                       ("Codex のログイン", ["codex", "login", "status"]), ("GitHub CLI", ["gh", "auth", "status"]),
                       ("Docker", ["docker", "info", "--format", "{{.ServerVersion}}"]),
                       ("sandbox-exec", ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"])):
        ok, detail = cmd(name, args)
        checks.append((name, ok, detail))
    from aar.audit.research import skill_available
    checks.append(("研究独立監査 Skill", skill_available(s.research_audit_skill_dir), str(s.research_audit_skill_dir)))
    checks.append(("GitHub 公開品質監査 Skill", skill_available(s.publication_audit_skill_dir),
                   str(s.publication_audit_skill_dir) + "（なければ内蔵の確認項目で代用）"))
    checks.append(("AI Scientist v3", (s.ai_scientist_dir / "pyproject.toml").exists(), str(s.ai_scientist_dir) + "（任意）"))
    from aar.memory.embed import get_embedder
    emb = get_embedder(s.embedding_model)
    checks.append(("埋め込みモデル", emb.semantic, emb.name + ("" if emb.semantic else "（代用。意味の近さは捉えない）")))
    policy = load_policy()
    checks.append(("方針ファイル", True, f"sha256 {policy.sha256[:12]} / sandbox={policy.safety.sandbox}"))
    for name, ok, detail in checks:
        typer.echo(f"{'✓' if ok else '✗'} {name}: {detail}")


@app.command("migrate")
def migrate_cmd() -> None:
    """DB のスキーマを最新にする。"""
    typer.echo(f"適用: {migrate() or 'なし（最新）'}")


@app.command()
def worker(no_schedules: Annotated[bool, typer.Option(help="定期実行を登録しない")] = False) -> None:
    """ワーカーを起動する（常駐）。"""
    import logging

    from aar.temporal.worker import run_worker
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_worker(schedules=not no_schedules))


@app.command()
def ui() -> None:
    """Web UI を起動する。"""
    import uvicorn
    s = get_settings()
    uvicorn.run("aar.ui.app:app", host=s.ui_host, port=s.ui_port, log_level="warning")


@app.command()
def ingest(day: Annotated[str, typer.Argument(help="対象日（YYYY-MM-DD）。省略すると今日")] = "") -> None:
    """論文を今すぐ収集する。"""
    from aar import service
    _print(asyncio.run(service.run_now("ingest", day or dt.date.today().isoformat())))


@proposals_app.command("generate")
def proposals_generate() -> None:
    """検証企画を今すぐ生成する。"""
    from aar import service
    _print(asyncio.run(service.run_now("proposals")))


@proposals_app.command("list")
def proposals_list() -> None:
    """選べる検証企画の一覧。"""
    with connect() as conn:
        rows = fetch_all(conn, """SELECT id, title, rank_score, execution_mode, body->>'est_cost_usd' AS cost,
                                         body->>'est_runtime_hours' AS hours, batch_key
                                  FROM research_proposals WHERE status = 'proposed' ORDER BY batch_key DESC, rank_score DESC NULLS LAST""")
    if not rows:
        typer.echo("企画はまだない。`aar ingest` のあと `aar proposals generate` で作れる。")
    for i, r in enumerate(rows, 1):
        mock = "【モック】" if r["execution_mode"] == "mock" else ""
        typer.echo(f"{i:2d}. {mock}{r['title']}\n    評価 {r['rank_score']} / 想定 {r['hours']} 時間・{r['cost']} ドル / {r['batch_key']} / {r['id']}")


@proposals_app.command("show")
def proposals_show(ref: str) -> None:
    """企画の詳細（番号または ID）。"""
    _print(_resolve_proposal(ref)["body"])


def _resolve_proposal(ref: str) -> dict[str, Any]:
    with connect() as conn:
        if ref.isdigit():
            rows = fetch_all(conn, "SELECT * FROM research_proposals WHERE status = 'proposed' "
                                   "ORDER BY batch_key DESC, rank_score DESC NULLS LAST")
            if not 1 <= int(ref) <= len(rows):
                raise typer.BadParameter(f"番号は 1〜{len(rows)}")
            return rows[int(ref) - 1]
        row = fetch_one(conn, "SELECT * FROM research_proposals WHERE id::text = %s", (ref,))
        if not row:
            raise typer.BadParameter("企画が見つからない")
        return row


@app.command()
def select(ref: Annotated[str, typer.Argument(help="企画の番号または ID")],
           budget: Annotated[float | None, typer.Option(help="費用上限（ドル）。省略すると方針の値")] = None,
           remote_repo: Annotated[str | None, typer.Option(help="公開候補の push 先（owner/name、非公開のリポジトリ）")] = None) -> None:
    """企画を選んで研究を開始する。以降は自動で進む。"""
    from aar import service
    project = asyncio.run(service.select_and_start(str(_resolve_proposal(ref)["id"]), budget_usd=budget, remote_repo=remote_repo))
    typer.echo(f"研究 {project['id']} を登録した。進捗: aar projects show {project['id']}")


@projects_app.command("list")
def projects_list() -> None:
    with connect() as conn:
        rows = fetch_all(conn, """SELECT p.id, p.title, p.status, p.phase, p.outcome, p.execution_mode, p.budget_usd,
                                         coalesce((SELECT sum(amount_usd) FROM cost_ledger c WHERE c.project_id = p.id), 0) AS spent
                                  FROM research_projects p ORDER BY p.created_at DESC""")
    for r in rows:
        mock = "【モック】" if r["execution_mode"] == "mock" else ""
        typer.echo(f"{r['id']}  {r['status']:<10} {r['phase']:<18} {r['outcome'] or '-':<16} "
                   f"{float(r['spent']):.2f}/{float(r['budget_usd']):.0f}$  {mock}{r['title'][:50]}")


@projects_app.command("show")
def projects_show(project_id: str) -> None:
    with connect() as conn:
        p = fetch_one(conn, "SELECT * FROM research_projects WHERE id = %s", (project_id,))
        if not p:
            raise typer.BadParameter("研究が見つからない")
        out = {
            "研究": {k: p[k] for k in ("id", "title", "status", "phase", "outcome", "outcome_reason", "execution_mode",
                                       "engine", "budget_usd", "workspace_path", "branch", "pr_url")},
            "仮説": fetch_all(conn, "SELECT cycle, label, kind, status, statement FROM hypotheses WHERE project_id = %s ORDER BY cycle, label", (project_id,)),
            "監査": fetch_all(conn, "SELECT kind, round, auditor, status, publication_verdict, score FROM audit_runs WHERE project_id = %s ORDER BY started_at", (project_id,)),
            "指摘": fetch_all(conn, "SELECT issue_id, severity, status, title FROM audit_findings WHERE project_id = %s ORDER BY id", (project_id,)),
            "品質ゲート": fetch_all(conn, "SELECT gate, round, passed FROM gate_evaluations WHERE project_id = %s ORDER BY id", (project_id,)),
            "成果物": fetch_all(conn, "SELECT kind, path, status FROM publication_drafts WHERE project_id = %s ORDER BY id", (project_id,)),
            "費用": fetch_all(conn, "SELECT category, billing, round(sum(amount_usd), 4) AS usd, count(*) AS calls FROM cost_ledger WHERE project_id = %s GROUP BY 1, 2", (project_id,)),
            "経過": [f"{e['created_at']:%m-%d %H:%M:%S} {e['event']} {json.dumps(e['detail'], ensure_ascii=False)[:120]}"
                     for e in fetch_all(conn, "SELECT * FROM workflow_events WHERE project_id = %s ORDER BY id DESC LIMIT 25", (project_id,))],
        }
    _print(out)


def _control(project_id: str, action: str) -> None:
    from aar import service
    asyncio.run(service.control_project(project_id, action))
    typer.echo(f"{project_id}: {action} を送った")


@projects_app.command("pause")
def projects_pause(project_id: str) -> None:
    _control(project_id, "pause")


@projects_app.command("resume")
def projects_resume(project_id: str) -> None:
    _control(project_id, "resume")


@projects_app.command("cancel")
def projects_cancel(project_id: str) -> None:
    _control(project_id, "cancel")


@projects_app.command("budget")
def projects_budget(project_id: str, budget_usd: float) -> None:
    """費用上限を変える（保留からの再開前に使う）。"""
    from aar import service
    asyncio.run(service.change_budget(project_id, budget_usd))
    typer.echo(f"{project_id}: 費用上限を {budget_usd} ドルにした")


@app.command()
def search(query: str, kind: Annotated[list[str] | None, typer.Option(help="paper / proposal / hypothesis / experiment / finding / lesson")] = None,
           limit: int = 10) -> None:
    """研究履歴を検索する。"""
    from aar import service
    for r in service.search_memory(query, kind, limit):
        typer.echo(f"[{r['entity_type']}] {r['title']}  (score {r['score']}, {r['signals']})"
                   + (f"  研究 {r['project_id']}" if r["project_id"] else "") + f"\n    {r['snippet'][:160]}")


@app.command()
def untested(limit: int = 20) -> None:
    """まだ検証されていない課題。"""
    from aar.memory.search import untested_questions
    with connect() as conn:
        for r in untested_questions(conn, limit):
            typer.echo(f"[{r['source']}/{r['state']}] {r['question'][:200]}" + (f"  研究 {r['project_id']}" if r["project_id"] else ""))


@app.command()
def metrics() -> None:
    """システムの自己評価。"""
    from aar import service
    _print(service.self_metrics())


@app.command("rescore")
def rescore() -> None:
    """重みを変えたあと、全論文の粗い採点をやり直す。"""
    from aar import discovery
    with connect() as conn:
        typer.echo(f"{discovery.score_unscored(conn, load_policy().scoring, limit=100000)} 件を採点した")


if __name__ == "__main__":
    app()
