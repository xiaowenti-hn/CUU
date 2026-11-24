"""读取需求正文。先走云效只读接口，失败时才附着已登录浏览器。"""

import base64
import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from qa_platform.runner.preflight import block, note, record
from qa_platform.store import read_state, timestamp, write_json

DEFAULT_DOMAIN = "openapi-rdc.aliyuncs.com"
QA_BUNDLES = {"local.qa.platform.browser", "local.qa.runner.browser"}
PREFERRED_BROWSERS = (
    "com.microsoft.edgemac",
    "com.google.chrome",
    "com.apple.safari",
    "company.thebrowser.Browser",
    "org.mozilla.firefox",
    "com.brave.Browser",
)
LIST_LINE = re.compile(r"^(.*?)\s+(?:—|--)\s+(.+?)\s+\[([^\]]*)\]$")
WORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SERIAL = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]*-[0-9]+)(?![0-9])")
FILE_ID = re.compile(r"fileIdentifier=([A-Za-z0-9]+)")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
# 只允许点开折叠，以及展开后向下滚动。不能输入、不能向上或横向滚动。
FORBIDDEN_TOOLS = {
    "type_text", "set_value", "press_key", "drag",
    "perform_secondary_action",
}
ALLOWED_TOOLS = {"list_apps", "get_app_state", "click", "query", "scroll"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_rich_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return html.unescape("".join(parser.parts)).strip()


def yunxiao_domain() -> str | None:
    value = os.environ.get("YUNXIAO_DOMAIN", "").strip() or DEFAULT_DOMAIN
    if DOMAIN_RE.fullmatch(value) is None:
        return None
    return value


def _one_id(values: list[str]) -> str | None:
    if len(values) != 1:
        return None
    item = values[0].strip()
    if WORK_ID.fullmatch(item) is None:
        return None
    return item


def _from_query(text: str) -> str | None:
    if not text:
        return None
    parsed = parse_qs(text, keep_blank_values=False)
    values = parsed.get("openWorkitemIdentifier") or []
    if "openWorkitemIdentifier" in parsed and len(values) != 1:
        return None
    return _one_id(values) if values else None


def _from_path(path: str) -> str | None:
    segments = [item for item in path.split("/") if item]
    found = []
    for marker in ("workitems", "workitem"):
        indexes = [index for index, segment in enumerate(segments) if segment == marker]
        if len(indexes) != 1:
            if indexes:
                return None
            continue
        index = indexes[0]
        if index + 1 >= len(segments):
            return None
        found.append(segments[index + 1])
    if len(found) != 1:
        return None
    return _one_id(found)


def extract_workitem_id(entry: str) -> str | None:
    """只接受明确的工作项 id。路径切分没有样例时不猜。"""
    parts = urlsplit(entry)
    found = []
    for value in (_from_query(parts.query), _from_query(parts.fragment), _from_path(parts.path)):
        if value:
            found.append(value)
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        return None
    return unique[0]


def extract_req_id(entry: str) -> str | None:
    """项目路径里的工作项 id。需求编号 WXLC-2731 这种不是 id。"""
    segments = [item for item in urlsplit(entry).path.split("/") if item]
    found = []
    for index, segment in enumerate(segments):
        if segment != "req" or index + 1 >= len(segments):
            continue
        candidate = segments[index + 1]
        if SERIAL.fullmatch(candidate):
            continue
        if WORK_ID.fullmatch(candidate):
            found.append(candidate)
    if len(found) != 1:
        return None
    return found[0]


def workitem_id_from_entry(entry: str) -> str | None:
    return extract_workitem_id(entry) or extract_req_id(entry)


def _resource_url(entry: str, edition: str, organization_id: str, suffix: str) -> str | None:
    work_id = workitem_id_from_entry(entry)
    domain = yunxiao_domain()
    if not work_id or not domain:
        return None
    item = quote(work_id, safe="")
    if edition == "region":
        path = f"/oapi/v1/projex/workitems/{item}{suffix}"
    elif edition == "center" and organization_id:
        path = f"/oapi/v1/projex/organizations/{quote(organization_id, safe='')}/workitems/{item}{suffix}"
    else:
        return None
    return f"https://{domain}{path}"


def workitem_url(entry: str, edition: str, organization_id: str) -> str | None:
    return _resource_url(entry, edition, organization_id, "")


def comments_url(entry: str, edition: str, organization_id: str) -> str | None:
    return _resource_url(entry, edition, organization_id, "/comments")


def http_get(url: str, token: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"x-yunxiao-token": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return 0, b""


def parse_list_apps(text: str) -> list[tuple[str, str]]:
    rows = []
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        match = LIST_LINE.fullmatch(trimmed)
        if not match:
            continue
        rows.append((match.group(1), match.group(2)))
    return rows


def listed_apps(text: str) -> list[tuple[str, str, bool]]:
    found = []
    for line in text.splitlines():
        trimmed = line.strip()
        match = LIST_LINE.fullmatch(trimmed)
        if not match:
            continue
        flags = [item.strip() for item in match.group(3).split(",")]
        found.append((match.group(1), match.group(2), "running" in flags))
    return found


def _usable(display: str, bundle: str) -> bool:
    return bool(display and bundle and display not in QA_BUNDLES and bundle not in QA_BUNDLES)


def choose_browser(text: str) -> tuple[str, str] | None:
    """配置对不上应用时，从正在运行的普通浏览器里选一个打开入口。"""
    apps = [item for item in listed_apps(text) if _usable(item[0], item[1])]
    running = [item for item in apps if item[2]]
    pool = running or apps
    for bundle_id in PREFERRED_BROWSERS:
        hits = [item for item in pool if item[1] == bundle_id]
        if len(hits) == 1:
            return ("bundle", hits[0][1])
    if len(pool) == 1:
        return ("bundle", pool[0][1])
    return None


def _mentions_app(configured: str, rows: list[tuple[str, str]]) -> bool:
    return any(display == configured or bundle == configured for display, bundle in rows)


def resolve_browser(configured: str, rows: list[tuple[str, str]]) -> tuple[str, str] | None:
    if not configured or configured in QA_BUNDLES:
        return None
    by_bundle = [(display, bundle) for display, bundle in rows if bundle == configured]
    by_name = [(display, bundle) for display, bundle in rows if display == configured]
    if by_bundle and by_name:
        return None
    if len(by_bundle) == 1:
        display, bundle = by_bundle[0]
        if bundle in QA_BUNDLES or display in QA_BUNDLES:
            return None
        return ("bundle", bundle)
    if len(by_name) == 1:
        display, bundle = by_name[0]
        if bundle in QA_BUNDLES or display in QA_BUNDLES:
            return None
        return ("name", display)
    return None


def same_entry_host(entry: str, url: str) -> bool:
    entry_host = urlsplit(entry).hostname
    url_host = urlsplit(url).hostname
    return bool(entry_host and url_host and entry_host.casefold() == url_host.casefold())


def extract_serial(text: str) -> str | None:
    found: list[str] = []
    for match in SERIAL.finditer(text):
        value = match.group(1)
        if value not in found:
            found.append(value)
    if len(found) != 1:
        return None
    return found[0]


_BOOK_TITLE = re.compile(r"《([^》]{1,200})》")
_INDEXED_LINE = re.compile(r"^\d+\s")


def extract_title(text: str, serial: str) -> str:
    """用地址栏里的《标题》。不要把浏览器进程信息当成需求标题。"""
    decoded = unquote(text)
    for line in decoded.splitlines():
        if serial and serial not in line:
            continue
        match = _BOOK_TITLE.search(line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    for match in _BOOK_TITLE.finditer(decoded):
        title = match.group(1).strip()
        if title:
            return title
    for line in decoded.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned == serial:
            continue
        if cleaned.startswith(("App=", "Window:", "The focused")):
            continue
        if _INDEXED_LINE.match(cleaned):
            continue
        return cleaned
    return ""


INDEX_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
_TRAIL = re.compile(r"\s+(?:Value|Frame|Help|Secondary Actions):.*$|, URL:.*$")
_TRAIT = re.compile(r"(?<!\])\((?:settable|string|selected|disabled|boolean|enabled)[^)]*\)")
_ROLES = (
    "弹出式按钮", "文本输入区", "书签文件夹按钮", "全屏幕按钮", "最小化按钮",
    "关闭按钮", "标准窗口", "HTML 内容", "文本栏", "滚动区", "标签组", "分离器",
    "菜单栏", "列表框", "工具栏", "按钮", "链接", "文本", "图像", "表格", "标签",
    "container", "button", "link",
)
_MORE = re.compile(r"^查看更多(?:\s+[⌄∨▼ˇvV>》])?$")
_FOLD = re.compile(
    r"^(?:查看更多|展开更多|展开全文|展开全部|查看全部评论|更多回复|展开)"
    r"(?:\s+[⌄∨▼ˇvV>》])?$"
)
_COMMENT_TAB = re.compile(r"^(?:查看评论|评论(?:\s*[·•・]\s*\d+)?)(?:\s+[⌄∨▼ˇ])?$")
_ACTIVITY = re.compile(r"^动态\s*[&＆]\s*评论")
_HTML_LINE = re.compile(r"^\d+\s+.*HTML 内容")


def tool_allowed(name: str) -> bool:
    return name not in FORBIDDEN_TOOLS and name in ALLOWED_TOOLS


def control_labels(text: str) -> list[tuple[int, str]]:
    found = []
    for line in text.splitlines():
        match = INDEX_LINE.match(line)
        if not match:
            continue
        name = _control_name(match.group(2))
        if name:
            found.append((int(match.group(1)), name))
    return found


def _control_name(rest: str) -> str:
    rest = _TRAIL.sub("", rest).strip()
    rest = _TRAIT.sub("", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    for role in _ROLES:
        if rest == role:
            return ""
        prefix = role + " "
        if rest.startswith(prefix):
            return rest[len(prefix):].strip()
    return rest


def _is_description_more(name: str) -> bool:
    return _MORE.fullmatch(name) is not None


def _is_fold(name: str) -> bool:
    return _FOLD.fullmatch(name) is not None


def _is_comment_tab(name: str) -> bool:
    return _COMMENT_TAB.fullmatch(name) is not None


def _is_activity_tab(name: str) -> bool:
    return _ACTIVITY.match(name) is not None


def _first_index(text: str, predicate) -> int | None:
    for index, name in control_labels(text):
        if predicate(name):
            return index
    return None


def _html_index(text: str) -> int | None:
    for line in text.splitlines():
        match = INDEX_LINE.match(line)
        if match is None or "HTML 内容" not in match.group(2) or "about:blank" in match.group(2):
            continue
        return int(match.group(1))
    return None


def _needs_web_reread(text: str) -> bool:
    """网页节点还没有子控件时，Chromium 往往要再读一次才会露出正文。"""
    if "HTML 内容" not in text:
        return False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if _HTML_LINE.match(stripped) is None:
            continue
        indent = len(line) - len(stripped)
        for later in lines[index + 1:]:
            if not later.strip():
                continue
            return (len(later) - len(later.lstrip(" \t"))) <= indent
        return True
    return False


def _match_label(match: dict) -> str:
    for key in ("title", "value", "name", "label", "text"):
        value = match.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _match_index(match: dict) -> int | None:
    for key in ("element_index", "index"):
        value = match.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _query_index(client, app: str, text: str, predicate) -> int | None:
    query = getattr(client, "query", None)
    if query is None:
        return None
    try:
        data = query(app, text)
    except RuntimeError:
        return None
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return None
    for match in matches:
        if not isinstance(match, dict):
            continue
        label = _match_label(match)
        if not label or not predicate(label):
            continue
        index = _match_index(match)
        if index is not None:
            return index
    return None


def _find(client, app: str, state: dict, predicate, query_text: str) -> int | None:
    index = _first_index(ax_text(state), predicate)
    if index is not None:
        return index
    return _query_index(client, app, query_text, predicate)


def _click_and_reread(client, app: str, index: int, state: dict) -> dict:
    click = getattr(client, "click", None)
    if click is None:
        return state
    click(app, index)
    return client.get_app_state(app)


def _try_click(client, app: str, index: int, state: dict) -> tuple[dict, bool]:
    before = ax_text(state)
    try:
        state = _click_and_reread(client, app, index, state)
    except RuntimeError:
        return state, False
    return state, ax_text(state) != before


def _scroll_down(client, app: str, state: dict, run_dir: Path) -> dict:
    """展开后版面变高，向下滚一屏，让新内容和评论进入画面。"""
    scroll = getattr(client, "scroll", None)
    index = _html_index(ax_text(state))
    if scroll is None or index is None:
        return state
    note(run_dir, step="需求正文", basis="正在向下查看展开后的内容")
    try:
        scroll(app, index, "down", 1)
    except RuntimeError:
        return state
    try:
        return client.get_app_state(app)
    except RuntimeError:
        return state


def _page_has_body(text: str) -> bool:
    if _first_index(text, _is_description_more) is not None:
        return True
    if _first_index(text, _is_comment_tab) is not None:
        return True
    return "业务场景" in text or "描述" in text


def _needs_load_wait(text: str) -> bool:
    if _page_has_body(text):
        return False
    return "App=" in text or "地址和搜索栏" in text


def wait_for_requirement_page(client, app: str, state: dict, run_dir: Path) -> dict:
    """刚打开时页面常常还是白的，这时点不到查看更多。"""
    if not _needs_load_wait(ax_text(state)):
        return state
    note(run_dir, step="需求正文", basis="正在等待需求页面打开")
    for _ in range(12):
        time.sleep(1)
        try:
            state = client.get_app_state(app)
        except RuntimeError:
            return state
        if not _needs_load_wait(ax_text(state)):
            return state
    return state


def expand_requirement(client, app: str, state: dict, run_dir: Path) -> dict:
    """展开描述里的查看更多，再打开评论；评论里还有折叠内容就继续展开。"""
    if _needs_web_reread(ax_text(state)):
        note(run_dir, step="需求正文", basis="正在等待需求页面内容")
        try:
            state = client.get_app_state(app)
        except RuntimeError:
            return state
    if _needs_web_reread(ax_text(state)):
        html_index = _html_index(ax_text(state))
        if html_index is not None:
            note(run_dir, step="需求正文", basis="正在打开需求页面")
            state, _changed = _try_click(client, app, html_index, state)
    for _ in range(4):
        index = _find(client, app, state, _is_description_more, "查看更多")
        if index is None:
            break
        note(run_dir, step="需求正文", basis="正在展开查看更多")
        state, changed = _try_click(client, app, index, state)
        if not changed:
            break
        state = _scroll_down(client, app, state, run_dir)
    index = _find(client, app, state, _is_comment_tab, "评论")
    if index is None:
        for _ in range(3):
            scrolled = _scroll_down(client, app, state, run_dir)
            if ax_text(scrolled) == ax_text(state):
                break
            state = scrolled
            index = _find(client, app, state, _is_comment_tab, "评论")
            if index is not None:
                break
    if index is None:
        activity = _find(client, app, state, _is_activity_tab, "动态")
        if activity is not None:
            note(run_dir, step="需求正文", basis="正在打开评论区")
            state, changed = _try_click(client, app, activity, state)
            if changed:
                index = _find(client, app, state, _is_comment_tab, "评论")
    if index is not None:
        note(run_dir, step="需求正文", basis="正在查看评论")
        state, changed = _try_click(client, app, index, state)
        if not changed:
            return state
    for _ in range(6):
        index = _first_index(ax_text(state), _is_fold)
        if index is None:
            for label in ("查看更多", "展开更多", "展开全文", "展开全部", "查看全部评论", "更多回复", "展开"):
                index = _query_index(client, app, label, _is_fold)
                if index is not None:
                    break
        if index is None:
            break
        note(run_dir, step="需求正文", basis="正在展开评论")
        state, changed = _try_click(client, app, index, state)
        if not changed:
            break
        state = _scroll_down(client, app, state, run_dir)
    return state


def ax_text(result: dict) -> str:
    return "\n".join(
        str(item.get("text", ""))
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    )


def save_requirement_png(run_dir: Path, result: dict) -> str:
    image = None
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        try:
            image = base64.b64decode(item.get("data", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("截图格式或文件完整性校验失败") from exc
        break
    if image is None:
        raise RuntimeError("Computer Use 未返回窗口截图")
    if not (image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) >= 8 and image[-8:-4] == b"IEND"):
        raise RuntimeError("截图格式或文件完整性校验失败")
    destination = run_dir / "images" / "requirement.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(image)
    temporary.replace(destination)
    digest = hashlib.sha256(image).hexdigest()
    state = read_state(run_dir)
    images = state.get("images")
    if not isinstance(images, dict):
        images = {}
    images["requirement"] = {
        "file": "images/requirement.png",
        "sha256": digest,
        "time": timestamp(),
    }
    state["images"] = images
    write_json(run_dir / "state.json", state)
    return digest


def write_body(run_dir: Path, text: str) -> str:
    path = run_dir / "requirement.txt"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return "requirement.txt"


def store_requirement(run_dir: Path, *, requirement_id: str, title: str, source: str,
                      body: str, basis: str) -> None:
    state = read_state(run_dir)
    state["requirement"] = {
        "id": requirement_id,
        "title": title,
        "source": source,
        "body_file": write_body(run_dir, body),
    }
    write_json(run_dir / "state.json", state)
    record(run_dir, exit_code=0, event_status="完成", basis=basis)


def _comment_plain(content: object, content_format: object) -> str:
    if not isinstance(content, str) or not content.strip():
        return ""
    text = content.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            html_value = data.get("htmlValue")
            if isinstance(html_value, str) and html_value.strip():
                return strip_rich_text(html_value) or html_value.strip()
            leaves: list[str] = []

            def walk(node: object) -> None:
                if isinstance(node, str):
                    leaves.append(node)
                elif isinstance(node, list):
                    for part in node:
                        walk(part)
                elif isinstance(node, dict):
                    for part in node.values():
                        walk(part)

            walk(data.get("jsonMLValue"))
            joined = "".join(leaves).strip()
            if joined:
                return joined
    if content_format == "RICHTEXT" or "<" in text:
        return strip_rich_text(text)
    return text


def format_comments(payload: object) -> str:
    if not isinstance(payload, list):
        return ""
    lines = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        name = user.get("name") if isinstance(user.get("name"), str) and user.get("name").strip() else "评论"
        body = _comment_plain(item.get("content"), item.get("contentFormat"))
        if body:
            lines.append(f"{name}：{body}")
    return "\n\n".join(lines)


def _payload_text(payload: dict) -> tuple[str, str, str] | None:
    subject = payload.get("subject")
    description = payload.get("description")
    serial = payload.get("serialNumber")
    if not isinstance(subject, str) or not subject.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(serial, str) or not serial.strip():
        return None
    plain = _comment_plain(description, payload.get("formatType"))
    if not plain:
        return None
    return serial.strip(), subject.strip(), plain


def file_ids(description: str) -> list[str]:
    text = description
    if description.strip().startswith("{"):
        try:
            data = json.loads(description)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("htmlValue"), str):
            text = data["htmlValue"]
    found = []
    for item in FILE_ID.findall(text):
        if item not in found:
            found.append(item)
    return found


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def _download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _save_description_images(run_dir: Path, item_url: str, description: str, token: str, http, download) -> None:
    saved = 0
    for file_id in file_ids(description):
        if saved >= 4:
            break
        status, meta = _http_json(http, f"{item_url}/files/{quote(file_id, safe='')}", token)
        if status != 200 or not isinstance(meta, dict):
            continue
        remote = meta.get("url")
        if not isinstance(remote, str) or not remote.startswith("https://"):
            continue
        try:
            blob = download(remote)
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, ValueError):
            continue
        if not isinstance(blob, bytes):
            continue
        kind = _image_kind(blob)
        if kind is None or len(blob) > 8_000_000:
            continue
        saved += 1
        destination = run_dir / "images" / f"requirement-{saved}.{kind if kind == 'png' else 'jpg'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_bytes(blob)
        temporary.replace(destination)


def open_entry(opener, kind: str, target: str, entry: str) -> bool:
    if not same_entry_host(entry, entry):
        return False
    if kind == "bundle":
        argv = ["open", "-b", target, entry]
    else:
        argv = ["open", "-a", target, entry]
    try:
        return opener(argv) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ocu_app_path(command: str) -> Path | None:
    resolved = Path(command).resolve()
    candidates = [
        resolved.parent.parent / "dist" / "Open Computer Use.app",
        Path.home() / ".grok/npm-global/lib/node_modules/open-computer-use/dist/Open Computer Use.app",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return None


def launch_ocu_app(app: Path) -> str:
    try:
        result = subprocess.run(
            ["open", "-a", str(app)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "打开 Open Computer Use 超时"
    detail = " ".join(part.strip() for part in (result.stderr, result.stdout) if part and part.strip())
    if result.returncode != 0:
        return detail or f"打开 Open Computer Use 失败，退出码 {result.returncode}"
    return ""


def default_open(argv: list[str]) -> int:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    return result.returncode


class Ocu:
    """只允许 list_apps 和 get_app_state。app 不能是 QA 浏览器。"""

    def __init__(self) -> None:
        command = shutil.which("ocu") or shutil.which("open-computer-use")
        if not command:
            command = str(Path.home() / ".grok" / "bin" / "ocu")
        self.command = command
        self.app = ocu_app_path(command)
        self.env = os.environ.copy()
        self.env.pop("DEEPSEEK_API_KEY", None)
        self.env.pop("YUNXIAO_TOKEN", None)
        self.env["OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE"] = "qa-platform"
        self.process: subprocess.Popen | None = None
        self.buffer = bytearray()
        self.call_id = 0
        self.stderr_lines: list[str] = []
        try:
            self._connect(launch=True)
        except BaseException:
            self.close()
            raise

    def _spawn(self) -> None:
        self.buffer = bytearray()
        self.stderr_lines = []
        self.process = subprocess.Popen(
            [self.command, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        pipe = self.process.stderr
        sink = self.stderr_lines

        def drain() -> None:
            if pipe is None:
                return
            try:
                for line in pipe:
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        sink.append(text)
            except Exception:
                return

        threading.Thread(target=drain, daemon=True).start()

    def _connect(self, *, launch: bool) -> None:
        if launch and self.app is not None:
            opened = launch_ocu_app(self.app)
            if opened:
                raise RuntimeError(opened)
        self._spawn()
        try:
            self.rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "qa-platform", "version": "0.1"},
            })
            self._send({"method": "notifications/initialized"})
        except RuntimeError:
            detail = self._failure_text("open-computer-use MCP 已退出")
            self.close()
            if not launch or self.app is None:
                raise RuntimeError(detail)
            opened = launch_ocu_app(self.app)
            if opened:
                raise RuntimeError(opened)
            self._spawn()
            self.rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "qa-platform", "version": "0.1"},
            })
            self._send({"method": "notifications/initialized"})

    def _failure_text(self, fallback: str) -> str:
        tail = " ".join(self.stderr_lines[-2:]).strip()
        if not tail:
            return fallback
        return f"{fallback}：{tail[:160]}"

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        import select
        import time

        self.call_id += 1
        current = self.call_id
        self._send({"id": current, "method": method, "params": params})
        deadline = time.monotonic() + 25
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        while True:
            line, separator, rest = self.buffer.partition(b"\n")
            if separator:
                self.buffer = bytearray(rest)
                result = json.loads(line)
                if result.get("id") != current:
                    continue
                if "error" in result:
                    raise RuntimeError("MCP 调用失败")
                return result["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise RuntimeError("MCP 调用超时")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError(self._failure_text("open-computer-use MCP 已退出"))
            self.buffer.extend(chunk)

    def tool(self, name: str, arguments: dict) -> dict:
        if not tool_allowed(name):
            raise RuntimeError("需求阶段禁止该操作")
        app = arguments.get("app")
        if app in QA_BUNDLES:
            raise RuntimeError("需求阶段不能附着 QA 浏览器")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("MCP 工具返回失败")
        return result

    def list_apps(self) -> str:
        result = self.tool("list_apps", {})
        return "\n".join(
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def get_app_state(self, app: str) -> dict:
        return self.tool("get_app_state", {
            "app": app,
            "include_screenshot": True,
            "text_limit": "max",
            "max_tree_nodes": 4000,
            "max_tree_depth": 96,
        })

    def click(self, app: str, element_index: int) -> None:
        self.tool("click", {"app": app, "element_index": str(element_index)})

    def scroll(self, app: str, element_index: int, direction: str, pages: float = 1) -> None:
        if direction != "down":
            raise RuntimeError("需求阶段只向下滚动")
        self.tool("scroll", {
            "app": app,
            "element_index": str(element_index),
            "direction": "down",
            "pages": pages,
        })

    def query(self, app: str, text: str) -> dict:
        result = self.tool("query", {
            "app": app,
            "text": text,
            "exact": False,
            "limit": 8,
            "max_nodes": 5000,
        })
        if isinstance(result, dict) and isinstance(result.get("matches"), list):
            return result
        raw = ax_text(result)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()


def _http_json(http, url: str, token: str):
    try:
        status, raw = http(url, token)
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        return 0, None
    if status != 200 or not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return status, None


def _read_api(run_dir: Path, entry: str, edition: str, organization_id: str, token: str, http, download) -> bool:
    """只发 GET。读到正文或评论就不再打开浏览器。"""
    item_url = workitem_url(entry, edition, organization_id)
    comment_endpoint = comments_url(entry, edition, organization_id)
    if not item_url and not comment_endpoint:
        return False
    note(run_dir, step="需求正文", basis="正在通过云效接口读取需求和评论")
    serial = extract_serial(entry)
    title = extract_title(unquote(entry), serial or "")
    description = ""
    if item_url:
        _status, payload = _http_json(http, item_url, token)
        parsed = _payload_text(payload) if isinstance(payload, dict) else None
        if parsed:
            serial, title, description = parsed
            raw_description = payload.get("description") if isinstance(payload.get("description"), str) else ""
            if raw_description:
                _save_description_images(run_dir, item_url, raw_description, token, http, download)
    comments = ""
    if comment_endpoint:
        _status, payload = _http_json(http, comment_endpoint, token)
        comments = format_comments(payload)
    if not serial or not (description or comments):
        return False
    parts = []
    if description:
        parts.append(description)
    if comments:
        parts.append("评论\n" + comments)
    if description and comments:
        basis = f"已从云效接口读取需求 {serial} 和评论"
    elif description:
        basis = f"已从云效接口读取需求 {serial}"
    else:
        basis = f"已从云效接口读取需求 {serial} 的评论，工作项正文没有接口权限"
    store_requirement(
        run_dir,
        requirement_id=serial,
        title=title,
        source="api",
        body="\n\n".join(parts),
        basis=basis,
    )
    return True


def read_with_ocu(run_dir: Path, entry: str, configured: str, ocu_factory, opener) -> int:
    if not configured or not same_entry_host(entry, entry):
        return block(run_dir, "没有可用的云效浏览器应用")
    note(run_dir, step="需求正文", basis="正在启动 Open Computer Use")
    client = ocu_factory()
    try:
        listing = client.list_apps()
        rows = parse_list_apps(listing)
        if configured in QA_BUNDLES:
            return block(run_dir, "云效浏览器应用无法唯一对应 list_apps")
        resolved = resolve_browser(configured, rows) if configured else None
        app_for_state = configured
        if resolved is None and configured and _mentions_app(configured, rows):
            return block(run_dir, "云效浏览器应用无法唯一对应 list_apps")
        if resolved is None:
            resolved = choose_browser(listing)
            app_for_state = resolved[1] if resolved else ""
        if resolved is None:
            return block(run_dir, "没有可用的浏览器")
        kind, target = resolved
        if not open_entry(opener, kind, target, entry):
            return block(run_dir, "无法打开云效入口")
        state = client.get_app_state(app_for_state)
        state = wait_for_requirement_page(client, app_for_state, state, run_dir)
        state = expand_requirement(client, app_for_state, state, run_dir)
        save_requirement_png(run_dir, state)
        text = ax_text(state)
        serial = extract_serial(text)
        if not serial:
            return block(run_dir, "页面文本里没有需求号")
        store_requirement(
            run_dir,
            requirement_id=serial,
            title=extract_title(text, serial),
            source="ocu",
            body=text,
            basis=f"已从已登录浏览器读取需求 {serial}",
        )
        return 0
    finally:
        client.close()


def fetch(run_dir: Path, token: str, *, transport=None, ocu_factory=None, opener=None, download=None) -> int:
    state = read_state(run_dir)
    entry = str(state.get("yunxiao_entry") or "")
    edition = str(state.get("yunxiao_edition") or "")
    organization_id = str(state.get("yunxiao_organization_id") or "")
    configured = str(state.get("yunxiao_browser_app") or "")
    token = token.strip()
    http = transport or http_get
    if token and _read_api(run_dir, entry, edition, organization_id, token, http, download or _download_bytes):
        return 0
    if ocu_factory is None:
        ocu_factory = Ocu
    if opener is None:
        opener = default_open
    try:
        return read_with_ocu(run_dir, entry, configured, ocu_factory, opener)
    except RuntimeError as exc:
        detail = " ".join(str(exc).split())
        if not detail or "token" in detail.lower():
            detail = "读取需求页面失败"
        elif not detail.startswith("读取需求页面失败"):
            detail = f"读取需求页面失败：{detail[:180]}"
        return block(run_dir, detail, step="需求正文")
