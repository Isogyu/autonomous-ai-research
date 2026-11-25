"""改善候補が変更してよい対象と、変更できない領域。

候補は、対象ごとに決めた少数のファイルだけを変更できる（許可リスト）。それ以外の変更を含む差分は、評価の前に無効にする。
品質ゲート、公開の判定、事前登録の検証、評価器、採否の基準、権限・隔離・送信先、利用上限・予算、監査の原本、移行、
テスト、CI は、候補からは変更できない（保護領域）。保護領域は許可リストより優先する。
"""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    name: str
    description: str
    module_path: str                    # 評価のときに別プロセスで読み込む、ただ1つのファイル
    entry: str                          # そのファイルの中の関数名
    allowed_paths: tuple[str, ...]      # 候補が変更してよいファイル
    max_patch_bytes: int = 40_000


TARGETS: dict[str, Target] = {
    "fix_context": Target(
        name="fix_context",
        description="修正担当へ渡す、監査の指摘の文脈の組み立て（指摘と、指摘が参照する主張・証拠）",
        module_path="src/aar/engine/fix_context.py",
        entry="build_fix_context",
        allowed_paths=("src/aar/engine/fix_context.py",),
    ),
    "log_excerpt": Target(
        name="log_excerpt",
        description="実験・テスト・分析が失敗したときに渡す、ログの抜粋の作り方",
        module_path="src/aar/engine/log_excerpt.py",
        entry="build_excerpt",
        allowed_paths=("src/aar/engine/log_excerpt.py",),
    ),
}

# 候補が変更できない領域（リポジトリのルートからのパターン）
PROTECTED: tuple[str, ...] = (
    "config/*", ".github/*", "tests/*", "scripts/*", "infra/*", "pyproject.toml", "skills/*",
    "src/aar/improve/*",                # 評価器、採否の基準、台帳、この許可リスト
    "src/aar/gates.py", "src/aar/audit/*", "src/aar/publish/*",   # 品質ゲート、監査、公開の判定と必須検査
    "src/aar/workspace.py",             # 事前登録の検証
    "src/aar/limits.py", "src/aar/agents/*", "src/aar/sandbox.py", "src/aar/config.py",   # 利用上限、権限、隔離、送信先
    "src/aar/db/*", "src/aar/store.py", "src/aar/temporal/*", "src/aar/service.py", "src/aar/cli.py",
    "src/aar/fixture.py", "src/aar/datasets.py", "src/aar/sources/*",
)

# 評価の比較可能性を決めるファイル。内容のハッシュを評価計画に固定し、実行のたびに確かめる
EVALUATOR_FILES: tuple[str, ...] = (
    "src/aar/improve/targets.py", "src/aar/improve/evalkit.py", "src/aar/improve/child.py", "src/aar/improve/decide.py",
    "src/aar/improve/spec_log_excerpt.py",
)


def is_protected(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in PROTECTED)


def evaluator_sha256(root: Path) -> str:
    """評価器のファイルの内容のハッシュ。root は、権限を持つ側（評価を実行する側）のチェックアウト。"""
    h = hashlib.sha256()
    for rel in EVALUATOR_FILES:
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=120).stdout


def check_patch(repo: Path, base: str, head: str, target: Target) -> list[str]:
    """候補の差分（base..head）が許可された範囲に収まっているかを確かめる。違反の一覧を返す（空なら可）。"""
    problems: list[str] = []
    try:
        raw = _git(repo, "diff", "--raw", "--no-renames", "-z", base, head)
    except subprocess.CalledProcessError as e:
        return [f"差分を取れない: {e.stderr.strip()[-200:]}"]
    fields = [x for x in raw.split("\0") if x]
    changed: list[tuple[str, str, str]] = []           # (状態, 新しいモード, パス)
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        parts = meta.lstrip(":").split()
        changed.append((parts[4], parts[1], path))
    if not changed:
        return ["変更がない"]
    for status, mode, path in changed:
        if is_protected(path):
            problems.append(f"保護領域を変更している: {path}")
        elif path not in target.allowed_paths:
            problems.append(f"許可された範囲の外を変更している: {path}")
        elif status != "M":
            problems.append(f"許可されたファイルの追加・削除・種類の変更はできない（{status}）: {path}")
        elif mode != "100644":
            problems.append(f"ファイルの種類や実行権限を変えている（{mode}）: {path}")
    size = len(_git(repo, "diff", base, head).encode())
    if size > target.max_patch_bytes:
        problems.append(f"差分が大きすぎる（{size} バイト > {target.max_patch_bytes}）")
    return problems
