import copy
import fcntl
import hashlib
import json
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from qa_platform.models import ProjectRecord

PROJECT_ID = re.compile(r"prj-[a-f0-9]{12}\Z")
RUN_ID = re.compile(r"run-[a-f0-9]{12}\Z")
SECRET_KEYS = {"token", "password", "api_key"}
EDITABLE = (
    "name",
    "repo_path",
    "branch",
    "git_remote",
    "yunxiao_entry",
    "yunxiao_organization_id",
    "yunxiao_edition",
    "yunxiao_browser_app",
    "product_browser",
    "base_url",
    "allowed_hosts",
    "allowed_databases",
)
LIST_FIELDS = {"allowed_hosts", "allowed_databases"}
ARTIFACTS = {
    "plan.json",
    "state.json",
    "requirement.txt",
    "baseline.json",
    "report.md",
    "console.log",
    "report.zip",
}
IMAGE = re.compile(r"images/[^/]+\.(?:png|jpg)\Z")
OPEN_CASE = {"waiting", "running", "paused"}
DEFAULT_DATABASE = "oms3_onlinepublish_biz"


class BodyError(ValueError):
    pass


class RunBusy(Exception):
    pass


class ApprovalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


BIND_ONLY_KINDS = {"commits_empty", "baseline", "sql", "baseline_actual"}
PLAN_KIND = "plan"


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def reject_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SECRET_KEYS:
                raise BodyError("不能把令牌或口令写入配置")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


def normalize_repo(value: str) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    try:
        if path.is_dir():
            return str(path.resolve())
    except OSError:
        return value
    return value


def coerce_field(key: str, value: object) -> object:
    if key in LIST_FIELDS:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise BodyError(f"{key} 必须是字符串列表")
        return [item.strip() for item in value if item.strip()]
    if not isinstance(value, str):
        raise BodyError(f"{key} 必须是字符串")
    text = value.strip()
    if key == "repo_path":
        return normalize_repo(text)
    if key == "git_remote":
        return text or "origin"
    if key == "yunxiao_edition":
        return text or "center"
    if key == "product_browser":
        if text not in {"", "edge", "chrome"}:
            raise BodyError("浏览器只能是 Edge 或 Chrome")
        return text
    return text


def blank_project(project_id: str, created_at: str, *, with_default_database: bool) -> dict:
    return {
        "id": project_id,
        "name": "",
        "repo_path": "",
        "branch": "",
        "git_remote": "origin",
        "yunxiao_entry": "",
        "yunxiao_organization_id": "",
        "yunxiao_edition": "center",
        "yunxiao_browser_app": "",
        "product_browser": "",
        "base_url": "",
        "allowed_hosts": [],
        "allowed_databases": [DEFAULT_DATABASE] if with_default_database else [],
        "created_at": created_at,
        "updated_at": created_at,
    }


def apply_body(base: dict, body: dict, *, replace: bool) -> dict:
    reject_secrets(body)
    if replace:
        project = blank_project(base["id"], base["created_at"], with_default_database=False)
    else:
        project = blank_project(base["id"], base["created_at"], with_default_database=False)
        project.update({
            key: list(base[key]) if key in LIST_FIELDS else base[key]
            for key in (*EDITABLE, "created_at")
        })
        project["id"] = base["id"]
    project["updated_at"] = timestamp()
    for key in EDITABLE:
        if key in body:
            project[key] = coerce_field(key, body[key])
    return ProjectRecord.model_validate(project).model_dump()


def read_record(path: Path, project_id: str) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("找不到项目") from exc
    if not isinstance(raw, dict):
        raise FileNotFoundError("找不到项目")
    known = {key: raw[key] for key in ProjectRecord.model_fields if key in raw}
    known["id"] = project_id
    try:
        record = ProjectRecord.model_validate(known).model_dump()
    except ValueError as exc:
        raise FileNotFoundError("找不到项目") from exc
    if raw.get("id") not in (None, project_id):
        raise FileNotFoundError("找不到项目")
    return record


def project_file(var_dir: Path, project_id: str) -> Path:
    if not PROJECT_ID.fullmatch(project_id):
        raise FileNotFoundError("找不到项目")
    folder = var_dir / "projects" / project_id
    path = folder / "project.json"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("找不到项目")
    return path


def load_project(var_dir: Path, project_id: str) -> dict:
    return read_record(project_file(var_dir, project_id), project_id)


def write_project(var_dir: Path, project: dict) -> None:
    record = ProjectRecord.model_validate(project).model_dump()
    write_json(var_dir / "projects" / record["id"] / "project.json", record)


def list_projects(var_dir: Path) -> list[dict]:
    root = var_dir / "projects"
    if not root.is_dir() or root.is_symlink():
        return []
    found = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not PROJECT_ID.fullmatch(folder.name):
            continue
        path = folder / "project.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            found.append(read_record(path, folder.name))
        except FileNotFoundError:
            continue
    found.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return found


def mint_project_id(var_dir: Path) -> str:
    root = var_dir / "projects"
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        project_id = "prj-" + secrets.token_hex(6)
        if not (root / project_id).exists():
            return project_id
    raise RuntimeError("无法分配项目 id")


def create_project(var_dir: Path, body: dict) -> dict:
    created_at = timestamp()
    project_id = mint_project_id(var_dir)
    base = blank_project(project_id, created_at, with_default_database=True)
    project = apply_body(base, body, replace=False)
    project["created_at"] = created_at
    write_project(var_dir, project)
    return project


def update_project(var_dir: Path, project_id: str, body: dict) -> dict:
    current = load_project(var_dir, project_id)
    project = apply_body(current, body, replace=True)
    write_project(var_dir, project)
    return project


def delete_project(var_dir: Path, project_id: str) -> None:
    path = project_file(var_dir, project_id)
    path.unlink()


def run_dir(var_dir: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise FileNotFoundError("找不到运行记录")
    path = var_dir / "runs" / run_id
    if path.is_symlink() or not path.is_dir():
        raise FileNotFoundError("找不到运行记录")
    return path


def read_state(path: Path) -> dict:
    file = path / "state.json"
    if file.is_symlink() or not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def case_rows(path: Path) -> list[dict]:
    root = path / "cases"
    if root.is_symlink() or not root.is_dir():
        return []
    rows = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        file = child / "case.json"
        if file.is_symlink() or not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def recover_runs(var_dir: Path, executor) -> None:
    """启动时只读套用 run_status。不把仍有 pending 的运行改写成 interrupted。"""
    root = var_dir / "runs"
    if not root.is_dir() or root.is_symlink():
        return
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        run_status(path, read_state(path), executor)


def run_status(path: Path, state: dict, executor) -> str:
    run_id = state.get("id") or path.name
    if executor.busy() and executor.run_id == run_id:
        return "running"
    if state.get("stop_reason") == "manual":
        return "stopped"
    if state.get("review"):
        return "review"
    for case in case_rows(path):
        if case.get("status") == "paused" or case.get("pending") is not None:
            return "paused"
    if state.get("exit_code") == 2 or state.get("stop_reason"):
        return "blocked"
    if state.get("stage") == "report" and (path / "report.md").is_file() and not (path / "report.md").is_symlink():
        return "complete"
    return "interrupted"


def project_has_running(var_dir: Path, project_id: str, executor) -> bool:
    if executor.busy() and executor.project_id == project_id:
        return True
    root = var_dir / "runs"
    if not root.is_dir() or root.is_symlink():
        return False
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        state = read_state(path)
        if state.get("project_id") != project_id:
            continue
        if run_status(path, state, executor) == "running":
            return True
    return False


def pending_case_ids(path: Path) -> list[str]:
    found = []
    for case in case_rows(path):
        if case.get("status") in OPEN_CASE:
            found.append(str(case.get("case_id") or ""))
    return found


def create_run(var_dir: Path, project: dict) -> str:
    root = var_dir / "runs"
    root.mkdir(parents=True, exist_ok=True)
    path = None
    run_id = ""
    for _ in range(5):
        run_id = "run-" + secrets.token_hex(6)
        folder = root / run_id
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        path = folder
        break
    if path is None:
        raise RuntimeError("无法分配运行 id")
    state = {
        "id": run_id,
        "project_id": project["id"],
        "stage": "preflight",
        "stop_reason": "",
        "repo_path": project["repo_path"],
        "branch": project["branch"],
        "git_remote": project.get("git_remote") or "origin",
        "commit": "",
        "base_url": project["base_url"],
        "allowed_hosts": list(project["allowed_hosts"]),
        "allowed_databases": list(project["allowed_databases"]),
        "yunxiao_entry": project.get("yunxiao_entry") or "",
        "yunxiao_organization_id": project.get("yunxiao_organization_id") or "",
        "yunxiao_edition": project.get("yunxiao_edition") or "center",
        "yunxiao_browser_app": project.get("yunxiao_browser_app") or "",
        "product_browser": project.get("product_browser") or "",
        "requirement": None,
        "environment_host": "",
        "review": None,
        "approvals": [],
        "commit_search": None,
        "manual_commits": [],
        "events": [],
        "ocu_version": "",
        "exit_code": None,
    }
    lock_path = path / ".run.lock"
    try:
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunBusy() from exc
            try:
                write_json(path / "state.json", state)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return run_id


def resolve_inside(root: Path, relative: str) -> Path:
    if not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        raise FileNotFoundError("找不到资源")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FileNotFoundError("找不到资源")
    file = root / relative
    if not file.is_file():
        raise FileNotFoundError("找不到资源")
    if not file.resolve().is_relative_to(root.resolve()):
        raise FileNotFoundError("找不到资源")
    return file


def resolve_artifact(run: Path, relative: str) -> Path:
    allowed = relative in ARTIFACTS or IMAGE.fullmatch(relative) is not None or relative.startswith("cases/")
    if not allowed:
        raise FileNotFoundError("找不到资源")
    return resolve_inside(run, relative)


def resolve_dist_file(dist: Path, relative: str) -> Path:
    if Path(relative).suffix.lower() == ".html":
        raise FileNotFoundError("找不到资源")
    return resolve_inside(dist, relative)


def run_names(var_dir: Path) -> list[str]:
    root = var_dir / "runs"
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink())


def review_digest(kind, payload) -> str:
    encoded = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def find_approval(state: dict, kind: str, payload):
    approvals = state.get("approvals")
    if not isinstance(approvals, list):
        return None
    relevant = [item for item in approvals if isinstance(item, dict) and item.get("kind") == kind]
    if relevant and relevant[-1].get("digest") == review_digest(kind, payload):
        return relevant[-1]
    return None


def _whole_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _decision_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _open_key(item: dict, index: int) -> str:
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        return item_id
    return f"open-{index}"


def _must_decide(item: dict) -> bool:
    if item.get("needs_decision") is True:
        return True
    question = item.get("question")
    return isinstance(question, str) and bool(question.strip())


def _apply_plan_fields(document: dict, decisions: dict, seed) -> None:
    opens = document.get("open")
    if isinstance(opens, list):
        for index, item in enumerate(opens):
            if not isinstance(item, dict):
                continue
            key = _open_key(item, index)
            if key in decisions:
                item["decision"] = decisions[key]
    if seed is None:
        return
    ui = document.get("ui")
    if isinstance(ui, dict):
        ui["seed_object_count"] = seed
    else:
        document["seed_object_count"] = seed


def _load_plan(run_dir: Path) -> dict | None:
    file = run_dir / "plan.json"
    if file.is_symlink() or not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError(400, "方案内容无效") from exc
    if not isinstance(data, dict):
        raise ApprovalError(400, "方案内容无效")
    return data


def _merge_plan(run_dir: Path, review: dict, body: dict) -> dict:
    if "seed_object_count" in body and not _whole_count(body.get("seed_object_count")):
        raise ApprovalError(400, "seed_object_count 必须是大于等于 0 的整数")
    decisions = body.get("open_decisions", {})
    if decisions is None:
        decisions = {}
    if not isinstance(decisions, dict):
        raise ApprovalError(400, "还有待决定没有填写")
    source = _load_plan(run_dir)
    if source is None:
        payload = review.get("payload")
        if not isinstance(payload, dict):
            raise ApprovalError(400, "方案内容无效")
        source = copy.deepcopy(payload)
    opens = source.get("open") if isinstance(source.get("open"), list) else []
    accepted: dict[str, str] = {}
    for index, item in enumerate(opens):
        if not isinstance(item, dict) or not _must_decide(item):
            continue
        key = _open_key(item, index)
        text = _decision_text(decisions.get(key))
        if text is None:
            raise ApprovalError(400, "还有待决定没有填写")
        accepted[key] = text
    for key, value in decisions.items():
        if key in accepted:
            continue
        text = _decision_text(value)
        if text is not None:
            accepted[str(key)] = text
    seed = body.get("seed_object_count") if "seed_object_count" in body else None
    _apply_plan_fields(source, accepted, seed)
    return source


def _write_progress(run_dir: Path, events: list) -> None:
    lines = ["# 执行进度", ""]
    for item in events:
        clock = str(item.get("time", ""))[11:19]
        lines.append(
            f"- `{clock}` **{item.get('status')} · {item.get('step')}**。{item.get('basis')}"
        )
    progress = run_dir / "执行进度.md"
    temporary = progress.with_name(progress.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(progress)


def approve_review(run_dir: Path, body: dict) -> dict:
    """在运行目录锁里批准当前评审。不启动 runner。"""
    handle = (run_dir / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RunBusy() from exc
    try:
        state = read_state(run_dir)
        review = state.get("review")
        review_id = body.get("review_id")
        if not isinstance(review, dict) or review.get("id") != review_id:
            raise ApprovalError(409, "确认已失效")
        kind = review.get("kind")
        if kind == PLAN_KIND:
            document = _merge_plan(run_dir, review, body)
            digest = review_digest(PLAN_KIND, document)
            approved = {
                **review,
                "id": review_id,
                "payload": document,
                "digest": digest,
            }
            write_json(run_dir / "plan.json", document)
        elif kind == "fixture_keys":
            from qa_platform.runner.baseline import merged_fixture_payload

            allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
            document = merged_fixture_payload(review, body, allowed)
            digest = review_digest(kind, document)
            approved = {
                **review,
                "id": review_id,
                "payload": document,
                "digest": digest,
            }
        elif kind in BIND_ONLY_KINDS:
            extra = set(body) - {"review_id"}
            if extra:
                raise ApprovalError(400, "这个确认只接受 review_id")
            payload = review.get("payload")
            digest = review_digest(kind, payload)
            if review.get("digest") != digest:
                raise ApprovalError(409, "确认已失效")
            approved = review
            if kind == "sql":
                from qa_platform.runner.db import arm_apply
                arm_apply(run_dir, payload if isinstance(payload, dict) else {})
        else:
            raise ApprovalError(400, "这个确认不能在审批接口上完成")
        item = {**approved, "approved_at": timestamp()}
        approvals = state.get("approvals")
        if not isinstance(approvals, list):
            approvals = []
        approvals.append(item)
        state["approvals"] = approvals
        state["review"] = None
        events = state.get("events")
        if not isinstance(events, list):
            events = []
        title = str(item.get("title") or item.get("kind") or "")
        short = str(item.get("id") or "")[:8]
        events.append({
            "time": timestamp(),
            "status": "已确认",
            "step": item.get("step") or "方案",
            "basis": f"已人工确认「{title}」，审批版本 {short}。",
        })
        state["events"] = events
        write_json(run_dir / "state.json", state)
        _write_progress(run_dir, events)
        return state
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
