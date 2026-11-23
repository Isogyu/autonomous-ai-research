"""研究エンジン（内蔵）: 研究理解 → 設計・事前登録 → 実装 → 予備実験 → 本実験 → 分析。

事前登録つきの仮説検証を行う。AI Scientist v3 は「評価関数の点数を上げるプログラムを探す」型の
研究に向いており、この型（仮説を先に固定して検証する）は扱わないため、こちらを既定にしている（ADR-0003）。

各工程は「エージェントに考えさせる部分」と「オーケストレーターが機械的に行う部分」に分ける。
  * エージェント: 設計、コードの作成、結果の批判的な検討
  * オーケストレーター: 事前登録の固定、隔離実行、判定規則の適用結果の採否、継続の判断
エージェントが仮説の判定を「支持」へ引き上げることはできない（引き下げだけ可能）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aar import workspace as wsmod
from aar.agents.runner import EDIT_TOOLS, READ_ONLY_TOOLS, AgentRunner, wrap_untrusted
from aar.config import Policy
from aar.models import AgentResult
from aar.sandbox import RunResult, run_in_sandbox


class PhaseError(Exception):
    """工程の失敗。kind で扱いを分ける（auth / rate_limit / budget は保留、他は再試行または失敗）。"""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Ctx:
    project_id: str
    ws: Path
    policy: Policy
    runners: dict[str, AgentRunner]
    on_cost: Callable[[str, str, AgentResult], None]     # (purpose, key, result) を費用台帳へ
    remaining_budget: Callable[[], float]
    heartbeat: Callable[[], None] | None = None
    packages: list[str] = field(default_factory=lambda: ["numpy"])   # 実験コードから使えるライブラリ

    async def ask(self, role: str, purpose: str, key: str, prompt: str, *, schema: dict[str, Any] | None = None,
                  writable: bool = False, budget: float = 2.0, timeout_s: int | None = None,
                  cwd: Path | None = None) -> AgentResult:
        """役割に応じたエージェントを1回呼ぶ。費用を記録し、失敗は PhaseError にする。"""
        remaining = self.remaining_budget()
        if remaining <= 0.01:
            raise PhaseError("研究の費用上限に達した", "budget")
        choice = self.policy.research.model_for(role)
        runner = self.runners[choice.agent]
        res = await runner.run(
            prompt, cwd=cwd or self.ws, model=choice.model, schema=schema,
            tools=EDIT_TOOLS if writable else READ_ONLY_TOOLS, writable=writable,
            max_budget_usd=min(budget, remaining),
            timeout_s=timeout_s or self.policy.research.phase_timeout_minutes * 60, heartbeat=self.heartbeat)
        self.on_cost(purpose, key, res)
        if not res.ok:
            raise PhaseError(f"{purpose}: {res.error}", res.error_kind or "other")
        return res


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props)}


_S = {"type": "string"}
_SL = {"type": "array", "items": _S}

UNDERSTAND_SCHEMA = _obj({
    "summary": _S, "research_problem": _S, "claims_in_paper": _SL, "limitations_of_prior_work": _SL,
    "open_questions": _SL, "related_work": _SL,
    "basis": {"type": "string", "enum": ["abstract_only", "fulltext"]},
})

DESIGN_SCHEMA = _obj({
    "research_question": _S,
    "hypotheses": {"type": "array", "items": _obj({"label": _S, "statement": _S, "decision_rule": _S})},
    "primary_metric": _S, "secondary_metrics": _SL, "baselines": _SL, "conditions": _SL,
    "datasets": {"type": "array", "items": _obj({"name": _S, "source": _S, "license": _S, "note": _S})},
    "data_split": _S,
    "sample_size": _obj({"pilot": {"type": "integer"}, "main": {"type": "integer"}, "rationale": _S}),
    "statistical_analysis": _S, "success_failure_criteria": _S, "exploratory_plan": _S,
    "seeds": {"type": "array", "items": {"type": "integer"}},
    "est_runtime_minutes": {"type": "number"}, "threats_to_validity": _SL,
    "value_if_hypotheses_fail": _S,
})

IMPLEMENT_SCHEMA = _obj({"files_written": _SL, "notes": _S, "deviations_from_prereg": _SL})

CRITIQUE_SCHEMA = _obj({
    "hypothesis_reviews": {"type": "array", "items": _obj({
        "label": _S, "status": {"type": "string", "enum": ["supported", "not_supported", "inconclusive"]}, "reason": _S})},
    "observations": _SL, "exploratory_findings": _SL, "limitations": _SL, "alternative_explanations": _SL,
    "lessons": {"type": "array", "items": _obj({"category": _S, "content": _S})},
    "next": _obj({"continue": {"type": "boolean"}, "expected_information_gain": {"type": "number"}, "rationale": _S,
                  "next_hypotheses": {"type": "array", "items": _obj({"label": _S, "statement": _S, "decision_rule": _S})},
                  "next_design_changes": _S}),
})


def experiment_contract(packages: list[str]) -> str:
    return f"""## 実験コードの決まり（オーケストレーターがこの形で実行する）
- 実行環境: ネットワークなしの隔離環境、Python 3.11 以上。使えるライブラリは標準ライブラリと {", ".join(packages)} だけ。
  外部 API・モデルのダウンロード・LLM の呼び出しはできない。データは `data/` にあるものか、コードで生成するものを使う。
- `experiments/run.py --stage pilot|main --out <dir>`
  - `<dir>/raw.jsonl`（試行ごとの生データ。後から独立に再計算できる粒度で）と `<dir>/run_meta.json`
    （シード、件数、所要時間、`prereg/cycleN.json` の SHA-256）を書く。集計値だけを保存してはいけない。
  - pilot は事前登録の pilot 件数で1分以内に終わること。main は事前登録の件数・シード・条件どおりに行う。
- `experiments/analyze.py --in <main の dir> --out <dir>`
  - `<dir>/analysis.json` を書く: {{"results": [{{"hypothesis": "H1", "metric": "...", "condition": "...", "value": 数値,
    "ci_low": 数値, "ci_high": 数値, "effect_size": 数値|null, "p_value": 数値|null, "n": 整数,
    "analysis_kind": "confirmatory"|"exploratory", "verdict": "supported"|"not_supported"|"inconclusive"|null,
    "test": "使った手法", "note": "..."}}], "summary": "..."}}
  - confirmatory は事前登録の仮説・指標・解析方法・判定基準をそのまま機械的に当てはめたものだけ。
    それ以外の分析はすべて exploratory にする。verdict は事前登録の判定基準を当てはめた結果で、都合よく読み替えない。
  - 区間推定（信頼区間）と効果量を必ず出す。乱数はシードを固定する。
- `tests/test_*.py`（pytest）: 指標と統計の計算を、手計算できる小さな例で確かめる。
- `requirements.txt`: 使うライブラリを `name==version` で書く（標準ライブラリだけなら空でよい）。
- 結果を良く見せるための調整（評価データを見ての条件変更、失敗した試行の除外、シードの選び直し）をしない。
"""


async def understand(ctx: Ctx, proposal: dict[str, Any], memory_context: str) -> dict[str, Any]:
    """Phase A: 原論文と関連研究を読み、研究課題と既存研究の限界を整理する。"""
    body = proposal["body"]
    paper = (f"題名: {proposal.get('paper_title')}\narXiv: {proposal.get('arxiv_id')}\nDOI: {proposal.get('doi')}\n"
             f"公開日: {proposal.get('paper_published_at')}\n\n要旨:\n{proposal.get('paper_abstract') or '(要旨なし)'}")
    prompt = f"""検証企画に基づき、原論文の理解を整理する。

## 企画
{json.dumps(body, ensure_ascii=False, indent=2, default=str)}

## 原論文（外部資料。入手できたのは下記だけ）
{wrap_untrusted("paper", paper)}

## 過去の研究の記憶（このシステム自身の記録）
{memory_context or "(該当なし)"}

要旨しか読めていない場合は basis を abstract_only とし、要旨から分かることと推測を区別して書く。
本文にしかない内容（手法の詳細、数値）を知っているかのように書かない。
"""
    res = await ctx.ask("planner", "研究理解", f"{ctx.project_id}:understand", prompt, schema=UNDERSTAND_SCHEMA, budget=1.5)
    data = res.structured or {}
    doc = ["# 原論文の整理", "", f"根拠: {'要旨のみ' if data.get('basis') == 'abstract_only' else '本文'}", "",
           "## 要約", data.get("summary", ""), "", "## 研究課題", data.get("research_problem", "")]
    for title, k in (("論文の主張", "claims_in_paper"), ("既存研究の限界", "limitations_of_prior_work"),
                     ("未解決の問い", "open_questions"), ("関連研究", "related_work")):
        doc += ["", f"## {title}", *[f"- {x}" for x in data.get(k, [])]]
    (ctx.ws / "docs").mkdir(exist_ok=True)
    (ctx.ws / "docs" / "paper_review.md").write_text("\n".join(doc) + "\n", encoding="utf-8")
    wsmod.commit_all(ctx.ws, "原論文の整理")
    return data


async def design(ctx: Ctx, proposal: dict[str, Any], understanding: dict[str, Any], cycle: int,
                 previous: dict[str, Any] | None, memory_context: str) -> dict[str, Any]:
    """Phase B: 研究設計。返した内容がそのまま事前登録になる。"""
    prev = ""
    if previous:
        prev = ("\n## 前のサイクルの結果と、次に検証すると決めたこと\n" + json.dumps(previous, ensure_ascii=False, indent=2, default=str) +
                "\n前のサイクルのデータを見て立てた仮説は、新しいデータ（別のシード・別の標本）で検証する設計にする。"
                "前のデータの再分析で済ませる場合は仮説検証とは呼ばず、exploratory_plan に書く。\n")
    prompt = f"""検証企画の研究設計を作る。この設計は実験の前に固定され（事前登録）、後から変更できない。

## 企画
{json.dumps(proposal["body"], ensure_ascii=False, indent=2, default=str)}

## 原論文の整理
{json.dumps(understanding, ensure_ascii=False, indent=2, default=str)}
{prev}
## 過去の研究の記憶
{memory_context or "(該当なし)"}

{experiment_contract(ctx.packages)}

## 設計の条件
- 仮説は反証できる形で書き、decision_rule に「どの指標がどの値・区間になれば支持／不支持／判定保留か」を数値で書く。
- 期待どおりでない結果になっても意味が残る設計にする（value_if_hypotheses_fail に書く）。結論ありきにしない。
- 個人の PC（CPU のみ、メモリ 8GB）で main が {ctx.policy.safety.experiment_timeout_minutes} 分以内に終わる規模にする。
- 件数の根拠（検出したい差と、その差を見分けられる見込み）を sample_size.rationale に書く。
- データが外部のものなら出所とライセンスを書く。ネットワークなしで用意できないデータは使わない。
- 仮説のラベルは H1, H2, ... とする。サイクル {cycle} の設計である。
"""
    res = await ctx.ask("planner", "研究設計", f"{ctx.project_id}:design:{cycle}", prompt, schema=DESIGN_SCHEMA, budget=2.0)
    d = res.structured or {}
    if not d.get("hypotheses"):
        raise PhaseError("研究設計に仮説がない")
    d["cycle"] = cycle
    return d


async def implement(ctx: Ctx, prereg: dict[str, Any], cycle: int, attempt: int, feedback: str = "") -> dict[str, Any]:
    """Phase D: 実験コード・分析コード・テストを書く。実行はしない（実行は隔離環境で別に行う）。"""
    fb = f"\n## 前回の実行で起きた問題（修正すること）\n{wrap_untrusted('execution_log', feedback)}\n" if feedback else ""
    prompt = f"""事前登録（`{wsmod.prereg_path(cycle)}`）に従って、実験を実装する。作業ディレクトリはこの研究のリポジトリである。

## 事前登録
{json.dumps(prereg, ensure_ascii=False, indent=2, default=str)}

{experiment_contract(ctx.packages)}
{fb}
## 守ること
- `prereg/` 以下のファイルは変更しない（変更すると研究は無効になる）。
- 事前登録から外れる必要が生じたら、勝手に変えずに deviations_from_prereg に理由つきで書く。
- あなたはコードを実行できない。実行はオーケストレーターが行い、失敗したらログが返ってくる。
- 既存のファイルがあれば読んでから直す。サイクル {cycle} の出力先は `results/cycle{cycle}/` 以下。
"""
    res = await ctx.ask("implementer", "実装", f"{ctx.project_id}:implement:{cycle}:{attempt}", prompt,
                        schema=IMPLEMENT_SCHEMA, writable=True, budget=3.0)
    for required in ("experiments/run.py", "experiments/analyze.py"):
        if not (ctx.ws / required).exists():
            raise PhaseError(f"実装が不完全: {required} がない")
    wsmod.commit_all(ctx.ws, f"実験の実装（サイクル{cycle}、{attempt}回目）")
    return res.structured or {}


async def run_tests(ctx: Ctx) -> RunResult:
    return await run_in_sandbox(["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], workspace=ctx.ws,
                                safety=ctx.policy.safety, timeout_s=600, heartbeat=ctx.heartbeat)


async def run_stage(ctx: Ctx, cycle: int, stage: str) -> RunResult:
    """Phase E / F: 予備実験または本実験を隔離環境で実行し、ログを保存する。"""
    out = f"results/cycle{cycle}/{stage}"
    (ctx.ws / out).mkdir(parents=True, exist_ok=True)
    timeout = 120 if stage == "pilot" else None
    r = await run_in_sandbox(["experiments/run.py", "--stage", stage, "--out", out], workspace=ctx.ws,
                             safety=ctx.policy.safety, timeout_s=timeout, heartbeat=ctx.heartbeat)
    (ctx.ws / out / "run.log").write_text(f"exit={r.returncode} duration={r.duration_s:.1f}s sandbox={r.sandbox}\n"
                                          f"--- stdout ---\n{r.stdout[-20000:]}\n--- stderr ---\n{r.stderr[-20000:]}\n",
                                          encoding="utf-8")
    if r.ok and not (ctx.ws / out / "raw.jsonl").exists():
        r = r.model_copy(update={"ok": False, "stderr": r.stderr + f"\n{out}/raw.jsonl が作られていない"})
    return r


async def run_analysis(ctx: Ctx, cycle: int) -> tuple[RunResult, dict[str, Any]]:
    """Phase G（機械的な部分）: 分析コードを実行し、analysis.json を読む。"""
    src, out = f"results/cycle{cycle}/main", f"results/cycle{cycle}/analysis"
    (ctx.ws / out).mkdir(parents=True, exist_ok=True)
    r = await run_in_sandbox(["experiments/analyze.py", "--in", src, "--out", out], workspace=ctx.ws,
                             safety=ctx.policy.safety, timeout_s=900, heartbeat=ctx.heartbeat)
    path = ctx.ws / out / "analysis.json"
    data: dict[str, Any] = {}
    if r.ok:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data.get("results"), list):
                raise ValueError("results がリストでない")
        except (OSError, ValueError) as e:
            r = r.model_copy(update={"ok": False, "stderr": r.stderr + f"\nanalysis.json を読めない: {e}"})
    return r, data


async def critique(ctx: Ctx, prereg: dict[str, Any], analysis: dict[str, Any], cycle: int) -> dict[str, Any]:
    """Phase G（判断の部分）: 結果を批判的に検討し、次に何を検証する価値があるかを述べる。"""
    prompt = f"""実験結果を批判的に検討する。あなたの役目は結果を良く見せることではなく、結論が証拠を超えていないか確かめることである。

## 事前登録（サイクル {cycle}）
{json.dumps(prereg, ensure_ascii=False, indent=2, default=str)}

## 分析結果（分析コードが事前登録の判定基準を機械的に当てはめたもの）
{wrap_untrusted("analysis.json", json.dumps(analysis, ensure_ascii=False, indent=2, default=str)[:30000])}

必要なら作業ディレクトリの `results/cycle{cycle}/` と `experiments/` を読んで確かめる。

- hypothesis_reviews: 各仮説について、分析結果の verdict が事前登録の判定基準と証拠に照らして妥当かを述べる。
  証拠が足りなければ inconclusive にする。不支持を支持に読み替えない。
- 事前登録にない分析から分かったことは exploratory_findings に書き、仮説検証の結果と混ぜない。
- alternative_explanations: 実装の誤り、データの偏り、偶然など、別の説明を挙げる。
- next.continue: 追加の実験で新しく分かることが大きい場合だけ true。同じ仮説を「支持されるまで」やり直す提案はしない。
  expected_information_gain は 0〜1 で、追加実験の結果がどちらに転んでも判断が変わる度合い。
- lessons: 次の研究で再利用できる具体的な学び（有効だった設計、失敗した評価方法、費用や時間の実績）。
"""
    res = await ctx.ask("planner", "批判的分析", f"{ctx.project_id}:critique:{cycle}", prompt, schema=CRITIQUE_SCHEMA, budget=2.0)
    return res.structured or {}


_RANK = {"supported": 2, "inconclusive": 1, "not_supported": 0}


def final_verdicts(analysis: dict[str, Any], critique_out: dict[str, Any], labels: list[str]) -> dict[str, str]:
    """仮説ごとの最終判定。分析コードの判定を基準にし、批判的検討は引き下げだけできる。

    分析に confirmatory の判定がない仮説は、検証されていないので inconclusive とする。
    """
    mech: dict[str, str] = {}
    for r in analysis.get("results", []):
        if r.get("analysis_kind") == "confirmatory" and r.get("verdict") in _RANK and r.get("hypothesis") in labels:
            h = r["hypothesis"]
            # 同じ仮説に複数の判定があれば最も弱いものを採る
            mech[h] = min(mech.get(h, r["verdict"]), r["verdict"], key=lambda v: _RANK[v])
    review = {h["label"]: h["status"] for h in critique_out.get("hypothesis_reviews", []) if h.get("status") in _RANK}
    out: dict[str, str] = {}
    for label in labels:
        m = mech.get(label, "inconclusive")
        rv = review.get(label, m)
        if m == "supported" and rv != "supported":
            out[label] = rv            # 引き下げ
        elif m == "not_supported":
            out[label] = "not_supported"
        else:
            out[label] = m if _RANK[rv] >= _RANK[m] else rv
    return out


def decide_continue(policy: Policy, cycle: int, critique_out: dict[str, Any], budget_ratio_spent: float,
                    elapsed_hours: float) -> tuple[bool, str]:
    """追加サイクルに進むかを、事前に決めた基準だけで判断する。"""
    r = policy.research
    nxt = critique_out.get("next") or {}
    if not nxt.get("continue"):
        return False, "研究側が追加実験の価値は小さいと判断した"
    if not nxt.get("next_hypotheses"):
        return False, "次に検証する仮説が示されていない"
    if cycle >= r.max_cycles:
        return False, f"サイクル数の上限（{r.max_cycles}）に達した"
    if budget_ratio_spent >= r.budget_soft_ratio:
        return False, f"費用が上限の {budget_ratio_spent:.0%} に達した（追加実験を止める基準は {r.budget_soft_ratio:.0%}）"
    if 1 - budget_ratio_spent < r.continue_if.get("min_remaining_budget_ratio", 0.0):
        return False, "残りの費用が追加サイクルの基準に満たない"
    if elapsed_hours >= r.max_wallclock_hours:
        return False, f"所要時間の上限（{r.max_wallclock_hours} 時間）に達した"
    gain = float(nxt.get("expected_information_gain") or 0)
    need = r.continue_if.get("min_expected_information_gain", 0.5)
    if gain < need:
        return False, f"追加実験で分かることの見込み（{gain:.2f}）が基準（{need}）に満たない"
    return True, f"追加実験の価値あり（見込み {gain:.2f}）: {nxt.get('rationale', '')}"
