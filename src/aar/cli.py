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
def proposals_generate(
    batch: Annotated[str, typer.Option(help="企画のまとまりの名前（省略すると今週）")] = "",
    paper: Annotated[list[str] | None, typer.Option(help="企画を作る論文の ID（複数可）。省略すると価値評価の上位から選ぶ")] = None,
    public_data: Annotated[bool, typer.Option(help="公開データセット（config/datasets.yaml）を使う企画にする")] = False,
) -> None:
    """検証企画を今すぐ生成する。"""
    from aar import service
    _print(asyncio.run(service.run_now("proposals", batch, prefer_public_data=public_data, paper_ids=paper)))


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


@projects_app.command("reopen")
def projects_reopen(
    project_id: str,
    from_: Annotated[str, typer.Option("--from", help="audit: 独立監査から / publication: 公開品質監査から")] = "audit",
    adopt_current_policy: Annotated[bool, typer.Option(help="方針ファイルが変わっている場合に、現在の方針に固定し直す（記録に残る）")] = False,
) -> None:
    """終わった研究を監査からやり直す。これまでの記録と成果物は残る。"""
    from aar import service
    _print(asyncio.run(service.reopen_project(project_id, from_=from_, adopt_current_policy=adopt_current_policy)))


@projects_app.command("required-check")
def projects_required_check(project_id: str) -> None:
    """公開前の必須項目の決定的な検査を、いまの公開候補に実行する（読み取りだけ。記録は変えない）。"""
    from aar.publish import required
    from aar.temporal.activities import _third_party_texts, workspace_of
    out = required.run_required_checks(workspace_of(project_id), _third_party_texts(project_id))
    for i in out["items"]:
        typer.echo(f"{'✓' if i['status'] == 'ok' else '✗'} {i['label']}: {i['status']}")
        for e in i["evidence"]:
            typer.echo(f"    {e}")
    typer.echo(f"対象コミット {out['commit'][:12]} / 違反 {len(out['violations'])} / 未確認 {len(out['unverified'])}")
    raise typer.Exit(0 if not out["violations"] and not out["unverified"] else 1)


@projects_app.command("adopt-policy")
def projects_adopt_policy(project_id: str) -> None:
    """方針の変更で保留になった研究を、現在の方針に固定し直す（記録に残る）。そのあと resume で再開する。"""
    from aar import service
    _print(service.adopt_policy(project_id))


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


@app.command("limits")
def limits_cmd(clear: Annotated[str, typer.Option(help="解除を確かめた実行担当（codex / claude）の記録を消す")] = "") -> None:
    """実行担当の利用上限の状態を表示する。解除の見込み時刻までは、パイプラインは相手に問い合わせない。"""
    from aar import limits
    from aar.db.core import connect
    with connect() as conn:
        if clear:
            typer.echo(f"{clear}: " + ("記録を消した（次の呼び出しから問い合わせる）" if limits.clear_limit(conn, clear) else "記録はない"))
            conn.execute("INSERT INTO human_interventions (kind, detail) VALUES ('clear_rate_limit', %s)",
                         (__import__("psycopg.types.json", fromlist=["Jsonb"]).Jsonb({"provider": clear}),))
        rows = limits.current_limits(conn)
    if not rows:
        typer.echo("利用上限に達している実行担当はない")
    for r in rows:
        typer.echo(f"{r['provider']}: {limits.fmt_local(r['limited_until'])} まで待機（{r['source']}、連続 {r['consecutive_hits']} 回）"
                   f" {(r['message'] or '')[:120]}")


improve_app = typer.Typer(help="基盤の改善候補（台帳、評価、採否。基盤には自動で反映しない）", no_args_is_help=True)
app.add_typer(improve_app, name="improve")
ActorOpt = Annotated[str | None, typer.Option("--actor", help="操作主体。human / claude_code / codex / scheduler のどれかで始める（例 claude_code:session）。"
                                                             "省略時は環境変数 AAR_ACTOR")]


@improve_app.command("areas")
def improve_areas(sync: Annotated[bool, typer.Option(help="登録内容を台帳に入れる")] = False) -> None:
    """継続して調べる領域と外部の資料（読んだ範囲つき）を表示する。"""
    from aar.improve import areas

    with connect() as conn:
        if sync:
            _print(areas.sync(conn))
        for a in fetch_all(conn, "SELECT key, title, status, target_problem, reject_conditions FROM improvement_areas ORDER BY key"):
            typer.echo(f"{a['key']:26s} {a['status']:9s} {a['title']}\n    問題: {a['target_problem']}\n    採用しない条件: {a['reject_conditions']}")
        for s in fetch_all(conn, "SELECT area_key, read_level, version, url FROM improvement_sources ORDER BY area_key, url"):
            typer.echo(f"  資料 {s['area_key']:26s} 読んだ範囲 {s['read_level']:9s} 版 {s['version'] or '-':10s} {s['url']}")


@improve_app.command("scan")
def improve_scan(propose: Annotated[bool, typer.Option(help="見つけた弱点を候補として登録する")] = False, actor: ActorOpt = None) -> None:
    """実行の記録から弱点を拾う（記録は読むだけ）。"""
    from aar.improve import ledger, loop

    found = loop.scan_weaknesses()
    if propose:
        who = ledger.parse_actor(actor)
        with connect() as conn:
            for w in found:
                c = loop.propose_from_weakness(conn, w, who)
                w["candidate"] = {"id": c["id"], "created": c["created"], "status": c["status"]}
    _print(found)


@improve_app.command("plan")
def improve_plan(actor: ActorOpt = None) -> None:
    """評価計画と課題を固定し、現行方式の基準値を測る。候補を作る前に実行する。"""
    from aar.improve import ledger, loop

    who = ledger.parse_actor(actor)
    with connect() as conn:
        plan = loop.freeze_plan(conn, who)
        runs = loop.run_baseline(conn, plan["id"], who)
    _print({"plan_id": plan["id"], "plan_sha256": plan["plan_sha256"], "created": plan["created"], "splits": plan["plan"]["splits"],
            "cases": plan["plan"]["cases"],
            "baseline": {k: {"status": r["status"], "isolation": r["isolation"], "metrics": r["metrics"]} for k, r in runs.items()}})


@improve_app.command("generate")
def improve_generate(candidate_id: str, plan_id: int, actor: ActorOpt = None,
                     model: Annotated[str, typer.Option(help="実行担当のモデル（空なら方針の implementer）")] = "") -> None:
    """実行担当に、隔離した複製の中で候補を作らせる。済んでいれば呼び直さない。"""
    from aar.improve import ledger, loop
    from aar.temporal.activities import runners_for

    who, choice = ledger.parse_actor(actor), load_policy().research.models["implementer"]
    with connect() as conn:
        _print(asyncio.run(loop.generate(conn, candidate_id, plan_id, who, runners_for(get_settings().mode)[choice.agent],
                                         model=model or choice.model)))


@improve_app.command("register")
def improve_register(candidate_id: str, plan_id: int, actor: ActorOpt = None) -> None:
    """複製（data_dir/improve/candidates/<ID>/repo）に手で入れた変更を、候補の差分として登録する。"""
    from aar.improve import ledger, loop

    who = ledger.parse_actor(actor)
    with connect() as conn:
        loop.prepare_clone(conn, candidate_id, plan_id)
        _print(loop.register_patch(conn, candidate_id, who, {"agent": str(who), "isolation": "基盤の複製での手作業"}))


@improve_app.command("eval")
def improve_eval(candidate_id: str, plan_id: int, split: Annotated[str, typer.Option(help="select か sealed")] = "select",
                 actor: ActorOpt = None) -> None:
    """候補を、現行方式と同じ課題・同じ条件で評価する。sealed（封印評価）は、候補を選び終えてから 1 回だけ行う。"""
    from aar.improve import ledger, loop

    if split not in ("dev", "select", "sealed"):
        raise typer.BadParameter("split は dev / select / sealed")
    with connect() as conn:
        r = loop.evaluate_candidate(conn, candidate_id, plan_id, split, ledger.parse_actor(actor))
    _print({"run_key": r["run_key"], "created": r["created"], "status": r["status"], "failure_kind": r["failure_kind"],
            "isolation": r["isolation"], "metrics": r["metrics"]})


@improve_app.command("decide")
def improve_decide(candidate_id: str, plan_id: int) -> None:
    """採否を機械的に判定して記録する（採用候補・棄却・保留）。基盤には反映しない。"""
    from aar.improve import loop

    with connect() as conn:
        d = loop.decide_candidate(conn, candidate_id, plan_id)
    _print({"verdict": d["verdict"], "reasons": d["reasons"], "checks": d["checks"]})


@improve_app.command("list")
def improve_list(text: Annotated[str, typer.Argument(help="問題、仮説、棄却の理由、不採用の条件から探す")] = "",
                 status: Annotated[str, typer.Option(help="状態で絞る")] = "") -> None:
    """候補の一覧。"""
    from aar.improve import ledger

    with connect() as conn:
        for c in ledger.search(conn, text, status):
            typer.echo(f"{c['id']}  {c['status']:15s} {c['target']:12s} {c['created_by']:24s} {c['problem'][:70]}")
            if c["status_reason"]:
                typer.echo(f"    理由: {c['status_reason'][:200]}")
            if c["rejected_under"]:
                typer.echo(f"    不採用の条件: {c['rejected_under'][:200]}")


@improve_app.command("show")
def improve_show(candidate_id: str, patch: Annotated[bool, typer.Option(help="差分も表示する")] = False) -> None:
    """候補の詳細（根拠、評価、判定、操作の記録）。"""
    from aar.improve import ledger

    with connect() as conn:
        c = ledger.get_candidate(conn, candidate_id)
        out = {"候補": {k: v for k, v in c.items() if k != "patch" or patch},
               "実行": fetch_all(conn, """SELECT run_key, variant, split, status, failure_kind, isolation, metrics, usage, actor, created_at
                                         FROM improvement_runs WHERE candidate_id = %s OR (candidate_id IS NULL AND commit_sha = %s)
                                         ORDER BY id""", (candidate_id, c["base_commit"])),
               "判定": fetch_all(conn, "SELECT verdict, reasons, checks, decided_by, approved_by, created_at FROM improvement_decisions "
                                       "WHERE candidate_id = %s ORDER BY id", (candidate_id,)),
               "操作": fetch_all(conn, "SELECT created_at, event, actor_kind, actor_detail, detail FROM improvement_events "
                                       "WHERE candidate_id = %s ORDER BY id", (candidate_id,))}
    _print(out)


dev_app = typer.Typer(help="検証用の道具（本番の研究には書き込まない）", no_args_is_help=True)
app.add_typer(dev_app, name="dev")


@projects_app.command("public-copy")
def projects_public_copy(project_id: str) -> None:
    """公開可で完了した研究から、公開用の独立した複製を作る（原本は変更しない）。

    履歴に残る第三者の文章（原論文の要旨の写し）を複製の側で取り除き、全履歴を検査する。監査はやり直しになるので、
    必要な修正をしてから `aar projects reopen <複製のID> --from audit` で独立監査から再開する。
    """
    from aar import fixture
    _print(fixture.clone_project(project_id, purpose="public"))


@dev_app.command("fixture")
def dev_fixture(
    source_id: str,
    defect: Annotated[str, typer.Option(help="入れる不具合。overclaim: 証拠を超える主張 / none: 入れない")] = "overclaim",
    start: Annotated[bool, typer.Option(help="作ったあと、公開品質監査から実行する")] = False,
    scrub_history: Annotated[bool, typer.Option(help="複製の履歴から、過去のコミットにだけ残る第三者の文章を取り除く")] = False,
) -> None:
    """公開可で完了した研究を検証用（FX-…）として複製し、複製にだけ意図的な不具合を入れる。元の研究は変えない。"""
    from aar import fixture, service
    out = fixture.clone_project(source_id, scrub=scrub_history)
    if defect == "overclaim":
        out["defect"] = fixture.inject_overclaim(out["project_id"])
    elif defect != "none":
        raise typer.BadParameter("defect は overclaim か none")
    if start:
        out["started"] = asyncio.run(service.reopen_project(out["project_id"], from_="publication"))
    _print(out)


@app.command("rescore")
def rescore() -> None:
    """重みを変えたあと、全論文の粗い採点をやり直す。"""
    from aar import discovery
    with connect() as conn:
        typer.echo(f"{discovery.score_unscored(conn, load_policy().scoring, limit=100000)} 件を採点した")


if __name__ == "__main__":
    app()
