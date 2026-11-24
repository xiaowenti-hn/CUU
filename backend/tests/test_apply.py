import hashlib
import json
import os

from qa_platform.runner.db import continue_apply, sql_digest
from qa_platform.runner.main import main
from qa_platform.store import review_digest
from tests.conftest import auth

RUN = "run-abc123abc123"
DB = "oms3_onlinepublish_biz"
UPDATE = "UPDATE `refunds` SET `status` = '' WHERE `detail_id` = 'DETAIL-01'"
SELECT = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-01' LIMIT 50"


def fields(sql=UPDATE, rows=1):
    return {
        "reason": "确认退款需要一行",
        "sql": sql,
        "parameters": "status 置空，只给人看",
        "database": DB,
        "expected_rows": rows,
        "untouched_reason": "其它行不动",
    }


def baseline_doc(strategy: str) -> dict:
    return {
        "strategy": strategy,
        "select_sql": {"K1": SELECT},
        "fixtures": {"K1": {"database": DB, "pk": {"detail_id": "DETAIL-01"}}},
        "records": {"K1": []},
    }


def write_run(var, *, strategy: str, review=None, extra=None):
    run = var / "runs" / RUN
    if run.exists():
        import shutil
        shutil.rmtree(run)
    run.mkdir(parents=True)
    document = baseline_doc(strategy)
    if extra:
        document.update(extra)
    (run / "baseline.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = {
        "id": RUN,
        "project_id": "prj-abc123abc123",
        "stage": "baseline",
        "exit_code": 4,
        "allowed_databases": [DB],
        "review": review,
        "approvals": [],
        "events": [],
        "stop_reason": "",
    }
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run


def offer_review(strategy_payload=None):
    payload = strategy_payload or {"strategy": "sql_offer"}
    return {
        "id": "ab" * 16,
        "digest": review_digest("sql_offer", payload),
        "kind": "sql_offer",
        "step": "基线",
        "title": "改数门槛",
        "payload": payload,
        "requested_at": "2026-01-01T00:00:00+08:00",
    }


def sql_review(payload):
    return {
        "id": "cd" * 16,
        "digest": review_digest("sql", payload),
        "kind": "sql",
        "step": "改数",
        "title": "确认改数语句",
        "payload": payload,
        "requested_at": "2026-01-01T00:00:00+08:00",
    }


def load(run, name):
    return json.loads((run / name).read_text(encoding="utf-8"))


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def post_sql(client, app, body):
    return client.post(f"/api/runs/{RUN}/approvals".replace("/approvals", "/sql-proposal"), headers=auth(app), json=body)


def test_none_and_page_seed_reject_sql(client, app):
    for strategy in ("none", "page_seed"):
        run = write_run(app.state.var_dir, strategy=strategy, review=offer_review())
        before_state = (run / "state.json").read_bytes()
        before_base = (run / "baseline.json").read_bytes()
        response = post_sql(client, app, fields())
        assert response.status_code == 409
        assert (run / "state.json").read_bytes() == before_state
        assert (run / "baseline.json").read_bytes() == before_base
        assert load(run, "baseline.json")["strategy"] == strategy


def test_update_without_where_keeps_sql_offer(client, app):
    run = write_run(app.state.var_dir, strategy="sql_offer", review=offer_review())
    before_state = (run / "state.json").read_bytes()
    before_base = (run / "baseline.json").read_bytes()
    response = post_sql(client, app, fields("UPDATE `refunds` SET `status` = ''"))
    assert response.status_code == 409
    assert "只能查询" in response.json()["error"]
    assert (run / "state.json").read_bytes() == before_state
    assert (run / "baseline.json").read_bytes() == before_base
    assert load(run, "state.json")["review"]["kind"] == "sql_offer"
    assert load(run, "baseline.json")["strategy"] == "sql_offer"


def test_valid_sql_becomes_review_without_mysql(client, app, monkeypatch):
    run = write_run(app.state.var_dir, strategy="sql_offer", review=offer_review())
    launched = []
    monkeypatch.setattr(
        "qa_platform.api.launch",
        lambda *args, **kwargs: launched.append(kwargs.get("apply_now", False)),
    )
    monkeypatch.setattr(
        "qa_platform.runner.db._execute_group",
        lambda argv: (_ for _ in ()).throw(AssertionError(argv)),
    )
    before_state = (run / "state.json").read_bytes()
    before_base = (run / "baseline.json").read_bytes()
    response = post_sql(client, app, fields())
    assert response.status_code == 409
    assert "只能查询" in response.json()["error"]
    assert (run / "state.json").read_bytes() == before_state
    assert (run / "baseline.json").read_bytes() == before_base
    assert launched == []
    assert not (run / "apply.sql").exists()


def test_apply_now_matching_rows_opens_actual(client, app, monkeypatch):
    payload = fields()
    run = write_run(app.state.var_dir, strategy="sql", review=sql_review(payload))
    calls = []

    def executor(argv):
        calls.append(list(argv))
        if "--apply-once" in argv:
            text = open(argv[argv.index("--sql-file") + 1], encoding="utf-8").read()
            assert text == UPDATE
            assert os.stat(argv[argv.index("--sql-file") + 1]).st_mode & 0o777 == 0o600
            return Result("1\n", 0)
        assert argv[2] == "-e"
        return Result("detail_id\tstatus\nDETAIL-01\tdone\n", 0)

    def fake_launch(slot, run_dir, project_id, apply_now=False):
        data = load(run_dir, "baseline.json")
        assert apply_now is True
        assert data["apply_exit_code"] is None
        assert data["apply_started_at"]
        assert data["apply_sql_digest"] == sql_digest(UPDATE)
        assert data["apply_database"] == DB
        assert data["apply_expect_rows"] == 1
        continue_apply(run_dir, apply_now=True, executor=executor)

    monkeypatch.setattr("qa_platform.api.launch", fake_launch)
    response = client.post(
        f"/api/runs/{RUN}/approvals",
        headers=auth(app),
        json={"review_id": "cd" * 16},
    )
    assert response.status_code == 409
    assert "只能查询" in response.json()["error"]
    saved = load(run, "baseline.json")
    assert saved.get("apply_exit_code") is None
    assert "actual" not in saved
    assert calls == []
    assert not (run / "apply.sql").exists()


def test_row_mismatch_does_not_open_actual(tmp_path):
    payload = fields()
    run = write_run(tmp_path, strategy="sql", review=None, extra={
        "apply_started_at": "2026-01-01T00:00:00+08:00",
        "apply_sql_digest": sql_digest(UPDATE),
        "apply_database": DB,
        "apply_expect_rows": 1,
        "apply_exit_code": None,
    })
    state = load(run, "state.json")
    state["approvals"] = [{
        **sql_review(payload),
        "approved_at": "2026-01-01T00:00:01+08:00",
    }]
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    calls = []

    def executor(argv):
        calls.append(list(argv))
        if "--apply-once" in argv:
            return Result("0\n", 3)
        return Result("detail_id\tstatus\n", 0)

    assert continue_apply(run, apply_now=True, executor=executor) == 2
    saved = load(run, "baseline.json")
    state = load(run, "state.json")
    assert saved["apply_exit_code"] is None
    assert state.get("review") is None
    assert "actual" not in saved
    assert "数据库只能查询，未执行改数" in state["events"][-1]["basis"]
    assert calls == []


def test_second_start_without_apply_now_does_not_execute(tmp_path):
    payload = fields()
    run = write_run(tmp_path, strategy="sql", review=None, extra={
        "apply_started_at": "2026-01-01T00:00:00+08:00",
        "apply_sql_digest": hashlib.sha256(UPDATE.encode()).hexdigest(),
        "apply_database": DB,
        "apply_expect_rows": 1,
        "apply_exit_code": None,
    })
    state = load(run, "state.json")
    state["approvals"] = [{**sql_review(payload), "approved_at": "2026-01-01T00:00:01+08:00"}]
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    calls = []

    def executor(argv):
        calls.append(list(argv))
        if "--apply-once" in argv:
            raise AssertionError("没有 --apply-now 仍在改数")
        return Result("detail_id\tstatus\nDETAIL-01\told\n", 0)

    assert continue_apply(run, apply_now=False, executor=executor) == 2
    saved = load(run, "baseline.json")
    assert saved["apply_exit_code"] is None
    assert saved["strategy"] == "sql"
    assert "actual" not in saved
    assert all("--apply-once" not in argv for argv in calls)
    assert load(run, "state.json").get("review") is None
