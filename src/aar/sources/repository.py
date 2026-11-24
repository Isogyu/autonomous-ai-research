"""論文の収集（collect_all）と PostgreSQL への保存（upsert_papers）。"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aar.config import IngestPolicy, Settings
from aar.models import PaperRecord
from aar.sources.arxiv import search_arxiv
from aar.sources.dedupe import (
    arxiv_id_from_doi,
    is_arxiv_doi,
    normalize_author,
    normalize_author_full,
    normalize_doi,
    normalize_title,
)
from aar.sources.openalex import search_openalex
from aar.sources.semantic_scholar import enrich_semantic_scholar

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 収集

ARXIV_MIN_LOOKBACK_DAYS = 7


async def collect_all(
    ingest: IngestPolicy, settings: Settings, today: date
) -> tuple[list[PaperRecord], list[str]]:
    """全 topic を arXiv と OpenAlex から集め、arXiv ID のあるものを Semantic Scholar で補強する。

    1つの情報源の失敗は errors に積んで続行する。
    """
    since = today - timedelta(days=ingest.lookback_days)
    # arXiv の検索 API は、投稿が索引に載るまで数日遅れることがあり、週末は新しい投稿が載らない
    # （2026-11-24 火曜の時点で、最新は 11-20 金曜の投稿だった）。短い期間だけを見ると 0 件が続くので、
    # arXiv だけは少なくとも ARXIV_MIN_LOOKBACK_DAYS 日さかのぼる。保存は同じ論文を上書きするので、重なっても増えない
    arxiv_since = today - timedelta(days=max(ingest.lookback_days, ARXIV_MIN_LOOKBACK_DAYS))
    records: list[PaperRecord] = []
    errors: list[str] = []
    for topic in ingest.topics:
        name, query = topic.get("name", ""), topic.get("query", "")
        try:
            records += await search_arxiv(
                query, ingest.arxiv_categories, arxiv_since, today, ingest.max_per_query
            )
        except Exception as e:
            errors.append(f"arxiv[{name}]: {e!r}")
        try:
            records += await search_openalex(
                query, since, ingest.max_per_query, settings.openalex_api_key
            )
        except Exception as e:
            errors.append(f"openalex[{name}]: {e!r}")
    ids = sorted({f"ARXIV:{r.arxiv_id}" for r in records if r.arxiv_id})
    try:
        records += await enrich_semantic_scholar(ids, settings.semantic_scholar_api_key)
    except Exception as e:
        errors.append(f"semantic_scholar: {e!r}")
    return records, errors


# ------------------------------------------------------------------ 保存

_UNIQUE_COLS = ("doi", "arxiv_id", "openalex_id", "s2_id")


def _find_existing(conn: psycopg.Connection, r: PaperRecord, ids: dict[str, str | None], fa: str | None, tn: str):
    row = conn.execute(
        "SELECT paper_id FROM paper_sources WHERE source = %s AND source_id = %s", (r.source, r.source_id)
    ).fetchone()
    if row:
        return row["paper_id"]
    for col in _UNIQUE_COLS:
        if ids[col]:
            row = conn.execute(f"SELECT id FROM papers WHERE {col} = %s", (ids[col],)).fetchone()  # noqa: S608
            if row:
                return row["id"]
    if tn:
        row = conn.execute(
            "SELECT id FROM papers WHERE title_norm = %s"
            " AND (first_author_norm = %s OR first_author_norm IS NULL OR %s::text IS NULL) LIMIT 1",
            (tn, fa, fa),
        ).fetchone()
        if row:
            return row["id"]
    return None


def _free_ids(conn: psycopg.Connection, ids: dict[str, str | None], paper_id: Any) -> dict[str, str | None]:
    """他の行が既に持っている一意な ID は書き込まない（制約違反の回避）。"""
    out = {}
    for col, val in ids.items():
        if val and conn.execute(
            f"SELECT 1 FROM papers WHERE {col} = %s AND id <> %s", (val, paper_id)  # noqa: S608
        ).fetchone():
            val = None
        out[col] = val
    return out


def _upsert_one(conn: psycopg.Connection, r: PaperRecord) -> str:
    """1件を保存して 'inserted' / 'updated' / 'merged' を返す。"""
    doi = normalize_doi(r.doi)
    arxiv_id = r.arxiv_id.lower() if r.arxiv_id else arxiv_id_from_doi(doi)
    if is_arxiv_doi(doi):
        doi = None  # arXiv の DOI は出版版の DOI を埋める妨げになるので持たない
    ids: dict[str, str | None] = {
        "doi": doi, "arxiv_id": arxiv_id, "openalex_id": r.openalex_id, "s2_id": r.s2_id,
    }
    fa = normalize_author(r.authors[0]) if r.authors else None
    fa = fa or None
    tn = normalize_title(r.title)

    pid = _find_existing(conn, r, ids, fa, tn)
    if pid is None:
        row = conn.execute(
            "INSERT INTO papers (title, title_norm, abstract, doi, arxiv_id, arxiv_version, openalex_id, s2_id,"
            " published_at, source_updated_at, peer_review_status, venue, categories, url, pdf_url, license,"
            " citation_count, first_author_norm)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                r.title, tn, r.abstract, doi, arxiv_id, r.arxiv_version, r.openalex_id, r.s2_id,
                r.published_at, r.source_updated_at, r.peer_review_status, r.venue, r.categories,
                r.url, r.pdf_url, r.license, r.citation_count, fa,
            ),
        ).fetchone()
        pid = row["id"]
        outcome = "inserted"
    else:
        seen = conn.execute(
            "SELECT 1 FROM paper_sources WHERE paper_id = %s AND source = %s", (pid, r.source)
        ).fetchone()
        outcome = "updated" if seen else "merged"
        free = _free_ids(conn, ids, pid)
        conn.execute(
            "UPDATE papers SET"
            " abstract = CASE WHEN length(coalesce(%s::text, '')) > length(coalesce(abstract, ''))"
            "            THEN %s::text ELSE abstract END,"
            " doi = COALESCE(doi, %s), arxiv_id = COALESCE(arxiv_id, %s),"
            " openalex_id = COALESCE(openalex_id, %s), s2_id = COALESCE(s2_id, %s),"
            " arxiv_version = GREATEST(arxiv_version, %s::int),"
            " published_at = COALESCE(published_at, %s),"
            " source_updated_at = GREATEST(source_updated_at, %s::timestamptz),"
            " peer_review_status = CASE"
            "   WHEN peer_review_status = 'peer_reviewed' OR %s = 'peer_reviewed' THEN 'peer_reviewed'"
            "   WHEN peer_review_status = 'preprint' OR %s = 'preprint' THEN 'preprint'"
            "   ELSE 'unknown' END,"
            " venue = COALESCE(venue, %s),"
            " categories = CASE WHEN cardinality(categories) = 0 THEN %s::text[] ELSE categories END,"
            " url = COALESCE(url, %s), pdf_url = COALESCE(pdf_url, %s), license = COALESCE(license, %s),"
            " citation_count = GREATEST(citation_count, %s::int),"
            " first_author_norm = COALESCE(first_author_norm, %s),"
            " last_seen_at = now()"
            " WHERE id = %s",
            (
                r.abstract, r.abstract,
                free["doi"], free["arxiv_id"], free["openalex_id"], free["s2_id"],
                r.arxiv_version, r.published_at, r.source_updated_at,
                r.peer_review_status, r.peer_review_status,
                r.venue, r.categories, r.url, r.pdf_url, r.license, r.citation_count, fa, pid,
            ),
        )

    conn.execute(
        "INSERT INTO paper_sources (paper_id, source, source_id, url, retrieved_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (source, source_id) DO UPDATE SET"
        " url = EXCLUDED.url, retrieved_at = EXCLUDED.retrieved_at, raw = EXCLUDED.raw",
        (pid, r.source, r.source_id, r.url, r.retrieved_at, Jsonb(r.raw)),
    )

    if r.arxiv_version:
        sha = hashlib.sha256(f"{r.title}\n{r.abstract or ''}".encode()).hexdigest()
        conn.execute(
            "INSERT INTO paper_versions (paper_id, version, source_updated_at, retrieved_at, content_sha256)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (paper_id, version) DO NOTHING",
            (pid, r.arxiv_version, r.source_updated_at, r.retrieved_at, sha),
        )

    for pos, name in enumerate(r.authors):
        norm = normalize_author_full(name)
        if not norm:
            continue
        a = conn.execute("SELECT id FROM authors WHERE name_norm = %s LIMIT 1", (norm,)).fetchone()
        if a is None:
            a = conn.execute(
                "INSERT INTO authors (name, name_norm) VALUES (%s, %s) RETURNING id", (name, norm)
            ).fetchone()
        conn.execute(
            "INSERT INTO paper_authors (paper_id, author_id, position) VALUES (%s,%s,%s)"
            " ON CONFLICT DO NOTHING",
            (pid, a["id"], pos),
        )

    # 引用: この論文が引用するもの
    for ref in dict.fromkeys(r.references):
        conn.execute(
            "INSERT INTO citations (citing_paper_id, cited_external_id, source, retrieved_at)"
            " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (pid, ref, r.source, r.retrieved_at),
        )
    conn.execute(
        "UPDATE citations c SET cited_paper_id = p.id FROM papers p"
        " WHERE c.citing_paper_id = %s AND c.cited_paper_id IS NULL AND p.id <> c.citing_paper_id AND ("
        "   c.cited_external_id = 'arxiv:' || p.arxiv_id OR c.cited_external_id = 'doi:' || p.doi"
        "   OR c.cited_external_id = 'openalex:' || p.openalex_id)",
        (pid,),
    )
    # 後埋め: この論文を引用していた既存の行
    cur = conn.execute("SELECT arxiv_id, doi, openalex_id FROM papers WHERE id = %s", (pid,)).fetchone()
    keys = [
        f"{p}:{cur[c]}" for p, c in (("arxiv", "arxiv_id"), ("doi", "doi"), ("openalex", "openalex_id")) if cur[c]
    ]
    if keys:
        conn.execute(
            "UPDATE citations SET cited_paper_id = %s"
            " WHERE cited_paper_id IS NULL AND citing_paper_id <> %s AND cited_external_id = ANY(%s)",
            (pid, pid, keys),
        )
    return outcome


def upsert_papers(conn: psycopg.Connection, records: list[PaperRecord]) -> dict[str, int]:
    """論文を保存する。1件ごとに SAVEPOINT で囲み、失敗は failed に数えてログに出す。"""
    stats = {"inserted": 0, "updated": 0, "merged": 0, "failed": 0}
    for r in records:
        try:
            with conn.transaction():
                stats[_upsert_one(conn, r)] += 1
        except Exception as e:
            stats["failed"] += 1
            log.warning("保存に失敗 %s:%s: %s", r.source, r.source_id, e)
    return stats
