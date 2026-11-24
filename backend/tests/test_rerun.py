import json
from base64 import b64decode

from fastapi.testclient import TestClient

from qa_platform.api import create_app
from qa_platform.executor import ExecutorSlot
from qa_platform.runner.report import console_png
from qa_platform.runner.rerun import accept_rerun
from qa_platform.store import review_digest, write_json
from tests.conftest import auth

OLD = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
NEW = console_png(["新截图"])
BASE = "https://omstest.example"
DB = "oms3_onlinepublish_biz"
SQL1 = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-01' LIMIT 50"
SQL2 = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-02' LIMIT 50"
JS = (
    "let app = await cua.getApp('Microsoft Edge'); "
    "await app.getAXState(); await app.query({text: '第一步', role: 'button'}); await app.click(1);"
)
OLD_JS = (
    "let app = await cua.getApp('Microsoft Edge'); "
    "await app.getAXState(); await app.click(9);"
)
RUN = "run-abc123abc123"
CONFIRMED = [{"detail_id": "DETAIL-01", "status": "待退款"}]


def step(name, expect):
    return {
        "index": 0 if name == "第一步" else 1,
        "phase": "test",
        "action": "click",
        "role": "button",
        "name": name,
        "locator_status": "ready",
        "effect": "read",
        "expect_text": expect,
    }


def build(root, *, status="passed", pending=None, steps=None, exit_code=0):
    run = root / "runs" / RUN
    steps = steps if steps is not None else [step("第一步", ["待退款"])]
    baseline = {
        "strategy": "reuse",
        "select_sql": {"K1": SQL1, "K2": SQL2},
        "fixtures": {
            "K1": {"database": DB, "pk": {"detail_id": "DETAIL-01"}},
            "K2": {"database": DB, "pk": {"detail_id": "DETAIL-02"}},
        },
        "records": {"K1": CONFIRMED, "K2": [{"detail_id": "DETAIL-02", "status": "待退款"}]},
    }
    plan = {
        "ui": {
            "seed_object_count": 0,
            "pages": [{"id": "refund", "path": "/refund", "title_text": "确认退款", "controls": []}],
        },
        "cases": [
            {"id": "A", "title": "确认退款", "check_id": "K1", "steps": steps},
            {"id": "B", "title": "下一条", "check_id": "K2", "steps": [step("旁路", ["其它"])]},
        ],
        "checks": [
            {"id": "K1", "kind": "table", "table": "refunds", "pk": ["detail_id"], "state_field": "status", "source": "Refund.java:1"},
            {"id": "K2", "kind": "table", "table": "refunds", "pk": ["detail_id"], "state_field": "status", "source": "Refund.java:2"},
        ],
        "open": [],
        "source": {"model": "m", "request_id": "1", "requirement_id": "DSDD-123", "commits": [], "files": []},
    }
    payload = json.loads(json.dumps(baseline))
    write_json(run / "plan.json", plan)
    write_json(run / "baseline.json", baseline)
    write_json(run / "state.json", {
        "id": RUN,
        "project_id": "prj-5471cfa8c60a",
        "requirement": {"id": "DSDD-123", "title": "确认退款明细", "source": "api"},
        "environment_host": "shop.example",
        "branch": "hotfix/insta360",
        "commit": "abc123def",
        "base_url": BASE,
        "allowed_databases": [DB],
        "review": None,
        "stop_reason": "",
        "exit_code": exit_code,
        "stage": "execute",
        "events": [],
        "approvals": [
            {"id": "b" * 32, "kind": "baseline", "digest": review_digest("baseline", payload), "payload": payload},
            {"id": "p" * 32, "kind": "plan", "digest": review_digest("plan", plan), "payload": plan},
        ],
    })
    folder = run / "cases" / "A"
    folder.mkdir(parents=True)
    (folder / "shot.png").write_bytes(OLD)
    digest = __import__("hashlib").sha256(OLD).hexdigest()
    write_json(folder / "case.json", {
        "id": "A",
        "case_id": "A",
        "title": "确认退款",
        "status": status,
        "check_id": "K1",
        "attempt": 1,
        "business_pk": {"detail_id": "DETAIL-01"},
        "state_before": "待退款",
        "state_after": "待退款",
        "expect_state": "待退款",
        "pending": pending,
        "sealed": status in {"passed", "failed", "blocked"},
        "seen": "旧结果",
        "blocked_reason": "",
        "result_reason": "",
        "steps": steps,
        "rounds": [{"seen_text": "旧结果", "screenshot": {"file": "cases/A/shot.png", "sha256": digest}}],
        "evidence": [{
            "id": "w1",
            "step_index": 0,
            "kind": "window",
            "file": "cases/A/shot.png",
            "sha256": digest,
            "seen_text": "旧结果",
        }],
    })
    other = run / "cases" / "B"
    other.mkdir(parents=True)
    (other / "shot.png").write_bytes(OLD)
    write_json(other / "case.json", {
        "id": "B",
        "case_id": "B",
        "title": "下一条",
        "status": "failed",
        "check_id": "K2",
        "attempt": 1,
        "pending": None,
        "sealed": True,
        "seen": "旁路旧图",
        "rounds": [{"seen_text": "旁路旧图", "screenshot": {"file": "cases/B/shot.png", "sha256": digest}}],
    })
    return run


def frozen(run):
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    return {
        "requirement": state["requirement"],
        "environment_host": state["environment_host"],
        "branch": state["branch"],
        "commit": state["commit"],
        "base_url": state["base_url"],
    }


def case_bytes(run, case_id):
    return (run / "cases" / case_id / "case.json").read_bytes()


class Session:
    def __init__(self):
        self.calls = []

    def reset(self):
        return None

    def list_apps(self):
        return "Microsoft Edge — com.microsoft.edgemac [running]"

    def get_app_state(self, app):
        return {"content": [{"type": "text", "text": f"地址 {BASE}/refund"}]}

    def run(self, code, app=None):
        self.calls.append(code)
        return {"url": BASE + "/refund", "text": "看到待退款", "screenshot": NEW}


def test_passed_case_reruns_from_the_first_step_and_replaces_only_itself(client, app, var_dir, monkeypatch):
    run = build(var_dir)
    other_case = case_bytes(run, "B")
    other_png = (run / "cases" / "B" / "shot.png").read_bytes()
    old_png = (run / "cases" / "A" / "shot.png").read_bytes()
    plan_bytes = (run / "plan.json").read_bytes()
    header = frozen(run)
    prompts = []
    queries = []
    browsers = []
    session = Session()

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"js": JS, "effect": "read"}
        return {"done": True}

    def query(_run_dir, database, sql, allowed):
        queries.append((database, sql, list(allowed)))
        assert sql == SQL1
        return CONFIRMED

    monkeypatch.setattr("qa_platform.runner.rerun.run_query", query)
    monkeypatch.setattr("qa_platform.runner.rerun.open_session", lambda: session)
    monkeypatch.setattr("qa_platform.runner.rerun.open_model", lambda: model)
    monkeypatch.setattr("qa_platform.runner.rerun.launch_browser", lambda *args: browsers.append(args))
    response = client.post(f"/api/runs/{RUN}/cases/A/rerun", headers=auth(app))
    assert response.status_code == 202
    assert response.json() == {"run": RUN, "case_id": "A", "attempt": 2}
    saved = json.loads(case_bytes(run, "A"))
    assert saved["status"] == "passed"
    assert saved["pending"] is None
    assert saved["seen"] == "看到待退款"
    assert saved["attempt"] == 2
    assert "第一步" in prompts[0]
    assert session.calls == [JS]
    assert queries and queries[0][1] == SQL1
    assert all(sql == SQL1 for _database, sql, _allowed in queries)
    new_file = saved["rounds"][0]["screenshot"]["file"]
    assert new_file == "cases/A/attempts/2/images/round-1.png"
    assert (run / new_file).read_bytes() == NEW
    assert (run / "cases" / "A" / "shot.png").read_bytes() == old_png
    archived = run / "cases" / "A" / "attempts" / "1" / "images" / "shot.png"
    assert archived.read_bytes() == old_png
    assert json.loads((run / "cases" / "A" / "attempt.json").read_text(encoding="utf-8")) == {"attempt": 2}
    assert (run / "cases" / "A" / "attempts" / "1" / "case.json").is_file()
    assert case_bytes(run, "B") == other_case
    assert (run / "cases" / "B" / "shot.png").read_bytes() == other_png
    assert (run / "plan.json").read_bytes() == plan_bytes
    assert frozen(run) == header
    assert browsers == [("Microsoft Edge", BASE + "/refund")]
    assert "Microsoft Edge" in prompts[0]
    assert "已经在这个浏览器里打开" in prompts[0]


def test_changed_baseline_blocks_the_case_without_a_click(tmp_path):
    run = build(tmp_path)
    other = case_bytes(run, "B")
    shot = (run / "cases" / "B" / "shot.png").read_bytes()
    session = Session()
    browsers = []

    def query(_database, sql, _allowed):
        assert sql == SQL1
        return [{"detail_id": "DETAIL-01", "status": "已改"}]

    status, body = accept_rerun(
        run, "A", ExecutorSlot(), run_id=RUN,
        session=session,
        model=lambda _prompt: (_ for _ in ()).throw(AssertionError("model")),
        query=query,
        browser=lambda *args: browsers.append(args),
    )
    assert status == 202
    assert body["attempt"] == 2
    saved = json.loads(case_bytes(run, "A"))
    assert saved["status"] == "blocked"
    assert saved["result_reason"] == "已确认的行已变化"
    assert saved["current_rows"] == [{"detail_id": "DETAIL-01", "status": "已改"}]
    assert session.calls == []
    assert browsers == []
    assert not (run / "cases" / "A" / "attempts" / "2" / "images" / "round-1.png").exists()
    assert (run / "cases" / "A" / "attempts" / "1" / "images" / "shot.png").is_file()
    assert case_bytes(run, "B") == other
    assert (run / "cases" / "B" / "shot.png").read_bytes() == shot


def test_paused_with_pending_only_reobserves(tmp_path):
    pending = {"step_index": 0, "mark": "尚未核对", "expect_state": "已退款", "js": OLD_JS}
    run = build(tmp_path, status="paused", pending=pending, steps=[step("第一步", ["待退款"]), step("第二步", ["完成"])])
    shot = (run / "cases" / "A" / "shot.png").read_bytes()
    session = Session()

    def query(_database, sql, _allowed):
        assert sql == SQL1
        return CONFIRMED

    status, _body = accept_rerun(
        run, "A", ExecutorSlot(), run_id=RUN,
        session=session,
        model=lambda _prompt: (_ for _ in ()).throw(AssertionError("model")),
        query=query,
        browser=lambda *args: (_ for _ in ()).throw(AssertionError("browser")),
    )
    assert status == 202
    saved = json.loads(case_bytes(run, "A"))
    assert saved["status"] == "paused"
    assert saved["pending"]["js"] == OLD_JS
    assert saved["rounds"][0]["screenshot"]["file"] == "cases/A/shot.png"
    assert session.calls == []
    assert (run / "cases" / "A" / "shot.png").read_bytes() == shot
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    assert state["exit_code"] == 3

    other = build(tmp_path / "next", status="paused", pending=pending, steps=[step("第一步", ["待退款"]), step("第二步", ["完成"])])
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return {"done": True}

    matched = Session()
    accept_rerun(
        other, "A", ExecutorSlot(), run_id=RUN,
        session=matched,
        model=model,
        query=lambda _database, _sql, _allowed: [{"detail_id": "DETAIL-01", "status": "已退款"}],
        browser=lambda *args: None,
    )
    assert prompts
    assert "第二步" in prompts[0]
    assert "第一步" not in prompts[0]
    assert matched.calls == []
    resumed = json.loads(case_bytes(other, "A"))
    assert resumed["pending"] is None


def test_running_case_returns_409(client, app, var_dir):
    run = build(var_dir, status="running", pending=None)
    called = []

    def explode(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("query")

    # The route uses run_query only after the running check. Patch it so a click cannot start.
    from qa_platform.runner import rerun as rerun_module
    original = rerun_module.run_query
    rerun_module.run_query = explode
    try:
        response = client.post(f"/api/runs/{RUN}/cases/A/rerun", headers=auth(app))
    finally:
        rerun_module.run_query = original
    assert response.status_code == 409
    assert response.json()["error"] == "这条用例正在执行"
    assert called == []
    assert json.loads(case_bytes(run, "A"))["status"] == "running"

    busy = ExecutorSlot()

    class Alive:
        def poll(self):
            return None

    busy.process = Alive()
    busy.run_id = RUN
    passed = build(var_dir / "other", status="passed")
    status, body = accept_rerun(
        passed, "A", busy, run_id=RUN,
        query=lambda *_args: (_ for _ in ()).throw(AssertionError("query")),
        browser=lambda *_args: (_ for _ in ()).throw(AssertionError("browser")),
    )
    assert status == 409
    assert body["error"] == "已有测试在运行，请等当前运行结束"


def test_exit_code_3_is_paused_after_api_restart(tmp_path):
    var = tmp_path / "var"
    run = build(var, status="running", pending={"step_index": 0, "mark": "尚未核对", "expect_state": "已退款", "js": OLD_JS}, exit_code=3)
    app = create_app(var, tmp_path / "dist")
    with TestClient(app, base_url="http://127.0.0.1:8876") as client:
        response = client.get(f"/api/runs/{RUN}", headers=auth(app))
    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert response.json()["exit_code"] == 3
    saved = json.loads(case_bytes(run, "A"))
    assert saved["pending"]["mark"] == "尚未核对"
    assert saved["status"] == "running"


def test_existing_report_rerenders_only_that_section(tmp_path):
    run = build(tmp_path)
    report = """# 验收报告

- 需求编号：DSDD-123
- 标题：确认退款明细
- 测试环境：shop.example
- 开发分支：hotfix/insta360
- commit：abc123def

## A 确认退款

- 结果：通过
- 标记：旧的A

## B 下一条

- 结果：失败
- 标记：其他用例原节
"""
    (run / "report.md").write_text(report, encoding="utf-8")
    header = report[:report.index("## A")]
    section_b = report[report.index("## B"):]
    before = frozen(run)
    plan_bytes = (run / "plan.json").read_bytes()
    other = case_bytes(run, "B")
    session = Session()

    def model(prompt):
        if not getattr(model, "done", False):
            model.done = True
            assert "第一步" in prompt
            return {"js": JS, "effect": "read"}
        return {"done": True}

    model.done = False
    status, body = accept_rerun(
        run, "A", ExecutorSlot(), run_id=RUN,
        session=session,
        model=model,
        query=lambda _database, sql, _allowed: CONFIRMED,
        browser=lambda *_args: None,
    )
    assert status == 202
    assert body["case_id"] == "A"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert text.startswith(header)
    assert text[text.index("## B"):] == section_b
    assert "旧的A" not in text
    assert "其他用例原节" in text
    assert "cases/A/attempts/2/images/round-1.png" in text
    assert "需求编号：DSDD-123" in text
    assert "标题：确认退款明细" in text
    assert "测试环境：shop.example" in text
    assert "开发分支：hotfix/insta360" in text
    assert "commit：abc123def" in text
    assert frozen(run) == before
    assert (run / "plan.json").read_bytes() == plan_bytes
    assert case_bytes(run, "B") == other
    assert json.loads(case_bytes(run, "B"))["status"] == "failed"
