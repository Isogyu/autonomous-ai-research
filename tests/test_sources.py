"""論文収集モジュールの単体テスト（ネットワーク不使用。DB テストは AAR_TEST_DATABASE_URL が必要）。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from aar.models import PaperRecord
from aar.sources import base
from aar.sources.arxiv import build_search_query, parse_arxiv_feed, search_arxiv
from aar.sources.base import RateLimiter, get_with_retry, user_agent
from aar.sources.dedupe import (
    arxiv_id_from_doi,
    merge_records,
    normalize_author,
    normalize_doi,
    normalize_title,
    same_paper,
)
from aar.sources.openalex import parse_openalex_work, reconstruct_abstract, search_openalex
from aar.sources.semantic_scholar import enrich_semantic_scholar, parse_s2_paper

NOW = datetime(2026, 11, 23, tzinfo=UTC)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2609.02095v2</id>
    <updated>2026-09-05T10:00:00Z</updated>
    <published>2026-09-02T08:00:00Z</published>
    <title>Agents   that
      Learn</title>
    <summary>  An abstract
      here. </summary>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Lee</name></author>
    <arxiv:doi>10.1000/ABC.1</arxiv:doi>
    <arxiv:journal_ref>Journal of X 3 (2026)</arxiv:journal_ref>
    <link href="http://arxiv.org/abs/2609.02095v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2609.02095v2" rel="related" type="application/pdf"/>
    <category term="cs.AI"/><category term="cs.CL"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/cs/0301001v1</id>
    <updated>2003-01-02T00:00:00Z</updated>
    <published>2003-01-01T00:00:00Z</published>
    <title>Old paper</title>
    <summary>Old.</summary>
    <author><name>Carol Old</name></author>
    <category term="cs.AI"/>
  </entry>
  <entry>
    <id>http://arxiv.org/api/errors#x</id>
    <title>Error</title>
  </entry>
</feed>"""


def rec(**kw) -> PaperRecord:
    d = {"source": "arxiv", "source_id": "x", "title": "T", "retrieved_at": NOW}
    d.update(kw)
    return PaperRecord(**d)


# ---------------------------------------------------------------- arXiv


def test_parse_arxiv_feed():
    rs = parse_arxiv_feed(FEED, NOW)
    assert len(rs) == 2  # エラーのエントリは飛ばす
    a, old = rs
    assert (a.arxiv_id, a.arxiv_version, a.source_id) == ("2609.02095", 2, "2609.02095")
    assert a.title == "Agents that Learn"
    assert a.abstract == "An abstract here."
    assert a.authors == ["Alice Smith", "Bob Lee"]
    assert a.published_at == date(2026, 9, 2)
    assert a.source_updated_at == datetime(2026, 9, 5, 10, tzinfo=UTC)
    assert a.doi == "10.1000/abc.1"
    assert a.venue == "Journal of X 3 (2026)" and a.peer_review_status == "unknown"
    assert a.categories == ["cs.AI", "cs.CL"]
    assert a.pdf_url == "http://arxiv.org/pdf/2609.02095v2"
    assert (old.arxiv_id, old.arxiv_version) == ("cs/0301001", 1)
    assert old.peer_review_status == "preprint" and old.venue is None


def test_arxiv_query_and_search():
    q = build_search_query("LLM agent", ["cs.AI", "cs.CL"], date(2026, 11, 20), date(2026, 11, 23))
    assert q == '(cat:cs.AI OR cat:cs.CL) AND all:"LLM agent" AND submittedDate:[202611200000 TO 202611232359]'
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params)
        return httpx.Response(200, text=FEED)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await search_arxiv(
                "x", ["cs.AI"], date(2026, 11, 20), date(2026, 11, 23), 10, c, limiter=RateLimiter(0)
            )

    import asyncio

    rs = asyncio.run(run())
    assert len(rs) == 2 and len(seen) == 1  # 件数が要求より少ないので1ページで終わる
    assert seen[0]["sortBy"] == "submittedDate"


# ---------------------------------------------------------------- OpenAlex


WORK = {
    "id": "https://openalex.org/W123",
    "ids": {"openalex": "https://openalex.org/W123", "doi": "https://doi.org/10.1000/ABC.1"},
    "doi": "https://doi.org/10.1000/ABC.1",
    "title": "Agents that Learn",
    "type": "article",
    "publication_date": "2026-09-02",
    "updated_date": "2026-10-01T00:00:00.000000",
    "cited_by_count": 7,
    "abstract_inverted_index": {"An": [0], "abstract": [1], "here.": [2]},
    "authorships": [{"author": {"display_name": "Alice Smith"}}],
    "primary_location": {
        "landing_page_url": "https://doi.org/10.1000/abc.1",
        "source": {"display_name": "Journal of X", "type": "journal"},
    },
    "locations": [{"landing_page_url": "https://arxiv.org/abs/2609.02095v2"}],
    "referenced_works": [f"https://openalex.org/W{i}" for i in range(80)],
}


def test_openalex_abstract_and_parse():
    assert reconstruct_abstract({"b": [1], "a": [0, 2]}) == "a b a"
    assert reconstruct_abstract(None) is None
    r = parse_openalex_work(WORK, NOW)
    assert r.openalex_id == "W123" and r.source_id == "W123"
    assert r.doi == "10.1000/abc.1"
    assert r.abstract == "An abstract here."
    assert r.venue == "Journal of X" and r.peer_review_status == "peer_reviewed"
    assert r.arxiv_id == "2609.02095"
    assert r.citation_count == 7
    assert len(r.references) == 50 and r.references[0] == "openalex:W0"
    assert parse_openalex_work({**WORK, "type": "preprint"}, NOW).peer_review_status == "preprint"
    assert parse_openalex_work({**WORK, "type": "book"}, NOW).peer_review_status == "unknown"


async def test_openalex_quota_returns_empty():
    transport = httpx.MockTransport(lambda req: httpx.Response(429, headers={"Retry-After": "0"}))
    async with httpx.AsyncClient(transport=transport) as c:
        assert await search_openalex("x", date(2026, 11, 1), 5, client=c) == []


# ---------------------------------------------------------------- Semantic Scholar


S2P = {
    "paperId": "abc123",
    "title": "Agents that Learn",
    "abstract": "An abstract here.",
    "externalIds": {"ArXiv": "2609.02095", "DOI": "10.1000/ABC.1"},
    "citationCount": 12,
    "publicationVenue": {"name": "NeurIPS", "type": "conference"},
    "publicationDate": "2026-09-02",
    "openAccessPdf": {"url": "https://example.org/a.pdf"},
    "authors": [{"name": "Alice Smith"}],
    "references": [
        {"externalIds": {"ArXiv": "2401.00001"}},
        {"externalIds": {"DOI": "10.1/X"}},
        {"externalIds": None},
    ],
}


def test_parse_s2():
    r = parse_s2_paper(S2P, NOW)
    assert r.s2_id == "abc123" and r.arxiv_id == "2609.02095" and r.doi == "10.1000/abc.1"
    assert r.peer_review_status == "peer_reviewed" and r.citation_count == 12
    assert r.references == ["arxiv:2401.00001", "doi:10.1/x"]
    assert r.published_at == date(2026, 9, 2)


async def test_s2_enrich_skips_null_and_failure(monkeypatch):
    monkeypatch.setattr(base.asyncio, "sleep", _nosleep)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=[S2P, None])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        rs = await enrich_semantic_scholar(["ARXIV:2609.02095", "ARXIV:0000.0000"], client=c)
        assert len(rs) == 1 and len(calls) == 1
    fail = httpx.MockTransport(lambda req: httpx.Response(429))
    async with httpx.AsyncClient(transport=fail) as c:
        assert await enrich_semantic_scholar(["ARXIV:1"], client=c) == []


# ---------------------------------------------------------------- dedupe


def test_normalizers():
    assert normalize_title(r"Learning $\alpha$-Agents:  A \textbf{Study}!") == "learning agents a study"
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_author("Smith, John") == normalize_author("J. Smith") == "smith j"
    assert arxiv_id_from_doi("10.48550/arXiv.2609.02095") == "2609.02095"
    assert arxiv_id_from_doi("10.1000/x") is None


def test_same_paper_rules():
    # DOI 一致
    assert same_paper(rec(title="A", doi="10.1/x"), rec(title="B", doi="https://doi.org/10.1/X"))
    # arXiv の DOI から ID を導出
    assert same_paper(rec(title="A", arxiv_id="2609.02095"), rec(title="B", doi="10.48550/arxiv.2609.02095"))
    # 別 DOI でも arXiv ID が同じなら同一（プレプリントと出版版）
    assert same_paper(
        rec(title="A", arxiv_id="2609.02095", doi="10.1/pre"), rec(title="B", arxiv_id="2609.02095", doi="10.2/pub")
    )
    # 題名ゆれ（完全一致）
    assert same_paper(
        rec(title="Agents: A Study", authors=["Alice Smith"]),
        rec(title="agents - a study.", authors=["A. Smith"]),
    )
    # 片方が著者不明なら題名だけで一致
    assert same_paper(rec(title="Agents Study"), rec(title="Agents study", authors=["Alice Smith"]))
    # 類似題名＋筆頭著者一致＋年差1以内
    long = "Evaluating large language model agents on realistic software engineering tasks"
    a = rec(title=long, authors=["Alice Smith"], published_at=date(2026, 1, 1))
    b = rec(title=long + "s", authors=["Alice Smith"], published_at=date(2026, 12, 1))
    assert same_paper(a, b)
    assert not same_paper(a, b.model_copy(update={"published_at": date(2028, 1, 1)}))
    # 題名が同じでも筆頭著者が違えば別
    assert not same_paper(rec(title="Agents Study", authors=["Alice Smith"]), rec(title="Agents Study", authors=["Bob Lee"]))
    assert not same_paper(a, b.model_copy(update={"authors": ["Bob Lee"]}))
    # 別 DOI・arXiv ID 不明で題名が違えば別
    assert not same_paper(rec(title="A", doi="10.1/a"), rec(title="B", doi="10.1/b"))


def test_merge_records_groups():
    a = rec(title="Agents Study", arxiv_id="2609.02095", authors=["Alice Smith"])
    b = rec(source="openalex", title="Other title", doi="10.48550/arxiv.2609.02095")
    c = rec(title="Agents Study", authors=["Alice Smith"], doi="10.1/pub")  # a と題名・著者で一致
    d = rec(title="Unrelated", authors=["Zed Q"])
    groups = merge_records([a, b, c, d])
    assert sorted(len(g) for g in groups) == [1, 3]


# ---------------------------------------------------------------- 再試行


async def _nosleep(_):
    return None


async def test_retry_429_then_200_with_retry_after(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(base.asyncio, "sleep", fake_sleep)
    seq = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503), httpx.Response(200, text="ok")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(seq))) as c:
        resp = await get_with_retry(c, "https://x.test/", {}, limiter=RateLimiter(0))
    assert resp.text == "ok"
    assert delays == [7.0, 2.0]  # Retry-After を尊重、次は指数バックオフ


async def test_retry_4xx_immediate_and_exhaustion(monkeypatch):
    monkeypatch.setattr(base.asyncio, "sleep", _nosleep)
    n = {"c": 0}

    def h404(req):
        n["c"] += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(h404)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(c, "https://x.test/", {})
    assert n["c"] == 1

    n["c"] = 0

    def h500(req):
        n["c"] += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(h500)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(c, "https://x.test/", {}, max_attempts=3)
    assert n["c"] == 3


async def test_retry_network_error(monkeypatch):
    monkeypatch.setattr(base.asyncio, "sleep", _nosleep)
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] < 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        assert (await get_with_retry(c, "https://x.test/", {})).text == "ok"


def test_user_agent():
    assert user_agent() == "autonomous-ai-research/0.1"
    assert user_agent("a@b.c") == "autonomous-ai-research/0.1 (mailto:a@b.c)"


async def test_rate_limiter_spacing(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(base.asyncio, "sleep", fake_sleep)
    lim = RateLimiter(3.0)
    await lim.wait()
    await lim.wait()
    assert len(delays) == 1 and 0 < delays[0] <= 3.0


# ---------------------------------------------------------------- DB


def _arxiv_rec(**kw):
    d = dict(
        source="arxiv", source_id="2609.02095", title="Agents that Learn", abstract="Short.",
        authors=["Alice Smith", "Bob Lee"], arxiv_id="2609.02095", arxiv_version=1,
        published_at=date(2026, 9, 2), source_updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        peer_review_status="preprint", categories=["cs.AI"], references=["arxiv:2401.00001"],
    )
    d.update(kw)
    return rec(**d)


@pytest.mark.db
def test_upsert_idempotent_and_versions(db_url):
    from aar.db.core import connect
    from aar.sources.repository import upsert_papers

    with connect(db_url) as conn:
        r1 = _arxiv_rec()
        assert upsert_papers(conn, [r1]) == {"inserted": 1, "updated": 0, "merged": 0, "failed": 0}
        assert upsert_papers(conn, [r1]) == {"inserted": 0, "updated": 1, "merged": 0, "failed": 0}
        assert conn.execute("SELECT count(*) AS n FROM papers").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM paper_authors").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM paper_versions").fetchone()["n"] == 1
        # v2: 版が増え、要旨は長い方、更新日時は新しい方
        r2 = _arxiv_rec(
            arxiv_version=2, abstract="A longer abstract.", source_updated_at=datetime(2026, 9, 9, tzinfo=UTC)
        )
        assert upsert_papers(conn, [r2])["updated"] == 1
        row = conn.execute("SELECT * FROM papers").fetchone()
        assert row["arxiv_version"] == 2 and row["abstract"] == "A longer abstract."
        assert row["source_updated_at"] == datetime(2026, 9, 9, tzinfo=UTC)
        assert conn.execute("SELECT count(*) AS n FROM paper_versions").fetchone()["n"] == 2
        # 古い版が後から来ても版は戻らない
        upsert_papers(conn, [r1])
        assert conn.execute("SELECT arxiv_version FROM papers").fetchone()["arxiv_version"] == 2


@pytest.mark.db
def test_upsert_merge_arxiv_and_openalex(db_url):
    from aar.db.core import connect
    from aar.sources.repository import upsert_papers

    oa = parse_openalex_work(WORK, NOW)  # arXiv ID と DOI を持つ査読済み版
    with connect(db_url) as conn:
        upsert_papers(conn, [_arxiv_rec()])
        stats = upsert_papers(conn, [oa])
        assert stats["merged"] == 1 and stats["inserted"] == 0
        rows = conn.execute("SELECT * FROM papers").fetchall()
        assert len(rows) == 1
        p = rows[0]
        assert p["openalex_id"] == "W123" and p["doi"] == "10.1000/abc.1" and p["arxiv_id"] == "2609.02095"
        assert p["peer_review_status"] == "peer_reviewed" and p["citation_count"] == 7
        assert p["venue"] == "Journal of X"
        assert conn.execute("SELECT count(*) AS n FROM paper_sources").fetchone()["n"] == 2
        # S2（DOI・arXiv ID 一致）も同じ行へ
        assert upsert_papers(conn, [parse_s2_paper(S2P, NOW)])["merged"] == 1
        assert conn.execute("SELECT count(*) AS n FROM papers").fetchone()["n"] == 1
        # 著者は重複して作られない
        assert conn.execute("SELECT count(*) AS n FROM authors").fetchone()["n"] == 2


@pytest.mark.db
def test_upsert_title_match_and_conflict_isolated(db_url):
    from aar.db.core import connect
    from aar.sources.repository import upsert_papers

    with connect(db_url) as conn:
        upsert_papers(conn, [rec(source="openalex", source_id="W1", openalex_id="W1", title="Same Title",
                                 authors=["Alice Smith"])])
        # ID 無し・題名＋筆頭著者で同じ行
        s = upsert_papers(conn, [rec(source="semantic_scholar", source_id="s1", title="same title!",
                                     authors=["A. Smith"], s2_id="s1")])
        assert s["merged"] == 1
        # 筆頭著者が違えば別行
        s = upsert_papers(conn, [rec(source="semantic_scholar", source_id="s2", title="Same Title",
                                     authors=["Bob Lee"], s2_id="s2")])
        assert s["inserted"] == 1
        assert conn.execute("SELECT count(*) AS n FROM papers").fetchone()["n"] == 2


@pytest.mark.db
def test_upsert_citations_backfill(db_url):
    from aar.db.core import connect
    from aar.sources.repository import upsert_papers

    with connect(db_url) as conn:
        upsert_papers(conn, [_arxiv_rec(references=["arxiv:2401.00001", "doi:10.9/z"])])
        assert conn.execute("SELECT count(*) AS n FROM citations WHERE cited_paper_id IS NULL").fetchone()["n"] == 2
        # 引用先が後から入ると cited_paper_id が埋まる
        upsert_papers(conn, [rec(source_id="2401.00001", arxiv_id="2401.00001", title="Cited", authors=["C D"])])
        row = conn.execute(
            "SELECT p.arxiv_id FROM citations c JOIN papers p ON p.id = c.cited_paper_id"
        ).fetchall()
        assert [r["arxiv_id"] for r in row] == ["2401.00001"]
        # 引用元が後から入る場合はその場で埋まる
        upsert_papers(conn, [rec(source_id="2609.99999", arxiv_id="2609.99999", title="Citing", authors=["E F"],
                                 references=["arxiv:2401.00001"])])
        assert conn.execute("SELECT count(*) AS n FROM citations WHERE cited_paper_id IS NOT NULL").fetchone()["n"] == 2
