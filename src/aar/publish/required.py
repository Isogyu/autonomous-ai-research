"""公開前の必須項目の、決定的な検査。

公開品質監査（Skill）は言語モデルが実行するので、同じ対象でも実行ごとに指摘が揺れる（docs/STATUS.md）。
見逃すと取り返しがつかない項目は、モデルの判断に任せず、ここで毎回同じ手順で確かめる。
  * secrets          秘密情報（追跡ファイルと、取得済みの全履歴）
  * personal_info    個人情報（作業用パス、コミットの記録やファイルにあるメールアドレス）
  * third_party_text 第三者の文章の収録（原論文の要旨・本文との照合。追跡ファイルと全履歴）
  * license          ライセンス（LICENSE の有無と種別、外部データの記録のライセンス）
  * third_party_data 第三者のデータの再配布（data/external を追跡していないこと、取得記録の整合）

結果は項目ごとに ok / violation / unverified。violation と unverified は、公開品質ゲートを通さない
（gates.evaluate_publication_gate）。この検査は方針ファイルで無効にできない。
ここで分かるのは「機械的に確かめられる範囲」だけで、Skill の監査の代わりにはならない。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from aar import datasets as ds
from aar import workspace as wsmod

ITEMS: list[tuple[str, str]] = [
    ("secrets", "秘密情報（追跡ファイルと全履歴）"),
    ("personal_info", "個人情報（作業用パス、メールアドレス）"),
    ("third_party_text", "第三者の文章の収録（原論文の要旨・本文との照合）"),
    ("license", "ライセンス（リポジトリと外部データ）"),
    ("third_party_data", "第三者のデータの再配布"),
]
ITEM_LABEL = dict(ITEMS)

# 連続してこの文字数（空白を除く）以上、第三者の文章と一致したら「収録」とみなす。
# 短い引用（題名、1〜2文の引用）は対象にしない。照合元が短い場合は、その8割（最低 120 文字）を基準にする
VERBATIM_MIN_CHARS = 300
SOURCE_MIN_CHARS = 150
_K, _STRIDE = 48, 24          # 照合の単位。_K + _STRIDE 文字以上の一致は必ず見つかる
_MAX_FILE_BYTES = 8_000_000
# 行列積の演算子（a@b.T など）を拾わないよう、末尾はよく使われるドメインに限り、直後が識別子や括弧でないものだけにする
_EMAIL = re.compile(r"(?<![\w.@])[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+"
                    r"(?:com|org|net|edu|gov|io|ai|dev|me|info|jp|uk|de|fr|cn|kr|in|ca|au|ch|nl|it|es|se|us|eu|sg|tw|il|br|ru)(?![\w(\[.])")
_EMAIL_OK = re.compile(r"(?i)(noreply|no-reply|@example\.(com|org|net)$|^git@|@users\.noreply\.github\.com$|@anthropic\.com$|@openai\.com$)")
_LICENSES = [("MIT", "Permission is hereby granted, free of charge"), ("Apache-2.0", "Apache License"),
             ("BSD", "Redistribution and use in source and binary forms"), ("CC-BY-4.0", "Creative Commons Attribution 4.0"),
             ("CC0-1.0", "CC0 1.0 Universal"), ("GPL", "GNU GENERAL PUBLIC LICENSE"), ("MPL-2.0", "Mozilla Public License")]


def _norm(text: str) -> str:
    """照合のために正規化する（空白と引用符の違いを無視し、小文字にする）。"""
    text = text.replace("\\n", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", "", text).lower()


class _Source:
    """第三者の文章1件。すべての位置の _K 文字の断片を索引にする。"""

    def __init__(self, label: str, text: str) -> None:
        self.label = label
        self.norm = _norm(text)
        self.threshold = min(VERBATIM_MIN_CHARS, max(120, int(len(self.norm) * 0.8)))
        self.index: dict[int, int] = {}
        for i in range(0, max(len(self.norm) - _K + 1, 0)):
            self.index.setdefault(hash(self.norm[i:i + _K]), i)

    def longest_run(self, other: str) -> int:
        """other（正規化済み）と、連続して一致する最長の文字数。"""
        best, n, m = 0, len(other), len(self.norm)
        j = 0
        while j + _K <= n:
            i = self.index.get(hash(other[j:j + _K]))
            if i is None or self.norm[i:i + _K] != other[j:j + _K]:
                j += _STRIDE
                continue
            a, b = i, j                       # 一致を前後に伸ばす
            while a > 0 and b > 0 and self.norm[a - 1] == other[b - 1]:
                a, b = a - 1, b - 1
            e, f = i + _K, j + _K
            while e < m and f < n and self.norm[e] == other[f]:
                e, f = e + 1, f + 1
            best = max(best, f - b)
            j = f + 1
        return best


def _decode(data: bytes) -> str | None:
    if len(data) > _MAX_FILE_BYTES or b"\x00" in data[:4000]:
        return None
    return data.decode("utf-8", errors="ignore")


def _history_blobs(ws: Path) -> list[tuple[str, str]]:
    """取得済みの全履歴（すべての参照）から到達できるファイルの (blob, パス)。同じ内容は1回だけ。"""
    out = subprocess.run(["git", "rev-list", "--all", "--objects"], cwd=ws, capture_output=True, text=True, timeout=300)
    seen: dict[str, str] = {}
    for line in out.stdout.splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in seen:
            seen[sha] = path
    return list(seen.items())


def _cat_blobs(ws: Path, shas: list[str]) -> dict[str, bytes]:
    """blob の内容をまとめて読む（木やコミットは除く）。"""
    if not shas:
        return {}
    p = subprocess.run(["git", "cat-file", "--batch"], cwd=ws, input=("\n".join(shas) + "\n").encode(), capture_output=True, timeout=600)
    data, pos, out = p.stdout, 0, {}
    while pos < len(data):
        nl = data.index(b"\n", pos)
        head = data[pos:nl].split()
        pos = nl + 1
        if len(head) < 3:
            continue
        size = int(head[2])
        if head[1] == b"blob":
            out[head[0].decode()] = data[pos:pos + size]
        pos += size + 1
    return out


def _item(key: str, status: str, evidence: list[str], method: str) -> dict[str, Any]:
    return {"id": key, "label": ITEM_LABEL[key], "status": status, "evidence": evidence[:20], "method": method}


def check_third_party_text(ws: Path, sources: list[tuple[str, str]]) -> dict[str, Any]:
    """原論文の要旨・本文が、追跡ファイルや履歴にまとまった長さで写っていないかを確かめる。"""
    srcs = [_Source(label, text) for label, text in sources if text and len(_norm(text)) >= SOURCE_MIN_CHARS]
    method = (f"照合元 {len(srcs)} 件（{'、'.join(s.label for s in srcs) or 'なし'}）と、追跡ファイル・全履歴のテキストを照合。"
              f"空白を除いて連続 {VERBATIM_MIN_CHARS} 文字以上（照合元が短ければその8割以上）の一致を収録とみなす")
    if not srcs:
        return _item("third_party_text", "unverified", ["照合元の文章（原論文の要旨・本文）が手元にない"], method)
    tracked = set(wsmod.git(ws, "ls-files").splitlines())
    blobs = _history_blobs(ws)
    head_blobs = {line.split()[2]: line.split("\t", 1)[1] for line in wsmod.git(ws, "ls-tree", "-r", "HEAD").splitlines() if "\t" in line}
    contents = _cat_blobs(ws, [sha for sha, _ in blobs])
    hits: list[str] = []
    for sha, path in blobs:
        text = _decode(contents.get(sha, b""))
        if not text:
            continue
        norm = _norm(text)
        for s in srcs:
            run = s.longest_run(norm)
            if run >= s.threshold:
                where = "現在のファイル" if sha in head_blobs and path in tracked else "過去のコミットだけ"
                hits.append(f"{path}（{where}）: {s.label}と連続 {run} 文字が一致")
    hits.sort(key=lambda h: "過去のコミットだけ" in h)
    return _item("third_party_text", "violation" if hits else "ok", hits or [f"一致なし（調べたファイル {len(contents)} 件）"], method)


REDACTED_NOTE = "第三者の文章（{labels}）を含むため、公開候補には写していない。監査の原本には残っている"


def redact_third_party_text(text: str, sources: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """文章から、第三者の文章がまとまった長さで写っている行を取り除く。（取り除いたあとの文章、理由の一覧）を返す。

    監査の実行ログのように、公開候補へ写す記録に原論文の要旨や本文が入り込む場合に使う（実行担当が、それらを含むファイルを
    表示した結果がログに残る）。基準は check_third_party_text と同じ。行を取り除いても一致が残る（行をまたぐ）ときは、全文を注記に置き換える。
    一致がなければ、文章はそのまま返す。
    """
    srcs = [_Source(label, t) for label, t in sources if t and len(_norm(t)) >= SOURCE_MIN_CHARS]
    if not srcs or len(text.encode("utf-8", errors="ignore")) > _MAX_FILE_BYTES:
        return text, []

    def hit(part: str) -> list[str]:
        norm = _norm(part)
        return [s.label for s in srcs if s.longest_run(norm) >= s.threshold]

    if not hit(text):
        return text, []
    reasons: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        labels = hit(line) if len(line) >= 120 else []
        if labels:
            note = REDACTED_NOTE.format(labels="、".join(labels))
            lines[i] = json.dumps({"type": "redacted", "reason": note}, ensure_ascii=False) if line.lstrip().startswith("{") else f"（{note}）"
            reasons.append(f"{i + 1} 行目: {'、'.join(labels)}")
    out = "\n".join(lines)
    left = hit(out)
    if left:                                  # 行をまたいで写っている
        note = REDACTED_NOTE.format(labels="、".join(left))
        return f"（{note}）\n", [f"全文: {'、'.join(left)}"]
    return out, reasons


def check_secrets(ws: Path) -> dict[str, Any]:
    hits = [h for h in wsmod.scan_secrets(ws) if h["kind"] != "利用者のホームのパス"]
    return _item("secrets", "violation" if hits else "ok", [f"{h['path']}（{h['kind']}）" for h in hits] or ["検出なし"],
                 "既知の形式のキー・トークン・秘密鍵と、秘密情報を入れる名前のファイルを、追跡ファイルと全履歴の差分から探す")


def check_personal_info(ws: Path) -> dict[str, Any]:
    hits = [f"{h['path']}（{h['kind']}）" for h in wsmod.scan_secrets(ws, include_history=False) if h["kind"] == "利用者のホームのパス"]
    for rel in wsmod.git(ws, "ls-files").splitlines():
        p = ws / rel
        try:
            text = _decode(p.read_bytes())
        except OSError:
            continue
        for m in _EMAIL.finditer(text or ""):
            if not _EMAIL_OK.search(m.group(0)):
                hits.append(f"{rel}:{(text or '').count(chr(10), 0, m.start()) + 1}（メールアドレス）")
                break
    emails = set(wsmod.git(ws, "log", "--all", "--format=%ae%n%ce", check=False).split())
    bad = sorted(e for e in emails if not _EMAIL_OK.search(e))
    if bad:
        hits.append(f"コミットの記録に、公開用でないメールアドレスが {len(bad)} 件ある")
    return _item("personal_info", "violation" if hits else "ok", hits or ["検出なし"],
                 "追跡ファイルの作業用パスとメールアドレス、全コミットの作成者・コミッターのメールアドレスを調べる")


def check_license(ws: Path) -> dict[str, Any]:
    method = "LICENSE の有無と既知のライセンス文の照合、README の記載、外部データの取得記録のライセンス欄と提供元の表示の記録との照合"
    p = ws / "LICENSE"
    if not p.exists() or not p.read_text(encoding="utf-8", errors="ignore").strip():
        return _item("license", "violation", ["LICENSE がない、または空"], method)
    text = p.read_text(encoding="utf-8", errors="ignore")
    kind = next((name for name, sig in _LICENSES if sig.lower() in text.lower()), None)
    problems, notes = [], []
    if kind is None:
        return _item("license", "unverified", ["LICENSE の文面が既知のライセンスと一致しない（種別を機械的に確かめられない）"], method)
    notes.append(f"LICENSE は {kind}")
    readme = (ws / "README.md").read_text(encoding="utf-8", errors="ignore") if (ws / "README.md").exists() else ""
    if not re.search(r"(?i)license|ライセンス", readme):
        problems.append("README にライセンスの記載がない")
    from aar.publish import licenses

    # 基盤が取得したときのままの記録だけを裏づけに使う（リポジトリの中のファイルなので、書き換えられ得る）
    ev = licenses.load_evidence(ws, trusted_only=True)
    tampered = (ws / licenses.EVIDENCE_JSON).exists() and not licenses.is_untouched(ws)
    confirmed = {d.get("filename"): d for d in ev.get("data", []) if d.get("confirmed")}
    unverified = []
    for f in _manifest_files(ws):
        name, lic = f.get("filename"), str(f.get("license") or "").strip()
        if not lic:
            problems.append(f"外部データ {name} のライセンスが記録されていない")
        elif name in confirmed and confirmed[name].get("declared") == lic:
            notes.append(f"外部データ {name}: {lic}（提供元の表示と一致。{licenses.EVIDENCE_MD}）")
        else:
            # 取得記録のライセンス欄は申告にすぎない。提供元の表示で裏づけられなければ、確かめたことにしない
            unverified.append(f"外部データ {name}: {lic} と申告されているが、提供元の表示の記録（{licenses.EVIDENCE_JSON}）で裏づけられていない")
    deps = {(d.get("name"), d.get("version")): d for d in ev.get("dependencies", [])}
    for n, v in licenses.pinned_requirements(ws):
        d = deps.get((n, v))
        if d and d.get("confirmed"):
            notes.append(f"依存 {n}=={v}: {d.get('license')}")
    if tampered:
        unverified.append(f"{licenses.EVIDENCE_JSON} が、基盤が取得したときの内容と違う（裏づけに使わない）")
    if problems:
        return _item("license", "violation", problems, method)
    return _item("license", "unverified" if unverified else "ok", unverified or notes, method)


def _manifest_files(ws: Path) -> list[dict[str, Any]]:
    import json

    p = ws / ds.MANIFEST
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("files", []))
    except ValueError:
        return [{"filename": "MANIFEST.json（読めない）", "license": ""}]


def check_third_party_data(ws: Path) -> dict[str, Any]:
    method = "data/external 以下が追跡されていないこと（現在と全履歴）、取得記録とファイルのハッシュの一致、取得し直すスクリプトの有無"
    problems = list(ds.verify_manifest(ws))
    allowed = {ds.MANIFEST}
    for sha, path in _history_blobs(ws):
        if path.startswith(ds.EXTERNAL_DIR + "/") and path not in allowed:
            problems.append(f"{path} が Git に入っている（{sha[:8]}）")
    files = _manifest_files(ws)
    if files and not (ws / "tools" / "fetch_data.py").exists():
        problems.append("外部データを取得し直すスクリプト（tools/fetch_data.py）がない")
    note = f"外部データ {len(files)} 件（取得記録あり、再配布なし）" if files else "外部データを使っていない"
    return _item("third_party_data", "violation" if problems else "ok", problems or [note], method)


def run_required_checks(ws: Path, third_party_texts: list[tuple[str, str]]) -> dict[str, Any]:
    """必須項目の決定的な検査をすべて実行する。"""
    items = [check_secrets(ws), check_personal_info(ws), check_third_party_text(ws, third_party_texts),
             check_license(ws), check_third_party_data(ws)]
    digest = hashlib.sha256(repr([(i["id"], i["status"], i["evidence"]) for i in items]).encode()).hexdigest()[:16]
    return {"commit": wsmod.head(ws), "items": items, "digest": digest,
            "violations": [i["id"] for i in items if i["status"] == "violation"],
            "unverified": [i["id"] for i in items if i["status"] == "unverified"]}
