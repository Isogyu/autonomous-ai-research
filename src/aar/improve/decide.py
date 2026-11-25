"""候補の採否の、機械的な判定。

基準は評価計画（候補を作る前に固定したもの）にあり、ここでは当てはめるだけ。モデルの判断は使わない。
結果は adopt_candidate（採用候補。基盤への反映は別に人が承認する）、rejected（棄却）、on_hold（保留）のどれか。
証拠の保全や出力の形式などの違反は、主指標の改善では相殺しない。効果を判定できないときは、改善したことにしない。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DEFAULT_PLAN: dict[str, Any] = {
    "target": "fix_context",
    "question": "修正担当へ渡す文脈に、指摘が参照する主張・証拠の定義が入っているか",
    "measures": "渡す文脈の完全さ（基盤の正しさ・研究過程の品質の一部）。修正の質、研究の質、科学的な成果は測らない",
    "primary_metric": "ref_resolution_micro",       # 参照のうち、定義が文脈に入っている割合
    "min_improvement": 0.30,                         # 実務上意味のある改善幅（封印評価で、現行方式との差）
    "min_level": 0.90,                               # 封印評価での水準
    "guards": ["ran", "valid_json", "issues_kept", "fields_kept", "within_budget", "deterministic"],
    "min_precision": 0.80,                           # 入れた主張・証拠のうち、指摘が参照しているものの割合
    "no_case_regression": True,                      # どの課題でも、参照の解決が現行方式を下回らない
    "max_mean_chars_ratio": 4.0,                     # 許容する後退（文脈の長さ。現行方式の平均に対する比）
    "budget_chars": 24000,
    "required_isolation": "podman",                 # 検証済みの Podman コンテナ。満たさない実行では、採否を保留にする
    "min_sealed_real_cases": 3,                      # 標本の下限。満たさなければ、指標が良くても保留
    "min_sealed_real_groups": 2,
    "max_sealed_evaluations": 1,                     # 封印評価を見てよい回数（候補の系統ごと）
    "splits": {"dev": [], "select": [], "sealed": []},   # グループの割り当て。凍結するときに埋める
    "synthetic_cases": {"select": 12, "sealed": 12},
    "limits": {"timeout_s": 120, "max_candidates_per_origin": 3, "max_generation_usd": 2.0, "parallel": 1},
    "stop_conditions": "候補の生成が上限（max_candidates_per_origin）に達したら、その発見元からの生成をやめる",
}


def plan_sha256(plan: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def decide(plan: dict[str, Any], runs: dict[str, dict[str, Any]], *, sealed_evaluations: int, patch_problems: list[str],
           evaluator_matches: bool) -> dict[str, Any]:
    """採否を決める。runs は {"baseline:select", "candidate:select", "baseline:sealed", "candidate:sealed"} → 実行の記録。"""
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def out(verdict: str) -> dict[str, Any]:
        return {"verdict": verdict, "checks": checks, "reasons": reasons}

    checks.append(_check("変更が許可された範囲に収まっている", not patch_problems, "; ".join(patch_problems) or "収まっている"))
    if patch_problems:
        reasons.append("許可された範囲の外、または保護領域への変更を含む")
        return out("rejected")
    checks.append(_check("評価器が計画の固定時から変わっていない", evaluator_matches, "一致" if evaluator_matches else "評価器のハッシュが計画と異なる"))
    if not evaluator_matches:
        reasons.append("評価器が変わっているので、基準値と比べられない（評価器の変更は、候補とは別の変更として扱う）")
        return out("on_hold")

    missing = [k for k in ("baseline:select", "candidate:select", "baseline:sealed", "candidate:sealed") if k not in runs]
    if missing:
        checks.append(_check("必要な実行がそろっている", False, "不足: " + "、".join(missing)))
        reasons.append("評価が終わっていない")
        return out("on_hold")
    iso = [k for k, r in runs.items() if r["isolation"] != plan["required_isolation"]]
    checks.append(_check("計画どおりの隔離で実行した", not iso, "不足: " + "、".join(iso) if iso else plan["required_isolation"]))
    if iso:
        reasons.append("隔離が計画より弱い実行があるので、採否を決めない")
        return out("on_hold")
    base_failed = [k for k in ("baseline:select", "baseline:sealed") if runs[k]["status"] != "completed"]
    if base_failed:
        checks.append(_check("現行方式の実行が完了している", False, "、".join(base_failed)))
        reasons.append("現行方式の基準値を測れていない")
        return out("on_hold")

    # --- 候補が動かない、出力の形式や証拠の保全に違反する → 棄却（主指標では相殺しない）
    for split in ("select", "sealed"):
        cand = runs[f"candidate:{split}"]
        ok = cand["status"] == "completed"
        checks.append(_check(f"候補の実行が完了している（{split}）", ok, cand.get("failure_kind") or "完了"))
        if not ok:
            reasons.append(f"候補が {split} の課題で実行できなかった（{cand.get('failure_kind')}）")
            return out("rejected")
        fails = {g: ids for g, ids in cand["metrics"].get("guard_failures", {}).items() if g in plan["guards"]}
        checks.append(_check(f"出力の形式と証拠の保全（{split}）", not fails,
                             "; ".join(f"{g}: {len(ids)} 件（{ids[0]} など）" for g, ids in fails.items()) or "違反なし"))
        if fails:
            reasons.append(f"{split} の課題で、形式または証拠の保全に違反した: " + "、".join(fails))
            return out("rejected")
        prec = cand["metrics"].get("min_precision")
        okp = prec is None or prec >= plan["min_precision"]
        checks.append(_check(f"参照されていない項目を入れすぎていない（{split}）", okp, f"最小 {prec}（基準 {plan['min_precision']} 以上）"))
        if not okp:
            reasons.append(f"{split} の課題で、指摘が参照していない主張・証拠を多く入れている")
            return out("rejected")
        base_by = {s["id"]: s for s in runs[f"baseline:{split}"]["per_case"]}
        worse = [s["id"] for s in cand["per_case"]
                 if s["ref_resolution"] is not None and base_by.get(s["id"], {}).get("ref_resolution") is not None
                 and s["ref_resolution"] < base_by[s["id"]]["ref_resolution"]]
        checks.append(_check(f"どの課題でも現行方式を下回らない（{split}）", not worse, "、".join(worse[:5]) or "後退なし"))
        if worse and plan["no_case_regression"]:
            reasons.append(f"{split} の課題に、現行方式より悪くなったものがある")
            return out("rejected")
        ratio = (cand["metrics"]["mean_chars"] / runs[f"baseline:{split}"]["metrics"]["mean_chars"]
                 if runs[f"baseline:{split}"]["metrics"]["mean_chars"] else 0.0)
        okr = ratio <= plan["max_mean_chars_ratio"]
        checks.append(_check(f"文脈の長さの増え方が許容の範囲（{split}）", okr, f"現行方式の {ratio:.2f} 倍（上限 {plan['max_mean_chars_ratio']}）"))
        if not okr:
            reasons.append(f"{split} の課題で、文脈が長くなりすぎる")
            return out("rejected")

    # --- 主指標（封印評価）
    metric = plan["primary_metric"]
    base, cand = runs["baseline:sealed"]["metrics"].get(metric), runs["candidate:sealed"]["metrics"].get(metric)
    if base is None or cand is None:
        checks.append(_check("主指標を計算できる", False, "封印評価に、参照を含む課題がない"))
        reasons.append("効果を判定できない（改善したことにはしない）")
        return out("on_hold")
    gain = cand - base
    ok_gain, ok_level = gain >= plan["min_improvement"], cand >= plan["min_level"]
    checks.append(_check("主指標の改善幅（封印評価）", ok_gain, f"{base:.3f} → {cand:.3f}（差 {gain:+.3f}、基準 +{plan['min_improvement']} 以上）"))
    checks.append(_check("主指標の水準（封印評価）", ok_level, f"{cand:.3f}（基準 {plan['min_level']} 以上）"))
    if not (ok_gain and ok_level):
        reasons.append("封印評価で、主指標が事前に決めた基準に届かない（改善は確認できていない）")
        return out("rejected")

    # --- ここから先は、指標は基準を満たしている。採用候補にしてよいだけの裏づけがあるか
    ok_looks = sealed_evaluations <= plan["max_sealed_evaluations"]
    checks.append(_check("封印評価を繰り返し見ていない", ok_looks, f"{sealed_evaluations} 回（上限 {plan['max_sealed_evaluations']}）"))
    m = runs["candidate:sealed"]["metrics"]
    ok_n = m["n_real_cases"] >= plan["min_sealed_real_cases"] and m["n_real_groups"] >= plan["min_sealed_real_groups"]
    checks.append(_check("封印評価の実物の標本が足りている", ok_n,
                         f"実物 {m['n_real_cases']} 件・{m['n_real_groups']} グループ"
                         f"（基準 {plan['min_sealed_real_cases']} 件・{plan['min_sealed_real_groups']} グループ以上）"))
    if not ok_looks:
        reasons.append("封印評価を上限より多く見ているので、未使用の最終評価とは言えない")
        return out("on_hold")
    if not ok_n:
        reasons.append("指標は基準を満たしたが、封印評価の実物の標本が足りない（改善は未確認として扱う）")
        return out("on_hold")
    reasons.append("事前に決めた基準をすべて満たした。基盤への反映には、人の承認とレビューが別に要る")
    return out("adopt_candidate")
