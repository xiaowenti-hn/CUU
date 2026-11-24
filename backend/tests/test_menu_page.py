from qa_platform.runner.menu_page import attach_page, choose_menu_page

ROWS = [
    {"menu_name": "电商结算单", "route": "/bill/platformBill", "component": "bill/platform-bill"},
    {"menu_name": "货款结算单", "route": "/bill/salesPaymentBill", "component": "bill/sales-payment-bill"},
    {"menu_name": "交易号规则", "route": "/financialSettings/billSetting/tradeIdRule", "component": "financial-settings/bill-settings/trade-id-rule"},
    {"menu_name": "结算账单", "route": "/bill", "component": ""},
]


def test_menu_name_must_appear_as_written():
    text = "从电商结算单按单号查询，单号支持放到500单"
    chosen = choose_menu_page(text, ROWS)
    assert chosen is not None
    assert chosen["path"] == "/bill/platformBill"
    assert chosen["name"] == "电商结算单"
    assert choose_menu_page("业务场景电商账单交易号查询条件", ROWS) is None


def test_two_matches_are_not_guessed():
    rows = ROWS + [
        {"menu_name": "电商账单", "route": "/bill/other", "component": "bill/other"},
    ]
    assert choose_menu_page("电商账单和电商结算单都要看", rows) is None


def test_attach_page_fills_only_when_missing():
    plan = {"ui": {"seed_object_count": 0}}
    chosen = attach_page(plan, "打开电商结算单", ROWS)
    assert chosen["path"] == "/bill/platformBill"
    assert plan["ui"]["pages"][0]["title_text"] == "电商结算单"
    assert attach_page(plan, "打开电商结算单", ROWS) is None
    assert plan["ui"]["pages"][0]["path"] == "/bill/platformBill"
