"""実験・テスト・分析が失敗したときに、実行の担当や記録へ渡すログの抜粋。

現行方式: 標準出力と標準エラーの、それぞれ末尾を渡す。エラーのあとに長い出力が続くと、肝心の行が抜粋から外れる。
この関数は、基盤の改善の対象（aar.improve.targets の log_excerpt）。変更は、隔離した比較評価を通す。標準ライブラリだけを使う。
"""

from __future__ import annotations


def build_excerpt(stdout: str, stderr: str, *, budget_chars: int = 3000) -> str:
    """ログの抜粋を返す。budget_chars は、流れ 1 つあたりの長さの目安。"""
    return (stdout[-budget_chars:] + ("\n--- stderr ---\n" + stderr[-budget_chars:] if stderr.strip() else "")).strip()
