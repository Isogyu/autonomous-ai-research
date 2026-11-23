"""テスト共通のフィクスチャ。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def db_url() -> str:
    """毎回スキーマを作り直したテスト用 DB の URL。AAR_TEST_DATABASE_URL が無ければ skip。"""
    url = os.environ.get("AAR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AAR_TEST_DATABASE_URL が未設定")
    import psycopg

    from aar.db.core import migrate

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    migrate(url)
    return url
