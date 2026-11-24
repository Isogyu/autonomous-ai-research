"""Semantic Scholar の batch API による補強（被引用数・参考文献・出版情報）。"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx

from aar.config import get_settings
from aar.models import PaperRecord
from aar.sources.base import RateLimiter, get_with_retry, make_client
from aar.sources.dedupe import normalize_doi

log = logging.getLogger(__name__)

S2_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
FIELDS = (
    "title,abstract,externalIds,citationCount,publicationVenue,publicationDate,"
    "openAccessPdf,authors,references.externalIds"
)
BATCH = 500


def parse_s2_paper(p: dict[str, Any], retrieved_at: datetime) -> PaperRecord:
    ext = p.get("externalIds") or {}
    venue = p.get("publicationVenue") or {}
    vtype = (venue.get("type") or "").lower()
    vname = venue.get("name")
    if vtype in ("journal", "conference"):
        status = "peer_reviewed"
    elif vtype == "repository" or (vname or "").lower().startswith("arxiv"):
        status = "preprint"
    else:
        status = "unknown"
    refs: list[str] = []
    for r in p.get("references") or []:
        rext = (r or {}).get("externalIds") or {}
        if rext.get("ArXiv"):
            refs.append(f"arxiv:{rext['ArXiv']}")
        elif rext.get("DOI"):
            refs.append(f"doi:{normalize_doi(rext['DOI'])}")
        if len(refs) >= 50:
            break
    pub = p.get("publicationDate")
    try:
        pub_d = date.fromisoformat(pub) if pub else None
    except ValueError:
        pub_d = None
    pid = p.get("paperId") or ""
    return PaperRecord(
        source="semantic_scholar",
        source_id=pid,
        title=" ".join((p.get("title") or "").split()),
        abstract=p.get("abstract"),
        authors=[a["name"] for a in p.get("authors") or [] if a.get("name")],
        doi=normalize_doi(ext.get("DOI")),
        arxiv_id=ext.get("ArXiv"),
        s2_id=pid or None,
        published_at=pub_d,
        peer_review_status=status,  # type: ignore[arg-type]
        venue=vname,
        url=f"https://www.semanticscholar.org/paper/{pid}" if pid else None,
        pdf_url=(p.get("openAccessPdf") or {}).get("url"),
        citation_count=p.get("citationCount"),
        references=refs,
        retrieved_at=retrieved_at,
        raw={k: p.get(k) for k in ("paperId", "externalIds", "citationCount", "publicationVenue")},
    )


async def enrich_semantic_scholar(
    ids: list[str], api_key: str = "", client: httpx.AsyncClient | None = None
) -> list[PaperRecord]:
    """ids は "ARXIV:2609.02095" / "DOI:10.x/y" 形式。失敗は空リスト＋警告（再試行のあと）。"""
    if not ids:
        return []
    own = client is None
    client = client or make_client(get_settings().contact_email)
    if api_key:
        client.headers["x-api-key"] = api_key
    limiter = RateLimiter(1.0 if api_key else 3.0)
    now = datetime.now(UTC)
    out: list[PaperRecord] = []
    try:
        for i in range(0, len(ids), BATCH):
            try:
                resp = await get_with_retry(
                    client, S2_URL, {"fields": FIELDS}, limiter=limiter, method="POST",
                    json={"ids": ids[i : i + BATCH]}, backoff_base=3.0,
                )
            except httpx.HTTPError as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                log.warning("Semantic Scholar の補強に失敗（HTTP %s）: %s", status, e)
                continue
            for p in resp.json():
                if p:  # 見つからなかった ID は null
                    r = parse_s2_paper(p, now)
                    if r.source_id and r.title:
                        out.append(r)
    finally:
        if own:
            await client.aclose()
    return out
