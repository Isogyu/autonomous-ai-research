"""発信用の成果物を作る。

数値の表とグラフはオーケストレーターが分析結果から直接作り、文章だけをエージェントに書かせる。
文章に出てくる数値は、後で分析結果と照合する（check_numbers）。
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

import markdown

from aar import workspace as wsmod
from aar.agents.runner import wrap_untrusted
from aar.engine.native import Ctx, _obj
from aar.publish.charts import charts_for, fmt, results_table

RESULTS_MARK = "<!-- AAR:RESULTS -->"
FIGURES_MARK = "<!-- AAR:FIGURES -->"

STORY = ["実務上の問題提起", "最新研究の紹介", "従来技術との差分", "検証する問い", "独自実験", "実験結果",
         "考察", "実務への示唆", "研究上の限界", "今後の課題"]

WRITE_SCHEMA = _obj({"files_written": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}})

MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def collect_results(ws: Path) -> list[dict[str, Any]]:
    """全サイクルの分析結果を集める（サイクル番号を付ける）。"""
    out: list[dict[str, Any]] = []
    for p in sorted(ws.glob("results/cycle*/analysis/analysis.json")):
        cycle = int(re.search(r"cycle(\d+)", p.as_posix()).group(1))  # type: ignore[union-attr]
        try:
            for r in json.loads(p.read_text(encoding="utf-8")).get("results", []):
                out.append(r | {"cycle": cycle})
        except (OSError, ValueError):
            continue
    return out


def results_block(results: list[dict[str, Any]], verdicts: dict[str, str]) -> str:
    v = {"supported": "支持", "not_supported": "不支持", "inconclusive": "判定保留"}
    lines = ["### 仮説の判定（事前登録の基準による）", ""]
    lines += [f"- **{k}**: {v.get(s, s)}" for k, s in sorted(verdicts.items())] or ["（判定なし）"]
    lines += ["", "### 事前登録した仮説の検証結果", "", results_table(results, "confirmatory"), "",
              "### 探索的分析（仮説の検証ではない。事前登録の判定基準を当てはめていない分析）", "", results_table(results, "exploratory")]
    return "\n".join(lines)


def write_render_tool(ws: Path) -> None:
    """報告書の表と図を分析結果から作り直すスクリプトを tools/ に置く（このモジュールと同じ処理の写し）。"""
    import inspect

    from aar.publish import charts

    tools = ws / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "report_charts.py").write_text(
        "# このファイルは自動生成（自律研究パイプラインの表・図の生成処理の写し）。標準ライブラリだけで動く。\n"
        + inspect.getsource(charts), encoding="utf-8", newline="\n")
    block_src = inspect.getsource(results_block).replace("results_table(", "report_charts.results_table(")
    (tools / "render_report_assets.py").write_text(f'''"""報告書の表（report/results.md）と図（report/figures/）を、分析結果から作り直す。

使い方: python tools/render_report_assets.py
入力: results/cycle*/analysis/analysis.json と report/verdicts.json（事前登録の基準による仮説の判定）
標準ライブラリだけで動く。report/report.md の本文と report.html は作り直さない。
"""
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_charts  # noqa: E402

root = Path(__file__).resolve().parents[1]


{block_src}

results = []
for p in sorted(root.glob("results/cycle*/analysis/analysis.json")):
    cycle = int(re.search(r"cycle(\\d+)", p.as_posix()).group(1))
    results += [r | {{"cycle": cycle}} for r in json.loads(p.read_text(encoding="utf-8")).get("results", [])]
verdicts = json.loads((root / "report" / "verdicts.json").read_text(encoding="utf-8"))
(root / "report" / "figures").mkdir(parents=True, exist_ok=True)
(root / "report" / "results.md").write_text(results_block(results, verdicts) + "\\n", encoding="utf-8")
for name, svg in report_charts.charts_for(results).items():
    (root / "report" / "figures" / name).write_text(svg, encoding="utf-8")
print("report/results.md と report/figures/ を作り直した")
''', encoding="utf-8", newline="\n")


def write_static(ctx: Ctx, verdicts: dict[str, str], *, license_spdx: str, holder: str) -> dict[str, Any]:
    """表・グラフ・ライセンスなど、機械的に作れるものを書く。"""
    results = collect_results(ctx.ws)
    rep = ctx.ws / "report"
    (rep / "figures").mkdir(parents=True, exist_ok=True)
    for stale in (rep / "figures").glob("fig*.svg"):
        stale.unlink()
    figs = charts_for(results)
    for name, svg in figs.items():
        (rep / "figures" / name).write_text(svg, encoding="utf-8", newline="\n")
    (rep / "results.md").write_text(results_block(results, verdicts) + "\n", encoding="utf-8", newline="\n")
    # 表と図を作り直せるよう、判定と、標準ライブラリだけで動く生成スクリプトをリポジトリに入れる
    (rep / "verdicts.json").write_text(json.dumps(verdicts, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    write_render_tool(ctx.ws)
    if license_spdx.upper() == "MIT" and not (ctx.ws / "LICENSE").exists():
        (ctx.ws / "LICENSE").write_text(MIT.format(year=dt.date.today().year, holder=holder), encoding="utf-8", newline="\n")
    return {"n_results": len(results), "figures": sorted(figs)}


async def write_narrative(ctx: Ctx, proposal: dict[str, Any], facts: dict[str, Any], *, round_no: int,
                          feedback: str = "") -> dict[str, Any]:
    """報告書・README・発表原稿・今後の課題を書かせる。"""
    fb = f"\n## 前回の監査の指摘（すべて直すこと）\n{wrap_untrusted('audit_findings', feedback)}\n" if feedback else ""
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(STORY, 1))
    prompt = f"""検証を終えた研究の発信用の資料を書く。作業ディレクトリは研究のリポジトリ。まず `prereg/`、`results/`、
`experiments/`、`docs/`、`report/results.md` を読んで事実を確かめる。

## 読者
AI とシステム開発の基礎を理解している SIer 企業の技術者。基礎用語の解説は要らない。
最新研究の新規性、検証結果、実務への示唆を重視する。

## この研究の事実（オーケストレーターの記録。これと食い違うことを書かない）
{json.dumps(facts, ensure_ascii=False, indent=2, default=str)}

## 企画（検証前に立てたもの。結果ではない）
{wrap_untrusted("proposal", json.dumps(proposal["body"], ensure_ascii=False, indent=2, default=str))}
{fb}
## 書くファイル
1. `report/report.md` — 技術レポート。次の10節をこの順で、節の題名もこのまま使う。
{sections}
   - 「実験結果」の節に `{RESULTS_MARK}` を1行だけ置く（判定と数値の表が自動で入る）。その下に `{FIGURES_MARK}` を1行置く（グラフが入る）。
   - 表にある数値を本文で引用するときは、表の値をそのまま写す。表にない数値を作らない。
2. `README.md` — リポジトリの入口。冒頭に「何を調べたか・結果・分からなかったこと・制約」、続けて
   「再現方法」（依存関係の導入、テスト、予備実験、本実験、分析のコマンド。pytest の版も書く。
   表と図の作り直しは `python tools/render_report_assets.py`。`data/external/MANIFEST.json` があれば、
   先に `python tools/fetch_data.py` で外部データを取得すること）、「構成」、「データ」（外部データを使った場合は
   出所・ライセンス・再配布していないこと。使っていなければ合成データであること）、「研究上の限界」、
   「この研究の作られ方」（下の「作られ方」の事実をそのまま書く）、「引用」、「ライセンス」。
3. `report/talk_script.md` — 10分程度の発表用原稿（話し言葉。スライド見出しごとに区切る）。
4. `report/future_work.md` — 今後の研究課題（今回の結果から具体的に言えるものだけ）。
5. `docs/limitations.md` — 研究上の限界を具体的に（データ、規模、手法、一般化できる範囲）。
6. `docs/references.md` — 参考文献。確認できた書誌情報だけを書く。確認していない文献を挙げない。

## 守ること
- 「この研究の作られ方」は、事実の「作られ方」に書かれていることだけを書く。そこに無いこと（使ったモデルの詳細、
  人が確認した範囲など）を推測で足さない。
- 結論を誇張しない。支持されなかった仮説・判定保留はそのまま書く。「示した」「証明した」は事前登録の基準を満たしたものだけに使う。
- 実測した値と、仮定・推測を文の上で区別する（「実測」「仮定」「推測」と明記する）。
- 事前登録した仮説の検証と、探索的分析を別の段落に分ける。探索的分析から結論を出さない。
- 原論文の内容は、要旨しか読めていないならそう書く。
- 作業用のパス（/Users/…、/home/…、C:\\Users\\…、\\\\サーバー\\共有 など）、API キー、個人名を書かない。
- `prereg/`、`results/`、`experiments/`、`tests/` は変更しない。
"""
    res = await ctx.ask("implementer", "成果物の作成", f"{ctx.project_id}:narrative:{round_no}", prompt,
                        schema=WRITE_SCHEMA, writable=True, budget=3.0)
    return res.structured or {}


def assemble(ws: Path, title: str) -> dict[str, Any]:
    """報告書に表とグラフを差し込み、HTML 版を作る。何度呼んでも同じ結果になる。"""
    rep = ws / "report"
    src = rep / "report.md"
    if not src.exists():
        raise FileNotFoundError("report/report.md がない")
    text = src.read_text(encoding="utf-8")
    block = (rep / "results.md").read_text(encoding="utf-8")
    figs = sorted((rep / "figures").glob("fig*.svg"))
    fig_md = "\n\n".join(f"![図{i}](figures/{f.name})" for i, f in enumerate(figs, 1)) or "（グラフにできる数値の結果がない）"
    begin, end = "<!-- AAR:RESULTS:BEGIN -->", "<!-- AAR:RESULTS:END -->"
    filled = f"{begin}\n{block}\n\n{fig_md}\n{end}"
    if begin in text and end in text:
        text = re.sub(re.escape(begin) + r".*?" + re.escape(end), lambda _: filled, text, flags=re.S)
    elif RESULTS_MARK in text:
        text = text.replace(RESULTS_MARK, filled, 1).replace(FIGURES_MARK, "")
    else:   # 目印が消されていても、結果の表は必ず載せる
        text += f"\n\n## 実験結果（自動集計）\n\n{filled}\n"
    src.write_text(text, encoding="utf-8", newline="\n")

    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc"])
    # HTML は1ファイルで読めるよう、グラフを埋め込む
    for f in figs:
        svg = f.read_text(encoding="utf-8")
        body = re.sub(rf'<img alt="[^"]*" src="figures/{re.escape(f.name)}"\s*/?>', lambda _, s=svg: f"<figure>{s}</figure>", body)
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#f9f9f7;--ink:#0b0b0b;--sub:#52514e;--line:#dcdbd6;--card:#fcfcfb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d0d0d;--ink:#f0efec;--sub:#c3c2b7;--line:#383835;--card:#1a1a19}}}}
body{{background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;
line-height:1.8;margin:0;padding:24px 16px}}
main{{max-width:820px;margin:0 auto}}h1{{font-size:1.6rem;line-height:1.4}}h2{{margin-top:2.2em;border-bottom:1px solid var(--line);padding-bottom:.3em}}
table{{border-collapse:collapse;font-size:.88rem;display:block;overflow-x:auto}}th,td{{border:1px solid var(--line);padding:6px 10px}}
th{{background:var(--card)}}code{{background:var(--card);padding:.1em .3em;border-radius:4px}}pre{{background:var(--card);padding:12px;overflow-x:auto}}
figure{{margin:1.2em 0;overflow-x:auto}}figure svg{{max-width:100%;height:auto}}a{{color:inherit}}
</style></head><body><main>
{body}
</main></body></html>
"""
    (rep / "report.html").write_text(page, encoding="utf-8", newline="\n")
    return {"figures": [f.name for f in figs]}


def copy_audit(ws: Path, audit_dirs: list[Path], third_party_texts: list[tuple[str, str]] | None = None) -> list[str]:
    """独立監査の報告を研究リポジトリの audit/ に写す（公開候補に監査結果を添えるため）。

    third_party_texts（原論文の要旨・本文）を渡すと、それがまとまった長さで写っている行を、写した側でだけ取り除く
    （監査の実行ログには、実行担当が表示したファイルの中身が残る）。取り除いた箇所の一覧を返す。監査の原本は変えない。
    """
    from aar.publish import required as req

    redacted: list[str] = []
    for d in audit_dirs:
        if not d.exists():
            continue
        dest = ws / "audit" / d.parent.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(d, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".home"))
        # 監査の記録には作業用の絶対パスが入るので、公開候補では相対的な表記に置き換える。
        # 毎回、監査の原本から写し直す（公開候補の側で監査記録が書き換えられても元に戻る）
        # 原論文の要旨は第三者の著作物なので、公開候補には写さない（書誌と取得記録だけを残す）
        rec = dest / "source_evidence" / "paper_record.json"
        if rec.exists():
            try:
                data = json.loads(rec.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.pop("要旨", None) is not None:
                    data["要旨"] = "（公開候補には含めていない。原論文の要旨は取得元で読める）"
                    rec.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
            except ValueError:
                rec.unlink()
        swaps = [(str(d.parent / "target"), "<監査対象>"), (str(d), "<監査の作業場所>"), (str(d.parent.parent.parent), "<研究の作業領域>"),
                 (str(Path.home()), "~")]
        swaps += [(str(Path(a).resolve()), b) for a, b in swaps if str(Path(a).resolve()) != a]
        for p in dest.rglob("*"):
            if not p.is_file() or p.stat().st_size > req._MAX_FILE_BYTES:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new = text
            if third_party_texts:
                new, reasons = req.redact_third_party_text(new, third_party_texts)
                redacted += [f"{p.relative_to(ws)}: {r}" for r in reasons]
            for a, b in swaps:
                new = new.replace(a, b)
            new = wsmod.redact_local_paths(new)
            if new != text:
                p.write_text(new, encoding="utf-8", newline="\n")
    return redacted


# ------------------------------------------------------------------ 機械的な確認

_NUM = re.compile(r"(?<![\w.#/\-])(-?\d+\.\d+)(?![\w/]|\.\d)")


def _allowed_numbers(obj: Any, acc: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        acc.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUM.finditer(obj):
            acc.add(float(m.group(1)))
    elif isinstance(obj, dict):
        for v in obj.values():
            _allowed_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _allowed_numbers(v, acc)


def check_numbers(ws: Path, files: list[str], extra_facts: Any = None) -> list[str]:
    """文章中の小数が、分析結果・事前登録・実行記録のどれかに由来するかを確かめる。

    表示の桁に丸めて一致すれば可（百分率と比率の言い換えも可）。由来が分からない数値を返す。
    """
    allowed: set[float] = set()
    for p in [*ws.glob("results/cycle*/analysis/analysis.json"), *ws.glob("prereg/*.json"),
              *ws.glob("results/cycle*/*/run_meta.json"), *ws.glob("results/cycle*/*/metrics.json")]:
        try:
            _allowed_numbers(json.loads(p.read_text(encoding="utf-8")), allowed)
        except (OSError, ValueError):
            continue
    _allowed_numbers(extra_facts, allowed)
    for a in list(allowed):
        allowed.update({a * 100, a / 100, 1 - a, (1 - a) * 100, abs(a)})
    unknown: list[str] = []
    for rel in files:
        path = ws / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"<!-- AAR:RESULTS:BEGIN -->.*?<!-- AAR:RESULTS:END -->", "", text, flags=re.S)
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for line_no, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?i)python|version|バージョン|==|arxiv|doi", line):
                continue
            for m in _NUM.finditer(line):
                tok = m.group(1)
                digits = len(tok.split(".")[1])
                val = float(tok)
                tol = 0.5 * 10 ** (-digits) + 1e-12
                if not any(abs(val - a) <= tol or (a != 0 and abs(val - float(fmt(a))) <= tol) for a in allowed):
                    unknown.append(f"{rel}:{line_no} の {tok}")
    return unknown


def mechanical_publication_checks(ws: Path, source_facts: Any = None) -> dict[str, Any]:
    """公開品質のうち、機械的に確かめられるもの。

    source_facts には原論文の要旨や企画を渡す（そこに書かれた数値の引用を、由来不明と誤って数えないため）。
    """
    readme = (ws / "README.md").read_text(encoding="utf-8") if (ws / "README.md").exists() else ""
    report = (ws / "report" / "report.md").read_text(encoding="utf-8") if (ws / "report" / "report.md").exists() else ""
    secrets = wsmod.scan_secrets(ws)
    missing_sections = [s for s in STORY if not re.search(rf"^#+\s*(?:\d+[.．]\s*)?{re.escape(s)}", report, flags=re.M)]
    from aar import datasets as ds

    return {
        "external_data_problems": ds.verify_manifest(ws),
        "secrets": secrets,
        "has_limitations": bool(re.search(r"^#+.*限界", readme, flags=re.M)) and (ws / "docs" / "limitations.md").exists()
                           and "研究上の限界" not in missing_sections,
        "has_repro": bool(re.search(r"^#+.*再現", readme, flags=re.M)) and "experiments/run.py" in readme,
        "has_license": (ws / "LICENSE").exists(),
        "missing_sections": missing_sections,
        "unknown_numbers": check_numbers(ws, ["report/report.md", "README.md", "report/talk_script.md"], source_facts),
        "deliverables": {p: (ws / p).exists() for p in (
            "report/report.md", "report/report.html", "README.md", "report/talk_script.md", "report/future_work.md",
            "docs/limitations.md", "docs/references.md", "LICENSE")},
    }
