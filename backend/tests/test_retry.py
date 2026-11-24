import json
from pathlib import Path

from qa_platform.runner.main import main
from qa_platform.runner.retry import prepare_retry
from qa_platform.store import ApprovalError, review_digest
from tests.conftest import auth

RUN = "run-abc123abc123"
DB = "bfrs_uat"


def _plan() -> dict:
    return {
        "ui": {"seed_object_count": 0, "pages": [{"id": "bill", "path": "/bill/alipay"}]},
        "cases": [],
        "checks": [],
        "open": [],
        "source": {"model": "m", "request_id": "1", "requirement_id": "WXLC-2731", "commits": [], "files": []},
    }


def test_retry_from_execute_keeps_the_plan_and_requirement(tmp_path, monkeypatch):
    run = tmp_path / RUN
    run.mkdir()
    plan = _plan()
    (run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (run / "requirement.txt").write_text("需求正文\n", encoding="utf-8")
    before_plan = (run / "plan.json").read_bytes()
    before_requirement = (run / "requirement.txt").read_bytes()
    baseline = {"strategy": "reuse", "records": {"K1": [{"trade_id": "1"}]}}
    (run / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "project_id": "prj-1",
        "commit": "abc",
        "base_url": "http://bfrs-uat.test.jeoms.com",
        "allowed_databases": [DB],
        "exit_code": 2,
        "stop_reason": "environment",
        "review": None,
        "requirement": {"id": "WXLC-2731", "title": "账单", "source": "api", "body_file": "requirement.txt"},
        "approvals": [
            {"kind": "plan", "digest": review_digest("plan", plan), "payload": plan},
            {"kind": "baseline", "digest": review_digest("baseline", baseline), "payload": baseline},
        ],
        "events": [
            {"time": "2026-11-17T15:42:09+08:00", "status": "阻塞", "step": "执行", "basis": "没有可以打开的测试页"},
        ],
    }), encoding="utf-8")
    case = run / "cases" / "C1"
    case.mkdir(parents=True)
    (case / "case.json").write_text(json.dumps({"id": "C1", "status": "blocked", "blocked_reason": "环境不在 base_url"}), encoding="utf-8")

    assert prepare_retry(run) == "执行"
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    assert state["exit_code"] is None
    assert state["stop_reason"] == ""
    assert state["commit"] == "abc"
    assert (run / "plan.json").read_bytes() == before_plan
    assert (run / "requirement.txt").read_bytes() == before_requirement
    assert "从「执行」重试，不重新开始" in state["events"][-1]["basis"]
    saved_case = json.loads((case / "case.json").read_text(encoding="utf-8"))
    assert saved_case["status"] == "waiting"

    called = []
    monkeypatch.setattr("qa_platform.runner.main.read_secrets", lambda: ("key", "token"))
    monkeypatch.setattr("qa_platform.runner.ocu.ProductSession", lambda: type("Session", (), {"close": lambda self: None})())
    monkeypatch.setattr("qa_platform.runner.main.fetch_requirement", lambda *args, **kwargs: called.append("requirement"))
    monkeypatch.setattr("qa_platform.runner.main.execute", lambda *args, **kwargs: called.append("git"))
    monkeypatch.setattr("qa_platform.runner.main.execute_reuse", lambda *args, **kwargs: called.append("execute") or 0)
    monkeypatch.setattr("qa_platform.runner.main.finish_observed", lambda run_dir, code: called.append(("finish", code)) or code)
    assert main(["--out", str(run)]) == 0
    assert called == ["execute", ("finish", 0)]


def test_manual_retry_from_plan_keeps_requirement_and_commit(tmp_path):
    run = tmp_path / RUN
    run.mkdir()
    plan = _plan()
    (run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    baseline = {"strategy": "reuse", "records": {}}
    (run / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "commit": "abc",
        "exit_code": 0,
        "stop_reason": "",
        "review": None,
        "requirement": {"id": "WXLC-2731", "title": "账单", "source": "api"},
        "approvals": [
            {"kind": "plan", "digest": review_digest("plan", plan), "payload": plan},
            {"kind": "baseline", "digest": review_digest("baseline", baseline), "payload": baseline},
        ],
        "events": [{"status": "完成", "step": "执行", "basis": "观察已记下"}],
    }), encoding="utf-8")
    assert prepare_retry(run, "方案", "edge") == "方案"
    assert json.loads((run / "state.json").read_text(encoding="utf-8"))["product_browser"] == "edge"
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    assert state["commit"] == "abc"
    assert state["requirement"]["id"] == "WXLC-2731"
    assert state["exit_code"] is None
    assert [item["kind"] for item in state["approvals"]] == []
    assert not (run / "baseline.json").exists()
    assert (run / "plan.json").is_file()


def test_manual_retry_refuses_the_step_that_is_waiting(tmp_path):
    run = tmp_path / RUN
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "exit_code": 4,
        "review": {"kind": "plan", "step": "方案"},
        "events": [],
    }), encoding="utf-8")
    try:
        prepare_retry(run, "方案")
    except ApprovalError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("待确认的步骤却允许重试")


def test_continue_after_twenty_rounds_writes_the_marker(client, app):
    var = app.state.var_dir
    run = var / "runs" / RUN
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "round_prompt": {"rounds": 20, "question": "已进行 20 轮，是否停止这次执行？"},
        "events": [],
    }), encoding="utf-8")
    refused = client.post(f"/api/runs/{RUN}/continue", headers=auth(app), json={})
    assert refused.status_code == 409

    class Alive:
        pid = None

        def poll(self):
            return None

    slot = app.state.executor
    slot.process = Alive()
    slot.run_id = RUN
    shown = client.get(f"/api/runs/{RUN}", headers=auth(app))
    assert shown.json()["round_prompt"]["question"] == "已进行 20 轮，是否停止这次执行？"
    accepted = client.post(f"/api/runs/{RUN}/continue", headers=auth(app), json={})
    assert accepted.status_code == 200
    assert (run / "continue.requested").read_text(encoding="utf-8") == "1"
    slot.process = None
    slot.run_id = None


def test_retry_api_relaunches_the_same_run(client, app, monkeypatch):
    var = app.state.var_dir
    run = var / "runs" / RUN
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({
        "id": RUN,
        "project_id": "prj-1",
        "exit_code": 2,
        "stop_reason": "",
        "review": None,
        "events": [{"status": "阻塞", "step": "基线", "basis": "没有查到行"}],
    }), encoding="utf-8")
    baseline = run / "baseline.json"
    baseline.write_text(json.dumps({"strategy": "none"}), encoding="utf-8")
    launched = []
    monkeypatch.setattr("qa_platform.api.launch", lambda slot, path, project_id: launched.append((path.name, project_id)))
    response = client.post(f"/api/runs/{RUN}/retry", headers=auth(app), json={})
    assert response.status_code == 202
    assert response.json()["step"] == "基线"
    assert launched == [(RUN, "prj-1")]
    assert not baseline.exists()
    assert json.loads((run / "state.json").read_text(encoding="utf-8"))["exit_code"] is None
