"""改善候補の台帳（候補、根拠、評価計画、実行、判定、操作の記録）。

操作主体は必ず種類を持つ: human（人）、claude_code、codex、scheduler（定期処理）、policy（機械的な判定）。
評価計画・実行・判定・操作の記録は追記だけで、DB の側で UPDATE と DELETE を拒否する。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aar.db.core import fetch_all, fetch_one

ACTOR_KINDS = ("human", "claude_code", "codex", "scheduler", "policy")
ACTIVE = ("proposed", "generated", "evaluated", "on_hold")


@dataclass(frozen=True)
class Actor:
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.detail}" if self.detail else self.kind


def parse_actor(text: str | None) -> Actor:
    """「種類:補足」の形の操作主体。指定がなければ環境変数 AAR_ACTOR。どちらもなければ受け付けない（人の操作と決めつけない）。"""
    text = (text or os.environ.get("AAR_ACTOR") or "").strip()
    kind, _, detail = text.partition(":")
    if kind not in ACTOR_KINDS:
        raise ValueError(f"操作主体を指定する（--actor または AAR_ACTOR）: {' / '.join(ACTOR_KINDS)} のどれかで始める。例 claude_code:session")
    return Actor(kind, detail)


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def log_event(conn: psycopg.Connection, candidate_id: str | None, event: str, actor: Actor, detail: dict[str, Any] | None = None,
              run_id: int | None = None) -> None:
    conn.execute("""INSERT INTO improvement_events (candidate_id, event, actor_kind, actor_detail, run_id, detail)
                    VALUES (%s, %s, %s, %s, %s, %s)""", (candidate_id, event, actor.kind, actor.detail, run_id, Jsonb(detail or {})))


def propose(conn: psycopg.Connection, *, target: str, base_commit: str, origin_kind: str, origin_fingerprint: str,
            origin: dict[str, Any], problem: str, hypothesis: str, allowed_paths: list[str], actor: Actor, area_key: str | None = None,
            applies_when: str = "", side_effects: str = "", parent_id: str | None = None, max_per_origin: int = 3) -> dict[str, Any]:
    """候補を登録する。同じ発見元から進行中の候補があればそれを返し、上限を超える登録は拒否する（乱造を抑える）。"""
    conn.execute("SELECT pg_advisory_xact_lock(727002)")
    same = fetch_all(conn, "SELECT * FROM improvement_candidates WHERE target = %s AND origin_fingerprint = %s ORDER BY created_at",
                     (target, origin_fingerprint))
    if parent_id is None:
        for c in same:
            if c["status"] in ACTIVE:
                log_event(conn, c["id"], "duplicate_proposal_suppressed", actor, {"origin_fingerprint": origin_fingerprint})
                return c | {"created": False}
    if len(same) >= max_per_origin:
        raise ValueError(f"同じ発見元からの候補が上限（{max_per_origin} 件）に達している。過去の候補: " + "、".join(c["id"] for c in same))
    cid = f"IC-{dt.datetime.now(dt.UTC):%Y%m%d}-{secrets.token_hex(3)}"
    conn.execute("""INSERT INTO improvement_candidates (id, parent_id, area_key, target, base_commit, origin_kind, origin_fingerprint, origin,
                                                        problem, hypothesis, applies_when, side_effects, allowed_paths, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (cid, parent_id, area_key, target, base_commit, origin_kind, origin_fingerprint, Jsonb(origin), problem, hypothesis,
                  applies_when, side_effects, allowed_paths, str(actor)))
    log_event(conn, cid, "proposed", actor, {"origin_kind": origin_kind, "origin_fingerprint": origin_fingerprint, "parent_id": parent_id})
    return fetch_one(conn, "SELECT * FROM improvement_candidates WHERE id = %s", (cid,)) | {"created": True}   # type: ignore[operator]


def get_candidate(conn: psycopg.Connection, candidate_id: str) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT * FROM improvement_candidates WHERE id = %s", (candidate_id,))
    if not row:
        raise KeyError(f"候補が見つからない: {candidate_id}")
    return row


def set_candidate(conn: psycopg.Connection, candidate_id: str, actor: Actor, event: str, **fields: Any) -> None:
    """候補の状態などを更新し、操作の記録を残す。"""
    cols = ", ".join(f"{k} = %s" for k in fields)
    vals = [Jsonb(v) if isinstance(v, dict) else v for v in fields.values()]
    conn.execute(f"UPDATE improvement_candidates SET {cols}, updated_at = now() WHERE id = %s", (*vals, candidate_id))   # noqa: S608
    log_event(conn, candidate_id, event, actor,
              {k: (v if isinstance(v, str | int | float | bool | type(None)) and k != "patch" else "(省略)") for k, v in fields.items()})


def freeze_plan(conn: psycopg.Connection, *, target: str, plan: dict[str, Any], plan_sha: str, evaluator_sha: str, cases_sha: str,
                actor: Actor) -> dict[str, Any]:
    """評価計画を固定する。同じ内容なら既存のものを返す（内容を変えたら別の計画になる）。"""
    row = fetch_one(conn, "SELECT * FROM improvement_eval_plans WHERE plan_sha256 = %s", (plan_sha,))
    if row:
        return row | {"created": False}
    conn.execute("""INSERT INTO improvement_eval_plans (target, plan, plan_sha256, evaluator_sha256, cases_sha256, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s)""", (target, Jsonb(plan), plan_sha, evaluator_sha, cases_sha, str(actor)))
    row = fetch_one(conn, "SELECT * FROM improvement_eval_plans WHERE plan_sha256 = %s", (plan_sha,))
    log_event(conn, None, "plan_frozen", actor, {"plan_id": row["id"], "plan_sha256": plan_sha, "evaluator_sha256": evaluator_sha,   # type: ignore[index]
                                                 "cases_sha256": cases_sha})
    return row | {"created": True}   # type: ignore[operator]


def get_plan(conn: psycopg.Connection, plan_id: int) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT * FROM improvement_eval_plans WHERE id = %s", (plan_id,))
    if not row:
        raise KeyError(f"評価計画が見つからない: {plan_id}")
    return row


def get_run(conn: psycopg.Connection, run_key: str) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT * FROM improvement_runs WHERE run_key = %s", (run_key,))


def record_run(conn: psycopg.Connection, *, run_key: str, plan_id: int, candidate_id: str | None, variant: str, split: str,
               commit_sha: str, result: dict[str, Any], environment: dict[str, Any], raw_path: str, actor: Actor) -> dict[str, Any]:
    """実行の結果を記録する。同じ run_key の記録があれば、それを返す（中断・再開で重ねない）。"""
    conn.execute("SELECT pg_advisory_xact_lock(727003)")
    existing = get_run(conn, run_key)
    if existing:
        return existing | {"created": False}
    usage = {"external_model_calls": 0, "internal_waits": 0, "seconds": result.get("seconds", 0.0), "billing": "none（モデルを呼ばない）"}
    conn.execute("""INSERT INTO improvement_runs (run_key, plan_id, candidate_id, variant, split, commit_sha, status, failure_kind, isolation,
                                                  environment, metrics, per_case, usage, raw_path, actor)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (run_key, plan_id, candidate_id, variant, split, commit_sha, result["status"], result.get("failure_kind", ""),
                  result["isolation"], Jsonb(environment), Jsonb(result.get("metrics", {})), Jsonb(result.get("per_case", [])),
                  Jsonb(usage), raw_path, str(actor)))
    row = get_run(conn, run_key)
    log_event(conn, candidate_id, "run_recorded", actor, {"run_key": run_key, "variant": variant, "split": split, "status": result["status"],
                                                           "failure_kind": result.get("failure_kind", "")}, run_id=row["id"])   # type: ignore[index]
    return row | {"created": True}   # type: ignore[operator]


def record_decision(conn: psycopg.Connection, candidate_id: str, plan_id: int, decision: dict[str, Any]) -> dict[str, Any]:
    conn.execute("""INSERT INTO improvement_decisions (candidate_id, plan_id, verdict, checks, reasons, decided_by)
                    VALUES (%s, %s, %s, %s, %s, 'policy')""",
                 (candidate_id, plan_id, decision["verdict"], Jsonb(decision["checks"]), Jsonb(decision["reasons"])))
    log_event(conn, candidate_id, "decided", Actor("policy", "aar.improve.decide"), {"verdict": decision["verdict"], "reasons": decision["reasons"]})
    return fetch_one(conn, "SELECT * FROM improvement_decisions WHERE candidate_id = %s ORDER BY id DESC LIMIT 1", (candidate_id,))   # type: ignore[return-value]


def search(conn: psycopg.Connection, text: str = "", status: str = "") -> list[dict[str, Any]]:
    """候補を探す。棄却の理由と、不採用になった条件も検索の対象にする。"""
    like = f"%{text}%"
    return fetch_all(conn, """SELECT id, target, status, problem, status_reason, rejected_under, origin_kind, created_by, created_at
                              FROM improvement_candidates
                              WHERE (%s = '' OR status = %s)
                                AND (%s = '' OR problem ILIKE %s OR hypothesis ILIKE %s OR status_reason ILIKE %s OR rejected_under ILIKE %s
                                     OR applies_when ILIKE %s)
                              ORDER BY created_at DESC""", (status, status, text, like, like, like, like, like))
