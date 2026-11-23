"""arXiv API（Atom）からの収集。3秒に1リクエスト・同時1接続を守る。"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

import httpx

from aar.config import get_settings
from aar.models import PaperRecord
from aar.sources.base import RateLimiter, get_with_retry, make_client
from aar.sources.dedupe import normalize_doi

log = logging.getLogger(__name__)

ARXIV_URL = "https://export.arxiv.org/api/query"
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "x": "http://arxiv.org/schemas/atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
}
# モジュール共通（全クエリで1つ）
_LIMITER = RateLimiter(3.0)
_ID_RE = re.compile(r"^(?P<id>.+?)(?:v(?P<v>\d+))?$")
PAGE_SIZE = 100


def _ws(s: str | None) -> str:
    return " ".join((s or "").split())


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def split_arxiv_url(id_url: str) -> tuple[str, int | None]:
    """'http://arxiv.org/abs/2609.02095v2' → ('2609.02095', 2)。旧形式 'cs/0301001v1' にも対応。"""
    tail = id_url.split("/abs/", 1)[1] if "/abs/" in id_url else id_url
    m = _ID_RE.match(tail.strip())
    assert m  # 常に一致する
    return m["id"], int(m["v"]) if m["v"] else None


def parse_arxiv_feed(xml_text: str, retrieved_at: datetime) -> list[PaperRecord]:
    root = ET.fromstring(xml_text)
    out: list[PaperRecord] = []
    for e in root.findall("a:entry", _NS):
        id_url = _ws(e.findtext("a:id", namespaces=_NS))
        if "/abs/" not in id_url:  # エラー応答のエントリなどは飛ばす
            continue
        arxiv_id, version = split_arxiv_url(id_url)
        title = _ws(e.findtext("a:title", namespaces=_NS))
        abstract = _ws(e.findtext("a:summary", namespaces=_NS)) or None
        authors = [_ws(n.text) for n in e.findall("a:author/a:name", _NS) if _ws(n.text)]
        published = _dt(e.findtext("a:published", namespaces=_NS))
        updated = _dt(e.findtext("a:updated", namespaces=_NS))
        doi = normalize_doi(e.findtext("x:doi", namespaces=_NS))
        journal_ref = _ws(e.findtext("x:journal_ref", namespaces=_NS)) or None
        cats = [c.get("term") for c in e.findall("a:category", _NS) if c.get("term")]
        pdf = None
        for ln in e.findall("a:link", _NS):
            if ln.get("title") == "pdf" or ln.get("type") == "application/pdf":
                pdf = ln.get("href")
        # journal_ref があれば出版情報は不明のまま venue に記録、どちらも無ければプレプリント
        status = "unknown" if (journal_ref or doi) else "preprint"
        out.append(
            PaperRecord(
                source="arxiv",
                source_id=arxiv_id,
                title=title,
                abstract=abstract,
                authors=authors,
                doi=doi,
                arxiv_id=arxiv_id,
                arxiv_version=version,
                published_at=published.date() if published else None,
                source_updated_at=updated,
                peer_review_status=status,
                venue=journal_ref,
                categories=cats,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=pdf,
                retrieved_at=retrieved_at,
                raw={"id": id_url, "journal_ref": journal_ref, "doi": doi},
            )
        )
    return out


def build_search_query(query: str | None, categories: list[str], since: date, until: date) -> str:
    parts = []
    if categories:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    if query:
        parts.append('all:"' + query.replace('"', " ") + '"')
    parts.append(f"submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]")
    return " AND ".join(parts)


async def search_arxiv(
    query: str | None,
    categories: list[str],
    since: date,
    until: date,
    max_results: int,
    client: httpx.AsyncClient | None = None,
    *,
    limiter: RateLimiter | None = None,
) -> list[PaperRecord]:
    own = client is None
    client = client or make_client(get_settings().contact_email)
    lim = limiter or _LIMITER
    search_query = build_search_query(query, categories, since, until)
    records: list[PaperRecord] = []
    try:
        start = 0
        while len(records) < max_results:
            n = min(PAGE_SIZE, max_results - len(records))
            resp = await get_with_retry(
                client,
                ARXIV_URL,
                {
                    "search_query": search_query,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "start": start,
                    "max_results": n,
                },
                limiter=lim,
            )
            page = parse_arxiv_feed(resp.text, datetime.now(UTC))
            records.extend(page)
            if len(page) < n:
                break
            start += n
    finally:
        if own:
            await client.aclose()
    return records[:max_results]
