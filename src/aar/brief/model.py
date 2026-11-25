"""社内向けの発信（Teams）の投稿候補。構造化データと入力の検証。

投稿候補 1 件が、対象の読者、目的、検証の状況、出典、制約を持つ。カード・本文・詳細ページは、すべてこのデータから
決まった手順で組み立てる（文章をモデルに書き直させない）。だから、カードと詳細ページが食い違うことも、
データにない数値や URL が出力に現れることもない。

投稿の目的（知る / 判断する / 試す）と、検証の状況（紹介だけ / 第三者の検証 / 自分たちの実測 / 表示確認用の架空データ）は、
別の項目として持つ。目的が「試す」でも、自分たちで確かめていなければ「検証済み」とは表示しない。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Purpose = Literal["know", "decide", "try"]
Verification = Literal["introduced_unreproduced", "third_party_reported", "self_verified", "illustrative"]
Triage = Literal["candidate", "hold", "reject", "archive_only"]

PURPOSE_LABEL = {"know": "知る", "decide": "判断する", "try": "試す"}
PURPOSE_GAIN = {"know": "何が登場し、自分に関係するかが分かる", "decide": "選択肢の違いと、採用の条件が分かる",
                "try": "自分の環境で試せるかが分かる"}
VERIFICATION_LABEL = {
    "introduced_unreproduced": "情報紹介・未再現",
    "third_party_reported": "第三者の検証を紹介",
    "self_verified": "自環境で検証済み",
    "illustrative": "表示確認用の架空データ",
}
TRIAGE_LABEL = {"candidate": "配信の候補", "hold": "保留", "reject": "棄却", "archive_only": "検索用の保存だけ"}

# 表示幅の単位: 全角 2、半角 1（カードに収まる量を決めるために使う）
def width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


_URL = re.compile(r"https?://[^\s<>\"'）)、。」]+")
# 自分たちで確かめていない内容を、確かめたように書く表現（「未検証」「検証していない」などの否定は対象にしない）
_OVERCLAIM = re.compile(r"(?<!未)(検証済み|再現済み|実証済み|動作確認済み|確認済み|有効性が確認|効果が実証|効果が証明)")
_EVERYONE = re.compile(r"^(全社員|全員|社員全員|みなさん|皆さん|すべての社員|全社)$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(_Strict):
    title: str = Field(min_length=1)
    url: str
    publisher: str = ""
    kind: Literal["primary", "secondary"] = "primary"      # primary: 提供元・著者自身の文書。secondary: それを伝える記事
    checked_on: dt.date                                    # 自分たちがこの出典を読んで確かめた日
    version: str = ""                                      # 対象の版。出典に版の記載がなければ、その旨を書く
    supports: str = Field(min_length=1)                    # この出典が裏づけている内容
    reports_verification: bool = False                     # 出典の著者自身による実測・検証の報告か
    read_scope: Literal["full", "partial", "abstract_only"] = "full"

    @model_validator(mode="after")
    def _check(self) -> Source:
        if not self.url.startswith("https://"):
            raise ValueError(f"出典の URL は https で書く: {self.url}")
        return self


class Need(_Strict):
    """読者のニーズ（誰が、何をしたくて、それはなぜか）。根拠の種類を必ず書く。"""

    who: str = Field(min_length=1)
    wants_to: str = Field(min_length=1)
    so_that: str = Field(min_length=1)
    evidence: Literal["assumed", "from_records", "reader_confirmed"]
    evidence_note: str = Field(min_length=1)               # どの記録・どの回答に基づくか。assumed なら、何を確かめる必要があるか


class Audience(_Strict):
    segment: str = Field(min_length=1)                     # 試行の対象にする読者（1 つ）
    relevance: str = Field(min_length=1)                   # その読者に関係する理由
    need: Need

    @model_validator(mode="after")
    def _check(self) -> Audience:
        if _EVERYONE.match(self.segment):
            raise ValueError("対象の読者を「全社員」にしない。試行の対象になる読者を 1 つ選ぶ")
        return self


class Evidence(_Strict):
    """自分たちの実測（verification が self_verified のときに必須）。"""

    environment: str = Field(min_length=1)
    procedure: list[str] = Field(min_length=1)
    result: str = Field(min_length=1)
    measured_on: dt.date
    record: str = Field(min_length=1)                      # 実測の記録の場所（CI の実行、コミット、ファイル）
    not_covered: list[str] = Field(min_length=1)           # この実測が確かめていない範囲


class Row(_Strict):
    label: str = Field(min_length=1)
    cells: list[str] = Field(default_factory=list)
    note: str = ""


class Diagram(_Strict):
    """図解。1 枚で 1 つの問いを扱う。形は内容に合わせて選ぶ。

    comparison: 選択肢（行）× 観点（列）の比較    steps: 手順    flow: 原因から結果への流れ    structure: 全体と部分
    """

    kind: Literal["comparison", "steps", "flow", "structure"]
    question: str = Field(min_length=1)                    # この図が答える問い
    columns: list[str] = Field(default_factory=list)       # comparison の観点
    rows: list[Row] = Field(min_length=2)
    long_description: str = Field(min_length=1)            # 図を見られない人のための、文章による説明

    @model_validator(mode="after")
    def _check(self) -> Diagram:
        limits = {"comparison": (4, 3), "steps": (5, 0), "flow": (5, 0), "structure": (5, 0)}
        max_rows, max_cols = limits[self.kind]
        if len(self.rows) > max_rows:
            raise ValueError(f"図の項目が多すぎる（{len(self.rows)} > {max_rows}）。1 枚で 1 つの問いに絞る")
        if self.kind == "comparison":
            if not 1 <= len(self.columns) <= max_cols:
                raise ValueError(f"比較の観点は 1〜{max_cols} 個にする")
            if any(len(r.cells) != len(self.columns) for r in self.rows):
                raise ValueError("比較の各行は、観点と同じ数の値を持つ")
            if any(width(c) > 16 for r in self.rows for c in r.cells) or any(width(c) > 16 for c in self.columns):
                raise ValueError("比較の値と観点は、全角 8 文字以内にする（詳しい条件は制約か詳細ページに書く）")
            if any(width(r.label) > 22 for r in self.rows):
                raise ValueError("比較の行の名前は、全角 11 文字以内にする")
        else:
            if any(r.cells for r in self.rows) or self.columns:
                raise ValueError(f"{self.kind} の図は label と note だけを使う")
            if any(width(r.label) > 36 for r in self.rows) or any(width(r.note) > 44 for r in self.rows):
                raise ValueError("図の項目は全角 18 文字以内、補足は全角 22 文字以内にする")
        return self


class Detail(_Strict):
    """詳細ページの本文。目的に応じて、必要な節だけを書く（全部を埋めることは求めない）。"""

    conclusion: str = Field(min_length=1)
    applies_when: list[str] = Field(min_length=1)          # 適用の条件
    overview: str = ""                                     # 知る: 概要
    confirmed_scope: list[str] = Field(default_factory=list)   # 知る: 確かめた範囲
    comparison_basis: list[str] = Field(default_factory=list)  # 判断する: 比較の条件と根拠
    environment: str = ""                                  # 試す: 必要な環境
    steps: list[str] = Field(default_factory=list)         # 試す: 手順
    results: list[str] = Field(default_factory=list)       # 試す: 結果
    failures: list[str] = Field(default_factory=list)      # 試す: 失敗した例、つまずいた点


class Brief(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{2,60}$")
    purpose: Purpose
    verification: Verification
    verification_basis: str = Field(min_length=1)          # その検証の状況だと言える根拠
    audience: Audience
    headline: str = Field(min_length=1)                    # 読者に関係する見出し（結論か問い）
    takeaways: list[str] = Field(min_length=1, max_length=3)   # 持ち帰ってほしい要点
    constraints: list[str] = Field(min_length=1, max_length=3)  # 判断を左右する条件・制約（カードの本体に必ず出す）
    unconfirmed: list[str] = Field(min_length=1)           # 確かめていない点（「なし」と言えることは、まずない）
    change: str = Field(min_length=1)                      # 既存の情報から、何が変わったか
    why_now: str = Field(min_length=1)                     # 今知らせる理由
    evidence_strength: Literal["strong", "moderate", "weak"]
    evidence_strength_note: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    evidence: Evidence | None = None
    diagram: Diagram
    detail: Detail
    similar_posts: list[str] = Field(default_factory=list)  # 似た内容の過去の投稿（id）
    similar_checked: str = Field(min_length=1)             # 似た投稿を、どこまで探したか
    triage: Triage = "candidate"
    triage_reason: str = Field(min_length=1)
    corrects: str = ""                                     # 訂正の対象（元の投稿候補の id）
    correction_note: str = ""                              # 何を、なぜ訂正したか
    updated_on: dt.date
    illustrative_note: str = ""                            # 架空データのとき: どこが架空か

    @model_validator(mode="after")
    def _check(self) -> Brief:
        problems: list[str] = []
        v = self.verification
        if v == "self_verified" and self.evidence is None:
            problems.append("「自環境で検証済み」には、実測の記録（evidence）が要る")
        if v in ("introduced_unreproduced", "third_party_reported") and self.evidence is not None:
            problems.append("自分たちの実測（evidence）があるのに、検証の状況が実測なしになっている。他者の結果と自分たちの実測を分ける")
        if v == "third_party_reported" and not any(s.reports_verification for s in self.sources):
            problems.append("「第三者の検証を紹介」には、検証を報告している出典（reports_verification）が要る")
        if v == "illustrative" and not self.illustrative_note:
            problems.append("架空データのときは、どこが架空かを illustrative_note に書く")
        if v != "illustrative" and self.illustrative_note:
            problems.append("illustrative_note は、架空データのときだけ書く")
        if not any(s.kind == "primary" for s in self.sources):
            problems.append("一次情報の出典が 1 つも無い")
        if any(s.checked_on > self.updated_on for s in self.sources):
            problems.append("出典の確認日が、更新日より後になっている")
        if self.purpose == "try" and not (self.detail.steps and self.detail.environment):
            problems.append("目的が「試す」のときは、必要な環境と手順を書く")
        if self.purpose == "decide" and not self.detail.comparison_basis:
            problems.append("目的が「判断する」のときは、比較の条件と根拠を書く")
        if self.purpose == "know" and not (self.detail.overview and self.detail.confirmed_scope):
            problems.append("目的が「知る」のときは、概要と確かめた範囲を書く")
        if bool(self.corrects) != bool(self.correction_note):
            problems.append("訂正のときは、対象（corrects）と内容（correction_note）の両方を書く")
        if self.corrects == self.id:
            problems.append("自分自身を訂正の対象にはできない")
        if width(self.headline) > 52:
            problems.append("見出しは全角 26 文字以内にする")
        for name, items, limit in (("要点", self.takeaways, 64), ("制約", self.constraints, 64)):
            problems += [f"{name}は 1 件あたり全角 32 文字以内にする: {t}" for t in items if width(t) > limit]
        # 出典にない URL を書かない（公開済みのように見える URL を作らない）
        known = {s.url for s in self.sources}
        for text in self.texts(include_sources=False):
            problems += [f"出典の一覧にない URL が本文にある: {u}" for u in _URL.findall(text) if u not in known]
        # 自分たちで確かめていないのに、確かめたように読める表現を使わない
        if v != "self_verified":
            for text in self.reader_texts():
                m = _OVERCLAIM.search(text)
                if m:
                    problems.append(f"検証の状況が「{VERIFICATION_LABEL[v]}」なのに、確かめたように読める表現がある: 「{m.group(0)}」（{text[:30]}…）")
        # 途中で切れた文を通さない（YAML では、空白のあとの # から後ろがコメントになり、気づかずに文が欠ける）
        for text in self.texts():
            if any(text.count(a) != text.count(z) for a, z in (("（", "）"), ("(", ")"), ("「", "」"))):
                problems.append(f"括弧が閉じていない（文が途中で切れていないか）: {text[:40]}")
        if problems:
            raise ValueError("\n".join(problems))
        return self

    def reader_texts(self) -> list[str]:
        """読者に見える文章（見出し、要点、制約、図、詳細ページの本文）。"""
        d = self.detail
        diagram = [self.diagram.question, *self.diagram.columns, self.diagram.long_description,
                   *(x for r in self.diagram.rows for x in (r.label, r.note, *r.cells))]
        return [t for t in (self.headline, *self.takeaways, *self.constraints, *self.unconfirmed, *diagram, d.conclusion, *d.applies_when,
                            d.overview, *d.confirmed_scope, *d.comparison_basis, d.environment, *d.steps, *d.results, *d.failures) if t]

    def texts(self, *, include_sources: bool = True) -> list[str]:
        own = [*self.reader_texts(), self.verification_basis, self.change, self.why_now, self.evidence_strength_note, self.triage_reason,
               self.correction_note, self.illustrative_note, self.audience.segment, self.audience.relevance, self.audience.need.who,
               self.audience.need.wants_to, self.audience.need.so_that, self.audience.need.evidence_note, self.similar_checked]
        if self.evidence:
            own += [self.evidence.environment, *self.evidence.procedure, self.evidence.result, *self.evidence.not_covered]
        if include_sources:
            own += [x for s in self.sources for x in (s.title, s.url, s.publisher, s.version, s.supports)]
        return [t for t in own if t]

    def content_sha256(self) -> str:
        """内容のハッシュ。承認はこのハッシュに結び付く（承認のあとで内容が変われば、承認は効かなくなる）。"""
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def review_notes(self) -> list[str]:
        """入力としては通るが、レビューする人に見てほしい点。"""
        notes = []
        if self.audience.need.evidence == "assumed":
            notes.append("読者のニーズは仮定（読者に確かめていない）。" + self.audience.need.evidence_note)
        if self.evidence_strength == "weak":
            notes.append("根拠が弱い。配信ではなく、保留か、検索用の保存にする選択肢がある")
        if any(s.read_scope != "full" for s in self.sources):
            notes.append("全文を読んでいない出典がある: " + "、".join(s.title for s in self.sources if s.read_scope != "full"))
        if any(s.kind == "primary" and not s.version for s in self.sources):
            notes.append("対象の版が書かれていない出典がある（出典に版の記載がなければ、その旨を書く）")
        if self.verification == "illustrative":
            notes.append("表示確認用の架空データ。承認も投稿もできない")
        if not self.similar_posts and "なし" not in self.similar_checked and "無" not in self.similar_checked:
            notes.append("似た投稿の有無が、はっきり書かれていない")
        return notes


def load(data: dict[str, Any]) -> Brief:
    return Brief.model_validate(data)
