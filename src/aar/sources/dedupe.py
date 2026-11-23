"""論文の重複判定。DOI → arXiv ID → 題名＋筆頭著者の順で、埋め込みは使わない。"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from aar.models import PaperRecord

_ARXIV_DOI_PREFIX = "10.48550/arxiv."
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\*?")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_title(t: str | None) -> str:
    """NFKC、小文字化、LaTeX・記号の除去、空白の正規化。"""
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t).lower()
    t = _LATEX_CMD.sub(" ", t)
    return _NON_WORD.sub(" ", t).strip()


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)
    return d or None


def is_arxiv_doi(doi: str | None) -> bool:
    return bool(doi and doi.startswith(_ARXIV_DOI_PREFIX))


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """arXiv の DOI（10.48550/arxiv.XXXX）から arXiv ID（バージョンなし）を導く。"""
    d = normalize_doi(doi)
    if not is_arxiv_doi(d):
        return None
    return re.sub(r"v\d+$", "", d[len(_ARXIV_DOI_PREFIX):]) or None  # type: ignore[index]


def effective_arxiv_id(r: PaperRecord) -> str | None:
    return (r.arxiv_id.lower() if r.arxiv_id else None) or arxiv_id_from_doi(r.doi)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_author_full(name: str | None) -> str:
    """氏名全体の正規化（authors テーブルの照合用）。"姓, 名" は "名 姓" の語順に直す。"""
    if not name:
        return ""
    n = unicodedata.normalize("NFKC", name)
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first} {last}"
    return _NON_WORD.sub(" ", _strip_accents(n).lower()).strip()


def normalize_author(name: str | None) -> str:
    """粗い正規化: 姓＋名の頭文字（例 "Smith, John" と "J. Smith" はどちらも "smith j"）。"""
    full = normalize_author_full(name)
    toks = full.split()
    if not toks:
        return ""
    if len(toks) == 1:
        return toks[0]
    return f"{toks[-1]} {toks[0][0]}"


def _first_author(r: PaperRecord) -> str:
    return normalize_author(r.authors[0]) if r.authors else ""


def same_paper(a: PaperRecord, b: PaperRecord) -> bool:
    # (1) DOI 一致（arXiv の DOI は (2) で扱う）
    da, db = normalize_doi(a.doi), normalize_doi(b.doi)
    if da and db and da == db:
        return True
    # (2) arXiv ID 一致。DOI が別でも同一（プレプリントと出版版）
    xa, xb = effective_arxiv_id(a), effective_arxiv_id(b)
    if xa and xb and xa == xb:
        return True
    # (3) 題名の完全一致＋筆頭著者
    ta, tb = normalize_title(a.title), normalize_title(b.title)
    if not ta or not tb:
        return False
    fa, fb = _first_author(a), _first_author(b)
    if ta == tb and (not fa or not fb or fa == fb):
        return True
    # (4) 題名の類似＋筆頭著者一致＋発行年の差が1以内
    if fa and fa == fb and a.published_at and b.published_at:
        if abs(a.published_at.year - b.published_at.year) <= 1:
            return SequenceMatcher(None, ta, tb).ratio() >= 0.95
    return False


def merge_records(records: list[PaperRecord]) -> list[list[PaperRecord]]:
    """同一論文をグループ化する（Union-Find）。入力順を保つ。"""
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if find(i) != find(j) and same_paper(records[i], records[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[PaperRecord]] = {}
    for i, r in enumerate(records):
        groups.setdefault(find(i), []).append(r)
    return list(groups.values())
