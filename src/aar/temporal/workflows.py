"""Temporal のワークフロー。システム全体の進行だけを管理する。

研究の中身（設計の考察、コードの作成、実験の探索）はアクティビティの中で行い、ここには持ち込まない。
ワークフローは決定的でなければならないので、時刻・乱数・I/O は使わず、アクティビティは名前で呼ぶ
（重いモジュールをワークフローのサンドボックスに読み込ませないため）。

研究1件が1つのワークフロー（Workflow ID = 研究 ID）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=5), maximum_attempts=3)
QUICK = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(minutes=1), maximum_attempts=10)
RATE_LIMIT_WAIT = timedelta(minutes=30)      # 旧方式（一定間隔）。実行中だった研究の再生のために残す
MAX_RATE_LIMIT_WAITS = 12
# 新方式: 解除の見込み時刻まで待つ。1つの工程で自動的に待つのはこの回数まで、1回の待ちはこの長さまで。
# 超える場合は、人が再開するまで止まる（上限を回避するための際限のない再試行はしない）
MAX_AUTO_RATE_WAITS = 5
MAX_AUTO_RATE_WAIT = timedelta(hours=8)
MAX_IMPLEMENT_ATTEMPTS = 3


@dataclass
class ResearchInput:
    project_id: str
    max_cycles: int = 3
    max_research_audit_rounds: int = 3
    max_publication_audit_rounds: int = 2
    phase_timeout_minutes: int = 90
    # やり直し（利用者が、終わった研究を再開した場合）。audit: 独立監査から / publication: 公開品質監査から。空なら最初から
    resume_from: str = ""
    cycle: int = 1
    run_no: int = 1
    start_research_round: int = 1
    start_publication_round: int = 1
    fix_kind: str = ""          # 最後に通らなかった監査の種類（research / publication_quality）
    fix_round: int = 0          # その回


@dataclass
class ResearchStatus:
    project_id: str
    phase: str = "registered"
    state: str = "running"          # running / paused / on_hold / finished
    hold_kind: str = ""
    hold_message: str = ""
    cycle: int = 0
    research_audit_round: int = 0
    publication_audit_round: int = 0
    outcome: str = ""
    history: list[str] = field(default_factory=list)


class _Cancelled(Exception):
    pass


def _hold_kind(err: BaseException) -> tuple[str, str] | None:
    """アクティビティの失敗が「保留にすべきもの」なら (種類, 説明) を返す。"""
    cause = err.cause if isinstance(err, ActivityError) else err
    if isinstance(cause, ApplicationError) and (cause.type or "").startswith("Hold:"):
        return cause.type.split(":", 1)[1], cause.message  # type: ignore[union-attr]
    return None


@workflow.defn
class ResearchWorkflow:
    def __init__(self) -> None:
        self.s = ResearchStatus(project_id="")
        self._paused = False
        self._cancel = False
        self._resume = False
        self._seq = 0

    # ------------------------------------------------------------ 外部からの操作

    @workflow.signal
    def pause(self) -> None:
        """一時停止。実行中の工程が終わったところで止まる。"""
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        """一時停止・保留からの再開。"""
        self._paused = False
        self._resume = True

    @workflow.signal
    def cancel(self) -> None:
        """中止。実行中の工程も打ち切る。ここまでの結果は残る。"""
        self._cancel = True

    @workflow.query
    def status(self) -> ResearchStatus:
        return self.s

    @workflow.update
    async def change_budget(self, budget_usd: float) -> float:
        """費用上限を変える（保留からの再開前に使う）。"""
        await workflow.execute_activity("set_budget", args=[self.s.project_id, budget_usd],
                                        start_to_close_timeout=timedelta(seconds=30), retry_policy=QUICK)
        return budget_usd

    @change_budget.validator
    def _validate_budget(self, budget_usd: float) -> None:
        if not (0 < budget_usd <= 500):
            raise ValueError("費用上限は 0 より大きく 500 ドル以下にする")

    # ------------------------------------------------------------ 共通の進め方

    async def _quick(self, name: str, *args: Any) -> Any:
        return await workflow.execute_activity(name, args=list(args), start_to_close_timeout=timedelta(minutes=2),
                                               retry_policy=QUICK)

    async def _wait_resume(self, kind: str, message: str, timeout: timedelta | None = None) -> None:
        """保留にして、再開の合図（または時間）を待つ。"""
        self._seq += 1
        self.s.state, self.s.hold_kind, self.s.hold_message = ("paused" if kind == "paused" else "on_hold"), kind, message
        self._resume = False
        await self._quick("mark_hold", self.s.project_id, kind, message, self._seq)
        by = "user"
        try:
            await workflow.wait_condition(lambda: self._resume or self._cancel, timeout=timeout)
        except TimeoutError:
            by = "timer"
        if self._cancel:
            raise _Cancelled()
        self._paused = False
        self.s.state, self.s.hold_kind, self.s.hold_message = "running", "", ""
        await self._quick("mark_resumed", self.s.project_id, by, self._seq)

    async def _step(self, phase: str, name: str, *args: Any, minutes: int = 90) -> dict[str, Any]:
        """工程を1つ実行する。一時停止・中止・保留をここでまとめて扱う。"""
        rate_waits = 0
        while True:
            if self._cancel:
                raise _Cancelled()
            if self._paused:
                await self._wait_resume("paused", "利用者が一時停止した")
            self.s.phase = phase
            handle = workflow.start_activity(
                name, args=[self.s.project_id, *args], start_to_close_timeout=timedelta(minutes=minutes),
                heartbeat_timeout=timedelta(minutes=3), retry_policy=RETRY)
            cancel_wait = asyncio.ensure_future(workflow.wait_condition(lambda: self._cancel))
            try:
                await workflow.wait([handle, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
                if not handle.done():
                    handle.cancel()
                    try:
                        await handle
                    except BaseException:  # noqa: BLE001 - 打ち切りの結果は使わない
                        pass
                    raise _Cancelled()
                result = await handle
                self.s.history.append(phase)
                return result
            except ActivityError as e:
                held = _hold_kind(e)
                if not held:
                    raise
                kind, message = held
                if kind == "rate_limit" and workflow.patched("rate-limit-reset-time"):
                    rate_waits += 1
                    plan = await self._quick("rate_limit_wait", self.s.project_id, rate_waits, MAX_AUTO_RATE_WAITS)
                    wait = timedelta(seconds=int(plan["seconds"]))
                    if rate_waits > MAX_AUTO_RATE_WAITS:
                        await self._wait_resume(kind, f"利用上限が {MAX_AUTO_RATE_WAITS} 回待っても解けない。自動の再試行をやめた。"
                                                      f"利用枠を確かめてから、人が再開する（aar projects resume）: {message}")
                        rate_waits = 0
                    elif wait > MAX_AUTO_RATE_WAIT:
                        await self._wait_resume(kind, f"利用上限の解除の見込みが {plan['until_local']} で、自動で待つ上限を超える。"
                                                      f"解除後に、人が再開する（aar projects resume）: {message}")
                    else:
                        await self._wait_resume(kind, f"利用上限のため {plan['until_local']} ごろまで待って自動で再開する"
                                                      f"（{rate_waits}/{MAX_AUTO_RATE_WAITS} 回目。それまで問い合わせない）: {message}",
                                                timeout=wait)
                elif kind == "rate_limit":
                    rate_waits += 1
                    if rate_waits > MAX_RATE_LIMIT_WAITS:
                        await self._wait_resume(kind, f"利用上限が解けない: {message}")
                    else:   # 時間をおいて自動で再開する
                        await self._wait_resume(kind, f"利用上限のため {RATE_LIMIT_WAIT} 待って自動で再開する: {message}",
                                                timeout=RATE_LIMIT_WAIT)
                else:
                    await self._wait_resume(kind, message)
            finally:
                if not cancel_wait.done():
                    cancel_wait.cancel()

    # ------------------------------------------------------------ 本体

    @workflow.run
    async def run(self, inp: ResearchInput) -> dict[str, Any]:
        self.s.project_id = inp.project_id
        outcome, reason = "failed", ""
        try:
            outcome, reason = await self._research(inp)
        except _Cancelled:
            outcome, reason = "cancelled", "利用者が中止した"
        except ActivityError as e:
            cause = e.cause
            outcome = "failed"
            reason = f"{self.s.phase} で失敗: {getattr(cause, 'message', None) or cause}"
        self.s.phase, self.s.state, self.s.outcome = "finished", "finished", outcome
        final = await workflow.execute_activity(
            "finalize_project", args=[inp.project_id, outcome, reason[:900]],
            start_to_close_timeout=timedelta(minutes=5), retry_policy=QUICK)
        await workflow.wait_condition(workflow.all_handlers_finished)
        return {"project_id": inp.project_id, "outcome": outcome, "reason": reason, **final}

    async def _research(self, inp: ResearchInput) -> tuple[str, str]:
        t = inp.phase_timeout_minutes
        started = workflow.now()
        if inp.resume_from in ("audit", "publication"):
            # 最後の監査の指摘がまだ直されていなければ（上限回数で止まった場合）、まず直してから監査をやり直す
            self.s.cycle = inp.cycle
            run_no = inp.run_no
            research_changed = False
            if inp.fix_kind:
                fix = await self._step(f"fix_{inp.fix_kind}", "fix_audit_findings", inp.fix_kind, inp.fix_round, minutes=t)
                research_changed = bool(fix.get("research_changed"))
                if fix.get("needs_rerun"):
                    run_no += 1
                    await self._rerun(inp, run_no, started)
            # 公開品質監査からのやり直しでも、直した内容が研究に及んでいれば独立監査からやり直す
            skip_research = inp.resume_from == "publication" and not research_changed
            label = "公開品質監査からやり直した" if skip_research else "独立監査からやり直した"
            return await self._audit_and_publish(inp, started, run_no, label, skip_research=skip_research)
        await self._step("prepare", "prepare_project", minutes=10)
        await self._step("understand", "understand_paper", minutes=t)

        # --- 仮説 → 実験 → 観察 → 批判的分析 → 次の仮説 のサイクル
        previous: dict[str, Any] | None = None
        cycle = 0
        stop_reason = ""
        while True:
            cycle += 1
            self.s.cycle = cycle
            await self._step("design", "design_and_preregister", cycle, previous, minutes=t)
            feedback, ok = "", False
            for attempt in range(1, MAX_IMPLEMENT_ATTEMPTS + 1):
                await self._step("implement", "implement_experiment", cycle, attempt, feedback, minutes=t)
                pilot = await self._step("pilot", "run_pilot", cycle, attempt, minutes=30)
                if pilot["ok"]:
                    ok = True
                    break
                feedback = f"[{pilot['stage']}] {pilot['log']}"
            if not ok:
                if cycle == 1:
                    return "failed", f"実装が {MAX_IMPLEMENT_ATTEMPTS} 回の修正でも予備実験を通らなかった: {feedback[-400:]}"
                stop_reason = f"サイクル{cycle}の実装が予備実験を通らなかったため、サイクル{cycle - 1}までの結果で進める"
                self.s.cycle = cycle - 1
                break
            await self._step("main_experiment", "run_main_experiment", cycle, 1, minutes=t)
            elapsed = (workflow.now() - started).total_seconds() / 3600
            analysis = await self._step("analysis", "analyze_results", cycle, 1, elapsed, minutes=t)
            if not analysis["continue"] or cycle >= inp.max_cycles:
                stop_reason = analysis["reason"]
                break
            previous = analysis["next"]

        return await self._audit_and_publish(inp, started, 1, stop_reason)

    async def _audit_and_publish(self, inp: ResearchInput, started: Any, run_no: int, stop_reason: str, *,
                                 skip_research: bool = False) -> tuple[str, str]:
        t = inp.phase_timeout_minutes
        # --- 独立監査 → 修正 → 再監査
        if skip_research:
            self.s.research_audit_round = inp.start_research_round - 1
        else:
            research_ok, audit = await self._research_audit_loop(inp, run_no, start_round=inp.start_research_round,
                                                                 extra_rounds=inp.max_research_audit_rounds - 1)
            if not research_ok:
                return "not_publishable", self._blocking_text("研究品質ゲートを通過できなかった", audit)

        # --- 発信用の成果物 → 公開品質監査 → 改善
        first_pub = inp.start_publication_round
        last_pub = first_pub + inp.max_publication_audit_rounds - 1
        await self._step("deliverables", "build_deliverables", first_pub, False, minutes=t)
        pub: dict[str, Any] = {}
        for rnd in range(first_pub, last_pub + 1):
            self.s.publication_audit_round = rnd
            pub = await self._step("publication_audit", "publication_audit", rnd, minutes=60)
            if pub["passed"]:
                break
            if rnd == last_pub:
                return "not_publishable", self._blocking_text("公開品質ゲートを通過できなかった", pub)
            fix = await self._step("fix_publication", "fix_audit_findings", "publication_quality", rnd, minutes=t)
            if fix.get("research_changed"):
                # 研究内容に及ぶ修正をした場合は、研究監査へ戻す
                if fix.get("needs_rerun"):
                    run_no += 1
                    await self._rerun(inp, run_no, started)
                research_ok, audit = await self._research_audit_loop(inp, run_no, start_round=self.s.research_audit_round + 1,
                                                                     extra_rounds=1)
                if not research_ok:
                    return "not_publishable", self._blocking_text("公開向けの修正のあと、研究品質ゲートを通過できなかった", audit)
            await self._step("deliverables", "refresh_deliverables", rnd, minutes=15)

        submit = await self._step("submit", "submit_candidate", minutes=15)
        note = {"created": f"PR を作成した: {submit.get('url')}", "exists": f"PR は作成済み: {submit.get('url')}",
                "held": f"公開候補はローカルのブランチにある（PR は保留: {submit.get('reason')}）",
                "skipped": f"公開候補はローカルのブランチにある（{submit.get('reason')}）"}[submit["status"]]
        return "publishable", f"研究品質ゲートと公開品質ゲート（{pub.get('score')} 点）を通過。{note}。{stop_reason}"

    async def _rerun(self, inp: ResearchInput, run_no: int, started: Any) -> None:
        """コードやデータを直したあと、最新サイクルの本実験と分析をやり直す。"""
        cycle = self.s.cycle
        pilot = await self._step("pilot", "run_pilot", cycle, 100 + run_no, minutes=30)
        if not pilot["ok"]:
            raise ApplicationError(f"修正後のコードが予備実験を通らない: {pilot['log'][-400:]}", non_retryable=True)
        await self._step("main_experiment", "run_main_experiment", cycle, run_no, minutes=inp.phase_timeout_minutes)
        elapsed = (workflow.now() - started).total_seconds() / 3600
        await self._step("analysis", "analyze_results", cycle, run_no, elapsed, minutes=inp.phase_timeout_minutes)

    async def _research_audit_loop(self, inp: ResearchInput, run_no: int, *, start_round: int = 1,
                                   extra_rounds: int | None = None) -> tuple[bool, dict[str, Any]]:
        last = start_round + (extra_rounds if extra_rounds is not None else inp.max_research_audit_rounds - 1)
        audit: dict[str, Any] = {}
        started = workflow.now()
        for rnd in range(start_round, last + 1):
            self.s.research_audit_round = rnd
            audit = await self._step("research_audit", "research_audit", rnd, minutes=90)
            if audit["passed"]:
                return True, audit
            if rnd == last:
                break
            fix = await self._step("fix_research", "fix_audit_findings", "research", rnd, minutes=inp.phase_timeout_minutes)
            if fix.get("needs_rerun"):
                run_no += 1
                await self._rerun(inp, run_no, started)
        return False, audit

    @staticmethod
    def _blocking_text(head: str, result: dict[str, Any]) -> str:
        items = "、".join(f"{c['name']}" + (f"（{c['detail']}）" if c.get("detail") else "") for c in result.get("blocking", []))
        return f"{head}: {items}"[:900]


# ====================================================================== 定期実行


@workflow.defn
class IngestWorkflow:
    """日次の論文収集。"""

    @workflow.run
    async def run(self, day: str = "") -> dict[str, Any]:
        day = day or workflow.now().date().isoformat()
        return await workflow.execute_activity(
            "ingest_papers", args=[day], start_to_close_timeout=timedelta(minutes=45),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(initial_interval=timedelta(minutes=2), maximum_attempts=3))


@workflow.defn
class ProposalWorkflow:
    """週次の検証企画の生成。"""

    @workflow.run
    async def run(self, batch_key: str = "") -> dict[str, Any]:
        if not batch_key:
            iso = workflow.now().isocalendar()
            batch_key = f"{iso.year}-W{iso.week:02d}"
        retry = RetryPolicy(initial_interval=timedelta(minutes=1), maximum_attempts=3)
        papers: list[str] = await workflow.execute_activity(
            "select_candidates", args=[batch_key], start_to_close_timeout=timedelta(minutes=60),
            heartbeat_timeout=timedelta(minutes=5), retry_policy=retry)
        created, skipped = 0, []
        for pid in papers:
            try:
                r = await workflow.execute_activity(
                    "generate_proposal", args=[batch_key, pid], start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=3), retry_policy=retry)
                if r.get("created"):
                    created += 1
                elif r.get("reason") != "作成済み":
                    skipped.append(r.get("reason", ""))
            except ActivityError as e:   # 1件の失敗で全体を止めない
                skipped.append(str(getattr(e.cause, "message", e))[:200])
        await workflow.execute_activity("notify_proposals", args=[batch_key, created, skipped],
                                        start_to_close_timeout=timedelta(minutes=1), retry_policy=QUICK)
        return {"batch_key": batch_key, "candidates": len(papers), "created": created, "skipped": skipped}


ALL_WORKFLOWS = [ResearchWorkflow, IngestWorkflow, ProposalWorkflow]
