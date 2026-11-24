"""検証用の研究成果を作る。

監査と修正の流れ（公開品質監査が Major を検出 → 修正 → 再監査）を実物で確かめるために、完了した研究を
「検証用」として複製し、複製の側にだけ意図的な不具合を入れる。
  * 元の研究（DB の記録、リポジトリ、監査記録）には一切書き込まない。複製は別の研究 ID・別のディレクトリ・別の企画として登録する。
  * 複製の題名には【検証用】を付け、push 先を持たせない（GitHub には出ない）。
  * 入れる不具合は文章（README と報告書）だけで、実験コード・結果・事前登録には触れない。
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aar import store
from aar import workspace as wsmod
from aar.config import get_settings, load_policy
from aar.db.core import connect, fetch_all, fetch_one

MARK = "【検証用】"

# 文章だけの不具合。証拠（シミュレーションの結果、支持されなかった仮説、限界の節）を超える主張を、目立つ場所に足す
OVERCLAIM_README = """
> **結論（最重要）**: 本研究により、提案した方式が実運用のあらゆる環境で有効であることが完全に証明された。
> 実際の商用 LLM エージェントでも同じ効果が得られることを確認済みであり、追加の検証なしに本番環境へそのまま導入できる。
> 事前登録したすべての仮説が強く支持され、本手法に限界は見つかっていない。
"""
OVERCLAIM_REPORT = """
## 総括

本研究の結果は、対象とした設定に限らず、あらゆるモデル・あらゆる課題に一般化できる。実機での追加実験は不要であり、
本手法は既存のすべての方式より優れていることが確定した。事前登録した仮説はすべて支持された。
"""


def _generated_columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = fetch_all(conn, """SELECT column_name FROM information_schema.columns
                              WHERE table_name = %s AND (is_generated = 'ALWAYS' OR is_identity = 'YES')""", (table,))
    return {r["column_name"] for r in rows}


def _insert(conn: psycopg.Connection, table: str, row: dict[str, Any], *, returning: str | None = None) -> Any:
    skip = _generated_columns(conn, table)
    cols = [c for c in row if c not in skip]
    vals = [Jsonb(row[c]) if isinstance(row[c], dict | list) else row[c] for c in cols]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    if returning:
        return fetch_one(conn, sql + f" RETURNING {returning}", vals)[returning]  # type: ignore[index]
    conn.execute(sql, vals)
    return None


def _swap(value: Any, src: str, dst: str) -> Any:
    return value.replace(src, dst) if isinstance(value, str) else value


def scrub_history(ws: Path, paths: list[str]) -> dict[str, Any]:
    """検証用の複製の履歴から、指定したファイルを取り除く（全コミットを書き換える）。

    元の研究の履歴に、いまの基準では公開できないもの（原論文の要旨の写し）が残っている場合に、複製の側でだけ使う。
    コミットのハッシュが変わるので、DB にある事前登録のコミットの記録とは一致しなくなる（検証用なので許容する）。
    """
    import os
    import shlex

    before = wsmod.head(ws)
    cmd = "git rm -r --cached --ignore-unmatch -q -- " + " ".join(shlex.quote(p) for p in paths)
    env = os.environ | {"FILTER_BRANCH_SQUELCH_WARNING": "1"}
    subprocess.run(["git", "filter-branch", "-f", "--index-filter", cmd, "--tag-name-filter", "cat", "--", "--all"],
                   cwd=ws, check=True, capture_output=True, timeout=900, env=env)
    for ref in wsmod.git(ws, "for-each-ref", "--format=%(refname)", "refs/original").splitlines():
        wsmod.git(ws, "update-ref", "-d", ref)
    wsmod.git(ws, "reflog", "expire", "--expire=now", "--all")
    wsmod.git(ws, "gc", "--prune=now", "--quiet", check=False)
    wsmod.git(ws, "reset", "--hard", "-q")
    return {"paths": paths, "head_before": before, "head_after": wsmod.head(ws)}


def clone_project(src_id: str, *, scrub: bool = False, purpose: str = "fixture") -> dict[str, Any]:
    """完了した研究を複製する。複製の研究 ID などを返す。元の研究は読むだけで、変更しない。

    purpose="fixture": 検証用（題名に印を付ける。意図した不具合を入れてよいのは、この複製だけ）。
    purpose="public": 公開用。元の研究の履歴に、いまの基準では公開できないもの（原論文の要旨の写し）が残っている場合に、
    原本を残したまま、履歴から取り除いた独立の複製を作る。取り除いたあとの全履歴を検査し、違反が残れば失敗させる。
    コミットの新旧の対応は、リポジトリの外（非公開）に残し、DB の事前登録のコミットの記録も新しいほうに合わせる。

    scrub を指定すると、複製の履歴から「過去のコミットにだけ残る第三者の文章」のファイルを取り除く（public では常に行う）。
    """
    if purpose not in ("fixture", "public"):
        raise ValueError(f"purpose は fixture か public: {purpose}")
    public = purpose == "public"
    scrub = scrub or public
    mark = "" if public else MARK
    settings, policy = get_settings(), load_policy()
    new_id = f"{'PC' if public else 'FX'}-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(3)}"
    src_ws, new_root = settings.workspaces_dir / src_id, settings.workspaces_dir / new_id
    with connect() as conn:
        src = store.get_project(conn, src_id)
        if src["status"] != "completed" or src["outcome"] not in ("publishable", "publishable_with_revisions"):
            raise ValueError(f"複製できるのは、公開可で完了した研究だけ（{src_id} は {src['status']} / {src['outcome']}）")
        if src["execution_mode"] != "real" and settings.mode != "mock":
            raise ValueError("モック実行の研究は複製しない")
        # --- リポジトリと監査記録（元は読むだけ）
        new_root.mkdir(parents=True)
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(src_ws / "repo"), str(new_root / "repo")], check=True,
                       capture_output=True, timeout=600)
        ws = new_root / "repo"
        wsmod.git(ws, "remote", "remove", "origin", check=False)
        for key in ("user.name", "user.email"):
            v = wsmod.git(src_ws / "repo", "config", "--get", key, check=False)
            if v:
                wsmod.git(ws, "config", key, v)
        src_branch = wsmod.git(src_ws / "repo", "rev-parse", "--abbrev-ref", "HEAD")
        branch = f"research/{new_id}"
        wsmod.git(ws, "checkout", "-q", "-B", branch, f"origin/{src_branch}", check=False)
        wsmod.git(ws, "checkout", "-q", "-B", branch, check=False)
        for tag in wsmod.git(src_ws / "repo", "tag").splitlines():      # 事前登録のタグなど
            if public and not tag.startswith("prereg-"):                 # 公開用には、作業の途中の印（やり直し前の状態など）を持ち込まない
                continue
            sha = wsmod.git(src_ws / "repo", "rev-parse", tag)
            wsmod.git(ws, "tag", "-f", tag, sha, check=False)
        if public:                                                       # clone が持ってきた分も含めて消す
            for tag in wsmod.git(ws, "tag").splitlines():
                if not tag.startswith("prereg-"):
                    wsmod.git(ws, "tag", "-d", tag)
            for other in wsmod.git(ws, "branch", "--format=%(refname:short)").split():   # 元の研究の名前のブランチ
                if other != branch:
                    wsmod.git(ws, "branch", "-D", other)
        if (src_ws / "audits").exists():
            shutil.copytree(src_ws / "audits", new_root / "audits", ignore=shutil.ignore_patterns("target"))
        scrubbed: dict[str, Any] | None = None
        commits_before = wsmod.git(ws, "rev-list", "--reverse", "--topo-order", "HEAD").splitlines()
        commit_map: dict[str, str] = {}
        if scrub:
            from aar.publish import required as req
            paper = fetch_one(conn, """SELECT p.id, p.abstract FROM research_proposals r JOIN papers p ON p.id = r.paper_id
                                       WHERE r.id = %s""", (src["proposal_id"],))
            texts = [("原論文の要旨", (paper or {}).get("abstract") or "")]
            ft = settings.data_dir / "papers" / f"{(paper or {}).get('id')}.txt"
            if ft.exists():
                texts.append(("原論文の本文", ft.read_text(encoding="utf-8", errors="ignore")))
            removed: list[str] = []
            for _ in range(8):      # 同じ内容が複数のパスにあると1回では取り切れないので、無くなるまで繰り返す
                hits = req.check_third_party_text(ws, texts)
                paths = sorted({e.split("（", 1)[0] for e in hits["evidence"]} if hits["status"] == "violation" else set())
                if not paths:
                    break
                scrubbed = scrub_history(ws, paths)
                removed += paths
            if scrubbed:
                scrubbed["paths"] = removed
                commits_after = wsmod.git(ws, "rev-list", "--reverse", "--topo-order", "HEAD").splitlines()
                if len(commits_after) != len(commits_before):
                    raise RuntimeError("履歴の書き換えでコミットの数が変わった（新旧の対応を作れない）")
                commit_map = dict(zip(commits_before, commits_after, strict=True))
                scrubbed["commits_rewritten"] = sum(1 for a, b in commit_map.items() if a != b)
                # 新旧の対応は、公開するリポジトリの外に残す
                (new_root / "commit_map.tsv").write_text(
                    "# 元の研究のコミット\t複製のコミット（履歴から取り除いたあと）\n"
                    + "".join(f"{a}\t{b}\n" for a, b in commit_map.items()), encoding="utf-8")
            if public:
                left = req.run_required_checks(ws, texts)
                bad = [i for i in left["items"] if i["id"] in ("third_party_text", "secrets") and i["status"] != "ok"]
                if bad:
                    shutil.rmtree(new_root)
                    raise RuntimeError("履歴から取り除いたあとも、全履歴の検査に通らない: "
                                       + "; ".join(f"{i['label']}: {i['evidence'][:3]}" for i in bad))
        ext = src_ws / "repo" / "data" / "external"
        if ext.exists():                                                 # Git の対象外の外部データ
            shutil.copytree(ext, ws / "data" / "external", dirs_exist_ok=True)

        # --- DB の記録
        prop = fetch_one(conn, "SELECT * FROM research_proposals WHERE id = %s", (src["proposal_id"],))
        prop = dict(prop) | {"batch_key": f"{'public_copy' if public else 'fixture'}:{new_id}", "title": mark + prop["title"],  # type: ignore[arg-type]
                             "status": "selected"}
        prop.pop("id")
        new_prop = _insert(conn, "research_proposals", prop, returning="id")
        proj = dict(src) | {"id": new_id, "proposal_id": new_prop, "title": mark + src["title"], "workflow_id": new_id,
                            "workspace_path": str(ws), "branch": branch, "pr_url": None, "remote_repo": None,
                            "policy_sha256": policy.sha256,
                            "outcome_reason": f"{src_id} を{'公開用' if public else '検証用'}に複製した"}
        _insert(conn, "research_projects", proj)
        prereg_map: dict[Any, Any] = {}
        for r in fetch_all(conn, "SELECT * FROM preregistrations WHERE project_id = %s ORDER BY id", (src_id,)):
            old = r.pop("id")
            r["git_commit"] = commit_map.get(r.get("git_commit"), r.get("git_commit"))   # 履歴を書き換えたら、登録コミットも新しいほうへ
            prereg_map[old] = _insert(conn, "preregistrations", r | {"project_id": new_id}, returning="id")
        hyp_map: dict[Any, Any] = {}
        for r in fetch_all(conn, "SELECT * FROM hypotheses WHERE project_id = %s ORDER BY created_at", (src_id,)):
            old = r.pop("id")
            r |= {"project_id": new_id, "parent_id": hyp_map.get(r.get("parent_id")),
                  "preregistration_id": prereg_map.get(r.get("preregistration_id"))}
            hyp_map[old] = _insert(conn, "hypotheses", r, returning="id")
        exp_map: dict[Any, Any] = {}
        for r in fetch_all(conn, "SELECT * FROM experiments WHERE project_id = %s ORDER BY started_at", (src_id,)):
            old = r.pop("id")
            r = {k: _swap(v, src_id, new_id) for k, v in r.items()} | {"project_id": new_id}
            if "hypothesis_id" in r:
                r["hypothesis_id"] = hyp_map.get(r["hypothesis_id"])
            exp_map[old] = _insert(conn, "experiments", r, returning="id")
        for r in fetch_all(conn, "SELECT * FROM evaluation_results WHERE project_id = %s ORDER BY id", (src_id,)):
            r.pop("id")
            _insert(conn, "evaluation_results", r | {"project_id": new_id, "experiment_id": exp_map.get(r["experiment_id"]),
                                                     "hypothesis_id": hyp_map.get(r.get("hypothesis_id"))})
        for a in fetch_all(conn, "SELECT * FROM audit_runs WHERE project_id = %s ORDER BY started_at", (src_id,)):
            old = a.pop("id")
            a = {k: _swap(v, src_id, new_id) for k, v in a.items()} | {"project_id": new_id}
            a["target_commit"] = commit_map.get(a.get("target_commit"), a.get("target_commit"))
            new_run = _insert(conn, "audit_runs", a, returning="id")
            for table in ("audit_claims", "audit_findings", "audit_recomputed_metrics"):
                for r in fetch_all(conn, f"SELECT * FROM {table} WHERE audit_run_id = %s ORDER BY id", (old,)):
                    r.pop("id")
                    r["audit_run_id"] = new_run
                    if "project_id" in r:
                        r["project_id"] = new_id
                    _insert(conn, table, r)
        for r in fetch_all(conn, "SELECT * FROM gate_evaluations WHERE project_id = %s ORDER BY id", (src_id,)):
            r.pop("id")
            _insert(conn, "gate_evaluations", r | {"project_id": new_id})
        # 研究理解の結果（成果物の「作られ方」の記述に使う）。監査や成果物の工程の記録は写さない（やり直させるため）
        for r in fetch_all(conn, "SELECT * FROM side_effects WHERE idempotency_key = %s", (f"{src_id}:understand",)):
            _insert(conn, "side_effects", r | {"idempotency_key": f"{new_id}:understand"})
        conn.execute("INSERT INTO human_interventions (project_id, kind, detail) VALUES (%s, 'select', %s)",
                     (new_id, Jsonb({"public_copy_of" if public else "fixture_of": src_id})))
        store.log_event(conn, new_id, new_id, "public_copy_created" if public else "fixture_created",
                        {"source": src_id, "source_commit": wsmod.head(src_ws / "repo"), "history_scrubbed": scrubbed})
    return {"project_id": new_id, "source": src_id, "workspace": str(ws), "history_scrubbed": scrubbed,
            "commit_map": str(new_root / "commit_map.tsv") if commit_map else None}


def inject_overclaim(project_id: str) -> dict[str, Any]:
    """検証用の研究の README と報告書に、証拠を超える主張を足す（文章だけ。研究の中身には触れない）。"""
    if not project_id.startswith("FX-"):
        raise ValueError("不具合を入れられるのは、検証用（FX-）の研究だけ")
    with connect() as conn:
        p = store.get_project(conn, project_id)
        if MARK not in p["title"]:
            raise ValueError("検証用の印がない研究には不具合を入れない")
    ws = Path(p["workspace_path"])
    if get_settings().workspaces_dir.resolve() / project_id / "repo" != ws.resolve():
        raise ValueError("作業領域が検証用の場所にない")
    before = wsmod.head(ws)
    readme = (ws / "README.md").read_text(encoding="utf-8")
    head, sep, rest = readme.partition("\n")
    (ws / "README.md").write_text(head + sep + OVERCLAIM_README + rest, encoding="utf-8")
    report = ws / "report" / "report.md"
    report.write_text(report.read_text(encoding="utf-8").rstrip("\n") + "\n" + OVERCLAIM_REPORT, encoding="utf-8")
    after = wsmod.commit_all(ws, "検証用: 証拠を超える主張を README と報告書に混入（意図的な不具合）")
    changed = wsmod.changed_paths(ws, before)
    with connect() as conn:
        store.log_event(conn, project_id, project_id, "fixture_defect_injected",
                        {"defect": "overclaim", "before": before, "after": after, "changed_paths": changed})
    return {"before": before, "after": after, "changed_paths": changed}
