"""Web UI（サーバー側で描画する素朴な画面）。JavaScript は使わない。

サービス層（aar.service）は get_service() 経由で受け取る。テストでは
app.dependency_overrides[get_service] で偽物に差し替える。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aar.db.core import connect
from aar.ui import queries as q

TEMPLATE_DIR = Path(__file__).parent / "templates"

STATUS_LABELS = {
    "registered": "登録済み", "running": "実行中", "paused": "一時停止", "on_hold": "保留",
    "completed": "完了", "failed": "失敗", "cancelled": "中止",
}
OUTCOME_LABELS = {
    "publishable": "公開可", "publishable_with_revisions": "修正すれば公開可",
    "not_publishable": "公開不可", "failed": "失敗", "on_hold": "保留", "cancelled": "中止",
}
KIND_LABELS = {
    "paper": "論文", "proposal": "企画", "hypothesis": "仮説",
    "experiment": "実験", "finding": "監査指摘", "lesson": "教訓",
}
FINDING_STATUS_LABELS = {
    "open": "未対応", "fix_attempted": "修正を試行", "resolved": "解決済み", "unresolved": "未解決",
}
LEVEL_LABELS = {"info": "情報", "success": "成功", "warning": "注意", "error": "エラー"}
CONTROL_ACTIONS = ("pause", "resume", "cancel")


def get_service():
    """サービス層を返す（import は遅延。テストでは dependency_overrides で差し替える）。"""
    import aar.service as s

    return s


Service = Annotated[Any, Depends(get_service)]


def safe_url(value: Any) -> str | None:
    """http:// か https:// で始まる文字列だけを URL として返す。"""
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()
    return None


def fmt_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    if isinstance(value, int | float | Decimal):
        if isinstance(value, int):
            return str(value)
        return f"{float(value):.{digits}f}"
    return str(value)


def fmt_json(value: Any, limit: int = 300) -> str:
    if not value:
        return ""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def verify_origin(request: Request) -> None:
    """別サイトからの POST を防ぐ。Origin があり、ホストが Host と違えば 403。"""
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc != host:
        raise HTTPException(status_code=403, detail="別のサイトからの送信は受け付けません")


def create_app() -> FastAPI:
    app = FastAPI(title="自律研究パイプライン", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))  # 自動エスケープは既定で有効
    env = templates.env
    env.filters.update(safe_url=safe_url, dt=fmt_dt, num=fmt_num, js=fmt_json)
    env.globals.update(
        STATUS_LABELS=STATUS_LABELS, OUTCOME_LABELS=OUTCOME_LABELS, KIND_LABELS=KIND_LABELS,
        FINDING_STATUS_LABELS=FINDING_STATUS_LABELS, LEVEL_LABELS=LEVEL_LABELS,
    )

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def error_page(request: Request, status_code: int, message: str):
        return render(request, "error.html", status_code, message=message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_page(request, exc.status_code, str(exc.detail))

    # ------------------------------------------------------------ 一覧

    @app.get("/healthz")
    def healthz():
        with connect() as conn:
            q.ping(conn)
        return JSONResponse({"ok": True})

    @app.get("/")
    def index(request: Request):
        with connect() as conn:
            return render(
                request, "index.html",
                proposals=q.latest_proposals(conn),
                notifications=q.unread_notifications(conn),
                unread_total=q.count_unread(conn),
                running=q.running_count(conn),
            )

    @app.post("/notifications/read", dependencies=[Depends(verify_origin)])
    def notifications_read():
        with connect() as conn:
            q.mark_all_read(conn)
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ 企画

    def proposal_context(conn, proposal_id: str) -> dict[str, Any]:
        try:
            proposal = q.get_proposal(conn, proposal_id)
        except Exception:  # uuid として不正な ID
            conn.rollback()
            proposal = None
        if proposal is None:
            raise HTTPException(status_code=404, detail="その企画は見つかりません")
        return {
            "proposal": proposal,
            "body": proposal["body"] or {},
            "score": q.latest_score(conn, proposal["paper_id"]),
            "project": q.project_of_proposal(conn, proposal_id),
            "paper_url": q.paper_url(conn, proposal["paper_id"]),
        }

    @app.get("/proposals/{proposal_id}")
    def proposal_detail(request: Request, proposal_id: str):
        with connect() as conn:
            return render(request, "proposal.html", error=None, **proposal_context(conn, proposal_id))

    @app.post("/proposals/{proposal_id}/select", dependencies=[Depends(verify_origin)])
    async def proposal_select(request: Request, proposal_id: str, service: Service):
        try:
            project = await service.select_and_start(proposal_id)
            project_id = project["id"]
        except Exception as exc:
            with connect() as conn:
                ctx = proposal_context(conn, proposal_id)
            message = f"研究を開始できませんでした（{type(exc).__name__}: {exc}）"
            return render(request, "proposal.html", status_code=502, error=message, **ctx)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    # ------------------------------------------------------------ 研究

    @app.get("/projects")
    def projects(request: Request, status: str | None = None):
        if status not in q.PROJECT_STATUSES:
            status = None
        with connect() as conn:
            return render(
                request, "projects.html", projects=q.list_projects(conn, status),
                status_filter=status, statuses=q.PROJECT_STATUSES,
            )

    def project_context(conn, project_id: str) -> dict[str, Any]:
        project = q.get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="その研究は見つかりません")
        cost_total, cost_rows = q.project_costs(conn, project_id)
        hypotheses = q.project_hypotheses(conn, project_id)
        return {
            "project": project,
            "cost_total": cost_total,
            "cost_rows": cost_rows,
            "hyp_pre": [h for h in hypotheses if h["kind"] == "preregistered"],
            "hyp_exp": [h for h in hypotheses if h["kind"] == "exploratory"],
            "preregs": q.project_preregistrations(conn, project_id),
            "experiments": q.project_experiments(conn, project_id),
            "results": q.project_results(conn, project_id),
            "audits": q.project_audits(conn, project_id),
            "gates": q.project_gates(conn, project_id),
            "drafts": q.project_drafts(conn, project_id),
            "artifacts": q.project_artifacts(conn, project_id),
            "events": q.project_events(conn, project_id),
            "refresh": project["status"] == "running",
        }

    @app.get("/projects/{project_id}")
    def project_detail(request: Request, project_id: str):
        with connect() as conn:
            return render(request, "project.html", error=None, **project_context(conn, project_id))

    @app.post("/projects/{project_id}/control", dependencies=[Depends(verify_origin)])
    async def project_control(
        request: Request, project_id: str, service: Service, action: Annotated[str, Form()] = ""
    ):
        if action not in CONTROL_ACTIONS:
            raise HTTPException(status_code=400, detail="action は pause / resume / cancel のいずれかです")
        try:
            await service.control_project(project_id, action)
        except Exception as exc:
            with connect() as conn:
                ctx = project_context(conn, project_id)
            ctx["refresh"] = False
            message = f"操作できませんでした（{type(exc).__name__}: {exc}）"
            return render(request, "project.html", status_code=502, error=message, **ctx)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    # ------------------------------------------------------------ 検索・指標

    @app.get("/search")
    def search(request: Request, service: Service, text: Annotated[str, Query(alias="q")] = ""):
        query = text.strip()
        params = request.query_params
        kinds = [k for k in params.getlist("kind") if k in KIND_LABELS]
        results: list[dict] = []
        error = None
        if query:
            try:
                results = service.search_memory(query, kinds or None, 20)
            except Exception as exc:
                error = f"検索できませんでした（{type(exc).__name__}: {exc}）"
        return render(
            request, "search.html", query=query, kinds=kinds, results=results, error=error,
            searched=bool(query),
        )

    @app.get("/metrics")
    def metrics(request: Request, service: Service):
        try:
            data, error = service.self_metrics(), None
        except Exception as exc:
            data, error = {}, f"指標を取得できませんでした（{type(exc).__name__}: {exc}）"
        return render(request, "metrics.html", metrics=data, error=error)

    return app


app = create_app()
