"""論文の本文の取得。

取得元は、arXiv の HTML 版（https://arxiv.org/html/<id>）を優先し、無ければ PDF（arXiv、または
情報源が示すオープンアクセスの PDF）。本文は分析のために読むだけで、再配布しない。
  * 保存先は var/papers/（Git の対象外）。DB には取得元・取得日時・ハッシュ・文字数だけを残す。
  * 研究のリポジトリ（公開候補）にも、監査記録の写しにも、本文は入れない。
  * arXiv へのアクセスは3秒に1回以下にする。
  * 本文は外部由来のデータで、エージェントに渡すときは必ず <untrusted_data> で囲む。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import io
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from aar.sources.base import RateLimiter, make_client

log = logging.getLogger(__name__)
_ARXIV_SITE_LIMITER = RateLimiter(3.0)
MAX_BYTES = 40_000_000
MAX_CHARS = 600_000
MIN_CHARS = 3_000     # これより短いものは本文として扱わない（目次だけ、エラーページなど）


@dataclass
class FullText:
    text: str
    url: str
    kind: str            # arxiv_html / arxiv_pdf / oa_pdf
    retrieved_at: dt.datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class _TextExtractor(HTMLParser):
    """HTML から読む順の文章を取り出す。数式は代替テキスト（LaTeX）を使い、ナビゲーションや脚注の装飾は捨てる。"""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "button", "form", "annotation-xml"}
    BLOCK = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "figcaption",
             "table", "blockquote", "pre", "dt", "dd"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._math = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
            return
        if tag == "math":
            self._math += 1
            alt = dict(attrs).get("alttext")
            if alt and not self._skip:
                self.parts.append(f" ${alt}$ ")
            return
        if tag in self.BLOCK and not self._skip:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3", "h4") and not self._skip:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        if tag in ("td", "th") and not self._skip:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(self._skip - 1, 0)
        elif tag == "math":
            self._math = max(self._math - 1, 0)
        elif tag in self.BLOCK and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and not self._math:
            self.parts.append(data)


def html_to_text(page: str) -> str:
    """論文の HTML を文章にする。"""
    # 本文の領域があればそこだけを使う（arXiv の HTML 版は <article> に本文が入る）
    m = re.search(r"<article\b.*?</article>", page, flags=re.S | re.I)
    parser = _TextExtractor()
    parser.feed(m.group(0) if m else page)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_CHARS]


def pdf_to_text(data: bytes, max_pages: int = 80) -> str:
    """PDF を文章にする。pypdf が入っていなければ空文字（pip install '.[fulltext]'）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            pages.append(page.extract_text() or "")
    except Exception as e:  # noqa: BLE001 - 壊れた PDF や未対応の形式は、本文なしとして扱う
        log.warning("PDF を読めない: %s", str(e)[:200])
        return ""
    text = "\n\n".join(pages)
    text = re.sub(r"-\n(?=[a-z])", "", text)          # 行末のハイフンでの分割をつなぐ
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_CHARS]


async def _get(client: httpx.AsyncClient, url: str, *, limiter: RateLimiter | None) -> tuple[bytes, str] | None:
    """大きさの上限つきで取得する。(内容, Content-Type) を返す。200 以外や失敗は None。"""
    if limiter:
        await limiter.wait()
    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    log.warning("本文が大きすぎるので取得をやめる: %s", url)
                    return None
                chunks.append(chunk)
            return b"".join(chunks), r.headers.get("content-type", "")
    except httpx.HTTPError as e:
        log.warning("本文を取得できない: %s（%s）", url, type(e).__name__)
        return None


async def fetch_fulltext(*, arxiv_id: str | None, arxiv_version: int | None = None, pdf_url: str | None = None,
                         contact_email: str = "", client: httpx.AsyncClient | None = None) -> FullText | None:
    """本文を取得する。取得できなければ None。"""
    own = client is None
    client = client or make_client(contact_email, timeout=60)
    now = dt.datetime.now(dt.UTC)
    try:
        if arxiv_id:
            vid = f"{arxiv_id}v{arxiv_version}" if arxiv_version else arxiv_id
            for url in dict.fromkeys((f"https://arxiv.org/html/{vid}", f"https://arxiv.org/html/{arxiv_id}")):
                r = await _get(client, url, limiter=_ARXIV_SITE_LIMITER)
                if r is not None and "html" in r[1]:
                    text = html_to_text(r[0].decode("utf-8", errors="replace"))
                    if len(text) >= MIN_CHARS:
                        return FullText(text=text, url=url, kind="arxiv_html", retrieved_at=now)
            url = f"https://arxiv.org/pdf/{vid}"
            r = await _get(client, url, limiter=_ARXIV_SITE_LIMITER)
            if r is not None:
                text = await asyncio.to_thread(pdf_to_text, r[0])
                if len(text) >= MIN_CHARS:
                    return FullText(text=text, url=url, kind="arxiv_pdf", retrieved_at=now)
        if pdf_url and pdf_url.startswith("https://"):
            r = await _get(client, pdf_url, limiter=_ARXIV_SITE_LIMITER if "arxiv.org" in pdf_url else None)
            if r is not None and (r[0][:5] == b"%PDF-" or "pdf" in r[1]):
                text = await asyncio.to_thread(pdf_to_text, r[0])
                if len(text) >= MIN_CHARS:
                    return FullText(text=text, url=pdf_url, kind="oa_pdf", retrieved_at=now)
        return None
    finally:
        if own:
            await client.aclose()


def fulltext_path(data_dir: Path, paper_id: str) -> Path:
    return data_dir / "papers" / f"{paper_id}.txt"


async def ensure_fulltext(conn: Any, data_dir: Path, paper: dict[str, Any], contact_email: str = "") -> str | None:
    """論文の本文を用意する。取得済みならそれを読み、無ければ取得して保存する。本文（無ければ None）を返す。

    conn は同期の psycopg 接続。取得できなかった場合は fulltext_status を unavailable にし、何度も取りに行かない。
    """
    path = fulltext_path(data_dir, str(paper["id"]))
    if paper.get("fulltext_status") == "fulltext_read" and path.exists():
        return path.read_text(encoding="utf-8")
    if paper.get("fulltext_status") == "unavailable":
        return None
    ft = await fetch_fulltext(arxiv_id=paper.get("arxiv_id"), arxiv_version=paper.get("arxiv_version"),
                              pdf_url=paper.get("pdf_url"), contact_email=contact_email)
    if ft is None:
        conn.execute("UPDATE papers SET fulltext_status = 'unavailable', fulltext_retrieved_at = now() WHERE id = %s", (paper["id"],))
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ft.text, encoding="utf-8")
    conn.execute(
        """UPDATE papers SET fulltext_status = 'fulltext_read', fulltext_url = %s, fulltext_sha256 = %s,
                             fulltext_retrieved_at = %s, fulltext_chars = %s WHERE id = %s""",
        (ft.url, ft.sha256, ft.retrieved_at, len(ft.text), paper["id"]))
    return ft.text


def excerpt_for_prompt(text: str, limit: int = 90_000) -> tuple[str, bool]:
    """エージェントに渡す長さに収める。切った場合は、冒頭と末尾（結論・付録の前）を残す。(文章, 切ったか) を返す。"""
    if len(text) <= limit:
        return text, False
    head, tail = int(limit * 0.75), int(limit * 0.25)
    return text[:head] + "\n\n[……中略（長いため省いた）……]\n\n" + text[-tail:], True
