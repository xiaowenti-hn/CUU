import base64
import json

from qa_platform.runner.execute import execute_reuse, navigation_allowed
from qa_platform.store import review_digest, write_json

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
BASE = "https://omstest.example"
APP_NAME = "Microsoft Edge"
JS = (
    f"let app = await cua.getApp('{APP_NAME}'); "
    "await app.getAXState(); "
    "await app.query({text: '提交退款确认', role: 'button'}); "
    "await app.click(3); await app.getAXState();"
)


def build(tmp, strategy="reuse", steps=None, pages=None):
    run = tmp / "run"
    plan = {
        "ui": {
            "seed_object_count": 0,
            "pages": pages if pages is not None else [{
                "id": "refund",
                "path": "/refund",
                "title_text": "确认退款",
                "controls": [{"role": "button", "name": "提交退款确认", "locator_status": "pending"}],
            }],
        },
        "cases": [
            {
                "id": "C1",
                "title": "确认退款",
                "check_id": "K1",
                "steps": steps or [{
                    "phase": "test",
                    "action": "click",
                    "role": "button",
                    "name": "提交退款确认",
                    "locator_status": "pending",
                    "effect": "read",
                    "expect_text": ["已退款"],
                }],
            },
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
        "select_sql": {"K1": "SELECT `detail_id`, `status` FROM `refunds` WHERE `detail_id` = 'DETAIL-01' LIMIT 50"},
        "fixtures": {"K1": {"database": "oms3_onlinepublish_biz", "pk": {"detail_id": "DETAIL-01"}}},
        "records": {"K1": [{"detail_id": "DETAIL-01", "status": "待退款"}]},
    }
    write_json(run / "plan.json", plan)
    write_json(run / "baseline.json", baseline)
    write_json(run / "state.json", {
        "id": "run",
        "base_url": BASE,
        "allowed_databases": ["oms3_onlinepublish_biz"],
        "review": None,
        "events": [],
        "exit_code": 4,
        "stop_reason": "",
        "approvals": [{
            "id": "b" * 32,
            "kind": "baseline",
            "digest": review_digest("baseline", baseline),
            "payload": baseline,
        }],
    })
    return run


class Session:
    def __init__(self, run=None):
        self.calls = []
        self.run_dir = run

    def reset(self):
        return None

    def list_apps(self):
        return f"{APP_NAME} — com.microsoft.edgemac [running]\nQA Platform Browser — local.qa.platform.browser [running]"

    def get_app_state(self, app):
        assert app == APP_NAME
        return {"content": [{"type": "text", "text": f"地址 {BASE}/refund"}]}

    def run(self, code, app=None):
        if self.run_dir is not None:
            text = (self.run_dir / "cases" / "C1" / "case.json").read_text(encoding="utf-8")
            assert "尚未核对" in text
        self.calls.append(code)
        return {"url": BASE + "/refund", "text": "看到提交退款确认", "screenshot": PNG}


def test_open_browser_uses_the_logged_in_window_not_the_blank_app():
    from qa_platform.runner.execute import open_browser_name

    class Desk:
        def list_apps(self):
            return "\n".join([
                "QA Platform Browser — local.qa.platform.browser [running]",
                "Google Chrome — com.google.chrome [running]",
                "Microsoft Edge — com.microsoft.edgemac [running]",
            ])

        def get_app_state(self, app):
            if app == "Microsoft Edge":
                return {"content": [{"type": "text", "text": "http://bfrs-uat.test.jeoms.com/bill/platformBill"}]}
            return {"content": [{"type": "text", "text": "about:blank"}]}

    assert open_browser_name(Desk(), "http://bfrs-uat.test.jeoms.com") == "Microsoft Edge"
    assert open_browser_name(Desk(), "http://other.example") == "Microsoft Edge"
    assert open_browser_name(Desk(), "http://bfrs-uat.test.jeoms.com", "chrome") == "Google Chrome"
    assert open_browser_name(Desk(), "http://bfrs-uat.test.jeoms.com", "edge") == "Microsoft Edge"

    class OnlyChrome:
        def list_apps(self):
            return "Google Chrome — com.google.chrome [running]"

        def get_app_state(self, app):
            return {"content": [{"type": "text", "text": "http://bfrs-uat.test.jeoms.com/bill/platformBill"}]}

    assert open_browser_name(OnlyChrome(), "http://bfrs-uat.test.jeoms.com", "edge") == "Microsoft Edge"


def test_trade_id_field_is_not_the_reprocess_button():
    from qa_platform.runner.execute import _match_index

    body = json.dumps({"matches": [
        {"element_index": 5, "role": "AXButton", "text": "重转交易号"},
        {"element_index": 9, "role": "AXButton", "text": "搜索"},
        {"element_index": 12, "role": "AXTextField", "text": "交易号"},
    ]})
    result = {"content": [{"type": "text", "text": body}]}
    assert _match_index(result, "交易号", "textbox") == "12"
    assert _match_index(result, "搜索", "button") == "9"
    assert _match_index(result, "重转交易号", "button") is None
    listed = json.dumps({"matches": [
        {"element_index": 3, "role": "AXCell", "text": "交易号"},
        {"element_index": 4, "role": "AXStaticText", "text": "交易号"},
        {"element_index": 12, "role": "AXTextField", "text": "交易号"},
    ]})
    listed_result = {"content": [{"type": "text", "text": listed}]}
    assert _match_index(listed_result, "交易号", "textbox") == "12"
    column_only = {"content": [{"type": "text", "text": json.dumps({"matches": [
        {"element_index": 3, "role": "AXCell", "text": "交易号"},
        {"element_index": 7, "role": "AXColumn", "text": "交易号"},
    ]})}]}
    assert _match_index(column_only, "交易号", "textbox") is None
    mixed = {"content": [{"type": "text", "text": json.dumps({"matches": [
        {"element_index": 2, "role": "AXButton", "text": "无交易号"},
        {"element_index": 5, "role": "AXButton", "text": "重转交易号"},
        {"element_index": 8, "role": "AXButton", "text": "AI分析交易号"},
        {"element_index": 12, "role": "AXTextField", "text": "交易号"},
    ]})}]}
    assert _match_index(mixed, "交易号", "") == "12"
    buttons_only = {"content": [{"type": "text", "text": json.dumps({"matches": [
        {"element_index": 2, "role": "AXButton", "text": "无交易号"},
        {"element_index": 5, "role": "AXButton", "text": "重转交易号"},
        {"element_index": 8, "role": "AXButton", "text": "AI分析交易号"},
    ]})}]}
    assert _match_index(buttons_only, "交易号", "") is None
    from qa_platform.runner.execute import _page_lines
    assert "列表字段" in _page_lines(column_only, "交易号")
    assert "不是查询框" in _page_lines(column_only, "交易号")
    tab = {"content": [{"type": "text", "text": json.dumps({"matches": [
        {"element_index": 4, "role": "AXStaticText", "text": "丙页"},
        {"element_index": 9, "role": "AXRadioButton", "text": "丙页"},
    ]})}]}
    assert _match_index(tab, "丙页", "") == "9"


def test_static_text_is_clicked_at_its_position(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "切页",
        "goal": "打开丙页。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "click", "role": "button", "name": "丙页"}],
    }
    clicks = []
    turns = [
        {"think": "点这一项", "action": "click", "text": "丙页"},
        {"think": "结束", "action": "done"},
    ]

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            matches = [{
                "element_index": 4,
                "role": "AXStaticText",
                "text": text,
                "bounds": {"x": 10, "y": 20, "w": 80, "h": 30},
            }]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index, method="", x=None, y=None):
            clicks.append((index, method, x, y))

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "",
    ) == 0
    assert clicks == [("4", "app_post", 50.0, 35.0)]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("点的是文字" in item["basis"] for item in events)


def test_click_uses_the_named_control_not_its_text(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "切页",
        "goal": "打开丙页。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "click", "role": "button", "name": "丙页"}],
    }
    clicks = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            matches = [
                {"element_index": 4, "role": "AXStaticText", "text": text, "bounds": {"x": 10, "y": 20, "w": 80, "h": 30}},
                {"element_index": 9, "role": "AXRadioButton", "text": text},
            ]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index, method="", x=None, y=None):
            clicks.append((index, method))

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(),
        lambda _prompt: {"think": "点页签", "action": "click", "text": "丙页"} if not clicks else {"think": "结束", "action": "done"},
        "test", "Microsoft Edge", "",
    ) == 0
    assert clicks == [("9", "")]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any("点的是文字" in item["basis"] for item in events)


def test_tree_button_is_clicked_when_query_only_sees_its_text():
    from qa_platform.runner.execute import _tree_control

    tree = "\n".join([
        "\t\t80 button 甲页 Frame: x=154, y=296, w=141, h=40",
        "\t\t82 button 丙页 Frame: x=154, y=376, w=141, h=40",
        "\t\t151 按钮 重转丙页",
        "\t\t232 弹出式按钮 搜索标签页",
        "\t\t4 AXStaticText 丙页",
    ])
    assert _tree_control(tree, "丙页") == "82"
    assert _tree_control(tree, "重转丙页") is None
    assert _tree_control(tree, "搜索") is None


def test_click_hits_the_button_that_contains_the_text(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "切页",
        "goal": "打开丙页。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "click", "role": "button", "name": "丙页"}],
    }
    clicks = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"82 button 丙页 Frame: x=154, y=376, w=141, h=40\nURL: {BASE}/refund"}

        def query(self, app, text, role=""):
            matches = [{
                "element_index": 1000165,
                "role": "AXStaticText",
                "text": text,
                "bounds": {"x": 158, "y": 388, "w": 70, "h": 16},
            }]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index, method="", x=None, y=None):
            clicks.append((str(index), method))

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(),
        lambda _prompt: {"think": "点页签", "action": "click", "text": "丙页"} if not clicks else {"think": "结束", "action": "done"},
        "test", "Microsoft Edge", "",
    ) == 0
    assert clicks == [("82", "")]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any("点的是文字" in item["basis"] for item in events)


def test_drive_plan_follows_each_step_on_the_page():
    from qa_platform.runner.execute import drive_plan

    class Hand:
        def __init__(self):
            self.calls = []

        def query(self, app, text, role=""):
            self.calls.append(("query", app, text, role))
            body = json.dumps({"matches": [{"element_index": len(self.calls), "role": role, "text": text}], "truncated": False, "stop_reason": "ok"})
            return {"content": [{"type": "text", "text": body}]}

        def click(self, app, index):
            self.calls.append(("click", app, index))

        def set_value(self, app, index, value):
            self.calls.append(("set", app, index, value))

    hand = Hand()
    seen = drive_plan(hand, "Microsoft Edge", [
        {"phase": "test", "action": "select", "role": "combobox", "name": "资金账户", "option": "请选择资金账户"},
        {"phase": "test", "action": "type", "role": "textbox", "name": "交易号", "value": "使用库里已有交易号"},
        {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
    ], "166996404164")
    assert ("query", "Microsoft Edge", "资金账户", "combobox") in hand.calls
    assert any(call[0] == "set" and call[3] == "166996404164" for call in hand.calls)
    assert ("query", "Microsoft Edge", "搜索", "button") in hand.calls
    assert "已填写「交易号」" in seen
    assert "已点击「搜索」" in seen


def test_implementation_brief_reads_the_tab_and_filter(tmp_path):
    from qa_platform.runner.execute import implementation_brief

    repo = tmp_path / "bfrs"
    front = tmp_path / "bfrs-front"
    controller = repo / "bfrs-app/src/main/java/com/greatonce/bfrs/controller/AlipayBillController.java"
    controller.parent.mkdir(parents=True)
    controller.write_text(
        '@RequestMapping("/bill/alipay")\npublic class AlipayBillController {}\n',
        encoding="utf-8",
    )
    index = front / "src/views/bill/platform-bill/index.vue"
    index.parent.mkdir(parents=True)
    index.write_text(
        "import Alipay from '@/views/bill/platform-bill/ali-pay/index.vue';\n"
        "{ name: '1', label: '支付宝账单', menuId: '1020101', component: Alipay },\n",
        encoding="utf-8",
    )
    api = front / "src/api/bill/platform-bill/ali-pay/index.ts"
    api.parent.mkdir(parents=True)
    api.write_text("return '/bill/alipay';\n", encoding="utf-8")
    filt = front / "src/views/bill/platform-bill/ali-pay/attrs/filter-bar-attrs.ts"
    filt.parent.mkdir(parents=True)
    filt.write_text(
        "label: '资金账户',\nfieldName: 'fundingAccountIds',\n"
        "label: '交易号',\nfieldName: 'tradeIds',\nplaceholder: Placeholders.IN,\n",
        encoding="utf-8",
    )
    util = front / "src/libs/util.js"
    util.parent.mkdir(parents=True)
    util.write_text("IN: '多个请用英文逗号隔开',\n", encoding="utf-8")
    text = implementation_brief(
        str(repo),
        "bfrs-app/src/main/java/com/greatonce/bfrs/controller/AlipayBillController.java:1",
    )
    assert "支付宝账单" in text
    assert "fundingAccountIds" in text
    assert "tradeIds" in text
    assert "多个请用英文逗号隔开" in text
    assert "/bill/alipay" in text
    assert "页面页签是「支付宝账单」" in text


def test_imported_page_without_a_label_does_not_invent_a_tab(tmp_path):
    from qa_platform.runner.execute import implementation_brief

    repo = tmp_path / "bfrs"
    front = tmp_path / "bfrs-front"
    controller = repo / "app/AmazonDateRangeReportController.java"
    controller.parent.mkdir(parents=True)
    controller.write_text(
        '@RequestMapping("/bill/amazon/dateRange")\npublic class AmazonDateRangeReportController {}\n',
        encoding="utf-8",
    )
    api = front / "src/api/bill/platform-bill/amazon-date-range/index.ts"
    api.parent.mkdir(parents=True)
    api.write_text("return '/bill/amazon/dateRange';\n", encoding="utf-8")
    index = front / "src/views/bill/platform-bill/index.vue"
    index.parent.mkdir(parents=True)
    index.write_text(
        "import AmazonDateRange from '@/views/bill/platform-bill/amazon-date-range/index.vue';\n"
        "{ name: '1', label: '支付宝账单', component: Alipay },\n",
        encoding="utf-8",
    )
    text = implementation_brief(str(repo), "app/AmazonDateRangeReportController.java:1")
    assert "/bill/amazon/dateRange" in text
    assert "页面页签是" not in text


def test_agent_reads_code_then_follows_the_plan(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Bill.java").write_text("class Bill { String tradeId; }\n", encoding="utf-8")
    run = build(tmp_path / "run")
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    state["repo_path"] = str(repo)
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    plan = {"checks": [{"id": "K1", "source": "Bill.java:1"}]}
    turns = [
        {"think": "先看账单代码里的交易号字段", "action": "read"},
        {"think": "带上路径再读", "action": "read", "text": "Bill.java"},
        {"think": "页面上先找交易号输入框", "action": "look", "text": "交易号", "role": "textbox"},
        {"think": "把库里的单号填进去", "action": "type", "text": "交易号", "role": "textbox"},
        {"think": "按方案点搜索", "action": "click", "text": "搜索", "role": "button"},
        {"think": "方案里的两步做完了", "done": True},
    ]

    class Hand:
        def query(self, app, text, role=""):
            body = json.dumps({"matches": [{"element_index": 4, "role": role, "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            assert value == "166996404164"
            return {}

    assert _think_and_act(run, state, case, plan, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "166996404164") == 0
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("读代码要带上相对路径" in item["basis"] for item in events)
    assert any("Bill.java" in item["basis"] or "交易号字段" in item["basis"] for item in events)
    assert any("已点击「搜索」" in item["basis"] for item in events)


def test_mismatch_goes_back_to_the_model(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    class Page:
        def query(self, app, text, role=""):
            if "逗号" in text:
                matches = [{"element_index": 21, "role": "AXTextField", "text": text}]
            else:
                matches = [{"element_index": 3, "role": "AXCell", "text": "交易号"}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    clicks = []
    seen = []

    class Hand(Page):
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def click(self, app, index):
            clicks.append(index)

        def set_value(self, app, index, value):
            assert index == "21"
            assert value == "166996404164"

    turns = [
        {"think": "先按名字填交易号", "action": "type", "text": "交易号", "role": "textbox"},
        {"think": "刚才对上的是列表列，改填页面上的输入框", "action": "type", "text": "多个请用英文逗号隔开", "role": "textbox"},
        {"think": "查询框已经填好", "done": True},
    ]

    def model(prompt):
        seen.append(prompt)
        return turns.pop(0)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "166996404164") == 0
    assert "列表字段" in seen[1]
    assert clicks == ["21"]


def test_model_sees_the_live_page_and_chooses_what_to_type(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查订单",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "订单号"}],
    }
    prompts = []
    values = []

    class Hand:
        def get_app_state(self, app):
            return {"content": [{"type": "text", "text": "AXTextField 订单号\nAXButton 查询"}]}

        def query(self, app, text, role=""):
            body = json.dumps({"matches": [{"element_index": 9, "role": "AXTextField", "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            values.append(value)

    turns = [
        {"think": "先看整页", "action": "look"},
        {"think": "页面上是订单号，填这一单", "action": "type", "text": "订单号", "role": "textbox", "value": "SO-1"},
        {"think": "这一步做完了", "done": True},
    ]

    def model(prompt):
        prompts.append(prompt)
        return turns.pop(0)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "KEEP") == 0
    assert "当前能填" in prompts[0]
    assert "订单号" in prompts[0]
    assert "AXButton 查询" in prompts[1]
    assert values == ["SO-1"]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any(item["basis"].startswith("准备填写「订单号」") for item in events)
    assert not any("交易号" in item["basis"] for item in events)


def test_type_uses_the_text_field_when_textbox_role_misses_it(tmp_path):
    from qa_platform.runner.execute import _page_lines, _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查订单",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "订单号"}],
    }
    asked = []

    class Hand:
        def query(self, app, text, role=""):
            asked.append((text, role))
            if role == "textfield":
                matches = [{"element_index": 52, "role": "AXTextField", "text": text}]
            elif role in {"", "textbox"}:
                matches = [
                    {"element_index": 5, "role": "AXButton", "text": f"重转{text}"},
                    {"element_index": 9, "role": "AXCell", "text": text},
                ]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            assert index == "52"

        def set_value(self, app, index, value):
            assert value == "SO-9"

    turns = [
        {"think": "框已经在，直接填", "action": "type", "text": "订单号 输入框", "role": "textbox"},
        {"think": "填好了", "done": True},
    ]
    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "SO-9",
    ) == 0
    assert ("订单号", "textfield") in asked
    assert all(text != "订单号 输入框" for text, _role in asked)
    menu = "\n".join(f"{index} button 菜单{index}" for index in range(60))
    page = _page_lines({"content": [{"type": "text", "text": menu + "\n88 AXTextField 订单号"}]})
    assert page.index("AXTextField 订单号") < page.index("button 菜单")


def test_first_prompt_already_lists_fillable_fields(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查订单",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "订单号"}],
    }
    prompts = []
    values = []

    class Hand:
        def get_app_state(self, app):
            return {"content": [{"type": "text", "text": "\n".join(f"{index} button 菜单{index}" for index in range(40))}]}

        def query(self, app, text, role=""):
            if "订单号" not in text:
                return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}
            if role == "textfield":
                matches = [{"element_index": 52, "role": "AXTextField", "text": "订单号"}]
            else:
                matches = [
                    {"element_index": 3, "role": "AXCell", "text": "订单号"},
                    {"element_index": 52, "role": "AXTextField", "text": "订单号"},
                ]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            values.append(value)

    turns = [
        {"think": "能填的是订单号输入框", "action": "type", "text": "订单号", "role": "textbox", "value": "SO-2"},
        {"think": "填好了", "done": True},
    ]

    def model(prompt):
        prompts.append(prompt)
        return turns.pop(0)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "KEEP") == 0
    assert "当前能填" in prompts[0]
    assert "可填写" in prompts[0]
    assert "列表字段" in prompts[0]
    assert values == ["SO-2"]


def test_plan_brief_reads_as_a_task():
    from qa_platform.runner.execute import _plan_brief

    text = _plan_brief(
        {
            "id": "C1",
            "title": "按订单号查询",
            "expect_text": ["共", "条"],
            "steps": [
                {"phase": "test", "action": "type", "role": "textbox", "name": "订单号"},
                {"phase": "test", "action": "select", "role": "combobox", "name": "状态", "option": "已审核"},
                {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
            ],
        },
        {"checks": []},
        "test",
        "SO-1",
    )
    assert "任务：C1 按订单号查询" in text
    assert "目标：按订单号查询" in text
    assert "起点：从当前已经打开的页面开始。" in text
    assert "填写「订单号」（输入框）" in text
    assert "选择「状态」为「已审核」（下拉）" in text
    assert "点击「搜索」（按钮）" in text
    assert "做完后页面上应出现：" in text
    assert "做法只是参考" in text
    from qa_platform.runner.execute import _agent_prompt
    prompt = _agent_prompt(text, "", "当前页面", [], "当前目标：按订单号查询")
    assert "不要点重转" in prompt
    assert "再看一次页面" in prompt
    assert "就是结果" not in prompt
    assert '"text":"控件名"' in prompt
    assert '"value":"要填的内容"' in prompt
    assert '{"read":"相对路径"}' in prompt

    example = _plan_brief(
        {
            "id": "C1",
            "title": "按条件查询",
            "expect_text": ["查询完成", "共 0 条"],
            "steps": [
                {"phase": "test", "action": "type", "role": "textbox", "name": "条件"},
            ],
        },
        {"checks": []},
        "test",
        "A-1,A-2",
    )
    assert "做完后页面上应出现：" not in example
    assert "共 0 条" not in example
    assert "共 N 条" in example
    assert "是示例" in example
    assert "测试输入" in example
    assert "A-1" in example and "A-2" in example
    assert "SO-1" in text
    assert "type textbox" not in text


def test_plan_brief_prefers_the_written_goal_and_start():
    from qa_platform.runner.execute import _plan_brief

    text = _plan_brief(
        {
            "id": "C1",
            "title": "标题",
            "goal": "查出这一行",
            "start": "账单页的第一个页签",
            "expect_text": ["共"],
            "steps": [],
        },
        {"ui": {"pages": [{"title_text": "别的页"}]}, "checks": []},
        "test",
        "",
    )
    assert "目标：查出这一行" in text
    assert "起点：账单页的第一个页签" in text
    assert "别的页" not in text


def test_decision_json_is_taken_from_the_reply_or_the_reasoning():
    from qa_platform.runner.execute import parse_decision

    fenced = parse_decision("先这样：\n```json\n{\"think\":\"填\",\"action\":\"type\",\"text\":\"交易号\",\"value\":\"\"}\n```")
    assert fenced["action"] == "type"
    assert fenced["text"] == "交易号"

    buried = parse_decision("我先看一下页面。", "页面上有输入框。\n{\"think\":\"填交易号\",\"action\":\"type\",\"text\":\"交易号\",\"value\":\"1\"}")
    assert buried["action"] == "type"
    assert buried["text"] == "交易号"
    assert buried["_reasoning"].startswith("页面上有输入框")

    prose = parse_decision("还没决定", "只是在想")
    assert prose.get("action") is None
    assert prose["done"] is False


def test_select_picks_the_option(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "按状态筛选",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "select", "role": "combobox", "name": "状态", "option": "已审核"}],
    }
    clicks = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            if "已审核" in text:
                matches = [{"element_index": 8, "role": "AXMenuItem", "text": "已审核"}]
            else:
                matches = [{"element_index": 2, "role": "AXComboBox", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            clicks.append(index)

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "选状态", "action": "select", "text": "状态", "option": "已审核"},
        {"think": "选好了", "done": True},
    ]
    assert _think_and_act(run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "") == 0
    assert clicks == ["2", "8"]


def test_more_than_twelve_rounds_keep_going_until_twenty(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    looks = [{"think": "再看结果", "action": "look", "text": "共", "role": "textbox"} for _ in range(14)]
    turns = looks + [
        {"think": "填查询框", "action": "type", "text": "交易号", "role": "textbox"},
        {"think": "结果已经出来", "done": True},
    ]

    show = {"on": False}

    class Hand:
        def query(self, app, text, role=""):
            kind = "AXStaticText" if show["on"] and text == "166996404164" else "AXTextField"
            body = json.dumps({"matches": [{"element_index": 8, "role": kind, "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    def ask(_run_dir):
        raise AssertionError("还没到 20 轮")

    def model(_prompt):
        decision = turns.pop(0)
        if decision.get("done"):
            show["on"] = True
        return decision

    assert _think_and_act(run, state, case, plan={"checks": []}, session=Hand(), model=model, phase="test", app="Microsoft Edge", typed="166996404164", ask=ask) == 0


def test_done_waits_for_the_result_and_keeps_the_window(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查支付宝",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["共 1 条"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    show = {"on": False}

    class Hand:
        def query(self, app, text, role=""):
            if text == "共 1 条" and show["on"]:
                matches = [{"element_index": 7, "role": "AXStaticText", "text": "共 1 条"}]
            elif text == "SO-9" and show["on"]:
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-9"}]
            elif text == "SO-9":
                matches = [{"element_index": 3, "role": "AXTextField", "text": "SO-9"}]
            else:
                kind = "AXButton" if text == "搜索" else "AXTextField"
                matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            content = [{"type": "text", "text": "支付宝账单"}]
            if include_screenshot:
                content.append({"type": "image", "data": base64.b64encode(PNG).decode()})
            return {"content": content}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "填单号", "action": "type", "text": "交易号", "value": "SO-9"},
        {"think": "点搜索", "action": "click", "text": "搜索"},
        {"think": "做完了", "done": True},
        {"think": "结果出来了", "done": True},
    ]

    def model(_prompt):
        decision = turns.pop(0)
        if decision.get("done") and not turns:
            show["on"] = True
        return decision

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "SO-9") == 0
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("页面上还没有出现：共 1 条" in item["basis"] for item in events)
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["observation"] == "final"
    shot = run / saved["rounds"][0]["screenshot"]["file"]
    assert shot.read_bytes() == PNG


def test_done_accepts_the_page_count_when_expect_is_the_example_zero(tmp_path):
    from qa_platform.runner.execute import _think_and_act
    from qa_platform.runner.report import render_report, seal_run

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "按条件查询",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["查询完成", "共 0 条"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "条件"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    prompts = []

    class Hand:
        def query(self, app, text, role=""):
            if text == "共":
                matches = [
                    {"element_index": 1, "role": "AXGroup", "text": "SQL Server 共 条 的标签"},
                    {"element_index": 2, "role": "AXStaticText", "text": "共 505 条"},
                ]
            elif text == "条件":
                matches = [{"element_index": 4, "role": "AXTextField", "text": "条件"}]
            elif text == "搜索":
                matches = [{"element_index": 5, "role": "AXButton", "text": "搜索"}]
            else:
                matches = [{"element_index": 3, "role": "AXTextField", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            return {"content": [{"type": "text", "text": "页面"}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "填入准备的值", "action": "type", "text": "条件", "value": ""},
        {"think": "查询", "action": "click", "text": "搜索"},
        {"think": "结果条数已经出来", "done": True},
    ]

    def model(prompt):
        prompts.append(prompt)
        return turns.pop(0)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "A-1,A-2") == 0
    assert any("共 N 条" in prompt for prompt in prompts)
    assert all("应出现：\n- 共 0 条" not in prompt and "应出现：\n- 查询完成" not in prompt for prompt in prompts)
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any("共 0 条" in item["basis"] for item in events)
    seal_run(run)
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "passed"
    assert saved["filled_values"] == ["A-1", "A-2"]
    assert render_report(run) == run / "report.md"
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "页面结果：共 505 条" in report
    assert "写入：2 条：A-1、A-2" in report


def test_query_count_waits_until_the_number_changes(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "按条件查询",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["查询完成", "共 0 条"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "条件"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    prompts = []

    class Hand:
        native = object()
        count_wait = 0.5
        reads = 0

        def query(self, app, text, role=""):
            if text == "共":
                self.reads += 1
                label = "共 0 条" if self.reads < 3 else "共 9 条"
                matches = [{"element_index": 2, "role": "AXStaticText", "text": label}]
            elif text == "条件":
                matches = [{"element_index": 4, "role": "AXTextField", "text": "条件"}]
            elif text == "搜索":
                matches = [{"element_index": 5, "role": "AXButton", "text": "搜索"}]
            else:
                matches = [{"element_index": 3, "role": "AXTextField", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            return {"content": [{"type": "text", "text": "页面"}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"think": "填入准备的值", "action": "type", "text": "条件", "value": "A-1,A-2"}
        if len(prompts) == 2:
            return {"think": "查询", "action": "click", "text": "搜索"}
        return {"think": "结果条数已经出来", "done": True}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "A-1,A-2") == 0
    assert "页面结果：共 9 条" in prompts[-1]
    assert "这是查询后的条数" not in prompts[1]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("正在等结果条数变化" in item["basis"] for item in events)
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["observation"] == "final"
    assert saved["status"] == "running"


def test_unchanged_query_count_does_not_finish_the_case(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "按条件查询",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["结果条数"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "条件"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    prompts = []

    class Hand:
        native = object()
        count_wait = 0.15

        def query(self, app, text, role=""):
            if text == "共":
                matches = [{"element_index": 2, "role": "AXStaticText", "text": "共 0 条"}]
            elif text == "条件":
                matches = [{"element_index": 4, "role": "AXTextField", "text": "条件"}]
            elif text == "搜索":
                matches = [{"element_index": 5, "role": "AXButton", "text": "搜索"}]
            else:
                matches = [{"element_index": 3, "role": "AXTextField", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            return {"content": [{"type": "text", "text": "页面"}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"think": "填入准备的值", "action": "type", "text": "条件", "value": "A-1,A-2"}
        if len(prompts) == 2:
            return {"think": "查询", "action": "click", "text": "搜索"}
        if len(prompts) == 3:
            assert "点查询之前" in prompt
            assert "这是查询后的条数" not in prompt
            return {"think": "先结束", "done": True}
        assert "等它变掉再结束" in prompt
        return {"think": "先停", "action": "skip", "text": "先停"}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "A-1,A-2") == 0
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["observation"] != "final"
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("等它变掉再结束" in item["basis"] for item in events)


def test_unqueried_baseline_is_not_filled(tmp_path):
    from qa_platform.runner.execute import _think_and_act
    from qa_platform.runner.report import UNQUERIED

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查视频号",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["结果条数"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "流水ID"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    typed = []

    class Hand:
        def query(self, app, text, role=""):
            kind = "AXTextField" if "ID" in text or text == "流水ID" else "AXButton"
            body = json.dumps({"matches": [{"element_index": 4, "role": kind, "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

        def get_app_state(self, app, include_screenshot=False):
            return {"content": [{"type": "text", "text": "页面"}]}

        def click(self, app, index):
            raise AssertionError("clicked")

        def set_value(self, app, index, value):
            typed.append(value)

    def model(_prompt):
        raise AssertionError("model")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), model,
        "test", "Microsoft Edge", "", unqueried=True,
    ) == 0
    assert typed == []
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "skipped"
    assert saved["result_reason"] == UNQUERIED


def test_count_is_read_only_when_the_case_expects_it(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    def run_case(root, expect):
        run = build(root)
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
        case = {
            "id": "C1",
            "title": "查一下",
            "check_id": "K1",
            "status": "running",
            "expect_text": expect,
            "steps": [{"phase": "test", "action": "click", "role": "button", "name": "搜索"}],
        }
        asked = []
        prompts = []

        class Hand:
            def get_app_state(self, app, include_screenshot=False):
                return {"text": f"URL: {BASE}/refund\n46 button 业务流水"}

            def query(self, app, text, role=""):
                asked.append(text)
                if text == "共":
                    matches = [{"element_index": 2, "role": "AXStaticText", "text": "共 505 条"}]
                elif text == "搜索":
                    matches = [
                        {"element_index": 9, "role": "AXPopUpButton", "text": "搜索标签页"},
                        {"element_index": 10, "role": "AXTextField", "text": "地址和搜索栏"},
                        {"element_index": 11, "role": "AXStaticText", "text": "搜索条件"},
                        {"element_index": 5, "role": "AXButton", "text": "搜索"},
                    ]
                else:
                    matches = []
                return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

            def click(self, app, index):
                assert index == "5"

        def model(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                return {"think": "点搜索", "action": "click", "text": "搜索"}
            return {"think": "结束", "done": True}

        assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "") == 0
        return prompts, asked

    counted, asked = run_case(tmp_path / "count", ["结果条数"])
    assert "页面结果：共 505 条" in counted[1]
    assert "搜索标签页" not in counted[1]
    assert "地址和搜索栏" not in counted[1]
    assert "共" in asked

    plain, asked = run_case(tmp_path / "plain", [])
    assert all("页面结果" not in prompt and "就是结果" not in prompt for prompt in plain)
    assert "共" not in asked


def test_action_done_finishes_the_case(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查一下",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["结果条数"],
        "steps": [{"phase": "test", "action": "click", "role": "button", "name": "搜索"}],
    }
    prompts = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            if text == "共":
                matches = [{"element_index": 2, "role": "AXStaticText", "text": "共 505 条"}]
            else:
                matches = [{"element_index": 5, "role": "AXButton", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"think": "点搜索", "action": "click", "text": "搜索"}
        return {"think": "结果已经出来", "action": "done"}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "") == 0
    assert all("没有可执行的动作" not in prompt for prompt in prompts)
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["observation"] == "final"
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any("没有可执行的动作" in item["basis"] for item in events)


def test_value_still_in_the_field_does_not_finish(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查支付宝",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["支付宝账单"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    show = {"on": False}
    prompts = []

    class Hand:
        def query(self, app, text, role=""):
            if text == "支付宝账单":
                matches = [{"element_index": 1, "role": "AXRadioButton", "text": "支付宝账单"}]
            elif text == "SO-9" and show["on"]:
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-9"}]
            elif text == "SO-9":
                matches = [{"element_index": 3, "role": "AXTextField", "text": "SO-9"}]
            else:
                kind = "AXButton" if text == "搜索" else "AXTextField"
                matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            content = [{"type": "text", "text": "页面"}]
            if include_screenshot:
                content.append({"type": "image", "data": base64.b64encode(PNG).decode()})
            return {"content": content}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "填单号", "action": "type", "text": "交易号", "value": "SO-9"},
        {"think": "点搜索", "action": "click", "text": "搜索"},
        {"think": "做完了", "done": True},
        {"think": "列表里有了", "done": True},
    ]

    def model(prompt):
        prompts.append(prompt)
        decision = turns.pop(0)
        if decision.get("done") and not turns:
            show["on"] = True
        return decision

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "SO-9") == 0
    assert any("录入控件以外" in prompt and "SO-9" in prompt for prompt in prompts)


def test_done_accepts_the_typed_value_when_expect_is_only_control_names(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查支付宝",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["交易号", "搜索"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    show = {"on": False}

    class Hand:
        def query(self, app, text, role=""):
            if text == "SO-9" and show["on"]:
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-9"}]
            elif text == "SO-9":
                matches = [{"element_index": 3, "role": "AXTextField", "text": "SO-9"}]
            else:
                kind = "AXButton" if text == "搜索" else "AXTextField"
                matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            content = [{"type": "text", "text": "页面"}]
            if include_screenshot:
                content.append({"type": "image", "data": base64.b64encode(PNG).decode()})
            return {"content": content}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "填单号", "action": "type", "text": "交易号", "value": "SO-9"},
        {"think": "点搜索", "action": "click", "text": "搜索"},
        {"think": "做完了", "done": True},
        {"think": "列表里有了", "done": True},
    ]

    def model(_prompt):
        decision = turns.pop(0)
        if decision.get("done") and not turns:
            show["on"] = True
        return decision

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "SO-9") == 0
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["filled_values"] == ["SO-9"]
    assert saved["observation"] == "final"
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("录入控件以外" in item["basis"] and "SO-9" in item["basis"] for item in events)


def test_done_rejects_a_borrowed_number_when_this_check_has_no_rows(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查快手",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["交易号", "搜索"],
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }

    class Hand:
        def query(self, app, text, role=""):
            kind = "AXTextField" if text == "交易号" else "AXButton"
            body = json.dumps({"matches": [{"element_index": 4, "role": kind, "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

        def get_app_state(self, app, include_screenshot=False):
            return {"content": [{"type": "text", "text": "页面"}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    turns = [
        {"think": "借用别的单号", "action": "type", "text": "交易号", "value": "166996404164"},
        {"think": "做完了", "done": True},
    ]
    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "", empty_data=True,
    ) == 0
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any(item["basis"] == "这一条没有库里的数据" for item in events)


def test_closing_shot_skips_a_window_outside_the_product(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查支付宝",
        "check_id": "K1",
        "status": "running",
        "expect_text": ["共 1 条"],
        "steps": [
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }

    class Hand:
        def query(self, app, text, role=""):
            matches = [{"element_index": 7, "role": "AXStaticText", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def get_app_state(self, app, include_screenshot=False):
            content = [{"type": "text", "text": "HTML 内容 需求, URL: https://devops.aliyun.com/project"}]
            if include_screenshot:
                content.append({"type": "image", "data": base64.b64encode(PNG).decode()})
            return {"content": content}

        def click(self, app, index):
            return {}

    turns = [
        {"think": "点搜索", "action": "click", "text": "搜索"},
        {"think": "做完了", "done": True},
    ]
    assert _think_and_act(run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "") == 0
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["rounds"][-1]["screenshot"] is None
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any(item["basis"] == "结束时前台窗口不是产品页" for item in events)
    assert not (run / "cases" / "C1" / "round-1.png").exists()


def test_prepare_cases_copies_the_pass_condition_and_clears_a_waiting_retry(tmp_path):
    from qa_platform.runner.execute import _prepare_cases

    run = build(tmp_path)
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    plan["cases"][0]["goal"] = "查出这一行"
    plan["cases"][0]["start"] = "账单页"
    plan["cases"][0]["expect_text"] = ["共 1 条"]
    folder = run / "cases" / "C1"
    folder.mkdir(parents=True)
    write_json(folder / "case.json", {
        "id": "C1",
        "status": "waiting",
        "filled_values": ["旧单号"],
        "rounds": [{"seen_text": "上一轮"}],
        "result_reason": "旧原因",
    })
    ready = _prepare_cases(run, plan)
    saved = next(item for item in ready if item["id"] == "C1")
    assert saved["goal"] == "查出这一行"
    assert saved["start"] == "账单页"
    assert saved["expect_text"] == ["共 1 条"]
    assert saved["filled_values"] == []
    assert saved["result_reason"] == ""
    assert saved["rounds"] == [{"seen_text": "上一轮"}]


def test_twenty_rounds_ask_on_the_console(tmp_path):
    from qa_platform.runner.execute import _ask_to_continue, _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {"id": "C1", "title": "查交易号", "check_id": "K1", "status": "running", "steps": []}
    turns = [{"think": "再看", "action": "look", "text": "共", "role": "textbox"} for _ in range(20)]
    asked = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            body = json.dumps({"matches": [{"element_index": 1, "role": "AXStaticText", "text": text}]})
            return {"content": [{"type": "text", "text": body}]}

    def ask(run_dir):
        asked.append(run_dir)
        return "stop"

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "", ask=ask) == 2
    assert asked == [run]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any(item.get("status") == "暂停" for item in events)

    waiting = build(tmp_path / "wait")

    def allow():
        (waiting / "continue.requested").write_text("1", encoding="utf-8")

    assert _ask_to_continue(waiting, pause=allow) == "continue"
    saved = json.loads((waiting / "state.json").read_text(encoding="utf-8"))
    assert "round_prompt" not in saved
    assert any(item["basis"] == "继续执行" for item in saved["events"])

    stopping = build(tmp_path / "halt")

    def halt():
        (stopping / "stop.requested").write_text("1", encoding="utf-8")

    assert _ask_to_continue(stopping, pause=halt) == "stop"
    halted = json.loads((stopping / "state.json").read_text(encoding="utf-8"))
    assert halted["stop_reason"] == "manual"
    assert "round_prompt" not in halted
    assert any(item["basis"] == "已手动停止" for item in halted["events"])


def test_navigation_matches_base_port_rules():
    assert navigation_allowed("https://OMSTest.Example/refund", "https://omstest.example")
    assert navigation_allowed("https://omstest.example:443/refund", "https://omstest.example")
    assert not navigation_allowed("https://omstest.example:8443/refund", "https://omstest.example")
    assert navigation_allowed("http://127.0.0.1:9000/refund", "http://127.0.0.1:9000")
    assert not navigation_allowed("http://127.0.0.1/refund", "http://127.0.0.1:9000")
    assert navigation_allowed("http://127.0.0.1/refund", "http://127.0.0.1")
    assert not navigation_allowed("https://evil.example/steal", "https://omstest.example")
    assert not navigation_allowed("javascript:alert(1)", "https://omstest.example")


def test_missing_open_page_blocks_without_launching(tmp_path):
    run = build(tmp_path)
    browsers = []

    class Closed(Session):
        def list_apps(self):
            return "QA Platform Browser — local.qa.platform.browser [running]"

    assert execute_reuse(
        run,
        session=Closed(),
        model=lambda _prompt: {"done": True},
        browser=lambda *args: browsers.append(args),
        query=lambda *_args: [],
    ) == 2
    assert browsers == []
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    assert state["exit_code"] == 2
    assert state["events"][-1]["basis"] == "没有已打开的浏览器。请先打开 Chrome 或 Edge 并登录测试站"
    assert state["events"][-1]["status"] == "阻塞"


def test_missing_page_uses_the_menu_route(tmp_path):
    run = build(tmp_path, pages=[])
    (run / "requirement.txt").write_text("从电商结算单查询\n", encoding="utf-8")
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    state["requirement"] = {"id": "DSDD-9", "title": "结算", "body_file": "requirement.txt"}
    write_json(run / "state.json", state)
    browsers = []

    def query(database, sql, allowed):
        assert database == "oms3_onlinepublish_biz"
        if "information_schema" in sql:
            return [{"TABLE_NAME": "app_menu"}]
        assert "app_menu" in sql
        return [{
            "menu_name": "电商结算单",
            "route": "/bill/platformBill",
            "component": "bill/platform-bill",
        }]

    def model(prompt):
        return {"done": True}

    assert execute_reuse(
        run,
        session=Session(),
        model=model,
        browser=lambda *args: browsers.append(args),
        query=query,
    ) == 0
    assert browsers == [("Microsoft Edge", BASE + "/bill/platformBill")]
    saved = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert saved["ui"]["pages"] == []
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("Microsoft Edge" in item["basis"] and "/bill/platformBill" in item["basis"] for item in events)


def test_reuse_model_chooses_control_and_keeps_screenshots(tmp_path):
    run = build(tmp_path)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"js": JS, "effect": "read"}
        return {"done": True}

    session = Session()
    browsers = []
    assert execute_reuse(
        run,
        session=session,
        model=model,
        browser=lambda *args: browsers.append(args),
        query=lambda *args: [],
    ) == 0
    assert browsers == []
    assert "Microsoft Edge" in prompts[0]
    assert "已经在这个浏览器里打开" in prompts[0]
    assert "待定位不是拒绝点击的理由" in prompts[0]
    assert "query({text, role})" in prompts[0]
    assert session.calls == [JS]
    case = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert case["status"] != "passed"
    assert case["rounds"][0]["seen_text"] == "看到提交退款确认"
    shot = run / case["rounds"][0]["screenshot"]["file"]
    assert shot.read_bytes() == PNG
    assert case["rounds"][0]["screenshot"]["sha256"]


def test_pending_locator_is_not_refused_before_the_model(tmp_path):
    run = build(tmp_path)
    called = []

    def model(prompt):
        called.append(prompt)
        if len(called) == 1:
            assert "locator_status=pending" in prompt
            return {"js": JS, "effect": "read"}
        return {"done": True}

    execute_reuse(run, session=Session(), model=model, browser=lambda *args: None, query=lambda *args: [])
    case = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert case["status"] != "blocked"
    assert "待定位" not in case.get("blocked_reason", "")
    assert called


def test_leaving_base_url_blocks_without_clicking(tmp_path):
    run = build(tmp_path)
    session = Session()

    def model(_prompt):
        return {
            "js": f"let app = await cua.getApp('{APP_NAME}'); await app.click('https://evil.example/steal');",
            "effect": "read",
        }

    assert execute_reuse(
        run, session=session, model=model, browser=lambda *args: None, query=lambda *args: [],
    ) == 2
    assert session.calls == []
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    other = json.loads((run / "cases" / "C2" / "case.json").read_text(encoding="utf-8"))
    assert case["status"] == "blocked"
    assert case["blocked_reason"] == "环境不在 base_url"
    assert other["status"] == "waiting"
    assert state["stop_reason"] == "environment"
    assert not (run / "report.md").exists()


def test_commit_pending_is_saved_before_js_and_resume_skips_it(tmp_path):
    run = build(tmp_path, steps=[{
        "phase": "test",
        "action": "click",
        "role": "button",
        "name": "提交退款确认",
        "locator_status": "ready",
        "effect": "commit",
        "expect_text": ["已退款"],
    }])
    answers = [
        [{"detail_id": "DETAIL-01", "status": "待退款"}],
        [{"detail_id": "DETAIL-01", "status": "待退款"}],
        [{"detail_id": "DETAIL-01", "status": "已退款"}],
    ]

    def query(_database, _sql, _allowed):
        return answers.pop(0)

    decisions = [{
        "js": JS,
        "effect": "commit",
        "expect_state": "已退款",
    }]

    def model(_prompt):
        if decisions:
            return decisions.pop(0)
        return {"done": True}

    session = Session(run)
    assert execute_reuse(
        run, session=session, model=model, browser=lambda *args: None, query=query,
    ) == 2
    assert session.calls == []
    case = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert case.get("pending") is None
    assert case["status"] == "blocked"
    assert case["blocked_reason"] == "数据库只能查询"
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    assert "数据库只能查询，未提交" in state["events"][-1]["basis"]


def test_hidden_deployed_page_skips_after_the_model_sees_the_page(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "case-amazon",
        "title": "亚马逊账单",
        "goal": "在亚马逊账单页按交易号单号搜索，页面能返回对应交易号的账单明细记录。",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    prompts = []

    class Hand:
        def query(self, app, text, role=""):
            if "亚马逊" in text:
                matches = []
            else:
                kind = "AXTextField" if "交易" in text else "AXButton"
                matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            raise AssertionError("clicked")

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    def model(prompt):
        prompts.append(prompt)
        return {"think": "这一页没有亚马逊账单", "action": "skip", "text": "当前部署的界面没有这一项"}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "") == 0
    assert prompts
    assert "当前能填" in prompts[0]
    assert "走到起点" not in prompts[0]
    saved = json.loads((run / "cases" / "case-amazon" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "skipped"
    assert saved["observation"] is None
    assert saved["result_reason"] == "当前部署的界面没有「亚马逊账单」，已跳过"
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("已跳过" in item["basis"] for item in events)


def test_type_puts_prepared_ids_into_the_fill_field(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    asked = []
    values = []
    ids = "0,166996404164,2701767867084076860"

    class Hand:
        def query(self, app, text, role=""):
            asked.append(text)
            if text == "交易号":
                matches = [{"element_index": 12, "role": "AXTextField", "text": "交易号"}]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            assert index == "12"

        def set_value(self, app, index, value):
            values.append(value)

    turns = [
        {"think": "把准备的单号填进去", "action": "type", "text": ids, "value": ""},
        {"think": "填好了", "done": True},
    ]
    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "KEEP",
    ) == 0
    assert values == [ids]
    assert "交易号" in asked
    assert ids not in asked


def test_type_payload_without_a_field_does_not_echo_the_ids(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    ids = ",".join(str(166996404164 + index) for index in range(20))
    prompts = []

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            if text == "搜索":
                matches = [{"element_index": 9, "role": "AXButton", "text": "搜索"}]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"think": "点搜索", "action": "click", "text": "搜索"}
        if len(prompts) == 2:
            return {"think": "填这些单号", "action": "type", "text": ids}
        return {"think": "结束", "done": True}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "KEEP") == 0
    assert "text 写控件名，value 写要填的内容" in prompts[2]
    assert ids not in prompts[2]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert not any(ids in item["basis"] for item in events)


def test_click_on_a_fill_name_focuses_the_entry(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    clicks = []

    class Hand:
        def query(self, app, text, role=""):
            if role == "textfield" and text == "交易号":
                matches = [{"element_index": 12, "role": "AXTextField", "text": "交易号"}]
            elif text == "交易号":
                matches = [
                    {"element_index": 2, "role": "AXButton", "text": "无交易号"},
                    {"element_index": 5, "role": "AXButton", "text": "重转交易号"},
                    {"element_index": 8, "role": "AXButton", "text": "AI分析交易号"},
                ]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            clicks.append(index)

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    turns = [
        {"think": "先点交易号", "action": "click", "text": "交易号"},
        {"think": "结束", "done": True},
    ]
    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "166996404164",
    ) == 0
    assert clicks == ["12"]


def test_skip_before_the_product_page_is_not_a_missing_tab(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "支付宝账单",
        "goal": "在支付宝账单页按交易号查询。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }

    class Hand:
        def query(self, app, text, role=""):
            return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}

        def click(self, app, index):
            raise AssertionError("clicked")

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(),
        lambda _prompt: {"think": "没有这一项", "action": "skip", "text": "当前部署的界面没有这一项"},
        "test", "Microsoft Edge", "",
    ) == 2
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert saved["result_reason"] == "测试页没有打开"
    assert "当前部署的界面没有" not in saved["result_reason"]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("先打开产品页" in item["basis"] for item in events)


def test_show_test_page_does_not_open_another_tab(tmp_path):
    from qa_platform.runner.execute import show_test_page

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    opened = []

    class Hand:
        def list_apps(self):
            return "Microsoft Edge — com.microsoft.edgemac [running]"

        def get_app_state(self, app):
            return {"content": [{"type": "text", "text": f"URL: {BASE}/refund"}]}

    app, error = show_test_page(
        run,
        json.loads((run / "plan.json").read_text(encoding="utf-8")),
        state,
        Hand(),
        None,
        lambda _app, url: opened.append(url),
        BASE,
    )
    assert app == "Microsoft Edge"
    assert error == ""
    assert opened == []
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("不再另开标签" in item["basis"] for item in events)


def test_browser_tab_is_not_clicked(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "查交易号",
        "goal": "在支付宝账单页按交易号查询。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    clicks = []
    values = []
    turns = [
        {"think": "切到业财标签", "action": "click", "text": "业财uat环境 - 内存使用率 - 198 MB"},
        {"think": "填交易号", "action": "type", "text": "交易号", "value": "SO-1"},
        {"think": "结束", "done": True},
    ]

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            if text == "SO-1":
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-1"}]
            elif "内存使用率" in text:
                matches = [{"element_index": 1, "role": "AXRadioButton", "text": text}]
            else:
                matches = [{"element_index": 12, "role": "AXTextField", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            clicks.append(index)

        def set_value(self, app, index, value):
            values.append(value)

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "SO-1",
    ) == 0
    assert "1" not in clicks
    assert values == ["SO-1"]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("不要点浏览器标签页" in item["basis"] for item in events)


def test_login_page_skip_is_not_a_missing_tab(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C2",
        "title": "京东日账单",
        "goal": "在京东日账单页按交易号查询。",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/login"}

        def query(self, app, text, role=""):
            return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}

        def click(self, app, index):
            raise AssertionError("clicked")

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(),
        lambda _prompt: {"think": "登录页", "action": "skip", "text": "当前部署的界面没有这一项"},
        "test", "Microsoft Edge", "",
    ) == 2
    saved = json.loads((run / "cases" / "C2" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert saved["result_reason"] == "测试页没有打开"
    assert "当前部署的界面没有" not in saved["result_reason"]
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("先打开产品页" in item["basis"] for item in events)


def test_case_tab_is_shown_on_the_live_page(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C3",
        "title": "京东钱包",
        "goal": "在京东钱包页按交易号查询。",
        "start": "电商结算单 - 京东钱包",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "交易号"}],
    }
    prompts = []
    values = []
    turns = [
        {"think": "先点页签", "action": "click", "text": "京东钱包"},
        {"think": "填交易号", "action": "type", "text": "交易号", "value": "SO-1"},
        {"think": "结束", "done": True},
    ]

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"URL: {BASE}/refund"}

        def query(self, app, text, role=""):
            if text == "SO-1":
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-1"}]
            elif text == "京东钱包":
                matches = [{"element_index": 7, "role": "AXRadioButton", "text": "京东钱包"}]
            elif text == "交易号":
                matches = [{"element_index": 12, "role": "AXTextField", "text": "交易号"}]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            values.append(value)

    def model(prompt):
        prompts.append(prompt)
        return turns.pop(0)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "SO-1") == 0
    assert "当前页签" in prompts[0]
    assert "京东钱包" in prompts[0]
    assert "这一条要测页签「京东钱包」" in prompts[0]
    assert "还没选中" in prompts[0]
    assert values == ["SO-1"]


def test_visible_page_is_not_skipped_and_the_address_bar_is_left_alone(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "case-amazon",
        "title": "亚马逊账单",
        "goal": "在亚马逊账单页按交易号单号搜索，页面能返回对应交易号的账单明细记录。",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    values = []
    turns = [
        {"think": "改地址去找页面", "action": "type", "text": "地址和搜索栏", "value": "http://example/bill/amazon"},
        {"think": "页签在，填交易号", "action": "type", "text": "交易号", "value": "SO-1"},
        {"think": "结果里已经有单号", "done": True},
    ]

    class Hand:
        def query(self, app, text, role=""):
            if text == "SO-1":
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "SO-1"}]
            elif "亚马逊" in text:
                matches = [{"element_index": 2, "role": "AXRadioButton", "text": "亚马逊账单"}]
            else:
                kind = "AXTextField" if "交易" in text else "AXButton"
                matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            values.append(value)

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0), "test", "Microsoft Edge", "SO-1") == 0
    assert values == ["SO-1"]
    saved = json.loads((run / "cases" / "case-amazon" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] != "skipped"
    events = json.loads((run / "state.json").read_text(encoding="utf-8"))["events"]
    assert any("不要改地址" in item["basis"] for item in events)


def test_browser_chrome_does_not_skip_and_then_blocks_the_run(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "case-amazon",
        "title": "亚马逊账单",
        "goal": "在亚马逊账单页按交易号单号搜索。",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "交易号"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"标签栏\n地址和搜索栏\nURL: {BASE}/"}

        def query(self, app, text, role=""):
            return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}

        def click(self, app, index):
            raise AssertionError("clicked")

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    decision = {"think": "直接打开路径", "action": "type", "text": "地址和搜索栏", "value": "/bill/amazon/dateRange"}
    assert _think_and_act(run, state, case, {"checks": []}, Hand(), lambda _prompt: decision, "test", "Microsoft Edge", "") == 2
    saved = json.loads((run / "cases" / "case-amazon" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert saved["result_reason"] == "测试页没有打开"
    assert "当前部署的界面没有" not in saved["result_reason"]


def test_skip_after_the_surface_was_seen_keeps_the_data_reason(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "case-shop",
        "title": "示例入口",
        "goal": "在示例入口页查询已有数据。",
        "check_id": "K1",
        "status": "running",
        "steps": [
            {"phase": "test", "action": "type", "role": "textbox", "name": "条件"},
            {"phase": "test", "action": "click", "role": "button", "name": "搜索"},
        ],
    }
    turns = [
        {"think": "先点开这一项", "action": "click", "text": "示例入口"},
        {"think": "库里没有数据", "action": "skip", "text": "当前部署的界面没有这一项"},
    ]

    class Hand:
        def query(self, app, text, role=""):
            kind = "AXTextField" if text == "条件" else "AXButton"
            matches = [{"element_index": 4, "role": kind, "text": text}]
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            raise AssertionError("typed")

    assert _think_and_act(
        run, state, case, {"checks": []}, Hand(), lambda _prompt: turns.pop(0),
        "test", "Microsoft Edge", "", empty_data=True,
    ) == 0
    saved = json.loads((run / "cases" / "case-shop" / "case.json").read_text(encoding="utf-8"))
    assert saved["status"] == "skipped"
    assert saved["result_reason"] == "这一条没有库里的数据"
    assert "当前部署的界面没有" not in saved["result_reason"]


def test_goal_waits_for_the_start_and_reports_a_click_that_stays(tmp_path):
    from qa_platform.runner.execute import _think_and_act

    run = build(tmp_path)
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    case = {
        "id": "C1",
        "title": "核对",
        "goal": "核对写入的值已经出现。",
        "start": "甲菜单 - 乙页面",
        "check_id": "K1",
        "status": "running",
        "steps": [{"phase": "test", "action": "type", "role": "textbox", "name": "条件"}],
    }
    ready = {"on": False}
    prompts = []
    url = f"{BASE}/home"

    class Hand:
        def get_app_state(self, app, include_screenshot=False):
            return {"text": f"名称「甲菜单 丙菜单」\nURL: {url}"}

        def query(self, app, text, role=""):
            if text == "V-1":
                matches = [{"element_index": 8, "role": "AXStaticText", "text": "V-1"}]
            elif ready["on"] and text in {"甲菜单", "乙页面", "条件"}:
                kind = "AXTextField" if text == "条件" else "AXButton"
                matches = [{"element_index": 6, "role": kind, "text": text}]
            elif text == "甲菜单":
                matches = [{"element_index": 3, "role": "AXStaticText", "text": "甲菜单 丙菜单"}]
            else:
                matches = []
            return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

        def click(self, app, index):
            return {}

        def set_value(self, app, index, value):
            return {}

    def model(prompt):
        prompts.append(prompt)
        step = len(prompts)
        if step == 1:
            return {"think": "先点菜单", "action": "click", "text": "甲菜单"}
        if step == 2:
            return {"think": "再点一次", "action": "click", "text": "甲菜单"}
        if step == 3:
            ready["on"] = True
            return {"think": "再看这一页", "action": "look", "text": "乙页面"}
        if step == 4:
            return {"think": "填值", "action": "type", "text": "条件", "value": "V-1"}
        return {"think": "结果里有了", "done": True}

    assert _think_and_act(run, state, case, {"checks": []}, Hand(), model, "test", "Microsoft Edge", "V-1") == 0
    assert "当前目标：核对写入的值已经出现。" in prompts[0]
    assert "走到起点" not in prompts[0]
    assert "当前能填" in prompts[0]
    assert "不是这个控件" in prompts[0]
    assert "窗口还在原处" in prompts[1]
    assert "这个动作已经做过，窗口没变" in prompts[2]
    saved = json.loads((run / "cases" / "C1" / "case.json").read_text(encoding="utf-8"))
    assert saved["observation"] == "final"


def test_prepared_values_stay_whole_when_the_model_leaves_them_blank():
    from qa_platform.runner.execute import _data_lines, _fill_value

    prepared = "A-1,A-2,A-3,A-4"
    assert _fill_value("", prepared) == prepared
    assert _fill_value("使用库里已有数据", prepared) == prepared
    assert _fill_value("使用库里已有交易号", prepared) == prepared
    assert _fill_value("只写这个", prepared) == "只写这个"
    text = "\n".join(_data_lines(prepared, False))
    assert "4 条" in text
    for item in ("A-1", "A-2", "A-3", "A-4"):
        assert item in text


def test_page_seed_and_none_do_not_start_browser(tmp_path):
    browsers = []
    for strategy in ("page_seed", "none"):
        run = build(tmp_path / strategy, strategy=strategy)
        code = execute_reuse(
            run,
            session=Session(),
            model=lambda _prompt: (_ for _ in ()).throw(AssertionError("model")),
            browser=lambda *args: browsers.append(args),
        )
        assert code == 0
    assert browsers == []
