"""基盤の改善候補: 許可された範囲、改変から保護された評価、採否の判定、台帳。

DB を使わないテストは、隔離を「別プロセスにするだけ」（process）で動かす。macOS の sandbox-exec での隔離は、その場にあるときだけ確かめる。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aar.improve import decide as dec
from aar.improve import evalkit as ek
from aar.improve.targets import (
    EVALUATOR_FILES,
    PROTECTED,
    TARGETS,
    check_patch,
    evaluator_sha256,
    is_protected,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = TARGETS["fix_context"]
BASELINE = (ROOT / TARGET.module_path).read_text(encoding="utf-8")
BUDGET = 6000

# 参照先の定義を添える候補（テスト用の最小の実装）
GOOD = '''
import json, re
def build_fix_context(findings, *, claims=None, evidence=None, budget_chars=24000):
    rows = [{"issue_id": f["issue_id"], "severity": f["severity"], "title": f["title"], **(f.get("detail") or {})} for f in findings]
    base = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    ids = set(re.findall(r"(?<![A-Za-z0-9])([CE]\\d{3})(?![A-Za-z0-9])", json.dumps(rows, ensure_ascii=False, default=str)))
    refs = []
    for c in claims or []:
        if c.get("claim_id") in ids and c.get("claim_text"):
            refs.append({"id": c["claim_id"], "text": str(c["claim_text"])[:400]})
            ids.discard(c["claim_id"])
    for e in evidence or []:
        if e.get("evidence_id") in ids and (e.get("locator") or e.get("source_uri")):
            refs.append({"id": e["evidence_id"], "locator": str(e.get("locator") or e.get("source_uri"))[:300]})
            ids.discard(e["evidence_id"])
    while refs:
        out = json.dumps({"findings": rows, "referenced": refs}, ensure_ascii=False, indent=2, default=str)
        if len(out) <= max(budget_chars, len(base)):
            return out
        refs.pop()
    return base
'''
# 指摘を落として短くする候補（点数だけを狙う）
DROPS = GOOD.replace('for f in findings]', 'for f in findings[:1]]')
# 主張をすべて詰め込む候補
DUMPS = '''
import json
def build_fix_context(findings, *, claims=None, evidence=None, budget_chars=24000):
    rows = [{"issue_id": f["issue_id"], "severity": f["severity"], "title": f["title"], **(f.get("detail") or {})} for f in findings]
    return json.dumps({"findings": rows, "claims": claims, "evidence": evidence}, ensure_ascii=False, default=str)
'''
CRASHES = "def build_fix_context(findings, **kw):\n    raise RuntimeError('boom')\n"
RANDOM = "import json, os\ndef build_fix_context(findings, **kw):\n    return json.dumps({'x': os.urandom(4).hex()})\n"


def _cases(seed: str = "t", n: int = 12):
    return ek.synthetic_cases(seed, BUDGET, n)


def _eval(source: str, cases=None):
    return ek.evaluate(source, TARGET, cases or _cases(), isolation="process", deny_read=[])


def _plan(**over):
    return json.loads(json.dumps(dec.DEFAULT_PLAN)) | {"required_isolation": "process", "min_sealed_real_cases": 0,
                                                       "min_sealed_real_groups": 0, "budget_chars": BUDGET} | over


def _decide(candidate: str, plan=None, **kw):
    select, sealed = _cases("select"), _cases("sealed")
    runs = {"baseline:select": _eval(BASELINE, select), "candidate:select": _eval(candidate, select),
            "baseline:sealed": _eval(BASELINE, sealed), "candidate:sealed": _eval(candidate, sealed)}
    args = {"sealed_evaluations": 1, "patch_problems": [], "evaluator_matches": True} | kw
    return dec.decide(plan or _plan(), runs, **args)


# ---------------------------------------------------------------------- 評価器

def test_baseline_resolves_no_references_and_keeps_every_guard():
    r = _eval(BASELINE)
    assert r["status"] == "completed" and r["metrics"]["guard_failures"] == {}
    assert r["metrics"]["n_refs"] > 0 and r["metrics"]["ref_resolution_micro"] == 0.0     # 現行方式は、参照先の定義を渡していない


def test_candidate_that_adds_referenced_definitions_improves_without_breaking_guards():
    r = _eval(GOOD)
    assert r["metrics"]["guard_failures"] == {} and r["metrics"]["min_precision"] == 1.0
    assert r["metrics"]["ref_resolution_micro"] > 0.9
    hostile = next(s for s in r["per_case"] if "hostile" in s["id"])
    assert hostile["valid_json"] and hostile["issues_kept"]                                # 命令や閉じタグに見える文章でも、JSON のまま


def test_evaluated_code_gets_inputs_only_and_cannot_see_the_answer_key():
    spy = "import json\ndef build_fix_context(findings, **kw):\n    return json.dumps({'kw': sorted(kw), 'n': len(findings)})\n"
    run = ek.run_variant(spy, TARGET, _cases()[:1], isolation="process", deny_read=[])
    assert json.loads(run["results"][0]["output"])["kw"] == ["budget_chars", "claims", "evidence"]     # 正解（key）は渡らない
    imports = "import aar\ndef build_fix_context(findings, **kw):\n    return '[]'\n"
    assert ek.run_variant(imports, TARGET, _cases()[:1], isolation="process", deny_read=[])["failure_kind"] == "code_error"   # 基盤のコードは読み込めない


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="sandbox-exec がない")
def test_seatbelt_isolation_blocks_reads_writes_and_network(tmp_path):
    secret = tmp_path / "sealed" / "answers.json"
    secret.parent.mkdir()
    secret.write_text("{}")
    probe = f'''
import json
def build_fix_context(findings, **kw):
    out = {{}}
    for name, fn in (("read", lambda: open({str(secret)!r}).read()), ("write", lambda: open({str(tmp_path / "x")!r}, "w").write("x")),
                     ("net", lambda: __import__("socket").create_connection(("127.0.0.1", 9), timeout=1))):
        try:
            fn(); out[name] = "ok"
        except Exception as e:
            out[name] = type(e).__name__
    return json.dumps(out)
'''
    run = ek.run_variant(probe, TARGET, _cases()[:1], isolation="seatbelt", deny_read=[secret.parent])
    got = json.loads(run["results"][0]["output"])
    assert got["read"] == "PermissionError" and got["write"] == "PermissionError" and got["net"] == "PermissionError"
    assert not (tmp_path / "x").exists()


def test_failures_of_the_evaluated_code_are_classified():
    assert _eval(CRASHES)["metrics"]["guard_failures"]["ran"]                              # 課題ごとの失敗
    assert _eval("def other(): pass\n")["failure_kind"] == "code_error"                    # 関数がない
    slow = ek.run_variant("import time\ndef build_fix_context(findings, **k):\n    time.sleep(30)\n", TARGET, _cases()[:1],
                          isolation="process", deny_read=[], timeout_s=2)
    assert slow["failure_kind"] == "timeout"


# ---------------------------------------------------------------------- 採否

def test_decision_rejects_score_gaming_and_regressions_and_never_trades_them_for_the_metric():
    assert _decide(GOOD)["verdict"] == "adopt_candidate"
    dropped = _decide(DROPS)
    assert dropped["verdict"] == "rejected" and any("証拠の保全" in r for r in dropped["reasons"])      # 指摘を落とす候補
    assert _decide(DUMPS)["verdict"] == "rejected"                                         # 参照されていない項目を詰め込む、予算を超える
    assert _decide(RANDOM)["verdict"] == "rejected"                                        # 同じ入力に同じ出力を返さない
    assert _decide(CRASHES)["verdict"] == "rejected"
    assert _decide(BASELINE)["verdict"] == "rejected"                                      # 変更なしは、改善の基準に届かない


def test_decision_holds_when_the_effect_cannot_be_established():
    assert _decide(GOOD, _plan(min_sealed_real_cases=3, min_sealed_real_groups=2))["verdict"] == "on_hold"      # 実物の標本が足りない
    assert _decide(GOOD, sealed_evaluations=2)["verdict"] == "on_hold"                     # 封印評価を繰り返し見た
    assert _decide(GOOD, evaluator_matches=False)["verdict"] == "on_hold"                  # 評価器が変わった
    assert _decide(GOOD, _plan(required_isolation="seatbelt"))["verdict"] == "on_hold"     # 隔離が計画より弱い
    no_refs = [c for c in _cases("x", 24) if "no_refs" in c["id"]]
    runs = {f"{v}:{s}": _eval(src, no_refs) for s in ("select", "sealed") for v, src in (("baseline", BASELINE), ("candidate", GOOD))}
    d = dec.decide(_plan(), runs, sealed_evaluations=1, patch_problems=[], evaluator_matches=True)
    assert d["verdict"] == "on_hold" and "改善したことにはしない" in d["reasons"][0]         # 効果を測れる課題がない


def test_decision_rejects_out_of_scope_patches_first():
    d = _decide(GOOD, patch_problems=["保護領域を変更している: config/policy.yaml"])
    assert d["verdict"] == "rejected" and len(d["checks"]) == 1


# ---------------------------------------------------------------------- 許可された範囲と保護領域

def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    for rel in (TARGET.module_path, "config/policy.yaml", "src/aar/gates.py", "src/aar/improve/decide.py", "src/aar/engine/fix.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo, subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def test_patch_scope_allows_only_the_target_file(tmp_path):
    repo, base = _repo(tmp_path)
    assert check_patch(repo, base, base, TARGET) == ["変更がない"]
    (repo / TARGET.module_path).write_text(GOOD, encoding="utf-8")
    assert check_patch(repo, base, _commit(repo), TARGET) == []
    for rel, word in (("config/policy.yaml", "保護領域"), ("src/aar/gates.py", "保護領域"), ("src/aar/improve/decide.py", "保護領域"),
                      ("src/aar/engine/fix.py", "許可された範囲の外"), ("src/aar/engine/new_helper.py", "許可された範囲の外")):
        repo2, base2 = _repo(tmp_path / rel.replace("/", "_"))
        (repo2 / rel).write_text("# changed\n", encoding="utf-8")
        problems = check_patch(repo2, base2, _commit(repo2), TARGET)
        assert problems and word in problems[0], (rel, problems)
    repo3, base3 = _repo(tmp_path / "mode")
    (repo3 / TARGET.module_path).chmod(0o755)
    assert "実行権限" in check_patch(repo3, base3, _commit(repo3), TARGET)[0]
    repo4, base4 = _repo(tmp_path / "delete")
    (repo4 / TARGET.module_path).unlink()
    assert check_patch(repo4, base4, _commit(repo4), TARGET)


def test_protected_areas_cover_gates_evaluator_limits_and_permissions():
    for path in ("config/policy.yaml", "src/aar/gates.py", "src/aar/publish/required.py", "src/aar/workspace.py", "src/aar/limits.py",
                 "src/aar/agents/runner.py", "src/aar/sandbox.py", "src/aar/improve/evalkit.py", "src/aar/improve/decide.py",
                 "src/aar/db/migrations/0006_improvement_ledger.sql", "tests/test_improve.py", ".github/workflows/ci.yml",
                 "src/aar/temporal/workflows.py", "src/aar/audit/research.py"):
        assert is_protected(path), path
    assert not any(is_protected(p) for t in TARGETS.values() for p in t.allowed_paths)     # 許可リストと保護領域は重ならない
    assert all(is_protected(f) for f in EVALUATOR_FILES) and "src/aar/improve/*" in PROTECTED


def test_evaluator_fingerprint_changes_when_any_evaluator_file_changes(tmp_path):
    for rel in EVALUATOR_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    before = evaluator_sha256(tmp_path)
    assert before == evaluator_sha256(ROOT)
    (tmp_path / "src/aar/improve/decide.py").write_text((ROOT / "src/aar/improve/decide.py").read_text(encoding="utf-8").replace("0.90", "0.10"))
    assert evaluator_sha256(tmp_path) != before                                            # 採否の基準を緩めると、評価器のハッシュが変わる


def test_extracted_context_builder_is_identical_to_the_previous_inline_code():
    """切り出しただけで、修正担当に渡る文章は変わらない（稼働中の動作を変えない）。"""
    from aar.engine.fix_context import build_fix_context

    for case in _cases("same", 12):
        f = case["inputs"]["findings"]
        old = json.dumps([{"issue_id": x["issue_id"], "severity": x["severity"], "title": x["title"], **(x.get("detail") or {})} for x in f],
                         ensure_ascii=False, indent=2, default=str)
        assert build_fix_context(f, claims=case["inputs"]["claims"], evidence=case["inputs"]["evidence"]) == old


def test_cases_keep_copies_of_one_study_in_one_group(tmp_path):
    """同じ研究の複製（事前登録が同じ）は同じグループになり、別の分割に分かれない。"""
    for project, prereg in (("RP-1", "A"), ("FX-1", "A"), ("RP-2", "B")):
        base = tmp_path / project
        (base / "repo" / "prereg").mkdir(parents=True)
        (base / "repo" / "prereg" / "cycle1.json").write_text(prereg)
        fix, audit = base / "audits" / "fix-research-round1", base / "audits" / "research-round1" / "audit"
        fix.mkdir(parents=True)
        audit.mkdir(parents=True)
        (fix / "handed_findings.json").write_text(json.dumps([{"issue_id": "I001", "severity": "Major", "title": "t", "claim_ids": "C001"}]))
        (audit / "claims.csv").write_text("claim_id,claim_text,status,limitations\nC001,主張の本文がここにある,Verified,\n", encoding="utf-8")
    cases = ek.cases_from_records(tmp_path, BUDGET)
    groups = {c["id"].split(":")[1]: c["group"] for c in cases}
    assert groups["RP-1"] == groups["FX-1"] != groups["RP-2"] and all(c["key"]["refs"] == {"C001": "主張の本文がここにある"} for c in cases)
    from aar.improve.loop import _assign_splits

    splits = _assign_splits(cases)
    assert splits["dev"] == [groups["RP-1"]] and splits["sealed"] == [groups["RP-2"]]


# ---------------------------------------------------------------------- 台帳と一巡（DB を使う）

@pytest.fixture
def improve_env(db_url, tmp_path, monkeypatch):
    """実物の DB と、一時の作業領域。基盤のリポジトリの代わりに、最小の Git リポジトリを使う。"""
    from aar import config
    from aar.improve import loop

    repo, base = _repo(tmp_path / "platform")
    for rel in EVALUATOR_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    base = _commit(repo)
    ws = tmp_path / "workspaces"
    for project, prereg, n in (("RP-A", "A", 3), ("RP-B", "B", 1)):
        (ws / project / "repo" / "prereg").mkdir(parents=True)
        (ws / project / "repo" / "prereg" / "cycle1.json").write_text(prereg)
        for r in range(1, n + 1):
            fix, audit = ws / project / "audits" / f"fix-research-round{r}", ws / project / "audits" / f"research-round{r}" / "audit"
            fix.mkdir(parents=True)
            audit.mkdir(parents=True)
            (fix / "handed_findings.json").write_text(json.dumps(
                [{"issue_id": "I001", "severity": "Major", "title": f"指摘{r}", "claim_ids": "C001;C002", "observed": "x"}], ensure_ascii=False))
            (audit / "claims.csv").write_text(f"claim_id,claim_text,status,limitations\nC001,主張1の本文 {project}{r} です,Verified,\n"
                                              f"C002,主張2の本文 {project}{r} です,Contradicted,\nC003,参照されない主張の本文,Verified,\n", encoding="utf-8")
    monkeypatch.setenv("AAR_DATABASE_URL", db_url)
    monkeypatch.setenv("AAR_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("AAR_WORKSPACES_DIR", str(ws))
    config.get_settings.cache_clear()
    monkeypatch.setattr(loop, "PROJECT_ROOT", repo)
    monkeypatch.setitem(dec.DEFAULT_PLAN, "required_isolation", "process")
    yield {"repo": repo, "base": base, "ws": ws, "tmp": tmp_path}
    config.get_settings.cache_clear()


def _write_candidate(conn, cid: str, plan_id: int, source: str, actor, extra: dict[str, str] | None = None):
    from aar.improve import loop

    clone = loop.prepare_clone(conn, cid, plan_id)
    (clone / TARGET.module_path).write_text(source, encoding="utf-8")
    for rel, text in (extra or {}).items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text, encoding="utf-8")
    return loop.register_patch(conn, cid, actor)


def test_full_cycle_records_plan_runs_decision_and_actors_without_touching_sources(improve_env):
    from aar.db.core import connect, fetch_all, fetch_one
    from aar.improve import ledger, loop

    actor = ledger.parse_actor("claude_code:test")
    before = {p: p.read_bytes() for p in improve_env["ws"].rglob("*") if p.is_file()}
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=improve_env["repo"], capture_output=True, text=True).stdout
    with connect() as conn:
        weak = loop.scan_weaknesses()
        assert weak[0]["frequency"] == {"fix_rounds": 4, "rounds_with_unresolved_refs": 4, "unresolved_refs": 8, "groups": 2}
        cand = loop.propose_from_weakness(conn, weak[0], actor, improve_env["base"])
        again = loop.propose_from_weakness(conn, weak[0], actor, improve_env["base"])
        assert cand["created"] and not again["created"] and again["id"] == cand["id"]      # 同じ発見元からの乱造を抑える
        plan = loop.freeze_plan(conn, actor)
        assert plan["plan"]["splits"]["dev"] and plan["plan"]["splits"]["sealed"] and not loop.freeze_plan(conn, actor)["created"]
        base_runs = loop.run_baseline(conn, plan["id"], actor, commit=improve_env["base"])
        assert base_runs["sealed"]["metrics"]["ref_resolution_micro"] == 0.0
        assert not loop.run_baseline(conn, plan["id"], actor, commit=improve_env["base"])["sealed"]["created"]   # 再開しても重ねない

        dev = json.loads((loop.candidate_dir(cand["id"]) / "repo" / ".improve_task" / "dev_cases.json").read_text()) \
            if (loop.candidate_dir(cand["id"]) / "repo").exists() else None
        assert dev is None
        reg = _write_candidate(conn, cand["id"], plan["id"], GOOD, actor)
        assert reg["problems"] == []
        handed = json.loads((loop.candidate_dir(cand["id"]) / "repo" / ".improve_task" / "dev_cases.json").read_text())
        held_back = loop._plan_cases(plan, "sealed") + loop._plan_cases(plan, "select")
        # 候補を作る側に渡るのは、開発用の課題だけ（ID も、入力の中身も重ならない）
        assert handed and not ({c["id"] for c in handed} & {c["id"] for c in held_back})
        assert not ({json.dumps(c["inputs"], sort_keys=True) for c in handed} & {json.dumps(c["inputs"], sort_keys=True) for c in held_back})
        first = loop.evaluate_candidate(conn, cand["id"], plan["id"], "select", actor)
        assert first["created"] and not loop.evaluate_candidate(conn, cand["id"], plan["id"], "select", actor)["created"]
        loop.evaluate_candidate(conn, cand["id"], plan["id"], "sealed", actor)
        d = loop.decide_candidate(conn, cand["id"], plan["id"])
        # 指標は基準を満たすが、封印評価の実物が 1 件・1 グループなので、採用候補にはしない
        assert d["verdict"] == "on_hold" and "標本が足りない" in d["reasons"][0]
        row = ledger.get_candidate(conn, cand["id"])
        assert row["status"] == "on_hold" and row["patch"] and row["created_by"] == "claude_code:test"
        events = fetch_all(conn, "SELECT event, actor_kind FROM improvement_events WHERE candidate_id = %s ORDER BY id", (cand["id"],))
        kinds = {e["actor_kind"] for e in events}
        assert kinds == {"claude_code", "policy"} and "human" not in kinds                  # AI の操作を人の操作として記録しない
        assert [e["event"] for e in events if e["actor_kind"] == "policy"] == ["decided", "status_set"]
        assert fetch_one(conn, "SELECT count(*) AS n FROM improvement_runs")["n"] == 5       # 基準値 3（dev・select・sealed）＋候補 2。やり直しの分は増えない
    assert {p: p.read_bytes() for p in improve_env["ws"].rglob("*") if p.is_file()} == before            # 研究の記録は変わらない
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=improve_env["repo"], capture_output=True, text=True).stdout == head_before
    assert subprocess.run(["git", "status", "--porcelain"], cwd=improve_env["repo"], capture_output=True, text=True).stdout == ""


def test_out_of_scope_and_gaming_candidates_are_rejected_with_reasons_that_can_be_searched(improve_env):
    from aar.db.core import connect
    from aar.improve import ledger, loop

    actor = ledger.parse_actor("claude_code:test")
    with connect() as conn:
        weak = loop.scan_weaknesses()[0]
        plan = loop.freeze_plan(conn, actor)
        loop.run_baseline(conn, plan["id"], actor, commit=improve_env["base"])
        first = loop.propose_from_weakness(conn, weak, actor, improve_env["base"])
        reg = _write_candidate(conn, first["id"], plan["id"], GOOD, actor, {"config/policy.yaml": "gates: {}\n"})
        assert any("保護領域" in p for p in reg["problems"]) and ledger.get_candidate(conn, first["id"])["status"] == "invalid"
        with pytest.raises(ValueError):
            loop.evaluate_candidate(conn, first["id"], plan["id"], "select", actor)        # 範囲外の候補は評価しない
        assert loop.decide_candidate(conn, first["id"], plan["id"])["verdict"] == "rejected"

        second = loop.propose_from_weakness(conn, weak, actor, improve_env["base"])        # 前の候補は終わっているので、次を作れる
        assert second["created"] and second["id"] != first["id"]
        _write_candidate(conn, second["id"], plan["id"], DROPS, actor)
        for split in ("select", "sealed"):
            loop.evaluate_candidate(conn, second["id"], plan["id"], split, actor)
        d = loop.decide_candidate(conn, second["id"], plan["id"])
        assert d["verdict"] == "rejected"
        row = ledger.get_candidate(conn, second["id"])
        assert "条件が変われば" in row["rejected_under"]                                    # 「常に無効」とは記録しない
        assert {c["id"] for c in ledger.search(conn, "証拠の保全")} == {second["id"]} and len(ledger.search(conn, status="rejected")) == 2
        third = loop.propose_from_weakness(conn, weak, actor, improve_env["base"])
        _write_candidate(conn, third["id"], plan["id"], BASELINE.replace("indent=2", "indent=1"), actor)
        assert loop.decide_candidate(conn, third["id"], plan["id"])["verdict"] == "on_hold"      # 評価が済んでいない候補は、採否を決めない
        assert loop.propose_from_weakness(conn, weak, actor, improve_env["base"])["id"] == third["id"]     # 保留中は、同じ発見元から増やさない
        for split in ("select", "sealed"):
            loop.evaluate_candidate(conn, third["id"], plan["id"], split, actor)
        assert loop.decide_candidate(conn, third["id"], plan["id"])["verdict"] == "rejected"     # 形だけ変えた候補は、改善の基準に届かない
        with pytest.raises(ValueError, match="上限"):
            loop.propose_from_weakness(conn, weak, actor, improve_env["base"])             # 同じ発見元からは 3 件まで


def test_tampering_with_cases_or_evaluator_is_detected(improve_env):
    from aar.db.core import connect
    from aar.improve import ledger, loop

    actor = ledger.parse_actor("codex:test")
    with connect() as conn:
        plan = loop.freeze_plan(conn, actor)
        cases = loop.workdir() / "plans" / plan["plan_sha256"] / "cases.json"
        cases.chmod(0o600)
        original = cases.read_text(encoding="utf-8")
        cases.write_text(original.replace("主張1の本文", "書き換えた正解"), encoding="utf-8")
        with pytest.raises(RuntimeError, match="課題が"):
            loop.run_baseline(conn, plan["id"], actor, commit=improve_env["base"])         # 期待結果の改変
        cases.write_text(original, encoding="utf-8")
        decide_py = improve_env["repo"] / "src/aar/improve/decide.py"
        decide_py.write_text(decide_py.read_text(encoding="utf-8").replace('"min_level": 0.90', '"min_level": 0.10'), encoding="utf-8")
        run = loop.run_baseline(conn, plan["id"], actor, commit=improve_env["base"])["sealed"]
        assert run["status"] == "failed" and run["failure_kind"] == "evaluator_changed"     # 採否の基準の改変
    with connect() as conn:
        for table in ("improvement_runs", "improvement_events", "improvement_eval_plans"):
            with pytest.raises(Exception, match="追記だけ"), conn.transaction():
                conn.execute(f"UPDATE {table} SET id = id")                                # 記録は書き換えられない
            with pytest.raises(Exception, match="追記だけ"), conn.transaction():
                conn.execute(f"DELETE FROM {table}")


def test_actor_is_required_and_generation_respects_limits_and_resume(improve_env):
    import asyncio

    from aar.db.core import connect, fetch_all
    from aar.improve import ledger, loop
    from aar.models import AgentResult

    with pytest.raises(ValueError):
        ledger.parse_actor(None)                                                           # 操作主体を省略できない（人の操作と決めつけない）
    with pytest.raises(ValueError):
        ledger.parse_actor("user")
    actor = ledger.parse_actor("scheduler:weekly")
    calls: list[str] = []

    class Limited:
        name = "claude"

        async def run(self, prompt, *, cwd, **kw):
            calls.append("limited")
            return AgentResult(ok=False, agent="claude", error_kind="rate_limit", error="claude は利用上限の解除待ち。それまでは問い合わせない")

    class Writes:
        name = "claude"

        async def run(self, prompt, *, cwd, **kw):
            calls.append("writes")
            assert (Path(cwd) / ".improve_task" / "task.md").exists() and Path(cwd) != improve_env["repo"]      # 稼働中のチェックアウトではない
            (Path(cwd) / TARGET.module_path).write_text(GOOD, encoding="utf-8")
            return AgentResult(ok=True, agent="claude", model="sonnet", cost_usd=0.12, session_id="s1")

    with connect() as conn:
        plan = loop.freeze_plan(conn, actor)
        cand = loop.propose_from_weakness(conn, loop.scan_weaknesses()[0], actor, improve_env["base"])
        out = asyncio.run(loop.generate(conn, cand["id"], plan["id"], actor, Limited()))
        assert out["ok"] is False and out["error_kind"] == "rate_limit" and ledger.get_candidate(conn, cand["id"])["status"] == "proposed"
        out = asyncio.run(loop.generate(conn, cand["id"], plan["id"], actor, Writes()))
        assert out["ok"] and out["problems"] == []
        assert "skipped" in asyncio.run(loop.generate(conn, cand["id"], plan["id"], actor, Writes()))      # 済んだ生成を呼び直さない
        assert calls == ["limited", "writes"]
        row = ledger.get_candidate(conn, cand["id"])
        assert row["status"] == "generated" and row["generation"]["external_model_calls"] == 1 and row["generation"]["cost_is_estimate"]
        events = fetch_all(conn, "SELECT event, actor_kind, actor_detail FROM improvement_events WHERE candidate_id = %s ORDER BY id", (cand["id"],))
        assert ("generation_deferred", "claude_code") in {(e["event"], e["actor_kind"]) for e in events}
        assert events[0]["actor_kind"] == "scheduler" and events[-1]["actor_kind"] == "claude_code"       # 起動した主体と、作った主体を分ける
        assert fetch_all(conn, "SELECT purpose FROM cost_ledger WHERE purpose LIKE '改善候補の生成%'")       # 既存の費用台帳に残る


def test_areas_and_sources_register_reading_level(db_url, monkeypatch):
    from aar import config
    from aar.db.core import connect, fetch_all
    from aar.improve import areas

    monkeypatch.setenv("AAR_DATABASE_URL", db_url)
    config.get_settings.cache_clear()
    with connect() as conn:
        assert areas.sync(conn) == {"areas": 10, "sources": 12} and areas.sync(conn) == {"areas": 10, "sources": 12}
        rows = fetch_all(conn, "SELECT read_level, kind FROM improvement_sources")
        assert len(rows) == 12 and not [r for r in rows if r["kind"] == "paper" and r["read_level"] != "abstract"]   # 要旨だけの論文を、読了としない
        assert all(a["reject_conditions"] and a["comparison"] for a in fetch_all(conn, "SELECT * FROM improvement_areas"))
    config.get_settings.cache_clear()
