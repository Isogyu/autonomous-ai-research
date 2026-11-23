"""Web UI のテスト。実際の PostgreSQL に行を直接入れ、サービス層は偽物に差し替える。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from aar.models import Proposal

pytestmark = pytest.mark.db

EVIL = "<script>alert(1)</script>"
HOST = {"host": "testserver"}


class FakeService:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.controls: list[tuple[str, str]] = []
        self.fail = False

    async def select_and_start(self, proposal_id: str) -> dict:
        if self.fail:
            raise RuntimeError("起動に失敗")
        self.started.append(proposal_id)
        return {"id": "RP-TEST-1"}

    async def control_project(self, project_id: str, action: str) -> None:
        self.controls.append((project_id, action))

    def search_memory(self, query, kinds=None, limit=20):
        self.last_search = (query, kinds, limit)
        return [{"entity_type": "lesson", "entity_id": "1", "title": "検索ヒット題名",
                 "snippet": "抜粋テキスト", "score": 0.5, "project_id": "RP-TEST-1"}]

    def self_metrics(self):
        return {"proposals_selected": 3, "note": "良好", "empty": None}


@pytest.fixture
def env(db_url, monkeypatch):
    monkeypatch.setenv("AAR_DATABASE_URL", db_url)
    from aar.config import get_settings

    get_settings.cache_clear()
    from aar.ui.app import create_app, get_service

    app = create_app()
    fake = FakeService()
    app.dependency_overrides[get_service] = lambda: fake
    yield TestClient(app, headers=HOST), fake, db_url
    get_settings.cache_clear()


def _body(title: str = "企画A") -> dict:
    p = Proposal(
        title=title, source_paper="論文X (arXiv:2609.00001)", published_at="2026-11-01",
        novelty="NOVELTY-TXT", known="KNOWN-TXT", unknown="UNKNOWN-TXT",
        hypotheses=["HYP-ONE", "HYP-TWO"], method="METHOD-TXT", datasets=["DS-ONE"],
        est_runtime_hours=3.5, est_cost_usd=12.5, compute="COMPUTE-TXT",
        expected_outcomes="EXPECTED-TXT", value_if_negative="NEGVALUE-TXT",
        implications="IMPL-TXT", story="STORY-TXT", risks=["RISK-ONE"],
    )
    return p.model_dump()


def _insert_proposal(url: str, title: str = "企画A", mode: str = "real", paper_title: str = "論文X") -> str:
    from aar.db.core import connect

    with connect(url) as conn:
        pid = conn.execute(
            "INSERT INTO papers (title, title_norm, url) VALUES (%s, %s, 'https://arxiv.org/abs/2609.00001')"
            " RETURNING id", (paper_title, paper_title.lower()),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO paper_scores (paper_id, rubric_version, weights_sha256, criteria, total, scorer)"
            " VALUES (%s, 'v1', 'x', %s, 77.5, 'heuristic')",
            (pid, Jsonb({"novelty": {"score": 4, "rationale": "RATIONALE-TXT"}})),
        )
        row = conn.execute(
            "INSERT INTO research_proposals (batch_key, paper_id, title, body, rank_score, generated_by,"
            " execution_mode) VALUES ('2026-W47', %s, %s, %s, 80, 'test', %s) RETURNING id",
            (pid, title, Jsonb(_body(title)), mode),
        ).fetchone()
    return str(row["id"])


def _insert_project(url: str, status: str = "running", mode: str = "real") -> str:
    from aar.db.core import connect

    with connect(url) as conn:
        conn.execute(
            "INSERT INTO research_projects (id, title, workflow_id, status, phase, execution_mode, engine,"
            " budget_usd, policy_sha256, started_at) VALUES ('RP-TEST-1', '研究T', 'wf-1', %s, 'experiment',"
            " %s, 'mock', 50, 'abc', now())", (status, mode),
        )
        conn.execute(
            "INSERT INTO cost_ledger (project_id, category, billing, purpose, amount_usd, idempotency_key)"
            " VALUES ('RP-TEST-1', 'claude', 'subscription_equivalent', 't', 1.25, 'k1')"
        )
        conn.execute(
            "INSERT INTO hypotheses (project_id, label, statement, kind) VALUES"
            " ('RP-TEST-1', 'H1', 'PREREG-HYP-STATEMENT', 'preregistered'),"
            " ('RP-TEST-1', 'X1', 'EXPLORE-HYP-STATEMENT', 'exploratory')"
        )
        run = conn.execute(
            "INSERT INTO audit_runs (project_id, kind, round, auditor, implementer, skill, status,"
            " idempotency_key) VALUES ('RP-TEST-1', 'research', 1, 'codex', 'claude', 's', 'completed', 'a1')"
            " RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO audit_findings (audit_run_id, project_id, issue_id, severity, title) VALUES"
            " (%s, 'RP-TEST-1', 'I-2', 'Minor', 'MINOR-FINDING'),"
            " (%s, 'RP-TEST-1', 'I-1', 'Critical', 'CRITICAL-FINDING')", (run, run),
        )
        conn.execute(
            "INSERT INTO gate_evaluations (project_id, gate, round, passed, policy_sha256, checks)"
            " VALUES ('RP-TEST-1', 'research', 1, false, 'abc', %s)",
            (Jsonb([{"name": "GATE-CHECK-NG", "passed": False, "detail": "d"}]),),
        )
    return "RP-TEST-1"


def test_一覧に企画が出る(env):
    client, _, url = env
    pid = _insert_proposal(url, mode="mock")
    r = client.get("/")
    assert r.status_code == 200
    assert "企画A" in r.text and "HYP-ONE" in r.text and f"/proposals/{pid}" in r.text
    assert "モック" in r.text


def test_企画がないときの案内(env):
    client, _, _ = env
    r = client.get("/")
    assert "aar proposals generate" in r.text


def test_詳細に16項目と価値評価(env):
    client, _, url = env
    pid = _insert_proposal(url)
    text = client.get(f"/proposals/{pid}").text
    for needle in ["企画A", "論文X (arXiv:2609.00001)", "2026-11-01", "NOVELTY-TXT", "KNOWN-TXT",
                   "UNKNOWN-TXT", "HYP-TWO", "METHOD-TXT", "DS-ONE", "3.5", "12.50", "COMPUTE-TXT",
                   "EXPECTED-TXT", "NEGVALUE-TXT", "IMPL-TXT", "STORY-TXT", "RISK-ONE", "要旨のみに基づく",
                   "RATIONALE-TXT", "77.5", "この企画で研究を開始する"]:
        assert needle in text, needle
    assert 'href="https://arxiv.org/abs/2609.00001"' in text


def test_選択で偽サービスが呼ばれ303(env):
    client, fake, url = env
    pid = _insert_proposal(url)
    r = client.post(f"/proposals/{pid}/select", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/projects/RP-TEST-1"
    assert fake.started == [pid]


def test_選択の失敗は画面に出す(env):
    client, fake, url = env
    pid = _insert_proposal(url)
    fake.fail = True
    r = client.post(f"/proposals/{pid}/select", follow_redirects=False)
    assert r.status_code != 500 and "起動に失敗" in r.text


def test_研究詳細(env):
    client, _, url = env
    pid = _insert_project(url, "running", "mock")
    text = client.get(f"/projects/{pid}").text
    assert "PREREG-HYP-STATEMENT" in text and "EXPLORE-HYP-STATEMENT" in text
    assert "CRITICAL-FINDING" in text and text.index("CRITICAL-FINDING") < text.index("MINOR-FINDING")
    assert "GATE-CHECK-NG" in text and "✗" in text
    assert "モック実行（実際の実験・監査ではない）" in text
    assert "定額契約の利用量を API 定価に換算した推定額" in text
    assert 'http-equiv="refresh"' in text
    assert "中止する（取り消せません）" in text


def test_停止中は自動更新とボタンの出し分け(env):
    client, _, url = env
    _insert_project(url, "completed")
    text = client.get("/projects/RP-TEST-1").text
    assert "refresh" not in text and "中止する" not in text


def test_研究一覧とフィルタ(env):
    client, _, url = env
    _insert_project(url, "running")
    assert "研究T" in client.get("/projects").text
    assert "研究T" not in client.get("/projects?status=failed").text


def test_外部由来の題名はエスケープされる(env):
    client, _, url = env
    pid = _insert_proposal(url, title=EVIL)
    for path in ("/", f"/proposals/{pid}"):
        text = client.get(path).text
        assert EVIL not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_javascriptのURLはリンクにしない(env):
    client, _, url = env
    from aar.db.core import connect

    _insert_project(url)
    with connect(url) as conn:
        conn.execute("UPDATE research_projects SET pr_url = 'javascript:alert(1)'")
    text = client.get("/projects/RP-TEST-1").text
    assert 'href="javascript:' not in text and "javascript:alert(1)" in text


def test_不正なactionは400(env):
    client, fake, url = env
    _insert_project(url)
    r = client.post("/projects/RP-TEST-1/control", data={"action": "explode"})
    assert r.status_code == 400 and fake.controls == []
    r = client.post("/projects/RP-TEST-1/control", data={"action": "pause"}, follow_redirects=False)
    assert r.status_code == 303 and fake.controls == [("RP-TEST-1", "pause")]


def test_別オリジンのPOSTは403(env):
    client, fake, url = env
    _insert_project(url)
    r = client.post("/projects/RP-TEST-1/control", data={"action": "pause"},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and fake.controls == []
    r = client.post("/projects/RP-TEST-1/control", data={"action": "pause"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_検索と指標(env):
    client, fake, _ = env
    text = client.get("/search?q=テスト&kind=lesson&kind=paper").text
    assert "検索ヒット題名" in text and "抜粋テキスト" in text and "/projects/RP-TEST-1" in text
    assert fake.last_search == ("テスト", ["lesson", "paper"], 20)
    text = client.get("/metrics").text
    assert "proposals_selected" in text and "良好" in text


def test_通知の既読化とhealthz(env):
    client, _, url = env
    from aar.db.core import connect

    with connect(url) as conn:
        conn.execute("INSERT INTO notifications (level, title, idempotency_key) VALUES ('info', 'NOTI-T', 'n1')")
    assert "NOTI-T" in client.get("/").text
    r = client.post("/notifications/read", follow_redirects=False)
    assert r.status_code == 303
    assert "NOTI-T" not in client.get("/").text
    assert client.get("/healthz").json() == {"ok": True}
