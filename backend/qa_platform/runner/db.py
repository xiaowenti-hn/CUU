"""数据库只能查询。不执行 INSERT、UPDATE，也不调用 --apply-once。"""

import hashlib
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path

from qa_platform.runner.preflight import record
from qa_platform.runner.sql_grammar import GrammarError, assert_read_only, main_verb, tokenize
from qa_platform.store import ApprovalError, read_state, review_digest, timestamp, write_json

QUERY_TIMEOUT = 60


class QueryError(RuntimeError):
    pass


def query_script() -> Path:
    return Path.home() / ".grok/skills/query-db/scripts/query.sh"


def query_argv(database: str, sql: str) -> list[str]:
    return [str(query_script()), database, "-e", sql]


def filter_log_text(text: str) -> str:
    kept = []
    for line in text.splitlines():
        lowered = line.lower()
        if "password" in lowered or ".pem" in lowered or "properties" in lowered:
            continue
        kept.append(line)
    return "\n".join(kept)


def parse_tsv(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line != ""]
    if not lines:
        raise QueryError("查询结果没有表头")
    header = lines[0].split("\t")
    if not header or any(not column for column in header):
        raise QueryError("查询结果没有表头")
    rows = []
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) != len(header):
            raise QueryError("查询结果列数和表头不一致")
        rows.append({
            key: None if value == r"\N" else value
            for key, value in zip(header, cells)
        })
    return rows


def _execute(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=QUERY_TIMEOUT,
        check=False,
    )


def read_only_query(database: str, sql: str, *, allowed: list[str],
                    run_dir: Path | None = None, executor=None) -> list[dict]:
    if database not in allowed:
        raise QueryError("库不在允许名单里")
    try:
        assert_read_only(sql)
    except GrammarError as exc:
        raise QueryError(str(exc)) from exc
    argv = query_argv(database, sql)
    run = executor or _execute
    try:
        result = run(argv)
    except subprocess.TimeoutExpired as exc:
        raise QueryError("查询超时") from exc
    stderr = filter_log_text(getattr(result, "stderr", "") or "")
    if run_dir is not None and stderr:
        log = run_dir / "runner.log"
        with log.open("a", encoding="utf-8") as handle:
            handle.write(stderr + "\n")
    if getattr(result, "returncode", 1) != 0:
        raise QueryError("查询失败")
    text = getattr(result, "stdout", "") or ""
    if not text.strip():
        return []
    return parse_tsv(text)


def assert_mutation(sql: str) -> None:
    raise GrammarError("数据库只能查询")


def sql_digest(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def apply_argv(database: str, expect_rows: int, sql_file: Path) -> list[str]:
    return [
        str(query_script()),
        "--apply-once",
        database,
        "--expect-rows",
        str(expect_rows),
        "--sql-file",
        str(sql_file),
    ]


def _whole_rows(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 1 <= value <= 20:
        return value
    return None


def _nonempty(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _load_baseline(run_dir: Path) -> dict | None:
    path = run_dir / "baseline.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def propose_sql(run_dir: Path, body: dict) -> dict:
    """拒绝一切改数。不写 baseline，不写审批。"""
    del run_dir, body
    raise ApprovalError(409, "数据库只能查询，不能改数")


def _proposal_payload(body: dict, state: dict) -> dict:
    reason = _nonempty(body.get("reason"))
    sql = _nonempty(body.get("sql"))
    parameters = _nonempty(body.get("parameters"))
    database = _nonempty(body.get("database"))
    untouched = _nonempty(body.get("untouched_reason"))
    rows = _whole_rows(body.get("expected_rows"))
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    if not all([reason, sql, parameters, database, untouched]) or rows is None or database not in allowed:
        raise ApprovalError(400, "改数字段不完整")
    return {
        "reason": reason,
        "sql": sql,
        "parameters": parameters,
        "database": database,
        "expected_rows": rows,
        "untouched_reason": untouched,
    }


def arm_apply(run_dir: Path, payload: dict) -> None:
    del run_dir, payload
    raise ApprovalError(409, "数据库只能查询，不能改数")


def _write_sql_file(run_dir: Path, sql: str) -> Path:
    path = run_dir / "apply.sql"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, sql.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _execute_group(argv: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=QUERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(argv, process.returncode if process.returncode is not None else 137,
                                       stdout or "", stderr or "")


def apply_once(database: str, expect_rows: int, sql: str, *, run_dir: Path, executor=None) -> int:
    del database, expect_rows, sql, executor
    record(run_dir, exit_code=2, event_status="阻塞", basis="数据库只能查询，未执行改数", step="改数")
    return 2


def _sql_approval(state: dict) -> dict | None:
    approvals = state.get("approvals")
    if not isinstance(approvals, list):
        return None
    relevant = [item for item in approvals if isinstance(item, dict) and item.get("kind") == "sql"]
    if not relevant:
        return None
    last = relevant[-1]
    payload = last.get("payload")
    if not isinstance(payload, dict) or last.get("digest") != review_digest("sql", payload):
        return None
    return last


def _select_actual(run_dir: Path, baseline: dict, allowed: list[str], executor) -> dict:
    found = {}
    statements = baseline.get("select_sql") if isinstance(baseline.get("select_sql"), dict) else {}
    fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
    for check_id, sql in statements.items():
        if not isinstance(sql, str):
            raise QueryError("查询失败")
        fixture = fixtures.get(check_id) if isinstance(fixtures.get(check_id), dict) else {}
        database = fixture.get("database") or baseline.get("apply_database")
        if not isinstance(database, str):
            raise QueryError("查询失败")
        found[check_id] = read_only_query(database, sql, allowed=allowed, run_dir=run_dir, executor=executor)
    return found


def _open_actual(run_dir: Path, baseline: dict) -> int:
    from qa_platform.runner.baseline import _open_review
    return _open_review(
        run_dir,
        kind="baseline_actual",
        title="确认改数后的实际行",
        step="基线",
        payload=baseline,
        basis="改数已提交，等待确认实际行",
    )


def continue_apply(run_dir: Path, *, apply_now: bool, executor=None) -> int:
    baseline = _load_baseline(run_dir)
    if not isinstance(baseline, dict):
        return 2
    strategy = baseline.get("strategy")
    if strategy in {"none", "page_seed"}:
        return 2 if strategy == "none" else 0
    if strategy != "sql":
        return 2
    state = read_state(run_dir)
    approval = _sql_approval(state)
    if approval is None:
        record(run_dir, exit_code=2, event_status="阻塞", basis="写入未被观察到", step="改数")
        return 2
    payload = approval["payload"]
    digest = sql_digest(str(payload.get("sql") or ""))
    exit_code = baseline.get("apply_exit_code", None)
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    can_apply = apply_now and exit_code is None and baseline.get("apply_sql_digest") == digest
    if can_apply:
        record(run_dir, exit_code=2, event_status="阻塞", basis="数据库只能查询，未执行改数", step="改数")
        return 2
    if exit_code == 0 and "actual" not in baseline:
        try:
            actual = _select_actual(run_dir, baseline, allowed, executor)
        except QueryError as exc:
            record(run_dir, exit_code=2, event_status="阻塞", basis=str(exc), step="改数")
            return 2
        baseline["actual"] = actual
        baseline["records"] = actual
        write_json(run_dir / "baseline.json", baseline)
        return _open_actual(run_dir, baseline)
    if exit_code == 0:
        return 0
    try:
        baseline["records"] = _select_actual(run_dir, baseline, allowed, executor)
        write_json(run_dir / "baseline.json", baseline)
    except QueryError:
        pass
    fresh = read_state(run_dir)
    fresh["stop_reason"] = "baseline"
    fresh["review"] = None
    write_json(run_dir / "state.json", fresh)
    record(run_dir, exit_code=2, event_status="阻塞", basis="写入未被观察到", step="改数")
    return 2
