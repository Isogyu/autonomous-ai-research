"""分析結果の表と比較グラフ（SVG）を、分析結果の数値からそのまま作る。

数値は LLM を通さず analysis.json から直接組み立てる（報告書の数値が実測と食い違わないようにするため）。
グラフは指標ごとに1枚（軸は1本）。条件ごとの推定値を点、信頼区間を線で示す。
系列は1つなので色は1色で凡例は置かない。値は文字色で直接書き、同じ内容の表を必ず併記する。
"""

from __future__ import annotations

import html
import math
from typing import Any

_STYLE = """<style>
.bg{fill:#fcfcfb}.ink{fill:#0b0b0b}.sub{fill:#52514e}.mark{fill:#2a78d6;stroke:#fcfcfb}.ci{stroke:#2a78d6}
.grid{stroke:#52514e;stroke-opacity:.18}.zero{stroke:#52514e;stroke-opacity:.6}
text{font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
@media (prefers-color-scheme:dark){.bg{fill:#1a1a19}.ink{fill:#f0efec}.sub{fill:#c3c2b7}
.mark{fill:#3987e5;stroke:#1a1a19}.ci{stroke:#3987e5}.grid,.zero{stroke:#c3c2b7}}
</style>"""


def fmt(v: Any, digits: int = 4) -> str:
    """数値を有効数字4桁で書く。値がなければ「—」。"""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == 0:
        return "0"
    return f"{f:.{digits}g}"


_VERDICT = {"supported": "支持", "not_supported": "不支持", "inconclusive": "判定保留"}


def results_table(results: list[dict[str, Any]], kind: str) -> str:
    """分析結果の Markdown 表。kind は confirmatory（事前登録の検証）か exploratory（探索的分析）。"""
    rows = [r for r in results if (r.get("analysis_kind") == "confirmatory") == (kind == "confirmatory")]
    if not rows:
        return "（該当なし）"
    head = "| 仮説 | 指標 | 条件 | 推定値 | 95%区間 | 効果量 | p値 | n | 手法 |"
    sep = "|---|---|---|---:|---|---:|---:|---:|---|"
    if kind == "confirmatory":
        head, sep = head + " 判定 |", sep + "---|"
    out = [head, sep]
    for r in rows:
        ci = f"{fmt(r.get('ci_low'))} 〜 {fmt(r.get('ci_high'))}" if r.get("ci_low") is not None else "—"
        line = (f"| {r.get('hypothesis') or '—'} | {_cell(r.get('metric'))} | {_cell(r.get('condition'))} | {fmt(r.get('value'))} "
                f"| {ci} | {fmt(r.get('effect_size'))} | {fmt(r.get('p_value'))} | {fmt(r.get('n'))} | {_cell(r.get('test'))} |")
        if kind == "confirmatory":
            line += f" {_VERDICT.get(r.get('verdict') or '', '—')} |"
        out.append(line)
    return "\n".join(out)


def _cell(v: Any) -> str:
    return str(v or "—").replace("|", "／").replace("\n", " ")


def _fit(text: str, units: int) -> str:
    """表示幅に収まるよう切り詰める（全角は2、半角は1と数える）。"""
    used, out = 0, ""
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > units - 1:
            return out + "…"
        used, out = used + w, out + ch
    return out


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=10) * mag
    # 両端の目盛りがデータの範囲を必ず含むようにする（区間の線が描画領域からはみ出さない）
    first, last = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
    return [round(i * step, 12) for i in range(first, last + 1)]


def interval_chart(metric: str, rows: list[dict[str, Any]], *, width: int = 720) -> str:
    """1つの指標について、条件ごとの推定値と信頼区間を横向きに並べた SVG。"""
    rows = [r for r in rows if isinstance(r.get("value"), int | float)]
    if not rows:
        return ""
    label_w, right_w, row_h, top, bottom = 220, 150, 34, 44, 38
    vals = [float(r["value"]) for r in rows]
    los = [float(r["ci_low"]) if isinstance(r.get("ci_low"), int | float) else v for r, v in zip(rows, vals, strict=True)]
    his = [float(r["ci_high"]) if isinstance(r.get("ci_high"), int | float) else v for r, v in zip(rows, vals, strict=True)]
    lo, hi = min(los), max(his)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.1 or 1.0
    ticks = _nice_ticks(lo - pad, hi + pad)
    x0, x1 = ticks[0], ticks[-1]
    plot_w = width - label_w - right_w
    height = top + row_h * len(rows) + bottom

    def x(v: float) -> float:
        return label_w + (v - x0) / (x1 - x0) * plot_w

    e = html.escape
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
             f'role="img" aria-label="{e(metric)}: 条件ごとの推定値と95%区間">', _STYLE,
             f'<rect class="bg" width="{width}" height="{height}"/>',
             f'<text class="ink" x="16" y="26" font-size="15" font-weight="600">{e(metric)}</text>']
    base_y = top + row_h * len(rows)
    for t in ticks:
        cls = "zero" if t == 0 else "grid"
        parts.append(f'<line class="{cls}" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{base_y}" stroke-width="1"/>')
        parts.append(f'<text class="sub" x="{x(t):.1f}" y="{base_y + 18}" font-size="11" text-anchor="middle">{fmt(t, 3)}</text>')
    for i, r in enumerate(rows):
        y = top + row_h * i + row_h / 2
        name = str(r.get("condition") or r.get("hypothesis") or "")
        short = _fit(name, 30)
        ci_tip = f"95%区間 {fmt(los[i])} 〜 {fmt(his[i])}、" if his[i] > los[i] else "区間なし、"
        tip = f"{name}: {fmt(vals[i])}（{ci_tip}n={fmt(r.get('n'))}）"
        parts.append(f'<g><title>{e(tip)}</title>'
                     f'<rect x="0" y="{y - row_h / 2:.1f}" width="{width}" height="{row_h}" fill="transparent"/>'
                     f'<text class="ink" x="{label_w - 12}" y="{y + 4:.1f}" font-size="12" text-anchor="end">{e(short)}</text>')
        if his[i] > los[i]:
            parts.append(f'<line class="ci" x1="{x(los[i]):.1f}" x2="{x(his[i]):.1f}" y1="{y:.1f}" y2="{y:.1f}" '
                         f'stroke-width="2" stroke-linecap="round"/>')
        parts.append(f'<circle class="mark" cx="{x(vals[i]):.1f}" cy="{y:.1f}" r="5" stroke-width="2"/>'
                     f'<text class="ink" x="{width - right_w + 12}" y="{y + 4:.1f}" font-size="12">{fmt(vals[i])}'
                     + (f'<tspan class="sub" font-size="11">  [{fmt(los[i], 3)}, {fmt(his[i], 3)}]</tspan>' if his[i] > los[i] else "")
                     + "</text></g>")
    parts.append(f'<text class="sub" x="{label_w}" y="{height - 6}" font-size="11">点は推定値、線は95%区間</text></svg>')
    return "\n".join(parts)


def charts_for(results: list[dict[str, Any]]) -> dict[str, str]:
    """指標ごとのグラフを作る。{ファイル名: SVG}。事前登録の検証結果を優先し、最大6枚。"""
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(results, key=lambda r: r.get("analysis_kind") != "confirmatory"):
        by_metric.setdefault(str(r.get("metric") or "metric"), []).append(r)
    out: dict[str, str] = {}
    for i, (metric, rows) in enumerate(list(by_metric.items())[:6], start=1):
        svg = interval_chart(metric, rows[:16])
        if svg:
            out[f"fig{i}.svg"] = svg
    return out
