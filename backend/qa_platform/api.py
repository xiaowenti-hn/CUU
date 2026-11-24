from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from qa_platform.auth import install_auth, respond
from qa_platform.executor import ExecutorSlot
from qa_platform.models import ProjectView, ValidationResult
from qa_platform.runner.commits import apply_manual_commits
from qa_platform.runner.db import propose_sql
from qa_platform.runner.preflight import record
from qa_platform.runner_proc import launch
from qa_platform.store import (
    ApprovalError,
    BodyError,
    RunBusy,
    approve_review,
    case_rows,
    create_project,
    create_run,
    delete_project,
    list_projects,
    load_project,
    pending_case_ids,
    project_has_running,
    read_state,
    resolve_artifact,
    resolve_dist_file,
    recover_runs,
    run_dir,
    run_status,
    timestamp,
    update_project,
    write_json,
    write_project,
)
from qa_platform.validate import check_project, git_snapshot, ocu_available, token_configured

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".zip": "application/zip",
}
BUSY = "已有测试在运行，请等当前运行结束"


def default_var() -> Path:
    return PACKAGE_ROOT / "var"


def default_dist() -> Path:
    return PACKAGE_ROOT / "frontend" / "dist"


def content_type(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "application/octet-stream")


async def read_object(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise BodyError("请求长度无效")
    if not raw.strip():
        return {}
    try:
        data = json_loads(raw)
    except ValueError as exc:
        raise BodyError("请求格式无效") from exc
    if not isinstance(data, dict):
        raise BodyError("请求格式无效")
    return data


def json_loads(raw: bytes):
    import json

    return json.loads(raw)


def view_project(project: dict) -> dict:
    branch, commit, error, _dirty = git_snapshot(project.get("repo_path") or "")
    return ProjectView.model_validate({
        **project,
        "token_configured": token_configured(),
        "current_branch": branch,
        "current_commit": commit,
        "git_error": error,
    }).model_dump()


def view_run(path: Path, executor: ExecutorSlot) -> dict:
    state = read_state(path)
    requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else None
    source = requirement.get("source") if requirement else None
    requirement_view = None
    if requirement:
        requirement_view = {
            "id": requirement.get("id") or "",
            "title": requirement.get("title") or "",
            "source": requirement.get("source") or "",
        }
    events = state.get("events")
    return {
        "id": state.get("id") or path.name,
        "project_id": state.get("project_id") or "",
        "stage": state.get("stage") or "",
        "status": run_status(path, state, executor),
        "stop_reason": state.get("stop_reason") or "",
        "repo_path": state.get("repo_path") or "",
        "branch": state.get("branch") or "",
        "commit": state.get("commit") or "",
        "exit_code": state.get("exit_code"),
        "base_url": state.get("base_url") or "",
        "product_browser": state.get("product_browser") or "",
        "allowed_hosts": state.get("allowed_hosts") or [],
        "allowed_databases": state.get("allowed_databases") or [],
        "events": events if isinstance(events, list) else [],
        "requirement_source": source,
        "requirement": requirement_view,
        "review": state.get("review"),
        "approvals": state.get("approvals") or [],
        "cases": [
            {
                "case_id": case.get("case_id") or case.get("id"),
                "title": case.get("title") or "",
                "status": case.get("status") or "waiting",
                "seen": case.get("seen") or "",
                "pending": case.get("pending"),
                "observed": bool(case.get("observed")),
                "blocked_reason": case.get("blocked_reason") or "",
            }
            for case in case_rows(path)
        ],
        "current_case": state.get("current_case") or "",
        "current_seen": state.get("current_seen") or "",
        "round_prompt": state.get("round_prompt") if isinstance(state.get("round_prompt"), dict) else None,
    }


def read_index(dist: Path, token: str) -> str:
    file = dist / "index.html"
    if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(dist.resolve()):
        raise LookupError("前端还没有构建")
    html = file.read_text(encoding="utf-8")
    if "@TOKEN@" not in html:
        raise LookupError("页面缺少令牌占位符")
    return html.replace("@TOKEN@", token)


def create_app(var_dir: Path | None = None, dist_dir: Path | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.var_dir = (var_dir or default_var()).resolve()
    app.state.dist_dir = (dist_dir or default_dist()).resolve()
    import secrets

    app.state.token = secrets.token_urlsafe(32)
    app.state.executor = ExecutorSlot()
    recover_runs(app.state.var_dir, app.state.executor)
    install_auth(app)

    @app.exception_handler(FileNotFoundError)
    async def missing(_request: Request, exc: FileNotFoundError):
        return respond(404, {"error": str(exc) or "找不到资源"})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            payload = exc.detail
        else:
            payload = {"error": str(exc.detail)}
        return respond(exc.status_code, payload)

    @app.get("/api/health")
    def health(request: Request):
        return {
            "ok": True,
            "busy": request.app.state.executor.busy(),
            "ocu_available": ocu_available(),
        }

    @app.get("/api/projects")
    def get_projects(request: Request):
        projects = [view_project(item) for item in list_projects(request.app.state.var_dir)]
        return {"projects": projects}

    @app.post("/api/projects", status_code=201)
    async def post_project(request: Request):
        try:
            body = await read_object(request)
            project = create_project(request.app.state.var_dir, body)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        return view_project(project)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, request: Request):
        return view_project(load_project(request.app.state.var_dir, project_id))

    @app.put("/api/projects/{project_id}")
    async def put_project(project_id: str, request: Request):
        try:
            body = await read_object(request)
            project = update_project(request.app.state.var_dir, project_id, body)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        return view_project(project)

    @app.delete("/api/projects/{project_id}", status_code=204)
    def remove_project(project_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        with executor.lock:
            if project_has_running(var_dir, project_id, executor):
                return respond(409, {"error": BUSY})
            delete_project(var_dir, project_id)
        return Response(status_code=204)

    @app.post("/api/projects/{project_id}/validate")
    def validate_project(project_id: str, request: Request):
        project = load_project(request.app.state.var_dir, project_id)
        return ValidationResult.model_validate(check_project(project)).model_dump()

    @app.post("/api/projects/{project_id}/runs")
    def start_run(project_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        with executor.lock:
            project = load_project(var_dir, project_id)
            result = ValidationResult.model_validate(check_project(project)).model_dump()
            if not result["ok"]:
                result["error"] = "配置不齐，未创建运行"
                return respond(409, result)
            if executor.busy():
                return respond(409, {"error": BUSY})
            try:
                run_id = create_run(var_dir, project)
            except RunBusy:
                return respond(409, {"error": BUSY})
            run_path = var_dir / "runs" / run_id
            try:
                launch(executor, run_path, project["id"])
            except OSError:
                record(run_path, exit_code=2, event_status="阻塞", basis="执行器没有启动")
        return respond(202, {"run": run_id})

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request):
        path = run_dir(request.app.state.var_dir, run_id)
        return view_run(path, request.app.state.executor)

    @app.post("/api/runs/{run_id}/browser")
    async def set_run_browser(run_id: str, request: Request):
        try:
            body = await read_object(request)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        browser = body.get("browser")
        if browser not in {"edge", "chrome"}:
            return respond(400, {"error": "浏览器只能是 Edge 或 Chrome"})
        path = run_dir(request.app.state.var_dir, run_id)
        state = read_state(path)
        state["product_browser"] = browser
        write_json(path / "state.json", state)
        project_id = str(state.get("project_id") or "")
        if project_id:
            try:
                current = load_project(request.app.state.var_dir, project_id)
                current["product_browser"] = browser
                current["updated_at"] = timestamp()
                write_project(request.app.state.var_dir, current)
            except FileNotFoundError:
                pass
        return {"browser": browser}

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str, request: Request):
        import os
        import signal
        import subprocess

        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        path = run_dir(var_dir, run_id)
        with executor.lock:
            if not executor.busy() or executor.run_id != run_id:
                return respond(409, {"error": "这次没有在执行"})
            process = executor.process
            marker = path / "stop.requested"
            marker.write_text("1", encoding="utf-8")
            if process is not None and getattr(process, "pid", None):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        state = read_state(path)
        events = state.get("events") if isinstance(state.get("events"), list) else []
        last = events[-1] if events and isinstance(events[-1], dict) else {}
        if last.get("basis") != "已手动停止":
            step = str(last.get("step") or "执行")
            record(path, exit_code=2, event_status="停止", basis="已手动停止", step=step)
        state = read_state(path)
        state["stop_reason"] = "manual"
        state.pop("round_prompt", None)
        write_json(path / "state.json", state)
        return {"run": run_id, "status": "stopped"}

    @app.post("/api/runs/{run_id}/continue")
    def continue_rounds(run_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        path = run_dir(var_dir, run_id)
        with executor.lock:
            if not executor.busy() or executor.run_id != run_id:
                return respond(409, {"error": "这次没有在等是否继续"})
            state = read_state(path)
            if not isinstance(state.get("round_prompt"), dict):
                return respond(409, {"error": "这次没有在等是否继续"})
            marker = path / "continue.requested"
            marker.write_text("1", encoding="utf-8")
        return {"run": run_id}

    @app.post("/api/runs/{run_id}/retry")
    async def retry_run(run_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        try:
            body = await read_object(request)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        requested = body.get("step")
        if requested is not None and (not isinstance(requested, str) or not requested.strip()):
            return respond(400, {"error": "没有这一步"})
        browser = body.get("browser")
        if browser is not None and browser not in {"edge", "chrome"}:
            return respond(400, {"error": "浏览器只能是 Edge 或 Chrome"})
        with executor.lock:
            if executor.busy():
                return respond(409, {"error": BUSY})
            path = run_dir(var_dir, run_id)
            from qa_platform.runner.retry import prepare_retry

            try:
                step = prepare_retry(
                    path,
                    requested.strip() if isinstance(requested, str) else None,
                    browser if isinstance(browser, str) else None,
                )
            except ApprovalError as exc:
                return respond(exc.status, {"error": str(exc)})
            state = read_state(path)
            try:
                launch(executor, path, str(state.get("project_id") or ""))
            except OSError:
                record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step=step)
        return respond(202, {"run": run_id, "step": step})

    @app.post("/api/runs/{run_id}/manual-commits")
    async def post_manual_commits(run_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        try:
            body = await read_object(request)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        shas = body.get("shas")
        if not isinstance(shas, list) or not shas or any(not isinstance(item, str) or not item for item in shas):
            return respond(400, {"error": "请给出至少一个提交"})
        with executor.lock:
            if executor.busy():
                return respond(409, {"error": BUSY})
            path = run_dir(var_dir, run_id)
            status, payload = apply_manual_commits(path, shas)
            if status != 200:
                return respond(status, payload)
            state = read_state(path)
            try:
                launch(executor, path, str(state.get("project_id") or ""))
            except OSError:
                record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="方案")
        return respond(202, {"run": run_id})

    @app.post("/api/runs/{run_id}/approvals")
    async def post_approval(run_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        try:
            body = await read_object(request)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        with executor.lock:
            if executor.busy():
                return respond(409, {"error": BUSY})
            path = run_dir(var_dir, run_id)
            try:
                saved = approve_review(path, body)
            except RunBusy:
                return respond(409, {"error": BUSY})
            except ApprovalError as exc:
                return respond(exc.status, {"error": str(exc)})
            approvals = saved.get("approvals") if isinstance(saved, dict) else None
            kind = ""
            if isinstance(approvals, list) and approvals and isinstance(approvals[-1], dict):
                kind = str(approvals[-1].get("kind") or "")
            if kind == "baseline":
                from qa_platform.runner.db import _load_baseline
                if (_load_baseline(path) or {}).get("strategy") in {"reuse", "page_seed"}:
                    try:
                        launch(executor, path, str(saved.get("project_id") or ""))
                    except OSError:
                        record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="执行")
            elif kind == "baseline_actual":
                from qa_platform.runner.db import _load_baseline
                current = _load_baseline(path) or {}
                if current.get("strategy") == "sql" and current.get("apply_exit_code") == 0:
                    try:
                        launch(executor, path, str(saved.get("project_id") or ""))
                    except OSError:
                        record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="执行")
            elif kind == "sql":
                try:
                    launch(executor, path, str(saved.get("project_id") or ""), apply_now=True)
                except OSError:
                    record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="改数")
            elif kind in {"plan", "fixture_keys"}:
                try:
                    launch(executor, path, str(saved.get("project_id") or ""))
                except OSError:
                    record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="基线")
        return view_run(path, executor)

    @app.post("/api/runs/{run_id}/sql-proposal")
    async def post_sql_proposal(run_id: str, request: Request):
        var_dir = request.app.state.var_dir
        executor = request.app.state.executor
        try:
            body = await read_object(request)
        except BodyError as exc:
            return respond(400, {"error": str(exc)})
        with executor.lock:
            if executor.busy():
                return respond(409, {"error": BUSY})
            path = run_dir(var_dir, run_id)
            try:
                saved = propose_sql(path, body)
            except ApprovalError as exc:
                return respond(exc.status, {"error": str(exc)})
            try:
                launch(executor, path, str(saved.get("project_id") or ""))
            except OSError:
                record(path, exit_code=2, event_status="阻塞", basis="执行器没有启动", step="改数")
        return view_run(path, executor)

    @app.post("/api/runs/{run_id}/cases/{case_id}/rerun")
    async def post_rerun(run_id: str, case_id: str, request: Request):
        executor = request.app.state.executor
        path = run_dir(request.app.state.var_dir, run_id)
        from qa_platform.runner.rerun import accept_rerun

        with executor.lock:
            status, body = accept_rerun(path, case_id, executor, run_id=run_id)
        return respond(status, body)

    @app.get("/api/runs/{run_id}/report")
    def get_report(run_id: str, request: Request):
        path = run_dir(request.app.state.var_dir, run_id)
        report = path / "report.md"
        if report.is_file():
            resolved = report.resolve()
            if resolved.is_relative_to(path.resolve()):
                return Response(report.read_bytes(), media_type="text/markdown; charset=utf-8")
            raise FileNotFoundError("找不到资源")
        return respond(409, {"error": "报告尚未生成", "cases": pending_case_ids(path)})

    @app.get("/api/runs/{run_id}/artifacts/{relative:path}")
    def get_artifact(run_id: str, relative: str, request: Request):
        path = run_dir(request.app.state.var_dir, run_id)
        file = resolve_artifact(path, relative)
        return Response(file.read_bytes(), media_type=content_type(file))

    def html_page(request: Request):
        try:
            html = read_index(request.app.state.dist_dir, request.app.state.token)
        except LookupError as exc:
            return respond(500, {"error": str(exc)})
        return Response(html.encode("utf-8"), media_type="text/html; charset=utf-8")

    @app.get("/")
    def index(request: Request):
        return html_page(request)

    @app.get("/index.html")
    def index_html(request: Request):
        return html_page(request)

    @app.get("/assets/{file_path:path}")
    def assets(file_path: str, request: Request):
        file = resolve_dist_file(request.app.state.dist_dir, f"assets/{file_path}")
        return Response(file.read_bytes(), media_type=content_type(file))

    @app.get("/{full_path:path}")
    def static_file(full_path: str, request: Request):
        try:
            file = resolve_dist_file(request.app.state.dist_dir, full_path)
        except FileNotFoundError:
            return respond(404, {"error": "找不到资源"})
        return Response(file.read_bytes(), media_type=content_type(file))

    return app


def main() -> None:
    import uvicorn

    print("验收平台 · http://127.0.0.1:8876/", flush=True)
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=8876,
        workers=1,
        proxy_headers=False,
        forwarded_allow_ips="",
    )
