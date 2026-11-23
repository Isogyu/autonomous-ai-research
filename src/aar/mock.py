"""モック実行用の台本。外部 API とエージェント CLI を一切呼ばずに、パイプライン全体を通すために使う。

モックで動かした研究・企画は DB 上で execution_mode='mock' と記録され、実際の成功とは区別される。
台本は「1回目の分析コードに誤りがあり、独立監査がそれを再計算で見つけ、修正と再監査で解消する」流れにしてある
（修正・再監査のループを実際に通すため）。モックの監査担当は、生データから平均の差を本当に再計算して照合する。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from aar.agents.runner import MockRunner
from aar.models import AgentResult, PaperRecord

RUN_PY = '''"""合成データによる2条件の比較（モック用の小さな実験）。"""
import argparse, hashlib, json, random, time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--stage", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
prereg = sorted(Path("prereg").glob("cycle*.json"))[-1]
design = json.loads(prereg.read_text(encoding="utf-8"))
n = design["sample_size"][a.stage]
rng = random.Random(design["seeds"][0] if a.stage == "main" else 1)
t0 = time.time()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
with (out / "raw.jsonl").open("w", encoding="utf-8") as f:
    for i in range(n):
        for cond, mu in (("baseline", 0.50), ("treatment", 0.56)):
            f.write(json.dumps({"trial": i, "condition": cond, "score": rng.gauss(mu, 0.1)}) + "\\n")
(out / "run_meta.json").write_text(json.dumps({
    "stage": a.stage, "n_per_condition": n, "seed": design["seeds"][0],
    "prereg_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(), "duration_s": time.time() - t0}))
'''

# 誤りのある版: 処置群の前半だけで平均を出している
ANALYZE_BUGGY = '''"""分析（事前登録の判定基準を機械的に当てはめる）。"""
import argparse, json, math
from pathlib import Path

def load(d):
    rows = [json.loads(line) for line in (Path(d) / "raw.jsonl").read_text().splitlines() if line.strip()]
    return ([r["score"] for r in rows if r["condition"] == "baseline"],
            [r["score"] for r in rows if r["condition"] == "treatment"])

def mean(x): return sum(x) / len(x)
def var(x): m = mean(x); return sum((v - m) ** 2 for v in x) / (len(x) - 1)

def diff_ci(b, t):
    d = mean(t) - mean(b)
    se = math.sqrt(var(b) / len(b) + var(t) / len(t))
    return d, d - 1.96 * se, d + 1.96 * se

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--in", dest="src"); p.add_argument("--out")
    a = p.parse_args()
    b, t = load(a.src)
    t = t[: len(t) // 2]
    d, lo, hi = diff_ci(b, t)
    sd = math.sqrt((var(b) + var(t)) / 2)
    verdict = "supported" if lo > 0 else ("not_supported" if hi < 0 else "inconclusive")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "analysis.json").write_text(json.dumps({"results": [
        {"hypothesis": "H1", "metric": "平均の差（処置−基準）", "condition": "treatment vs baseline", "value": d,
         "ci_low": lo, "ci_high": hi, "effect_size": d / sd, "p_value": None, "n": len(b) + len(t),
         "analysis_kind": "confirmatory", "verdict": verdict, "test": "正規近似による95%区間"},
        {"hypothesis": None, "metric": "基準条件の平均", "condition": "baseline", "value": mean(b), "ci_low": None,
         "ci_high": None, "effect_size": None, "p_value": None, "n": len(b), "analysis_kind": "exploratory",
         "verdict": None, "test": "記述統計"}],
        "summary": "合成データでの2条件比較"}, ensure_ascii=False, indent=2))
'''
ANALYZE_FIXED = ANALYZE_BUGGY.replace("    t = t[: len(t) // 2]\n", "")

TEST_PY = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from analyze import diff_ci, mean, var

def test_mean_var():
    assert mean([1, 2, 3]) == 2
    assert var([1, 2, 3]) == 1

def test_diff_ci_contains_estimate():
    d, lo, hi = diff_ci([0.0, 1.0, 2.0], [1.0, 2.0, 3.0])
    assert abs(d - 1.0) < 1e-12 and lo < d < hi
'''

SECTIONS = ["実務上の問題提起", "最新研究の紹介", "従来技術との差分", "検証する問い", "独自実験", "実験結果",
            "考察", "実務への示唆", "研究上の限界", "今後の課題"]


def _files(cwd: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _claude(prompt: str, cwd: Path, schema: dict[str, Any] | None) -> dict[str, Any] | AgentResult:
    props = set((schema or {}).get("properties", {}))
    if "research_problem" in props:
        return {"summary": "（モック）要旨の要約", "research_problem": "処置が基準より良いかを小さな実験で確かめる",
                "claims_in_paper": ["（モック）論文の主張"], "limitations_of_prior_work": ["（モック）限界"],
                "open_questions": ["合成データ以外でも成り立つか"], "related_work": [], "basis": "abstract_only"}
    if "research_question" in props:
        return {"research_question": "処置条件は基準条件より平均が高いか",
                "hypotheses": [{"label": "H1", "statement": "処置条件の平均は基準条件より高い",
                                "decision_rule": "平均の差の95%区間の下限が 0 を超えれば支持、上限が 0 未満なら不支持、それ以外は判定保留"}],
                "primary_metric": "平均の差（処置−基準）", "secondary_metrics": [], "baselines": ["baseline"],
                "conditions": ["baseline", "treatment"], "datasets": [{"name": "合成データ", "source": "コードで生成",
                                                                        "license": "なし", "note": "正規乱数"}],
                "data_split": "分割なし（全試行を分析）", "sample_size": {"pilot": 20, "main": 400, "rationale": "モック"},
                "statistical_analysis": "正規近似による平均の差の95%区間", "success_failure_criteria": "判定基準のとおり",
                "exploratory_plan": "条件ごとの記述統計", "seeds": [20261123], "est_runtime_minutes": 1,
                "threats_to_validity": ["合成データであり実データではない"], "value_if_hypotheses_fail": "差がないことが分かる"}
    if "deviations_from_prereg" in props:
        _files(cwd, {"experiments/run.py": RUN_PY, "experiments/analyze.py": ANALYZE_BUGGY, "tests/test_analyze.py": TEST_PY,
                     "requirements.txt": ""})
        return {"files_written": ["experiments/run.py", "experiments/analyze.py", "tests/test_analyze.py"], "notes": "モック",
                "deviations_from_prereg": []}
    if "hypothesis_reviews" in props:
        return {"hypothesis_reviews": [{"label": "H1", "status": "supported", "reason": "区間の下限が 0 を超える"}],
                "observations": ["処置条件の平均が高い"], "exploratory_findings": ["基準条件の平均は設定どおり"],
                "limitations": ["合成データ"], "alternative_explanations": ["乱数の偶然"],
                "lessons": [{"category": "design", "content": "（モック）2条件比較は区間推定で判定すると解釈しやすい"}],
                "next": {"continue": False, "expected_information_gain": 0.1, "rationale": "追加で分かることは少ない",
                         "next_hypotheses": [], "next_design_changes": ""}}
    if "not_fixed" in props:
        if (cwd / "experiments/analyze.py").exists() and "len(t) // 2" in (cwd / "experiments/analyze.py").read_text():
            _files(cwd, {"experiments/analyze.py": ANALYZE_FIXED})
            return {"fixed": [{"issue_id": "I001", "what_changed": "処置群の全試行で平均を計算するよう修正"}], "not_fixed": [],
                    "needs_rerun": True}
        return {"fixed": [], "not_fixed": [], "needs_rerun": False}
    if "files_written" in props:   # 成果物
        body = "\n\n".join(f"## {s}\n\n（モック）{s}の内容。" + ("\n\n<!-- AAR:RESULTS -->\n\n<!-- AAR:FIGURES -->" if s == "実験結果" else "")
                           for s in SECTIONS)
        _files(cwd, {
            "report/report.md": f"# （モック）2条件比較の検証レポート\n\nこれはモック実行の成果物であり、実際の研究結果ではない。\n\n{body}\n",
            "README.md": "# （モック）2条件比較\n\nモック実行の成果物。\n\n## 再現方法\n\n```\npython -m pytest -q tests\n"
                         "python experiments/run.py --stage main --out results/cycle1/main\n"
                         "python experiments/analyze.py --in results/cycle1/main --out results/cycle1/analysis\n```\n\n"
                         "## 研究上の限界\n\n合成データだけで確かめている。\n\n## ライセンス\n\nMIT\n",
            "report/talk_script.md": "# 発表原稿（モック）\n\n## はじめに\n\nモックです。\n",
            "report/future_work.md": "# 今後の課題（モック）\n\n- 実データでの確認\n",
            "docs/limitations.md": "# 研究上の限界\n\n- 合成データであり、実データでの検証ではない。\n",
            "docs/references.md": "# 参考文献\n\n- （モック）文献なし\n"})
        return {"files_written": ["report/report.md", "README.md"], "notes": "モック"}
    if "novelty" in props and "credibility" in props:
        return {c: {"score": 4, "rationale": "（モック）"} for c in schema["properties"]}  # type: ignore[index]
    if "feasible" in props:
        return {"feasible": True, "infeasible_reason": "", "title": "（モック）2条件比較を区間推定で確かめる",
                "novelty": "（モック）", "known": "（モック）要旨に書かれていること", "unknown": "（モック）未解決の点",
                "hypotheses": ["処置条件の平均は基準条件より高い"], "method": "合成データでの2条件比較と区間推定",
                "datasets": ["合成データ"], "est_runtime_hours": 0.1, "est_cost_usd": 0.0, "compute": "CPU のみ",
                "expected_outcomes": "平均の差と区間", "value_if_negative": "差がないことが分かる",
                "implications": "（モック）", "story": "（モック）", "risks": ["合成データである"]}
    return AgentResult(ok=False, agent="claude", model="mock", error="モックの台本にない呼び出し", error_kind="other")


def _recompute(target: Path) -> tuple[float | None, float | None]:
    """生データから平均の差を独立に再計算し、対象の分析結果と並べて返す。"""
    raws = sorted(target.glob("results/cycle*/main/raw.jsonl"))
    analyses = sorted(target.glob("results/cycle*/analysis/analysis.json"))
    if not raws or not analyses:
        return None, None
    rows = [json.loads(line) for line in raws[-1].read_text().splitlines() if line.strip()]
    b = [r["score"] for r in rows if r["condition"] == "baseline"]
    t = [r["score"] for r in rows if r["condition"] == "treatment"]
    independent = sum(t) / len(t) - sum(b) / len(b)
    original = next((r["value"] for r in json.loads(analyses[-1].read_text())["results"]
                     if r.get("analysis_kind") == "confirmatory"), None)
    return original, independent


def _codex(prompt: str, cwd: Path, schema: dict[str, Any] | None) -> dict[str, Any] | AgentResult:
    props = set((schema or {}).get("properties", {}))
    m = re.search(r"複製\S*\s*`([^`]+)`|監査対象（読み取り専用の複製）: `([^`]+)`", prompt)
    target = Path(next(g for g in m.groups() if g)) if m else cwd
    if "audit_status" in props:
        original, independent = _recompute(target)
        match = original is not None and independent is not None and abs(original - independent) < 1e-9
        with (cwd / "claims.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["claim_id", "claim_text", "source_locator", "scope", "primary_or_secondary", "status",
                        "recalculation_level", "evidence_ids", "limitations", "next_check"])
            w.writerow(["C001", "処置条件の平均は基準条件より高い（H1）", "results/cycle1/analysis/analysis.json", "合成データ",
                        "primary", "Verified" if match else "Contradicted", "生データからの再計算", "E001", "合成データのみ", ""])
        with (cwd / "issues.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["issue_id", "severity", "certainty", "title", "source_locator", "evidence_ids", "claim_ids", "expected",
                        "observed", "reproduce_steps", "impact", "proposed_fix", "recheck", "priority", "effort_estimate",
                        "additional_api_cost", "currency", "status"])
            if not match:
                w.writerow(["I001", "Major", "Confirmed", "分析コードが処置群の一部の試行だけで平均を計算している",
                            "experiments/analyze.py", "E001", "C001", f"{independent}", f"{original}",
                            "raw.jsonl から平均の差を再計算する", "主要な数値が生データと一致しない",
                            "全試行で計算する", "再計算値との一致", "1", "小", "0", "USD", "open"])
        with (cwd / "recomputed_metrics.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["claim_id", "metric", "condition", "original_value", "independent_value", "difference", "tolerance",
                        "tolerance_basis", "n", "unit", "recalculation_level", "evidence_ids", "interpretation"])
            if original is not None and independent is not None:
                w.writerow(["C001", "平均の差", "treatment vs baseline", original, independent, original - independent, 1e-9,
                            "浮動小数の誤差", "", "", "生データからの再計算", "E001", "一致" if match else "不一致"])
        (cwd / "executive_summary.md").write_text("# 監査の要約（モック）\n\n生データから平均の差を再計算して照合した。\n", encoding="utf-8")
        return {"audit_status": "completed", "research_verdict": "モックの監査（平均の差だけを再計算）",
                "publication_verdict": "Publishable" if match else "Not Yet Publishable",
                "independent_recalculation_done": independent is not None, "data_leak_found": False,
                "summary": "（モック）生データからの再計算は" + ("分析結果と一致した。" if match else "分析結果と一致しなかった。"),
                "not_tested": ["統計手法の妥当性（モックでは確かめていない）"]}
    if "scores" in props:
        return {"scores": {"research_validity": 22, "reproducibility": 18, "code_quality": 13, "security": 14,
                           "documentation": 9, "credibility": 8, "outreach": 4},
                "issues": [], "claims_grounded": True, "measured_vs_assumed_distinguished": True,
                "summary": "（モック）公開品質の確認", "not_checked": ["実際の内容（モック）"]}
    return AgentResult(ok=False, agent="codex", model="mock", error="モックの台本にない呼び出し", error_kind="other")


def mock_runners() -> dict[str, MockRunner]:
    return {"claude": MockRunner("claude", _claude), "codex": MockRunner("codex", _codex)}


def mock_papers() -> list[PaperRecord]:
    now = dt.datetime.now(dt.UTC)
    return [PaperRecord(
        source="arxiv", source_id=f"0000.0000{i}v1", arxiv_id=f"0000.0000{i}", arxiv_version=1,
        title=f"(Mock) A lightweight evaluation benchmark for coding agents, part {i}",
        abstract="(Mock paper for pipeline tests.) We release an open-source benchmark and evaluation harness for agent "
                 "reliability with ablation analysis. Code is available.",
        authors=["Mock Author"], published_at=now.date(), source_updated_at=now, peer_review_status="preprint",
        categories=["cs.AI"], url=f"https://example.invalid/abs/0000.0000{i}", retrieved_at=now) for i in (1, 2)]
