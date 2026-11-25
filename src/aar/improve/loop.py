"""改善の一巡: 弱点の抽出 → 評価計画の固定と基準値 → 候補の生成（隔離した複製）→ 評価 → 採否の判定。

稼働中のチェックアウト、研究の原本、研究の記録は変更しない。読むのは研究監査の記録だけで、書くのは次の場所だけ。
  * 台帳（improvement_* の表）
  * data_dir/improve/ 以下（封印評価の seed と課題、実行の生の出力、候補を作るための複製）
候補は、基盤のリポジトリを複製した別の作業領域で作る。基盤への反映（マージ、配備）は、ここでは行わない。
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psycopg

from aar.config import PROJECT_ROOT, get_settings
from aar.db.core import fetch_all, fetch_one
from aar.improve import evalkit, ledger
from aar.improve.decide import DEFAULT_PLAN, decide, plan_sha256
from aar.improve.ledger import Actor
from aar.improve.targets import TARGETS, check_patch, evaluator_sha256

WEAKNESS_FIX_CONTEXT = "fix_context_unresolved_refs"


def workdir() -> Path:
    d = get_settings().data_dir / "improve"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check, timeout=300).stdout.strip()


def _sealed_seed() -> str:
    """封印評価の合成課題を決める seed。リポジトリの外（data_dir）に置き、候補を作る側には渡さない。"""
    path = workdir() / "sealed_seed"
    if not path.exists():
        path.write_text(secrets.token_hex(16), encoding="utf-8", newline="\n")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def _deny_read() -> list[Path]:
    """評価対象のプロセスに読ませない場所（研究の記録、封印評価の課題、候補の複製、基盤のチェックアウト）。"""
    s = get_settings()
    return [s.workspaces_dir, s.data_dir, PROJECT_ROOT]


# ---------------------------------------------------------------------- 弱点の抽出

def scan_weaknesses(workspaces: Path | None = None) -> list[dict[str, Any]]:
    """実行の記録から、基盤の弱点を機械的に拾う。いまは「修正担当への文脈に、参照先の定義が入っていない」だけを見る。"""
    cases = evalkit.cases_from_records(workspaces or get_settings().workspaces_dir, DEFAULT_PLAN["budget_chars"])
    per = [{"case": c["id"], "group": c["group"], "refs": sorted(c["key"]["refs"])} for c in cases if c["key"]["refs"]]
    refs = sum(len(p["refs"]) for p in per)
    if not refs:
        return []
    return [{
        "kind": WEAKNESS_FIX_CONTEXT, "target": "fix_context", "fingerprint": ledger.fingerprint(WEAKNESS_FIX_CONTEXT, "fix_context"),
        "frequency": {"fix_rounds": len(cases), "rounds_with_unresolved_refs": len(per), "unresolved_refs": refs,
                      "groups": len({p["group"] for p in per})},
        "evidence": per,
        "problem": f"研究監査の修正 {len(cases)} 回のうち {len(per)} 回で、修正担当に渡した指摘が主張・証拠の ID を参照しているのに、"
                   f"その定義（主張の本文、証拠の所在）を渡していなかった（参照 {refs} 件）。",
        "hypothesis": "指摘が参照する主張・証拠の定義を、予算の範囲で文脈に添えれば、修正担当が参照先を特定できないまま対応することが減る。",
        "applies_when": "指摘が主張や証拠を ID で参照していて、その回の監査の主張・証拠の一覧が手元にあるとき",
        "side_effects": "文脈が長くなる。監査担当が書いた文章（信頼しない入力）が増える。",
    }]


def propose_from_weakness(conn: psycopg.Connection, weakness: dict[str, Any], actor: Actor, base_commit: str | None = None) -> dict[str, Any]:
    target = TARGETS[weakness["target"]]
    return ledger.propose(
        conn, target=target.name, base_commit=base_commit or _git(PROJECT_ROOT, "rev-parse", "HEAD"), origin_kind="run_record",
        origin_fingerprint=weakness["fingerprint"], origin={"kind": weakness["kind"], "frequency": weakness["frequency"],
                                                             "evidence": weakness["evidence"]},
        problem=weakness["problem"], hypothesis=weakness["hypothesis"], applies_when=weakness["applies_when"],
        side_effects=weakness["side_effects"], allowed_paths=list(target.allowed_paths), actor=actor, area_key=None,
        max_per_origin=DEFAULT_PLAN["limits"]["max_candidates_per_origin"])


# ---------------------------------------------------------------------- 評価計画と基準値

def _assign_splits(real: list[dict[str, Any]]) -> dict[str, list[str]]:
    """実物の課題のグループを分割に割り当てる。課題のいちばん多いグループを開発用に、残りを封印評価にする（規則は固定）。"""
    sizes: dict[str, int] = {}
    for c in real:
        sizes[c["group"]] = sizes.get(c["group"], 0) + 1
    order = sorted(sizes, key=lambda g: (-sizes[g], g))
    return {"dev": order[:1], "select": [], "sealed": order[1:]}


def freeze_plan(conn: psycopg.Connection, actor: Actor, *, workspaces: Path | None = None,
                overrides: dict[str, Any] | None = None, target: str = "fix_context") -> dict[str, Any]:
    """評価計画と課題を固定する。候補を作る前に行う。課題（正解を含む）は data_dir に保存し、ハッシュを計画に結び付ける。"""
    spec = evalkit.spec_for(target)
    plan = json.loads(json.dumps(DEFAULT_PLAN)) | json.loads(json.dumps(spec.plan)) | {"target": target} | (overrides or {})
    real = spec.cases_from_records(workspaces or get_settings().workspaces_dir, plan["budget_chars"])
    plan["splits"] = _assign_splits(real)
    seed = _sealed_seed()
    cases = {
        "dev": [c for c in real if c["group"] in plan["splits"]["dev"]] + spec.synthetic_cases("dev-public", plan["budget_chars"], 6, "dev"),
        "select": spec.synthetic_cases(seed + ":select", plan["budget_chars"], plan["synthetic_cases"]["select"], "select"),
        "sealed": [c for c in real if c["group"] in plan["splits"]["sealed"]]
                  + spec.synthetic_cases(seed + ":sealed", plan["budget_chars"], plan["synthetic_cases"]["sealed"], "sealed"),
    }
    cases_sha = evalkit.cases_sha256([c for split in ("dev", "select", "sealed") for c in cases[split]])
    plan["cases"] = {k: len(v) for k, v in cases.items()}
    plan["cases_sha256"] = cases_sha            # 課題が変われば、別の計画になる
    sha = plan_sha256(plan)
    row = ledger.freeze_plan(conn, target=plan["target"], plan=plan, plan_sha=sha, evaluator_sha=evaluator_sha256(PROJECT_ROOT),
                             cases_sha=cases_sha, actor=actor)
    path = workdir() / "plans" / sha
    path.mkdir(parents=True, exist_ok=True)
    if not (path / "cases.json").exists():
        (path / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, default=str), encoding="utf-8", newline="\n")
        (path / "cases.json").chmod(0o600)
    return row


def _plan_cases(plan_row: dict[str, Any], split: str) -> list[dict[str, Any]]:
    path = workdir() / "plans" / plan_row["plan_sha256"] / "cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    flat = [c for s in ("dev", "select", "sealed") for c in cases[s]]
    if evalkit.cases_sha256(flat) != plan_row["cases_sha256"]:
        raise RuntimeError("評価の課題が、計画を固定したときから変わっている（期待結果の改変の疑い）。評価しない")
    return cases[split]


def _run(conn: psycopg.Connection, plan_row: dict[str, Any], *, candidate_id: str | None, variant: str, split: str, commit: str,
         source: str, run_key: str, actor: Actor, isolation: str | None) -> dict[str, Any]:
    existing = ledger.get_run(conn, run_key)
    if existing:                                             # 中断・再開: 済んだ実行はやり直さない
        return existing | {"created": False}
    plan, target = plan_row["plan"], TARGETS[plan_row["target"]]
    if evaluator_sha256(PROJECT_ROOT) != plan_row["evaluator_sha256"]:
        result: dict[str, Any] = {"status": "failed", "failure_kind": "evaluator_changed", "isolation": isolation or "none", "seconds": 0.0,
                                  "error": "評価器が、計画を固定したときから変わっている", "metrics": {}, "per_case": [], "outputs": {}}
    else:
        result = evalkit.evaluate(source, target, _plan_cases(plan_row, split), isolation=isolation or plan["required_isolation"],
                                  deny_read=_deny_read(), timeout_s=plan["limits"]["timeout_s"])
    raw = workdir() / "runs" / f"{run_key.replace(':', '_').replace('/', '_')}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({"run_key": run_key, "error": result.get("error", ""), "per_case": result["per_case"],
                               "outputs": result.get("outputs", {})}, ensure_ascii=False, indent=1, default=str), encoding="utf-8", newline="\n")
    return ledger.record_run(conn, run_key=run_key, plan_id=plan_row["id"], candidate_id=candidate_id, variant=variant, split=split,
                             commit_sha=commit, result=result, environment=evalkit.environment() | {"cases": len(result["per_case"]), "isolation_detail": result.get("isolation_detail", {})},
                             raw_path=str(raw), actor=actor)


def run_baseline(conn: psycopg.Connection, plan_id: int, actor: Actor, *, commit: str | None = None,
                 isolation: str | None = None) -> dict[str, dict[str, Any]]:
    """現行方式の基準値を測る（開発用・候補選択用・封印評価の3つ）。候補を作る前に行う。"""
    plan_row = ledger.get_plan(conn, plan_id)
    target = TARGETS[plan_row["target"]]
    commit = commit or _git(PROJECT_ROOT, "rev-parse", "HEAD")
    source = _git(PROJECT_ROOT, "show", f"{commit}:{target.module_path}") + "\n"
    return {split: _run(conn, plan_row, candidate_id=None, variant="baseline", split=split, commit=commit, source=source,
                        run_key=f"baseline:{plan_id}:{commit[:12]}:{split}", actor=actor, isolation=isolation)
            for split in ("dev", "select", "sealed")}


# ---------------------------------------------------------------------- 候補の生成（隔離した複製）

def candidate_dir(candidate_id: str) -> Path:
    return workdir() / "candidates" / candidate_id


def prepare_clone(conn: psycopg.Connection, candidate_id: str, plan_id: int) -> Path:
    """候補を作るための、基盤のリポジトリの複製を用意する。稼働中のチェックアウトとは別の場所で、リモートは持たせない。

    複製には開発用の課題（正解つき）と作業の説明だけを置く。候補選択用と封印評価の課題は置かない。
    """
    cand, plan_row = ledger.get_candidate(conn, candidate_id), ledger.get_plan(conn, plan_id)
    repo = candidate_dir(candidate_id) / "repo"
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(PROJECT_ROOT), str(repo)], check=True, capture_output=True, timeout=300)
        _git(repo, "checkout", "-q", "-B", f"improve/{candidate_id}", cand["base_commit"])
        _git(repo, "remote", "remove", "origin", check=False)
        for key, value in (("user.name", "aar-improve"), ("user.email", "aar-improve@users.noreply.github.com")):
            _git(repo, "config", key, value)
    task = repo / ".improve_task"
    task.mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(".improve_task/\n", encoding="utf-8", newline="\n")
    target = TARGETS[cand["target"]]
    (task / "dev_cases.json").write_text(json.dumps(_plan_cases(plan_row, "dev"), ensure_ascii=False, indent=1, default=str), encoding="utf-8", newline="\n")
    (task / "task.md").write_text(f"""# 改善の作業

対象: `{target.module_path}` の関数 `{target.entry}`（{target.description}）。

## 問題
{cand["problem"]}

## 仮説
{cand["hypothesis"]}

## してよいこと
- 変更してよいのは `{target.module_path}` だけ。関数の名前と引数は変えない。標準ライブラリ以外を import しない。
- `.improve_task/dev_cases.json` は開発用の課題。`inputs` が関数の引数、`key` が採点に使う正解。

## 採点（別の課題で行う。採点の処理と基準は変更できない）
{evalkit.spec_for(target.name).task_notes}
""", encoding="utf-8", newline="\n")
    return repo


def register_patch(conn: psycopg.Connection, candidate_id: str, actor: Actor, generation: dict[str, Any] | None = None) -> dict[str, Any]:
    """複製での変更をコミットし、許可された範囲に収まっているかを確かめて、差分を台帳に入れる。"""
    cand = ledger.get_candidate(conn, candidate_id)
    repo, target = candidate_dir(candidate_id) / "repo", TARGETS[cand["target"]]
    _git(repo, "add", "-A")
    if _git(repo, "status", "--porcelain"):
        _git(repo, "commit", "-q", "-m", f"改善候補 {candidate_id}")
    head = _git(repo, "rev-parse", "HEAD")
    problems = check_patch(repo, cand["base_commit"], head, target)
    patch = _git(repo, "diff", cand["base_commit"], head) if head != cand["base_commit"] else ""
    import hashlib

    ledger.set_candidate(conn, candidate_id, actor, "patch_registered" if not problems else "patch_invalid",
                         patch=patch, patch_sha256=hashlib.sha256(patch.encode()).hexdigest(), candidate_commit=head,
                         generation=generation or {}, status="generated" if not problems else "invalid",
                         status_reason="; ".join(problems))
    return {"candidate_id": candidate_id, "commit": head, "problems": problems, "patch_bytes": len(patch.encode())}


async def generate(conn: psycopg.Connection, candidate_id: str, plan_id: int, actor: Actor, runner: Any, *, model: str = "",
                   max_budget_usd: float | None = None) -> dict[str, Any]:
    """実行担当（エージェント）に候補を作らせる。複製の中だけで作業させ、済んでいれば呼び直さない。

    利用上限のときは、候補を proposed のまま残して理由を記録する（こちらからは再試行しない）。
    実行担当の CLI は外部と通信する。複製と作業の説明の内容は、モデルの提供元に送られる。
    """
    cand = ledger.get_candidate(conn, candidate_id)
    if cand["status"] != "proposed":                          # 中断・再開: 生成が済んだ候補を作り直さない
        return {"candidate_id": candidate_id, "skipped": f"状態が {cand['status']}"}
    # 候補を作る側の隔離は未完成（ADR-0012）。実行担当の CLI は利用者の権限で動き、本番の DB・研究の原本・認証情報・
    # 封印評価の課題の読み取りを、仕組みとしては防げない。人が明示して認めたときだけ動かし、その事実を記録する
    unisolated_ok = get_settings().improve_allow_unisolated_generator
    if not unisolated_ok:
        ledger.log_event(conn, candidate_id, "generation_refused", actor,
                         {"reason": "候補を作る側の隔離が未完成。AAR_IMPROVE_ALLOW_UNISOLATED_GENERATOR を人が設定したときだけ生成する"})
        return {"candidate_id": candidate_id, "ok": False, "error_kind": "generator_isolation_unavailable",
                "error": "候補を作る側の隔離が未完成のため、生成しない（弱い方式へは切り替えない）"}
    repo = prepare_clone(conn, candidate_id, plan_id)
    budget = max_budget_usd or ledger.get_plan(conn, plan_id)["plan"]["limits"]["max_generation_usd"]
    prompt = ("作業ディレクトリは、基盤のリポジトリの複製である。`.improve_task/task.md` を読み、そこに書かれた範囲で改善を実装する。"
              "変更してよいファイルは task.md に書かれた 1 つだけ。ほかのファイルは変更しない。")
    res = await runner.run(prompt, cwd=repo, model=model, writable=True, max_budget_usd=budget, timeout_s=1500,
                           log_path=candidate_dir(candidate_id) / "generation.log")
    from aar import store

    store.record_agent_cost(conn, None, f"改善候補の生成（{candidate_id}）", f"improve:generate:{candidate_id}:{secrets.token_hex(4)}", res)
    generation = {"agent": res.agent, "model": res.model or model, "tools": "Read, Glob, Grep, Write, Edit", "billing": res.billing,
                  "cost_usd": res.cost_usd, "cost_is_estimate": res.cost_is_estimate, "usage": res.usage, "duration_s": res.duration_s,
                  "session_id": res.session_id, "isolation": "基盤の複製（リモートなし）を作業ディレクトリにした CLI の実行。OS による読み取りの制限はない",
                  "generator_isolation": "none（未完成。人の設定 AAR_IMPROVE_ALLOW_UNISOLATED_GENERATOR で許可）",
                  # 利用上限の解除待ちで呼び出さなかった場合（内部で待ちを返した）は、外部への呼び出しに数えない
                  "external_model_calls": 0 if (res.error_kind == "rate_limit" and "それまでは問い合わせない" in (res.error or "")) else 1}
    if not res.ok:
        ledger.log_event(conn, candidate_id, "generation_deferred" if res.error_kind == "rate_limit" else "generation_failed",
                         Actor(res.agent if res.agent in ledger.ACTOR_KINDS else "claude_code", "aar.improve.generate"),
                         {"error_kind": res.error_kind, "error": (res.error or "")[:400], "retry_at": str(res.retry_at or "")})
        return {"candidate_id": candidate_id, "ok": False, "error_kind": res.error_kind, "retry_at": str(res.retry_at or "")}
    author = Actor("claude_code" if res.agent in ("claude", "mock") else "codex", f"aar.improve.generate（起動: {actor}）")
    return register_patch(conn, candidate_id, author, generation) | {"ok": True}


# ---------------------------------------------------------------------- 候補の評価と判定

def _lineage(conn: psycopg.Connection, candidate_id: str) -> list[str]:
    """同じ系統の候補（最初の祖先と、そのすべての子孫）。封印評価を見た回数は、系統で数える。"""
    root = candidate_id
    while True:
        row = fetch_one(conn, "SELECT parent_id FROM improvement_candidates WHERE id = %s", (root,))
        if not row or not row["parent_id"]:
            break
        root = row["parent_id"]
    rows = fetch_all(conn, """WITH RECURSIVE t AS (SELECT id FROM improvement_candidates WHERE id = %s
                              UNION ALL SELECT c.id FROM improvement_candidates c JOIN t ON c.parent_id = t.id) SELECT id FROM t""", (root,))
    return [r["id"] for r in rows]


def evaluate_candidate(conn: psycopg.Connection, candidate_id: str, plan_id: int, split: str, actor: Actor, *,
                       isolation: str | None = None) -> dict[str, Any]:
    """候補を、現行方式と同じ課題・同じ条件で評価する。候補のファイルは複製の Git から取り出し、評価する側には読み込まない。"""
    cand, plan_row = ledger.get_candidate(conn, candidate_id), ledger.get_plan(conn, plan_id)
    if cand["status"] not in ("generated", "evaluated", "on_hold"):
        raise ValueError(f"評価できる状態ではない（{cand['status']}: {cand['status_reason']}）")
    repo, target = candidate_dir(candidate_id) / "repo", TARGETS[cand["target"]]
    problems = check_patch(repo, cand["base_commit"], cand["candidate_commit"], target)     # 評価の直前にも確かめる
    if problems:
        ledger.set_candidate(conn, candidate_id, actor, "patch_invalid", status="invalid", status_reason="; ".join(problems))
        raise ValueError("候補の差分が許可された範囲に収まっていない: " + "; ".join(problems))
    source = _git(repo, "show", f"{cand['candidate_commit']}:{target.module_path}") + "\n"
    run = _run(conn, plan_row, candidate_id=candidate_id, variant="candidate", split=split, commit=cand["candidate_commit"], source=source,
               run_key=f"candidate:{candidate_id}:{plan_id}:{cand['patch_sha256'][:12]}:{split}", actor=actor, isolation=isolation)
    if cand["status"] == "generated":
        ledger.set_candidate(conn, candidate_id, actor, "evaluation_started", status="evaluated")
    return run


def decide_candidate(conn: psycopg.Connection, candidate_id: str, plan_id: int) -> dict[str, Any]:
    """採否を機械的に判定して記録する。基盤には何も反映しない。"""
    cand, plan_row = ledger.get_candidate(conn, candidate_id), ledger.get_plan(conn, plan_id)
    repo, target = candidate_dir(candidate_id) / "repo", TARGETS[cand["target"]]
    problems = (check_patch(repo, cand["base_commit"], cand["candidate_commit"], target) if cand["candidate_commit"] and repo.exists()
                else [cand["status_reason"] or "候補の差分がない"])
    runs: dict[str, dict[str, Any]] = {}
    for r in fetch_all(conn, """SELECT * FROM improvement_runs WHERE plan_id = %s AND split IN ('select', 'sealed')
                                AND (candidate_id = %s OR (candidate_id IS NULL AND commit_sha = %s)) ORDER BY id""",
                       (plan_id, candidate_id, cand["base_commit"])):
        runs.setdefault(f"{r['variant']}:{r['split']}", r)
    sealed = fetch_one(conn, """SELECT count(*) AS n FROM improvement_runs
                                WHERE variant = 'candidate' AND split = 'sealed' AND candidate_id = ANY(%s)""", (_lineage(conn, candidate_id),))
    decision = decide(plan_row["plan"], runs, sealed_evaluations=sealed["n"], patch_problems=problems,   # type: ignore[index]
                      evaluator_matches=evaluator_sha256(PROJECT_ROOT) == plan_row["evaluator_sha256"])
    row = ledger.record_decision(conn, candidate_id, plan_id, decision)
    rejected_under = ""
    if decision["verdict"] == "rejected":
        rejected_under = (f"評価計画 {plan_row['plan_sha256'][:12]}（対象 {cand['target']}、基盤 {cand['base_commit'][:12]}）の条件での不採用。"
                          "条件が変われば結論は変わり得る")
    ledger.set_candidate(conn, candidate_id, Actor("policy", "aar.improve.decide"), "status_set", status=decision["verdict"],
                         status_reason="; ".join(decision["reasons"]), rejected_under=rejected_under)
    return row


def cleanup_clone(candidate_id: str) -> None:
    """候補の複製を消す（差分は台帳に残っている）。"""
    shutil.rmtree(candidate_dir(candidate_id) / "repo", ignore_errors=True)
