"""AI Scientist v3（SakanaAI/AI-Scientist-v3、パッケージ名 agentic-scientist、CLI は `agsci`）のアダプター。

外部依存として vendor/AI-Scientist-v3 に置いたものを、サブプロセスで外側から制御する。中身は再実装しない。
向いているのは「評価関数の点数を上げるプログラムを探す」型の研究で、タスクは
task.md・evaluate.py・capture.py・program_0/ を持つディレクトリとして渡す（上流の harness-contract）。

調査で分かった制約と、ここでの扱い（docs/research/ai-scientist-v3.md、ADR-0003）:
  * 費用の上限機能がない（計測だけ）→ 回数と時間の上限を必ず渡し、終了後に cost_summary.json を台帳へ写す。
  * フルパイプライン（論文生成まで）は数 GB の Docker イメージが必須 → ここでは探索（investigate）だけを使い、
    監査と成果物はこのシステムの側で行う。
  * エージェントが全権限（--allow yolo）で動く → ネイティブ実行はホストを危険にさらすので、
    allow_host_execution を明示的に指定しない限り起動しない。
  * 0.1.0 Alpha でリリースタグがない → 動作確認したコミットに固定して使う（PINNED_COMMIT）。

**このアダプターは 2026-11-23 時点で実機での実行を確認していない**（ディスク残量の不足で依存関係を導入できなかった）。
コマンドの組み立てと結果の読み取りは、上流のコードと文書を読んで書いたもので、単体テストは形式だけを確かめている。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PINNED_COMMIT = "f8ae47d"   # 2026-11-23 に内容を調べたコミット


@dataclass
class AisLimits:
    max_idea_cycles: int = 2
    max_program_evals_per_attempt: int = 2
    run_timeout_seconds: int = 3600
    agent: str = "claude"          # claude / codex
    model: str = ""


class AiScientistUnavailable(RuntimeError):
    pass


def doctor(ais_dir: Path) -> dict[str, Any]:
    """使える状態かを確かめる。"""
    out: dict[str, Any] = {"installed": (ais_dir / "pyproject.toml").exists(), "path": str(ais_dir)}
    if out["installed"]:
        head = ais_dir / ".git" / "HEAD"
        out["uv"] = bool(shutil.which("uv"))
        out["has_venv"] = (ais_dir / ".venv").exists()
        out["git"] = head.exists()
    out["ready"] = bool(out.get("installed") and out.get("has_venv"))
    return out


def build_task_dir(task_dir: Path, *, goal: str, rubrics: list[str], evaluate_py: str, capture_py: str,
                   program_files: dict[str, str], entrypoint: str = "main.py", requirements: str = "") -> None:
    """上流の決まりに沿ったタスクディレクトリを作る。"""
    (task_dir / "program_0" / "src").mkdir(parents=True, exist_ok=True)
    rub = "\n".join(f"- {r}" for r in rubrics)
    (task_dir / "task.md").write_text(f"# Goal\n\n{goal}\n\n# Rubrics\n\n{rub}\n", encoding="utf-8")
    (task_dir / "evaluate.py").write_text(evaluate_py, encoding="utf-8")
    (task_dir / "capture.py").write_text(capture_py, encoding="utf-8")
    (task_dir / "program_0" / "program_spec.toml").write_text(
        f'schema_version = 1\ncode_root = "src"\n\n[entrypoint]\npath = "{entrypoint}"\nlanguage = "python"\ntype = "script"\n',
        encoding="utf-8")
    for rel, text in program_files.items():
        p = task_dir / "program_0" / "src" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if requirements:
        (task_dir / "requirements.txt").write_text(requirements, encoding="utf-8")


def investigate_command(ais_dir: Path, task_dir: Path, run_dir: Path, limits: AisLimits, *, resume: bool) -> list[str]:
    """探索ループ（agsci investigate）のコマンド。Docker を使わないネイティブ実行。"""
    cmd = ["uv", "run", "--project", str(ais_dir), "agsci", "investigate",
           f"agent.name={limits.agent}", f"paths.task_dir={task_dir}", f"paths.run_dir={run_dir}",
           f"execution.max_idea_cycles={limits.max_idea_cycles}",
           f"execution.max_program_evals_per_attempt={limits.max_program_evals_per_attempt}",
           f"execution.run_timeout_seconds={limits.run_timeout_seconds}", "container.enabled=false", "logging.format=json"]
    if limits.model:
        cmd.append(f"agent.model={limits.model}")
    if resume:
        cmd.append("execution.resume=true")
    return cmd


async def run_investigation(ais_dir: Path, task_dir: Path, run_dir: Path, limits: AisLimits, *,
                            allow_host_execution: bool = False, heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """探索を実行する。run_dir が既にあれば続きから再開する。"""
    d = doctor(ais_dir)
    if not d["ready"]:
        raise AiScientistUnavailable(f"AI Scientist v3 が使える状態でない: {d}（docs/research/ai-scientist-v3.md の導入手順を参照）")
    if not allow_host_execution:
        raise AiScientistUnavailable("AI Scientist v3 のネイティブ実行は、エージェントがホスト上で全権限で動く。"
                                     "専用のユーザーや仮想マシンで動かす場合だけ allow_host_execution を指定する")
    resume = (run_dir / "outer_state.json").exists()
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")} | {"HEADLESS_BILLING": "subscription"}
    proc = await asyncio.create_subprocess_exec(
        *investigate_command(ais_dir, task_dir, run_dir, limits, resume=resume), cwd=str(ais_dir), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    comm = asyncio.ensure_future(proc.communicate())
    hard_limit = limits.run_timeout_seconds * (limits.max_idea_cycles + 1)
    waited = 0
    try:
        while not comm.done():
            await asyncio.wait({comm}, timeout=15)
            waited += 15
            if heartbeat:
                heartbeat()
            if waited > hard_limit:
                break
    finally:
        if not comm.done():
            try:
                os.killpg(proc.pid, 15)   # 上流は SIGTERM で子プロセス群を止めてから抜ける
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(comm), timeout=30)
            except (TimeoutError, asyncio.CancelledError):
                pass
    out = comm.result()[0].decode(errors="replace") if comm.done() and not comm.cancelled() else ""
    (run_dir / "aar_stdout.log").write_text(out[-200000:], encoding="utf-8") if run_dir.exists() else None
    return read_results(run_dir) | {"returncode": proc.returncode, "resumed": resume}


def read_results(run_dir: Path) -> dict[str, Any]:
    """実行結果を読む。終了コード 0 には degraded も含まれるので、成否はイベントと成果物で判断する。"""
    def load(rel: str) -> Any:
        p = run_dir / rel
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except ValueError:
            return None

    def jsonl(rel: str) -> list[dict[str, Any]]:
        p = run_dir / rel
        rows = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    events = jsonl("events.jsonl") + jsonl("pipeline_events.jsonl")
    end = next((e for e in reversed(events) if e.get("event") in ("pipeline_end", "investigation_end")), None)
    attempts = jsonl("attempts_history.jsonl")
    cost = load("metrics/cost_summary.json") or {}
    best = load("best_program_manifest.json")
    return {
        "status": (end or {}).get("status") or ("completed" if best else "unknown"),
        "outcome": (end or {}).get("outcome"),
        "n_ideas": len(jsonl("ideas_history.jsonl")), "n_attempts": len(attempts),
        "best": best, "attempts": attempts[-50:],
        "cost_usd_estimate": float(cost.get("total_cost_usd") or cost.get("total_usd") or 0.0),
        "conclusions": (run_dir / "conclusions.md").read_text(encoding="utf-8")[:20000] if (run_dir / "conclusions.md").exists() else "",
    }
