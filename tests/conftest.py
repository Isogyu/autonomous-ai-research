"""テスト共通のフィクスチャ。"""

from __future__ import annotations

import os

import pytest

# 既存のテストの前提を保つための既定値（実運用の既定とは違う。個別のテストで上書きして確かめる）
os.environ.setdefault("AAR_HOLD_ON_UNKNOWN_SLEEP", "0")                  # 電源の記録を読めない CI で、失敗の経路を保留にしない
os.environ.setdefault("AAR_IMPROVE_ALLOW_UNISOLATED_GENERATOR", "1")     # 候補の生成は台本の実行担当で確かめる


def pytest_collection_modifyitems(config, items):
    import sys

    for item in items:
        if "podman" in item.keywords and os.environ.get("AAR_PODMAN_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="AAR_PODMAN_TESTS=1 のときだけ実行（skip は Podman での合格に数えない）"))
        if "windows_only" in item.keywords and sys.platform != "win32":
            item.add_marker(pytest.mark.skip(reason="Windows でだけ実行（skip は Windows での合格に数えない）"))


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
