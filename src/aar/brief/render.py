"""投稿候補から、図解カード・投稿の本文・代替テキスト・詳細ページ・プレビューを組み立てる。

どれも、投稿候補のデータを決まった手順で並べるだけで、文章を作り直さない（モデルを呼ばない、画像生成を使わない）。
  * 図解カード: SVG のテンプレート。幅は 1024 で固定、高さは内容で決まり、1024 を超える内容は「収まらない」として拒否する。
    文字の大きさには下限がある。条件・制約は、カードの本体に、ほかの文字と同じ大きさで入れる（注記に追いやらない）。
  * 投稿の本文: 画像を見られなくても、要点・検証の状況・制約・出典が分かる文章。図の内容も文章で入れる。
  * Teams の Adaptive Card の JSON: **形を確かめるための下書き**。送信は実装していない。Teams での表示は確かめていない。
  * 詳細ページ: Markdown。保存先が決まっていないあいだは、ローカルのファイルとして出すだけで、URL は書かない。
"""

from __future__ import annotations

import html
import json
from typing import Any

from aar.brief.model import PURPOSE_GAIN, PURPOSE_LABEL, VERIFICATION_LABEL, Brief

CARD_W, CARD_MAX_H, PAD = 1024, 1024, 56
MIN_FONT = 32                     # 幅 360 の画面に縮小されても 11px 相当を保つ下限（設計上の提案。実機での確認が要る）
ADAPTIVE_CARD_VERSION = "1.5"     # Teams は 1.6 までを表示できる（公式の文書。2026-11-25 に確認）
TEAMS_WEBHOOK_LIMIT_BYTES = 28 * 1024

# 色は 1 か所で決める。文字と背景の明暗の比は、テストで 4.5:1 以上を確かめる
INK, SUB, BG, LINE, ACCENT, PANEL, WARN_BG, WARN_INK = "#111111", "#454545", "#ffffff", "#8a8a8a", "#0b4f9e", "#eef2f7", "#fff1c2", "#4a3200"
# 検証の状況の表示。色だけに頼らず、必ず文字で書き、枠の形も変える
BADGE = {
    "self_verified": {"fill": ACCENT, "ink": "#ffffff", "stroke": ACCENT, "dash": ""},
    "third_party_reported": {"fill": BG, "ink": ACCENT, "stroke": ACCENT, "dash": ""},
    "introduced_unreproduced": {"fill": BG, "ink": INK, "stroke": INK, "dash": "10 6"},
    "illustrative": {"fill": WARN_BG, "ink": WARN_INK, "stroke": WARN_INK, "dash": ""},
}


def vis(text: str) -> float:
    """描いたときの幅の見積もり（全角 2 を単位）。英数字は、太字やフォントの違いを見込んで、半角 1 より広く数える。

    フォントは環境で変わるので、実際の幅は測れない。はみ出すより、余るほうへ倒す。
    """
    return sum(2.0 if ord(ch) > 0x2E7F else (0.7 if ch == " " else 1.3) for ch in text)


class DoesNotFit(ValueError):
    """カードに収まらない。文字を小さくせず、内容を減らす。"""


def wrap(text: str, units: int) -> list[str]:
    """表示幅（全角 2、半角 1）で折り返す。半角の語の途中では、できるだけ切らない。"""
    lines, cur, cur_w = [], "", 0
    token = ""
    def flush_token() -> None:
        nonlocal cur, cur_w, token
        tw = vis(token)
        if cur and cur_w + tw > units:
            lines.append(cur.rstrip())
            cur, cur_w = "", 0
        while vis(token) > units:                         # 1 語が 1 行より長い
            cut, used = "", 0.0
            for ch in token:
                if used + vis(ch) > units:
                    break
                cut, used = cut + ch, used + vis(ch)
            lines.append(cut)
            token = token[len(cut):]
        cur, cur_w = cur + token, cur_w + vis(token)
        token = ""
    for ch in text:
        if ord(ch) > 0x2E7F or ch == " ":
            flush_token()
            if cur_w + vis(ch) > units and ch not in "。、）」":      # 句読点と閉じ括弧は、行の頭に送らない
                lines.append(cur.rstrip())
                cur, cur_w = "", 0
            if not (ch == " " and not cur):
                cur, cur_w = cur + ch, cur_w + vis(ch)
        else:
            token += ch
    flush_token()
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


def _units(px_width: float, font: int) -> int:
    return int(px_width / (font / 2))


def _text(x: float, y: float, s: str, font: int, *, fill: str = INK, weight: int = 400, anchor: str = "start") -> str:
    if font < MIN_FONT:
        raise DoesNotFit(f"文字の大きさが下限（{MIN_FONT}）を下回る")
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-size="{font}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">'
            f"{html.escape(s)}</text>")


def _badge(x: float, y: float, label: str, style: dict[str, str], font: int = 34) -> tuple[str, float]:
    w = vis(label) * font / 2 + 36
    dash = f' stroke-dasharray="{style["dash"]}"' if style["dash"] else ""
    return (f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{font + 22}" rx="10" fill="{style["fill"]}" stroke="{style["stroke"]}" '
            f'stroke-width="3"{dash}/>' + _text(x + 18, y + font + 4, label, font, fill=style["ink"], weight=700)), w


def diagram_text(b: Brief) -> list[str]:
    """図の内容を、図と同じデータから文章にする（読み上げ・検索用。図と食い違わない）。"""
    d = b.diagram
    if d.kind == "comparison":
        return [f"{r.label}: " + "、".join(f"{c} は {v}" for c, v in zip(d.columns, r.cells, strict=True)) + (f"（{r.note}）" if r.note else "")
                for r in d.rows]
    mark = {"steps": lambda i: f"{i}. ", "flow": lambda i: ("" if i == 1 else "→ "), "structure": lambda i: "・"}[d.kind]
    return [f"{mark(i)}{r.label}" + (f"（{r.note}）" if r.note else "") for i, r in enumerate(d.rows, 1)]


def card_svg(b: Brief) -> str:
    """図解カード（SVG）。収まらなければ DoesNotFit。"""
    inner = CARD_W - 2 * PAD
    out: list[str] = []
    y = PAD
    # 目的と検証の状況（必ず文字で）
    s1, w1 = _badge(PAD, y, PURPOSE_LABEL[b.purpose], {"fill": INK, "ink": "#ffffff", "stroke": INK, "dash": ""})
    s2, _ = _badge(PAD + w1 + 16, y, VERIFICATION_LABEL[b.verification], BADGE[b.verification])
    out += [s1, s2]
    y += 56 + 26
    # 見出し
    for line in wrap(b.headline, _units(inner, 54)):
        y += 62
        out.append(_text(PAD, y, line, 54, weight=700))
    y += 30
    # 図: 問い
    for line in wrap(b.diagram.question, _units(inner, 36)):
        y += 44
        out.append(_text(PAD, y, line, 36, fill=ACCENT, weight=700))
    y += 18
    d = b.diagram
    if d.kind == "comparison":
        label_w = 330
        col_w = (inner - label_w) / len(d.columns)
        row_h = 70
        too_wide = [c for c in [*d.columns, *(c for r in d.rows for c in r.cells)] if vis(c) * 17 > col_w - 12] \
            + [r.label for r in d.rows if vis(r.label) * 17 > label_w - 12]
        if too_wide:
            raise DoesNotFit("比較の欄に収まらない文字がある（観点を減らすか、言葉を短くする）: " + "、".join(too_wide))
        out.append(f'<rect x="{PAD}" y="{y}" width="{inner}" height="{row_h}" fill="{PANEL}"/>')
        for i, c in enumerate(d.columns):
            out.append(_text(PAD + label_w + col_w * i + col_w / 2, y + 46, c, 34, fill=SUB, weight=700, anchor="middle"))
        y += row_h
        for r in d.rows:
            out.append(_text(PAD + 8, y + 46, r.label, 34, weight=700))
            for i, c in enumerate(r.cells):
                out.append(_text(PAD + label_w + col_w * i + col_w / 2, y + 46, c, 34, anchor="middle"))
            y += row_h
            out.append(f'<line x1="{PAD}" y1="{y}" x2="{PAD + inner}" y2="{y}" stroke="{LINE}" stroke-width="2"/>')
    else:
        for i, r in enumerate(d.rows, 1):
            if d.kind == "flow" and i > 1:
                out.append(_text(PAD + 26, y + 34, "↓", 34, fill=SUB, anchor="middle"))
                y += 42
            h = 64 + (44 if r.note else 0)
            if vis(r.label) * 18 > inner - 84 or vis(r.note) * (MIN_FONT / 2) > inner - 84:
                raise DoesNotFit(f"図の項目が幅に収まらない（言葉を短くする）: {r.label}")
            out.append(f'<rect x="{PAD}" y="{y}" width="{inner}" height="{h}" rx="10" fill="{PANEL}"/>')
            marker = {"steps": str(i), "flow": "", "structure": "・"}[d.kind]
            tx = PAD + 20
            if marker:
                out.append(_text(PAD + 30, y + 46, marker, 36, fill=ACCENT, weight=700, anchor="middle"))
                tx = PAD + 64
            out.append(_text(tx, y + 46, r.label, 36, weight=700))
            if r.note:
                out.append(_text(tx, y + 90, r.note, MIN_FONT, fill=SUB))
            y += h + 12
    y += 24
    # 条件・制約（本体に、同じ大きさの文字で）
    lines = [ln for c in b.constraints for ln in wrap("・" + c, _units(inner - 40, 34))]
    if b.verification == "illustrative":
        lines = ["・数値と結果は架空（表示の確認用）", *lines]
    box_h = 62 + 46 * len(lines) + 14
    out.append(f'<rect x="{PAD}" y="{y}" width="{inner}" height="{box_h}" rx="10" fill="{WARN_BG}" stroke="{WARN_INK}" stroke-width="2"/>')
    out.append(_text(PAD + 20, y + 46, "条件・制約", 34, fill=WARN_INK, weight=700))
    yy = y + 62
    for ln in lines:
        yy += 46
        out.append(_text(PAD + 20, yy - 8, ln, 34, fill=WARN_INK))
    y += box_h + 22
    # 出典と確認日
    src = b.sources[0]
    foot = f"出典: {src.publisher or src.title}（確認 {src.checked_on.isoformat()}）" + (f" ほか {len(b.sources) - 1} 件" if len(b.sources) > 1 else "")
    for line in wrap(foot, _units(inner, MIN_FONT)):
        y += 42
        out.append(_text(PAD, y, line, MIN_FONT, fill=SUB))
    height = y + PAD - 14
    if height > CARD_MAX_H:
        raise DoesNotFit(f"カードに収まらない（高さ {height:.0f} > {CARD_MAX_H}）。文字を小さくせず、図の項目・制約・見出しを減らす")
    alt = html.escape(alt_text(b))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CARD_W} {height:.0f}" width="{CARD_W}" height="{height:.0f}" role="img" '
            f'aria-label="{alt}" font-family="\'Yu Gothic UI\',\'Meiryo\',\'Hiragino Sans\',\'Noto Sans JP\',sans-serif">'
            f"<title>{alt}</title><rect width=\"100%\" height=\"100%\" fill=\"{BG}\"/>" + "".join(out) + "</svg>\n")


def alt_text(b: Brief) -> str:
    """画像の短い説明。詳しい内容は、投稿の本文（図の内容）に文章で入れる。"""
    return f"{PURPOSE_LABEL[b.purpose]}・{VERIFICATION_LABEL[b.verification]}。{b.headline}。図: {b.diagram.question}（内容は本文に文章で記載）"


def _source_line(s: Any) -> str:
    kind = "一次情報" if s.kind == "primary" else "二次情報"
    scope = {"full": "", "partial": "、一部を確認", "abstract_only": "、要旨だけを確認"}[s.read_scope]
    return f"{s.title}（{s.publisher + '、' if s.publisher else ''}{kind}、確認日 {s.checked_on.isoformat()}、対象の版: {s.version or '記載なし'}{scope}）{s.url}"


def post_text(b: Brief, detail_url: str = "") -> str:
    """Teams の投稿の本文。画像を見られなくても、要点・検証の状況・制約・出典が分かるようにする。"""
    lines = [f"【{PURPOSE_LABEL[b.purpose]}｜{VERIFICATION_LABEL[b.verification]}】{b.headline}"]
    if b.verification == "illustrative":
        lines.append(f"※ 表示確認用の架空データ: {b.illustrative_note}")
    if b.corrects:
        lines.append(f"※ 訂正: {b.correction_note}（訂正の対象: {b.corrects}）")
    lines += ["", "要点"] + [f"・{t}" for t in b.takeaways]
    lines += ["", "条件・制約"] + [f"・{c}" for c in b.constraints]
    lines += ["", f"図の内容（{b.diagram.question}）"] + diagram_text(b)
    lines += ["", f"検証の状況: {VERIFICATION_LABEL[b.verification]} — {b.verification_basis}"]
    lines += ["確かめていない点: " + " ／ ".join(b.unconfirmed)]
    lines += ["", f"対象: {b.audience.segment}", "出典"] + [f"・{_source_line(s)}" for s in b.sources]
    lines += ["", f"詳細: {detail_url}" if detail_url else "詳細: 詳細ページの保存先が未定（ローカルのプレビューだけがある）"]
    return "\n".join(lines) + "\n"


def adaptive_card(b: Brief, *, image_url: str = "", detail_url: str = "") -> dict[str, Any]:
    """Teams の Workflows の webhook が受け取る形（Adaptive Card を 1 つ持つ message）の下書き。送信はしない。

    画像は、Teams から取得できる https の場所に置いたときだけ入れる（SVG は使えないので、PNG などに変換したもの）。
    置き場所が決まっていなければ、画像なしで、同じ内容を文字で持つ。
    """
    def block(text: str, **kw: Any) -> dict[str, Any]:
        return {"type": "TextBlock", "text": text, "wrap": True, **kw}

    body: list[dict[str, Any]] = [
        block(f"{PURPOSE_LABEL[b.purpose]}｜{VERIFICATION_LABEL[b.verification]}", weight="Bolder", isSubtle=True),
        block(b.headline, size="Large", weight="Bolder"),
    ]
    if b.verification == "illustrative":
        body.append(block(f"表示確認用の架空データ: {b.illustrative_note}", color="Attention", weight="Bolder"))
    if b.corrects:
        body.append(block(f"訂正: {b.correction_note}（訂正の対象: {b.corrects}）", weight="Bolder"))
    if image_url:
        body.append({"type": "Image", "url": image_url, "altText": alt_text(b), "size": "Stretch"})
    body += [block("\n".join(f"- {t}" for t in b.takeaways)),
             block("条件・制約", weight="Bolder"), block("\n".join(f"- {c}" for c in b.constraints)),
             block(f"図の内容（{b.diagram.question}）", weight="Bolder"), block("\n".join(f"- {t}" for t in diagram_text(b))),
             block(f"検証の状況: {VERIFICATION_LABEL[b.verification]} — {b.verification_basis}"),
             block("確かめていない点: " + " ／ ".join(b.unconfirmed), isSubtle=True),
             block(f"対象: {b.audience.segment}", isSubtle=True)]
    actions = [{"type": "Action.OpenUrl", "title": "一次情報を開く", "url": next(s.url for s in b.sources if s.kind == "primary")}]
    if detail_url:
        actions.insert(0, {"type": "Action.OpenUrl", "title": "詳細を読む", "url": detail_url})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": ADAPTIVE_CARD_VERSION,
            "msteams": {"width": "Full"}, "body": body, "actions": actions}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


def adaptive_card_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def detail_markdown(b: Brief, corrections: list[dict[str, str]] | None = None) -> str:
    """詳細ページ（Markdown）。冒頭に、結論・対象の読者・適用の条件・重要な制約を置く。目的に応じた節だけを出す。"""
    d = b.detail
    def bullets(items: list[str]) -> list[str]:
        return [f"- {x}" for x in items]

    md = [f"# {b.headline}", "", f"**{PURPOSE_LABEL[b.purpose]}**（{PURPOSE_GAIN[b.purpose]}）｜**{VERIFICATION_LABEL[b.verification]}**", ""]
    if b.verification == "illustrative":
        md += [f"> **表示確認用の架空データ。** {b.illustrative_note}", ""]
    if b.corrects:
        md += [f"> **訂正版。** 訂正の対象: `{b.corrects}`。{b.correction_note}", ""]
    md += ["## 結論", "", d.conclusion, "", "## 対象の読者", "", f"{b.audience.segment} — {b.audience.relevance}", "",
           "## 適用の条件", "", *bullets(d.applies_when), "", "## 重要な制約", "", *bullets(b.constraints), "",
           "## 要点", "", *bullets(b.takeaways), ""]
    md += [f"## {b.diagram.question}", "", *bullets(diagram_text(b)), "", b.diagram.long_description, ""]
    if d.overview:
        md += ["## 概要", "", d.overview, ""]
    if d.confirmed_scope:
        md += ["## 確かめた範囲", "", *bullets(d.confirmed_scope), ""]
    if d.comparison_basis:
        md += ["## 比較の条件と根拠", "", *bullets(d.comparison_basis), ""]
    if d.environment:
        md += ["## 必要な環境", "", d.environment, ""]
    if d.steps:
        md += ["## 手順", "", *[f"{i}. {s}" for i, s in enumerate(d.steps, 1)], ""]
    if d.results:
        md += ["## 結果", "", *bullets(d.results), ""]
    if d.failures:
        md += ["## 失敗した例・つまずいた点", "", *bullets(d.failures), ""]
    md += ["## 検証の状況", "", f"{VERIFICATION_LABEL[b.verification]}: {b.verification_basis}", ""]
    if b.evidence:
        e = b.evidence
        md += ["### 自分たちの実測", "", f"- 環境: {e.environment}", f"- 実施日: {e.measured_on.isoformat()}", f"- 結果: {e.result}",
               f"- 記録: {e.record}", "- 手順:", *[f"  {i}. {s}" for i, s in enumerate(e.procedure, 1)],
               "- この実測が確かめていない範囲:", *[f"  - {x}" for x in e.not_covered], ""]
    md += ["## 確かめていない点", "", *bullets(b.unconfirmed), "",
           "## 根拠の強さ", "", f"{ {'strong': '強い', 'moderate': '中程度', 'weak': '弱い'}[b.evidence_strength] }: {b.evidence_strength_note}", "",
           "## 出典", "", *bullets([_source_line(s) + f" — 裏づけている内容: {s.supports}" for s in b.sources]), "",
           "## この情報の位置づけ", "", f"- 既存の情報からの変化: {b.change}", f"- 今知らせる理由: {b.why_now}", "",
           "## 更新と訂正の履歴", "", f"- 更新日: {b.updated_on.isoformat()}"]
    md += [f"- 訂正（{c['at']}）: {c['note']}（`{c['from']}` → `{c['to']}`）" for c in (corrections or [])] or ["- 訂正: なし"]
    return "\n".join(md) + "\n"


_PREVIEW_CSS = """
:root{color-scheme:light dark;--bg:#f5f5f3;--fg:#161616;--sub:#555;--panel:#fff;--line:#c9c9c9;--warn:#fff1c2;--warnfg:#4a3200}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1a;--fg:#eeeeea;--sub:#b9b9b2;--panel:#262624;--line:#4a4a46;--warn:#4a3a00;--warnfg:#ffe9a8}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font-family:'Yu Gothic UI','Meiryo','Hiragino Sans','Noto Sans JP',sans-serif;line-height:1.7}
main{max-width:1100px;margin:0 auto}h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:2rem;border-bottom:1px solid var(--line)}
.note{background:var(--warn);color:var(--warnfg);padding:10px 14px;border-radius:8px}
.frames{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}.frame{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.frame h3{font-size:.85rem;color:var(--sub);margin:0 0 8px}.desk{width:min(100%,600px)}.mob{width:min(100%,336px)}
.frame svg{width:100%;height:auto;display:block;border:1px solid var(--line)}pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;margin:8px 0 0}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:4px 10px;text-align:left;vertical-align:top}
details{margin-top:1rem}code,pre.code{font-family:ui-monospace,Consolas,monospace;font-size:.85rem}.detail{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 20px}
"""


def preview_html(b: Brief, *, state: dict[str, Any], checks: list[dict[str, Any]], corrections: list[dict[str, str]] | None = None) -> str:
    """送信の前に、人が確かめるためのページ（1 ファイル。外部の資源を読み込まない）。"""
    import markdown as md

    esc = html.escape
    svg = card_svg(b)
    text = post_text(b)
    payload = adaptive_card(b)
    rows = "".join(f"<tr><td>{'✓' if c['ok'] else '✗'}</td><td>{esc(c['name'])}</td><td>{esc(c['detail'])}</td></tr>" for c in checks)
    notes = "".join(f"<li>{esc(n)}</li>" for n in b.review_notes()) or "<li>なし</li>"
    need = b.audience.need
    need_ev = {"assumed": "仮定（読者に確かめていない）", "from_records": "既存の記録から", "reader_confirmed": "読者に確かめた"}[need.evidence]
    frame = lambda cls, title: (f'<div class="frame {cls}"><h3>{title}</h3>{svg}<pre>{esc(text)}</pre></div>')  # noqa: E731
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>発信のプレビュー: {esc(b.id)}</title><style>{_PREVIEW_CSS}</style></head><body><main>
<h1>発信のプレビュー: {esc(b.id)}</h1>
<p class="note">これはローカルのプレビュー。<strong>Teams には送信していない。</strong>下の枠は幅を合わせた近似で、Teams の実際の表示ではない
（Teams のデスクトップとモバイルでの表示、画像の取得、Adaptive Card の表示は、社内の環境で確かめる必要がある）。</p>
<h2>レビューする人が見るもの</h2>
<table><tr><th>状態</th><td>{esc(state.get('label', ''))}</td></tr><tr><th>内容のハッシュ</th><td><code>{esc(b.content_sha256()[:16])}</code></td></tr>
<tr><th>配信の扱い</th><td>{esc(b.triage)}: {esc(b.triage_reason)}</td></tr>
<tr><th>対象の読者</th><td>{esc(b.audience.segment)} — {esc(b.audience.relevance)}</td></tr>
<tr><th>読者のニーズ</th><td>{esc(need.who)}は、{esc(need.wants_to)}。それは、{esc(need.so_that)}。<br>根拠: {need_ev} — {esc(need.evidence_note)}</td></tr>
<tr><th>既存の情報からの変化</th><td>{esc(b.change)}</td></tr><tr><th>今知らせる理由</th><td>{esc(b.why_now)}</td></tr>
<tr><th>似た投稿</th><td>{esc('、'.join(b.similar_posts) or 'なし')}（{esc(b.similar_checked)}）</td></tr></table>
<h3>機械的な確認</h3><table><tr><th></th><th>項目</th><th>結果</th></tr>{rows}</table>
<h3>人に見てほしい点</h3><ul>{notes}</ul>
<h2>Teams の投稿（近似）</h2>
<div class="frames">{frame('desk', 'デスクトップの幅（約 600px）')}{frame('mob', 'モバイルの幅（約 336px）')}</div>
<h2>画像の代替テキスト</h2><p>{esc(alt_text(b))}</p>
<h2>詳細ページ（Markdown から表示。保存先は未定）</h2>
<div class="detail">{md.markdown(detail_markdown(b, corrections).replace('&', '&amp;').replace('<', '&lt;'), extensions=['tables'])}</div>
<details><summary>Adaptive Card の JSON の下書き（{adaptive_card_bytes(payload)} バイト。上限は {TEAMS_WEBHOOK_LIMIT_BYTES}。未送信・表示は未確認）</summary>
<pre class="code">{esc(json.dumps(payload, ensure_ascii=False, indent=1))}</pre></details>
</main></body></html>
"""
