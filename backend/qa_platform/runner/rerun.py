"""单用例重跑。不改方案，不改五元头，不调用 planner。"""

import hashlib
import json
import re
import shutil
from pathlib import Path

from qa_platform.runner.execute import NO_OPEN_PAGE, URL_RE, _case_prompt, navigation_allowed, observe, show_test_page
from qa_platform.runner.ocu import assert_product_js
from qa_platform.runner.preflight import record
from qa_platform.runner.report import replace_report_section, seal_case
from qa_platform.store import read_state, review_digest, timestamp, write_json

CASE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
BUSY = "已有测试在运行，请等当前运行结束"
CHANGED = "已确认的行已变化"
FROZEN = ("requirement", "environment_host", "branch", "commit", "base_url")
REPLAY = {"passed", "failed", "blocked", "waiting"}


def launch_browser(binary, url, console_log) -> None:
    from qa_platform.runner.execute import _launch

    _launch(binary, url, console_log)


def open_session():
    from qa_platform.runner.ocu import ProductSession

    return ProductSession()


def open_model():
    import os

    from qa_platform.runner.execute import deepseek_model
    from qa_platform.runner_proc import local_secrets, one_line

    key = one_line(os.environ.get("DEEPSEEK_API_KEY", "") or local_secrets().get("DEEPSEEK_API_KEY", ""))
    return deepseek_model(key)


def run_query(run_dir: Path, database: str, sql: str, allowed: list[str]):
    from qa_platform.runner.db import read_only_query

    return read_only_query(database, sql, allowed=allowed, run_dir=run_dir)


def _read_json(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _plan(run_dir: Path) -> dict:
    return _read_json(run_dir / "plan.json") or {}


def _baseline(run_dir: Path) -> dict:
    return _read_json(run_dir / "baseline.json") or {}


def _case_file(run_dir: Path, case_id: str) -> Path | None:
    if not CASE_ID.fullmatch(case_id):
        return None
    folder = run_dir / "cases" / case_id
    file = folder / "case.json"
    if folder.is_symlink() or file.is_symlink() or not file.is_file():
        return None
    return file


def _attempt_number(case: dict) -> int:
    current = case.get("attempt")
    if isinstance(current, int) and not isinstance(current, bool) and current >= 1:
        return current
    return 1


def _digest(rows: list) -> str:
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _approved_rows(state: dict, check_id: str) -> list | None:
    approvals = state.get("approvals")
    if not isinstance(approvals, list):
        return None
    for item in reversed(approvals):
        if not isinstance(item, dict) or item.get("kind") not in {"baseline", "baseline_actual"}:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or item.get("digest") != review_digest(item.get("kind"), payload):
            continue
        actual = payload.get("actual")
        if isinstance(actual, dict):
            nested = actual.get("by_check")
            if isinstance(nested, dict) and isinstance(nested.get(check_id), list):
                return nested[check_id]
            if isinstance(actual.get(check_id), list):
                return actual[check_id]
        records = payload.get("records")
        if isinstance(records, dict) and isinstance(records.get(check_id), list):
            return records[check_id]
    return None


def _state_field(plan: dict, check_id: str) -> str:
    for check in plan.get("checks") or []:
        if isinstance(check, dict) and check.get("id") == check_id:
            return str(check.get("state_field") or "")
    return ""


def _field_value(rows: list, field: str) -> str:
    if not rows or not isinstance(rows[0], dict) or not field:
        return ""
    value = rows[0].get(field)
    return "" if value is None else str(value)


def _same_snapshot(confirmed: list, current: list, fixture: dict, field: str) -> bool:
    if _digest(confirmed) != _digest(current):
        return False
    if not confirmed and not current:
        return True
    pk = fixture.get("pk") if isinstance(fixture, dict) else {}
    if isinstance(pk, dict):
        for column, value in pk.items():
            if not any(isinstance(row, dict) and row.get(column) == value for row in current):
                return False
            if not any(isinstance(row, dict) and row.get(column) == value for row in confirmed):
                return False
    if field and _field_value(confirmed, field) != _field_value(current, field):
        return False
    return True


def _test_steps(plan: dict, case: dict) -> list[dict]:
    case_id = str(case.get("case_id") or case.get("id") or "")
    for item in plan.get("cases") or []:
        if isinstance(item, dict) and item.get("id") == case_id:
            return [
                step for step in item.get("steps") or []
                if isinstance(step, dict) and step.get("phase") == "test"
            ]
    steps = case.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _later_waiting(run_dir: Path, plan: dict, case_id: str) -> None:
    ids = [item.get("id") for item in plan.get("cases") or [] if isinstance(item, dict)]
    if case_id not in ids:
        return
    for later_id in ids[ids.index(case_id) + 1:]:
        if not isinstance(later_id, str):
            continue
        file = _case_file(run_dir, later_id)
        if file is None:
            continue
        data = _read_json(file)
        if not isinstance(data, dict) or data.get("status") != "running":
            continue
        data["status"] = "waiting"
        write_json(file, data)


def _archive(run_dir: Path, case_id: str, number: int) -> None:
    root = run_dir / "cases" / case_id
    dest = root / "attempts" / str(number)
    if dest.exists():
        return
    images = dest / "images"
    images.mkdir(parents=True)
    listed = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == "attempt.json":
            continue
        if path.suffix.lower() == ".json":
            shutil.copy2(path, dest / path.name)
            continue
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        shutil.copy2(path, images / path.name)
        listed.append({
            "id": path.stem,
            "kind": "window",
            "file": f"cases/{case_id}/attempts/{number}/images/{path.name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    write_json(dest / "evidence.json", {"attempt": number, "evidence": listed})


def _pointer(run_dir: Path, case_id: str, number: int) -> None:
    write_json(run_dir / "cases" / case_id / "attempt.json", {"attempt": number})


def _keep_frozen(state: dict, frozen: dict) -> None:
    for key, value in frozen.items():
        state[key] = value


def _finish_state(run_dir: Path, *, exit_code: int, stage: str | None = None) -> None:
    state = read_state(run_dir)
    frozen = {key: state.get(key) for key in FROZEN}
    state["exit_code"] = exit_code
    if stage is not None:
        state["stage"] = stage
    _keep_frozen(state, frozen)
    write_json(run_dir / "state.json", state)


def _select(run_dir: Path, case: dict, plan: dict, query) -> tuple[list, str, dict]:
    baseline = _baseline(run_dir)
    check_id = case.get("check_id")
    statements = baseline.get("select_sql") if isinstance(baseline.get("select_sql"), dict) else {}
    fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
    fixture = fixtures.get(check_id) if isinstance(fixtures.get(check_id), dict) else {}
    sql = statements.get(check_id)
    database = fixture.get("database")
    allowed = [item for item in read_state(run_dir).get("allowed_databases") or [] if isinstance(item, str)]
    if not isinstance(sql, str) or not isinstance(database, str) or database not in allowed or query is None:
        return [], _state_field(plan, str(check_id or "")), fixture
    rows = query(database, sql, allowed)
    if not isinstance(rows, list):
        rows = []
    return rows, _state_field(plan, str(check_id or "")), fixture


def _write_image(run_dir: Path, case_id: str, attempt: int, index: int, image: bytes) -> dict | None:
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    relative = f"cases/{case_id}/attempts/{attempt}/images/round-{index}.png"
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return {
        "id": f"round-{index}",
        "step_index": index - 1,
        "kind": "window",
        "file": relative,
        "sha256": hashlib.sha256(image).hexdigest(),
        "time": timestamp(),
    }


def _open_browser(browser, run_dir: Path, plan: dict, base_url: str, session) -> tuple[str | None, str]:
    state = read_state(run_dir)
    return show_test_page(run_dir, plan, state, session, None, browser, base_url)


def _play(run_dir, state, case, steps, plan, session, model, browser, base_url: str, attempt: int) -> int:
    if not steps:
        case["status"] = "running"
        case["observed"] = True
        case["pending"] = None
        case["sealed"] = False
        return 0
    app, browser_error = _open_browser(browser, run_dir, plan, base_url, session)
    if not app:
        case["status"] = "blocked"
        case["blocked_reason"] = browser_error or NO_OPEN_PAGE
        case["result_reason"] = browser_error or NO_OPEN_PAGE
        return 2
    session.reset()
    view = {"id": case.get("id"), "title": case.get("title"), "steps": steps}
    rounds = []
    evidence = []
    for _ in range(8):
        decision = model(_case_prompt(view, plan, base_url, "test", app=app))
        if not isinstance(decision, dict) or decision.get("done"):
            break
        code = str(decision.get("js") or "")
        try:
            assert_product_js(code, app)
        except Exception:
            case["status"] = "blocked"
            case["blocked_reason"] = "控件环境错误"
            case["result_reason"] = "控件环境错误"
            return 2
        if any(found and not navigation_allowed(found, base_url) for found in URL_RE.findall(code)):
            case["status"] = "blocked"
            case["blocked_reason"] = "环境不在 base_url"
            case["result_reason"] = "环境不在 base_url"
            return 2
        if decision.get("effect") == "commit":
            case["status"] = "blocked"
            case["blocked_reason"] = "数据库只能查询"
            case["result_reason"] = "数据库只能查询，未提交"
            return 2
        committed = False
        result = observe(session.run(code, app))
        seen = str(result.get("text") or "")
        shot = _write_image(run_dir, str(case.get("case_id") or case.get("id")), attempt, len(rounds) + 1, result.get("screenshot") or b"")
        rounds.append({"time": timestamp(), "summary": code[:180], "seen_text": seen, "screenshot": shot})
        if shot is not None:
            evidence.append({**shot, "seen_text": seen})
        current = str(result.get("url") or base_url)
        if not navigation_allowed(current, base_url):
            case["status"] = "blocked"
            case["blocked_reason"] = "环境不在 base_url"
            case["result_reason"] = "环境不在 base_url"
            case["rounds"] = rounds
            case["evidence"] = evidence
            return 2
        if committed:
            case["rounds"] = rounds
            case["evidence"] = evidence
            case["seen"] = seen
            case["observed"] = True
            case["status"] = "paused"
            return 3
    case["rounds"] = rounds
    case["evidence"] = evidence
    if rounds:
        case["seen"] = rounds[-1]["seen_text"]
    case["observed"] = True
    case["pending"] = None
    case["sealed"] = False
    case["status"] = "running"
    return 0


def _block_changed(case: dict, rows: list, field: str) -> None:
    case["status"] = "blocked"
    case["sealed"] = True
    case["blocked_reason"] = CHANGED
    case["result_reason"] = CHANGED
    case["current_rows"] = rows
    case["state_after"] = _field_value(rows, field)
    case["pending"] = None


def _reobserve(case: dict, rows: list, field: str, plan: dict, run_dir: Path) -> int | None:
    pending = case.get("pending") if isinstance(case.get("pending"), dict) else {}
    expect = str(pending.get("expect_state") or "")
    current = _field_value(rows, field)
    before = pending.get("before_state")
    if before is None:
        before = case.get("state_before")
    if expect and current == expect:
        case["pending"] = None
        case["status"] = "running"
        case["sealed"] = False
        case["state_after"] = current
        return None
    if str(before or "") == current:
        case["status"] = "paused"
        case["state_after"] = current
        record(run_dir, exit_code=3, event_status="暂停", basis="没有变化，保持暂停", step="重跑")
        return 3
    case["status"] = "failed"
    case["sealed"] = True
    case["pending"] = None
    case["result_reason"] = "提交结果不符合预期"
    case["blocked_reason"] = "提交结果不符合预期"
    case["current_rows"] = rows
    case["state_after"] = current
    _later_waiting(run_dir, plan, str(case.get("case_id") or case.get("id") or ""))
    _finish_state(run_dir, exit_code=0)
    return 2


def accept_rerun(run_dir: Path, case_id: str, executor, *, run_id: str | None = None,
                 session=None, model=None, query=None, browser=None) -> tuple[int, dict]:
    if executor.busy():
        return 409, {"error": BUSY}
    file = _case_file(run_dir, case_id)
    if file is None:
        return 404, {"error": "找不到用例"}
    case = _read_json(file)
    if not isinstance(case, dict):
        return 404, {"error": "找不到用例"}
    if case.get("status") == "running" and not isinstance(case.get("pending"), dict):
        return 409, {"error": "这条用例正在执行"}
    state = read_state(run_dir)
    frozen = {key: state.get(key) for key in FROZEN}
    plan_bytes = (run_dir / "plan.json").read_bytes() if (run_dir / "plan.json").is_file() else None
    plan = _plan(run_dir)
    steps = _test_steps(plan, case)
    query_fn = query if query is not None else (lambda database, sql, allowed: run_query(run_dir, database, sql, allowed))
    rows, field, fixture = _select(run_dir, case, plan, query_fn)
    status = case.get("status")
    current = _attempt_number(case)
    new_attempt = current + 1
    _archive(run_dir, case_id, current)
    case["attempt"] = new_attempt
    _pointer(run_dir, case_id, new_attempt)
    outcome = 0
    if isinstance(case.get("pending"), dict):
        decided = _reobserve(case, rows, field, plan, run_dir)
        if decided is not None:
            write_json(file, case)
            _restore_plan(run_dir, plan_bytes)
            _restore_frozen(run_dir, frozen)
            if (run_dir / "report.md").is_file():
                replace_report_section(run_dir, case_id)
            return 202, {"run": run_id or run_dir.name, "case_id": case_id, "attempt": new_attempt}
        previous = _read_json(run_dir / "cases" / case_id / "attempts" / str(current) / "case.json") or {}
        pending = previous.get("pending") if isinstance(previous.get("pending"), dict) else {}
        index = pending.get("step_index")
        start = index + 1 if isinstance(index, int) and not isinstance(index, bool) else 0
        outcome = _play(
            run_dir, state, case, steps[start:], plan,
            session if session is not None else open_session(),
            model if model is not None else open_model(),
            browser if browser is not None else launch_browser,
            str(state.get("base_url") or ""),
            new_attempt,
        )
    elif status == "paused":
        index = case.get("step_index")
        start = index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else 0
        case["pending"] = None
        outcome = _play(
            run_dir, state, case, steps[start:], plan,
            session if session is not None else open_session(),
            model if model is not None else open_model(),
            browser if browser is not None else launch_browser,
            str(state.get("base_url") or ""),
            new_attempt,
        )
    elif status in REPLAY:
        confirmed = _approved_rows(state, str(case.get("check_id") or ""))
        if confirmed is None or not _same_snapshot(confirmed, rows, fixture, field):
            _block_changed(case, rows, field)
            write_json(file, case)
            write_json(run_dir / "cases" / case_id / "attempts" / str(new_attempt) / "rows.json", {"rows": rows})
            _restore_plan(run_dir, plan_bytes)
            _restore_frozen(run_dir, frozen)
            if (run_dir / "report.md").is_file():
                replace_report_section(run_dir, case_id)
            return 202, {"run": run_id or run_dir.name, "case_id": case_id, "attempt": new_attempt}
        case["pending"] = None
        case["sealed"] = False
        case["status"] = "running"
        case["state_before"] = _field_value(confirmed, field)
        case["state_after"] = _field_value(rows, field)
        case["expect_state"] = _field_value(confirmed, field)
        case["rounds"] = []
        case["evidence"] = []
        outcome = _play(
            run_dir, state, case, steps, plan,
            session if session is not None else open_session(),
            model if model is not None else open_model(),
            browser if browser is not None else launch_browser,
            str(state.get("base_url") or ""),
            new_attempt,
        )
    else:
        return 409, {"error": "这条用例正在执行"}
    if outcome == 0 and case.get("status") == "running":
        sealed = seal_case(run_dir, case, _baseline(run_dir))
        if sealed is not None:
            case = sealed
    write_json(file, case)
    if outcome == 3:
        record(run_dir, exit_code=3, event_status="暂停", basis="没有变化，保持暂停", step="重跑")
    elif outcome != 3:
        stage = "report" if (run_dir / "report.md").is_file() else None
        _finish_state(run_dir, exit_code=0 if outcome == 0 else outcome, stage=stage)
    _restore_plan(run_dir, plan_bytes)
    _restore_frozen(run_dir, frozen)
    if (run_dir / "report.md").is_file():
        replace_report_section(run_dir, case_id)
        _restore_frozen(run_dir, frozen)
    return 202, {"run": run_id or run_dir.name, "case_id": case_id, "attempt": new_attempt}


def _restore_plan(run_dir: Path, plan_bytes: bytes | None) -> None:
    if plan_bytes is None:
        return
    current = run_dir / "plan.json"
    if current.is_file() and current.read_bytes() != plan_bytes:
        current.write_bytes(plan_bytes)


def _restore_frozen(run_dir: Path, frozen: dict) -> None:
    state = read_state(run_dir)
    changed = False
    for key, value in frozen.items():
        if state.get(key) != value:
            state[key] = value
            changed = True
    if changed:
        write_json(run_dir / "state.json", state)
