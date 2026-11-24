import base64
import hashlib
import json
from pathlib import Path

import pytest

from qa_platform.runner.requirement import (
    control_labels,
    extract_title,
    ocu_app_path,
    extract_req_id,
    extract_workitem_id,
    fetch,
    parse_list_apps,
    tool_allowed,
    workitem_url,
)
from qa_platform.runner_proc import launch
from qa_platform.executor import ExecutorSlot

TOKEN = "yunxiao-secret-value"
ENTRY = "https://devops.aliyun.com/projex/project/p1/req#openWorkitemIdentifier=wi-1"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PAGE = "确认退款\n需求号 DSDD-123\n请核对明细\n"


def write_run(tmp: Path, **overrides) -> Path:
    run = tmp / "run-abc123abc123"
    run.mkdir(parents=True)
    state = {
        "id": run.name,
        "stage": "preflight",
        "commit": "abc123",
        "exit_code": 0,
        "events": [],
        "yunxiao_entry": ENTRY,
        "yunxiao_organization_id": "org-1",
        "yunxiao_edition": "center",
        "yunxiao_browser_app": "",
        "requirement": None,
    }
    state.update(overrides)
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run


def load(run: Path) -> dict:
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def assert_token_hidden(run: Path) -> None:
    text = (run / "state.json").read_text(encoding="utf-8")
    assert TOKEN not in text
    progress = run / "执行进度.md"
    if progress.exists():
        assert TOKEN not in progress.read_text(encoding="utf-8")


class FakeOcu:
    def __init__(self, listing: str, app_seen: list):
        self.listing = listing
        self.app_seen = app_seen
        self.calls: list = []

    def list_apps(self) -> str:
        self.calls.append("list_apps")
        return self.listing

    def get_app_state(self, app: str) -> dict:
        self.calls.append(("get_app_state", app))
        self.app_seen.append(app)
        return {
            "content": [
                {"type": "text", "text": PAGE},
                {"type": "image", "data": base64.b64encode(PNG).decode()},
            ]
        }

    def close(self) -> None:
        self.calls.append("close")


def test_extract_title_uses_the_page_title_not_the_browser():
    encoded = (
        "App=com.microsoft.edgemac (pid 481)\n"
        "Window: \"无标题 - Microsoft Edge\", App: Microsoft Edge.\n"
        "10 文本栏 Value: https://devops.aliyun.com/projex/req/WXLC-2731#%20"
        "%E3%80%8A%E3%80%90%E7%9F%B3%E5%A4%B4%E3%80%91%E7%94%B5%E5%95%86%E8%B4%A6%E5%8D%95"
        "%E4%BA%A4%E6%98%93%E5%8F%B7%E6%9F%A5%E8%AF%A2%E6%9D%A1%E4%BB%B6%EF%BC%8C"
        "%E5%8D%95%E5%8F%B7%E6%94%AF%E6%8C%81%E6%94%BE%E5%88%B0500%E5%8D%95%E3%80%8B\n"
    )
    assert extract_title(encoded, "WXLC-2731") == "【石头】电商账单交易号查询条件，单号支持放到500单"
    assert extract_title("确认退款\n需求号 DSDD-123\n", "DSDD-123") == "确认退款"


def test_requirement_tools_allow_expand_clicks_only():
    assert tool_allowed("click")
    assert tool_allowed("query")
    assert tool_allowed("get_app_state")
    assert tool_allowed("list_apps")
    assert not tool_allowed("type_text")
    assert not tool_allowed("press_key")
    assert tool_allowed("scroll")
    assert not tool_allowed("set_value")
    assert not tool_allowed("drag")


def test_control_labels_read_edge_web_lines():
    text = "\n".join([
        "9 弹出式按钮 (settable, string) 查看站点信息",
        "12 link 查看更多",
        "20 按钮 评论 · 1",
        "30 link 展开全文",
        "41 HTML 内容 云效, URL: https://devops.aliyun.com/projex/req/DSDD-123",
        "76 查看",
        "77 文本 (selected) GPG 公钥",
    ])
    labels = dict(control_labels(text))
    assert labels[9] == "查看站点信息"
    assert labels[12] == "查看更多"
    assert labels[20] == "评论 · 1"
    assert labels[30] == "展开全文"
    assert labels[76] == "查看"
    assert labels[77] == "GPG 公钥"
    assert "查看更多" not in labels[41]


def _page(text: str) -> dict:
    return {
        "content": [
            {"type": "text", "text": text},
            {"type": "image", "data": base64.b64encode(PNG).decode()},
        ]
    }


def test_requirement_expands_more_and_comments(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    pages = [
        "9 弹出式按钮 (settable, string) 查看站点信息\n12 链接 (settable, string) 查看更多\n需求号 DSDD-123\n",
        "20 按钮 (settable, string) 评论 · 1\n需求号 DSDD-123\n描述全文已经展开\n",
        "30 按钮 (settable, string) 展开全文\n需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
        "需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
    ]

    class Expanding:
        def __init__(self):
            self.stage = 0
            self.clicks = []

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            text = pages[min(self.stage, len(pages) - 1)]
            return {
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image", "data": base64.b64encode(PNG).decode()},
                ]
            }

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def close(self) -> None:
            return None

    client = Expanding()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.clicks == [12, 20, 30]
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "描述全文已经展开" in body
    assert "评论：请改成500单" in body
    events = "\n".join(item.get("basis", "") for item in load(run)["events"])
    assert "正在展开查看更多" in events
    assert "正在查看评论" in events
    assert "正在展开评论" in events


def test_blank_browser_waits_until_the_page_can_expand(tmp_path, monkeypatch):
    monkeypatch.setattr("qa_platform.runner.requirement.time.sleep", lambda _seconds: None)
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    blank = "\n".join([
        "App=com.microsoft.edgemac (pid 481)",
        "10 文本栏 (settable, string) 地址和搜索栏 Value: https://devops.aliyun.com/projex/req/DSDD-123",
        "71 标签 (selected) 无标题 Value: on",
    ])
    pages = [
        "12 link 查看更多\n需求号 DSDD-123\n",
        "20 按钮 评论 · 1\n需求号 DSDD-123\n业务场景\n",
        "需求号 DSDD-123\n业务场景\n评论：请改成500单\n",
    ]

    class SlowPage:
        def __init__(self):
            self.reads = 0
            self.clicks = []
            self.stage = 0

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            self.reads += 1
            if self.reads < 4:
                return _page(blank)
            return _page(pages[min(self.stage, len(pages) - 1)])

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def close(self) -> None:
            return None

    client = SlowPage()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.reads >= 4
    assert client.clicks[:2] == [12, 20]
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "评论：请改成500单" in body
    assert "正在等待需求页面打开" in "\n".join(item.get("basis", "") for item in load(run)["events"])


def test_expand_scrolls_down_after_the_layout_changes(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    folded = "41 HTML 内容 云效, URL: https://devops.aliyun.com/projex/req/DSDD-123\n\t12 link 查看更多\n需求号 DSDD-123\n"
    pushed = "41 HTML 内容 云效\n\t12 文本 描述已经展开\n需求号 DSDD-123\n"
    revealed = "41 HTML 内容 云效\n\t20 按钮 评论 · 1\n需求号 DSDD-123\n描述已经展开\n评论区在下方\n"
    opened = "41 HTML 内容 云效\n\t20 文本 评论：请改成500单\n需求号 DSDD-123\n"

    class Scrolling:
        def __init__(self):
            self.clicks = []
            self.scrolls = []
            self.stage = 0

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            if self.scrolls and self.stage == 1:
                return _page(revealed)
            pages = (folded, pushed, opened)
            return _page(pages[min(self.stage, len(pages) - 1)])

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def scroll(self, app: str, index: int, direction: str, pages: float) -> None:
            self.scrolls.append((index, direction, pages))

        def close(self) -> None:
            return None

    client = Scrolling()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.clicks[0] == 12
    assert client.scrolls[0] == (41, "down", 1)
    assert 20 in client.clicks
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "评论：请改成500单" in body
    events = "\n".join(item.get("basis", "") for item in load(run)["events"])
    assert "正在向下查看展开后的内容" in events


def test_empty_web_tree_is_reread_before_expand(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    pages = [
        "12 link 查看更多\n需求号 DSDD-123\n",
        "20 按钮 评论 · 1\n需求号 DSDD-123\n描述全文已经展开\n",
        "30 link 展开全文\n需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
        "需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
    ]

    class LazyWeb:
        def __init__(self):
            self.reads = 0
            self.clicks = []
            self.stage = 0

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            self.reads += 1
            if self.reads == 1:
                return _page("41 HTML 内容 云效, URL: https://devops.aliyun.com/projex/req/DSDD-123\n需求号 DSDD-123\n")
            return _page(pages[min(self.stage, len(pages) - 1)])

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def close(self) -> None:
            return None

    client = LazyWeb()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.reads >= 2
    assert client.clicks == [12, 20, 30]
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "评论：请改成500单" in body


def test_empty_web_tree_is_focused_when_the_reread_stays_empty(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    bare = "41 HTML 内容 云效, URL: https://devops.aliyun.com/projex/req/DSDD-123\n需求号 DSDD-123\n"
    pages = [
        "12 link 查看更多\n需求号 DSDD-123\n",
        "20 按钮 评论 · 1\n需求号 DSDD-123\n描述全文已经展开\n",
        "需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
    ]

    class StillEmpty:
        def __init__(self):
            self.reads = 0
            self.clicks = []
            self.stage = 0

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            self.reads += 1
            if self.reads < 3:
                return _page(bare)
            return _page(pages[min(self.stage, len(pages) - 1)])

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            if index != 41:
                self.stage += 1

        def close(self) -> None:
            return None

    client = StillEmpty()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.clicks[0] == 41
    assert 12 in client.clicks and 20 in client.clicks
    assert "评论：请改成500单" in (run / "requirement.txt").read_text(encoding="utf-8")


def test_comment_query_skips_the_activity_tab(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    pages = [
        "需求号 DSDD-123\n页面已打开\n",
        "需求号 DSDD-123\n描述全文已经展开\n",
        "需求号 DSDD-123\n描述全文已经展开\n评论区已打开\n",
        "需求号 DSDD-123\n描述全文已经展开\n评论：请改成500单\n",
    ]

    class QueryClient:
        def __init__(self):
            self.stage = 0
            self.clicks = []

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            return _page(pages[min(self.stage, len(pages) - 1)])

        def query(self, app: str, text: str) -> dict:
            if self.stage == 0 and text == "查看更多":
                return {"matches": [{"index": 12, "title": "查看更多"}]}
            if self.stage == 1 and text == "评论":
                return {"matches": [
                    {"index": 3, "title": "动态&评论"},
                    {"index": 20, "title": "评论 · 1"},
                ]}
            if self.stage == 2 and text == "展开全文":
                return {"matches": [{"index": 30, "title": "展开全文"}]}
            return {"matches": []}

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def close(self) -> None:
            return None

    client = QueryClient()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.clicks == [12, 20, 30]
    assert 3 not in client.clicks
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "评论：请改成500单" in body


def test_activity_tab_opens_before_the_comment_tab(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    pages = [
        "4 按钮 动态&评论\n需求号 DSDD-123\n描述已经展开\n",
        "20 按钮 评论 · 1\n需求号 DSDD-123\n描述已经展开\n",
        "需求号 DSDD-123\n描述已经展开\n评论：单号放到500\n",
    ]

    class ActivityFirst:
        def __init__(self):
            self.stage = 0
            self.clicks = []

        def list_apps(self) -> str:
            return "Microsoft Edge — com.microsoft.edgemac [running]\n"

        def get_app_state(self, app: str) -> dict:
            return _page(pages[min(self.stage, len(pages) - 1)])

        def click(self, app: str, index: int) -> None:
            self.clicks.append(index)
            self.stage += 1

        def close(self) -> None:
            return None

    client = ActivityFirst()
    assert fetch(run, "", ocu_factory=lambda: client, opener=lambda argv: 0) == 0
    assert client.clicks == [4, 20]
    assert "评论：单号放到500" in (run / "requirement.txt").read_text(encoding="utf-8")


def test_ocu_app_path_uses_the_package_bundle(tmp_path):
    package = tmp_path / "open-computer-use"
    binary = package / "bin" / "ocu"
    app = package / "dist" / "Open Computer Use.app"
    binary.parent.mkdir(parents=True)
    app.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    assert ocu_app_path(str(binary)) == app


def test_png_fixture_matches_magic():
    assert PNG.startswith(b"\x89PNG\r\n\x1a\n")
    assert PNG[-8:-4] == b"IEND"


def test_token_success_uses_center_path_and_strips_rich_text(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_DOMAIN", "example.test")
    run = write_run(tmp_path)
    seen = {"urls": []}

    def transport(url: str, token: str):
        seen["urls"].append(url)
        seen["token"] = token
        if url.endswith("/comments"):
            return 200, json.dumps([{
                "content": json.dumps({"htmlValue": "请改成500单"}),
                "contentFormat": "RICHTEXT",
                "user": {"name": "阿宁"},
            }]).encode()
        body = {
            "id": "wi-1",
            "subject": "确认退款",
            "description": "<p>确认 <b>退款</b></p>",
            "formatType": "RICHTEXT",
            "serialNumber": "DSDD-123",
        }
        return 200, json.dumps(body).encode()

    def ocu_factory():
        raise AssertionError("令牌成功时不应打开浏览器")

    assert fetch(run, TOKEN, transport=transport, ocu_factory=ocu_factory) == 0
    assert seen["token"] == TOKEN
    assert seen["urls"] == [
        "https://example.test/oapi/v1/projex/organizations/org-1/workitems/wi-1",
        "https://example.test/oapi/v1/projex/organizations/org-1/workitems/wi-1/comments",
    ]
    state = load(run)
    assert state["requirement"]["id"] == "DSDD-123"
    assert state["requirement"]["title"] == "确认退款"
    assert state["requirement"]["source"] == "api"
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert body == "确认 退款\n\n评论\n阿宁：请改成500单"
    assert "<p>" not in body
    assert_token_hidden(run)


def test_region_omits_organization(tmp_path, monkeypatch):
    monkeypatch.delenv("YUNXIAO_DOMAIN", raising=False)
    run = write_run(tmp_path, yunxiao_edition="region", yunxiao_organization_id="")
    seen = {"urls": []}

    def transport(url: str, token: str):
        seen["urls"].append(url)
        if url.endswith("/comments"):
            return 200, b"[]"
        return 200, json.dumps({
            "subject": "标题",
            "description": "纯文本",
            "serialNumber": "DSDD-9",
        }).encode()

    assert fetch(run, TOKEN, transport=transport, ocu_factory=lambda: pytest.fail("no ocu")) == 0
    assert seen["urls"][0] == "https://openapi-rdc.aliyuncs.com/oapi/v1/projex/workitems/wi-1"
    assert seen["urls"][1] == "https://openapi-rdc.aliyuncs.com/oapi/v1/projex/workitems/wi-1/comments"
    assert "/organizations/" not in seen["urls"][0]


def test_non_200_falls_back_to_ocu(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    opened = []
    apps = []

    def transport(url: str, token: str):
        assert token == TOKEN
        return 503, b"nope"

    def opener(argv: list[str]) -> int:
        opened.append(argv)
        return 0

    def factory():
        return FakeOcu("Microsoft Edge — com.microsoft.edgemac [running]\n", apps)

    assert fetch(run, TOKEN, transport=transport, ocu_factory=factory, opener=opener) == 0
    state = load(run)
    assert state["requirement"]["source"] == "ocu"
    assert state["requirement"]["id"] == "DSDD-123"
    assert state["requirement"]["title"] == "确认退款"
    image = (run / "images" / "requirement.png").read_bytes()
    assert image == PNG
    assert state["images"]["requirement"]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert opened == [["open", "-b", "com.microsoft.edgemac", ENTRY]]
    assert apps == ["com.microsoft.edgemac"]
    assert_token_hidden(run)


def test_ocu_opens_by_bundle_id(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.microsoft.edgemac")
    opened = []
    apps = []
    listing = "\n Microsoft Edge — com.microsoft.edgemac [running, last-used=2026-11-12] \n不是可用行\n"

    def opener(argv):
        opened.append(argv)
        return 0

    assert fetch(
        run, "",
        transport=lambda *_: pytest.fail("无令牌不应请求云效"),
        ocu_factory=lambda: FakeOcu(listing, apps),
        opener=opener,
    ) == 0
    assert opened == [["open", "-b", "com.microsoft.edgemac", ENTRY]]
    assert apps == ["com.microsoft.edgemac"]
    assert load(run)["requirement"]["source"] == "ocu"
    assert (run / "images" / "requirement.png").read_bytes() == PNG


def test_ocu_opens_by_display_name(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="Microsoft Edge")
    opened = []
    apps = []
    listing = "Microsoft Edge -- com.microsoft.edgemac [running]\n"

    def opener(argv):
        opened.append(argv)
        return 0

    assert fetch(
        run, "",
        transport=lambda *_: pytest.fail("无令牌不应请求云效"),
        ocu_factory=lambda: FakeOcu(listing, apps),
        opener=opener,
    ) == 0
    assert opened == [["open", "-a", "Microsoft Edge", ENTRY]]
    assert apps == ["Microsoft Edge"]
    assert load(run)["requirement"]["id"] == "DSDD-123"


def test_unknown_browser_name_opens_a_running_browser(tmp_path):
    run = write_run(
        tmp_path,
        yunxiao_browser_app="https://devops.aliyun.com/projex/project/abc/bug/def",
    )
    opened = []
    apps = []
    listing = (
        "Google Chrome — com.google.chrome [running]\n"
        "Safari — com.apple.safari []\n"
        "QA Platform Browser — local.qa.platform.browser [running]\n"
    )

    def opener(argv):
        opened.append(argv)
        return 0

    assert fetch(
        run, "",
        transport=lambda *_: pytest.fail("无令牌不应请求云效"),
        ocu_factory=lambda: FakeOcu(listing, apps),
        opener=opener,
    ) == 0
    assert opened == [["open", "-b", "com.google.chrome", ENTRY]]
    assert apps == ["com.google.chrome"]
    assert load(run)["requirement"]["id"] == "DSDD-123"


def test_ocu_ambiguous_match_blocks(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="com.example.browser")
    opened = []
    listing = (
        "Edge — com.example.browser [running]\n"
        "com.example.browser — com.other.app [running]\n"
    )

    def opener(argv):
        opened.append(argv)
        return 0

    def factory():
        client = FakeOcu(listing, [])
        original = client.get_app_state

        def refuse(app):
            raise AssertionError(app)

        client.get_app_state = refuse
        client._original = original
        return client

    assert fetch(run, "", transport=lambda *_: (200, b"{}"), ocu_factory=factory, opener=opener) == 2
    assert opened == []
    assert load(run)["requirement"] is None
    assert load(run)["exit_code"] == 2


def test_ocu_rejects_qa_browser_bundle(tmp_path):
    listing = "QA Platform Browser — local.qa.platform.browser [running]\nRunner — local.qa.runner.browser [running]\n"
    for configured in ("local.qa.platform.browser", "local.qa.runner.browser"):
        run = write_run(tmp_path / configured, yunxiao_browser_app=configured)
        opened = []

        def opener(argv, opened=opened):
            opened.append(argv)
            return 0

        def factory(opened=opened):
            client = FakeOcu(listing, [])
            client.get_app_state = lambda app: pytest.fail(app)
            return client

        assert fetch(run, "", ocu_factory=factory, opener=opener) == 2
        assert opened == []
        assert load(run)["requirement"] is None


def test_unparsed_line_cannot_open_app(monkeypatch):
    monkeypatch.setenv("YUNXIAO_DOMAIN", "example.test")
    rows = parse_list_apps("整行都是显示名，没有 bundle\nEdge — com.example.app [running]\n")
    assert rows == [("Edge", "com.example.app")]
    assert extract_workitem_id("https://devops.aliyun.com/projex/req/JEOOMS-120368") is None
    assert extract_req_id("https://devops.aliyun.com/projex/req/JEOOMS-120368") is None
    assert extract_req_id(
        "https://devops.aliyun.com/projex/project/p1/req/e1218910e6d8440282d45cc711"
    ) == "e1218910e6d8440282d45cc711"
    assert workitem_url(
        "https://devops.aliyun.com/projex/req/JEOOMS-120368",
        "center",
        "org-1",
    ) is None
    assert workitem_url(
        "https://devops.aliyun.com/projex/project/p1/req/e1218910e6d8440282d45cc711",
        "center",
        "org-1",
    ) == "https://example.test/oapi/v1/projex/organizations/org-1/workitems/e1218910e6d8440282d45cc711"


def test_api_unpacks_rich_description_and_saves_its_image(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_DOMAIN", "example.test")
    run = write_run(tmp_path)
    description = json.dumps({
        "htmlValue": (
            "<p>业务场景</p>"
            '<img src="https://devops.aliyun.com/projex/api/workitem/file/url?fileIdentifier=abc123">'
        ),
    }, ensure_ascii=False)

    def transport(url: str, token: str):
        assert token == TOKEN
        if url.endswith("/files/abc123"):
            return 200, json.dumps({"url": "https://files.test/a.png", "suffix": "png"}).encode()
        if url.endswith("/comments"):
            return 200, b"[]"
        return 200, json.dumps({
            "subject": "确认退款",
            "description": description,
            "formatType": "RICHTEXT",
            "serialNumber": "DSDD-123",
        }).encode()

    def download(url: str) -> bytes:
        assert url == "https://files.test/a.png"
        return PNG

    assert fetch(
        run, TOKEN, transport=transport, download=download,
        ocu_factory=lambda: pytest.fail("接口成功时不应打开浏览器"),
    ) == 0
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert "业务场景" in body
    assert "htmlValue" not in body
    assert (run / "images" / "requirement-1.png").read_bytes() == PNG
    assert_token_hidden(run)


def test_comments_are_kept_when_the_workitem_body_is_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_DOMAIN", "example.test")
    entry = "https://devops.aliyun.com/projex/project/p1/req/e1218910e6d8440282d45cc711#WXLC-2731 《账单查询》"
    run = write_run(tmp_path, yunxiao_entry=entry)

    def transport(url: str, token: str):
        assert token == TOKEN
        if url.endswith("/comments"):
            return 200, json.dumps([{
                "content": json.dumps({"htmlValue": "<p>环境：uat</p>"}),
                "contentFormat": "RICHTEXT",
                "user": {"name": "阿宁"},
            }]).encode()
        return 403, b""

    assert fetch(run, TOKEN, transport=transport, ocu_factory=lambda: pytest.fail("有评论就不再打开浏览器")) == 0
    state = load(run)
    assert state["requirement"]["source"] == "api"
    assert state["requirement"]["id"] == "WXLC-2731"
    assert state["requirement"]["title"] == "账单查询"
    body = (run / "requirement.txt").read_text(encoding="utf-8")
    assert body == "评论\n阿宁：环境：uat"
    assert "没有接口权限" in state["events"][-1]["basis"]
    assert_token_hidden(run)


def test_page_without_serial_does_not_use_title(tmp_path):
    run = write_run(tmp_path, yunxiao_browser_app="Microsoft Edge")

    class TitleOnly(FakeOcu):
        def get_app_state(self, app: str) -> dict:
            return {
                "content": [
                    {"type": "text", "text": "确认退款"},
                    {"type": "image", "data": base64.b64encode(PNG).decode()},
                ]
            }

    assert fetch(
        run, "",
        ocu_factory=lambda: TitleOnly("Microsoft Edge — com.microsoft.edgemac [running]\n", []),
        opener=lambda argv: 0,
    ) == 2
    assert load(run)["requirement"] is None


def test_launch_strips_secret_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deep-secret")
    monkeypatch.setenv("YUNXIAO_TOKEN", TOKEN)
    monkeypatch.setenv("YUNXIAO_DOMAIN", "example.test")
    seen = {}

    class Stdin:
        def __init__(self):
            self.buf = b""

        def write(self, data: bytes) -> None:
            self.buf += data

        def close(self) -> None:
            return None

    class Process:
        def __init__(self):
            self.stdin = Stdin()

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        process = Process()
        seen["process"] = process
        return process

    monkeypatch.setattr("qa_platform.runner_proc.subprocess.Popen", fake_popen)
    run = tmp_path / "run-abc123abc123"
    run.mkdir()
    launch(ExecutorSlot(), run, "prj-abc123abc123")
    assert "DEEPSEEK_API_KEY" not in seen["env"]
    assert "YUNXIAO_TOKEN" not in seen["env"]
    assert seen["env"]["YUNXIAO_DOMAIN"] == "example.test"
    assert TOKEN not in " ".join(seen["cmd"])
    assert b"deep-secret" in seen["process"].stdin.buf
    assert TOKEN.encode() in seen["process"].stdin.buf
