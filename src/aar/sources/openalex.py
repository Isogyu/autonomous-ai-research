"""OpenAlex の works 検索。キーなしは日次枠が小さいので、枠切れは空リストと警告で返す。"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx

from aar.config import get_settings
from aar.models import PaperRecord
from aar.sources.base import RateLimiter, get_with_retry, make_client
from aar.sources.dedupe import normalize_doi

log = logging.getLogger(__name__)

OPENALEX_URL = "https://api.openalex.org/works"
_LIMITER = RateLimiter(0.2)
_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/((?:[a-z\-]+(?:\.[a-z]{2})?/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?", re.I
)


def short_id(url: str | None) -> str | None:
    """'https://openalex.org/W123' → 'W123'。"""
    return url.rsplit("/", 1)[-1] if url else None


def reconstruct_abstract(inv: dict[str, list[int]] | None) -> str | None:
    """abstract_inverted_index（語 → 位置の一覧）から要旨を復元する。"""
    if not inv:
        return None
    slots: dict[int, str] = {}
    for word, positions in inv.items():
        for p in positions:
            slots[p] = word
    return " ".join(slots[i] for i in sorted(slots)) or None


def _arxiv_id_from_locations(work: dict[str, Any]) -> str | None:
    locs = [work.get("primary_location"), *(work.get("locations") or [])]
    for loc in locs:
        for key in ("landing_page_url", "pdf_url"):
            m = _ARXIV_RE.search((loc or {}).get(key) or "")
            if m:
                return m.group(1)
    return None


def _peer_status(work: dict[str, Any], source_type: str | None) -> str:
    t = work.get("type")
    if t in ("preprint", "posted-content"):
        return "preprint"
    if t == "article" and source_type in ("journal", "conference"):
        return "peer_reviewed"
    return "unknown"


def parse_openalex_work(work: dict[str, Any], retrieved_at: datetime) -> PaperRecord:
    oa_id = short_id((work.get("ids") or {}).get("openalex") or work.get("id"))
    prim = work.get("primary_location") or {}
    src = prim.get("source") or {}
    updated = work.get("updated_date")
    try:
        updated_dt = datetime.fromisoformat(updated) if updated else None
    except ValueError:
        updated_dt = None
    pub = work.get("publication_date")
    refs = [f"openalex:{short_id(r)}" for r in (work.get("referenced_works") or [])[:50] if r]
    best_oa = work.get("best_oa_location") or {}
    doi = normalize_doi(work.get("doi") or (work.get("ids") or {}).get("doi"))
    return PaperRecord(
        source="openalex",
        source_id=oa_id or "",
        title=" ".join((work.get("title") or work.get("display_name") or "").split()),
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        authors=[
            a["author"]["display_name"]
            for a in work.get("authorships") or []
            if (a.get("author") or {}).get("display_name")
        ],
        doi=doi,
        arxiv_id=_arxiv_id_from_locations(work),
        openalex_id=oa_id,
        published_at=date.fromisoformat(pub) if pub else None,
        source_updated_at=updated_dt,
        peer_review_status=_peer_status(work, src.get("type")),  # type: ignore[arg-type]
        venue=src.get("display_name"),
        url=prim.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else None),
        pdf_url=prim.get("pdf_url") or best_oa.get("pdf_url"),
        license=prim.get("license"),
        citation_count=work.get("cited_by_count"),
        references=refs,
        retrieved_at=retrieved_at,
        raw={k: work.get(k) for k in ("id", "doi", "type", "publication_date", "cited_by_count")},
    )


async def search_openalex(
    query: str,
    since: date,
    max_results: int,
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
) -> list[PaperRecord]:
    own = client is None
    client = client or make_client(get_settings().contact_email)
    params: dict[str, Any] = {
        "search": query,
        "filter": f"from_publication_date:{since.isoformat()}",
        "per-page": min(max_results, 200),
        "sort": "publication_date:desc",
    }
    if api_key:
        params["api_key"] = api_key
    try:
        resp = await get_with_retry(client, OPENALEX_URL, params, limiter=_LIMITER, max_attempts=2)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (402, 403, 429):
            log.warning("OpenAlex の利用枠切れまたは制限（HTTP %d）。空で返します", e.response.status_code)
            return []
        raise
    finally:
        if own:
            await client.aclose()
    now = datetime.now(UTC)
    out = []
    for w in resp.json().get("results", [])[:max_results]:
        r = parse_openalex_work(w, now)
        if r.source_id and r.title:
            out.append(r)
    return out
