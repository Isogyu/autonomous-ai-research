"""モジュール間でやり取りするデータの型。

Temporal のアクティビティの入出力にも使うので、すべて JSON にできる型だけで構成する。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ 論文


class PaperRecord(BaseModel):
    """情報源から取得した論文1件（正規化済み）。値はすべて外部資料の記載に基づく。"""

    source: Literal["arxiv", "openalex", "semantic_scholar"]
    source_id: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None          # バージョンなし
    arxiv_version: int | None = None
    openalex_id: str | None = None
    s2_id: str | None = None
    published_at: date | None = None
    source_updated_at: datetime | None = None
    peer_review_status: Literal["preprint", "peer_reviewed", "unknown"] = "unknown"
    venue: str | None = None
    categories: list[str] = Field(default_factory=list)
    url: str | None = None
    pdf_url: str | None = None
    license: str | None = None
    citation_count: int | None = None
    references: list[str] = Field(default_factory=list)   # 例 "arxiv:2401.00001" / "doi:10.x/y" / "openalex:W1"
    retrieved_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ 価値評価と企画

CRITERIA = [
    "novelty", "credibility", "delta_vs_prior", "practical_importance", "verifiability",
    "local_feasibility", "cost", "interpretability", "reproducibility", "outreach_value",
]


class CriterionScore(BaseModel):
    score: int = Field(ge=0, le=5)
    rationale: str


class PaperScore(BaseModel):
    paper_id: str
    criteria: dict[str, CriterionScore]
    total: float                          # 0〜100
    scorer: str
    capped_by: str | None = None          # 上限規則が効いた場合、その理由


class Proposal(BaseModel):
    """検証企画。要件の16項目をそのまま持つ。"""

    title: str
    source_paper: str                     # 原論文（題名と ID）
    published_at: str
    novelty: str
    known: str                            # 何が分かっているか
    unknown: str                          # 何が未解決か
    hypotheses: list[str]
    method: str
    datasets: list[str]
    est_runtime_hours: float
    est_cost_usd: float
    compute: str
    expected_outcomes: str
    value_if_negative: str                # 期待した結果が出なかった場合に残る価値
    implications: str                     # 企業エンジニアへの示唆
    story: str                            # 情報発信のストーリー
    risks: list[str]
    based_on: Literal["abstract_only", "fulltext"] = "abstract_only"


# ------------------------------------------------------------------ エージェント実行


class AgentResult(BaseModel):
    ok: bool
    agent: str                            # claude / codex / mock
    model: str = ""
    text: str = ""                        # 最終応答
    structured: dict[str, Any] | None = None
    cost_usd: float = 0.0
    cost_is_estimate: bool = True
    billing: Literal["metered", "subscription_equivalent", "free"] = "subscription_equivalent"
    usage: dict[str, Any] = Field(default_factory=dict)
    duration_s: float = 0.0
    session_id: str | None = None
    error: str | None = None
    error_kind: Literal["auth", "rate_limit", "timeout", "budget", "other"] | None = None


# ------------------------------------------------------------------ 監査

ClaimStatus = Literal["Verified", "Supported", "Inconclusive", "Contradicted", "Not Tested"]
Severity = Literal["Critical", "Major", "Minor", "Improvement"]


class AuditClaim(BaseModel):
    claim_id: str
    claim_text: str
    is_primary: bool = True
    status: ClaimStatus
    recalculation_level: str = ""
    limitations: str = ""


class AuditFinding(BaseModel):
    issue_id: str
    severity: Severity
    certainty: Literal["Confirmed", "Suspected"] = "Confirmed"
    category: str = ""
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RecomputedMetric(BaseModel):
    claim_id: str = ""
    metric: str
    condition: str = ""
    original_value: float | None = None
    independent_value: float | None = None
    difference: float | None = None
    tolerance: float | None = None
    interpretation: str = ""


class AuditReport(BaseModel):
    """監査 Skill の実行結果を構造化したもの。品質ゲートはこれだけを見て判定する。"""

    kind: Literal["research", "publication_quality"]
    status: Literal["completed", "partial", "failed"]
    auditor: str
    skill: str
    research_verdict: str = ""
    publication_verdict: str = ""        # Publishable / Publishable with Revisions / Not Yet Publishable
    score: float | None = None            # 公開品質監査の点数
    claims: list[AuditClaim] = Field(default_factory=list)
    findings: list[AuditFinding] = Field(default_factory=list)
    recomputed: list[RecomputedMetric] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)   # 機械的な確認（テスト合格、秘密情報なし等）
    summary: str = ""
    report_dir: str = ""
    execution_mode: Literal["real", "mock"] = "real"
    cost_usd: float = 0.0


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class GateResult(BaseModel):
    gate: Literal["research", "publication"]
    passed: bool
    checks: list[GateCheck]
    policy_sha256: str

    def blocking(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]
