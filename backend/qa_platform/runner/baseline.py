"""方案批准后的只读基线。先停在主键，查询之后不点击、不改数。"""

import copy
import json
import re
import uuid
from pathlib import Path

from qa_platform.runner.db import QueryError, query_script, read_only_query
from qa_platform.runner.preflight import note, record
from qa_platform.store import (
    ApprovalError,
    find_approval,
    read_state,
    review_digest,
    timestamp,
    write_json,
)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TABLE_ANN = re.compile(r'@Table\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)')
UNSAFE_VALUE = ("\\", "'", "\n", "\r", "\t")
QUOTE_RE = re.compile(r"'([^'\\]{1,80})'|\"([^\"\\]{1,80})\"|「([^」]{1,80})」")


def quoted_suggestions(text: str) -> list[str]:
    found = []
    for match in QUOTE_RE.finditer(text or ""):
        value = next(group for group in match.groups() if group is not None)
        if value not in found:
            found.append(value)
    return found


def _requirement_text(run_dir: Path, state: dict) -> str:
    requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else {}
    relative = str(requirement.get("body_file") or "requirement.txt")
    path = run_dir / relative
    if path.is_symlink() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _load_plan(run_dir: Path) -> dict | None:
    path = run_dir / "plan.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def plan_is_approved(run_dir: Path, state: dict) -> bool:
    plan = _load_plan(run_dir)
    if plan is None:
        return False
    return find_approval(state, "plan", plan) is not None


def fixture_payload(plan: dict, requirement_text: str, allowed: list[str]) -> dict:
    suggestions = quoted_suggestions(requirement_text)
    checks = []
    for check in plan.get("checks") or []:
        if not isinstance(check, dict):
            continue
        checks.append({
            "id": check.get("id"),
            "table": check.get("table"),
            "pk": list(check.get("pk") or []),
            "state_field": check.get("state_field"),
            "source": check.get("source"),
            "allowed_databases": list(allowed),
            "suggestions": list(suggestions),
        })
    return {"checks": checks}


def _safe_value(value) -> str | None:
    if not isinstance(value, str) or any(char in value for char in UNSAFE_VALUE):
        return None
    return value


def merged_fixture_payload(review: dict, body: dict, allowed: list[str]) -> dict:
    if set(body) - {"review_id", "fixtures"}:
        raise ApprovalError(400, "主键确认只接受 fixtures")
    fixtures = body.get("fixtures")
    payload = review.get("payload")
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(fixtures, dict) or not isinstance(checks, list) or not checks:
        raise ApprovalError(400, "请填写每个检查的主键")
    expected = [check.get("id") for check in checks if isinstance(check, dict)]
    if set(fixtures) != set(expected) or any(not item for item in expected):
        raise ApprovalError(400, "请填写每个检查的主键")
    clean = {}
    for check in checks:
        if not isinstance(check, dict):
            raise ApprovalError(400, "检查标识符无效")
        check_id = check.get("id")
        item = fixtures.get(check_id)
        if not isinstance(item, dict):
            raise ApprovalError(400, "请填写每个检查的主键")
        database = item.get("database")
        if (not isinstance(database, str) or database not in allowed
                or IDENT.fullmatch(database) is None):
            raise ApprovalError(400, "库不在允许名单里")
        columns = check.get("pk")
        table = check.get("table")
        state_field = check.get("state_field")
        if (not isinstance(columns, list) or not columns
                or any(not isinstance(column, str) or IDENT.fullmatch(column) is None for column in columns)
                or not isinstance(table, str) or IDENT.fullmatch(table) is None
                or not isinstance(state_field, str) or IDENT.fullmatch(state_field) is None):
            raise ApprovalError(400, "检查标识符无效")
        pk = item.get("pk")
        if not isinstance(pk, dict) or set(pk) != set(columns):
            raise ApprovalError(400, "主键列不完整")
        values = {}
        for column in columns:
            value = _safe_value(pk.get(column))
            if value is None:
                raise ApprovalError(400, "主键值不能包含引号或反斜杠")
            values[column] = value
        clean[check_id] = {"database": database, "pk": values}
    updated = copy.deepcopy(payload)
    updated["fixtures"] = clean
    return updated


def select_statement(check: dict, fixture: dict) -> str:
    columns = list(check["pk"])
    if check["state_field"] not in columns:
        columns.append(check["state_field"])
    selected = ", ".join(f"`{column}`" for column in columns)
    where = " AND ".join(
        f"`{column}` = '{fixture['pk'][column]}'" for column in check["pk"]
    )
    return f"SELECT {selected} FROM `{check['table']}` WHERE {where} LIMIT 50"


def seed_steps(plan: dict) -> list[dict]:
    found = []
    for case in plan.get("cases") or []:
        if not isinstance(case, dict):
            continue
        for step in case.get("steps") or []:
            if isinstance(step, dict) and step.get("phase") == "seed":
                found.append(step)
    return found


def object_count(plan: dict) -> int:
    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else {}
    value = ui.get("seed_object_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def choose_strategy(plan: dict, row_counts: dict[str, int]) -> str:
    del plan
    if row_counts and all(count >= 1 for count in row_counts.values()):
        return "reuse"
    return "none"


def _open_review(run_dir: Path, *, kind: str, title: str, step: str, payload: dict, basis: str) -> int:
    state = read_state(run_dir)
    state["stage"] = "baseline"
    state["exit_code"] = 4
    state["review"] = {
        "id": uuid.uuid4().hex,
        "digest": review_digest(kind, payload),
        "kind": kind,
        "step": step,
        "title": title,
        "payload": payload,
        "requested_at": timestamp(),
    }
    write_json(run_dir / "state.json", state)
    record(run_dir, exit_code=4, event_status="待确认", basis=basis, step=step)
    return 4


def open_fixture_keys(run_dir: Path) -> int:
    state = read_state(run_dir)
    plan = _load_plan(run_dir)
    if plan is None:
        record(run_dir, exit_code=2, event_status="阻塞", basis="没有已批准的方案", step="主键")
        return 2
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    payload = fixture_payload(plan, _requirement_text(run_dir, state), allowed)
    if not payload["checks"]:
        record(run_dir, exit_code=2, event_status="阻塞", basis="方案里没有检查", step="主键")
        return 2
    return _open_review(
        run_dir,
        kind="fixture_keys",
        title="填写主键",
        step="主键",
        payload=payload,
        basis="等待填写主键，尚未查询",
    )


def _fixture_approval(state: dict) -> dict | None:
    approvals = state.get("approvals")
    if not isinstance(approvals, list):
        return None
    relevant = [item for item in approvals if isinstance(item, dict) and item.get("kind") == "fixture_keys"]
    if not relevant:
        return None
    last = relevant[-1]
    payload = last.get("payload")
    if not isinstance(payload, dict) or "fixtures" not in payload:
        return None
    if last.get("digest") != review_digest("fixture_keys", payload):
        return None
    return last


def _baseline_exists(run_dir: Path) -> dict | None:
    path = run_dir / "baseline.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _query_and_decide(run_dir: Path, state: dict, payload: dict, *, executor=None) -> int:
    plan = _load_plan(run_dir) or {}
    fixtures = payload.get("fixtures") if isinstance(payload.get("fixtures"), dict) else {}
    checks = [check for check in payload.get("checks") or [] if isinstance(check, dict)]
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    statements = {}
    rows_by_check = {}
    try:
        for check in checks:
            fixture = fixtures.get(check.get("id"))
            if not isinstance(fixture, dict):
                raise QueryError("主键还没有批准")
            sql = select_statement(check, fixture)
            statements[check["id"]] = sql
            rows_by_check[check["id"]] = read_only_query(
                fixture["database"],
                sql,
                allowed=allowed,
                run_dir=run_dir,
                executor=executor,
            )
    except QueryError as exc:
        record(run_dir, exit_code=2, event_status="阻塞", basis=str(exc), step="基线")
        return 2
    strategy = choose_strategy(plan, {key: len(rows) for key, rows in rows_by_check.items()})
    document = {
        "strategy": strategy,
        "fixtures": fixtures,
        "tables": [
            {
                "check_id": check.get("id"),
                "name": check.get("table"),
                "pk": list(check.get("pk") or []),
                "state_field": check.get("state_field"),
                "source": check.get("source"),
            }
            for check in checks
        ],
        "records": rows_by_check,
        "select_sql": statements,
        "queried_at": timestamp(),
    }
    if strategy == "page_seed":
        document["seed_steps"] = [
            {
                "phase": step.get("phase"),
                "action": step.get("action"),
                "role": step.get("role"),
                "name": step.get("name"),
                "locator_status": step.get("locator_status"),
            }
            for step in seed_steps(plan)
        ]
    write_json(run_dir / "baseline.json", document)
    if strategy == "none":
        fresh = read_state(run_dir)
        fresh["stage"] = "baseline"
        fresh["stop_reason"] = "baseline"
        fresh["review"] = None
        write_json(run_dir / "state.json", fresh)
        record(
            run_dir, exit_code=2, event_status="阻塞",
            basis="数据库只能查询，没有可复用的行，未改数也未铺垫", step="基线",
        )
        return 2
    if strategy == "sql_offer":
        return _open_review(
            run_dir,
            kind="sql_offer",
            title="改数门槛",
            step="基线",
            payload=document,
            basis="查询为空，已记下改数门槛，尚未执行",
        )
    title = "确认基线" if strategy == "reuse" else "确认页面铺垫"
    basis = "每个检查都有行，等待确认基线" if strategy == "reuse" else "查询为空，将用页面铺垫，尚未点击"
    return _open_review(
        run_dir,
        kind="baseline",
        title=title,
        step="基线",
        payload=document,
        basis=basis,
    )


def _split_samples(value: str) -> list[str]:
    found = []
    for part in re.split(r"[,，\s]+", value):
        safe = _safe_value(part.strip())
        if safe and safe not in found:
            found.append(safe)
        if len(found) == 3:
            break
    return found


def lookup_samples(plan: dict, check_id: str) -> tuple[str, list[str]]:
    for case in plan.get("cases") or []:
        if not isinstance(case, dict) or case.get("check_id") != check_id:
            continue
        for step in case.get("steps") or []:
            if not isinstance(step, dict) or step.get("action") != "type":
                continue
            name = step.get("name")
            raw = step.get("value")
            if not isinstance(name, str) or not name.strip() or not isinstance(raw, str):
                continue
            values = _split_samples(raw)
            if values:
                return name.strip(), values
    return "", []


def match_column(rows: list[dict], label: str) -> tuple[str, str] | None:
    """先对列注释，再对完整列名。不用固定的字段别名。"""
    if not label:
        return None
    for row in rows:
        comment = str(row.get("COLUMN_COMMENT") or "")
        if label in comment:
            return str(row["COLUMN_NAME"]), comment
    wanted = label.lower()
    for row in rows:
        name = str(row.get("COLUMN_NAME") or "")
        if name.lower() == wanted:
            return name, str(row.get("COLUMN_COMMENT") or "")
    return None


def _pk_column(rows: list[dict], pk: list[str]) -> tuple[str, str] | None:
    comments = {
        str(row.get("COLUMN_NAME") or ""): str(row.get("COLUMN_COMMENT") or "")
        for row in rows
    }
    for name in pk:
        if name in comments:
            return name, comments[name]
    return None


def annotated_table(repo: str, source: str) -> str:
    if not repo or not isinstance(source, str) or ":" not in source:
        return ""
    relative, _, _line = source.rpartition(":")
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        return ""
    file = Path(repo) / path
    try:
        if file.is_symlink() or not file.is_file():
            return ""
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = TABLE_ANN.search(text)
    return match.group(1) if match else ""


def choose_table(planned: str, rows: list[dict]) -> str | None:
    names = [str(row.get("TABLE_NAME") or "") for row in rows if row.get("TABLE_NAME")]
    if planned in names:
        return planned
    suffix = [name for name in names if name.endswith("_" + planned)]
    if not suffix:
        return None
    suffix.sort(key=len)
    return suffix[0]


def _plain(expr: str) -> str:
    """注释里的换行和制表符会把批处理结果拆行，先压成空格。"""
    return (
        f"REPLACE(REPLACE(REPLACE(IFNULL({expr}, ''), CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')"
    )


def _schema_sql(database: str, table: str) -> str:
    return (
        f"SELECT TABLE_NAME, {_plain('TABLE_COMMENT')} AS TABLE_COMMENT "
        "FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{database}' AND "
        f"(TABLE_NAME = '{table}' OR TABLE_NAME LIKE '%\\_{table}')"
    )


def _column_sql(database: str, table: str) -> str:
    return (
        f"SELECT COLUMN_NAME, {_plain('COLUMN_COMMENT')} AS COLUMN_COMMENT, COLUMN_KEY "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{database}' AND TABLE_NAME = '{table}' "
        "ORDER BY ORDINAL_POSITION"
    )


def tables_in_code(materials: str) -> list[str]:
    """代码注解里写到的表。没有注解就不另找一套固定的表。"""
    found = []
    for match in TABLE_ANN.finditer(materials or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
        if len(found) == 40:
            break
    return found


def code_table_catalog(database: str, materials: str, extra_tables: list[str] | None = None) -> list[dict]:
    """这些表在库里真实存在时，带上列名和列注释交给方案。"""
    names = tables_in_code(materials)
    for name in extra_tables or []:
        if isinstance(name, str) and IDENT.fullmatch(name) and name not in names:
            names.append(name)
        if len(names) >= 80:
            break
    if not names or IDENT.fullmatch(database) is None or not query_script().is_file():
        return []
    catalog = []
    for planned in names:
        try:
            found = read_only_query(database, _schema_sql(database, planned), allowed=[database])
            table = choose_table(planned, found)
            if table is None:
                continue
            comment = ""
            for row in found:
                if str(row.get("TABLE_NAME") or "") == table:
                    comment = str(row.get("TABLE_COMMENT") or "")
                    break
            columns = read_only_query(database, _column_sql(database, table), allowed=[database])
        except (QueryError, OSError):
            return catalog
        described = []
        for row in columns:
            column = str(row.get("COLUMN_NAME") or "")
            if not column:
                continue
            described.append({
                "name": column,
                "comment": str(row.get("COLUMN_COMMENT") or ""),
            })
            if len(described) == 40:
                break
        if described:
            catalog.append({
                "annotation": planned,
                "table": table,
                "table_comment": comment,
                "columns": described,
            })
    return catalog


def _harvest_sql(table: str, column: str) -> str:
    """不拿模型编的示例号当条件。从表现有数据里取最多 500 条。"""
    return (
        f"SELECT `{column}` FROM `{table}` "
        f"WHERE `{column}` IS NOT NULL AND `{column}` <> '' LIMIT 500"
    )


def _query_from_schema(run_dir: Path, state: dict, *, executor=None) -> int:
    plan = _load_plan(run_dir) or {}
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    if len(allowed) != 1:
        return open_fixture_keys(run_dir)
    database = allowed[0]
    if IDENT.fullmatch(database) is None:
        record(run_dir, exit_code=2, event_status="阻塞", basis="库名不是标识符", step="基线")
        return 2
    checks = [check for check in plan.get("checks") or [] if isinstance(check, dict)]
    if not checks:
        record(run_dir, exit_code=2, event_status="阻塞", basis="方案里没有检查", step="基线")
        return 2
    note(run_dir, step="基线", basis="正在根据表注释决定查询，不再询问主键")
    statements = {}
    rows_by_check = {}
    fixtures = {}
    tables = []
    test_values = {}
    skipped = []
    repo = str(state.get("repo_path") or "")
    try:
        for check in checks:
            planned = str(check.get("table") or "")
            state_field = str(check.get("state_field") or "")
            if IDENT.fullmatch(planned) is None or IDENT.fullmatch(state_field) is None:
                raise QueryError("检查标识符无效")
            found = read_only_query(
                database, _schema_sql(database, planned), allowed=allowed,
                run_dir=run_dir, executor=executor,
            )
            table = choose_table(planned, found)
            if table is None:
                annotated = annotated_table(repo, str(check.get("source") or ""))
                if annotated and annotated != planned:
                    skipped.append(f"{planned}（代码里的表是 {annotated}，{database} 里没有）")
                else:
                    skipped.append(f"{planned}（{database} 里没有这张表）")
                continue
            columns = read_only_query(
                database, _column_sql(database, table), allowed=allowed,
                run_dir=run_dir, executor=executor,
            )
            names = [str(row.get("COLUMN_NAME") or "") for row in columns]
            if not names:
                raise QueryError(f"表 {table} 没有字段")
            label, _samples = lookup_samples(plan, str(check.get("id") or ""))
            pk = [str(item) for item in (check.get("pk") or []) if isinstance(item, str) and item]
            if label:
                matched = match_column(columns, label)
                tried = label
            else:
                matched = _pk_column(columns, pk)
                tried = pk[0] if pk else ""
            if not matched:
                skipped.append(f"{table}（没有注释或列名对上「{tried or '要填的字段'}」）")
                continue
            where_column, comment = matched
            if where_column not in names:
                skipped.append(f"{table}.{where_column}（表里没有这一列）")
                continue
            sql = _harvest_sql(table, where_column)
            statements[check["id"]] = sql
            rows = read_only_query(
                database, sql, allowed=allowed, run_dir=run_dir, executor=executor,
            )
            values = []
            for row in rows:
                value = row.get(where_column)
                if value is None:
                    continue
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)
            rows_by_check[check["id"]] = rows
            test_values[check["id"]] = values
            fixtures[check["id"]] = {
                "database": database,
                "pk": {where_column: values[0]} if values else {},
                "column_comment": comment,
            }
            tables.append({
                "check_id": check.get("id"),
                "name": table,
                "pk": [where_column] if where_column else list(check.get("pk") or []),
                "state_field": state_field,
                "source": check.get("source"),
            })
    except QueryError as exc:
        record(run_dir, exit_code=2, event_status="阻塞", basis=str(exc), step="基线")
        return 2
    if not rows_by_check:
        record(
            run_dir, exit_code=2, event_status="阻塞",
            basis="没有可查询的表：" + "、".join(skipped),
            step="基线",
        )
        return 2
    strategy = "reuse" if any(len(rows) >= 1 for rows in rows_by_check.values()) else "none"
    document = {
        "strategy": strategy,
        "fixtures": fixtures,
        "tables": tables,
        "records": rows_by_check,
        "select_sql": statements,
        "test_values": test_values,
        "queried_at": timestamp(),
    }
    write_json(run_dir / "baseline.json", document)
    if strategy == "none":
        described = []
        for item in tables:
            key = (item.get("pk") or [""])[0]
            described.append(f"{item.get('name')}.{key}" if key else str(item.get("name")))
        fresh = read_state(run_dir)
        fresh["stage"] = "baseline"
        fresh["stop_reason"] = "baseline"
        fresh["review"] = None
        write_json(run_dir / "state.json", fresh)
        queries = [f"{key}: {sql}" for key, sql in statements.items()]
        basis = "没有查到行。下面是只读查询，平台不会执行插入。请在页面准备数据，或在库外插入。准备好后从「基线」重试。"
        if queries:
            basis += "\n" + "\n".join(queries[:6])
        if skipped:
            basis += "\n未查：" + "、".join(skipped)
        record(
            run_dir, exit_code=2, event_status="阻塞",
            basis=basis,
            step="基线",
        )
        return 2
    counts = "、".join(f"{key} {len(values)} 条" for key, values in test_values.items())
    basis = f"已从库里取出真实数据：{counts}"
    if skipped:
        basis += "。未查：" + "、".join(skipped)
    return _open_review(
        run_dir,
        kind="baseline",
        title="确认基线",
        step="基线",
        payload=document,
        basis=basis,
    )


def continue_baseline(run_dir: Path, *, executor=None) -> int:
    state = read_state(run_dir)
    review = state.get("review")
    if isinstance(review, dict) and review.get("kind") != "fixture_keys":
        return 4
    existing = _baseline_exists(run_dir)
    if existing is not None:
        if existing.get("strategy") == "none":
            return 2
        return 0
    approval = _fixture_approval(state)
    if approval is not None:
        return _query_and_decide(run_dir, state, approval["payload"], executor=executor)
    if isinstance(review, dict) and review.get("kind") == "fixture_keys":
        fresh = read_state(run_dir)
        fresh["review"] = None
        write_json(run_dir / "state.json", fresh)
        state = fresh
    return _query_from_schema(run_dir, state, executor=executor)
