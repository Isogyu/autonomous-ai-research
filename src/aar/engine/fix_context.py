"""修正担当へ渡す「指摘の文脈」の組み立て。

監査の指摘と、その指摘が参照する主張・証拠から、修正担当のプロンプトに入れる JSON の文字列を作る。
この関数は、改善候補が変更してよい対象（aar.improve.targets の fix_context）である。評価は別プロセスでこのファイルだけを
読み込んで行うので、標準ライブラリ以外を import しない（aar の他のモジュールも import しない）。

現行方式は、指摘の行（ID、重大度、題名、詳細）だけを渡す。指摘が参照する主張や証拠の中身は渡していない。
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_BUDGET_CHARS = 24000


def build_fix_context(findings: list[dict[str, Any]], *, claims: list[dict[str, Any]] | None = None,
                      evidence: list[dict[str, Any]] | None = None, budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """修正担当に渡す指摘の文脈（JSON の文字列）を返す。

    findings: 指摘。各要素は issue_id、severity、title と、任意の detail（辞書）を持つ。
    claims:   その回の監査の主張（claim_id、claim_text、status、limitations など）。
    evidence: その回の監査の証拠（evidence_id、locator、observed_result など）。
    budget_chars: 返す文字列の長さの上限（現行方式は使っていない）。
    """
    rows = [{"issue_id": f["issue_id"], "severity": f["severity"], "title": f["title"], **(f.get("detail") or {})} for f in findings]
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)
