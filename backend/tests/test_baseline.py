import json

from qa_platform.runner.baseline import _column_sql, _schema_sql, continue_baseline, select_statement
from qa_platform.runner.db import read_only_query
from qa_platform.runner.main import main
from qa_platform.runner.sql_grammar import GrammarError, assert_read_only
from qa_platform.store import review_digest
from tests.conftest import auth

RUN = "run-abc123abc123"
DB = "oms3_onlinepublish_biz"
SQL = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-01' LIMIT 50"


def plan_with(steps, seed_count=1) -> dict:
    return {
        "ui": {"seed_object_count": seed_count, "pages": []},
        "cases": [{
            "id": "C1",
            "title": "确认退款",
            "check_id": "K1",
            "pk_columns": ["detail_id"],
            "steps": steps,
        }],
        "checks": [{
            "id": "K1",
            "kind": "table",
            "table": "refunds",
            "pk": ["detail_id"],
            "state_field": "status",
            "source": "RefundService.java:4",
        }],
        "open": [],
        "source": {
            "model": "deepseek-flash",
            "request_id": "1",
            "requirement_id": "DSDD-123",
            "commits": [],
            "files": ["RefundService.java"],
        },
    }


def seed(count: int, status: str = "ready") -> list[dict]:
    return [
        {
            "phase": "seed",
            "action": "click",
            "role": "button",
            "name": f"铺垫{index}",
            "locator_status": status,
        }
        for index in range(count)
    ]


def write_run(tmp, plan: dict, *, approved: bool, review=None) -> "object":
    run = tmp / RUN
    run.mkdir()
    (run / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run / "requirement.txt").write_text("请核对 'DETAIL-01'\n", encoding="utf-8")
    from qa_platform.runner.baseline import fixture_payload

    payload = fixture_payload(plan, "请核对 'DETAIL-01'\n", [DB])
    approvals = [{
        "id": "p" * 32,
        "kind": "plan",
        "digest": review_digest("plan", plan),
        "payload": plan,
    }]
    if approved:
        approved_payload = {**payload, "fixtures": {
            "K1": {"database": DB, "pk": {"detail_id": "DETAIL-01"}},
        }}
        approvals.append({
            "id": "f" * 32,
            "kind": "fixture_keys",
            "digest": review_digest("fixture_keys", approved_payload),
            "payload": approved_payload,
        })
    state = {
        "id": RUN,
        "stage": "plan",
        "exit_code": 4,
        "commit": "abc123",
        "allowed_databases": [DB],
        "requirement": {
            "id": "DSDD-123",
            "title": "确认退款",
            "source": "api",
            "body_file": "requirement.txt",
        },
        "review": review,
        "approvals": approvals,
        "events": [],
        "stop_reason": "",
    }
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run


def load(run):
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def baseline(run):
    return json.loads((run / "baseline.json").read_text(encoding="utf-8"))


class Result:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def executor_for(stdout: str):
    calls = []

    def executor(argv):
        calls.append(list(argv))
        assert argv[0].endswith("/.grok/skills/query-db/scripts/query.sh")
        assert argv[1:] == [DB, "-e", SQL]
        assert "--apply-once" not in argv
        return Result(stdout)

    return calls, executor


def test_empty_query_output_is_zero_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "qa_platform.runner.db._execute",
        lambda argv: Result("\n"),
    )
    assert read_only_query(DB, "SELECT 1", allowed=[DB], run_dir=tmp_path) == []


def test_select_statement_quotes_identifiers_and_limits_inside():
    sql = select_statement(
        {"table": "refunds", "pk": ["detail_id"], "state_field": "status"},
        {"pk": {"detail_id": "DETAIL-01"}},
    )
    assert sql == SQL
    assert sql.count("LIMIT") == 1


def test_quoted_or_backslash_pk_does_not_write_select_sql(client, app, tmp_path):
    plan = plan_with([])
    payload_checks = {
        "checks": [{
            "id": "K1",
            "table": "refunds",
            "pk": ["detail_id"],
            "state_field": "status",
            "source": "RefundService.java:4",
            "allowed_databases": [DB],
            "suggestions": ["DETAIL-01"],
        }]
    }
    pending = {
        "id": "ab" * 16,
        "digest": review_digest("fixture_keys", payload_checks),
        "kind": "fixture_keys",
        "step": "主键",
        "title": "填写主键",
        "payload": payload_checks,
        "requested_at": "2026-01-01T00:00:00+08:00",
    }
    run = app.state.var_dir / "runs" / RUN
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "allowed_databases": [DB],
        "review": pending,
        "approvals": [],
        "events": [],
        "exit_code": 4,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    raw = (run / "state.json").read_bytes()
    stale = client.post(
        f"/api/runs/{RUN}/approvals",
        headers=auth(app),
        json={"review_id": "ff" * 16, "fixtures": {"K1": {"database": DB, "pk": {"detail_id": "DETAIL-01"}}}},
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "确认已失效"
    assert (run / "state.json").read_bytes() == raw
    for value in ("O'Hare", "A\\B", "line\nbreak", "tab\there"):
        response = client.post(
            f"/api/runs/{RUN}/approvals",
            headers=auth(app),
            json={"review_id": pending["id"], "fixtures": {"K1": {"database": DB, "pk": {"detail_id": value}}}},
        )
        assert response.status_code == 400
        assert (run / "state.json").read_bytes() == raw
        assert not (run / "baseline.json").exists()
        assert "select_sql" not in (run / "state.json").read_text(encoding="utf-8")
        assert load(run)["review"]["kind"] == "fixture_keys"


def test_all_rows_become_reuse(tmp_path):
    plan = plan_with([])
    run = write_run(tmp_path, plan, approved=True)
    calls, executor = executor_for("detail_id\tstatus\nDETAIL-01\tok\n")
    assert continue_baseline(run, executor=executor) == 4
    saved = baseline(run)
    state = load(run)
    assert saved["strategy"] == "reuse"
    assert saved["select_sql"]["K1"] == SQL
    assert state["review"]["kind"] == "baseline"
    assert state["review"]["digest"] == review_digest("baseline", state["review"]["payload"])
    assert calls and "--apply-once" not in calls[0]


def test_zero_seed_and_empty_rows_are_none(tmp_path):
    plan = plan_with([], seed_count=2)
    run = write_run(tmp_path, plan, approved=True)
    _calls, executor = executor_for("detail_id\tstatus\n")
    assert continue_baseline(run, executor=executor) == 2
    saved = baseline(run)
    state = load(run)
    assert saved["strategy"] == "none"
    assert state["exit_code"] == 2
    assert state["review"] is None
    assert state["stop_reason"] == "baseline"


def test_one_ready_seed_and_empty_rows_are_page_seed_without_click(tmp_path, monkeypatch):
    plan = plan_with(seed(1), seed_count=1)
    run = write_run(tmp_path, plan, approved=True)
    calls, executor = executor_for("detail_id\tstatus\n")
    clicks = []
    monkeypatch.setattr("qa_platform.runner.baseline.record", __import__(
        "qa_platform.runner.preflight", fromlist=["record"]
    ).record)
    assert continue_baseline(run, executor=executor) == 2
    saved = baseline(run)
    state = load(run)
    assert saved["strategy"] == "none"
    assert state["exit_code"] == 2
    assert state["review"] is None
    assert "数据库只能查询" in state["events"][-1]["basis"]
    assert clicks == []
    assert all("--apply-once" not in argv for argv in calls)
    assert "click" not in json.dumps(calls)


def test_many_seed_steps_and_empty_rows_are_sql_offer_without_apply(tmp_path):
    plan = plan_with(seed(7), seed_count=1)
    run = write_run(tmp_path, plan, approved=True)
    calls, executor = executor_for("detail_id\tstatus\n")
    assert continue_baseline(run, executor=executor) == 2
    saved = baseline(run)
    state = load(run)
    assert saved["strategy"] == "none"
    assert state["exit_code"] == 2
    assert state["review"] is None
    assert "数据库只能查询" in state["events"][-1]["basis"]
    assert all("--apply-once" not in argv for argv in calls)
    assert "UPDATE" not in json.dumps(saved)
    assert "INSERT" not in json.dumps(saved["select_sql"])


def test_pending_seed_beats_large_object_count(tmp_path):
    plan = plan_with(seed(7, "pending"), seed_count=3)
    run = write_run(tmp_path, plan, approved=True)
    _calls, executor = executor_for("detail_id\tstatus\n")
    assert continue_baseline(run, executor=executor) == 2
    assert baseline(run)["strategy"] == "none"


def test_schema_comments_choose_the_query_without_asking(tmp_path, monkeypatch):
    plan = plan_with([{
        "phase": "test",
        "action": "type",
        "role": "textbox",
        "name": "交易号",
        "value": "202406250001,202406250002,202406250003,202406250004",
        "locator_status": "ready",
    }])
    plan["checks"][0]["table"] = "alipay_bill"
    plan["checks"][0]["pk"] = ["alipay_bill_id"]
    plan["checks"][0]["state_field"] = "match"
    run = write_run(tmp_path, plan, approved=False)
    calls = []

    def execute(argv):
        calls.append(argv[-1])
        sql = argv[-1]
        if "information_schema.TABLES" in sql:
            return Result("TABLE_NAME\tTABLE_COMMENT\nbfrs_alipay_bill\t支付宝原始账单\nbfrs_alipay_bill_251105\t\n")
        if "information_schema.COLUMNS" in sql:
            return Result(
                "COLUMN_NAME\tCOLUMN_COMMENT\tCOLUMN_KEY\n"
                "alipay_bill_id\tid\tPRI\n"
                "trade_id\t交易号\tMUL\n"
                "match\t是否匹配\t\n"
            )
        return Result("trade_id\nREAL-1\n")

    monkeypatch.setattr("qa_platform.runner.db._execute", execute)
    monkeypatch.setattr("qa_platform.runner.preflight.execute", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("git")))
    assert main(["--out", str(run)]) == 4
    state = load(run)
    assert state["review"]["kind"] == "baseline"
    sql = baseline(run)["select_sql"]["K1"]
    assert sql == (
        "SELECT `trade_id` FROM `bfrs_alipay_bill` "
        "WHERE `trade_id` IS NOT NULL AND `trade_id` <> '' LIMIT 500"
    )
    assert baseline(run)["test_values"]["K1"] == ["REAL-1"]
    assert "202406250001" not in sql
    assert state.get("review", {}).get("kind") != "fixture_keys"
    assert any("information_schema" in item for item in calls)


def test_missing_table_does_not_stop_the_other_checks(tmp_path, monkeypatch):
    plan = plan_with([{
        "phase": "test",
        "action": "type",
        "role": "textbox",
        "name": "交易号",
        "value": "T1,T2",
        "locator_status": "ready",
    }])
    plan["checks"].append({
        "id": "K2",
        "kind": "table",
        "table": "amazon_date_range_report",
        "pk": ["amazon_date_range_report_id"],
        "state_field": "transaction_id",
        "source": "Missing.java:1",
    })
    plan["cases"].append({
        "id": "C2",
        "title": "亚马逊",
        "check_id": "K2",
        "pk_columns": ["amazon_date_range_report_id"],
        "steps": plan["cases"][0]["steps"],
    })
    run = write_run(tmp_path, plan, approved=False)

    def execute(argv):
        sql = argv[-1]
        if "amazon_date_range_report" in sql and "information_schema.TABLES" in sql:
            return Result("TABLE_NAME\tTABLE_COMMENT\n")
        if "information_schema.TABLES" in sql:
            return Result("TABLE_NAME\tTABLE_COMMENT\nbfrs_refunds\t退款\n")
        if "information_schema.COLUMNS" in sql:
            return Result("COLUMN_NAME\tCOLUMN_COMMENT\tCOLUMN_KEY\ntrade_id\t交易号\tMUL\n")
        return Result("trade_id\nT1\n")

    monkeypatch.setattr("qa_platform.runner.db._execute", execute)
    assert continue_baseline(run) == 4
    state = load(run)
    assert state["review"]["kind"] == "baseline"
    assert "amazon_date_range_report" in state["events"][-1]["basis"]
    assert "K2" not in baseline(run)["select_sql"]
    assert "bfrs_refunds" in baseline(run)["select_sql"]["K1"]


def test_column_match_uses_the_written_label():
    from qa_platform.runner.baseline import match_column, tables_in_code

    rows = [
        {"COLUMN_NAME": "trade_id", "COLUMN_COMMENT": ""},
        {"COLUMN_NAME": "order_no", "COLUMN_COMMENT": "订单号"},
    ]
    assert match_column(rows, "订单号")[0] == "order_no"
    assert match_column(rows, "交易号") is None
    assert match_column(rows, "order_no")[0] == "order_no"
    assert tables_in_code("class A {}\n") == []
    assert tables_in_code('@Table("refunds")\n@Table("refunds")\n@Table("orders")') == ["refunds", "orders"]


def test_comment_sql_flattens_newlines_and_tabs():
    column_sql = _column_sql("bfrs_uat", "bfrs_dou_yin_bill")
    schema_sql = _schema_sql("bfrs_uat", "bfrs_alipay_bill")
    for sql in (column_sql, schema_sql):
        assert "CHAR(10)" in sql
        assert "CHAR(13)" in sql
        assert "CHAR(9)" in sql
    assert "AS COLUMN_COMMENT" in column_sql
    assert "AS TABLE_COMMENT" in schema_sql


def test_read_only_grammar_and_log_filter(tmp_path, monkeypatch):
    assert_read_only("SELECT `detail_id` FROM `refunds` LIMIT 50")
    assert_read_only("SELECT 'INTO OUTFILE' AS label")
    for sql in (
        "WITH c AS (SELECT 1) UPDATE t SET a = 1",
        "WITH c AS (SELECT 1) INSERT INTO t VALUES (1)",
        "SELECT `id` FROM `t` INTO OUTFILE '/tmp/a'",
        "SELECT `id` FROM `t` FOR UPDATE",
        "SELECT `id` FROM `t` LOCK IN SHARE MODE",
        "SELECT LOAD_FILE('/tmp/a')",
        "SELECT `id` FROM `t` INTO @value",
        "SELECT 1; SELECT 2",
    ):
        try:
            assert_read_only(sql)
        except GrammarError:
            continue
        raise AssertionError(sql)
    monkeypatch.setenv("QA_QUERY_SH", "/tmp/evil.sh")
    seen = []

    def executor(argv):
        seen.append(list(argv))
        return Result("status\nok\n", stderr="password=secret\nfile.pem\nkeep-this\nuse properties\n")

    rows = read_only_query(
        DB,
        "SELECT `status` FROM `refunds` LIMIT 50",
        allowed=[DB],
        run_dir=tmp_path,
        executor=executor,
    )
    assert rows == [{"status": "ok"}]
    assert seen[0][0].endswith("/.grok/skills/query-db/scripts/query.sh")
    assert not seen[0][0].endswith("evil.sh")
    log = (tmp_path / "runner.log").read_text(encoding="utf-8")
    assert "keep-this" in log
    assert "password" not in log
    assert "pem" not in log
    assert "properties" not in log
    mismatched = Result("detail_id\tstatus\nonly-one\n")

    def bad(_argv):
        return mismatched

    try:
        read_only_query(DB, "SELECT 1", allowed=[DB], executor=bad)
    except Exception as exc:
        assert "列数" in str(exc)
    else:
        raise AssertionError("列数不一致却被接受")
