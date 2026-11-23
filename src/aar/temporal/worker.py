"""Temporal のワーカー。ワークフローとアクティビティを実行する常駐プロセス。"""

from __future__ import annotations

import asyncio
import logging
import signal

from temporalio.worker import Worker

from aar import service
from aar.config import get_settings
from aar.db.core import migrate
from aar.temporal.activities import ALL_ACTIVITIES
from aar.temporal.workflows import ALL_WORKFLOWS

log = logging.getLogger("aar.worker")


async def run_worker(*, schedules: bool = True) -> None:
    settings = get_settings()
    migrate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    client = await service.get_client()
    worker = Worker(
        client, task_queue=settings.task_queue, workflows=ALL_WORKFLOWS, activities=ALL_ACTIVITIES,
        max_concurrent_activities=2,                       # メモリ 8GB の PC を想定し、同時に動かす工程を絞る
        graceful_shutdown_timeout=__import__("datetime").timedelta(seconds=20),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with worker:
        if schedules:
            created = await service.ensure_schedules()
            if created:
                log.info("定期実行を登録: %s", ", ".join(created))
        started = await service.start_pending()
        if started:
            log.info("登録済みの研究を開始: %s", ", ".join(started))
        log.info("ワーカー起動（task queue=%s, mode=%s）", settings.task_queue, settings.mode)

        async def reconcile_loop() -> None:
            while True:
                try:
                    for f in await service.reconcile_projects():
                        log.warning("ワークフローが終わっていた研究を失敗として確定: %s（%s）", f["project_id"], f["workflow_state"])
                except Exception as e:  # noqa: BLE001 - 見回りの失敗でワーカーを止めない
                    log.warning("状態の見回りに失敗: %s", str(e)[:200])
                await asyncio.sleep(600)

        watcher = asyncio.create_task(reconcile_loop())
        await stop.wait()
        watcher.cancel()
        log.info("ワーカーを停止する（実行中の工程は Temporal が再開時にやり直す）")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
