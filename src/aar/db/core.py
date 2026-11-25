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


def pending_migrations(url: str | None = None) -> list[str]:
    """まだ適用していない移行の名前（読むだけ。アプリ用の権限でも呼べる）。"""
    names = sorted(f.name for f in resources.files("aar.db").joinpath("migrations").iterdir() if f.name.endswith(".sql"))
    with connect(url, vector=False) as conn:
        if not conn.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"]:   # type: ignore[index]
            return names
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    return [n for n in names if n not in done]


# 追記だけの表（改善の台帳の評価計画・実行・判定・操作の記録）と、移行の記録
APPEND_ONLY_TABLES = ("improvement_eval_plans", "improvement_runs", "improvement_decisions", "improvement_events")
APP_ROLE = "aar_app"


def setup_app_role(owner_url: str, password: str) -> dict[str, Any]:
    """アプリの実行用の役割（aar_app）を用意し、権限を付け直す。表の所有者の接続で呼ぶ。何度呼んでもよい。

    表の所有者（移行を適用する役割）は、追記だけの表のトリガーを無効にも削除にもできる。アプリを所有者の権限で
    動かすと、保護は「UPDATE と DELETE の拒否」までになる。実行用の役割を分け、追記だけの表には SELECT と INSERT
    だけを許すと、アプリの権限では、書き換えも、トリガーの無効化・削除も、TRUNCATE もできない。
    """
    from psycopg import sql

    role = sql.Identifier(APP_ROLE)
    with psycopg.connect(owner_url, autocommit=True, row_factory=dict_row) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)).fetchone()
        opts = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Literal(password))
        conn.execute(sql.SQL("{} ROLE {} {}").format(sql.SQL("ALTER" if exists else "CREATE"), role, opts))
        db = conn.execute("SELECT current_database() AS d").fetchone()["d"]   # type: ignore[index]
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), role))
        conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
        protected = [t for t in (*APPEND_ONLY_TABLES, "schema_migrations")
                     if conn.execute("SELECT to_regclass(%s) AS t", (f"public.{t}",)).fetchone()["t"]]   # type: ignore[index]
        for t in protected:
            conn.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON {} FROM {}").format(sql.Identifier(t), role))
        if "schema_migrations" in protected:
            conn.execute(sql.SQL("REVOKE INSERT ON schema_migrations FROM {}").format(role))
    return {"role": APP_ROLE, "database": db, "created": not exists, "append_only": [t for t in protected if t != "schema_migrations"]}


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
