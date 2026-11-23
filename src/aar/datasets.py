"""公開データセットの取得。

実験コードはネットワークなしで動くので、外部のデータはオーケストレーターが実験の前に取得しておく。
  * 取得できるのは、方針で許したホストの https の URL だけ。転送先のホストも毎回確かめる。
  * 大きさに上限がある。圧縮ファイルは展開しない（展開は実験コードが隔離環境の中で行う）。
  * 取得したファイルは data/external/ に置く。ここは Git の対象外で、公開候補のリポジトリには入らない
    （第三者のデータを再配布しないため）。代わりに、取得元・ハッシュ・ライセンス・取得日時を書いた
    MANIFEST.json と、それを読んで取得し直すスクリプト（tools/fetch_data.py）をコミットする。
  * ライセンスが書かれていないデータは取得しない。
  * 取得したファイルのハッシュは事前登録に入るので、実験の後でデータを差し替えると検出できる。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

EXTERNAL_DIR = "data/external"
MANIFEST = f"{EXTERNAL_DIR}/MANIFEST.json"
GITIGNORE_BLOCK = "\n# 外部から取得したデータは再配布しない（tools/fetch_data.py で取得し直せる）\ndata/external/*\n!data/external/MANIFEST.json\n"

FETCH_SCRIPT = '''"""外部データを取得し直す。data/external/MANIFEST.json に書かれた取得元から取得し、SHA-256 を照合する。

使い方: python tools/fetch_data.py
標準ライブラリだけで動く。データは再配布していないので、再現の前にこのスクリプトを実行する。
各データの利用条件（ライセンス）は MANIFEST.json にある。
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "data" / "external" / "MANIFEST.json").read_text(encoding="utf-8"))
failed = False
for item in manifest["files"]:
    dest = root / "data" / "external" / item["filename"]
    if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != item["sha256"]:
        print("取得:", item["url"])
        req = urllib.request.Request(item["url"], headers={"User-Agent": "fetch_data/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            dest.write_bytes(r.read())
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        print(f"ハッシュが違う: {item['filename']}（取得元の内容が変わった可能性がある）", file=sys.stderr)
        failed = True
    else:
        print("確認:", item["filename"], item["sha256"][:12])
sys.exit(1 if failed else 0)
'''


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allowed)


def safe_filename(name: str, url: str) -> str:
    base = name or Path(urlparse(url).path).name or "data"
    base = re.sub(r"[^A-Za-z0-9._\-]", "_", Path(base).name).lstrip(".")
    return base[:120] or "data"


async def _download(client: httpx.AsyncClient, url: str, allowed: list[str], max_bytes: int) -> bytes:
    """転送を自分でたどり、各段のホストを確かめながら取得する。"""
    for _ in range(6):
        u = urlparse(url)
        if u.scheme != "https":
            raise ValueError(f"https でない URL は取得しない: {url[:120]}")
        if not host_allowed(u.hostname or "", allowed):
            raise ValueError(f"許可されていないホスト: {u.hostname}")
        async with client.stream("GET", url, follow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            declared = int(r.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise ValueError(f"大きすぎる（{declared / 1e6:.0f}MB、上限 {max_bytes / 1e6:.0f}MB）")
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"大きすぎる（上限 {max_bytes / 1e6:.0f}MB を超えた）")
                chunks.append(chunk)
            return b"".join(chunks)
    raise ValueError("転送が多すぎる")


async def fetch_datasets(ws: Path, datasets: list[dict[str, Any]], *, allowed_hosts: list[str], max_mb: int,
                         client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """設計に書かれた外部データを取得する。{"files": [取得できたもの], "failed": [取得できなかったものと理由]} を返す。

    URL のないデータ（コードで生成するもの）は対象外。既に同じ内容で取得済みなら取り直さない。
    """
    wanted = [d for d in datasets if str(d.get("url") or "").strip()]
    out: dict[str, Any] = {"files": [], "failed": []}
    if not wanted:
        return out
    ext = ws / EXTERNAL_DIR
    ext.mkdir(parents=True, exist_ok=True)
    existing = {}
    if (ws / MANIFEST).exists():
        existing = {f["url"]: f for f in json.loads((ws / MANIFEST).read_text(encoding="utf-8")).get("files", [])}
    own = client is None
    client = client or httpx.AsyncClient(timeout=120, headers={"User-Agent": "autonomous-ai-research/0.1"})
    try:
        used: set[str] = set()
        for d in wanted:
            url = str(d["url"]).strip()
            lic = str(d.get("license") or "").strip()
            if not lic or lic in ("なし", "不明", "unknown", "N/A", "-"):
                out["failed"].append({"name": d.get("name"), "url": url, "reason": "ライセンスが書かれていない"})
                continue
            filename = safe_filename(str(d.get("filename") or ""), url)
            if filename in used:
                out["failed"].append({"name": d.get("name"), "url": url, "reason": f"ファイル名 {filename} が重複している"})
                continue
            prev = existing.get(url)
            dest = ext / filename
            if prev and prev["filename"] == filename and dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == prev["sha256"]:
                out["files"].append(prev)
                used.add(filename)
                continue
            try:
                data = await _download(client, url, allowed_hosts, max_mb * 1_000_000)
            except (ValueError, httpx.HTTPError) as e:
                out["failed"].append({"name": d.get("name"), "url": url, "reason": str(e)[:200] or type(e).__name__})
                continue
            digest = hashlib.sha256(data).hexdigest()
            expected = str(d.get("sha256") or "").strip().lower()
            if expected and expected != digest:
                out["failed"].append({"name": d.get("name"), "url": url, "reason": "設計に書かれた SHA-256 と一致しない"})
                continue
            dest.write_bytes(data)
            used.add(filename)
            out["files"].append({"name": d.get("name"), "filename": filename, "url": url, "sha256": digest, "bytes": len(data),
                                 "license": lic, "source": d.get("source", ""),
                                 "retrieved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")})
    finally:
        if own:
            await client.aclose()
    if out["files"]:
        write_manifest(ws, out["files"])
    return out


def write_manifest(ws: Path, files: list[dict[str, Any]]) -> None:
    """取得記録・取得し直すスクリプト・.gitignore を整える。"""
    (ws / EXTERNAL_DIR).mkdir(parents=True, exist_ok=True)
    (ws / MANIFEST).write_text(json.dumps({
        "note": "外部から取得したデータの記録。データそのものは再配布していない。tools/fetch_data.py で取得し直す。",
        "files": sorted(files, key=lambda f: f["filename"])}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ws / "tools").mkdir(exist_ok=True)
    (ws / "tools" / "fetch_data.py").write_text(FETCH_SCRIPT, encoding="utf-8")
    gi = ws / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if "data/external/*" not in text:
        gi.write_text(text.rstrip("\n") + "\n" + GITIGNORE_BLOCK, encoding="utf-8")


def verify_manifest(ws: Path) -> list[str]:
    """取得済みのデータが記録どおりかを確かめる。食い違いの一覧を返す。"""
    if not (ws / MANIFEST).exists():
        return []
    problems = []
    for f in json.loads((ws / MANIFEST).read_text(encoding="utf-8")).get("files", []):
        p = ws / EXTERNAL_DIR / f["filename"]
        if not p.exists():
            problems.append(f"{f['filename']} がない")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != f["sha256"]:
            problems.append(f"{f['filename']} の内容が取得時と違う")
        if not str(f.get("license") or "").strip():
            problems.append(f"{f['filename']} のライセンスが記録されていない")
    return problems


def copy_external_data(ws: Path, dest: Path) -> None:
    """監査用の複製に、取得済みの外部データを写す（Git の対象外なので、複製には入っていない）。"""
    import shutil

    src = ws / EXTERNAL_DIR
    if src.exists():
        shutil.copytree(src, dest / EXTERNAL_DIR, dirs_exist_ok=True)


# ---------------------------------------------------------------------- カタログと下見


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    """公開データセットのカタログ（config/datasets.yaml）。無ければ空。"""
    import yaml

    from aar.config import PROJECT_ROOT
    path = path or PROJECT_ROOT / "config" / "datasets.yaml"
    if not path.exists():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("datasets") or [])


def catalog_text(catalog: list[dict[str, Any]], allowed_hosts: list[str]) -> str:
    """担当に見せるカタログ（許可されたホストのものだけ）。"""
    rows = []
    for d in catalog:
        if not host_allowed(urlparse(str(d.get("url", ""))).hostname or "", allowed_hosts):
            continue
        rows.append(f"- {d['name']}: {d.get('task', '')}\n  url: {d['url']} / filename: {d.get('filename', '')} / "
                    f"license: {d.get('license', '')} / source: {d.get('source', '')} / 形式: {d.get('format', '')}")
    return "\n".join(rows)


def _head_lines(data: bytes, n: int = 6, width: int = 240) -> tuple[list[str], int] | None:
    if b"\x00" in data[:2000]:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return [line[:width] for line in lines[:n]], len(lines)


def preview_files(ws: Path, files: list[dict[str, Any]], max_members: int = 8) -> str:
    """取得したデータの形式の下見（ファイルの一覧、行数、先頭の数行）。設計を確定する前に担当に見せる。

    中身は第三者のデータなので、担当に渡すときは <untrusted_data> で囲み、リポジトリにはコミットしない。
    """
    import io
    import zipfile

    out: list[str] = []

    def describe(name: str, data: bytes, depth: int = 0) -> None:
        pad = "  " * depth
        if name.lower().endswith(".zip") and depth < 2:
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                out.append(f"{pad}- {name}: zip として読めない")
                return
            infos = [i for i in z.infolist() if not i.is_dir()]
            out.append(f"{pad}- {name}: zip（{len(infos)} ファイル）")
            for i in infos[:max_members]:
                if i.file_size > 60_000_000:
                    out.append(f"{pad}  - {i.filename}: {i.file_size:,} バイト（大きいので中は見ていない）")
                    continue
                describe(i.filename, z.read(i), depth + 1)
            if len(infos) > max_members:
                out.append(f"{pad}  - （ほか {len(infos) - max_members} ファイル）")
            return
        head = _head_lines(data)
        if head is None:
            out.append(f"{pad}- {name}: {len(data):,} バイト（バイナリ）")
            return
        lines, total = head
        out.append(f"{pad}- {name}: {len(data):,} バイト、{total:,} 行。先頭:")
        out.extend(f"{pad}    {line}" for line in lines)

    for f in files:
        p = ws / EXTERNAL_DIR / f["filename"]
        if p.exists():
            describe(f["filename"], p.read_bytes())
    return "\n".join(out)
