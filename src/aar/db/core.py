"""PostgreSQL への接続と、SQL ファイルによるスキーマ移行。

移行は番号順の SQL ファイルを1つずつトランザクションで適用し、適用済みを schema_migrations に残す。
ORM は使わず、SQL をそのまま書く（検索の SQL を隠さないため。docs/adr/0004 を参照）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from aar.config import get_settings


@contextmanager
def connect(url: str | None = None, *, vector: bool = True) -> Iterator[psycopg.Connection]:
    """1回の処理単位の接続。正常終了でコミット、例外でロールバックする。"""
    with psycopg.connect(url or get_settings().database_url, row_factory=dict_row) as conn:
        if vector:
            try:
                register_vector(conn)
            except psycopg.ProgrammingError:
                conn.rollback()  # 拡張がまだ無い（移行前）
        yield conn


def migrate(url: str | None = None) -> list[str]:
    """未適用の移行を適用し、適用したファイル名を返す。"""
    applied: list[str] = []
    files = sorted(
        (f for f in resources.files("aar.db").joinpath("migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    with connect(url, vector=False) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # 同時に2つの移行が走らないようにする
        conn.execute("SELECT pg_advisory_xact_lock(727001)")
        done = {r["name"]: r["sha256"] for r in conn.execute("SELECT name, sha256 FROM schema_migrations")}
        for f in files:
            sql = f.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if f.name in done:
                if done[f.name] != digest:
                    raise RuntimeError(f"適用済みの移行 {f.name} が書き換えられています。新しい番号で追加してください。")
                continue
            conn.execute(sql)  # type: ignore[arg-type]
            conn.execute("INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)", (f.name, digest))
            applied.append(f.name)
    return applied


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict[str, Any]]:
    return list(conn.execute(sql, params))  # type: ignore[arg-type]


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict[str, Any] | None:
    return conn.execute(sql, params).fetchone()  # type: ignore[arg-type,return-value]


def run_once(conn: psycopg.Connection, key: str, kind: str) -> dict[str, Any] | None:
    """副作用の重複防止。初めてなら None、実行済みなら保存済みの結果を返す。"""
    row = fetch_one(conn, "SELECT result FROM side_effects WHERE idempotency_key = %s", (key,))
    return row["result"] if row else None


def record_once(conn: psycopg.Connection, key: str, kind: str, result: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO side_effects (idempotency_key, kind, result) VALUES (%s, %s, %s)"
        " ON CONFLICT (idempotency_key) DO NOTHING",
        (key, kind, Jsonb(result)),
    )
