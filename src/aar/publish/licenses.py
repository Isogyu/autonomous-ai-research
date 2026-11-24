"""外部データと依存パッケージのライセンスの一次資料の記録。

取得記録（data/external/MANIFEST.json）のライセンス欄は、カタログや設計担当が書いた申告にすぎない。
公開前の監査では「提供元がそう明示している」ことを対象内の資料で確かめられる必要があるので、
提供元のページにあるライセンスの文言と、PyPI に登録された依存パッケージのライセンスを取得し、
取得元・取得日時・ハッシュとともに研究のリポジトリに残す。

ここは事実を記録するだけで、合否は決めない。申告と提供元の文言が一致しなければ「未確認」と書く
（決定的な検査 aar.publish.required がそれを未確認として扱い、ゲートは通らない）。
データそのものと事前登録、取得記録には触れない。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from aar import datasets as ds

EVIDENCE_JSON = "docs/license_evidence.json"
EVIDENCE_MD = "docs/third_party_licenses.md"
PYPI = "https://pypi.org/pypi/{name}/{version}/json"

# 申告されたライセンス名と、提供元の文言に現れるはずの表現
_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("CC BY 4.0", ("cc by 4.0", "creative commons attribution 4.0", "creativecommons.org/licenses/by/4.0")),
    ("CC0", ("cc0", "creativecommons.org/publicdomain/zero")),
    ("CC BY-SA 4.0", ("cc by-sa 4.0", "creativecommons.org/licenses/by-sa/4.0")),
    ("MIT", ("mit license", "mit")),
    ("Apache-2.0", ("apache license", "apache-2.0", "apache 2.0")),
]
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*==\s*([A-Za-z0-9.!+\-]+)\s*(?:#.*)?$")


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", s.lower()).strip()


def statement_matches(declared: str, statement: str) -> bool:
    """提供元の文言が、申告されたライセンスを指しているか。"""
    d, st = _norm(declared), _norm(statement)
    if not d or not st:
        return False
    for name, forms in _ALIASES:
        if _norm(name) == d:
            return any(_norm(f) in st for f in forms)
    return d in st


def extract_statement(page: str) -> tuple[str, list[str]]:
    """ページからライセンスの文言（前後を含む短い抜粋）と、ライセンス文書へのリンクを取り出す。無ければ空。"""
    links = sorted(set(re.findall(r'href="(https?://(?:creativecommons\.org|opensource\.org|spdx\.org)/[^"]+)"', page)))
    text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)))
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"(?i)\b(?:is|are) licensed under\b|\blicensed under\b|\blicen[sc]e\s*:", text)
    if not m:
        return "", links
    # 文の頭（直前の This / The）まで戻り、あとの2文ほどまでを取る。見出しや DOI など、手前の語は含めない
    heads = list(re.finditer(r"\b(?:This|The|These)\b", text[max(0, m.start() - 60):m.start()]))
    start = max(0, m.start() - 60) + heads[-1].start() if heads else m.start()
    chunk = text[start:m.start() + 300]
    end = chunk.rfind(". ")
    return (chunk[:end + 1] if end > 60 else chunk).strip(), links


def pinned_requirements(ws: Path) -> list[tuple[str, str]]:
    p = ws / "requirements.txt"
    if not p.exists():
        return []
    return [(m.group(1), m.group(2)) for line in p.read_text(encoding="utf-8", errors="ignore").splitlines() if (m := _PIN.match(line))]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


async def _data_evidence(client: httpx.AsyncClient, f: dict[str, Any], entry: dict[str, Any] | None,
                         allowed_hosts: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"filename": f.get("filename"), "name": f.get("name"), "declared": f.get("license", ""),
                           "source": f.get("source", ""), "confirmed": False}
    url = str((entry or {}).get("license_url") or "").strip()
    if not url:
        return out | {"reason": "提供元のライセンス表示のページが、カタログに登録されていない"}
    if not ds.host_allowed(urlparse(url).hostname or "", allowed_hosts):
        return out | {"license_url": url, "reason": "許可されていないホスト"}
    try:
        r = await client.get(url, follow_redirects=False)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return out | {"license_url": url, "reason": f"取得できなかった: {str(e)[:120] or type(e).__name__}"}
    statement, links = extract_statement(r.text)
    ok = statement_matches(str(f.get("license") or ""), statement + " " + " ".join(links))
    out |= {"license_url": url, "retrieved_at": _now(), "page_sha256": hashlib.sha256(r.content).hexdigest(),
            "statement": statement, "license_documents": links, "confirmed": ok}
    if not ok:
        out["reason"] = "提供元のページに、申告と一致するライセンスの文言が見つからない"
    return out


async def _dependency_evidence(client: httpx.AsyncClient, name: str, version: str) -> dict[str, Any]:
    url = PYPI.format(name=name, version=version)
    out: dict[str, Any] = {"name": name, "version": version, "record_url": url, "license": "", "confirmed": False}
    try:
        r = await client.get(url, follow_redirects=False)
        r.raise_for_status()
        info = r.json().get("info") or {}
    except (httpx.HTTPError, ValueError) as e:
        return out | {"reason": f"取得できなかった: {str(e)[:120] or type(e).__name__}"}
    expr = str(info.get("license_expression") or "").strip()
    classifiers = [c.split("::")[-1].strip() for c in info.get("classifiers") or [] if c.startswith("License ::")]
    first_line = str(info.get("license") or "").strip().splitlines()[0][:120] if str(info.get("license") or "").strip() else ""
    lic = expr or "、".join(classifiers) or first_line
    out |= {"license": lic, "retrieved_at": _now(), "record_sha256": hashlib.sha256(r.content).hexdigest(),
            "from": "license_expression" if expr else ("classifiers" if classifiers else "license"), "confirmed": bool(lic)}
    if not lic:
        out["reason"] = "PyPI の登録情報にライセンスの記載がない"
    return out


def load_evidence(ws: Path) -> dict[str, Any]:
    p = ws / EVIDENCE_JSON
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except ValueError:
        return {}


def _key(ev: dict[str, Any]) -> dict[str, Any]:
    """記録が対象とするもの（データのファイルと依存の版）。これが同じで、すべて確認済みなら取り直さない。"""
    return {"data": sorted((d.get("filename"), d.get("declared")) for d in ev.get("data", [])),
            "deps": sorted((d.get("name"), d.get("version")) for d in ev.get("dependencies", []))}


async def record_license_evidence(ws: Path, *, allowed_hosts: list[str], repo_license: str = "MIT",
                                  catalog: list[dict[str, Any]] | None = None,
                                  client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """外部データと依存のライセンスの一次資料を取得して、docs/ に記録する。対象が無ければ何もしない。"""
    files = []
    if (ws / ds.MANIFEST).exists():
        try:
            files = list(json.loads((ws / ds.MANIFEST).read_text(encoding="utf-8")).get("files", []))
        except ValueError:
            files = []
    deps = pinned_requirements(ws)
    if not files and not deps:
        return {"recorded": False, "reason": "外部データも、版を固定した依存もない"}
    prev = load_evidence(ws)
    want = {"data": sorted((f.get("filename"), f.get("license", "")) for f in files), "deps": sorted(deps)}
    if prev and _key(prev) == {"data": [tuple(x) for x in want["data"]], "deps": [tuple(x) for x in want["deps"]]} and all(
            x.get("confirmed") for x in prev.get("data", []) + prev.get("dependencies", [])):
        return {"recorded": True, "unchanged": True, "unconfirmed": []}
    by_url = {str(c.get("url")): c for c in (catalog if catalog is not None else ds.load_catalog())}
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, headers={"User-Agent": "autonomous-ai-research/0.1"})
    try:
        data = [await _data_evidence(client, f, by_url.get(str(f.get("url"))), allowed_hosts) for f in files]
        dependencies = [await _dependency_evidence(client, n, v) for n, v in deps]
    finally:
        if own:
            await client.aclose()
    ev = {"note": "外部データと依存パッケージのライセンスについて、提供元の表示を取得した記録。"
                  "データと依存パッケージそのものは、このリポジトリに含めていない。",
          "repository_license": repo_license, "data": data, "dependencies": dependencies}
    (ws / "docs").mkdir(exist_ok=True)
    (ws / EVIDENCE_JSON).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ws / EVIDENCE_MD).write_text(render(ev), encoding="utf-8")
    return {"recorded": True, "unchanged": False,
            "unconfirmed": [str(x.get("filename") or x.get("name")) for x in data + dependencies if not x.get("confirmed")]}


def render(ev: dict[str, Any]) -> str:
    lines = ["# 第三者のデータと依存パッケージのライセンス", "",
             f"このリポジトリ自身のコードと文章のライセンスは {ev.get('repository_license')}（`LICENSE`）。",
             "下の外部データと依存パッケージは、このリポジトリに含めていない（再配布していない）。",
             "データは `tools/fetch_data.py` が提供元から取得し、依存パッケージは `requirements.txt` に従って利用者が導入する。",
             "したがって、このリポジトリのライセンスが下のデータや依存パッケージに及ぶことはなく、",
             "それぞれの利用条件は提供元のライセンスに従う。",
             "", "この文書は、提供元の表示を機械的に取得した記録である（`docs/license_evidence.json` と同じ内容）。",
             "法的な助言ではない。「未確認」の行は、提供元の表示を確かめられなかったことを示す。", ""]
    if ev.get("data"):
        lines += ["## 外部データ", ""]
        for d in ev["data"]:
            lines += [f"### {d.get('name') or d.get('filename')}（`{d.get('filename')}`）", "",
                      f"- 取得記録に書かれたライセンス: {d.get('declared') or '（なし）'}",
                      f"- 提供元の表示との照合: {'一致' if d.get('confirmed') else '未確認（' + str(d.get('reason', '')) + '）'}"]
            if d.get("license_url"):
                lines.append(f"- 提供元のページ: <{d['license_url']}>")
            if d.get("retrieved_at"):
                lines.append(f"- 取得日時: {d['retrieved_at']} / ページの SHA-256: `{d.get('page_sha256')}`")
            if d.get("statement"):
                lines.append(f"- 提供元の文言（抜粋）: “{d['statement']}”")
            for link in d.get("license_documents") or []:
                lines.append(f"- ライセンス文書: <{link}>")
            if d.get("source"):
                lines.append(f"- 帰属の表示（出典）: {d['source']}")
            lines.append("- このリポジトリでの扱い: 取得して分析に使うだけで、データを変更して再配布することはしていない")
            lines.append("")
    if ev.get("dependencies"):
        lines += ["## 依存パッケージ（`requirements.txt` で版を固定したもの）", "",
                  "| パッケージ | 版 | ライセンス（PyPI の登録情報） | 記録 | 取得日時 |", "|---|---|---|---|---|"]
        for d in ev["dependencies"]:
            lic = d.get("license") if d.get("confirmed") else f"未確認（{d.get('reason', '')}）"
            lines.append(f"| {d.get('name')} | {d.get('version')} | {lic} | <{d.get('record_url')}> | {d.get('retrieved_at', '')} |")
        lines.append("")
    return "\n".join(lines)
