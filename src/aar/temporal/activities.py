"""Temporal のアクティビティ。副作用（エージェント呼び出し、実験の実行、DB、Git、GitHub）はすべてここに置く。

再試行への備え:
  * 完了した工程の結果は side_effects に保存し、同じ工程が再実行されたら保存済みの結果を返す
    （有料のエージェント呼び出しや GitHub 操作を繰り返さない）。
  * 途中で落ちた工程は、作業領域に残ったファイルの上からやり直す。
  * 保留にすべき失敗（認証切れ、費用上限、基準の変更、秘密情報）は再試行しない種類のエラーにして、
    ワークフロー側で保留にする。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from aar import discovery, gates, store
from aar import workspace as wsmod
from aar.agents.runner import AgentRunner, ClaudeCodeRunner, CodexRunner
from aar.audit import publication as pubaudit
from aar.audit import research as raudit
from aar.config import Policy, get_settings, load_policy, policy_file_sha256
from aar.db.core import connect, fetch_all, fetch_one, record_once, run_once
from aar.engine import fix as fixmod
from aar.engine import native
from aar.memory import search as memsearch
from aar.memory.embed import get_embedder
from aar.models import AgentResult
from aar.publish import deliverables as deliv
from aar.publish import github as ghpub
from aar.sources.repository import collect_all, upsert_papers

HOLD_KINDS = ("auth", "budget", "policy_changed", "secret_detected", "publish_scope", "rate_limit", "setup")


def hold(kind: str, message: str) -> ApplicationError:
    """保留にすべき失敗。type に種類を入れ、再試行させない。"""
    return ApplicationError(message, type=f"Hold:{kind}", non_retryable=True)


def _beat() -> None:
    try:
        activity.heartbeat()
    except RuntimeError:
        pass   # アクティビティの外（テストや CLI）から呼ばれた場合


def runners_for(mode: str) -> dict[str, AgentRunner]:
    if mode == "mock":
        from aar.mock import mock_runners

        return mock_runners()
    return {"claude": ClaudeCodeRunner(), "codex": CodexRunner()}


def workspace_of(project_id: str) -> Path:
    return get_settings().workspaces_dir / project_id / "repo"


def audit_root_of(project_id: str) -> Path:
    return get_settings().workspaces_dir / project_id / "audits"


def _policy_for(project: dict[str, Any]) -> Policy:
    """方針を読み、研究開始時に固定したハッシュと違えば保留にする。"""
    policy = load_policy()
    if policy.sha256 != project["policy_sha256"]:
        raise hold("policy_changed", "品質ゲートを含む方針ファイル（config/policy.yaml）が研究の開始後に変更された。"
                                     "意図した変更なら研究の中止と再登録、意図しない変更なら元に戻して再開する")
    return policy


def make_ctx(project_id: str) -> tuple[native.Ctx, dict[str, Any], Policy]:
    with connect() as conn:
        project = store.get_project(conn, project_id)
    policy = _policy_for(project)
    mode = project["execution_mode"]

    def on_cost(purpose: str, key: str, res: AgentResult) -> None:
        with connect() as c:
            store.record_agent_cost(c, project_id, purpose, key + f":{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S%f}", res)

    def remaining() -> float:
        with connect() as c:
            return store.budget_state(c, project_id)["remaining"]

    ctx = native.Ctx(project_id=project_id, ws=workspace_of(project_id), policy=policy, runners=runners_for(mode),
                     on_cost=on_cost, remaining_budget=remaining, heartbeat=_beat)
    return ctx, project, policy


async def _phase(project_id: str, phase: str, key: str, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """1つの工程を実行する。完了済みなら保存済みの結果を返す。"""
    full_key = f"{project_id}:{key}"
    with connect() as conn:
        done = run_once(conn, full_key, "phase")
        if done is not None:
            return done
        store.set_project(conn, project_id, phase=phase, status="running")
        store.log_event(conn, project_id, project_id, "phase_started", {"phase": phase, "key": key,
                                                                         "attempt": _attempt()})
    try:
        result = await fn()
    except native.PhaseError as e:
        if e.kind in ("auth", "budget", "rate_limit"):
            raise hold(e.kind, str(e)) from e
        raise ApplicationError(str(e), type="PhaseError") from e
    with connect() as conn:
        record_once(conn, full_key, "phase", result)
        store.log_event(conn, project_id, project_id, "phase_completed", {"phase": phase, "key": key}, key=f"{full_key}:done")
    return result


def _attempt() -> int:
    try:
        return activity.info().attempt
    except RuntimeError:
        return 1


def _git_identity() -> tuple[str, str]:
    root = Path(__file__).resolve().parents[3]
    def cfg(k: str, default: str) -> str:
        r = subprocess.run(["git", "config", k], cwd=root, capture_output=True, text=True)
        return r.stdout.strip() or default
    return cfg("user.name", "autonomous-ai-research"), cfg("user.email", "autonomous-ai-research@users.noreply.github.com")


# ====================================================================== 研究


@activity.defn
async def prepare_project(project_id: str) -> dict[str, Any]:
    """作業領域を作り、過去の記憶を検索して研究に固定する。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        name, email = _git_identity()
        wsmod.init_workspace(ctx.ws, author_name=name, author_email=email, base_branch=policy.publication.base_branch)
        branch = f"{policy.publication.branch_prefix}{project_id}"
        wsmod.ensure_branch(ctx.ws, branch)
        with connect() as conn:
            prop = store.get_proposal_for_project(conn, project_id)
            body = prop["body"]
            emb = get_embedder(get_settings().embedding_model)
            memsearch.index_pending(conn, emb)
            mem = memsearch.memory_context(conn, emb, project_id=project_id, title=prop["title"],
                                           hypotheses=body.get("hypotheses", []), method=body.get("method", ""),
                                           paper_id=str(prop["paper_id"]) if prop.get("paper_id") else None)
            store.set_project(conn, project_id, workspace_path=str(ctx.ws), branch=branch, memory_context=mem,
                              started_at=dt.datetime.now(dt.UTC))
        return {"branch": branch, "memory_used": bool(mem)}
    return await _phase(project_id, "prepare", "prepare", run)


@activity.defn
async def understand_paper(project_id: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        ctx, project, _ = make_ctx(project_id)
        with connect() as conn:
            prop = store.get_proposal_for_project(conn, project_id)
        return await native.understand(ctx, prop, project.get("memory_context") or "")
    return await _phase(project_id, "understand", "understand", run)


@activity.defn
async def design_and_preregister(project_id: str, cycle: int, previous: dict[str, Any] | None) -> dict[str, Any]:
    """研究設計を作り、事前登録として固定する（ハッシュと Git コミット）。"""
    async def run() -> dict[str, Any]:
        ctx, project, _ = make_ctx(project_id)
        with connect() as conn:
            prop = store.get_proposal_for_project(conn, project_id)
            understanding = run_once(conn, f"{project_id}:understand", "phase") or {}
        design = await native.design(ctx, prop, understanding, cycle, previous, project.get("memory_context") or "")
        sha, commit = wsmod.register_prereg(ctx.ws, cycle, design)
        # 既に登録済みだった場合は、登録済みの内容を正とする
        design = json.loads((ctx.ws / wsmod.prereg_path(cycle)).read_text(encoding="utf-8"))
        with connect() as conn:
            store.save_prereg(conn, project_id, cycle, design, sha, commit)
            store.log_event(conn, project_id, project_id, "preregistered", {"cycle": cycle, "sha256": sha, "commit": commit},
                            key=f"{project_id}:prereg:{cycle}")
        return {"sha256": sha, "commit": commit, "hypotheses": [h["label"] for h in design["hypotheses"]]}
    return await _phase(project_id, "design", f"design:{cycle}", run)


@activity.defn
async def implement_experiment(project_id: str, cycle: int, attempt: int, feedback: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        ctx, _, _ = make_ctx(project_id)
        with connect() as conn:
            prereg = store.get_prereg(conn, project_id, cycle)
        return await native.implement(ctx, prereg["content"], cycle, attempt, feedback)  # type: ignore[index]
    return await _phase(project_id, "implement", f"implement:{cycle}:{attempt}", run)


@activity.defn
async def run_pilot(project_id: str, cycle: int, attempt: int) -> dict[str, Any]:
    """テストと予備実験を隔離環境で実行する。失敗したらログを返し、実装のやり直しに使う。"""
    async def run() -> dict[str, Any]:
        ctx, project, _ = make_ctx(project_id)
        tests = await native.run_tests(ctx)
        if not tests.ok:
            return {"ok": False, "stage": "tests", "log": tests.tail()}
        pilot = await native.run_stage(ctx, cycle, "pilot")
        commit = wsmod.commit_all(ctx.ws, f"予備実験（サイクル{cycle}）")
        with connect() as conn:
            store.upsert_experiment(conn, project_id, cycle, "pilot", f"pilot-{attempt}",
                                    status="completed" if pilot.ok else "failed", duration_s=pilot.duration_s,
                                    log_uri=f"results/cycle{cycle}/pilot/run.log", git_commit=commit,
                                    execution_mode=project["execution_mode"])
        return {"ok": pilot.ok, "stage": "pilot", "log": pilot.tail(), "duration_s": pilot.duration_s}
    return await _phase(project_id, "pilot", f"pilot:{cycle}:{attempt}", run)


@activity.defn
async def run_main_experiment(project_id: str, cycle: int, run_no: int) -> dict[str, Any]:
    """本実験を実行する。事前登録が変わっていないことを先に確かめる。"""
    async def run() -> dict[str, Any]:
        ctx, project, _ = make_ctx(project_id)
        with connect() as conn:
            prereg = store.get_prereg(conn, project_id, cycle)
        ok, detail = wsmod.verify_prereg(ctx.ws, cycle, prereg["content_sha256"], prereg["git_commit"])  # type: ignore[index]
        if not ok and run_no == 1:
            raise ApplicationError(f"事前登録が本実験の前に変更されている: {detail}", type="PreregViolation", non_retryable=True)
        r = await native.run_stage(ctx, cycle, "main")
        commit = wsmod.commit_all(ctx.ws, f"本実験（サイクル{cycle}、{run_no}回目）")
        design = prereg["content"]  # type: ignore[index]
        with connect() as conn:
            store.upsert_experiment(
                conn, project_id, cycle, "main", f"main-{run_no}", status="completed" if r.ok else "failed",
                design=f"{design.get('research_question', '')} / 指標: {design.get('primary_metric', '')} / "
                       f"解析: {design.get('statistical_analysis', '')}",
                config={"sample_size": design.get("sample_size"), "seeds": design.get("seeds")},
                duration_s=r.duration_s, log_uri=f"results/cycle{cycle}/main/run.log", git_commit=commit,
                execution_mode=project["execution_mode"])
        if not r.ok:
            raise ApplicationError(f"本実験が失敗した: {r.tail(1500)}", type="ExperimentFailed", non_retryable=True)
        return {"ok": True, "duration_s": r.duration_s, "commit": commit}
    return await _phase(project_id, "main_experiment", f"main:{cycle}:{run_no}", run)


@activity.defn
async def analyze_results(project_id: str, cycle: int, run_no: int, elapsed_hours: float) -> dict[str, Any]:
    """分析コードを実行し、批判的に検討し、仮説の判定と次の方針を保存する。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        r, analysis = await native.run_analysis(ctx, cycle)
        if not r.ok:
            raise ApplicationError(f"分析が失敗した: {r.tail(1500)}", type="AnalysisFailed", non_retryable=True)
        with connect() as conn:
            prereg = store.get_prereg(conn, project_id, cycle)
        design = prereg["content"]  # type: ignore[index]
        labels = [h["label"] for h in design["hypotheses"]]
        crit = await native.critique(ctx, design, analysis, cycle)
        verdicts = native.final_verdicts(analysis, crit, labels)
        (ctx.ws / "docs" / "critique").mkdir(parents=True, exist_ok=True)
        (ctx.ws / "docs" / "critique" / f"cycle{cycle}.json").write_text(
            json.dumps({"critique": crit, "verdicts": verdicts}, ensure_ascii=False, indent=2), encoding="utf-8")
        commit = wsmod.commit_all(ctx.ws, f"分析と批判的検討（サイクル{cycle}、{run_no}回目）")
        with connect() as conn:
            exp_id = store.upsert_experiment(conn, project_id, cycle, "main", f"main-{run_no}", status="completed",
                                             git_commit=commit, execution_mode=project["execution_mode"])
            store.save_results(conn, project_id, exp_id, cycle, analysis.get("results", []))
            for label, status in verdicts.items():
                store.set_hypothesis_status(conn, project_id, cycle, label, status)
            for o in crit.get("observations", []):
                store.add_observation(conn, project_id, o, "ai_inference", exp_id)
            for o in crit.get("exploratory_findings", []):
                store.add_observation(conn, project_id, "[探索的] " + o, "ai_inference", exp_id, {"exploratory": True})
            for le in crit.get("lessons", []):
                store.add_lesson(conn, project_id, le.get("category", "design"), le.get("content", ""))
            b = store.budget_state(conn, project_id)
        cont, reason = native.decide_continue(policy, cycle, crit, b["ratio"], elapsed_hours)
        nxt = crit.get("next") or {}
        return {"verdicts": verdicts, "continue": cont, "reason": reason,
                "next": {"hypotheses": nxt.get("next_hypotheses", []), "design_changes": nxt.get("next_design_changes", ""),
                         "previous_verdicts": verdicts} if cont else None}
    return await _phase(project_id, "analysis", f"analysis:{cycle}:{run_no}", run)


# ====================================================================== 監査と修正


def _paper_text(prop: dict[str, Any]) -> str:
    return (f"題名: {prop.get('paper_title')}\narXiv: {prop.get('arxiv_id')}\nDOI: {prop.get('doi')}\n"
            f"URL: {prop.get('paper_url')}\n要旨: {prop.get('paper_abstract') or '(なし)'}")


def _latest_cycle(conn: Any, project_id: str) -> int:
    row = fetch_one(conn, "SELECT coalesce(max(cycle), 1) AS c FROM preregistrations WHERE project_id = %s", (project_id,))
    return int(row["c"])  # type: ignore[index]


def _verify_all_preregs(conn: Any, ws: Path, project_id: str) -> tuple[bool, str]:
    rows = fetch_all(conn, "SELECT * FROM preregistrations WHERE project_id = %s ORDER BY cycle", (project_id,))
    if not rows:
        return False, "事前登録の記録がない"
    details = []
    ok_all = True
    for r in rows:
        ok, d = wsmod.verify_prereg(ws, r["cycle"], r["content_sha256"], r["git_commit"])
        ok_all &= ok
        details.append(f"サイクル{r['cycle']}: {d}")
    return ok_all, " / ".join(details)


@activity.defn
async def research_audit(project_id: str, round_no: int) -> dict[str, Any]:
    """独立監査を実行し、品質ゲートを判定する。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        settings = get_settings()
        choice = policy.research.model_for("auditor")
        implementer = policy.research.model_for("implementer")
        implementer_name = f"{implementer.agent}:{implementer.model or 'default'}"
        with connect() as conn:
            prop = store.get_proposal_for_project(conn, project_id)
            prior = store.latest_findings(conn, project_id, "research") if round_no > 1 else []
        try:
            report, res, commit = await raudit.run_research_audit(
                ctx.runners[choice.agent], model=choice.model, skill_dir=settings.research_audit_skill_dir, ws=ctx.ws,
                audit_root=audit_root_of(project_id), round_no=round_no, title=project["title"], paper=_paper_text(prop),
                prior_issues=[p for p in prior if p["severity"] in ("Critical", "Major")], heartbeat=_beat)
        except FileNotFoundError as e:
            raise hold("setup", str(e)) from e
        ctx.on_cost("研究独立監査", f"{project_id}:audit:research:{round_no}", res)
        if not res.ok and res.error_kind in ("auth", "rate_limit"):
            raise hold(res.error_kind, f"監査担当を実行できない: {res.error}")
        tests = await native.run_tests(ctx)
        with connect() as conn:
            prereg_ok, prereg_detail = _verify_all_preregs(conn, ctx.ws, project_id)
            store.save_audit(conn, project_id, round_no, report, implementer=implementer_name, target_commit=commit)
            gate = gates.evaluate_research_gate(
                policy.gates.research, report, tests_passed=tests.ok, prereg_intact=prereg_ok, prereg_detail=prereg_detail,
                implementer=implementer_name, pinned_policy_sha=project["policy_sha256"],
                current_policy_sha=policy_file_sha256(settings.policy_path))
            store.save_gate(conn, project_id, round_no, gate)
        return {"passed": gate.passed, "status": report.status, "verdict": report.publication_verdict,
                "blocking": [c.model_dump() for c in gate.blocking()], "checks": [c.model_dump() for c in gate.checks],
                "n_critical": sum(f.severity == "Critical" for f in report.findings),
                "n_major": sum(f.severity == "Major" for f in report.findings),
                "summary": report.summary[:2000], "report_dir": report.report_dir}
    return await _phase(project_id, "research_audit", f"audit:research:{round_no}", run)


@activity.defn
async def fix_audit_findings(project_id: str, kind: str, round_no: int) -> dict[str, Any]:
    """監査の重大な指摘と、ゲートで不合格になった機械的な確認を直させる。"""
    async def run() -> dict[str, Any]:
        ctx, _, _ = make_ctx(project_id)
        with connect() as conn:
            findings = [f for f in store.latest_findings(conn, project_id, kind) if f["severity"] in ("Critical", "Major")]
            gate_name = "research" if kind == "research" else "publication"
            g = fetch_one(conn, "SELECT checks FROM gate_evaluations WHERE project_id = %s AND gate = %s AND round = %s",
                          (project_id, gate_name, round_no))
            cycle = _latest_cycle(conn, project_id)
        for i, c in enumerate([c for c in (g["checks"] if g else []) if not c["passed"]], 1):
            findings.append({"issue_id": f"G{round_no}{i:02d}", "severity": "Major", "title": f"品質ゲート不合格: {c['name']}",
                             "detail": {"observed": c.get("detail", "")}})
        if not findings:
            return {"changed_paths": [], "research_changed": False, "needs_rerun": False, "prereg_touched": False}
        out = await fixmod.fix_findings(ctx, findings, kind=kind, round_no=round_no, cycle=cycle)
        with connect() as conn:
            conn.execute("""UPDATE audit_findings f SET status = 'fix_attempted' FROM audit_runs a
                            WHERE f.audit_run_id = a.id AND a.project_id = %s AND a.kind = %s AND a.round = %s
                              AND f.severity IN ('Critical', 'Major') AND f.status = 'open'""", (project_id, kind, round_no))
        return out
    return await _phase(project_id, f"fix_{kind}", f"fix:{kind}:{round_no}", run)


# ====================================================================== 成果物と公開


def _verdicts(conn: Any, project_id: str) -> dict[str, str]:
    rows = fetch_all(conn, "SELECT cycle, label, status FROM hypotheses WHERE project_id = %s AND kind = 'preregistered' "
                           "ORDER BY cycle, label", (project_id,))
    multi = len({r["cycle"] for r in rows}) > 1
    return {(f"サイクル{r['cycle']} {r['label']}" if multi else r["label"]): r["status"] for r in rows}


def _facts(conn: Any, project_id: str, project: dict[str, Any]) -> dict[str, Any]:
    exps = fetch_all(conn, "SELECT cycle, kind, name, status, duration_s FROM experiments WHERE project_id = %s "
                           "ORDER BY started_at", (project_id,))
    audit = fetch_one(conn, "SELECT round, auditor, research_verdict, publication_verdict, summary FROM audit_runs "
                            "WHERE project_id = %s AND kind = 'research' ORDER BY round DESC LIMIT 1", (project_id,))
    hyps = fetch_all(conn, "SELECT cycle, label, statement, decision_rule, status FROM hypotheses WHERE project_id = %s "
                           "ORDER BY cycle, label", (project_id,))
    return {
        "研究ID": project_id, "題名": project["title"],
        "実行の種類": "モック（実際の実験・監査ではない）" if project["execution_mode"] == "mock" else "実際の実行",
        "仮説と判定": [{"サイクル": h["cycle"], "ラベル": h["label"], "仮説": h["statement"], "判定基準": h["decision_rule"],
                        "判定": h["status"]} for h in hyps],
        "実験": [{"サイクル": e["cycle"], "種類": e["kind"], "状態": e["status"],
                  "所要秒": float(e["duration_s"]) if e["duration_s"] is not None else None} for e in exps],
        "独立監査": ({"回数": audit["round"], "監査担当": audit["auditor"], "研究妥当性": audit["research_verdict"],
                      "発信可否": audit["publication_verdict"], "要約": audit["summary"][:1500]} if audit else None),
        "実験環境": "ネットワークなしの隔離環境、CPU のみ",
    }


@activity.defn
async def build_deliverables(project_id: str, round_no: int, feedback_from_audit: bool) -> dict[str, Any]:
    """発信用の成果物を作る。数値の表とグラフは分析結果から機械的に作る。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        name, _ = _git_identity()
        with connect() as conn:
            prop = store.get_proposal_for_project(conn, project_id)
            verdicts = _verdicts(conn, project_id)
            facts = _facts(conn, project_id, project)
        static = deliv.write_static(ctx, verdicts, license_spdx="MIT", holder=name)
        if round_no == 1 or not (ctx.ws / "report" / "report.md").exists():
            await deliv.write_narrative(ctx, prop, facts, round_no=round_no)
        deliv.copy_audit(ctx.ws, sorted(audit_root_of(project_id).glob("research-round*/audit")))
        asm = deliv.assemble(ctx.ws, project["title"])
        commit = wsmod.commit_all(ctx.ws, f"発信用の成果物（{round_no}回目）")
        with connect() as conn:
            for kind, path in (("report_md", "report/report.md"), ("report_html", "report/report.html"),
                               ("readme", "README.md"), ("talk_script", "report/talk_script.md"),
                               ("future_work", "report/future_work.md"), ("limitations", "docs/limitations.md"),
                               ("references", "docs/references.md"), ("audit_report", "audit")):
                p = ctx.ws / path
                if p.exists():
                    conn.execute(
                        """INSERT INTO publication_drafts (project_id, kind, path, sha256) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (project_id, kind) DO UPDATE SET sha256 = EXCLUDED.sha256, path = EXCLUDED.path""",
                        (project_id, kind, path, wsmod.file_sha256(p) if p.is_file() else None))
        return {"commit": commit, **static, **asm}
    return await _phase(project_id, "deliverables", f"deliverables:{round_no}", run)


@activity.defn
async def publication_audit(project_id: str, round_no: int) -> dict[str, Any]:
    """GitHub 公開品質監査を実行し、公開ゲートを判定する。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        settings = get_settings()
        choice = policy.research.model_for("auditor")
        implementer = policy.research.model_for("implementer")
        mech = deliv.mechanical_publication_checks(ctx.ws)
        tests = await native.run_tests(ctx)
        with connect() as conn:
            prior = store.latest_findings(conn, project_id, "publication_quality") if round_no > 1 else []
        mech_view = {k: v for k, v in mech.items() if k != "secrets"} | {"secrets": [h["path"] + " / " + h["kind"] for h in mech["secrets"]]}
        report, res, commit = await pubaudit.run_publication_audit(
            ctx.runners[choice.agent], model=choice.model, skill_dir=settings.publication_audit_skill_dir, ws=ctx.ws,
            audit_root=audit_root_of(project_id), round_no=round_no, mech=mech_view | {"tests_passed": tests.ok},
            prior_issues=[p for p in prior if p["severity"] in ("Critical", "Major")], heartbeat=_beat)
        ctx.on_cost("公開品質監査", f"{project_id}:audit:publication:{round_no}", res)
        if not res.ok and res.error_kind in ("auth", "rate_limit"):
            raise hold(res.error_kind, f"監査担当を実行できない: {res.error}")
        # 機械的な確認で見つかった問題は、監査担当の採点にかかわらず指摘として残す
        from aar.models import AuditFinding
        if mech["secrets"]:
            report.findings.append(AuditFinding(issue_id=f"S{round_no}01", severity="Critical", category="security",
                                                title="秘密情報または作業用パスらしい記述がある",
                                                detail={"evidence": "; ".join(h["path"] + "（" + h["kind"] + "）" for h in mech["secrets"][:20])}))
        if mech["unknown_numbers"]:
            report.findings.append(AuditFinding(issue_id=f"N{round_no}01", severity="Major", category="grounding",
                                                title="分析結果に由来が見つからない数値が文章にある",
                                                detail={"evidence": "; ".join(mech["unknown_numbers"][:30]),
                                                        "proposed_fix": "表（report/results.md）の値をそのまま写すか、その数値を削る"}))
        if mech["missing_sections"]:
            report.findings.append(AuditFinding(issue_id=f"D{round_no}01", severity="Major", category="documentation",
                                                title="報告書に決まった節がない", detail={"evidence": "、".join(mech["missing_sections"])}))
        with connect() as conn:
            store.save_audit(conn, project_id, round_no, report,
                             implementer=f"{implementer.agent}:{implementer.model or 'default'}", target_commit=commit)
            gate = gates.evaluate_publication_gate(
                policy.gates.publication, report, secrets_found=len(mech["secrets"]), has_limitations=mech["has_limitations"],
                has_repro=mech["has_repro"], has_license=mech["has_license"], tests_passed=tests.ok,
                claims_grounded=not mech["unknown_numbers"], pinned_policy_sha=project["policy_sha256"],
                current_policy_sha=policy_file_sha256(settings.policy_path))
            store.save_gate(conn, project_id, round_no, gate)
            if gate.passed:
                conn.execute("UPDATE publication_drafts SET status = 'gate_passed' WHERE project_id = %s", (project_id,))
        return {"passed": gate.passed, "score": report.score, "status": report.status, "skill": report.skill,
                "blocking": [c.model_dump() for c in gate.blocking()], "checks": [c.model_dump() for c in gate.checks],
                "n_critical": sum(f.severity == "Critical" for f in report.findings),
                "n_major": sum(f.severity == "Major" for f in report.findings), "summary": report.summary[:2000]}
    return await _phase(project_id, "publication_audit", f"audit:publication:{round_no}", run)


@activity.defn
async def refresh_deliverables(project_id: str, round_no: int) -> dict[str, Any]:
    """修正のあと、表・グラフ・HTML を作り直す（文章は修正担当が直したものを使う）。"""
    async def run() -> dict[str, Any]:
        ctx, project, _ = make_ctx(project_id)
        name, _ = _git_identity()
        with connect() as conn:
            verdicts = _verdicts(conn, project_id)
        deliv.write_static(ctx, verdicts, license_spdx="MIT", holder=name)
        deliv.copy_audit(ctx.ws, sorted(audit_root_of(project_id).glob("research-round*/audit")))
        asm = deliv.assemble(ctx.ws, project["title"])
        return {"commit": wsmod.commit_all(ctx.ws, f"成果物の更新（{round_no}回目）"), **asm}
    return await _phase(project_id, "deliverables", f"refresh:{round_no}", run)


@activity.defn
async def submit_candidate(project_id: str) -> dict[str, Any]:
    """公開候補を提出する（専用ブランチの push と PR）。push 先がなければローカルのブランチに留める。"""
    async def run() -> dict[str, Any]:
        ctx, project, policy = make_ctx(project_id)
        deliv.copy_audit(ctx.ws, sorted(audit_root_of(project_id).glob("*-round*/audit")))
        wsmod.commit_all(ctx.ws, "独立監査と公開品質監査の報告を追加")
        with connect() as conn:
            verdicts = _verdicts(conn, project_id)
            rg = fetch_one(conn, "SELECT checks FROM gate_evaluations WHERE project_id = %s AND gate = 'research' ORDER BY round DESC LIMIT 1", (project_id,))
            pg = fetch_one(conn, "SELECT checks FROM gate_evaluations WHERE project_id = %s AND gate = 'publication' ORDER BY round DESC LIMIT 1", (project_id,))
            ar = fetch_one(conn, "SELECT summary FROM audit_runs WHERE project_id = %s AND kind = 'research' ORDER BY round DESC LIMIT 1", (project_id,))
            pa = fetch_one(conn, "SELECT score FROM audit_runs WHERE project_id = %s AND kind = 'publication_quality' ORDER BY round DESC LIMIT 1", (project_id,))
        body = ghpub.pr_body(project_id=project_id, title=project["title"], verdicts=verdicts,
                             research_gate=rg["checks"] if rg else [], publication_gate=pg["checks"] if pg else [],
                             audit_summary=ar["summary"] if ar else "", score=float(pa["score"]) if pa and pa["score"] is not None else None,
                             mode=project["execution_mode"])
        (ctx.ws.parent / "pr_body.md").write_text(body, encoding="utf-8")
        if project["execution_mode"] == "mock":
            result: dict[str, Any] = {"status": "skipped", "reason": "モック実行の結果は GitHub に出さない"}
        else:
            result = await asyncio.to_thread(ghpub.submit_pull_request, ctx.ws, branch=project["branch"],
                                             title=f"研究: {project['title']}", body=body, policy=policy.publication,
                                             remote_repo=project.get("remote_repo"))
        with connect() as conn:
            decision = "auto_allowed" if result["status"] in ("created", "exists") else "pending"
            from psycopg.types.json import Jsonb
            conn.execute(
                """INSERT INTO publication_approvals (project_id, channel, decision, decided_by, detail)
                   VALUES (%s, 'github_pr', %s, 'policy', %s)
                   ON CONFLICT (project_id, channel) DO UPDATE SET decision = EXCLUDED.decision, detail = EXCLUDED.detail""",
                (project_id, decision, Jsonb(result)))
            if result.get("url"):
                store.set_project(conn, project_id, pr_url=result["url"])
        return result
    return await _phase(project_id, "submit", "submit", run)


@activity.defn
async def finalize_project(project_id: str, outcome: str, reason: str) -> dict[str, Any]:
    """結果を確定する。成功・失敗・保留を区別して記録し、通知し、次の研究で使える記憶を残す。"""
    with connect() as conn:
        project = store.get_project(conn, project_id)
        ws = workspace_of(project_id)
        status = {"publishable": "completed", "publishable_with_revisions": "completed", "not_publishable": "completed",
                  "failed": "failed", "cancelled": "cancelled", "on_hold": "on_hold"}[outcome]
        if (ws / ".git").exists() and outcome != "on_hold":
            commit = wsmod.head(ws)
            for pattern, kind, reusable in (("experiments/*.py", "code", False), ("experiments/analyze.py", "eval_code", True),
                                            ("tests/*.py", "code", False), ("results/cycle*/*/raw.jsonl", "data", False),
                                            ("results/cycle*/analysis/analysis.json", "data", False),
                                            ("report/figures/*.svg", "figure", False), ("report/report.*", "report", False)):
                for p in sorted(ws.glob(pattern)):
                    rel = p.relative_to(ws).as_posix()
                    store.register_artifact(conn, project_id, kind, rel, sha256=wsmod.file_sha256(p), size=p.stat().st_size,
                                            git_commit=commit, reusable=reusable,
                                            description=f"{project['title']} の{'評価・分析コード' if reusable else kind}")
        b = store.budget_state(conn, project_id)
        if outcome != "on_hold":
            n_rounds = fetch_one(conn, "SELECT count(*) n FROM audit_runs WHERE project_id = %s AND kind = 'research'", (project_id,))
            store.add_lesson(conn, project_id, "cost",
                             f"「{project['title']}」は費用 {b['spent']:.2f} ドル（上限 {b['budget']:.0f}）、研究監査 {n_rounds['n']} 回で"  # type: ignore[index]
                             f" {outcome} になった。理由: {reason[:200]}", origin="measured")
            for f in fetch_all(conn, """SELECT DISTINCT f.title FROM audit_findings f WHERE f.project_id = %s
                                        AND f.severity IN ('Critical', 'Major') LIMIT 8""", (project_id,)):
                store.add_lesson(conn, project_id, "audit", f"監査で重大な指摘になった点: {f['title']}", origin="measured")
        fields: dict[str, Any] = {"status": status, "outcome": outcome, "outcome_reason": reason, "phase": "finished"}
        if outcome != "on_hold":
            fields["finished_at"] = dt.datetime.now(dt.UTC)
        store.set_project(conn, project_id, **fields)
        level = {"publishable": "success", "publishable_with_revisions": "success", "not_publishable": "warning",
                 "failed": "error", "cancelled": "info", "on_hold": "warning"}[outcome]
        label = {"publishable": "完了（公開候補あり）", "publishable_with_revisions": "完了（修正つきで公開可）",
                 "not_publishable": "完了（公開不可）", "failed": "失敗", "cancelled": "中止", "on_hold": "保留"}[outcome]
        mock = "【モック】" if project["execution_mode"] == "mock" else ""
        store.notify(conn, project_id, level, f"{mock}{label}: {project['title'][:60]}", reason[:500],
                     f"{project_id}:final:{outcome}", macos=bool(load_policy().notify.get("macos", True)))
        store.log_event(conn, project_id, project_id, "finalized", {"outcome": outcome, "reason": reason, "spent_usd": b["spent"]})
        try:
            memsearch.index_pending(conn, get_embedder(get_settings().embedding_model))
        except Exception:  # noqa: BLE001 - 索引づけの失敗で結果の確定を止めない（次回の収集時に作り直される）
            conn.rollback()
    return {"outcome": outcome, "spent_usd": b["spent"]}


@activity.defn
async def mark_hold(project_id: str, kind: str, message: str, seq: int) -> None:
    """研究を保留にして通知する。"""
    with connect() as conn:
        project = store.get_project(conn, project_id)
        store.set_project(conn, project_id, status="on_hold", outcome_reason=f"[{kind}] {message}"[:1000])
        store.log_event(conn, project_id, project_id, "on_hold", {"kind": kind, "message": message}, key=f"{project_id}:hold:{seq}")
        title = {"auth": "認証が必要", "budget": "費用上限に到達", "policy_changed": "方針ファイルが変更された",
                 "rate_limit": "利用上限のため待機", "setup": "設定が不足", "paused": "一時停止"}.get(kind, "保留")
        store.notify(conn, project_id, "warning", f"{title}: {project['title'][:50]}", message[:500],
                     f"{project_id}:hold:{seq}", macos=bool(load_policy().notify.get("macos", True)))


@activity.defn
async def mark_resumed(project_id: str, by: str, seq: int) -> None:
    with connect() as conn:
        store.set_project(conn, project_id, status="running", outcome_reason=None)
        store.log_event(conn, project_id, project_id, "resumed", {"by": by}, key=f"{project_id}:resume:{seq}")
        if by == "user":
            conn.execute("INSERT INTO human_interventions (project_id, kind) VALUES (%s, 'resume')", (project_id,))


@activity.defn
async def set_budget(project_id: str, budget_usd: float) -> None:
    with connect() as conn:
        store.set_project(conn, project_id, budget_usd=budget_usd)
        from psycopg.types.json import Jsonb
        conn.execute("INSERT INTO human_interventions (project_id, kind, detail) VALUES (%s, 'budget_change', %s)",
                     (project_id, Jsonb({"budget_usd": budget_usd})))


# ====================================================================== 論文収集と企画


@activity.defn
async def ingest_papers(day: str) -> dict[str, Any]:
    """日次の論文収集。同じ日の再実行は、取り込み済みの論文を更新するだけで重複させない。"""
    settings, policy = get_settings(), load_policy()
    key = f"ingest:{day}"
    with connect() as conn:
        conn.execute("INSERT INTO ingest_runs (idempotency_key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
    if settings.mode == "mock":
        from aar.mock import mock_papers
        records, errors = mock_papers(), []
    else:
        task = asyncio.ensure_future(collect_all(policy.ingest, settings, dt.date.fromisoformat(day)))
        while not task.done():
            await asyncio.wait({task}, timeout=15)
            _beat()
        records, errors = task.result()
    with connect() as conn:
        stats = upsert_papers(conn, records)
        scored = discovery.score_unscored(conn, policy.scoring)
        indexed = memsearch.index_pending(conn, get_embedder(settings.embedding_model), limit=2000)
        from psycopg.types.json import Jsonb
        conn.execute("""UPDATE ingest_runs SET finished_at = now(), fetched = %s, inserted = %s, updated = %s, merged = %s,
                        errors = %s WHERE idempotency_key = %s""",
                     (len(records), stats["inserted"], stats["updated"], stats["merged"], Jsonb(errors), key))
    return {"fetched": len(records), **stats, "scored": scored, "indexed": indexed, "errors": errors}


@activity.defn
async def select_candidates(batch_key: str) -> list[str]:
    """企画の候補にする論文を選ぶ（粗い採点の上位＋実務重視の別枠）。LLM での採点も行う。"""
    settings, policy = get_settings(), load_policy()
    runners = runners_for(settings.mode)
    choice = policy.research.model_for("scorer")
    with connect() as conn:
        cands = discovery.shortlist(conn, policy.scoring)
    scored: list[tuple[float, bool, str]] = []
    for p in cands:
        pid = str(p["id"])
        scorer = f"{choice.agent}:{choice.model or 'default'}"
        with connect() as conn:
            row = fetch_one(conn, "SELECT total FROM paper_scores WHERE paper_id = %s AND scorer = %s AND rubric_version = %s "
                                  "AND weights_sha256 = %s", (pid, scorer, policy.scoring.rubric_version, policy.scoring.weights_sha256()))
        if row:
            scored.append((float(row["total"]), bool(p.get("_reserve")), pid))
            continue
        res = await runners[choice.agent].run(discovery.score_prompt(p), cwd=settings.data_dir, model=choice.model,
                                              schema=discovery.SCORE_SCHEMA, tools=[], max_budget_usd=0.3, timeout_s=300,
                                              heartbeat=_beat)
        with connect() as conn:
            store.record_agent_cost(conn, None, "論文の価値評価", f"score:{pid}:{scorer}:{batch_key}", res)
            if res.ok and res.structured:
                s = discovery.parse_llm_score(pid, res.structured, policy.scoring, scorer)
                discovery.save_score(conn, s, policy.scoring)
                scored.append((s.total, bool(p.get("_reserve")), pid))
        if not res.ok and res.error_kind in ("auth", "rate_limit"):
            raise hold(res.error_kind, f"採点担当を実行できない: {res.error}")
    n = policy.scoring.proposals_per_week
    slots = int((policy.scoring.practical_reserve or {}).get("slots", 0))
    ranked = sorted(scored, key=lambda t: -t[0])
    chosen = [pid for _, _, pid in ranked[: max(n - slots, 1)]]
    # 別枠: 実務重視の候補のうち、LLM の採点でも実務上の重要性が高いもの
    for total, reserve, pid in ranked:
        if len(chosen) >= n:
            break
        if pid not in chosen and (reserve or total >= 0):
            chosen.append(pid)
    return chosen[:n]


@activity.defn
async def generate_proposal(batch_key: str, paper_id: str) -> dict[str, Any]:
    settings, policy = get_settings(), load_policy()
    with connect() as conn:
        if fetch_one(conn, "SELECT 1 FROM research_proposals WHERE batch_key = %s AND paper_id = %s", (batch_key, paper_id)):
            return {"created": False, "reason": "作成済み"}
        paper = fetch_one(conn, "SELECT * FROM papers WHERE id = %s", (paper_id,))
        score = fetch_one(conn, "SELECT criteria, total, scorer FROM paper_scores WHERE paper_id = %s ORDER BY (scorer = 'heuristic'), created_at DESC LIMIT 1", (paper_id,))
        emb = get_embedder(settings.embedding_model)
        mem = memsearch.memory_context(conn, emb, project_id="", title=paper["title"], hypotheses=[], method="",  # type: ignore[index]
                                       paper_id=paper_id)
        record = discovery.track_record(conn)
    choice = policy.research.model_for("planner")
    runners = runners_for(settings.mode)
    res = await runners[choice.agent].run(
        discovery.proposal_prompt(paper, score or {}, mem, record, policy.research.budget_usd, ["numpy"]),  # type: ignore[arg-type]
        cwd=settings.data_dir, model=choice.model, schema=discovery.PROPOSAL_SCHEMA, tools=[], max_budget_usd=1.0,
        timeout_s=600, heartbeat=_beat)
    with connect() as conn:
        store.record_agent_cost(conn, None, "企画の生成", f"proposal:{batch_key}:{paper_id}:{_attempt()}", res)
        if not res.ok:
            if res.error_kind in ("auth", "rate_limit"):
                raise hold(res.error_kind, f"企画担当を実行できない: {res.error}")
            raise ApplicationError(f"企画を生成できない: {res.error}", type="ProposalFailed")
        s = res.structured or {}
        if not s.get("feasible"):
            return {"created": False, "reason": f"実行できない: {s.get('infeasible_reason', '')}"[:300]}
        prop = discovery.to_proposal(s, paper)  # type: ignore[arg-type]
        pid = discovery.save_proposal(conn, batch_key, paper_id, prop, float(score["total"]) if score else 0.0,
                                      f"{choice.agent}:{choice.model or 'default'}",
                                      "mock" if settings.mode == "mock" else "real")
    return {"created": bool(pid), "proposal_id": pid, "title": prop.title}


@activity.defn
async def notify_proposals(batch_key: str, created: int, skipped: list[str]) -> None:
    with connect() as conn:
        conn.execute("UPDATE research_proposals SET status = 'expired' WHERE status = 'proposed' AND batch_key <> %s "
                     "AND created_at < now() - interval '28 days'", (batch_key,))
        body = f"{created} 件の検証企画ができた。" + (f"見送り {len(skipped)} 件。" if skipped else "")
        store.notify(conn, None, "info" if created else "warning", f"今週の検証企画（{batch_key}）", body, f"proposals:{batch_key}",
                     macos=bool(load_policy().notify.get("macos", True)))


ALL_ACTIVITIES = [
    prepare_project, understand_paper, design_and_preregister, implement_experiment, run_pilot, run_main_experiment,
    analyze_results, research_audit, fix_audit_findings, build_deliverables, publication_audit, refresh_deliverables,
    submit_candidate, finalize_project, mark_hold, mark_resumed, set_budget, ingest_papers, select_candidates,
    generate_proposal, notify_proposals,
]
