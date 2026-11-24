"""研究ごとの作業領域（独立した Git リポジトリ）と、事前登録・秘密情報の検査。

作業領域は研究1件につき1つ。既存の研究リポジトリを対象にする場合も、元を直接触らず複製して使う。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

GITIGNORE = """.home/
.aar-build/
__pycache__/
*.pyc
.pytest_cache/
.env
.env.*
"""


def git(ws: Path, *args: str, check: bool = True) -> str:
    """作業領域で git を実行する。利用者のグローバル設定やフックの影響を受けないようにする。"""
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=ws, capture_output=True, text=True, timeout=120,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} が失敗: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def init_workspace(ws: Path, *, author_name: str, author_email: str, base_branch: str = "main",
                   clone_from: Path | None = None) -> None:
    """作業領域を用意する。何度呼んでも同じ結果になる。"""
    if (ws / ".git").exists():
        return
    ws.parent.mkdir(parents=True, exist_ok=True)
    if clone_from:
        # 元のリポジトリは読むだけ。複製先の origin は外し、誤って元へ push しないようにする
        subprocess.run(["git", "clone", "--no-hardlinks", str(clone_from), str(ws)], check=True,
                       capture_output=True, timeout=600)
        git(ws, "remote", "remove", "origin", check=False)
    else:
        ws.mkdir(parents=True, exist_ok=True)
        git(ws, "init", "-b", base_branch)
    git(ws, "config", "user.name", author_name)
    git(ws, "config", "user.email", author_email)
    if not clone_from:
        (ws / ".gitignore").write_text(GITIGNORE)
        git(ws, "add", ".gitignore")
        git(ws, "commit", "-m", "作業領域を初期化")


def ensure_branch(ws: Path, branch: str) -> None:
    current = git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    if current == branch:
        return
    exists = git(ws, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    git(ws, "checkout", branch) if exists else git(ws, "checkout", "-b", branch)


def commit_all(ws: Path, message: str) -> str:
    """変更をすべてコミットし、HEAD のハッシュを返す。変更がなければ何もしない。"""
    git(ws, "add", "-A")
    if git(ws, "status", "--porcelain"):
        git(ws, "commit", "-m", message)
    return git(ws, "rev-parse", "HEAD")


def head(ws: Path) -> str:
    return git(ws, "rev-parse", "HEAD")


def changed_paths(ws: Path, since_commit: str) -> list[str]:
    out = git(ws, "diff", "--name-only", since_commit, "HEAD")
    return [p for p in out.splitlines() if p]


# ------------------------------------------------------------------ 事前登録


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def prereg_path(cycle: int) -> str:
    return f"prereg/cycle{cycle}.json"


def register_prereg(ws: Path, cycle: int, content: dict[str, Any]) -> tuple[str, str]:
    """事前登録を書いてコミットする。(内容の SHA-256, コミット) を返す。既にあれば上書きしない。"""
    path = ws / prereg_path(cycle)
    if path.exists():
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        commit = git(ws, "log", "-1", "--format=%H", "--", prereg_path(cycle)) or commit_all(ws, f"事前登録（サイクル{cycle}）")
        return sha, commit
    path.parent.mkdir(exist_ok=True)
    text = canonical_json(content)
    path.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    git(ws, "add", prereg_path(cycle))
    git(ws, "commit", "-m", f"事前登録（サイクル{cycle}）: 仮説・指標・分割・解析・判定基準を固定", "--", prereg_path(cycle))
    commit = git(ws, "rev-parse", "HEAD")
    git(ws, "tag", "-f", f"prereg-cycle{cycle}", commit)
    return sha, commit


def verify_prereg(ws: Path, cycle: int, expected_sha: str, expected_commit: str | None) -> tuple[bool, str]:
    """事前登録が登録後に変えられていないかを確かめる。"""
    rel = prereg_path(cycle)
    path = ws / rel
    if not path.exists():
        return False, f"{rel} がない"
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        return False, f"{rel} の内容が登録時と違う"
    commits = git(ws, "log", "--format=%H", "--", rel).splitlines()
    if len(commits) != 1:
        return False, f"{rel} に登録後のコミットが {len(commits) - 1} 件ある"
    if expected_commit and commits[0] != expected_commit:
        return False, f"{rel} の登録コミットが記録と違う（履歴が書き換えられた可能性）"
    # 登録コミットが、実験結果の最初のコミットより前にあること
    first_results = git(ws, "log", "--diff-filter=A", "--format=%H", "--reverse", "--", f"results/cycle{cycle}").splitlines()
    if first_results:
        is_ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commits[0], first_results[0]], cwd=ws).returncode == 0
        if not is_ancestor or commits[0] == first_results[0]:
            return False, "事前登録が実験結果より前にコミットされていない"
    return True, "登録時のハッシュ・コミットと一致し、実験結果より前にある"


# ------------------------------------------------------------------ 秘密情報の検査

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic API キー", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API キー", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("GitHub トークン", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|github_pat_[A-Za-z0-9_]{40,}")),
    ("AWS アクセスキー", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("秘密鍵", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("Slack トークン", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Hugging Face トークン", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("汎用の秘密の代入", re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['"][A-Za-z0-9_\-/+=]{16,}['"]""")),
    ("利用者のホームのパス", re.compile(r"/Users/[A-Za-z0-9_.\-]+/")),
]
_SECRET_FILENAMES = re.compile(r"(^|/)(\.env(\..+)?|.*\.pem|id_rsa|id_ed25519|auth\.json|credentials\.json|\.netrc)$")


def scan_secrets(ws: Path, *, include_history: bool = True) -> list[dict[str, str]]:
    """Git が追跡するファイル（と全履歴の差分）から秘密情報らしいものを探す。値そのものは返さない。"""
    hits: list[dict[str, str]] = []
    tracked = git(ws, "ls-files").splitlines()
    for rel in tracked:
        if _SECRET_FILENAMES.search(rel) and not rel.endswith(".env.example"):
            hits.append({"path": rel, "kind": "秘密情報を入れる名前のファイル", "where": "tracked"})
        p = ws / rel
        try:
            if p.stat().st_size > 5_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                hits.append({"path": f"{rel}:{line}", "kind": kind, "where": "tracked"})
    if include_history:
        log = git(ws, "log", "-p", "--all", "--no-color", "--unified=0", check=False)
        for kind, pat in SECRET_PATTERNS[:-1]:   # パスの検査は現在のファイルだけ
            added = "\n".join(line for line in log.splitlines() if line.startswith("+"))
            if pat.search(added) and not any(h["kind"] == kind for h in hits):
                hits.append({"path": "(過去のコミット)", "kind": kind, "where": "history"})
    return hits


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
