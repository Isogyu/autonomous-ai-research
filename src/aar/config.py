"""設定の読み込み。

環境ごとの値（接続先など）は環境変数、利用者が事前に決める判断基準は config/policy.yaml に置く。
方針ファイルはハッシュを研究に固定し、実行中に書き換えられたら検出できるようにする。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AAR_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://aar:aar@127.0.0.1:55432/aar"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    task_queue: str = "aar"
    policy_path: Path = PROJECT_ROOT / "config" / "policy.yaml"
    data_dir: Path = PROJECT_ROOT / "var"
    workspaces_dir: Path = PROJECT_ROOT / "workspaces"
    # 監査 Skill の置き場所（skills/README.md を参照）
    research_audit_skill_dir: Path = PROJECT_ROOT / "skills" / "independent-research-audit"
    publication_audit_skill_dir: Path = PROJECT_ROOT / "skills" / "github-publication-quality-audit"
    ai_scientist_dir: Path = PROJECT_ROOT / "vendor" / "AI-Scientist-v3"
    # 論文 API の連絡先・キー（任意）
    contact_email: str = ""
    openalex_api_key: str = ""
    semantic_scholar_api_key: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # mock にすると外部 API とエージェント CLI を呼ばない（結果は mock として記録される）
    mode: str = "real"
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765


class ModelChoice(BaseModel):
    agent: str
    model: str = ""


class GateResearch(BaseModel):
    max_open_critical: int = 0
    max_open_major: int = 0
    require_primary_claims_in: list[str] = ["Verified", "Supported"]
    forbid_claim_status: list[str] = ["Contradicted"]
    require_tests_pass: bool = True
    require_preregistration_intact: bool = True
    require_independent_recalculation: bool = True
    require_auditor_differs_from_implementer: bool = True
    max_audit_rounds: int = 3


class GatePublication(BaseModel):
    min_score: float = 80
    max_open_critical: int = 0
    max_open_major: int = 0
    require_no_secrets: bool = True
    require_limitations_section: bool = True
    require_reproduction_steps: bool = True
    require_license: bool = True
    max_audit_rounds: int = 2


class Gates(BaseModel):
    research: GateResearch = GateResearch()
    publication: GatePublication = GatePublication()


class ResearchPolicy(BaseModel):
    default_engine: str = "native"
    budget_usd: float = 15.0
    budget_soft_ratio: float = 0.8
    max_cycles: int = 3
    max_wallclock_hours: float = 12
    phase_timeout_minutes: int = 90
    pilot_max_budget_ratio: float = 0.15
    continue_if: dict[str, float] = Field(default_factory=dict)
    models: dict[str, ModelChoice] = Field(default_factory=dict)

    def model_for(self, role: str) -> ModelChoice:
        return self.models.get(role) or ModelChoice(agent="claude", model="sonnet")


class ScoringPolicy(BaseModel):
    rubric_version: str = "2026-11"
    weights: dict[str, float]
    caps: list[dict[str, Any]] = Field(default_factory=list)
    practical_reserve: dict[str, Any] = Field(default_factory=dict)
    shortlist_size: int = 12
    proposals_per_week: int = 5

    def weights_sha256(self) -> str:
        canon = yaml.safe_dump({"w": self.weights, "c": self.caps}, sort_keys=True)
        return hashlib.sha256(canon.encode()).hexdigest()


class IngestPolicy(BaseModel):
    lookback_days: int = 2
    max_per_query: int = 60
    arxiv_categories: list[str] = Field(default_factory=list)
    topics: list[dict[str, str]] = Field(default_factory=list)


class PublicationPolicy(BaseModel):
    create_pull_request: bool = True
    allowed_remote_visibility: list[str] = ["private"]
    base_branch: str = "main"
    branch_prefix: str = "research/"
    channels: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SafetyPolicy(BaseModel):
    sandbox: str = "docker"
    docker_image: str = "python:3.12-slim"
    docker_network: str = "none"
    docker_memory: str = "2g"
    docker_cpus: float = 2
    experiment_timeout_minutes: int = 30
    allow_metered_api_in_experiments: bool = False
    min_free_disk_mb: int = 150
    hold_on: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    version: int = 1
    ingest: IngestPolicy
    scoring: ScoringPolicy
    research: ResearchPolicy
    gates: Gates
    publication: PublicationPolicy
    safety: SafetyPolicy
    notify: dict[str, Any] = Field(default_factory=dict)
    sha256: str = ""  # ファイル全体のハッシュ（読み込み時に設定）


def policy_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_policy(path: Path | None = None) -> Policy:
    path = path or get_settings().policy_path
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = Policy.model_validate(data)
    policy.sha256 = policy_file_sha256(path)
    return policy


@lru_cache
def get_settings() -> Settings:
    return Settings()
