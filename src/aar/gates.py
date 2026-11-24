"""品質ゲート。監査の構造化結果と機械的な確認だけから合否を決める純粋な関数。

基準は config/policy.yaml にあり、研究の開始時にファイルのハッシュを研究に固定する。
判定のたびに現在のハッシュと照合し、違っていれば不合格にする（基準を後から緩めることを防ぐ）。
ここには「合格に近いから通す」ような裁量を入れない。
"""

from __future__ import annotations

from aar.config import GatePublication, GateResearch
from aar.models import AuditReport, GateCheck, GateResult


def _count(report: AuditReport, severity: str) -> int:
    return sum(1 for f in report.findings if f.severity == severity)


def policy_check(pinned_sha: str, current_sha: str) -> GateCheck:
    return GateCheck(name="品質ゲートの基準が研究開始時から変わっていない", passed=pinned_sha == current_sha,
                     detail="一致" if pinned_sha == current_sha else f"開始時 {pinned_sha[:12]} ≠ 現在 {current_sha[:12]}")


def evaluate_research_gate(
    gate: GateResearch, report: AuditReport, *, tests_passed: bool, prereg_intact: bool, prereg_detail: str,
    implementer: str, pinned_policy_sha: str, current_policy_sha: str,
) -> GateResult:
    checks: list[GateCheck] = [policy_check(pinned_policy_sha, current_policy_sha)]
    checks.append(GateCheck(name="独立監査が最後まで実行された", passed=report.status == "completed",
                            detail=f"監査の状態: {report.status}"))
    n_crit, n_major = _count(report, "Critical"), _count(report, "Major")
    checks.append(GateCheck(name="Critical の指摘が残っていない", passed=n_crit <= gate.max_open_critical, detail=f"{n_crit} 件"))
    checks.append(GateCheck(name="Major の指摘が残っていない", passed=n_major <= gate.max_open_major, detail=f"{n_major} 件"))
    primary = [c for c in report.claims if c.is_primary]
    bad = [c for c in primary if c.status not in gate.require_primary_claims_in]
    checks.append(GateCheck(
        name="主要な主張が独立に検証されている", passed=bool(primary) and not bad,
        detail="主要な主張がない" if not primary else
        ("すべて " + "/".join(gate.require_primary_claims_in) if not bad else
         "、".join(f"{c.claim_id}={c.status}" for c in bad))))
    forbidden = [c for c in report.claims if c.status in gate.forbid_claim_status]
    checks.append(GateCheck(name="証拠と矛盾する主張がない", passed=not forbidden,
                            detail="、".join(f"{c.claim_id}={c.status}" for c in forbidden) or "なし"))
    if gate.require_independent_recalculation:
        checks.append(GateCheck(name="主要な計算結果が生データから独立に再計算されている",
                                passed=report.checks.get("independent_recalculation", False),
                                detail=f"再計算した指標 {len(report.recomputed)} 件"))
    checks.append(GateCheck(name="データリークが確認されていない", passed=report.checks.get("no_data_leak", False)))
    checks.append(GateCheck(name="監査の台帳が決まった形式になっている", passed=report.checks.get("ledgers_well_formed", False)))
    if gate.require_tests_pass:
        checks.append(GateCheck(name="テストが合格している", passed=tests_passed))
    if gate.require_preregistration_intact:
        checks.append(GateCheck(name="事前登録が登録後に変更されていない", passed=prereg_intact, detail=prereg_detail))
    if gate.require_auditor_differs_from_implementer:
        differs = report.auditor.split(":")[0] != implementer.split(":")[0]
        checks.append(GateCheck(name="監査担当が研究実装担当と別である", passed=differs,
                                detail=f"監査 {report.auditor} / 実装 {implementer}"))
    return GateResult(gate="research", passed=all(c.passed for c in checks), checks=checks, policy_sha256=current_policy_sha)


def evaluate_publication_gate(
    gate: GatePublication, report: AuditReport, *, secrets_found: int, has_limitations: bool, has_repro: bool,
    has_license: bool, tests_passed: bool, claims_grounded: bool, pinned_policy_sha: str, current_policy_sha: str,
) -> GateResult:
    checks: list[GateCheck] = [policy_check(pinned_policy_sha, current_policy_sha)]
    checks.append(GateCheck(name="公開品質監査が最後まで実行された", passed=report.status == "completed",
                            detail=f"監査の状態: {report.status}（{report.skill}）"))
    if gate.require_formal_skill:
        # 利用者の Skill による監査で、結果が Skill の決まりに合っていること。代用の監査では通らない
        formal = bool(report.checks.get("formal_skill"))
        checks.append(GateCheck(name="正式な公開品質監査 Skill による監査で、結果が正常である", passed=formal,
                                detail="" if formal else "; ".join(report.detail.get("problems") or ["Skill による監査ではない"])[:300]))
        verdict = report.publication_verdict
        checks.append(GateCheck(name=f"Skill の公開判定が {'・'.join(gate.accept_verdicts)} のいずれか",
                                passed=formal and verdict in gate.accept_verdicts, detail=f"判定 {verdict or 'なし'}"))
        checks.append(GateCheck(name="Skill が公開保留としていない", passed=formal and not report.checks.get("publication_hold", True)))
    score = report.score if report.score is not None else -1
    checks.append(GateCheck(name=f"公開品質の点数が {gate.min_score:g} 点以上", passed=score >= gate.min_score,
                            detail=f"{score:g} 点" if score >= 0 else "点数なし"))
    n_crit, n_major = _count(report, "Critical"), _count(report, "Major")
    checks.append(GateCheck(name="Critical の指摘が残っていない", passed=n_crit <= gate.max_open_critical, detail=f"{n_crit} 件"))
    checks.append(GateCheck(name="Major の指摘が残っていない", passed=n_major <= gate.max_open_major, detail=f"{n_major} 件"))
    if gate.require_no_secrets:
        checks.append(GateCheck(name="秘密情報が含まれていない", passed=secrets_found == 0, detail=f"検出 {secrets_found} 件"))
    if gate.require_limitations_section:
        checks.append(GateCheck(name="研究の限界が明記されている", passed=has_limitations))
    if gate.require_reproduction_steps:
        checks.append(GateCheck(name="再現手順が整備されている", passed=has_repro))
    if gate.require_license:
        checks.append(GateCheck(name="ライセンスが明示されている", passed=has_license))
    checks.append(GateCheck(name="テストが合格している", passed=tests_passed))
    checks.append(GateCheck(name="発信の主張に根拠がある", passed=claims_grounded and report.checks.get("claims_grounded", False),
                            detail="報告書の数値が分析結果と一致" if claims_grounded else "報告書に分析結果にない数値がある"))
    return GateResult(gate="publication", passed=all(c.passed for c in checks), checks=checks, policy_sha256=current_policy_sha)
