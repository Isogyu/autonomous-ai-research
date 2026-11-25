"""投稿候補の置き場所、状態、訂正の関係、確認、感想の記録。

置き場所は、リポジトリの中のファイル（新しいサービスも DB の表も足さない）。
  briefs/<id>/brief.yaml      投稿候補（人が読んで直せる）
  briefs/ledger.jsonl         状態の変更の記録（追記だけ。前の行のハッシュを持ち、途中の書き換えが分かる）
  briefs/feedback.jsonl       小規模の試行での感想（個人を特定する情報は入れない）
  briefs/<id>/preview/        生成したカード・本文・詳細ページ・プレビュー
内容の差分は Git で見る。レビューは、ふつうの PR のレビューと同じ形でできる。

状態: draft（下書き）→ in_review（レビュー待ち）→ approved（承認済み）。ほかに hold（保留）、rejected（棄却）、archived（検索用の保存）。
承認は、内容のハッシュに結び付く。承認のあとで内容が変わると、承認は効かなくなり、下書きに戻ったものとして扱う。
承認できるのは人だけ。**送信の機能はない**（投稿は人が Teams で行い、その事実を記録する）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from aar import workspace as wsmod
from aar.brief import render
from aar.brief.model import Brief, load
from aar.publish import required as req

STATES = {"draft": "下書き", "in_review": "レビュー待ち", "approved": "承認済み", "hold": "保留", "rejected": "棄却", "archived": "検索用の保存"}
ACTOR_KINDS = ("human", "claude_code", "codex", "scheduler")
_EVENT_STATE = {"submitted": "in_review", "approved": "approved", "held": "hold", "rejected": "rejected", "archived": "archived", "reopened": "draft"}
# 感想に、個人を特定する書き方を入れない（メールアドレス、@ での名指し）
_PERSONAL = re.compile(r"@[\w\-.]+|[\w.+\-]+@[\w\-]+\.[\w.\-]+")


class BriefError(RuntimeError):
    pass


def parse_actor(text: str | None) -> tuple[str, str]:
    """操作した主体。「種類:名前」で必ず書く（省略できない。AI の操作を人の操作として記録しない）。"""
    kind, _, detail = (text or "").partition(":")
    if kind not in ACTOR_KINDS or not detail.strip():
        raise BriefError("--actor を「human:名前」「claude_code:セッション」のように指定する（種類: " + " / ".join(ACTOR_KINDS) + "）")
    return kind, detail.strip()


def brief_path(root: Path, brief_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{2,60}", brief_id):
        raise BriefError(f"id の形が正しくない: {brief_id}")
    return root / brief_id / "brief.yaml"


def read(root: Path, brief_id: str) -> Brief:
    path = brief_path(root, brief_id)
    if not path.exists():
        raise BriefError(f"投稿候補がない: {path}")
    raw = path.read_text(encoding="utf-8")
    for n, line in enumerate(raw.splitlines(), 1):
        # YAML では、空白のあとの # から後ろがコメントになる。引用符で囲んでいない値にあると、文が黙って欠ける
        if re.match(r"""^\s*(?:- )?(?:[a-z_]+: )?[^'"\[{#\s][^'"]* #""", line):
            raise BriefError(f"{path.name} の {n} 行目: 空白のあとの # から後ろはコメントになり、文が欠ける。値を引用符で囲む")
    b = load(yaml.safe_load(raw))
    if b.id != brief_id:
        raise BriefError(f"フォルダーの名前と id が違う: {brief_id} / {b.id}")
    return b


def list_ids(root: Path) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/brief.yaml"))


# ---------------------------------------------------------------------- 状態の記録（追記だけ）

def _ledger(root: Path) -> Path:
    return root / "ledger.jsonl"


def events(root: Path) -> list[dict[str, Any]]:
    """記録を読み、つながり（前の行のハッシュ）を確かめる。途中が書き換えられていれば、読まずに失敗する。"""
    path = _ledger(root)
    if not path.exists():
        return []
    out, prev = [], ""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        ev = json.loads(line)
        body = {k: v for k, v in ev.items() if k != "sha256"}
        if ev.get("prev") != prev or ev.get("sha256") != hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest():
            raise BriefError(f"状態の記録の {n} 行目が、書き換えられているか、順序が崩れている（{path}）")
        prev = ev["sha256"]
        out.append(ev)
    return out


def _append(root: Path, brief_id: str, event: str, actor: tuple[str, str], content_sha: str, detail: dict[str, Any] | None = None,
            now: dt.datetime | None = None) -> dict[str, Any]:
    past = events(root)
    body = {"at": (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"), "brief": brief_id, "event": event,
            "actor_kind": actor[0], "actor": actor[1], "content_sha256": content_sha, "detail": detail or {},
            "prev": past[-1]["sha256"] if past else ""}
    body["sha256"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    with _ledger(root).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
    return body


def state(root: Path, b: Brief) -> dict[str, Any]:
    """いまの状態。記録の最後の状態が、いまの内容に対するものでなければ、下書きとして扱う。"""
    mine = [e for e in events(root) if e["brief"] == b.id]
    last = next((e for e in reversed(mine) if e["event"] in _EVENT_STATE), None)
    sha = b.content_sha256()
    posted = [e for e in mine if e["event"] == "posted"]
    out: dict[str, Any] = {"state": "draft", "stale": False, "posted": posted, "events": mine}
    if last:
        out["state"] = _EVENT_STATE[last["event"]]
        if last["content_sha256"] != sha and out["state"] in ("in_review", "approved"):
            out.update(state="draft", stale=True, stale_from=_EVENT_STATE[last["event"]])
    out["label"] = STATES[out["state"]] + (f"（{STATES[out['stale_from']]}のあとで内容が変わったので、やり直し）" if out["stale"] else "")
    if any(e["content_sha256"] == sha for e in posted):
        out["label"] += "・投稿済み（人が記録）"
    return out


def corrections(root: Path, b: Brief) -> list[dict[str, str]]:
    """この投稿候補に至る訂正の履歴（古い順）と、この候補を訂正した候補。"""
    out: list[dict[str, str]] = []
    seen, cur = {b.id}, b
    while cur.corrects:
        out.append({"from": cur.corrects, "to": cur.id, "note": cur.correction_note, "at": cur.updated_on.isoformat()})
        if cur.corrects in seen or not brief_path(root, cur.corrects).exists():
            break
        seen.add(cur.corrects)
        cur = read(root, cur.corrects)
    out.reverse()
    return out


def corrected_by(root: Path, brief_id: str) -> list[str]:
    out = []
    for other in list_ids(root):
        if other == brief_id:
            continue
        try:
            if read(root, other).corrects == brief_id:
                out.append(other)
        except Exception:  # noqa: BLE001 - 読めない候補は、一覧では飛ばす（validate が報告する）
            continue
    return out


# ---------------------------------------------------------------------- 確認

def checks(root: Path, b: Brief, third_party_texts: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """送る前の機械的な確認。秘密情報・個人情報・第三者の文章は、既存の公開前の検査と同じ基準を使う。

    ここで分かるのは、機械的に確かめられる範囲だけ。社内の機密にあたるかどうかは、人が確かめる。
    """
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        svg = render.card_svg(b)
        add("カードに収まる（文字の大きさの下限を保つ）", True, f"文字の下限 {render.MIN_FONT}、幅 {render.CARD_W}")
    except render.DoesNotFit as e:
        svg = ""
        add("カードに収まる（文字の大きさの下限を保つ）", False, str(e))
    text, detail = render.post_text(b), render.detail_markdown(b, corrections(root, b))
    payload = render.adaptive_card(b)
    size = render.adaptive_card_bytes(payload)
    add("Adaptive Card の大きさが上限以内", size <= render.TEAMS_WEBHOOK_LIMIT_BYTES, f"{size} バイト（上限 {render.TEAMS_WEBHOOK_LIMIT_BYTES}）")
    everything = "\n".join([svg, text, detail, json.dumps(payload, ensure_ascii=False)])
    secrets = [kind for kind, pat in wsmod.SECRET_PATTERNS if pat.search(everything)]
    add("秘密情報がない", not secrets, "、".join(secrets) or "検出なし（既知の形式の照合。未知の形式は見つけられない）")
    # 作業した PC のパス（macOS・Linux・WSL のホーム、Windows のホーム、UNC）。公開前の検査と同じ照合
    local = wsmod.find_local_path("\n".join([text, detail, json.dumps(payload, ensure_ascii=False), *b.texts()]))
    add("作業した PC のパスがない", local is None, "検出なし" if local is None else "作業用のパスらしい記述がある（利用者名や社内の共有名が入っていないかを確かめる）")
    mails = sorted({m.group(0) for m in req._EMAIL.finditer(everything) if not req._EMAIL_OK.search(m.group(0))})
    add("メールアドレスがない", not mails, f"{len(mails)} 件" if mails else "検出なし")
    srcs = [req._Source(label, t) for label, t in (third_party_texts or []) if t and len(req._norm(t)) >= req.SOURCE_MIN_CHARS]
    if srcs:
        norm = req._norm(everything)
        hit = [s.label for s in srcs if s.longest_run(norm) >= s.threshold]
        add("第三者の文章を、まとまった長さで写していない", not hit, "一致: " + "、".join(hit) if hit else f"照合元 {len(srcs)} 件と一致なし")
    else:
        add("第三者の文章を、まとまった長さで写していない", False,
            "照合元の文章が手元にない（briefs/<id>/sources/*.txt に置くと照合する）。置けない場合は、承認する人が確かめて申告する")
        out[-1]["unverified"] = True          # 一致を検出したのではなく、照合できなかった（人の申告で補えるのは、この場合だけ）
    missing = [x for x in (*b.takeaways, *b.constraints) if x not in text or x not in detail]
    add("要点と制約が、本文と詳細ページの両方にある", not missing, "欠け: " + "、".join(missing) if missing else "すべてある")
    for label in (render.VERIFICATION_LABEL[b.verification], render.PURPOSE_LABEL[b.purpose]):
        add(f"「{label}」が、カード・本文・詳細ページのすべてにある", all(label in x for x in (svg or label, text, detail)), "表示あり")
    known = {s.url for s in b.sources}
    extra = sorted({u for u in re.findall(r"https?://[^\s<>\"'）)]+", text + detail) if u not in known})
    add("出典の一覧にない URL が出力にない", not extra, "、".join(extra) or "なし")
    if b.corrects:
        add("訂正の対象が存在する", brief_path(root, b.corrects).exists(), b.corrects)
    return out


def source_texts(root: Path, brief_id: str) -> list[tuple[str, str]]:
    d = root / brief_id / "sources"
    return [(p.name, p.read_text(encoding="utf-8", errors="ignore")) for p in sorted(d.glob("*.txt"))] if d.exists() else []


THIRD_PARTY = "第三者の文章を、まとまった長さで写していない"


def write_preview(root: Path, b: Brief) -> Path:
    out = root / b.id / "preview"
    out.mkdir(parents=True, exist_ok=True)
    cs = checks(root, b, source_texts(root, b.id))
    corr = corrections(root, b)
    files = {"card.svg": render.card_svg(b), "post.txt": render.post_text(b), "detail.md": render.detail_markdown(b, corr),
             "alt.txt": render.alt_text(b) + "\n",
             "adaptive_card.json": json.dumps(render.adaptive_card(b), ensure_ascii=False, indent=1) + "\n",
             "preview.html": render.preview_html(b, state=state(root, b), checks=cs, corrections=corr)}
    for name, content in files.items():
        (out / name).write_text(content, encoding="utf-8", newline="\n")
    return out


# ---------------------------------------------------------------------- 状態の変更

def transition(root: Path, brief_id: str, event: str, actor: tuple[str, str], *, reason: str = "", confirm_shareable: bool = False,
               confirm_no_copied_text: bool = False, now: dt.datetime | None = None) -> dict[str, Any]:
    b = read(root, brief_id)
    st = state(root, b)
    sha = b.content_sha256()
    if event in ("held", "rejected", "archived", "reopened"):
        if not reason:
            raise BriefError("理由を書く（--reason）")
        return _append(root, brief_id, event, actor, sha, {"reason": reason}, now)
    cs = checks(root, b, source_texts(root, brief_id))
    failed = [c for c in cs if not c["ok"]]
    if event == "submitted":
        blocking = [c for c in failed if not c.get("unverified")]
        if blocking:
            raise BriefError("確認に通っていない: " + "; ".join(f"{c['name']}（{c['detail']}）" for c in blocking))
        return _append(root, brief_id, event, actor, sha, {"review_notes": b.review_notes()}, now)
    if event == "approved":
        if actor[0] != "human":
            raise BriefError("承認できるのは人だけ（--actor human:名前）")
        if st["state"] != "in_review":
            raise BriefError(f"承認できるのは、レビュー待ちの候補だけ（いまは {st['label']}）")
        if b.verification == "illustrative":
            raise BriefError("表示確認用の架空データは、承認も投稿もできない")
        if b.triage != "candidate":
            raise BriefError(f"配信の扱いが「{b.triage}」の候補は承認できない")
        blocking = [c for c in failed if not (c.get("unverified") and confirm_no_copied_text)]
        if blocking:
            raise BriefError("確認に通っていない: " + "; ".join(f"{c['name']}（{c['detail']}）" for c in blocking))
        if not confirm_shareable:
            raise BriefError("社内に出してよい内容（秘密情報・個人情報・社内の機密を含まない）であることを、承認する人が確かめて --confirm-shareable を付ける")
        return _append(root, brief_id, event, actor, sha, {
            "attested": {"shareable": True, "no_copied_text_by": "human" if any(c.get("unverified") for c in failed) else "照合"},
            "review_notes": b.review_notes(), "reason": reason}, now)
    raise BriefError(f"未知の操作: {event}")


def record_post(root: Path, brief_id: str, actor: tuple[str, str], *, where: str, reference: str = "", detail_location: str = "",
                now: dt.datetime | None = None) -> dict[str, Any]:
    """人が Teams に投稿した事実を記録する（この基盤は送信しない）。同じ内容の、同じ投稿先への二重の記録は拒否する。"""
    if actor[0] != "human":
        raise BriefError("投稿の記録は、投稿した人が行う（--actor human:名前）")
    b = read(root, brief_id)
    st = state(root, b)
    if st["state"] != "approved":
        raise BriefError(f"承認済みの内容だけを投稿できる（いまは {st['label']}）")
    sha = b.content_sha256()
    if any(e["content_sha256"] == sha and e["detail"].get("where") == where for e in st["posted"]):
        raise BriefError(f"この内容は、すでに「{where}」への投稿として記録されている（重複）")
    return _append(root, brief_id, "posted", actor, sha, {"where": where, "reference": reference, "detail_location": detail_location}, now)


def start_correction(root: Path, brief_id: str, note: str, actor: tuple[str, str], *, today: dt.date | None = None) -> str:
    """訂正版の下書きを作る。元の候補は書き換えない（何が出ていたかを残す）。"""
    b = read(root, brief_id)
    if not note:
        raise BriefError("何を、なぜ訂正するのかを書く（--note）")
    n = 2
    while brief_path(root, f"{brief_id}-r{n}").exists():
        n += 1
    new_id = f"{brief_id}-r{n}"
    data = b.model_dump(mode="json") | {"id": new_id, "corrects": brief_id, "correction_note": note,
                                        "updated_on": (today or dt.date.today()).isoformat()}
    path = brief_path(root, new_id)
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8", newline="\n")
    _append(root, brief_id, "correction_opened", actor, b.content_sha256(), {"correction": new_id, "note": note})
    return new_id


# ---------------------------------------------------------------------- 感想の記録

FEEDBACK_FIELDS = {
    "understood": "何を伝える投稿かが分かったか（yes / partly / no）",
    "relevant": "自分の仕事との関係が分かったか（yes / partly / no）",
    "status_clear": "確かめたことと、確かめていないことの区別が分かったか（yes / partly / no）",
    "reached_detail": "必要な詳細へ進めたか（yes / no / not_needed）",
    "helped_decision": "比較や選定の助けになったか（yes / partly / no / not_applicable）",
    "tried": "試せたか（yes / blocked / no / not_applicable）",
    "too_much": "自分に関係のない情報や通知が多いと感じたか（yes / no）",
}
_ANSWERS = {"yes", "partly", "no", "not_needed", "not_applicable", "blocked"}


def add_feedback(root: Path, brief_id: str, *, reader_group: str, recorded_by: tuple[str, str], answers: dict[str, str], note: str = "",
                 blocked_at: str = "", now: dt.datetime | None = None) -> dict[str, Any]:
    """小規模の試行での感想を 1 件記録する。答えは任意。個人を特定する書き方は受け付けない。"""
    b = read(root, brief_id)
    bad = [k for k in answers if k not in FEEDBACK_FIELDS] + [f"{k}={v}" for k, v in answers.items() if v not in _ANSWERS]
    if bad:
        raise BriefError("受け付けない項目か値: " + "、".join(bad))
    for label, text in (("読者の区分", reader_group), ("感想", note), ("止まった所", blocked_at)):
        if _PERSONAL.search(text):
            raise BriefError(f"{label}に、個人を特定する書き方（メールアドレス、@ での名指し）を入れない")
    if not reader_group.strip():
        raise BriefError("読者の区分を書く（例: 開発の担当。個人の名前は書かない）")
    row = {"at": (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"), "brief": brief_id, "content_sha256": b.content_sha256(),
           "purpose": b.purpose, "reader_group": reader_group.strip(), "answers": answers, "blocked_at": blocked_at, "note": note,
           "recorded_by_kind": recorded_by[0], "recorded_by": recorded_by[1]}
    with (root / "feedback.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def feedback(root: Path, brief_id: str = "") -> list[dict[str, Any]]:
    path = root / "feedback.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
    return [r for r in rows if not brief_id or r["brief"] == brief_id]


def feedback_summary(root: Path, brief_id: str = "") -> dict[str, Any]:
    """感想の件数を、項目ごとに数える。点数にはまとめない（少数の感想から、有用性を数値で言わない）。"""
    rows = feedback(root, brief_id)
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for k, v in r["answers"].items():
            counts.setdefault(k, {}).setdefault(v, 0)
            counts[k][v] += 1
    return {"n": len(rows), "caution": "少数の感想。傾向の手がかりで、有用性の証明ではない。反応の数から、理解や業務の成果は推定しない",
            "counts": counts, "blocked_at": [r["blocked_at"] for r in rows if r["blocked_at"]], "notes": [r["note"] for r in rows if r["note"]]}
