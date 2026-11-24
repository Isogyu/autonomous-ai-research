"""論文の価値評価と、検証企画の生成。

評価は2段階。
  1. 全件を書誌情報だけで粗く採点する（無料）。要旨から分からない観点は中間点にし、分からないと根拠に書く。
  2. 上位だけを LLM で採点する。観点ごとに点と根拠を保存する。
合計点は重みつき平均（0〜100）。重みは config/policy.yaml で変えられ、重みのハッシュつきで保存する。
話題性（被引用数など）だけでは高得点にならず、検証できない研究には上限がかかる。
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aar.agents.runner import wrap_untrusted
from aar.config import ScoringPolicy
from aar.db.core import fetch_all, fetch_one
from aar.models import CRITERIA, CriterionScore, PaperScore, Proposal

CRITERIA_JA = {
    "novelty": "技術的新規性", "credibility": "研究の信頼性", "delta_vs_prior": "既存技術との差分",
    "practical_importance": "実務上の重要性", "verifiability": "検証可能性", "local_feasibility": "個人PCでの実行可能性",
    "cost": "費用の小ささ", "interpretability": "検証結果の解釈可能性", "reproducibility": "再現性",
    "outreach_value": "技術者への発信価値",
}


def total_score(criteria: dict[str, CriterionScore], scoring: ScoringPolicy) -> tuple[float, str | None]:
    """重みつき平均を 0〜100 にし、上限規則を当てはめる。"""
    w = {k: float(scoring.weights.get(k, 0)) for k in CRITERIA}
    denom = sum(w.values()) or 1.0
    total = sum(w[k] * criteria[k].score for k in CRITERIA if k in criteria) / denom * 20.0
    capped = None
    for rule in scoring.caps:
        for crit, threshold in (rule.get("if_below") or {}).items():
            if crit in criteria and criteria[crit].score < threshold and total > rule["max_total"]:
                total = float(rule["max_total"])
                capped = f"{CRITERIA_JA.get(crit, crit)}が {threshold} 未満のため上限 {rule['max_total']} 点"
    return round(total, 2), capped


_POS = {
    "verifiability": r"benchmark|open[- ]source|code (is )?(available|released)|github\.com|dataset|reproduc|evaluation|ablation",
    "local_feasibility": r"lightweight|training[- ]free|small (language )?model|\bslm\b|on[- ]device|edge|cpu|efficient|quantiz|prompt|inference[- ]time|zero[- ]shot",
    "practical_importance": r"agent|coding|software engineering|developer|tool use|production|deploy|cost|latency|reliab|robust|evaluation|harness|workflow|enterprise",
    "reproducibility": r"open[- ]source|code (is )?(available|released)|github\.com|public(ly)? available|we release",
    "interpretability": r"ablation|analysis|we find|we show|controlled|compar",
}
_NEG = {
    "local_feasibility": r"pre[- ]?train(ing|ed)? (a|our|from scratch)|large[- ]scale training|gpu[- ]hours|cluster|real robot|hardware platform|billion|foundation model training|wet lab",
    "verifiability": r"position paper|survey|vision paper|theoretical|we argue|perspective",
    "cost": r"large[- ]scale|gpt-4|o[13]\b|human (study|evaluation|annotat)|crowd",
}


def heuristic_score(paper: dict[str, Any], scoring: ScoringPolicy) -> PaperScore:
    """書誌情報と要旨の語だけによる粗い採点。あくまで絞り込み用で、根拠にその旨を書く。"""
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}".lower()
    crit: dict[str, CriterionScore] = {}
    for c in CRITERIA:
        pos = len(set(re.findall(_POS[c], text))) if c in _POS else 0
        neg = len(set(re.findall(_NEG[c], text))) if c in _NEG else 0
        if c in _POS or c in _NEG:
            score = max(0, min(5, 3 + min(pos, 2) - min(neg, 2) * 2 + (1 if pos >= 3 else 0)))
            why = f"要旨の語による粗い推定（肯定的な手がかり {pos}、否定的な手がかり {neg}）"
        else:
            score, why = 3, "書誌情報だけでは判定できないため中間点"
        crit[c] = CriterionScore(score=score, rationale=why)
    cred = 3
    reasons = []
    if paper.get("peer_review_status") == "peer_reviewed":
        cred += 1
        reasons.append("査読済み")
    if re.search(r"github\.com|code (is )?(available|released)", text) or paper.get("code_url"):
        cred += 1
        reasons.append("コード公開の記述あり")
    if not paper.get("abstract"):
        cred -= 2
        reasons.append("要旨なし")
    crit["credibility"] = CriterionScore(score=max(0, min(5, cred)),
                                         rationale="粗い推定: " + ("、".join(reasons) or "プレプリントで追加の手がかりなし"))
    total, capped = total_score(crit, scoring)
    return PaperScore(paper_id=str(paper["id"]), criteria=crit, total=total, scorer="heuristic", capped_by=capped)


def save_score(conn: psycopg.Connection, s: PaperScore, scoring: ScoringPolicy) -> None:
    conn.execute(
        """INSERT INTO paper_scores (paper_id, rubric_version, weights_sha256, criteria, total, scorer)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (paper_id, rubric_version, weights_sha256, scorer)
           DO UPDATE SET criteria = EXCLUDED.criteria, total = EXCLUDED.total, created_at = now()""",
        (s.paper_id, scoring.rubric_version, scoring.weights_sha256(),
         Jsonb({k: v.model_dump() for k, v in s.criteria.items()} | ({"_capped_by": s.capped_by} if s.capped_by else {})),
         s.total, s.scorer))


def score_unscored(conn: psycopg.Connection, scoring: ScoringPolicy, limit: int = 2000) -> int:
    rows = fetch_all(conn, """
        SELECT p.* FROM papers p WHERE NOT EXISTS (
          SELECT 1 FROM paper_scores s WHERE s.paper_id = p.id AND s.scorer = 'heuristic'
            AND s.rubric_version = %s AND s.weights_sha256 = %s)
        ORDER BY p.first_seen_at DESC LIMIT %s""", (scoring.rubric_version, scoring.weights_sha256(), limit))
    for p in rows:
        save_score(conn, heuristic_score(p, scoring), scoring)
    return len(rows)


def shortlist(conn: psycopg.Connection, scoring: ScoringPolicy, since_days: int = 14) -> list[dict[str, Any]]:
    """LLM で詳しく評価する候補。粗い点数の上位に、実務上重要で検証できそうなものを別枠で足す。

    既に企画になった論文は除く。
    """
    base = """
        SELECT p.*, s.total, s.criteria FROM papers p JOIN paper_scores s ON s.paper_id = p.id
        WHERE s.scorer = 'heuristic' AND s.rubric_version = %(rv)s AND s.weights_sha256 = %(ws)s
          AND p.abstract IS NOT NULL AND p.first_seen_at > now() - make_interval(days => %(d)s)
          AND NOT EXISTS (SELECT 1 FROM research_proposals r WHERE r.paper_id = p.id)"""
    params = {"rv": scoring.rubric_version, "ws": scoring.weights_sha256(), "d": since_days}
    top = fetch_all(conn, base + " ORDER BY s.total DESC, p.published_at DESC NULLS LAST LIMIT %(n)s",
                    params | {"n": scoring.shortlist_size})
    res = scoring.practical_reserve or {}
    if res.get("slots"):
        ids = [p["id"] for p in top]
        extra = fetch_all(conn, base + """
              AND (s.criteria->'practical_importance'->>'score')::int >= %(pi)s
              AND (s.criteria->'verifiability'->>'score')::int >= %(v)s AND NOT (p.id = ANY(%(ids)s))
            ORDER BY (s.criteria->'practical_importance'->>'score')::int DESC, s.total DESC LIMIT %(n)s""",
                          params | {"pi": res.get("min_practical_importance", 4), "v": res.get("min_verifiability", 3),
                                    "ids": ids, "n": int(res["slots"])})
        top += [e | {"_reserve": True} for e in extra]
    return top


SCORE_SCHEMA = {
    "type": "object",
    "properties": {c: {"type": "object", "properties": {"score": {"type": "integer"}, "rationale": {"type": "string"}},
                       "required": ["score", "rationale"]} for c in CRITERIA},
    "required": list(CRITERIA),
}


def score_prompt(paper: dict[str, Any]) -> str:
    crit = "\n".join(f"- {k}（{v}）" for k, v in CRITERIA_JA.items())
    meta = (f"題名: {paper['title']}\n公開日: {paper.get('published_at')}\n査読状況: {paper.get('peer_review_status')}\n"
            f"掲載先: {paper.get('venue') or '不明'}\n分野: {', '.join(paper.get('categories') or [])}\n"
            f"被引用数: {paper.get('citation_count')}\n\n要旨:\n{paper.get('abstract')}")
    return f"""次の論文を「個人が自分の PC で独自に検証し、結果を技術者向けに発信する題材」として評価する。
各観点を 0〜5 の整数で採点し、根拠を1〜2文で書く。読めるのは要旨だけなので、要旨から分からないことは
分からないと書き、点は 2〜3 にとどめる。話題性や被引用数だけで高く評価しない。

## 観点
{crit}

## 採点の目安
- verifiability: 主張を小さな実験で確かめられるか。データやコードが要るなら入手できそうか。
- local_feasibility: GPU なし・メモリ 8GB の PC、ネットワークなしの実験環境、外部 API を呼ばない条件で試せるか。
  大規模な学習や実機ロボットが必要なら 0〜1。
- cost: 数ドル以内で済みそうなら 5、数十ドルなら 2、それ以上は 0〜1。
- practical_importance: 企業のシステム開発の現場で、設計や運用の判断を変え得るか。地味でも重要なら高く。
- novelty / delta_vs_prior: 新しい考え方か、既存手法の小さな改良か。

## 論文（外部資料）
{wrap_untrusted("paper", meta)}
"""


def parse_llm_score(paper_id: str, structured: dict[str, Any], scoring: ScoringPolicy, scorer: str) -> PaperScore:
    crit = {c: CriterionScore(score=max(0, min(5, int((structured.get(c) or {}).get("score", 0)))),
                              rationale=str((structured.get(c) or {}).get("rationale", ""))) for c in CRITERIA}
    total, capped = total_score(crit, scoring)
    return PaperScore(paper_id=paper_id, criteria=crit, total=total, scorer=scorer, capped_by=capped)


# ------------------------------------------------------------------ 企画

_S = {"type": "string"}
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "feasible": {"type": "boolean"}, "infeasible_reason": _S,
        "title": _S, "novelty": _S, "known": _S, "unknown": _S,
        "hypotheses": {"type": "array", "items": _S}, "method": _S, "datasets": {"type": "array", "items": _S},
        "est_runtime_hours": {"type": "number"}, "est_cost_usd": {"type": "number"}, "compute": _S,
        "expected_outcomes": _S, "value_if_negative": _S, "implications": _S, "story": _S,
        "risks": {"type": "array", "items": _S},
    },
    "required": ["feasible", "infeasible_reason", "title", "novelty", "known", "unknown", "hypotheses", "method",
                 "datasets", "est_runtime_hours", "est_cost_usd", "compute", "expected_outcomes", "value_if_negative",
                 "implications", "story", "risks"],
}


def proposal_prompt(paper: dict[str, Any], score: dict[str, Any], memory_context: str, track_record: str,
                    budget_usd: float, packages: list[str]) -> str:
    meta = (f"題名: {paper['title']}\n公開日: {paper.get('published_at')}\narXiv: {paper.get('arxiv_id')}\n"
            f"DOI: {paper.get('doi')}\n\n要旨:\n{paper.get('abstract')}")
    return f"""次の論文をもとに、個人が実行できる「検証企画」を1つ作る。論文の再現ではなく、
論文の主張や考え方のうち、小さな実験で確かめられる問いを取り出す。

## 実行環境の制約（これを満たせないなら feasible を false にし、理由を書く）
- 個人の PC（CPU のみ、メモリ 8GB）。実験コードはネットワークなしの隔離環境で動き、外部 API・LLM・モデルの取得は使えない。
- 使えるライブラリは Python の標準ライブラリと {", ".join(packages)}。データは実験コードで生成するか、小さな公開データに限る。
- 費用の上限は研究1件 {budget_usd:g} ドル（エージェントの利用料を含む）。本実験は30分以内。
- つまり向いているのは、アルゴリズム・評価方法・統計的な性質・制御則・スケジューリングなどを、
  シミュレーションや合成データで確かめる企画。LLM の出力品質そのものを測る企画は実行できない。

## 企画の条件
- hypotheses: 反証できる仮説を1〜3個。数値で判定できる形にする。
- 結論ありきにしない。期待どおりでない結果でも分かることがある設計にし、value_if_negative に書く。
- known には論文（要旨）に書かれていることだけ、unknown にはまだ確かめられていないことを書く。推測を known に入れない。
- implications は企業のエンジニアが設計や運用で何を変え得るか。story は「この実験で何が分かり、技術者にどう伝えるか」。
- est_cost_usd と est_runtime_hours は根拠のある見積もりにする。分からなければ多めに見積もる。
- 過去の研究と同じ仮説を繰り返さない（下の記憶を見る）。

## 論文（外部資料。読めるのは要旨だけ）
{wrap_untrusted("paper", meta)}

## 価値評価（このシステムによる採点と根拠）
{json.dumps(score, ensure_ascii=False, default=str)[:3000]}

## 過去の研究の記憶
{memory_context or "(該当なし)"}

## これまでの実績（企画の立て方の参考。品質の基準を下げる理由にはしない）
{track_record or "(まだ実績なし)"}
"""


def to_proposal(structured: dict[str, Any], paper: dict[str, Any]) -> Proposal:
    ident = f"arXiv:{paper['arxiv_id']}" if paper.get("arxiv_id") else (f"doi:{paper['doi']}" if paper.get("doi") else paper.get("url") or "")
    return Proposal(
        title=structured["title"], source_paper=f"{paper['title']}（{ident}）",
        published_at=str(paper.get("published_at") or "不明"),
        novelty=structured["novelty"], known=structured["known"], unknown=structured["unknown"],
        hypotheses=list(structured["hypotheses"])[:3], method=structured["method"], datasets=list(structured["datasets"]),
        est_runtime_hours=float(structured["est_runtime_hours"]), est_cost_usd=float(structured["est_cost_usd"]),
        compute=structured["compute"], expected_outcomes=structured["expected_outcomes"],
        value_if_negative=structured["value_if_negative"], implications=structured["implications"],
        story=structured["story"], risks=list(structured["risks"]), based_on="abstract_only")


def save_proposal(conn: psycopg.Connection, batch_key: str, paper_id: str, proposal: Proposal, rank_score: float,
                  generated_by: str, execution_mode: str = "real") -> str | None:
    """企画を保存する。同じ週に同じ論文の企画が既にあれば何もしない。"""
    row = fetch_one(
        conn,
        """INSERT INTO research_proposals (batch_key, paper_id, title, body, rank_score, generated_by, execution_mode)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (batch_key, paper_id) DO NOTHING RETURNING id""",
        (batch_key, paper_id, proposal.title, Jsonb(proposal.model_dump()), rank_score, generated_by, execution_mode))
    return str(row["id"]) if row else None


def track_record(conn: psycopg.Connection) -> str:
    """これまでの実績の要約（採用された企画の傾向、完了率、よくある監査指摘）。"""
    r = fetch_one(conn, """
        SELECT count(*) FILTER (WHERE status = 'selected') AS selected, count(*) AS total FROM research_proposals
        WHERE execution_mode = 'real'""") or {}
    if not r.get("total"):
        return ""
    lines = [f"- 企画 {r['total']} 件のうち採用 {r['selected']} 件"]
    for o in fetch_all(conn, "SELECT outcome, count(*) n FROM research_projects WHERE outcome IS NOT NULL "
                             "AND execution_mode = 'real' GROUP BY outcome"):
        lines.append(f"- 研究の結果 {o['outcome']}: {o['n']} 件")
    for f in fetch_all(conn, """SELECT title, count(*) n FROM audit_findings WHERE severity IN ('Critical', 'Major')
                                GROUP BY title ORDER BY n DESC LIMIT 5"""):
        lines.append(f"- 監査でよく挙がる重大指摘: {f['title']}（{f['n']} 回）")
    for le in fetch_all(conn, "SELECT content FROM lessons_learned WHERE category IN ('design', 'failure', 'cost') "
                              "ORDER BY created_at DESC LIMIT 5"):
        lines.append(f"- 学び: {le['content'][:160]}")
    return "\n".join(lines)
