import json

import pytest

from qa_platform.runner.data_plan import (
    PAGE_BLOCK,
    SQL_BLOCK,
    assess_items,
    case_value_flags,
    compile_select,
    ensure_assessed,
)
from qa_platform.runner.db import propose_sql
from qa_platform.runner.main import main
from qa_platform.runner.planner import validate_plan
from qa_platform.runner.sql_grammar import assert_read_only
from qa_platform.store import ApprovalError, find_approval, review_digest, write_json

PATH = "BillController.java"
REQUIREMENT = "交易号可以查询"
DB = "bfrs_uat"


def shell() -> dict:
    return {
        "ui": {"seed_object_count": 0, "pages": []},
        "open": [],
        "source": {
            "model": "m",
            "request_id": "1",
            "requirement_id": "R1",
            "commits": [],
            "files": [PATH],
        },
    }


def click(name: str = "查询") -> dict:
    return {
        "phase": "test",
        "action": "click",
        "role": "button",
        "name": name,
        "locator_status": "ready",
    }


def typed(name: str = "交易号") -> dict:
    return {
        "phase": "test",
        "action": "type",
        "role": "textbox",
        "name": name,
        "locator_status": "ready",
        "value": "使用库里已有数据",
    }


def page_plan() -> dict:
    doc = shell()
    doc["data_plan"] = {
        "acceptances": [{"id": "A1", "expect": "页面能按条件查出结果"}],
        "items": [{
            "id": "D1",
            "acceptance_id": "A1",
            "evidence": "page",
            "purpose": "require",
            "prepare": "none",
            "needs": [],
            "blockers": [],
        }],
    }
    doc["cases"] = [{
        "id": "C1",
        "acceptance_id": "A1",
        "data_plan_id": "D1",
        "title": "打开查询",
        "goal": "页面上能看到查询结果",
        "start": "",
        "expect_text": ["查询结果"],
        "steps": [click()],
    }]
    doc["checks"] = []
    return doc


def check(check_id: str = "K1") -> dict:
    return {
        "id": check_id,
        "kind": "table",
        "table": "bills",
        "pk": ["id"],
        "state_field": "status",
        "source": f"{PATH}:12",
    }


def where(column: str = "trade_id", op: str = "eq", value: str = "T1", source: str | None = None) -> dict:
    clause = {
        "column": column,
        "op": op,
        "source": source or "requirement:交易号可以查询",
    }
    if op != "is_null":
        clause["value"] = value
    return clause


def table_item(item_id: str = "D2", **overrides) -> dict:
    item = {
        "id": item_id,
        "acceptance_id": "A1",
        "evidence": "table",
        "purpose": "require",
        "prepare": "reuse",
        "needs": [],
        "blockers": [],
        "table": "bills",
        "check_id": "K1",
        "where": [where()],
    }
    item.update(overrides)
    return item


def case(case_id: str, plan_id: str, steps: list, check_id: str | None = None) -> dict:
    body = {
        "id": case_id,
        "acceptance_id": "A1",
        "data_plan_id": plan_id,
        "title": case_id,
        "goal": "页面上能看到查询结果",
        "start": "",
        "expect_text": ["查询结果"],
        "steps": steps,
    }
    if check_id:
        body["check_id"] = check_id
        body["pk_columns"] = ["id"]
    return body


def mixed_plan(**item_overrides) -> dict:
    doc = shell()
    doc["data_plan"] = {
        "acceptances": [{"id": "A1", "expect": "交易号可以查询"}],
        "items": [
            {
                "id": "D1",
                "acceptance_id": "A1",
                "evidence": "page",
                "purpose": "require",
                "prepare": "none",
                "needs": [],
                "blockers": [],
            },
            table_item(**item_overrides),
        ],
    }
    doc["cases"] = [
        case("C1", "D1", [click()]),
        case("C2", "D2", [typed()], "K1"),
    ]
    doc["checks"] = [check()]
    return doc


def checked(doc: dict, **kwargs) -> list[str]:
    return validate_plan(
        doc,
        allowed_commits=set(),
        material_files={PATH},
        allowed_databases=[DB],
        requirement=REQUIREMENT,
        table_columns={"bills": ["trade_id", "id", "status"]},
        **kwargs,
    )


def approved_run(tmp, plan: dict, allowed: list[str] | None = None):
    run = tmp / "run"
    run.mkdir()
    write_json(run / "plan.json", plan)
    write_json(run / "state.json", {
        "id": "run",
        "stage": "plan",
        "exit_code": 4,
        "review": None,
        "allowed_databases": [DB] if allowed is None else allowed,
        "base_url": "https://bill.example",
        "events": [],
        "stop_reason": "",
        "approvals": [{
            "id": "p" * 32,
            "kind": "plan",
            "digest": review_digest("plan", plan),
            "payload": plan,
        }],
    })
    return run


class Result:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def live_columns(database: str, table: str):
    assert database == DB
    if table != "bills":
        return None
    return {"table": "bills", "columns": ["trade_id", "id", "status"]}


def old_plan() -> dict:
    doc = shell()
    doc["ui"]["seed_object_count"] = 1
    doc["cases"] = [{
        "id": "C1",
        "title": "确认退款",
        "goal": "页面上能看到这一笔已经退款",
        "start": "",
        "expect_text": ["已退款"],
        "check_id": "K1",
        "pk_columns": ["detail_id"],
        "steps": [click("提交退款确认")],
    }]
    doc["checks"] = [{
        "id": "K1",
        "kind": "table",
        "table": "refunds",
        "pk": ["detail_id"],
        "state_field": "status",
        "source": f"{PATH}:4",
    }]
    return doc


def test_live_column_spelling_is_kept():
    doc = mixed_plan()
    doc["checks"][0]["pk"] = ["ID"]
    doc["checks"][0]["state_field"] = "Status"
    doc["cases"][1]["pk_columns"] = ["id"]
    doc["data_plan"]["items"][1]["where"] = [where("Trade_Id")]
    assert checked(doc) == []
    assert doc["checks"][0]["pk"] == ["id"]
    assert doc["checks"][0]["state_field"] == "status"
    assert doc["cases"][1]["pk_columns"] == ["id"]
    assert doc["data_plan"]["items"][1]["where"][0]["column"] == "trade_id"


def test_page_plan_has_no_table_check():
    doc = page_plan()
    assert checked(doc) == []
    assert doc["checks"] == []
    assert "check_id" not in doc["cases"][0]


def test_page_case_with_check_id_is_rejected():
    doc = page_plan()
    doc["cases"][0]["check_id"] = "K1"
    errors = checked(doc)
    assert any("不要写 check_id" in item for item in errors)


def test_old_plan_still_requires_a_check():
    doc = old_plan()
    assert checked(doc) == []
    doc["checks"] = []
    assert any("checks 至少一条" in item for item in checked(doc))


def test_old_approval_does_not_cover_a_changed_plan():
    original = old_plan()
    changed = old_plan()
    changed["data_plan"] = page_plan()["data_plan"]
    assert review_digest("plan", original) != review_digest("plan", changed)
    state = {"approvals": [{
        "kind": "plan",
        "digest": review_digest("plan", original),
        "payload": original,
    }]}
    assert find_approval(state, "plan", original) is not None
    assert find_approval(state, "plan", changed) is None


def test_missing_source_and_unknown_column_are_specific():
    doc = mixed_plan()
    doc["data_plan"]["items"][1]["where"] = [{"column": "trade_id", "op": "eq", "value": "T1"}]
    errors = checked(doc)
    assert any("trade_id" in item and "没有来源" in item for item in errors)

    doc = mixed_plan()
    doc["data_plan"]["items"][1]["where"] = [where("shop_id")]
    errors = checked(doc)
    assert any("shop_id" in item and "不在表 bills" in item for item in errors)


def test_blocker_cannot_be_an_executable_case():
    doc = page_plan()
    doc["data_plan"]["items"][0]["blockers"] = ["不知道前置条件"]
    errors = checked(doc)
    assert any("不能写成可执行用例" in item for item in errors)
    doc["cases"][0]["steps"] = []
    assert checked(doc) == []


def test_compile_select_is_read_only_and_quotes_values():
    item = table_item(where=[
        where("trade_id", "eq", "O'Hare"),
        {"column": "status", "op": "is_null", "source": f"{PATH}:12"},
    ])
    sql = compile_select(
        item,
        check(),
        table="bills",
        columns=["trade_id", "id", "status"],
    )
    assert sql.startswith("SELECT ")
    assert "O''Hare" in sql
    assert "IS NULL" in sql
    assert "LIMIT 50" in sql
    assert "INSERT" not in sql.upper()
    assert "UPDATE" not in sql.upper()
    assert_read_only(sql)


def test_page_plan_skips_the_database(tmp_path):
    run = approved_run(tmp_path, page_plan(), allowed=[])

    def executor(argv):
        raise AssertionError(argv)

    assert ensure_assessed(run, executor=executor) == 0
    saved = json.loads((run / "baseline.json").read_text(encoding="utf-8"))
    assert saved["strategy"] == "data_plan"
    assert saved["select_sql"] == {}
    assert saved["items"]["D1"]["status"] == "ready"
    assert not (run / "cases" / "C1" / "case.json").exists()


def test_missing_table_row_blocks_only_that_case(tmp_path):
    plan = mixed_plan()
    run = approved_run(tmp_path, plan)
    calls = []

    def query(database, sql):
        calls.append(sql)
        return []

    document = assess_items(plan, allowed=[DB], columns_for=live_columns, query=query)
    assert document["strategy"] == "data_plan"
    assert document["items"]["D1"]["status"] == "ready"
    assert document["items"]["D2"]["status"] == "blocked"
    assert "没有查到行" in document["items"]["D2"]["reason"]
    assert "trade_id" in document["items"]["D2"]["reason"]
    assert calls and calls[0].startswith("SELECT ")
    assert "INSERT" not in calls[0].upper()
    write_json(run / "baseline.json", document)
    assert ensure_assessed(run, executor=lambda argv: (_ for _ in ()).throw(AssertionError(argv))) == 0
    blocked = json.loads((run / "cases" / "C2" / "case.json").read_text(encoding="utf-8"))
    assert blocked["status"] == "blocked"
    assert "没有查到行" in blocked["blocked_reason"]
    assert not (run / "cases" / "C1" / "case.json").exists()


def test_existing_rows_are_reused_read_only():
    plan = mixed_plan()
    seen = []

    def query(database, sql):
        seen.append((database, sql))
        assert_read_only(sql)
        return [{"trade_id": "T-9", "id": "15", "status": "ok"}]

    document = assess_items(plan, allowed=[DB], columns_for=live_columns, query=query)
    assert document["items"]["D2"]["status"] == "ready"
    assert document["test_values"]["D2"] == ["T-9"]
    assert document["test_values"]["K1"] == ["T-9"]
    assert seen[0][0] == DB
    assert "UPDATE" not in seen[0][1].upper()
    assert "INSERT" not in seen[0][1].upper()


def test_expected_absence_is_ready_and_existing_rows_block_that_case():
    plan = mixed_plan(purpose="expect_absent")

    def query(database, sql):
        return []

    ready = assess_items(plan, allowed=[DB], columns_for=live_columns, query=query)
    assert ready["items"]["D2"]["status"] == "ready"
    assert ready["test_values"]["D2"] == []
    assert "缺数据" not in ready["items"]["D2"]["reason"]
    values, empty, unqueried = case_value_flags(ready, {"data_plan_id": "D2", "check_id": "K1"})
    assert values == []
    assert empty is False
    assert unqueried is False

    def found(database, sql):
        return [{"trade_id": "T1", "id": "1", "status": "ok"}]

    blocked = assess_items(plan, allowed=[DB], columns_for=live_columns, query=found)
    assert blocked["items"]["D2"]["status"] == "blocked"
    assert "预期没有匹配行" in blocked["items"]["D2"]["reason"]
    assert "D2" not in blocked["test_values"]


def test_runtime_unknown_column_or_table_blocks_without_a_data_query():
    plan = mixed_plan()
    plan["data_plan"]["items"][1]["where"] = [where("shop_id")]
    calls = []

    def columns(database, table):
        return {"table": "bills", "columns": ["trade_id", "id", "status"]}

    def query(database, sql):
        calls.append(sql)
        return []

    document = assess_items(plan, allowed=[DB], columns_for=columns, query=query)
    assert document["items"]["D2"]["status"] == "blocked"
    assert "shop_id" in document["items"]["D2"]["reason"]
    assert "不在表 bills" in document["items"]["D2"]["reason"]
    assert calls == []

    def missing(database, table):
        return None

    gone = assess_items(plan, allowed=[DB], columns_for=missing, query=query)
    assert "不存在" in gone["items"]["D2"]["reason"]
    assert calls == []


def test_page_and_sql_prepare_are_still_refused():
    plan = shell()
    plan["data_plan"] = {
        "acceptances": [{"id": "A1", "expect": "交易号可以查询"}],
        "items": [
            table_item("D1", prepare="page", where=[]),
            table_item("D2", prepare="sql", check_id="K2", where=[]),
        ],
    }
    plan["cases"] = [
        case("C1", "D1", [click()], "K1"),
        case("C2", "D2", [click("保存")], "K2"),
    ]
    plan["checks"] = [check("K1"), check("K2")]
    plan["checks"][1]["id"] = "K2"
    calls = []
    document = assess_items(
        plan, allowed=[DB], columns_for=live_columns,
        query=lambda database, sql: calls.append(sql),
    )
    assert document["items"]["D1"]["reason"] == PAGE_BLOCK
    assert document["items"]["D2"]["reason"] == SQL_BLOCK
    assert calls == []
    assert "INSERT" not in json.dumps(document)
    with pytest.raises(ApprovalError) as caught:
        propose_sql(plan, {"sql": "UPDATE bills SET status = 'x'"})
    assert caught.value.status == 409
    assert str(caught.value) == "数据库只能查询，不能改数"


def test_expect_absent_does_not_skip_as_empty(tmp_path, monkeypatch):
    from qa_platform.runner.execute import _run_case

    captured = {}

    def fake(*args, **kwargs):
        captured["empty"] = kwargs["empty_data"]
        captured["unqueried"] = kwargs["unqueried"]
        return 0

    monkeypatch.setattr("qa_platform.runner.execute._think_and_act", fake)
    run = tmp_path / "run"
    run.mkdir()
    write_json(run / "state.json", {"events": [], "base_url": "https://bill.example"})
    write_json(run / "baseline.json", {
        "strategy": "data_plan",
        "items": {"D1": {"purpose": "expect_absent", "status": "ready"}},
        "test_values": {"D1": [], "K1": []},
    })

    class Hand:
        def query(self, *args, **kwargs):
            return ""

        def click(self, *args, **kwargs):
            return None

        def set_value(self, *args, **kwargs):
            return None

    case_body = {
        "id": "C1",
        "status": "waiting",
        "data_plan_id": "D1",
        "check_id": "K1",
        "steps": [typed()],
    }
    assert _run_case(
        run, {"events": []}, case_body, [], {}, "https://bill.example", Hand(), lambda prompt: {}, None,
    ) == 0
    assert captured == {"empty": False, "unqueried": False}


def test_old_flags_still_treat_an_empty_list_as_missing():
    values, empty, unqueried = case_value_flags({"test_values": {"K1": []}}, {"check_id": "K1"})
    assert values == []
    assert empty is True
    assert unqueried is False
    values, empty, unqueried = case_value_flags({"test_values": {"K2": ["A-1"]}}, {"check_id": "K1"})
    assert values is None
    assert empty is False
    assert unqueried is True


def test_page_plan_does_not_enter_the_old_baseline(tmp_path, monkeypatch):
    run = approved_run(tmp_path, page_plan(), allowed=[])
    called = {"baseline": 0, "execute": 0}

    def baseline(*args, **kwargs):
        called["baseline"] += 1
        raise AssertionError("old baseline")

    monkeypatch.setattr("qa_platform.runner.main.continue_baseline", baseline)
    monkeypatch.setattr("qa_platform.runner.main.read_secrets", lambda: ("k", "y"))
    monkeypatch.setattr("qa_platform.runner.main.finish_observed", lambda run_dir, code: code)

    class Product:
        def close(self):
            return None

    monkeypatch.setattr("qa_platform.runner.ocu.ProductSession", lambda: Product())

    def execute(run_dir, **kwargs):
        called["execute"] += 1
        assert kwargs["session"] is not None
        assert kwargs["model"] is not None
        return 0

    monkeypatch.setattr("qa_platform.runner.execute.execute_data_plan", execute)
    assert main(["--out", str(run)]) == 0
    assert called == {"baseline": 0, "execute": 1}
    saved = json.loads((run / "baseline.json").read_text(encoding="utf-8"))
    assert saved["strategy"] == "data_plan"


def test_old_plan_still_enters_baseline(tmp_path, monkeypatch):
    run = approved_run(tmp_path, old_plan())
    called = {"baseline": 0}

    def baseline(*args, **kwargs):
        called["baseline"] += 1
        return 2

    monkeypatch.setattr("qa_platform.runner.main.continue_baseline", baseline)
    monkeypatch.setattr(
        "qa_platform.runner.ocu.ProductSession",
        lambda: (_ for _ in ()).throw(AssertionError("session")),
    )
    assert main(["--out", str(run)]) == 2
    assert called["baseline"] == 1
    assert not (run / "baseline.json").exists()
