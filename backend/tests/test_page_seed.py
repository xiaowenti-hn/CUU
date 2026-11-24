import base64
import json

from qa_platform.runner.execute import execute_page_seed, execute_reuse, execute_sql_tests
from qa_platform.store import review_digest, write_json

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
BASE = "https://omstest.example"
DB = "oms3_onlinepublish_biz"
SQL1 = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-01' LIMIT 50"
SQL2 = "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-02' LIMIT 50"
SEED_JS = (
    "let app = await cua.getApp('local.qa.platform.browser'); "
    "await app.getAXState(); "
    "await app.query({text: '准备数据', role: 'button'}); "
    "await app.click(3); await app.getAXState();"
)
TEST_JS = (
    "let app = await cua.getApp('Microsoft Edge'); "
    "await app.getAXState(); "
    "await app.query({text: '正式验收', role: 'button'}); "
    "await app.click(4); await app.getAXState();"
)


def steps():
    return [
        {
            "phase": "seed",
            "action": "click",
            "role": "button",
            "name": "准备数据",
            "locator_status": "ready",
            "effect": "commit",
            "expect_text": ["已铺好"],
        },
        {
            "phase": "test",
            "action": "click",
            "role": "button",
            "name": "正式验收",
            "locator_status": "ready",
            "effect": "read",
            "expect_text": ["已退款"],
        },
    ]


def build(tmp, strategy, *, exit_code=4, apply_exit_code=None, review=None, approve_baseline=True, approve_actual=False):
    run = tmp / "run"
    plan = {
        "ui": {
            "seed_object_count": 1,
            "pages": [{
                "id": "refund",
                "path": "/refund",
                "title_text": "确认退款",
                "controls": [{"role": "button", "name": "提交退款确认", "locator_status": "ready"}],
            }],
        },
        "cases": [
            {"id": "C1", "title": "确认退款", "check_id": "K1", "steps": steps()},
            {
                "id": "C2",
                "title": "下一条",
                "check_id": "K1",
                "steps": [{
                    "phase": "test",
                    "action": "click",
                    "role": "button",
                    "name": "返回",
                    "locator_status": "ready",
                    "effect": "read",
                }],
            },
        ],
        "checks": [{
            "id": "K1",
            "kind": "table",
            "table": "refunds",
            "pk": ["detail_id"],
            "state_field": "status",
            "source": "RefundService.java:4",
        }],
        "open": [],
        "source": {"model": "m", "request_id": "1", "requirement_id": "DSDD-123", "commits": [], "files": []},
    }
    baseline = {
        "strategy": strategy,
        "select_sql": {"K1": SQL1, "K2": SQL2},
        "fixtures": {
            "K1": {"database": DB, "pk": {"detail_id": "DETAIL-01"}},
            "K2": {"database": DB, "pk": {"detail_id": "DETAIL-02"}},
        },
        "records": {"K1": [], "K2": []},
    }
    if apply_exit_code is not None:
        baseline["apply_exit_code"] = apply_exit_code
    write_json(run / "plan.json", plan)
    write_json(run / "baseline.json", baseline)
    approvals = []
    if approve_baseline:
        approvals.append({
            "id": "b" * 32,
            "kind": "baseline",
            "digest": review_digest("baseline", baseline),
            "payload": baseline,
        })
    if approve_actual:
        approvals.append({
            "id": "a" * 32,
            "kind": "baseline_actual",
            "digest": review_digest("baseline_actual", baseline),
            "payload": baseline,
        })
    write_json(run / "state.json", {
        "id": "run",
        "base_url": BASE,
        "allowed_databases": [DB],
        "review": review,
        "events": [],
        "exit_code": exit_code,
        "stop_reason": "",
        "approvals": approvals,
    })
    return run


class Session:
    def __init__(self, run, *, require_pending=False):
        self.calls = []
        self.run_dir = run
        self.require_pending = require_pending

    def reset(self):
        return None

    def list_apps(self):
        return "Microsoft Edge — com.microsoft.edgemac [running]"

    def get_app_state(self, app):
        return {"content": [{"type": "text", "text": f"地址 {BASE}/refund"}]}

    def run(self, code, app=None):
        if self.require_pending:
            text = (self.run_dir / "cases" / "C1" / "case.json").read_text(encoding="utf-8")
            assert "尚未核对" in text
        self.calls.append(code)
        return {"url": BASE + "/refund", "text": "看到准备数据", "screenshot": PNG}


def forbid_model(_prompt):
    raise AssertionError("model")


def forbid_query(*_args):
    raise AssertionError("query")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_page_seed_runs_only_seed_then_stops_for_actual(tmp_path):
    run = build(tmp_path, "page_seed")
    prompts = []
    seen = []
    turn = {"n": 0}

    def query(_database, sql, allowed):
        assert allowed == [DB]
        seen.append(sql)
        if len(seen) <= 2:
            return [{"detail_id": "DETAIL-01", "status": "空"}]
        if sql == SQL2:
            return [{"detail_id": "DETAIL-02", "status": "已铺好"}]
        return [{"detail_id": "DETAIL-01", "status": "已铺好"}]

    def model(prompt):
        prompts.append(prompt)
        assert "正式验收" not in prompt
        assert "返回" not in prompt
        assert "准备数据" in prompt
        assert "local.qa.platform.browser" in prompt
        assert BASE in prompt
        turn["n"] += 1
        if turn["n"] == 1:
            return {"js": SEED_JS, "effect": "commit", "expect_state": "已铺好"}
        return {"done": True}

    session = Session(run, require_pending=True)
    browsers = []
    assert execute_page_seed(
        run, session=session, model=model, query=query, browser=lambda *args: browsers.append(args),
    ) == 2
    assert session.calls == []
    assert browsers == []
    assert seen == []
    assert prompts == []
    state = load(run / "state.json")
    assert "数据库只能查询，未做页面铺垫" in state["events"][-1]["basis"]
    assert "actual" not in load(run / "baseline.json")
    assert not (run / "report.md").exists()


def test_unconfirmed_baseline_actual_does_not_start_tests(tmp_path):
    review = {"id": "r" * 32, "kind": "baseline_actual", "payload": {"strategy": "page_seed"}}
    browsers = []

    def browser(*args):
        browsers.append(args)

    seeded = build(tmp_path / "seed", "page_seed", review=review)
    assert execute_page_seed(
        seeded, session=Session(seeded), model=forbid_model, query=forbid_query, browser=browser,
    ) == 4
    assert execute_sql_tests(
        seeded, session=Session(seeded), model=forbid_model, query=forbid_query, browser=browser,
    ) == 4

    waiting = build(
        tmp_path / "sql",
        "sql",
        apply_exit_code=0,
        review={"id": "s" * 32, "kind": "baseline_actual", "payload": {}},
        approve_actual=False,
    )
    assert execute_sql_tests(
        waiting, session=Session(waiting), model=forbid_model, query=forbid_query, browser=browser,
    ) == 4

    unapproved = build(tmp_path / "open", "sql", apply_exit_code=0, review=None, approve_actual=False)
    assert execute_sql_tests(
        unapproved, session=Session(unapproved), model=forbid_model, query=forbid_query, browser=browser,
    ) == 0
    assert browsers == []
    assert load(unapproved / "baseline.json")["strategy"] == "sql"
    assert not (unapproved / "report.md").exists()


def test_sql_runs_test_phase_only_after_commit_and_actual_approval(tmp_path):
    browsers = []

    def browser(*args):
        browsers.append(args)

    offered = build(tmp_path / "offer", "sql_offer", review={"id": "o" * 32, "kind": "sql_offer"})
    before = (offered / "baseline.json").read_text(encoding="utf-8")
    assert execute_sql_tests(
        offered, session=Session(offered), model=forbid_model, query=forbid_query, browser=browser,
    ) == 0
    assert (offered / "baseline.json").read_text(encoding="utf-8") == before
    assert load(offered / "baseline.json")["strategy"] == "sql_offer"

    failed = build(tmp_path / "failed", "sql", apply_exit_code=2, approve_actual=True)
    assert execute_sql_tests(
        failed, session=Session(failed), model=forbid_model, query=forbid_query, browser=browser,
    ) == 0
    assert load(failed / "baseline.json")["apply_exit_code"] == 2

    missing = build(tmp_path / "missing", "sql", apply_exit_code=None, approve_actual=True)
    assert execute_sql_tests(
        missing, session=Session(missing), model=forbid_model, query=forbid_query, browser=browser,
    ) == 0

    ready = build(tmp_path / "ready", "sql", apply_exit_code=0, approve_actual=True)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert "正式验收" in prompt
            assert "准备数据" not in prompt
            return {"js": TEST_JS, "effect": "read"}
        assert "准备数据" not in prompt
        return {"done": True}

    session = Session(ready)
    assert execute_sql_tests(
        ready, session=session, model=model, query=lambda *_args: [], browser=browser,
    ) == 0
    assert browsers == [("Microsoft Edge", BASE + "/refund")]
    assert session.calls == [TEST_JS]
    assert any("正式验收" in prompt for prompt in prompts)
    assert all("准备数据" not in prompt for prompt in prompts)
    assert load(ready / "baseline.json")["strategy"] == "sql"
    assert not (ready / "report.md").exists()


def test_none_does_not_start_the_browser(tmp_path):
    run = build(tmp_path, "none", exit_code=2, approve_baseline=False)
    before_baseline = (run / "baseline.json").read_text(encoding="utf-8")
    before_state = (run / "state.json").read_text(encoding="utf-8")
    browsers = []

    def browser(*args):
        browsers.append(args)

    assert execute_page_seed(
        run, session=Session(run), model=forbid_model, query=forbid_query, browser=browser,
    ) == 2
    assert execute_sql_tests(
        run, session=Session(run), model=forbid_model, query=forbid_query, browser=browser,
    ) == 2
    assert execute_reuse(
        run, session=Session(run), model=forbid_model, query=forbid_query, browser=browser,
    ) == 0
    assert browsers == []
    assert (run / "baseline.json").read_text(encoding="utf-8") == before_baseline
    assert (run / "state.json").read_text(encoding="utf-8") == before_state
    assert load(run / "baseline.json")["strategy"] == "none"
    assert load(run / "state.json")["exit_code"] == 2
    assert load(run / "state.json")["review"] is None
    assert not (run / "report.md").exists()
