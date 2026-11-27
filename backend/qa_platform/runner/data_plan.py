"""方案里的数据计划。只读核对，不造数。没有 data_plan 的旧方案不走这里。"""

import json
import re
from pathlib import Path

from qa_platform.runner.db import QueryError, read_only_query
from qa_platform.runner.preflight import note, record
from qa_platform.runner.sql_grammar import GrammarError, assert_read_only
from qa_platform.store import read_state, timestamp, write_json

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SOURCE_LINE = re.compile(r"^(.+):([0-9]+)$")
EVIDENCE = {"page", "api", "task", "message", "file", "table", "db", "external"}
TABLE_EVIDENCE = {"table", "db"}
PURPOSE = {"require", "expect_absent"}
PREPARE = {"reuse", "page", "sql", "none"}
OPS = {"eq", "ne", "in", "is_null", "range"}
ROW_LIMIT = 50
PAGE_BLOCK = "数据库只能查询，未做页面铺垫"
SQL_BLOCK = "数据库只能查询，不能改数"
_UNSAFE = ("\\", "\n", "\r", "\x00")


def has_data_plan(plan: dict) -> bool:
    return isinstance(plan, dict) and isinstance(plan.get("data_plan"), dict)


def case_omits_check(doc: dict, case: dict) -> bool:
    """这条用例的证据不是表，方案里就不写 check_id。"""
    if not isinstance(case, dict):
        return False
    item = _items(doc).get(case.get("data_plan_id"))
    if not isinstance(item, dict):
        return False
    return item.get("evidence") in EVIDENCE and item.get("evidence") not in TABLE_EVIDENCE


def case_value_flags(baseline: dict, case: dict) -> tuple[list | None, bool, bool]:
    """返回这条用例能填的值、是不是空结果、是不是没查到。

    expect_absent 查到 0 行是核对通过，不算缺数据。
    """
    saved = baseline.get("test_values") if isinstance(baseline.get("test_values"), dict) else {}
    items = baseline.get("items") if isinstance(baseline.get("items"), dict) else {}
    plan_id = case.get("data_plan_id") if isinstance(case.get("data_plan_id"), str) else ""
    check_id = case.get("check_id") if isinstance(case.get("check_id"), str) else ""
    purpose = ""
    item = items.get(plan_id) if plan_id else None
    if isinstance(item, dict):
        purpose = str(item.get("purpose") or "")
    found = False
    real = None
    if plan_id and plan_id in saved:
        real = saved.get(plan_id)
        found = True
    elif check_id and check_id in saved:
        real = saved.get(check_id)
        found = True
    values = real if isinstance(real, list) else None
    absent_ok = purpose == "expect_absent"
    empty_data = isinstance(values, list) and not values and not absent_ok
    unqueried = (
        not found
        and not absent_ok
        and bool(saved)
        and bool(check_id)
        and check_id not in saved
        and (not plan_id or plan_id not in saved)
    )
    return values, empty_data, unqueried


def validate_data_plan(
    doc: dict,
    *,
    material_files=None,
    allowed_databases=None,
    requirement: str = "",
    table_columns=None,
    index_paths=None,
) -> list[str]:
    """新方案才调用。旧方案没有 data_plan，不经过这里。"""
    errors: list[str] = []
    plan = doc.get("data_plan")
    if not isinstance(plan, dict):
        return ["data_plan 必须是对象"]
    acceptances = plan.get("acceptances")
    items = plan.get("items")
    if not isinstance(acceptances, list) or not acceptances:
        errors.append("data_plan.acceptances 至少一条")
        acceptances = []
    if not isinstance(items, list) or not items:
        errors.append("data_plan.items 至少一条")
        items = []
    acceptance_ids: set[str] = set()
    for item in acceptances:
        if not isinstance(item, dict):
            errors.append("验收点必须是对象")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in acceptance_ids:
            errors.append("验收点 id 必须唯一且非空")
        else:
            acceptance_ids.add(item_id)
        expect = item.get("expect")
        label = item_id if isinstance(item_id, str) and item_id else "未命名"
        if not isinstance(expect, str) or not expect.strip():
            errors.append(f"验收点「{label}」要有 expect")
    known_columns = _column_index(table_columns)
    paths = index_paths if isinstance(index_paths, set) else set()
    item_by_id: dict[str, dict] = {}
    seen_items: set[str] = set()
    seen_checks: set[str] = set()
    checks = {
        check.get("id"): check
        for check in (doc.get("checks") or [])
        if isinstance(check, dict) and isinstance(check.get("id"), str) and check.get("id")
    }
    for item in items:
        if not isinstance(item, dict):
            errors.append("数据计划里的每一条必须是对象")
            continue
        item_id = item.get("id")
        label = item_id if isinstance(item_id, str) and item_id else "未命名"
        if not isinstance(item_id, str) or not item_id or item_id in seen_items:
            errors.append("数据计划 id 必须唯一且非空")
        else:
            seen_items.add(item_id)
            item_by_id[item_id] = item
        acceptance_id = item.get("acceptance_id")
        if not isinstance(acceptance_id, str) or acceptance_id not in acceptance_ids:
            errors.append(f"数据计划「{label}」的 acceptance_id 对不上验收点")
        evidence = item.get("evidence")
        if evidence not in EVIDENCE:
            errors.append(
                f"数据计划「{label}」的 evidence 只能是 page、api、task、message、file、table、external"
            )
        purpose = item.get("purpose")
        if purpose not in PURPOSE:
            errors.append(f"数据计划「{label}」的 purpose 只能是 require 或 expect_absent")
        prepare = item.get("prepare")
        if prepare not in PREPARE:
            errors.append(f"数据计划「{label}」的 prepare 只能是 reuse、page、sql、none")
        if "min_rows" in item and purpose == "require":
            minimum = item.get("min_rows")
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
                errors.append(f"数据计划「{label}」的 min_rows 必须是大于等于 1 的整数")
        blockers = item.get("blockers", [])
        if not isinstance(blockers, list) or any(not isinstance(text, str) or not text.strip() for text in blockers):
            errors.append(f"数据计划「{label}」的 blockers 必须是字符串数组")
            blockers = []
        errors.extend(_need_errors(
            label, item.get("needs", []),
            requirement=requirement, material_files=material_files, index_paths=paths,
        ))
        table_item = evidence in TABLE_EVIDENCE
        where = item.get("where", [])
        if not table_item:
            if where:
                errors.append(f"数据计划「{label}」不查表，不要写 where")
            if item.get("check_id"):
                errors.append(f"数据计划「{label}」不查表，不要写 check_id")
            continue
        table = item.get("table")
        if not isinstance(table, str) or IDENT.fullmatch(table) is None:
            errors.append(f"数据计划「{label}」的 table 必须是标识符")
            table = ""
        check_id = item.get("check_id")
        check = checks.get(check_id) if isinstance(check_id, str) else None
        if not isinstance(check_id, str) or not check_id or check is None:
            errors.append(f"数据计划「{label}」查表要写 check_id，并指向 checks")
        elif check_id in seen_checks:
            errors.append(f"数据计划「{label}」的 check_id 和别的计划重复")
        else:
            seen_checks.add(check_id)
            if table and check.get("table") != table:
                errors.append(f"数据计划「{label}」的 table 要和检查「{check_id}」一致")
        if prepare in {"reuse", "none"}:
            if not isinstance(where, list) or not where:
                errors.append(f"数据计划「{label}」查表要写 where")
        if "database" in item or (isinstance(check, dict) and "database" in check):
            named = item.get("database", check.get("database") if isinstance(check, dict) else None)
            if allowed_databases is not None and named not in allowed_databases:
                errors.append(f"数据计划「{label}」的库不在允许名单里")
        elif (
            table_item
            and prepare in {"reuse", "none"}
            and isinstance(allowed_databases, list)
            and len(allowed_databases) != 1
        ):
            errors.append(f"数据计划「{label}」要写 database")
        if isinstance(where, list):
            live = known_columns.get(table) if table else None
            for clause in where:
                errors.extend(_where_errors(
                    label, clause,
                    table=table,
                    live=live,
                    requirement=requirement,
                    material_files=material_files,
                    index_paths=paths,
                ))
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        case_id = case.get("id") if isinstance(case.get("id"), str) and case.get("id") else "未命名"
        acceptance_id = case.get("acceptance_id")
        if not isinstance(acceptance_id, str) or acceptance_id not in acceptance_ids:
            errors.append(f"用例「{case_id}」要有 acceptance_id")
        plan_id = case.get("data_plan_id")
        item = item_by_id.get(plan_id) if isinstance(plan_id, str) else None
        if not isinstance(plan_id, str) or not plan_id or item is None:
            errors.append(f"用例「{case_id}」要有 data_plan_id")
            continue
        if item.get("evidence") in TABLE_EVIDENCE and case.get("check_id") != item.get("check_id"):
            errors.append(f"用例「{case_id}」的 check_id 要和数据计划「{plan_id}」一致")
        blockers = item.get("blockers") if isinstance(item.get("blockers"), list) else []
        if blockers and _has_steps(case):
            errors.append(f"数据计划「{plan_id}」有阻断，不能写成可执行用例")
    return errors


def compile_select(item: dict, check: dict | None, *, table: str, columns: list[str]) -> str:
    """用白名单运算符拼一条 SELECT。不执行模型写的 SQL。"""
    if IDENT.fullmatch(table) is None:
        raise ValueError(f"数据计划「{_id(item)}」的 table 必须是标识符")
    selected: list[str] = []
    where_sql: list[str] = []
    clauses = item.get("where") if isinstance(item.get("where"), list) else []
    known = set(columns)
    for clause in clauses:
        if not isinstance(clause, dict):
            raise ValueError(f"数据计划「{_id(item)}」的 where 必须是对象")
        column = clause.get("column")
        op = clause.get("op")
        if not isinstance(column, str) or IDENT.fullmatch(column) is None or column not in known:
            raise ValueError(f"数据计划「{_id(item)}」的列 {column} 不在表 {table} 里")
        if op not in OPS:
            raise ValueError(f"数据计划「{_id(item)}」的运算符只能是 eq、ne、in、is_null、range")
        if column not in selected:
            selected.append(column)
        where_sql.append(_clause_sql(item, column, op, clause.get("value")))
    for extra in _check_columns(check):
        if extra in known and extra not in selected:
            selected.append(extra)
    if not selected:
        raise ValueError(f"数据计划「{_id(item)}」没有可查询的列")
    if not where_sql:
        raise ValueError(f"数据计划「{_id(item)}」查表要写 where")
    chosen = ", ".join(f"`{column}`" for column in selected)
    sql = f"SELECT {chosen} FROM `{table}` WHERE {' AND '.join(where_sql)} LIMIT {ROW_LIMIT}"
    try:
        assert_read_only(sql)
    except GrammarError as exc:
        raise ValueError("数据库只能查询") from exc
    return sql


def assess_items(plan: dict, *, allowed: list[str], columns_for, query) -> dict:
    """逐条核对。缺数据只记在这一条上，不把整次运行判成没有策略。"""
    described: dict[str, dict] = {}
    test_values: dict[str, list] = {}
    select_sql: dict[str, str] = {}
    checks = {
        check.get("id"): check
        for check in (plan.get("checks") or [])
        if isinstance(check, dict) and isinstance(check.get("id"), str)
    }
    for item in _plan_items(plan):
        item_id = _id(item)
        purpose = item.get("purpose") if item.get("purpose") in PURPOSE else ""
        evidence = item.get("evidence")
        prepare = item.get("prepare")
        base = {
            "status": "ready",
            "reason": "",
            "purpose": purpose,
            "evidence": evidence if isinstance(evidence, str) else "",
            "prepare": prepare if isinstance(prepare, str) else "",
            "sql": "",
            "row_count": 0,
            "values": [],
        }
        blockers = [text.strip() for text in (item.get("blockers") or []) if isinstance(text, str) and text.strip()]
        if blockers:
            base["status"] = "blocked"
            base["reason"] = "；".join(blockers)
            described[item_id] = base
            continue
        if prepare == "page":
            base["status"] = "blocked"
            base["reason"] = PAGE_BLOCK
            described[item_id] = base
            continue
        if prepare == "sql":
            base["status"] = "blocked"
            base["reason"] = SQL_BLOCK
            described[item_id] = base
            continue
        if evidence not in TABLE_EVIDENCE:
            described[item_id] = base
            continue
        check = checks.get(item.get("check_id"))
        database = _database(item, check if isinstance(check, dict) else None, allowed)
        table = item.get("table") if isinstance(item.get("table"), str) else ""
        if not database or IDENT.fullmatch(table or "") is None:
            base["status"] = "blocked"
            base["reason"] = (
                f"数据计划「{item_id}」的库不在允许名单里"
                if not database
                else f"数据计划「{item_id}」的 table 必须是标识符"
            )
            described[item_id] = base
            continue
        try:
            live = columns_for(database, table)
        except (QueryError, OSError, ValueError) as exc:
            base["status"] = "blocked"
            base["reason"] = str(exc) or "查询失败"
            described[item_id] = base
            continue
        resolved, names = _live_table(live, table)
        if not names:
            base["status"] = "blocked"
            base["reason"] = f"数据计划「{item_id}」的表 {table} 在库里不存在"
            described[item_id] = base
            continue
        missing = [
            clause.get("column")
            for clause in (item.get("where") or [])
            if isinstance(clause, dict) and clause.get("column") not in names
        ]
        if missing:
            base["status"] = "blocked"
            base["reason"] = f"数据计划「{item_id}」的列 {'、'.join(str(column) for column in missing)} 不在表 {resolved} 里"
            described[item_id] = base
            continue
        try:
            sql = compile_select(item, check if isinstance(check, dict) else None, table=resolved, columns=names)
        except ValueError as exc:
            base["status"] = "blocked"
            base["reason"] = str(exc)
            described[item_id] = base
            continue
        base["sql"] = sql
        select_sql[item_id] = sql
        try:
            rows = query(database, sql)
        except (QueryError, OSError, ValueError) as exc:
            base["status"] = "blocked"
            base["reason"] = str(exc) or "查询失败"
            described[item_id] = base
            continue
        if not isinstance(rows, list):
            rows = []
        base["row_count"] = len(rows)
        if purpose == "expect_absent":
            if rows:
                base["status"] = "blocked"
                base["reason"] = (
                    f"数据计划「{item_id}」预期没有匹配行，{resolved} 上还有 {len(rows)} 行。这一条还不能跑。"
                )
            described[item_id] = base
            if base["status"] == "ready":
                _remember_values(test_values, item, [])
            continue
        values = _row_values(rows, item, check if isinstance(check, dict) else None)
        minimum = item.get("min_rows")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            minimum = 1
        if len(rows) < minimum:
            base["status"] = "blocked"
            base["reason"] = (
                f"数据计划「{item_id}」没有查到行：{resolved} 上 {_where_text(item)}。这一条还不能跑。"
            )
            described[item_id] = base
            continue
        base["values"] = values
        described[item_id] = base
        _remember_values(test_values, item, values)
    return {
        "strategy": "data_plan",
        "items": described,
        "test_values": test_values,
        "select_sql": select_sql,
        "queried_at": timestamp(),
    }


def ensure_assessed(run_dir: Path, *, executor=None) -> int:
    """批准后的新方案按数据计划核对。有能跑的用例就返回 0，整次都没有就阻塞。"""
    from qa_platform.runner.baseline import _column_sql, _load_plan, _schema_sql, choose_table

    state = read_state(run_dir)
    plan = _load_plan(run_dir) or {}
    state["stage"] = "baseline"
    state["review"] = None
    write_json(run_dir / "state.json", state)
    existing = _read_baseline(run_dir)
    if isinstance(existing, dict) and existing.get("strategy") == "data_plan" and isinstance(existing.get("items"), dict):
        document = existing
    else:
        allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]

        def columns_for(database: str, table: str):
            found = read_only_query(
                database, _schema_sql(database, table),
                allowed=allowed, run_dir=run_dir, executor=executor,
            )
            chosen = choose_table(table, found)
            if not chosen:
                return None
            rows = read_only_query(
                database, _column_sql(database, chosen),
                allowed=allowed, run_dir=run_dir, executor=executor,
            )
            names = [str(row.get("COLUMN_NAME") or "") for row in rows if row.get("COLUMN_NAME")]
            if not names:
                return None
            return {"table": chosen, "columns": names}

        def query(database: str, sql: str):
            return read_only_query(
                database, sql, allowed=allowed, run_dir=run_dir, executor=executor,
            )

        note(run_dir, step="基线", basis="正在按数据计划做只读核对")
        document = assess_items(plan, allowed=allowed, columns_for=columns_for, query=query)
        write_json(run_dir / "baseline.json", document)
    ready, blocked = _split_cases(plan, document)
    _write_blocked(run_dir, blocked)
    if ready:
        if blocked:
            brief = "、".join(case_id for case_id, _reason in blocked)
            note(run_dir, step="基线", basis=f"能跑的用例继续。已单独阻塞：{brief}")
        else:
            note(run_dir, step="基线", basis="数据计划核对过了，开始执行")
        return 0
    reasons = [reason for _case_id, reason in blocked if reason]
    basis = "；".join(dict.fromkeys(reasons)) or "没有可以执行的用例"
    fresh = read_state(run_dir)
    fresh["stop_reason"] = "baseline"
    write_json(run_dir / "state.json", fresh)
    record(run_dir, exit_code=2, event_status="阻塞", basis=basis, step="基线")
    return 2


def _items(doc: dict) -> dict[str, dict]:
    plan = doc.get("data_plan") if isinstance(doc.get("data_plan"), dict) else {}
    found = {}
    for item in plan.get("items") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id"):
            found[item["id"]] = item
    return found


def _plan_items(plan: dict) -> list[dict]:
    data = plan.get("data_plan") if isinstance(plan.get("data_plan"), dict) else {}
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


def _id(item: dict) -> str:
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) and item_id else "未命名"


def _column_index(table_columns) -> dict[str, set[str]]:
    found = {}
    if not isinstance(table_columns, dict):
        return found
    for table, columns in table_columns.items():
        if not isinstance(table, str):
            continue
        if isinstance(columns, set):
            found[table] = set(columns)
        elif isinstance(columns, (list, tuple)):
            found[table] = {column for column in columns if isinstance(column, str)}
    return found


def _need_errors(label: str, needs, *, requirement: str, material_files, index_paths: set[str]) -> list[str]:
    if needs is None:
        return []
    if not isinstance(needs, list):
        return [f"数据计划「{label}」的 needs 必须是数组"]
    errors = []
    for need in needs:
        if not isinstance(need, dict):
            errors.append(f"数据计划「{label}」的 needs 要写 text 和 source")
            continue
        text = need.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"数据计划「{label}」的 needs 要写 text 和 source")
        errors.extend(_source_errors(
            label, need.get("source"),
            requirement=requirement, material_files=material_files, index_paths=index_paths,
        ))
    return errors


def _where_errors(label, clause, *, table: str, live, requirement, material_files, index_paths) -> list[str]:
    if not isinstance(clause, dict):
        return [f"数据计划「{label}」的 where 必须是对象"]
    errors = []
    column = clause.get("column")
    if not isinstance(column, str) or IDENT.fullmatch(column) is None:
        errors.append(f"数据计划「{label}」的列名必须是标识符")
        column = ""
    op = clause.get("op")
    if op not in OPS:
        errors.append(f"数据计划「{label}」的运算符只能是 eq、ne、in、is_null、range")
    elif column:
        errors.extend(_value_errors(label, column, op, clause.get("value"), clause))
    if column and isinstance(live, set) and column not in live:
        shown = table or "该表"
        errors.append(f"数据计划「{label}」的列 {column} 不在表 {shown} 里")
    if "source" not in clause or clause.get("source") in (None, ""):
        errors.append(f"数据计划「{label}」的条件 {column or '未命名'} 没有来源")
    else:
        errors.extend(_source_errors(
            label, clause.get("source"),
            requirement=requirement, material_files=material_files, index_paths=index_paths,
        ))
    return errors


def _value_errors(label: str, column: str, op: str, value, clause: dict) -> list[str]:
    if op == "is_null":
        if "value" in clause and value is not None:
            return [f"数据计划「{label}」的条件 {column} 不要写值"]
        return []
    if op == "range":
        if not isinstance(value, dict):
            return [f"数据计划「{label}」的条件 {column} 要写起止"]
        start = value.get("start")
        end = value.get("end")
        if start in (None, "") and end in (None, ""):
            return [f"数据计划「{label}」的条件 {column} 要写起止"]
        errors = []
        for bound in (start, end):
            if bound in (None, ""):
                continue
            if not isinstance(bound, str) or any(char in bound for char in _UNSAFE):
                errors.append(f"数据计划「{label}」的条件值不能包含反斜杠或换行")
        return errors
    if op == "in":
        if not isinstance(value, list) or not value:
            return [f"数据计划「{label}」的条件 {column} 要有字符串值"]
        if any(not isinstance(item, str) or not item or any(char in item for char in _UNSAFE) for item in value):
            return [f"数据计划「{label}」的条件值不能包含反斜杠或换行"]
        return []
    if not isinstance(value, str) or not value or any(char in value for char in _UNSAFE):
        if not isinstance(value, str) or not value:
            return [f"数据计划「{label}」的条件 {column} 要有字符串值"]
        return [f"数据计划「{label}」的条件值不能包含反斜杠或换行"]
    return []


def _source_errors(label: str, source, *, requirement: str, material_files, index_paths: set[str]) -> list[str]:
    if not isinstance(source, str) or not source.strip():
        return [f"数据计划「{label}」的来源要写成 requirement:需求原句 或 path:line"]
    if source.startswith("requirement:"):
        quote = source[len("requirement:"):].strip()
        if not quote or quote not in (requirement or ""):
            return [f"数据计划「{label}」的来源不在需求正文里"]
        return []
    match = SOURCE_LINE.fullmatch(source)
    if not match:
        return [f"数据计划「{label}」的来源要写成 requirement:需求原句 或 path:line"]
    path = match.group(1)
    if material_files is not None and path not in material_files and path not in index_paths:
        return [f"数据计划「{label}」的来源路径不在本次材料里"]
    return []


def _has_steps(case: dict) -> bool:
    steps = case.get("steps")
    return isinstance(steps, list) and any(isinstance(step, dict) for step in steps)


def _check_columns(check: dict | None) -> list[str]:
    if not isinstance(check, dict):
        return []
    columns = []
    for column in check.get("pk") or []:
        if isinstance(column, str) and column not in columns:
            columns.append(column)
    state_field = check.get("state_field")
    if isinstance(state_field, str) and state_field and state_field not in columns:
        columns.append(state_field)
    return columns


def _clause_sql(item: dict, column: str, op: str, value) -> str:
    quoted = f"`{column}`"
    if op == "eq":
        return f"{quoted} = {_literal(item, value)}"
    if op == "ne":
        return f"{quoted} <> {_literal(item, value)}"
    if op == "in":
        if not isinstance(value, list) or not value:
            raise ValueError(f"数据计划「{_id(item)}」的条件 {column} 要有字符串值")
        joined = ", ".join(_literal(item, item_value) for item_value in value)
        return f"{quoted} IN ({joined})"
    if op == "is_null":
        return f"{quoted} IS NULL"
    if op == "range":
        if not isinstance(value, dict):
            raise ValueError(f"数据计划「{_id(item)}」的条件 {column} 要写起止")
        parts = []
        start = value.get("start")
        end = value.get("end")
        if isinstance(start, str) and start:
            parts.append(f"{quoted} >= {_literal(item, start)}")
        if isinstance(end, str) and end:
            parts.append(f"{quoted} < {_literal(item, end)}")
        if not parts:
            raise ValueError(f"数据计划「{_id(item)}」的条件 {column} 要写起止")
        return " AND ".join(parts)
    raise ValueError(f"数据计划「{_id(item)}」的运算符只能是 eq、ne、in、is_null、range")


def _literal(item: dict, value) -> str:
    if not isinstance(value, str) or any(char in value for char in _UNSAFE):
        raise ValueError(f"数据计划「{_id(item)}」的条件值不能包含反斜杠或换行")
    return "'" + value.replace("'", "''") + "'"


def _where_text(item: dict) -> str:
    parts = []
    for clause in item.get("where") or []:
        if not isinstance(clause, dict):
            continue
        column = str(clause.get("column") or "")
        op = clause.get("op")
        value = clause.get("value")
        if op == "eq":
            parts.append(f"{column} = 「{value}」")
        elif op == "ne":
            parts.append(f"{column} <> 「{value}」")
        elif op == "in" and isinstance(value, list):
            parts.append(f"{column} 属于 「{'、'.join(str(item_value) for item_value in value)}」")
        elif op == "is_null":
            parts.append(f"{column} 为空")
        elif op == "range" and isinstance(value, dict):
            parts.append(f"{column} 从 「{value.get('start') or ''}」到 「{value.get('end') or ''}」")
    return "、".join(parts) or "没有条件"


def _database(item: dict, check: dict | None, allowed: list[str]) -> str:
    for source in (item, check or {}):
        if not isinstance(source, dict):
            continue
        name = source.get("database")
        if isinstance(name, str) and name:
            if name not in allowed or IDENT.fullmatch(name) is None:
                return ""
            return name
    if len(allowed) == 1 and isinstance(allowed[0], str) and IDENT.fullmatch(allowed[0]):
        return allowed[0]
    return ""


def _live_table(live, planned: str) -> tuple[str, list[str]]:
    if isinstance(live, dict):
        table = live.get("table") if isinstance(live.get("table"), str) and live.get("table") else planned
        columns = live.get("columns")
        if isinstance(columns, list):
            return table, [column for column in columns if isinstance(column, str) and column]
        return table, []
    if isinstance(live, list):
        return planned, [column for column in live if isinstance(column, str) and column]
    return planned, []


def _row_values(rows: list, item: dict, check: dict | None) -> list[str]:
    column = ""
    for clause in item.get("where") or []:
        if isinstance(clause, dict) and clause.get("op") in {"eq", "in", "range"} and clause.get("column"):
            column = str(clause["column"])
            break
    if not column:
        for extra in _check_columns(check):
            column = extra
            break
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not column:
            continue
        raw = row.get(column)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text not in values:
            values.append(text)
        if len(values) == ROW_LIMIT:
            break
    return values


def _remember_values(test_values: dict, item: dict, values: list) -> None:
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        test_values[item_id] = list(values)
    check_id = item.get("check_id")
    if isinstance(check_id, str) and check_id:
        test_values[check_id] = list(values)


def _read_baseline(run_dir: Path) -> dict | None:
    path = run_dir / "baseline.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _split_cases(plan: dict, document: dict) -> tuple[list[str], list[tuple[str, str]]]:
    items = document.get("items") if isinstance(document.get("items"), dict) else {}
    ready = []
    blocked = []
    for case in plan.get("cases") or []:
        if not isinstance(case, dict) or not case.get("id"):
            continue
        case_id = str(case["id"])
        item = items.get(case.get("data_plan_id"))
        if isinstance(item, dict) and item.get("status") == "ready" and _has_steps(case):
            ready.append(case_id)
            continue
        reason = item.get("reason") if isinstance(item, dict) else ""
        if isinstance(item, dict) and item.get("status") == "ready":
            reason = f"用例「{case_id}」没有可执行步骤"
        if not isinstance(reason, str) or not reason.strip():
            reason = "没有这条数据计划"
        blocked.append((case_id, reason.strip()))
    return ready, blocked


def _write_blocked(run_dir: Path, blocked: list[tuple[str, str]]) -> None:
    plan = {}
    from qa_platform.runner.baseline import _load_plan

    loaded = _load_plan(run_dir)
    if isinstance(loaded, dict):
        plan = loaded
    cases = {
        str(case.get("id")): case
        for case in (plan.get("cases") or [])
        if isinstance(case, dict) and case.get("id")
    }
    for case_id, reason in blocked:
        folder = run_dir / "cases" / case_id
        path = folder / "case.json"
        existing = {}
        if path.is_file() and not path.is_symlink():
            try:
                loaded_case = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded_case = None
            if isinstance(loaded_case, dict):
                existing = loaded_case
        if existing.get("status") == "passed":
            continue
        case = cases.get(case_id) or {}
        steps = [step for step in (case.get("steps") or []) if isinstance(step, dict)]
        write_json(path, {
            "id": case_id,
            "case_id": case_id,
            "title": case.get("title") or existing.get("title") or "",
            "goal": case.get("goal") or existing.get("goal") or "",
            "start": case.get("start") if isinstance(case.get("start"), str) else existing.get("start") or "",
            "expect_text": case.get("expect_text") if case.get("expect_text") is not None else existing.get("expect_text"),
            "check_id": case.get("check_id"),
            "acceptance_id": case.get("acceptance_id") or "",
            "data_plan_id": case.get("data_plan_id") or "",
            "status": "blocked",
            "pending": None,
            "steps": steps,
            "rounds": existing.get("rounds") or [],
            "seen": existing.get("seen") or "",
            "observed": False,
            "blocked_reason": reason,
            "filled_values": [],
            "result_reason": reason,
            "observation": None,
            "sealed": False,
        })
