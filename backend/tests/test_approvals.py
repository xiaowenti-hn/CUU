import hashlib
import json
import shutil
import subprocess

from qa_platform.store import find_approval, review_digest
from tests.conftest import auth

RUN_ID = "run-abc123abc123"


def plan_document() -> dict:
    return {
        "ui": {
            "seed_object_count": 1,
            "pages": [{
                "id": "refund-confirm",
                "path": "/order/refund",
                "title_text": "确认退款",
                "controls": [{"role": "button", "name": "提交退款确认", "locator_status": "ready"}],
            }],
        },
        "cases": [{
            "id": "C1",
            "title": "确认退款",
            "check_id": "K1",
            "pk_columns": ["detail_id"],
            "steps": [{
                "phase": "test",
                "action": "click",
                "role": "button",
                "name": "提交退款确认",
                "locator_status": "ready",
                "effect": "commit",
            }],
        }],
        "checks": [{
            "id": "K1",
            "kind": "table",
            "table": "refunds",
            "pk": ["detail_id"],
            "state_field": "status",
            "source": "RefundService.java:4",
        }],
        "open": [
            {"id": "O1", "topic": "提示文案", "needs_decision": True, "decision": ""},
            {"id": "O2", "topic": "只是备注", "needs_decision": False, "decision": ""},
        ],
        "source": {
            "model": "deepseek-flash",
            "request_id": "chatcmpl-1",
            "requirement_id": "DSDD-123",
            "commits": ["a" * 40],
            "files": ["RefundService.java"],
        },
    }


def review(kind: str, payload: dict, review_id: str = "ab" * 16) -> dict:
    return {
        "id": review_id,
        "digest": review_digest(kind, payload),
        "kind": kind,
        "step": "方案",
        "title": "确认界面和用例" if kind == "plan" else "没有找到包含需求号的提交",
        "payload": payload,
        "requested_at": "2026-01-01T00:00:00+08:00",
    }


def write_run(var, state: dict, plan: dict | None = None):
    run = var / "runs" / RUN_ID
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    (run / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if plan is not None:
        (run / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return run


def load(run):
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def post(client, app, body: dict):
    return client.post(
        f"/api/runs/{RUN_ID}/approvals",
        headers=auth(app),
        json=body,
    )


def test_digest_keeps_chinese():
    payload = {"requirement_id": "确认退款", "topic": "提示文案"}
    raw = json.dumps(
        {"kind": "commits_empty", "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    escaped = json.dumps(
        {"kind": "commits_empty", "payload": payload},
        sort_keys=True,
    ).encode()
    digest = review_digest("commits_empty", payload)
    assert digest == hashlib.sha256(raw).hexdigest()
    assert digest != hashlib.sha256(escaped).hexdigest()
    assert "\\u" not in raw.decode()


def test_stale_review_id_is_409(client, app):
    document = plan_document()
    pending = review("plan", document)
    run = write_run(app.state.var_dir, {
        "id": RUN_ID,
        "stage": "plan",
        "exit_code": 4,
        "review": pending,
        "approvals": [],
        "events": [],
    }, document)
    state_bytes = (run / "state.json").read_bytes()
    plan_bytes = (run / "plan.json").read_bytes()
    response = post(client, app, {
        "review_id": "ff" * 16,
        "open_decisions": {"O1": "按后端文案验收"},
    })
    assert response.status_code == 409
    assert response.json()["error"] == "确认已失效"
    assert (run / "state.json").read_bytes() == state_bytes
    assert (run / "plan.json").read_bytes() == plan_bytes


def test_missing_open_decision_is_400_and_plan_unchanged(client, app):
    document = plan_document()
    pending = review("plan", document)
    run = write_run(app.state.var_dir, {
        "id": RUN_ID,
        "stage": "plan",
        "exit_code": 4,
        "review": pending,
        "approvals": [],
        "events": [],
    }, document)
    state_bytes = (run / "state.json").read_bytes()
    plan_bytes = (run / "plan.json").read_bytes()
    missing = post(client, app, {"review_id": pending["id"], "open_decisions": {}})
    assert missing.status_code == 400
    assert (run / "plan.json").read_bytes() == plan_bytes
    assert (run / "state.json").read_bytes() == state_bytes
    blank = post(client, app, {"review_id": pending["id"], "open_decisions": {"O1": "  "}})
    assert blank.status_code == 400
    assert (run / "plan.json").read_bytes() == plan_bytes
    counted = post(client, app, {
        "review_id": pending["id"],
        "open_decisions": {"O1": "按后端文案验收"},
        "seed_object_count": True,
    })
    assert counted.status_code == 400
    assert (run / "plan.json").read_bytes() == plan_bytes
    assert (run / "state.json").read_bytes() == state_bytes


def test_question_without_id_must_be_answered(client, app, monkeypatch):
    monkeypatch.setattr("qa_platform.api.launch", lambda *args, **kwargs: None)
    document = plan_document()
    document["open"] = [
        {"question": "其他账单要不要同样支持批量交易号？", "decision": ""},
        {"id": "O2", "topic": "只是备注", "needs_decision": False, "decision": ""},
    ]
    pending = review("plan", document)
    run = write_run(app.state.var_dir, {
        "id": RUN_ID,
        "stage": "plan",
        "exit_code": 4,
        "review": pending,
        "approvals": [],
        "events": [],
    }, document)
    refused = post(client, app, {"review_id": pending["id"], "open_decisions": {}})
    assert refused.status_code == 400
    assert json.loads((run / "plan.json").read_text(encoding="utf-8"))["open"][0]["decision"] == ""
    accepted = post(client, app, {
        "review_id": pending["id"],
        "open_decisions": {"open-0": "只改亚马逊和快手"},
    })
    assert accepted.status_code == 200
    saved = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert saved["open"][0]["decision"] == "只改亚马逊和快手"
    assert saved["open"][1]["decision"] == ""


def test_plan_approval_digest_matches_saved_plan(client, app, monkeypatch):
    monkeypatch.setattr("qa_platform.api.launch", lambda *args, **kwargs: None)
    document = plan_document()
    pending = review("plan", document)
    run = write_run(app.state.var_dir, {
        "id": RUN_ID,
        "stage": "plan",
        "exit_code": 4,
        "review": pending,
        "approvals": [],
        "events": [],
    }, document)
    response = post(client, app, {
        "review_id": pending["id"],
        "open_decisions": {"O1": "按后端文案验收"},
        "seed_object_count": 2,
    })
    assert response.status_code == 200
    state = load(run)
    saved = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert state["review"] is None
    assert state["stage"] == "plan"
    assert saved["open"][0]["decision"] == "按后端文案验收"
    assert saved["open"][1]["decision"] == ""
    assert saved["ui"]["seed_object_count"] == 2
    assert "确认退款" in (run / "plan.json").read_text(encoding="utf-8")
    approval = state["approvals"][-1]
    assert approval["id"] == pending["id"]
    assert approval["digest"] == review_digest("plan", saved)
    assert find_approval(state, "plan", saved)["digest"] == approval["digest"]
    escaped = json.dumps(
        {"kind": "plan", "payload": saved},
        sort_keys=True,
    ).encode()
    assert approval["digest"] != hashlib.sha256(escaped).hexdigest()


def test_commits_empty_approval_does_not_start_runner(client, app, monkeypatch):
    payload = {"requirement_id": "确认退款", "commits": []}
    pending = review("commits_empty", payload, review_id="cd" * 16)
    run = write_run(app.state.var_dir, {
        "id": RUN_ID,
        "stage": "plan",
        "exit_code": 4,
        "review": pending,
        "approvals": [],
        "events": [],
    })
    calls = []
    monkeypatch.setattr("qa_platform.api.launch", lambda *args, **kwargs: calls.append("launch"))
    monkeypatch.setattr(
        "qa_platform.runner.planner.propose",
        lambda *args, **kwargs: calls.append("propose"),
    )
    monkeypatch.setattr(
        "qa_platform.runner.commits.continue_plan",
        lambda *args, **kwargs: calls.append("continue"),
    )

    def refuse_popen(*args, **kwargs):
        calls.append("popen")
        raise AssertionError("批准空结果启动了进程")

    monkeypatch.setattr(subprocess, "Popen", refuse_popen)
    rejected = post(client, app, {
        "review_id": pending["id"],
        "open_decisions": {"O1": "不能带决定"},
    })
    assert rejected.status_code == 400
    assert load(run)["review"]["kind"] == "commits_empty"
    stale = load(run)
    stale["review"]["digest"] = "0" * 64
    (run / "state.json").write_text(json.dumps(stale, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mismatch = post(client, app, {"review_id": pending["id"]})
    assert mismatch.status_code == 409
    assert load(run)["review"]["digest"] == "0" * 64
    (run / "state.json").write_text(
        json.dumps({
            "id": RUN_ID,
            "stage": "plan",
            "exit_code": 4,
            "review": pending,
            "approvals": [],
            "events": [],
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    response = post(client, app, {"review_id": pending["id"]})
    assert response.status_code == 200
    state = load(run)
    assert state["review"] is None
    assert state["approvals"][-1]["digest"] == review_digest("commits_empty", payload)
    assert calls == []


def test_fixture_and_sql_offer_are_rejected(client, app):
    for kind in ("fixture_keys", "sql_offer", "unknown"):
        payload = {"note": "确认退款"}
        pending = review(kind, payload)
        run = write_run(app.state.var_dir, {
            "id": RUN_ID,
            "stage": "plan",
            "exit_code": 4,
            "review": pending,
            "approvals": [],
            "events": [],
        })
        raw = (run / "state.json").read_bytes()
        response = post(client, app, {"review_id": pending["id"]})
        assert response.status_code == 400
        assert (run / "state.json").read_bytes() == raw
