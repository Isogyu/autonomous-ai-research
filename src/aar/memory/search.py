"""研究記憶の検索。キーワード検索・ベクトル検索・メタデータの絞り込み・引用関係を組み合わせる。

順位の統合は Reciprocal Rank Fusion（順位だけを使うので、種類の違う点数を無理に足さない）。
ベクトルの近さは「関連しそうな記録を拾う」ためだけに使い、新規性や研究の正しさの判定には使わない。
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import psycopg

from aar.db.core import fetch_all, fetch_one
from aar.memory.embed import Embedder

KINDS = ["paper", "proposal", "hypothesis", "experiment", "finding", "lesson"]
RRF_K = 60


def index_pending(conn: psycopg.Connection, embedder: Embedder, *, limit: int = 500) -> int:
    """まだ埋め込みがない（または内容が変わった）記録の埋め込みを作る。作った件数を返す。"""
    rows = fetch_all(
        conn,
        """SELECT d.entity_type, d.entity_id, d.title, d.body FROM memory_documents d
           LEFT JOIN embeddings e ON e.entity_type = d.entity_type AND e.entity_id = d.entity_id AND e.model = %s
           WHERE e.id IS NULL OR e.content_sha256 <> encode(sha256(convert_to(d.title || E'\n' || d.body, 'UTF8')), 'hex')
           ORDER BY d.created_at DESC LIMIT %s""",
        (embedder.name, limit))
    for i in range(0, len(rows), 32):
        batch = rows[i:i + 32]
        texts = [f"{r['title']}\n{r['body']}" for r in batch]
        for r, text, vec in zip(batch, texts, embedder.embed(texts), strict=True):
            conn.execute(
                """INSERT INTO embeddings (entity_type, entity_id, model, content_sha256, embedding)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (entity_type, entity_id, model) DO UPDATE
                   SET content_sha256 = EXCLUDED.content_sha256, embedding = EXCLUDED.embedding, created_at = now()""",
                (r["entity_type"], r["entity_id"], embedder.name, hashlib.sha256(text.encode()).hexdigest(),
                 np.asarray(vec, dtype=np.float32)))
    return len(rows)


def hybrid_search(conn: psycopg.Connection, embedder: Embedder, query: str, *, kinds: list[str] | None = None,
                  project_id: str | None = None, exclude_project: str | None = None, limit: int = 20,
                  related_to_paper: str | None = None) -> list[dict[str, Any]]:
    """ハイブリッド検索。各結果に、どの手がかりで拾われたか（keyword / vector / citation の順位）を付ける。"""
    query = query.strip()
    kinds = [k for k in (kinds or KINDS) if k in KINDS] or KINDS
    filt = "d.entity_type = ANY(%(kinds)s)"
    params: dict[str, Any] = {"kinds": kinds, "q": query, "like": f"%{query}%", "n": max(limit * 3, 30)}
    if project_id:
        filt += " AND d.project_id = %(pid)s"
        params["pid"] = project_id
    if exclude_project:
        filt += " AND d.project_id IS DISTINCT FROM %(xpid)s"
        params["xpid"] = exclude_project
    ranks: dict[tuple[str, str], dict[str, int]] = {}

    if query:
        # 1) キーワード: 英語は全文検索、日本語など空白で区切られない語は部分一致で拾う
        kw = fetch_all(conn, f"""
            SELECT d.entity_type, d.entity_id,
                   ts_rank_cd(d.tsv, websearch_to_tsquery('simple', %(q)s)) AS r,
                   (d.title ILIKE %(like)s)::int AS in_title
            FROM memory_documents d
            WHERE {filt} AND (d.tsv @@ websearch_to_tsquery('simple', %(q)s) OR d.title ILIKE %(like)s OR d.body ILIKE %(like)s)
            ORDER BY in_title DESC, r DESC LIMIT %(n)s""", params)  # noqa: S608
        for i, r in enumerate(kw, 1):
            ranks.setdefault((r["entity_type"], r["entity_id"]), {})["keyword"] = i
        # 2) ベクトル: 近さの順位だけを使う。絞り込みで件数が不足しないよう、索引の探索を続けさせる
        qv = np.asarray(embedder.embed([query], is_query=True)[0], dtype=np.float32)
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        vec = fetch_all(conn, f"""
            SELECT e.entity_type, e.entity_id FROM embeddings e
            JOIN memory_documents d ON d.entity_type = e.entity_type AND d.entity_id = e.entity_id
            WHERE e.model = %(model)s AND {filt}
            ORDER BY e.embedding <=> %(qv)s LIMIT %(n)s""", params | {"model": embedder.name, "qv": qv})  # noqa: S608
        for i, r in enumerate(vec, 1):
            ranks.setdefault((r["entity_type"], r["entity_id"]), {})["vector"] = i

    # 3) 引用関係: 指定した論文が引用している／されている論文
    if related_to_paper and "paper" in kinds:
        cit = fetch_all(conn, """
            SELECT cited_paper_id::text AS id FROM citations WHERE citing_paper_id = %(p)s AND cited_paper_id IS NOT NULL
            UNION SELECT citing_paper_id::text FROM citations WHERE cited_paper_id = %(p)s""", {"p": related_to_paper})
        for i, r in enumerate(cit, 1):
            ranks.setdefault(("paper", r["id"]), {})["citation"] = i

    scored = sorted(((sum(1.0 / (RRF_K + rank) for rank in sig.values()), key, sig) for key, sig in ranks.items()),
                    key=lambda t: -t[0])[:limit]
    out: list[dict[str, Any]] = []
    for score, (etype, eid), sig in scored:
        d = fetch_one(conn, "SELECT title, body, project_id, origin, created_at FROM memory_documents "
                            "WHERE entity_type = %s AND entity_id = %s", (etype, eid))
        if d:
            out.append({"entity_type": etype, "entity_id": eid, "title": d["title"], "snippet": d["body"][:280],
                        "score": round(score, 5), "project_id": d["project_id"], "origin": d["origin"],
                        "signals": sig, "embedding_model": embedder.name, "semantic": embedder.semantic})
    return out


# ------------------------------------------------------------------ 研究での再利用


def memory_context(conn: psycopg.Connection, embedder: Embedder, *, project_id: str, title: str, hypotheses: list[str],
                   method: str, paper_id: str | None) -> str:
    """新しい研究の設計に渡す、過去の記憶の要約を作る。該当がなければ空文字。"""
    sections: list[tuple[str, list[str]]] = []

    def lines(rows: list[dict[str, Any]]) -> list[str]:
        return [f"- ({r['entity_type']}{', 研究 ' + r['project_id'] if r['project_id'] else ''}) {r['title']}: "
                f"{r['snippet'][:200]}" for r in rows]

    similar = hybrid_search(conn, embedder, title, kinds=["proposal", "paper"], exclude_project=project_id, limit=4,
                            related_to_paper=paper_id)
    similar = [r for r in similar if not (r["entity_type"] == "paper" and r["entity_id"] == paper_id)
               and (r["entity_type"] != "proposal" or r["project_id"])]
    if similar:
        sections.append(("この論文・企画に近い過去の研究や関連論文", lines(similar)))
    tested: list[dict[str, Any]] = []
    for h in hypotheses[:3]:
        tested += hybrid_search(conn, embedder, h, kinds=["hypothesis"], exclude_project=project_id, limit=3)
    if tested:
        uniq = {r["entity_id"]: r for r in tested}
        sections.append(("以前に検証した近い仮説（判定つき）", lines(list(uniq.values())[:5])))
    failures = fetch_all(conn, """
        SELECT 'experiment' AS entity_type, e.project_id, e.name AS title, coalesce(e.design, '') AS snippet
        FROM experiments e WHERE e.status = 'failed' AND e.project_id <> %s ORDER BY e.started_at DESC LIMIT 3""", (project_id,))
    fail_lessons = hybrid_search(conn, embedder, method or title, kinds=["lesson", "finding"], exclude_project=project_id, limit=5)
    if failures or fail_lessons:
        sections.append(("同じような評価方法での失敗や監査の指摘、学び", lines(failures) + lines(fail_lessons)))
    designs = fetch_all(conn, """
        SELECT 'experiment' AS entity_type, e.project_id, e.name AS title, coalesce(e.design, '') AS snippet
        FROM experiments e JOIN research_projects p ON p.id = e.project_id
        WHERE p.outcome IN ('publishable', 'publishable_with_revisions') AND e.kind = 'main' AND e.project_id <> %s
        ORDER BY e.finished_at DESC NULLS LAST LIMIT 3""", (project_id,))
    if designs:
        sections.append(("監査を通過した研究の実験設計", lines(designs)))
    code = fetch_all(conn, """
        SELECT 'artifact' AS entity_type, a.project_id, a.path AS title, coalesce(a.description, '') AS snippet
        FROM research_artifacts a WHERE a.reusable AND a.project_id <> %s ORDER BY a.created_at DESC LIMIT 5""", (project_id,))
    if code:
        sections.append(("再利用できる評価コード（過去の研究の作業領域にある）", lines(code)))
    if not sections:
        return ""
    return "\n\n".join(f"### {t}\n" + "\n".join(ls) for t, ls in sections)


def untested_questions(conn: psycopg.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """まだ検証されていない課題: 判定保留・未検証の仮説と、企画に書かれた未解決の問い（未選択のもの）。"""
    return fetch_all(conn, """
        SELECT * FROM (
          SELECT 'hypothesis' AS source, h.project_id, h.statement AS question, h.status AS state, h.created_at
            FROM hypotheses h WHERE h.status IN ('untested', 'inconclusive')
          UNION ALL
          SELECT 'proposal', NULL, r.title || ': ' || coalesce(r.body->>'unknown', ''), r.status, r.created_at
            FROM research_proposals r WHERE r.status IN ('proposed', 'expired', 'dismissed')
          UNION ALL
          SELECT 'lesson', l.project_id, l.content, l.category, l.created_at
            FROM lessons_learned l WHERE l.category = 'open_question'
        ) q ORDER BY created_at DESC LIMIT %s""", (limit,))
