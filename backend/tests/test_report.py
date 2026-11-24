import hashlib
import io
import json
import zipfile
from base64 import b64decode

from qa_platform.executor import ExecutorSlot
from qa_platform.runner.report import render_report, seal_run
from qa_platform.store import read_state, run_status, write_json
from tests.conftest import auth

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
RUN_ID = "run-abc123abc123"


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def write_state(run, **extra):
    state = {
        "id": run.name,
        "stage": "execute",
        "exit_code": 0,
        "stop_reason": "",
        "branch": "hotfix/insta360",
        "commit": "abc123def",
        "base_url": "https://omstest.example",
        "environment_host": "shop.example",
        "requirement": {"id": "DSDD-123", "title": "确认退款明细", "source": "api"},
        "review": None,
        "events": [],
        "approvals": [],
    }
    state.update(extra)
    write_json(run / "state.json", state)
    return state


def write_case(run, case_id, *, seen, expect, pk, before, after, expected, status="running", pending=None, reason="", evidence=True):
    folder = run / "cases" / case_id
    folder.mkdir(parents=True, exist_ok=True)
    relative = f"cases/{case_id}/shot.png"
    if evidence:
        (folder / "shot.png").write_bytes(PNG)
    case = {
        "id": case_id,
        "case_id": case_id,
        "title": "确认退款",
        "status": status,
        "check_id": "K1",
        "business_pk": {"detail_id": pk},
        "state_before": before,
        "state_after": after,
        "expect_state": expected,
        "pending": pending,
        "observed": evidence,
        "blocked_reason": reason,
        "result_reason": reason,
        "steps": [{"index": 0, "expect_text": expect, "evidence_ids": ["w1"] if evidence else []}],
        "evidence": [],
    }
    if evidence:
        case["evidence"] = [{
            "id": "w1",
            "step_index": 0,
            "kind": "window",
            "file": relative,
            "sha256": digest(PNG),
            "seen_text": seen,
        }]
    write_json(folder / "case.json", case)
    return case


def load_case(run, case_id):
    return json.loads((run / "cases" / case_id / "case.json").read_text(encoding="utf-8"))


def test_matching_page_text_and_observation_passes(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_case(
        run, "C1",
        seen="确认已退款，成功",
        expect=["已退款", "成功"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
    )
    seal_run(run)
    case = load_case(run, "C1")
    assert case["status"] == "passed"
    assert case["sealed"] is True
    assert case["result_reason"] != "日志无相关记录"
    assert not any(item.get("kind") == "console" for item in case["evidence"])
    assert not (run / "cases" / "C1" / "console.png").exists()


def test_missing_console_blocks_even_when_the_database_matches(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_case(
        run, "C1",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
    )
    (run / "console.log").write_text('{"level":"log","text":"unrelated noise"}\n', encoding="utf-8")
    seal_run(run)
    case = load_case(run, "C1")
    assert case["state_after"] == case["expect_state"] == "已退款"
    assert case["status"] == "blocked"
    assert case["blocked_reason"] == "日志无相关记录"
    assert case["result_reason"] == "日志无相关记录"
    assert case["sealed"] is True
    assert not (run / "cases" / "C1" / "console.png").exists()


def test_relevant_console_lines_become_a_png_and_follow_the_observation(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_case(
        run, "A",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
    )
    write_case(
        run, "B",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-02",
        before="待退款",
        after="待退款",
        expected="已退款",
    )
    (run / "console.log").write_text(
        "\n".join([
            '{"level":"log","text":"noise only"}',
            '{"level":"log","text":"DETAIL-01 done"}',
            '{"level":"log","text":"已退款 extra"}',
        ]) + "\n",
        encoding="utf-8",
    )
    seal_run(run)
    passed = load_case(run, "A")
    failed = load_case(run, "B")
    assert passed["status"] == "passed"
    assert failed["status"] == "failed"
    assert failed["result_reason"] == "观察不符"
    for case_id, case, included, excluded in (
        ("A", passed, (b"DETAIL-01", "已退款".encode()), (b"noise only",)),
        ("B", failed, ("已退款".encode(),), (b"noise only", b"DETAIL-01")),
    ):
        console = next(item for item in case["evidence"] if item.get("kind") == "console")
        assert console["note"] == "可访问性文本未包含预期"
        blob = (run / console["file"]).read_bytes()
        assert blob.startswith(b"\x89PNG\r\n\x1a\n")
        assert console["sha256"] == digest(blob)
        for needle in included:
            assert needle in blob
        for needle in excluded:
            assert needle not in blob
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    section = text.split("## B")[0]
    assert section.index("cases/A/shot.png") < section.index("cases/A/console.png")
    assert "可访问性文本未包含预期" in section
    assert load_case(run, "A")["status"] == "passed"
    assert load_case(run, "B")["status"] == "failed"


def test_final_observation_seals_with_the_window_and_writes_the_report(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    (folder / "round-1.png").write_bytes(PNG)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "支付宝",
        "status": "running",
        "observation": "final",
        "check_id": "K1",
        "expect_text": ["支付宝账单", "交易号", "搜索"],
        "filled_values": ["SO-9"],
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{
            "seen_text": "AXRadioButton 名称「支付宝账单」\n9 AXStaticText 名称「SO-9」",
            "screenshot": {"file": "cases/C1/round-1.png", "sha256": digest(PNG)},
        }],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "passed"
    assert saved["sealed"] is True
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "cases/C1/round-1.png" in text
    assert "通过" in text


def test_final_observation_fails_when_the_number_stays_in_the_field(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "快手",
        "status": "running",
        "observation": "final",
        "expect_text": ["快手资金账单"],
        "filled_values": ["SO-9"],
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{
            "seen_text": "AXRadioButton 名称「快手资金账单」\n3 AXTextField 名称「SO-9」 可以填写",
            "screenshot": None,
        }],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "failed"
    assert "SO-9" in saved["result_reason"]
    assert "录入控件以外" in saved["result_reason"]


def test_final_observation_passes_when_the_typed_value_leaves_the_field(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_json(run / "baseline.json", {"test_values": {"K1": ["SO-9", "SO-10"]}})
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    (folder / "round-1.png").write_bytes(PNG)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "",
        "goal": "查出这一行",
        "status": "running",
        "observation": "final",
        "check_id": "K1",
        "expect_text": ["交易号", "搜索"],
        "filled_values": ["SO-9"],
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{
            "seen_text": "AXStaticText 名称「SO-9」",
            "screenshot": {"file": "cases/C1/round-1.png", "sha256": digest(PNG)},
        }],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "passed"
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "查出这一行" in text
    assert "通过" in text


def test_final_observation_fails_when_this_check_has_no_rows(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_json(run / "baseline.json", {"test_values": {"K1": []}})
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "快手",
        "status": "running",
        "observation": "final",
        "check_id": "K1",
        "expect_text": ["交易号", "搜索"],
        "filled_values": ["166996404164"],
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{"seen_text": "AXStaticText 名称「166996404164」", "screenshot": None}],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "failed"
    assert saved["result_reason"] == "这一条没有库里的数据"
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "原因：这一条没有库里的数据" in text


def test_final_observation_fails_when_expect_is_only_control_names(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "亚马逊",
        "status": "running",
        "observation": "final",
        "expect_text": ["交易号", "搜索"],
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{"seen_text": "名称「交易号」\n名称「搜索」", "screenshot": None}],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "failed"
    assert saved["result_reason"] == "通过条件只写了要操作的控件"


def test_absence_line_and_surface_name_do_not_count_as_a_result():
    from qa_platform.runner.report import judge_final

    case = {
        "goal": "在示例页核对写入的值。",
        "surface_name": "示例",
        "expect_text": ["示例"],
        "filled_values": ["A-1", "A-2"],
        "steps": [
            {"action": "type", "name": "条件", "phase": "test"},
        ],
    }
    seen = "AXRadioButton 名称「示例」\n页面上没有找到「A-1」\nAXStaticText 名称「A-2」"
    outcome, reason = judge_final(case, seen, ["A-1", "A-2"])
    assert outcome == "wait"
    assert "A-1" in reason
    assert "A-2" not in reason

    both = "AXStaticText 名称「A-1」\nAXStaticText 名称「A-2」"
    assert judge_final(case, both, ["A-1", "A-2"]) == ("passed", "")

    only_surface = {
        "goal": "在示例页看一眼。",
        "expect_text": ["示例"],
        "steps": [{"action": "click", "name": "打开", "phase": "test"}],
        "rounds": [],
    }
    assert judge_final(only_surface, "AXRadioButton 名称「示例」", None) == ("failed", "通过条件只写了要操作的控件")


def test_report_counts_prepared_filled_and_seen_values(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_json(run / "baseline.json", {"test_values": {"K1": ["A-1", "A-2", "A-3"]}})
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    (folder / "round-1.png").write_bytes(PNG)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "核对一批数据",
        "status": "running",
        "observation": "final",
        "check_id": "K1",
        "expect_text": ["结果已列出"],
        "filled_values": ["A-1", "A-2"],
        "steps": [
            {"action": "type", "name": "条件", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{
            "seen_text": "AXStaticText 名称「结果已列出」\nAXStaticText 名称「A-1」\n页面上没有找到「A-2」",
            "screenshot": {"file": "cases/C1/round-1.png", "sha256": digest(PNG)},
        }],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "failed"
    assert "A-2" in saved["result_reason"]
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "准备：3 条：A-1、A-2、A-3" in text
    assert "写入：2 条：A-1、A-2" in text
    assert "出现在录入控件以外：1 条：A-1" in text
    assert "未出现：1 条：A-2" in text


def test_page_count_is_the_result_and_example_zero_is_not():
    from qa_platform.runner.report import judge_final

    case = {
        "goal": "查出匹配的行",
        "expect_text": ["查询完成", "共 0 条"],
        "filled_values": ["A-1", "A-2"],
        "steps": [
            {"action": "type", "name": "条件", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
    }
    seen = "\n".join([
        "AXGroup 名称「SQL Server 共 条 的标签」",
        "AXStaticText 名称「共 505 条」",
        "AXTextField 名称「A-1」 可以填写",
    ])
    assert judge_final(case, seen, ["A-1", "A-2"]) == ("passed", "")
    assert judge_final(case, "AXGroup 名称「SQL Server 共 条 的标签」", ["A-1", "A-2"]) == (
        "wait",
        "页面上还没有出现结果条数",
    )

    exact = {
        "expect_text": ["共 1 条"],
        "filled_values": ["SO-9"],
        "steps": [{"action": "type", "name": "条件", "phase": "test"}],
    }
    assert judge_final(exact, "AXStaticText 名称「共 505 条」", ["SO-9"])[0] == "wait"
    assert "共 1 条" in judge_final(exact, "AXStaticText 名称「共 505 条」", ["SO-9"])[1]


def test_report_records_the_page_result_apart_from_the_inputs(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_json(run / "baseline.json", {"test_values": {"K1": ["A-1", "A-2", "A-3"]}})
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    (folder / "round-1.png").write_bytes(PNG)
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "按条件查询",
        "status": "running",
        "observation": "final",
        "check_id": "K1",
        "expect_text": ["结果条数"],
        "filled_values": ["A-1", "A-2"],
        "steps": [
            {"action": "type", "name": "条件", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{
            "seen_text": "AXStaticText 名称「共 505 条」",
            "screenshot": {"file": "cases/C1/round-1.png", "sha256": digest(PNG)},
        }],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "passed"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "准备：3 条：A-1、A-2、A-3" in text
    assert "写入：2 条：A-1、A-2" in text
    assert "页面结果：共 505 条" in text
    assert "未出现" not in text


def test_skipped_case_stays_skipped_and_the_report_names_the_reason(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    reason = "当前部署的页面没有「亚马逊账单」，已跳过"
    write_json(folder / "case.json", {
        "id": "C1",
        "case_id": "C1",
        "title": "亚马逊账单",
        "goal": "在亚马逊账单页按交易号搜索",
        "status": "skipped",
        "sealed": True,
        "result_reason": reason,
        "blocked_reason": reason,
        "steps": [
            {"action": "type", "name": "交易号", "phase": "test"},
            {"action": "click", "name": "搜索", "phase": "test"},
        ],
        "rounds": [{"seen_text": reason, "screenshot": None}],
    })
    seal_run(run)
    saved = load_case(run, "C1")
    assert saved["status"] == "skipped"
    assert saved["result_reason"] == reason
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "跳过" in text
    assert f"原因：{reason}" in text


def test_paused_blocked_and_failed_cases_stay_as_they_are(tmp_path):
    run = tmp_path / "run"
    write_state(run)
    write_case(
        run, "paused",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
        status="paused",
        pending={"step_index": 0, "mark": "尚未核对"},
        reason="尚未核对",
    )
    write_case(
        run, "blocked",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
        status="blocked",
        reason="环境不在 base_url",
    )
    write_case(
        run, "failed",
        seen="页面上没有结果",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="其它",
        expected="已退款",
        status="failed",
        reason="提交结果不符合预期",
    )
    write_case(
        run, "open",
        seen="",
        expect=["已退款"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
        evidence=False,
    )
    (run / "console.log").write_text("", encoding="utf-8")
    before = {
        name: (run / "cases" / name / "case.json").read_bytes()
        for name in ("paused", "blocked", "failed", "open")
    }
    seal_run(run)
    after = {
        name: (run / "cases" / name / "case.json").read_bytes()
        for name in before
    }
    assert after == before
    assert load_case(run, "paused")["status"] == "paused"
    assert load_case(run, "blocked")["status"] == "blocked"
    assert load_case(run, "blocked")["blocked_reason"] == "环境不在 base_url"
    assert load_case(run, "failed")["status"] == "failed"
    assert load_case(run, "failed")["result_reason"] == "提交结果不符合预期"
    assert load_case(run, "open")["status"] == "running"


def test_paused_case_prevents_the_report(tmp_path, monkeypatch):
    run = tmp_path / "run"
    write_state(run)
    write_case(
        run, "C1",
        seen="确认已退款，成功",
        expect=["已退款", "成功"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
    )
    write_case(
        run, "C2",
        seen="确认已退款，成功",
        expect=["已退款", "成功"],
        pk="DETAIL-02",
        before="待退款",
        after="已退款",
        expected="已退款",
        status="paused",
        reason="尚未核对",
    )
    paused_bytes = (run / "cases" / "C2" / "case.json").read_bytes()

    def explode(*_args, **_kwargs):
        raise AssertionError("渲染调用了封口")

    monkeypatch.setattr("qa_platform.runner.report.seal_run", explode)
    assert render_report(run) is None
    assert not (run / "report.md").exists()
    assert load_case(run, "C1")["status"] == "running"
    assert load_case(run, "C2")["status"] == "paused"

    monkeypatch.undo()
    seal_run(run)
    assert load_case(run, "C1")["status"] == "passed"
    assert (run / "cases" / "C2" / "case.json").read_bytes() == paused_bytes
    assert render_report(run) is None
    assert not (run / "report.md").exists()


def test_report_lists_frozen_fields_and_marks_a_changed_screenshot(tmp_path, client, app, var_dir):
    run = var_dir / "runs" / RUN_ID
    write_state(run, id=RUN_ID)
    plan = {"ui": {"seed_object_count": 0, "pages": []}, "cases": [], "checks": [], "open": [], "source": {}}
    write_json(run / "plan.json", plan)
    plan_bytes = (run / "plan.json").read_bytes()
    write_case(
        run, "A",
        seen="确认已退款，成功",
        expect=["已退款", "成功"],
        pk="DETAIL-01",
        before="待退款",
        after="已退款",
        expected="已退款",
    )
    write_case(
        run, "B",
        seen="确认已退款，成功",
        expect=["已退款", "成功"],
        pk="DETAIL-02",
        before="待退款",
        after="待退款",
        expected="已退款",
    )
    (run / "db-before.json").write_text("{}\n", encoding="utf-8")
    (run / "db-after-1.json").write_text("{}\n", encoding="utf-8")
    frozen = read_state(run)
    seal_run(run)
    assert load_case(run, "A")["status"] == "passed"
    assert load_case(run, "B")["status"] == "failed"
    assert render_report(run) == run / "report.md"
    text = (run / "report.md").read_text(encoding="utf-8")
    assert "需求编号：DSDD-123" in text
    assert "标题：确认退款明细" in text
    assert "测试环境：shop.example" in text
    assert "开发分支：hotfix/insta360" in text
    assert "commit：abc123def" in text
    assert "结果：通过" in text
    assert "结果：失败" in text
    assert "业务主键：DETAIL-01" in text
    assert "操作前状态：待退款" in text
    assert "操作后查库结果：已退款" in text
    assert "![步骤 0](cases/A/shot.png)" in text
    assert str(run) not in text
    assert "全部通过" not in text
    assert "data:image" not in text
    kept = read_state(run)
    for key in ("requirement", "branch", "commit", "base_url", "environment_host"):
        assert kept[key] == frozen[key]
    assert kept["stage"] == "report"
    assert run_status(run, kept, ExecutorSlot()) == "complete"
    assert (run / "plan.json").read_bytes() == plan_bytes

    denied = client.get(f"/api/runs/{RUN_ID}/report")
    assert denied.status_code == 403
    assert client.get(f"/api/runs/{RUN_ID}/artifacts/report.zip").status_code == 403
    body = client.get(f"/api/runs/{RUN_ID}/report", headers=auth(app))
    assert body.status_code == 200
    assert "需求编号：DSDD-123" in body.text
    packed = client.get(f"/api/runs/{RUN_ID}/artifacts/report.zip", headers=auth(app))
    assert packed.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(packed.content)).namelist()
    assert "report.md" in names
    assert "cases/A/shot.png" in names
    assert "db-before.json" not in names
    assert "db-after-1.json" not in names

    image = run / "cases" / "A" / "shot.png"
    blob = image.read_bytes()
    image.write_bytes(blob[:-1] + bytes([blob[-1] ^ 1]))
    assert render_report(run) == run / "report.md"
    revised = (run / "report.md").read_text(encoding="utf-8")
    assert "证据缺失：cases/A/shot.png" in revised
    assert "![步骤 0](cases/A/shot.png)" not in revised
    assert "![步骤 0](cases/B/shot.png)" in revised
    assert "需求编号：DSDD-123" in revised
    assert load_case(run, "A")["status"] == "passed"
    assert load_case(run, "B")["status"] == "failed"
    assert (run / "report.md").is_file()
    names = zipfile.ZipFile(run / "report.zip").namelist()
    assert "db-before.json" not in names
    assert "db-after-1.json" not in names
