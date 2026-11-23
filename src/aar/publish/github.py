"""GitHub への公開候補の提出（専用ブランチへの push と Pull Request の作成）。

決まり:
  * main へ直接 push しない。研究ごとの専用ブランチだけを push する。
  * push 先のリポジトリの公開範囲が、方針で許した範囲（既定は private だけ）にあることを毎回確かめる。
    PR を作ることと、情報を外部に公開することは別の判断として扱う。
  * 再試行されても PR が重複しないよう、同じブランチの PR が既にあればそれを返す。
  * リモートが設定されていない場合は何もせず、保留として返す（リポジトリを勝手に作らない）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from aar import workspace as wsmod
from aar.config import PublicationPolicy


def _gh(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def submit_pull_request(ws: Path, *, branch: str, title: str, body: str, policy: PublicationPolicy,
                        remote_repo: str | None) -> dict[str, Any]:
    """公開候補を提出する。結果は {"status": "created"|"exists"|"held"|"skipped", "url"?, "reason"?}。"""
    if not policy.create_pull_request or not policy.channels.get("github_pr", {}).get("enabled", True):
        return {"status": "skipped", "reason": "方針で PR の作成が無効になっている"}
    if not remote_repo:
        return {"status": "held", "reason": "push 先のリポジトリが設定されていない（研究の remote_repo を設定すると PR を作れる）"}
    if branch == policy.base_branch or not branch.startswith(policy.branch_prefix):
        return {"status": "held", "reason": f"ブランチ {branch} は公開候補用の接頭辞 {policy.branch_prefix} で始まっていない"}

    view = _gh(["repo", "view", remote_repo, "--json", "visibility,defaultBranchRef,nameWithOwner"], ws)
    if view.returncode != 0:
        return {"status": "held", "reason": f"リポジトリ {remote_repo} を確認できない: {view.stderr.strip()[-200:]}"}
    info = json.loads(view.stdout)
    visibility = str(info.get("visibility", "")).lower()
    if visibility not in [v.lower() for v in policy.allowed_remote_visibility]:
        return {"status": "held", "reason": f"リポジトリの公開範囲が {visibility} で、許可された範囲"
                                            f"（{', '.join(policy.allowed_remote_visibility)}）の外にある"}
    if wsmod.scan_secrets(ws):
        return {"status": "held", "reason": "秘密情報らしい記述が残っている"}

    url = f"https://github.com/{info['nameWithOwner']}.git"
    wsmod.git(ws, "remote", "remove", "origin", check=False)
    wsmod.git(ws, "remote", "add", "origin", url)
    # push は同じ内容なら何度行っても結果が変わらない。強制 push はしない
    push = subprocess.run(["git", "push", "origin", f"{branch}:{branch}"], cwd=ws, capture_output=True, text=True, timeout=300)
    if push.returncode != 0:
        return {"status": "held", "reason": f"push に失敗: {push.stderr.strip()[-300:]}"}

    existing = _gh(["pr", "list", "--repo", remote_repo, "--head", branch, "--state", "all", "--json", "url,state"], ws)
    if existing.returncode == 0 and json.loads(existing.stdout or "[]"):
        return {"status": "exists", "url": json.loads(existing.stdout)[0]["url"]}
    base = (info.get("defaultBranchRef") or {}).get("name") or policy.base_branch
    created = _gh(["pr", "create", "--repo", remote_repo, "--head", branch, "--base", base, "--title", title,
                   "--body", body], ws, timeout=120)
    if created.returncode != 0:
        return {"status": "held", "reason": f"PR の作成に失敗: {created.stderr.strip()[-300:]}"}
    return {"status": "created", "url": created.stdout.strip().splitlines()[-1]}


def pr_body(*, project_id: str, title: str, verdicts: dict[str, str], research_gate: list[dict[str, Any]],
            publication_gate: list[dict[str, Any]], audit_summary: str, score: float | None, mode: str) -> str:
    v = {"supported": "支持", "not_supported": "不支持", "inconclusive": "判定保留"}
    lines = [f"## {title}", "", f"研究 ID: `{project_id}`（自律研究パイプラインによる公開候補）", ""]
    if mode != "real":
        lines += ["> **注意: この研究はモック実行の結果であり、実際の実験・監査ではない。**", ""]
    lines += ["### 仮説の判定", *[f"- {k}: {v.get(s, s)}" for k, s in sorted(verdicts.items())], "",
              "### 研究品質ゲート", *[f"- {'✅' if c['passed'] else '❌'} {c['name']}" + (f"（{c['detail']}）" if c.get("detail") else "") for c in research_gate], "",
              f"### 公開品質ゲート（{score:g} 点）" if score is not None else "### 公開品質ゲート",
              *[f"- {'✅' if c['passed'] else '❌'} {c['name']}" + (f"（{c['detail']}）" if c.get("detail") else "") for c in publication_gate], "",
              "### 独立監査の要約", audit_summary.strip() or "（なし）", "",
              "監査の台帳と再計算コードは `audit/` にある。マージと外部への公開は人が判断する。", "",
              "🤖 Generated with [Claude Code](https://claude.com/claude-code)"]
    return "\n".join(lines)
