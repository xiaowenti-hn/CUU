"""页面执行。reuse 只跑已确认基线的 phase=test。
page_seed 只跑已确认基线的 phase=seed，查完停在 baseline_actual。
SQL 只在改数退出码为 0 且 baseline_actual 已批准后跑 phase=test。不封口，不写报告。"""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from qa_platform.runner.db import QueryError, read_only_query
from qa_platform.runner.menu_page import fetch_menus, load_menus, pick_path
from qa_platform.runner.ocu import APP, CACHE, OcuError, assert_product_js, browser_argv, ensure_browser
from qa_platform.runner.preflight import note, record
from qa_platform.store import find_approval, read_state, timestamp, write_json

URL_RE = re.compile(r"https?://[^\s'\"`)]+")
DANGER = ("发送", "删除", "付款")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def navigation_allowed(url: str, base_url: str) -> bool:
    target = urlparse(url)
    base = urlparse(base_url)
    scheme = (target.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False
    if not target.hostname or not base.hostname:
        return False
    if target.hostname.lower() != base.hostname.lower():
        return False
    if base.port is not None:
        return target.port == base.port
    if scheme == "https":
        return target.port in {None, 443}
    return target.port in {None, 80}


def page_path(pages: list) -> str | None:
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        path = str(page.get("path") or "")
        if path.startswith("/") and not path.startswith("//") and "://" not in path:
            return path
    return None


def start_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def opening_path(run_dir: Path, plan: dict, state: dict, query) -> str | None:
    """方案里的路径优先。没有时按菜单名对需求，只在对上唯一一条时使用。"""
    from qa_platform.runner.baseline import _requirement_text

    requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else {}
    title = str(requirement.get("title") or "") if isinstance(requirement, dict) else ""
    body = _requirement_text(run_dir, state)
    text = "\n".join(part for part in (title, body) if part)
    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else {}
    pages = ui.get("pages") if isinstance(ui.get("pages"), list) else []
    rows: list = []
    if page_path(pages) is None:
        allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
        if len(allowed) == 1:
            database = allowed[0]
            try:
                rows = fetch_menus(database, query) if query is not None else load_menus(database)
            except (QueryError, OSError, TypeError):
                rows = []
    path, basis = pick_path(plan, text, rows)
    if basis:
        note(run_dir, step="执行", basis=basis)
    base_url = str(state.get("base_url") or "")
    if not path or not navigation_allowed(start_url(base_url, path), base_url):
        return None
    return path


def _load_json(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reuse_ready(run_dir: Path, state: dict) -> bool:
    baseline = _load_json(run_dir / "baseline.json")
    if not isinstance(baseline, dict) or baseline.get("strategy") != "reuse":
        return False
    return find_approval(state, "baseline", baseline) is not None


def _visible_copy(plan: dict) -> list[str]:
    found = []
    ui = plan.get("ui") if isinstance(plan.get("ui"), dict) else {}
    for page in ui.get("pages") or []:
        if not isinstance(page, dict):
            continue
        if page.get("title_text"):
            found.append(str(page["title_text"]))
        for control in page.get("controls") or []:
            if isinstance(control, dict) and control.get("name"):
                found.append(str(control["name"]))
    return found


NO_OPEN_PAGE = "没有已打开的浏览器。请先打开 Chrome 或 Edge 并登录测试站"


def _tool_text(result) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


BROWSER_BUNDLE = {
    "edge": "com.microsoft.edgemac",
    "chrome": "com.google.chrome",
}
BROWSER_LABEL = {"edge": "Edge", "chrome": "Chrome"}


def _chosen_browser(session, base_url: str, choice: str = "") -> tuple[str, str] | None:
    """选定 edge 或 chrome 时只用那一个。返回显示名和 bundle id。"""
    from qa_platform.runner.requirement import PREFERRED_BROWSERS, QA_BUNDLES, listed_apps

    host = (urlparse(base_url).hostname or "").casefold()
    if not host or session is None or not hasattr(session, "list_apps"):
        return None
    try:
        listing = session.list_apps()
    except (OSError, OcuError, TypeError):
        return None
    apps = [
        item for item in listed_apps(_tool_text(listing))
        if item[2] and item[0] not in QA_BUNDLES and item[1] not in QA_BUNDLES
    ]
    wanted = BROWSER_BUNDLE.get(choice)
    if wanted:
        matched = [item for item in apps if item[1] == wanted]
        display = matched[0][0] if matched else ("Microsoft Edge" if choice == "edge" else "Google Chrome")
        return display, wanted
    ordered = []
    for bundle_id in PREFERRED_BROWSERS:
        ordered.extend(item for item in apps if item[1] == bundle_id)
    ordered.extend(item for item in apps if item[1] not in PREFERRED_BROWSERS)
    fallback: tuple[str, str] | None = None
    for display, bundle, _running in ordered:
        if fallback is None:
            fallback = (display, bundle)
        try:
            state = session.get_app_state(display)
        except (OSError, OcuError, TypeError):
            continue
        if host in _tool_text(state).casefold():
            return display, bundle
    return fallback if fallback else None


def open_browser_name(session, base_url: str, choice: str = "") -> str | None:
    """选定 edge 或 chrome 时只用那一个。没选时优先停在测试站上的浏览器。"""
    found = _chosen_browser(session, base_url, choice)
    return found[0] if found else None


def open_in_app(app: str, url: str, bundle: str = "") -> None:
    """按 bundle id 打开，避免选了 Edge 却落到默认的 Chrome。"""
    if bundle:
        argv = ["open", "-b", bundle, url]
    else:
        argv = ["open", "-a", app, url]
    result = subprocess.run(argv, check=False, timeout=20, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "浏览器没有打开").strip()
        raise OSError(detail)


def _page_shows_url(text: str, url: str) -> bool:
    target = (url or "").rstrip("/")
    if not target:
        return False
    blob = text or ""
    if target in blob:
        return True
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = (parsed.path or "").rstrip("/")
    return bool(host and host in blob.casefold() and path and path in blob)


def show_test_page(run_dir: Path, plan: dict, state: dict, session, query, browser, base_url: str) -> tuple[str | None, str]:
    choice = str(state.get("product_browser") or "")
    found = _chosen_browser(session, base_url, choice)
    if not found:
        label = BROWSER_LABEL.get(choice)
        if label:
            return None, f"{label} 没有打开"
        return None, NO_OPEN_PAGE
    app, bundle = found
    path = opening_path(run_dir, plan, state, query)
    url = start_url(base_url, path) if path else base_url.rstrip("/") + "/"
    note(run_dir, step="执行", basis=f"正在用「{app}」打开 {url}")
    seen = ""
    getter = getattr(session, "get_app_state", None)
    if callable(getter):
        try:
            raw = getter(app)
        except (OSError, OcuError, TypeError):
            raw = {}
        seen = raw if isinstance(raw, str) else _tool_text(raw)
        if not seen and isinstance(raw, dict):
            seen = str(raw.get("text") or "")
    if _page_shows_url(seen, url):
        note(run_dir, step="执行", basis="测试页已经打开，不再另开标签")
        return app, ""
    try:
        if browser is not None:
            browser(app, url)
        else:
            open_in_app(app, url, bundle)
    except OSError as exc:
        return None, f"没有在「{app}」里打开页面：{exc}"
    return app, ""


def observe(result: dict) -> dict:
    """把 computer use 的返回收成页面文字和截图。测试里直接给的 text 保持原样。"""
    if isinstance(result, dict) and "content" not in result:
        image = result.get("screenshot") or b""
        return {
            "text": str(result.get("text") or ""),
            "url": str(result.get("url") or ""),
            "screenshot": image if isinstance(image, (bytes, bytearray)) else b"",
        }
    texts: list[str] = []
    image = b""
    if isinstance(result, dict):
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            if item.get("type") == "image" and isinstance(item.get("data"), str):
                import base64
                try:
                    image = base64.b64decode(item["data"])
                except ValueError:
                    image = b""
    return {"text": "\n".join(part for part in texts if part)[:4000], "url": "", "screenshot": image}


def _case_prompt(case: dict, plan: dict, base_url: str, phase: str = "test", typed: str = "", app: str = APP, seen: str = "") -> str:
    steps = []
    for index, step in enumerate(case.get("steps") or []):
        if not isinstance(step, dict) or step.get("phase") != phase:
            continue
        value = typed if typed and step.get("action") == "type" else (step.get("value") or "")
        steps.append(
            f"{index}. {step.get('action')} {step.get('role')} {step.get('name')} "
            f"value={value} "
            f"locator_status={step.get('locator_status')} effect={step.get('effect') or 'read'} "
            f"expect={step.get('expect_text') or []}"
        )
    return "\n".join([
        f"用例 {case.get('id')} {case.get('title')}",
        "步骤是任务说明，不是唯一点击脚本。待定位不是拒绝点击的理由。",
        f"硬边界：app 只能是 {app}。只用 js 和 js_reset。",
        "测试页已经在这个浏览器里打开。就在这个页面里操作，不要再换浏览器。",
        f"js 必须用 computer use 操作这个浏览器，例如 let app = await cua.getApp('{app}'); let state = await app.getAXState(); let found = await app.query({{text: '页面上的文字', role: 'textbox'}});",
        "先 getApp 或 getAXState，再用 query({text, role}) 或刚刚读到的 element_index。",
        "编号过期就再读一次。连续动作和随后的 getAXState 放在同一次 js 里。",
        f"只能留在 {base_url}。数据库只能查询。effect 必须是 read，不要提交、不要改数、不要造数。",
        "发送、删除、付款、保存都不做。",
        "可见文案：" + "、".join(_visible_copy(plan)),
        "步骤：",
        *steps,
        "上一轮页面读到：" + (seen[:1500] if seen else "还没有。必须先执行 js，按步骤操作这个浏览器，不能直接 done。"),
    ])


def _write_case(run_dir: Path, case: dict) -> None:
    folder = run_dir / "cases" / str(case["id"])
    write_json(folder / "case.json", case)


def _save_shot(run_dir: Path, case_id: str, index: int, image: bytes) -> dict | None:
    if not image.startswith(PNG_MAGIC):
        return None
    relative = f"cases/{case_id}/round-{index}.png"
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return {"file": relative, "sha256": hashlib.sha256(image).hexdigest()}


def _remember(run_dir: Path, state: dict, case: dict, summary: str, seen: str, shot: dict | None) -> None:
    rounds = case.get("rounds")
    if not isinstance(rounds, list):
        rounds = []
    rounds.append({
        "time": timestamp_now(),
        "summary": summary,
        "seen_text": seen,
        "screenshot": shot,
    })
    case["rounds"] = rounds
    case["seen"] = seen
    case["observed"] = True
    _write_case(run_dir, case)
    state["current_case"] = case["id"]
    state["current_seen"] = seen
    state["stage"] = "execute"
    write_json(run_dir / "state.json", state)


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    found = []
    raw = text or ""
    index = 0
    while True:
        start = raw.find("{", index)
        if start < 0:
            break
        try:
            data, _end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(data, dict):
            found.append(data)
        index = start + 1
    return found


def _usable_decision(data: dict) -> bool:
    return any(key in data for key in ("action", "done", "read"))


def parse_decision(content: str, reasoning: str = "") -> dict:
    """思考在 reasoning 里。可执行动作优先取正文里的 JSON，正文没有再从思考末尾取。"""
    content_hit = next((item for item in reversed(_json_objects(content)) if _usable_decision(item)), None)
    reason_hit = next((item for item in reversed(_json_objects(reasoning)) if _usable_decision(item)), None)
    chosen = content_hit or reason_hit
    if chosen is None:
        objects = _json_objects(content) or _json_objects(reasoning)
        chosen = objects[-1] if objects else {"done": False}
    chosen = dict(chosen)
    if reasoning:
        chosen["_reasoning"] = reasoning[:800]
    return chosen


def deepseek_model(key: str):
    """同一次执行共用一段对话。上一轮的思考会随回复交回去，下一轮接着想。"""
    messages = [{
        "role": "system",
        "content": "你在已打开的产品窗口里完成目标。每轮只返回一个 JSON，先写 think，再写一个动作：look、type、select、set、click、scroll、press、read、skip 或 done。",
    }]
    thinking = True

    def turn(prompt: str) -> dict:
        nonlocal thinking
        if not key:
            return {"done": True}
        import json as json_mod
        import urllib.request
        import urllib.error

        messages.append({"role": "user", "content": prompt})

        def ask() -> dict:
            payload = {
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                "messages": messages,
                "thinking": {"type": "enabled" if thinking else "disabled"},
                "max_tokens": 4000,
            }
            if not thinking:
                payload["response_format"] = {"type": "json_object"}
            request = urllib.request.Request(
                "https://api.deepseek.com/chat/completions",
                data=json_mod.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return json_mod.load(response)

        try:
            body = ask()
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or not thinking:
                raise
            thinking = False
            body = ask()
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        reasoning = str(message.get("reasoning_content") or "").strip()
        assistant = {"role": "assistant", "content": content}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        messages.append(assistant)
        return parse_decision(content, reasoning)
    return turn


def timestamp_now() -> str:
    from qa_platform.store import timestamp
    return timestamp()


def _danger(code: str, case: dict) -> str | None:
    expected = json.dumps({
        "title": case.get("title"),
        "steps": case.get("steps"),
    }, ensure_ascii=False)
    for word in DANGER:
        if word in code and word not in expected:
            return word
    return None


def _db_matches(run_dir: Path, case: dict, expect_state: str, query) -> bool | None:
    baseline = _load_json(run_dir / "baseline.json") or {}
    statements = baseline.get("select_sql") if isinstance(baseline.get("select_sql"), dict) else {}
    sql = statements.get(case.get("check_id"))
    fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
    fixture = fixtures.get(case.get("check_id")) if isinstance(fixtures.get(case.get("check_id")), dict) else {}
    database = fixture.get("database")
    if not isinstance(sql, str) or not isinstance(database, str) or query is None:
        return None
    state = read_state(run_dir)
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    rows = query(database, sql, allowed)
    field = ""
    for check in (_load_json(run_dir / "plan.json") or {}).get("checks") or []:
        if isinstance(check, dict) and check.get("id") == case.get("check_id"):
            field = str(check.get("state_field") or "")
    if not field:
        return None
    return any(isinstance(row, dict) and row.get(field) == expect_state for row in rows)


def _block_environment(run_dir: Path, state: dict, case: dict, later: list[dict]) -> int:
    case["status"] = "blocked"
    case["blocked_reason"] = "环境不在 base_url"
    _write_case(run_dir, case)
    for item in later:
        item["status"] = "waiting"
        _write_case(run_dir, item)
    state["stop_reason"] = "environment"
    state["current_case"] = case["id"]
    write_json(run_dir / "state.json", state)
    record(run_dir, exit_code=2, event_status="阻塞", basis="环境不在 base_url", step="执行")
    return 2


def _prepare_cases(run_dir: Path, plan: dict, phase: str = "test") -> list[dict]:
    ready = []
    for item in plan.get("cases") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        steps = [step for step in item.get("steps") or [] if isinstance(step, dict) and step.get("phase") == phase]
        folder = run_dir / "cases" / str(item["id"])
        existing = _load_json(folder / "case.json") or {}
        status = existing.get("status") or "waiting"
        fresh = status == "waiting"
        goal = item.get("goal") if isinstance(item.get("goal"), str) else existing.get("goal") or ""
        start = item.get("start") if isinstance(item.get("start"), str) else (
            existing.get("start") if isinstance(existing.get("start"), str) else ""
        )
        expect = item.get("expect_text") if item.get("expect_text") is not None else existing.get("expect_text")
        case = {
            "id": item["id"],
            "case_id": item["id"],
            "title": item.get("title") or existing.get("title") or "",
            "goal": goal or "",
            "start": start,
            "expect_text": expect,
            "check_id": item.get("check_id"),
            "status": status,
            "pending": existing.get("pending"),
            "steps": steps,
            "rounds": existing.get("rounds") or [],
            "seen": existing.get("seen") or "",
            "observed": False if fresh else existing.get("observed") or False,
            "blocked_reason": "" if fresh else existing.get("blocked_reason") or "",
            "filled_values": [] if fresh else (
                existing.get("filled_values") if isinstance(existing.get("filled_values"), list) else []
            ),
            "result_reason": "" if fresh else existing.get("result_reason") or "",
            "observation": None if fresh else existing.get("observation"),
            "sealed": False if fresh else bool(existing.get("sealed")),
        }
        _write_case(run_dir, case)
        ready.append(case)
    return ready


def _launch(binary: Path, url: str, console_log: Path) -> None:
    env = os.environ.copy()
    env["QA_PLATFORM_CONSOLE_LOG"] = str(console_log)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("YUNXIAO_TOKEN", None)
    target = Path(binary)
    if not target.is_file() or target.is_symlink():
        target = ensure_browser()
    subprocess.Popen(browser_argv(target, url), env=env, start_new_session=True)


def execute_reuse(run_dir: Path, *, session=None, model=None, query=None, browser=None) -> int:
    state = read_state(run_dir)
    if not reuse_ready(run_dir, state):
        return 0
    plan = _load_json(run_dir / "plan.json") or {}
    base_url = str(state.get("base_url") or "")
    app_name, browser_error = show_test_page(run_dir, plan, state, session, query, browser, base_url)
    if not app_name:
        record(run_dir, exit_code=2, event_status="阻塞", basis=browser_error, step="执行")
        return 2
    cases = _prepare_cases(run_dir, plan)
    if session is None or model is None:
        record(run_dir, exit_code=2, event_status="阻塞", basis="没有执行会话", step="执行")
        return 2
    for index, case in enumerate(cases):
        if case.get("status") in {"blocked", "failed", "paused", "skipped"} and not case.get("pending"):
            continue
        outcome = _run_case(
            run_dir, state, case, cases[index + 1:], plan, base_url, session, model, query, "test", app_name,
        )
        if outcome != 0:
            return outcome
    record(run_dir, exit_code=0, event_status="完成", basis="观察已记下，尚未封口", step="执行")
    return 0


def _finish_commit(run_dir, state, case, later, expect: str, before: str | None, query) -> int | None:
    after = _observed_state(run_dir, case, query)
    if expect and after == expect:
        case["pending"] = None
        case["status"] = "running"
        _write_case(run_dir, case)
        return None
    if after is None or after == before:
        case["status"] = "paused"
        _write_case(run_dir, case)
        record(run_dir, exit_code=3, event_status="暂停", basis="尚未核对", step="执行")
        return 3
    case["status"] = "failed"
    case["blocked_reason"] = "提交结果不符合预期"
    _write_case(run_dir, case)
    for item in later:
        item["status"] = "waiting"
        _write_case(run_dir, item)
    record(run_dir, exit_code=2, event_status="失败", basis="提交结果不符合预期", step="执行")
    return 2


def _observed_state(run_dir: Path, case: dict, query) -> str | None:
    baseline = _load_json(run_dir / "baseline.json") or {}
    statements = baseline.get("select_sql") if isinstance(baseline.get("select_sql"), dict) else {}
    sql = statements.get(case.get("check_id"))
    fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
    fixture = fixtures.get(case.get("check_id")) if isinstance(fixtures.get(case.get("check_id")), dict) else {}
    database = fixture.get("database")
    if not isinstance(sql, str) or not isinstance(database, str) or query is None:
        return None
    allowed = [item for item in read_state(run_dir).get("allowed_databases") or [] if isinstance(item, str)]
    rows = query(database, sql, allowed)
    field = ""
    for check in (_load_json(run_dir / "plan.json") or {}).get("checks") or []:
        if isinstance(check, dict) and check.get("id") == case.get("check_id"):
            field = str(check.get("state_field") or "")
    if not field or not rows:
        return ""
    value = rows[0].get(field) if isinstance(rows[0], dict) else ""
    return "" if value is None else str(value)


DANGEROUS = ("重转", "删除", "作废", "审核", "手工转化", "手工下载", "提交")
ENTRY_ROLES = {"textfield", "textarea", "searchfield", "combobox", "textbox", "searchbox"}
LIST_ROLES = {"cell", "row", "column", "columnheader", "gridcell", "table", "grid", "list", "outline"}
CLICK_ROLES = {"button", "checkbox", "radiobutton", "radio", "tab", "link", "menuitem", "popupbutton"}
ROUND_LIMIT = 20
UNREADY_LIMIT = 3
PAGE_NOT_OPEN = "测试页没有打开"
SHAPE_HINT = "text 写控件名，value 写要填的内容。"
_CHROME_WORDS = ("地址和搜索栏", "地址栏", "网址栏", "标签栏", "标签条", "重新加载", "刷新按钮", "后退按钮", "前进按钮")
_HIDDEN_WORDS = ("当前部署的页面没有", "当前部署的界面没有")


def _control_label(item: dict) -> str:
    for key in ("text", "name", "title", "label", "description", "value"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _matches(result: dict) -> list[dict]:
    text = observe(result).get("text") or ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    matches = payload.get("matches") if isinstance(payload, dict) else None
    return [item for item in matches if isinstance(item, dict)] if isinstance(matches, list) else []


def _role_key(kind: str) -> str:
    key = str(kind or "").lower()
    if key.startswith("ax"):
        key = key[2:]
    return key


def _is_entry(kind: str) -> bool:
    return _role_key(kind) in ENTRY_ROLES


def _is_list_field(kind: str) -> bool:
    return _role_key(kind) in LIST_ROLES


def _is_dangerous(label: str) -> bool:
    text = (label or "").strip()
    return any(text == word or text.startswith(word) for word in DANGEROUS)


def _pick_match(result: dict, wanted: str = "", role: str = "") -> dict | None:
    """只点名称对得上的控件。填写只认输入框。同名时先认输入框，再认能点的控件，最后才是文字。"""
    matches = _matches(result)
    if not matches or _is_dangerous(wanted):
        return None
    wanted_role = role.lower()

    def allowed(item: dict) -> bool:
        label = _control_label(item)
        kind = str(item.get("role") or "")
        if _is_dangerous(label) or _is_chrome_control(label):
            return False
        if not label:
            return False
        if wanted and (_is_chrome_control(wanted) or (wanted not in label and label not in wanted)):
            return False
        if wanted and label != wanted and _role_key(kind) in CLICK_ROLES:
            return False
        if wanted_role in {"textbox", "text", "searchbox"}:
            return _is_entry(kind)
        if _is_list_field(kind) and wanted_role in {"button", "link"}:
            return False
        return True

    pool = [item for item in matches if allowed(item)]
    exact = [item for item in pool if wanted and _control_label(item) == wanted]
    ranked = exact or pool
    if not wanted_role and exact:
        entries = [item for item in exact if _is_entry(str(item.get("role") or ""))]
        actions = [item for item in exact if _role_key(str(item.get("role") or "")) in CLICK_ROLES]
        if entries:
            ranked = entries
        elif actions:
            ranked = actions
    chosen = ranked[0] if ranked else None
    return chosen if isinstance(chosen, dict) else None


def _match_index(result: dict, wanted: str = "", role: str = "") -> str | None:
    chosen = _pick_match(result, wanted, role)
    if not isinstance(chosen, dict):
        return None
    for key in ("element_index", "index"):
        if chosen.get(key) is not None:
            return str(chosen[key])
    return None


def _needs_pointer(item: dict) -> bool:
    """静态文字没有按下动作，辅助功能点下去只会把鼠标停在字上。"""
    kind = str(item.get("role") or "")
    if _is_entry(kind) or _is_list_field(kind):
        return False
    return _role_key(kind) not in CLICK_ROLES


_TREE_ROLES = CLICK_ROLES | {"按钮", "单选按钮", "单选", "标签", "链接", "菜单项", "弹出式按钮", "复选框"}


def _state_text(result: dict) -> str:
    if isinstance(result, dict) and "content" not in result:
        return str(result.get("text") or "")
    parts = []
    for item in (result or {}).get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def _app_tree(session, app: str) -> str:
    getter = getattr(session, "get_app_state", None)
    if not callable(getter):
        return ""
    try:
        result = getter(app, include_screenshot=False)
    except TypeError:
        try:
            result = getter(app)
        except (OSError, OcuError, TypeError):
            return ""
    except (OSError, OcuError):
        return ""
    return _state_text(result if isinstance(result, dict) else {})


def _tree_control(tree: str, wanted: str) -> str | None:
    """整页树会把按钮里的文字写在按钮那一行。按名字查询常常只返回里面的静态文字。"""
    name = (wanted or "").strip()
    if not name or not tree:
        return None
    for raw in tree.splitlines():
        line = raw.strip()
        match = re.match(r"^(\d+)\s+(\S+)\s+(.*)$", line)
        if not match:
            continue
        index, role, rest = match.group(1), match.group(2), match.group(3)
        if role not in _TREE_ROLES and _role_key(role) not in CLICK_ROLES:
            continue
        label = re.split(r"\s+Frame:", rest, maxsplit=1)[0].strip()
        if label == name and not _is_dangerous(label) and not _is_chrome_control(label):
            return index
    return None


def _bounds_center(item: dict) -> tuple[float, float] | None:
    bounds = item.get("bounds")
    if not isinstance(bounds, dict):
        return None
    try:
        x = float(bounds["x"])
        y = float(bounds["y"])
        width = bounds.get("w", bounds.get("width"))
        height = bounds.get("h", bounds.get("height"))
        w = float(width)
        h = float(height)
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x + w / 2, y + h / 2


def _press(session, app: str, item: dict, name: str = "") -> bool:
    """点同名的真实控件。只有树里也没有按钮时，才按文字的位置点。返回是否点到了控件。"""
    index = ""
    for key in ("element_index", "index"):
        if item.get(key) is not None:
            index = str(item[key])
            break
    if not index:
        raise OcuError("没有控件编号")
    if _needs_pointer(item):
        parent = _tree_control(_app_tree(session, app), name or _control_label(item))
        if parent:
            session.click(app, parent)
            return True
        point = _bounds_center(item)
        if point is not None:
            try:
                session.click(app, index, "app_post", point[0], point[1])
                return False
            except TypeError:
                pass
    session.click(app, index)
    return not _needs_pointer(item)


def drive_plan(session, app: str, steps: list, typed: str, tell=None) -> str:
    """按方案逐步看页面、点控件、填写。不让模型写一整段脚本。"""
    done = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "")
        role = str(step.get("role") or "")
        action = str(step.get("action") or "click")
        if tell:
            tell(f"正在页面上找「{name or role}」")
        item = None
        index = None
        if action == "type":
            index, _found = _entry_index(session, app, name)
            if not index:
                raise LookupError(name or role)
        else:
            item = _pick_match(session.query(app, name, role), name, role)
            if item is None:
                raise LookupError(name or role)
        if action == "type":
            value = typed or str(step.get("value") or "")
            if value in {"使用库里已有交易号", "使用库里已有数据"}:
                value = typed
            if not value:
                raise LookupError("没有可填的数据")
            if tell:
                tell(f"正在填写「{name}」")
            session.click(app, index)
            session.set_value(app, index, value)
            done.append(f"已填写「{name}」")
        elif action == "select":
            if tell:
                tell(f"正在点开「{name}」")
            _press(session, app, item, name)
            option = str(step.get("option") or "")
            if option and "请选择" not in option:
                option_item = _pick_match(session.query(app, option, ""), option, "")
                if option_item:
                    _press(session, app, option_item, option)
            done.append(f"已点开「{name}」")
        else:
            if tell:
                tell(f"正在点击「{name}」")
            _press(session, app, item, name)
            done.append(f"已点击「{name}」")
    return "；".join(done)


def _read_repo(repo: str, relative: str, front_path: str = "") -> str:
    root = Path(repo).resolve() if repo else None
    if root is None or not root.is_dir():
        return "没有项目目录"
    cleaned = relative.strip().lstrip("./")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        return "只能读项目里的相对路径"
    path = (root / cleaned).resolve()
    front = _front_root(root, front_path)
    if (not str(path).startswith(str(root) + os.sep) or not path.is_file()) and front is not None:
        path = (front / cleaned).resolve()
        root = front
    if not str(path).startswith(str(root) + os.sep) or not path.is_file() or path.is_symlink():
        return "文件不在项目里"
    if path.suffix.lower() not in {".java", ".xml", ".vue", ".ts", ".js", ".sql", ".md"}:
        return "这个文件类型不读"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "文件读不到"
    lines = text.splitlines()
    return "\n".join(lines[:160])


def _useful_page(rows: list[str]) -> str:
    """整页树先给输入框和按钮。侧栏和浏览器外框排在后面，避免把要填的框挤出这一轮。"""
    url = next((row for row in rows if "URL:" in row or row.startswith("http")), "")
    entries: list[str] = []
    actions: list[str] = []
    for row in rows:
        if _is_chrome_control(row):
            continue
        low = row.lower()
        if any(word in low for word in ("textfield", "textarea", "searchfield", "combobox", "textbox")):
            entries.append(row)
        elif any(word in low for word in ("button", "radio", "checkbox", "popup")):
            actions.append(row)
    from qa_platform.runner.report import page_result_count

    counts = [row for row in rows if page_result_count(row)]
    if not entries and not actions and not counts:
        return "\n".join(rows[:48])[:1800] or "页面没有返回内容"
    body = [url] if url else []
    if entries:
        body.append("输入框：")
        body.extend(entries[:12])
    if actions:
        body.append("可点：")
        body.extend(actions[:16])
    if counts:
        body.append("页面结果：")
        body.extend(counts[:4])
    return "\n".join(body)[:1800]


def _page_lines(result: dict, focus: str = "") -> str:
    text = str(observe(result).get("text") or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if "HTML 内容" in text:
            text = text.split("HTML 内容", 1)[1]
        rows = [line.strip() for line in text.splitlines() if line.strip()]
        if focus:
            rows = [line for line in rows if focus in line]
            if not rows:
                return f"页面上没有找到「{focus}」"
            return "\n".join(rows[:12]) or "页面没有返回内容"
        return _useful_page(rows)
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list) or not matches:
        if focus:
            return f"页面上没有找到「{focus}」"
        rows = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(rows[:48])[:1800] or "没有匹配的控件"
    lines = []
    for item in matches[:8]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("role") or "")
        mark = " 列表字段" if _is_list_field(kind) else (" 可以填写" if _is_entry(kind) else "")
        lines.append(
            f"{item.get('element_index') or item.get('index') or ''} "
            f"{kind} 名称「{_control_label(item)}」{mark}".strip()
        )
    text_out = "\n".join(lines) or "没有匹配的控件"
    if focus and lines and all(_is_list_field(str(item.get("role") or "")) for item in matches[:8] if isinstance(item, dict)):
        text_out += f"\n这些是列表里的「{focus}」字段，不是查询框。"
    return text_out


_FRONT_SUFFIXES = {".vue", ".ts", ".js"}
_FRONT_SKIP = {"node_modules", "dist", ".git"}
_SCAN_LIMIT = 4000


def _front_root(repo: Path, configured: str = "") -> Path | None:
    from qa_platform.runner.page_index import front_root

    return front_root(repo, configured)


def _placeholder(front: Path, token: str) -> str:
    util = front / "src/libs/util.js"
    if not util.is_file():
        return token
    match = re.search(rf"\b{re.escape(token)}\s*:\s*'([^']*)'", util.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else token


def _front_files(src: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [name for name in dirnames if name not in _FRONT_SKIP]
        for name in filenames:
            if Path(name).suffix not in _FRONT_SUFFIXES:
                continue
            found.append(Path(dirpath) / name)
            if len(found) >= _SCAN_LIMIT:
                return found
    return found


def _quoted_path(text: str, mapping: str) -> bool:
    return f"'{mapping}'" in text or f'"{mapping}"' in text


def _read_front(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mapping_file(src: Path, mapping: str) -> Path | None:
    hits = [path for path in _front_files(src) if _quoted_path(_read_front(path), mapping)]
    if not hits:
        return None
    api = [path for path in hits if f"{os.sep}api{os.sep}" in str(path)]
    return api[0] if api else hits[0]


def _tab_and_view(front: Path, folder: str) -> tuple[str, str]:
    """页签写在哪个文件里，就用那个文件。组件被引用但没有 label 时，不编页签名。"""
    needle = f"/{folder}/"
    for path in _front_files(front / "src"):
        if path.suffix not in {".vue", ".ts"}:
            continue
        text = _read_front(path)
        if needle not in text:
            continue
        imported = re.search(
            rf"import\s+(\w+)\s+from\s+'([^']*{re.escape(needle)}[^']*)'",
            text,
        )
        if imported is None:
            continue
        component, source = imported.group(1), imported.group(2)
        for label, used in re.findall(r"label:\s*'([^']+)'[^}]*component:\s*(\w+)", text):
            if used == component:
                return label, source
    return "", ""


def _filter_lines(front: Path, view: Path) -> list[str]:
    candidates = [view / "attrs/filter-bar-attrs.ts"]
    attrs = view / "attrs"
    if attrs.is_dir():
        candidates.extend(path for path in attrs.glob("*.ts") if path not in candidates)
    fields: list[str] = []
    for filt in candidates:
        body = _read_front(filt)
        if "fieldName" not in body:
            continue
        for label, field in re.findall(r"label:\s*'([^']+)'[\s\S]{0,240}?fieldName:\s*'([^']+)'", body):
            holder = ""
            window = body[body.find(f"label: '{label}'"):body.find(f"label: '{label}'") + 400]
            plain = re.search(r"placeholder:\s*'([^']*)'", window)
            named = re.search(r"placeholder:\s*Placeholders\.(\w+)", window)
            if plain:
                holder = plain.group(1)
            elif named:
                holder = _placeholder(front, named.group(1))
            extra = f"，提示「{holder}」" if holder else ""
            fields.append(f"{label} 对应字段 {field}{extra}")
        if fields:
            break
    return fields


def _view_dir(front: Path, imported: str) -> Path | None:
    relative = imported.removeprefix("@/").removeprefix("/")
    if relative.endswith("/index.vue"):
        relative = relative[: -len("/index.vue")]
    elif relative.endswith(".vue"):
        relative = str(Path(relative).parent)
    view = (front / "src" / relative).resolve()
    src = (front / "src").resolve()
    if not str(view).startswith(str(src) + os.sep) or not view.is_dir():
        return None
    return view


def implementation_brief(repo: str, source: str, front_path: str = "") -> str:
    """从方案出处指向的控制器，按它的接口路径到前端里找页签和筛选项。"""
    root = Path(repo).resolve() if repo else None
    if root is None or not root.is_dir() or not source:
        return ""
    relative = source.split(":", 1)[0].strip().lstrip("./")
    if not relative or ".." in Path(relative).parts:
        return ""
    controller = (root / relative).resolve()
    if not str(controller).startswith(str(root) + os.sep) or not controller.is_file():
        return ""
    java = _read_front(controller)
    class_name = ""
    found = re.search(r"\bclass\s+(\w+)", java)
    if found:
        class_name = found.group(1)
    mapping = ""
    found = re.search(r'@RequestMapping\(\s*(?:(?:value|path)\s*=\s*)?"([^"{}]+)"\)', java)
    if found:
        mapping = found.group(1).strip()
        if mapping and not mapping.startswith("/") and " " not in mapping:
            mapping = "/" + mapping
    tab = ""
    fields: list[str] = []
    front = _front_root(root, front_path)
    if front is not None and mapping.startswith("/") and ".." not in mapping:
        hit = _mapping_file(front / "src", mapping)
        folder = hit.parent.name if hit is not None and hit.name.startswith("index.") else ""
        if folder and folder not in {"src", "api", "views"}:
            label, imported = _tab_and_view(front, folder)
            tab = label
            view = _view_dir(front, imported) if imported else None
            if tab and view is not None:
                fields = _filter_lines(front, view)
    lines = [f"代码实现：{class_name} {mapping}".strip()]
    if tab:
        lines.append(f"页面页签是「{tab}」。")
    if fields:
        lines.append("筛选项：" + "；".join(fields[:8]))
    lines.append(f"出处文件：{relative}")
    return "\n".join(lines)


def _expect_texts(case: dict, phase: str) -> list[str]:
    found: list[str] = []

    def take(value) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in found:
            found.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                take(item)

    take(case.get("expect_text"))
    for step in case.get("steps") or []:
        if isinstance(step, dict) and step.get("phase", "test") == phase:
            take(step.get("expect_text"))
    return found


def _how_line(step: dict) -> str:
    action = str(step.get("action") or "")
    name = str(step.get("name") or "")
    role = {"textbox": "输入框", "combobox": "下拉", "button": "按钮", "link": "链接", "checkbox": "勾选框", "radio": "单选"}.get(str(step.get("role") or ""), "")
    extra = f"（{role}）" if role else ""
    if action == "type":
        return f"- 填写「{name}」{extra}"
    if action == "select":
        option = str(step.get("option") or "")
        chosen = f"为「{option}」" if option else ""
        return f"- 选择「{name}」{chosen}{extra}"
    if action == "set":
        return f"- 勾选「{name}」{extra}"
    if action == "click":
        return f"- 点击「{name}」{extra}"
    return f"- {action}「{name}」{extra}"


def _plan_brief(case: dict, plan: dict, phase: str, typed: str, repo: str = "", requirement: str = "", front_path: str = "") -> str:
    from qa_platform.runner.planner import acceptance_criteria, case_goal, case_start

    criteria = acceptance_criteria(requirement)
    lines = [
        f"任务：{case.get('id') or ''} {case.get('title') or ''}".strip(),
        f"目标：{case_goal(case)}",
        f"起点：{case_start(case, plan)}",
        "做法只是参考。以当前页面为准。",
    ]
    if criteria:
        lines.append("要验证：")
        lines.extend(f"- {item}" for item in criteria)
    how = [
        _how_line(step)
        for step in case.get("steps") or []
        if isinstance(step, dict) and step.get("phase", "test") == phase and step.get("name")
    ]
    if how:
        lines.append("做法：")
        lines.extend(how)
    from qa_platform.runner.report import satisfied_by_count

    expects = _expect_texts(case, phase)
    literal = [item for item in expects if not satisfied_by_count(item, expects)]
    counted = [item for item in expects if item not in literal]
    if literal:
        lines.append("做完后页面上应出现：")
        lines.extend(f"- {item}" for item in literal[:8])
    if counted:
        lines.append("查询结果看页面上名称正好是「共 N 条」的那一行。准备的值是测试输入，条数可以和这个 N 不同。")
        lines.append("空表上的 0 和「查询完成」是示例，不要拿来和页面结果对比。")
    lines.extend(_data_lines(typed, bool(case.get("data_empty")), bool(case.get("data_unqueried"))))
    source = ""
    for check in plan.get("checks") or []:
        if isinstance(check, dict) and check.get("id") == case.get("check_id"):
            source = str(check.get("source") or "")
            if check.get("source"):
                lines.append(f"相关代码：{check.get('source')}")
            break
    code = implementation_brief(repo, source, front_path)
    if code:
        lines.append("页面线索：")
        lines.append(code)
    return "\n".join(line for line in lines if line)


def _named(text: str) -> str:
    """控件名只留页面上的字。模型若把「输入框」一起抄回来，去掉后再找。"""
    cleaned = text.strip().strip("「」")
    for suffix in (" 可以填写", " 输入框", "输入框", " 列表字段", "列表字段"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    return cleaned or text.strip()


def _entry_index(session, app: str, text: str) -> tuple[str | None, dict]:
    """填写时先在同名结果里挑输入框。按 textbox 过滤会错过 AXTextField，再按输入框角色找一次。"""
    name = _named(text)
    found = session.query(app, name, "")
    index = _match_index(found, name, "textbox")
    if index:
        return index, found
    for role in ("textfield", "textarea", "searchfield", "combobox"):
        narrowed = session.query(app, name, role)
        index = _match_index(narrowed, name, "textbox")
        if index:
            return index, narrowed
    return None, found


def _clip_label(text: str, limit: int = 24) -> str:
    raw = text.strip()
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}…"


def _looks_like_payload(text: str, prepared: str) -> bool:
    """控件名槽里塞进了要填的数据：整串准备值、其中一条、逗号串，或一长串数字。"""
    raw = text.strip()
    if not raw:
        return False
    ready = prepared.strip()
    if ready and raw == ready:
        return True
    ids = [item.strip() for item in ready.split(",") if item.strip()] if ready else []
    if raw in ids:
        return True
    if "," in raw and len(raw) > 12:
        return True
    return raw.isdigit() and len(raw) >= 6


def _short_label(text: str, prepared: str) -> bool:
    raw = text.strip()
    return bool(raw) and len(raw) <= 24 and not _looks_like_payload(raw, prepared)


def _entry_for_payload(session, app: str, value: str, fill_names: list[str], prepared: str) -> tuple[str | None, str]:
    ordered: list[str] = []
    label = value.strip()
    if _short_label(label, prepared):
        ordered.append(label)
    for name in fill_names:
        if name not in ordered:
            ordered.append(name)
    for name in ordered:
        index, _found = _entry_index(session, app, name)
        if index:
            return index, name
    return None, ""


def _step_targets(case: dict, phase: str) -> tuple[list[str], list[str], list[str]]:
    fill: list[str] = []
    click: list[str] = []
    select: list[str] = []
    for step in case.get("steps") or []:
        if not isinstance(step, dict) or step.get("phase", "test") != phase:
            continue
        name = str(step.get("name") or "").strip()
        if not name:
            continue
        action = str(step.get("action") or "")
        if action == "type" and name not in fill:
            fill.append(name)
        elif action == "select" and name not in select:
            select.append(name)
        elif action in {"click", "set"} and name not in click:
            click.append(name)
    return fill, click, select


def _is_selected(item: dict) -> bool:
    for key in ("selected", "AXSelected"):
        value = item.get(key)
        if value is True or str(value).lower() in {"true", "1", "yes"}:
            return True
    blob = " ".join(str(item.get(key) or "") for key in ("description", "value", "title", "state"))
    return "selected" in blob.lower() or "选中" in blob


def _format_control(item: dict) -> str:
    kind = str(item.get("role") or "")
    label = _control_label(item)
    if _is_dangerous(label) or _is_chrome_control(label):
        mark = "不要点"
    elif _is_entry(kind):
        mark = "可填写"
    elif _is_list_field(kind):
        mark = "列表字段"
    elif _is_selected(item):
        mark = "已选中"
    else:
        mark = ""
    return f"名称「{label}」 {kind} {mark}".strip()


def _collect_named(session, app: str, name: str, want_entry: bool) -> list[dict]:
    items = _matches(session.query(app, name, ""))
    if want_entry and not any(_is_entry(str(item.get("role") or "")) for item in items):
        items = items + _matches(session.query(app, name, "textfield"))
    return items


def _observe_page(session, app: str, fill_names: list[str], click_names: list[str], select_names: list[str] | None = None, *, announce: bool = True, tab_names: list[str] | None = None) -> str:
    """挑出页面上和这条用例相关的控件。还没走到起点时不宣布打算填写或点击。"""
    select_names = select_names or []
    tab_names = tab_names or []
    lines: list[str] = []
    if announce and fill_names:
        lines.append("参考做法要填：" + "、".join(f"「{name}」" for name in fill_names) + "。填的是输入框。")
    if announce and select_names:
        lines.append("参考做法要选：" + "、".join(f"「{name}」" for name in select_names) + "。")
    if announce and click_names:
        lines.append("参考做法要点：" + "、".join(f"「{name}」" for name in click_names) + "。")
    fillable: list[str] = []
    not_entry: list[str] = []
    clickable: list[str] = []
    seen: set[tuple[str, str]] = set()
    query = getattr(session, "query", None)
    if callable(query):
        try:
            for name in fill_names[:6]:
                for item in _collect_named(session, app, name, True):
                    key = (_control_label(item), _role_key(str(item.get("role") or "")))
                    if not key[0] or key in seen:
                        continue
                    seen.add(key)
                    row = _format_control(item)
                    if _is_entry(str(item.get("role") or "")):
                        fillable.append(row)
                    else:
                        not_entry.append(row)
            for name in [*select_names[:4], *click_names[:6]]:
                for item in _collect_named(session, app, name, False):
                    key = (_control_label(item), _role_key(str(item.get("role") or "")))
                    if not key[0] or key in seen:
                        continue
                    seen.add(key)
                    if _is_chrome_control(key[0]):
                        continue
                    if _is_dangerous(key[0]):
                        not_entry.append(_format_control(item))
                    else:
                        clickable.append(_format_control(item))
            for name in tab_names[:8]:
                if name in fill_names or name in click_names or name in select_names:
                    continue
                for item in _collect_named(session, app, name, False):
                    key = (_control_label(item), _role_key(str(item.get("role") or "")))
                    if not key[0] or key in seen or _is_chrome_control(key[0]):
                        continue
                    seen.add(key)
                    clickable.append(_format_control(item))
        except (OSError, OcuError, TypeError):
            pass
    tabs = [row for row in clickable if any(name and name in row for name in tab_names)]
    if tabs:
        lines.append("当前页签：")
        lines.extend(f"- {row}" for row in tabs[:8])
        clickable = [row for row in clickable if row not in tabs]
    if fillable:
        lines.append("当前能填：")
        lines.extend(f"- {row}" for row in fillable[:12])
    elif announce and fill_names:
        lines.append("当前能填：还没有找到输入框。")
    if not_entry:
        lines.append("同名但不是输入框：")
        lines.extend(f"- {row}" for row in not_entry[:8])
    if clickable:
        lines.append("当前能点：")
        lines.extend(f"- {row}" for row in clickable[:12])
    snap = _see_page(session, app)
    if snap:
        lines.append(snap)
    return "\n".join(line for line in lines if line)


def _agent_prompt(brief: str, repo: str, page: str, history: list[str], situation: str) -> str:
    done = "\n".join(history[-8:]) or "还没有"
    return "\n".join([
        "你在已经打开的产品窗口里完成目标。每轮看当前页面，只做一个动作，先写 think。",
        '每轮一个 JSON。text 是控件名，value 是要填的内容：{"think":"","action":"type","text":"控件名","value":"要填的内容"}。读代码：{"read":"相对路径"}。',
        "方案只说明要测什么。按你在页面上看见的菜单、页签、输入框去做。对不上可以读这一页的前端，再看一次页面。窗口还只是浏览器时不要操作。当前部署没有这一项就 skip。",
        "不要点重转、删除、作废、审核、手工转化、提交，也不要点表格列名。",
        situation,
        f"项目目录：{repo or '没有'}",
        brief,
        "当前页面：",
        page or "还没有看到可操作的控件。",
        "已经做过：",
        done,
    ])


def _fill_value(raw: str, prepared: str) -> str:
    """模型写出的内容优先。留空时用这一条准备的全部值，不再改成第一条或前几条。"""
    text = raw.strip()
    if text in {"", "使用库里已有数据", "使用库里已有交易号"}:
        return prepared.strip()
    return text


def _see_page(session, app: str) -> str:
    """有整页读取时，先把当前界面交回模型。没有这个能力就留空，仍按名字看。"""
    getter = getattr(session, "get_app_state", None)
    if not callable(getter):
        return ""
    try:
        seen = _page_lines(getter(app))
    except (OSError, OcuError, TypeError):
        return ""
    if seen in {"页面没有返回内容", "没有匹配的控件"}:
        return ""
    return seen


def manual_stop(run_dir: Path) -> bool:
    marker = run_dir / "stop.requested"
    if not marker.is_file() or marker.is_symlink():
        return False
    record(run_dir, exit_code=2, event_status="停止", basis="已手动停止", step="执行")
    fresh = read_state(run_dir)
    fresh["stop_reason"] = "manual"
    fresh.pop("round_prompt", None)
    write_json(run_dir / "state.json", fresh)
    return True


def _ask_to_continue(run_dir: Path, pause=None) -> str:
    """满 20 轮后停在总台，等继续或手动停止。不自己结束。"""
    fresh = read_state(run_dir)
    fresh["round_prompt"] = {
        "rounds": ROUND_LIMIT,
        "question": "已进行 20 轮，是否停止这次执行？",
    }
    write_json(run_dir / "state.json", fresh)
    note(run_dir, step="执行", basis="已进行 20 轮。总台上可以停止，或让它继续。")
    wait = pause or (lambda: time.sleep(0.4))
    while True:
        if manual_stop(run_dir):
            return "stop"
        marker = run_dir / "continue.requested"
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()
            fresh = read_state(run_dir)
            fresh.pop("round_prompt", None)
            write_json(run_dir / "state.json", fresh)
            note(run_dir, step="执行", basis="继续执行")
            return "continue"
        wait()


def _split_values(typed: str) -> list[str]:
    return [part.strip() for part in str(typed or "").split(",") if part.strip()]


def _data_lines(typed: str, empty: bool, unqueried: bool = False) -> list[str]:
    if empty:
        return ["这一条库里没有数据。不要填写，也不要查询。"]
    if unqueried:
        return ["这一条基线没有查出可填的值。不要编一个值去填，也不要查询。"]
    values = _split_values(typed)
    if not values:
        return []
    shown = "、".join(values)
    return [f"这一条准备了 {len(values)} 条测试输入。填写时用这些值，不要用别的用例的数据：{shown}。"]


def _window_hosts(text: str) -> list[str]:
    found = []
    for url in re.findall(r"URL:\s*(https?://[^\s,]+)", text or "", flags=re.I):
        host = (urlparse(url).hostname or "").casefold()
        if host:
            found.append(host)
    return found


def _shot_matches_product(text: str, base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").casefold()
    if not host:
        return True
    found = _window_hosts(text)
    if not found:
        return True
    return any(item == host for item in found)


def _remember_filled(case: dict, value: str) -> None:
    filled = case.get("filled_values")
    if not isinstance(filled, list):
        filled = []
    for part in _split_values(value) or ([value] if value else []):
        if part not in filled:
            filled.append(part)
    case["filled_values"] = filled


def _probe_text(session, app: str, text: str) -> str:
    query = getattr(session, "query", None)
    if not text or not callable(query):
        return ""
    try:
        return _page_lines(query(app, text, ""), text)
    except (OSError, OcuError, TypeError):
        return ""


def _query_click_names(case: dict, phase: str) -> set[str]:
    """填写之后要点的控件。点它才算提交这次查询。"""
    names: set[str] = set()
    saw_type = False
    for step in case.get("steps") or []:
        if not isinstance(step, dict) or step.get("phase", "test") != phase:
            continue
        action = str(step.get("action") or "")
        name = str(step.get("name") or "").strip()
        if action == "type" and name:
            saw_type = True
        elif saw_type and action == "click" and name:
            names.add(name)
    return names


def _case_types(case: dict, phase: str) -> bool:
    return any(
        isinstance(step, dict)
        and step.get("phase", "test") == phase
        and step.get("action") == "type"
        and str(step.get("name") or "").strip()
        for step in case.get("steps") or []
    )


_COUNT_WAIT_S = 32.0
_COUNT_POLL_S = 0.4
_LOADING_WORDS = ("加载中", "正在查询")


def _count_wait_seconds(session) -> float:
    """真实窗口才等结果条数变化。测试替身没有 native，不等。"""
    custom = getattr(session, "count_wait", None)
    if isinstance(custom, (int, float)) and not isinstance(custom, bool):
        return max(0.0, float(custom))
    if getattr(session, "native", None) is None:
        return 0.0
    return _COUNT_WAIT_S


def _count_poll_seconds(session) -> float:
    wait = _count_wait_seconds(session)
    if wait <= 0:
        return 0.0
    if wait < _COUNT_WAIT_S:
        return min(0.05, wait / 4)
    return _COUNT_POLL_S


def _stop_marker(run_dir: Path) -> bool:
    marker = run_dir / "stop.requested"
    return marker.is_file() and not marker.is_symlink()


def _page_loading(session, app: str) -> bool:
    query = getattr(session, "query", None)
    if not callable(query):
        return False
    for word in _LOADING_WORDS:
        try:
            found = query(app, word, "")
        except (OSError, OcuError, TypeError):
            continue
        for item in _matches(found):
            label = _control_label(item)
            if word in label and not _is_entry(str(item.get("role") or "")):
                return True
    return False


def _count_is_new(text: str, before: str, loading: bool, saw_loading: bool) -> bool:
    from qa_platform.runner.report import page_result_count

    current = page_result_count(text)
    if not current or loading:
        return False
    if before and current != before:
        return True
    return saw_loading


def _wait_for_count(session, app: str, before: str, run_dir: Path) -> tuple[str, bool]:
    """点查询之后，等到「共 N 条」变成另一个数字，或加载出现后又消失。"""
    from qa_platform.runner.report import page_result_count

    latest = _probe_count(session, app)
    if _count_is_new(latest, before, _page_loading(session, app), False):
        return latest, True
    note(run_dir, step="执行", basis="查询已点下去，正在等结果条数变化")
    deadline = time.monotonic() + _count_wait_seconds(session)
    pause = _count_poll_seconds(session)
    saw_loading = False
    while time.monotonic() < deadline:
        if _stop_marker(run_dir):
            return latest, False
        if pause:
            time.sleep(pause)
        loading = _page_loading(session, app)
        saw_loading = saw_loading or loading
        latest = _probe_count(session, app)
        if _count_is_new(latest, before, loading, saw_loading):
            return latest, True
    current = page_result_count(latest)
    return latest, bool(current and before and current != before)


def _probe_count(session, app: str) -> str:
    """只留下名称正好是「共 N 条」的行。长标题里夹着「条」不算结果。"""
    from qa_platform.runner.report import page_result_count

    query = getattr(session, "query", None)
    if not callable(query):
        return ""
    try:
        found = query(app, "共", "")
    except (OSError, OcuError, TypeError):
        return ""
    lines = []
    for item in _matches(found):
        if _is_entry(str(item.get("role") or "")):
            continue
        row = f"{item.get('role') or ''} 名称「{_control_label(item)}」".strip()
        if page_result_count(row):
            lines.append(row)
    return "\n".join(lines)


def _with_count(session, app: str, page: str, wanted: bool) -> tuple[str, str]:
    """这条用例要看条数时，把页面上的「共 N 条」补进当前页。不要条数时不查。"""
    if not wanted:
        return page, ""
    from qa_platform.runner.report import page_result_count

    found = _probe_count(session, app)
    if not found:
        return page, ""
    if page_result_count(page):
        return page, found
    return (f"{page}\n{found}" if page else found), found


def _exact_seen(result: dict, wanted: str) -> str:
    """点完只回看名称正好是这个字的控件。浏览器外壳和更长的名字不带上。"""
    lines = []
    for item in _matches(result):
        label = _control_label(item)
        if not label or label != wanted or _is_chrome_control(label) or _is_dangerous(label):
            continue
        kind = str(item.get("role") or "")
        mark = " 列表字段" if _is_list_field(kind) else (" 可以填写" if _is_entry(kind) else "")
        lines.append(
            f"{item.get('element_index') or item.get('index') or ''} "
            f"{kind} 名称「{label}」{mark}".strip()
        )
        if len(lines) >= 4:
            break
    if lines:
        return "\n".join(lines)
    return f"页面上没有名称正好是「{_clip_label(wanted)}」的控件。"


def _page_for_result(session, app: str, page: str, case: dict, phase: str, typed: str) -> str:
    from qa_platform.runner.report import expects_result_count, filled_values, page_result_count, proof_texts

    parts = [page] if page else []
    joined = "\n".join(parts)
    needles = proof_texts(case, phase) + filled_values(case)
    if not filled_values(case):
        needles.extend(_split_values(typed))
    for item in needles:
        if item and item not in joined:
            found = _probe_text(session, app, item)
            if found:
                parts.append(found)
                joined = "\n".join(parts)
    if expects_result_count(case, phase) and not page_result_count(joined):
        found = _probe_count(session, app)
        if found:
            parts.append(found)
    return "\n".join(parts)


def _capture_shot(run_dir: Path, session, app: str, case: dict, base_url: str = "") -> dict | None:
    getter = getattr(session, "get_app_state", None)
    if not callable(getter):
        note(run_dir, step="执行", basis="这一轮没有拿到窗口截图")
        return None
    try:
        result = getter(app, include_screenshot=True)
    except TypeError:
        try:
            result = getter(app)
        except (OSError, OcuError, TypeError):
            note(run_dir, step="执行", basis="这一轮没有拿到窗口截图")
            return None
    except (OSError, OcuError):
        note(run_dir, step="执行", basis="这一轮没有拿到窗口截图")
        return None
    observed = observe(result if isinstance(result, dict) else {})
    if not _shot_matches_product(str(observed.get("text") or ""), base_url):
        note(run_dir, step="执行", basis="结束时前台窗口不是产品页")
        return None
    image = observed.get("screenshot") or b""
    shot = _save_shot(
        run_dir,
        str(case.get("id") or "case"),
        len(case.get("rounds") or []) + 1,
        image if isinstance(image, (bytes, bytearray)) else b"",
    )
    if shot is None:
        note(run_dir, step="执行", basis="这一轮没有拿到窗口截图")
    return shot


_GENERIC_PAGE = {"当前", "这个", "该", "本", "同一", "对应", "结果", "列表", "这里", "上面", "下面"}


def _control_names(case: dict) -> set[str]:
    return {
        str(step.get("name") or "").strip()
        for step in (case.get("steps") or [])
        if isinstance(step, dict) and str(step.get("name") or "").strip()
    }


def _target_page_name(case: dict, brief: str) -> str:
    """这一条要测的页或页签。代码里写了页签就用页签，否则从目标里的「在某页」取。"""
    name = ""
    if "页面页签是「" in brief:
        name = brief.split("页面页签是「", 1)[1].split("」", 1)[0].strip()
    if not name:
        match = re.search(r"在([^，。；\s]{2,16})页", str(case.get("goal") or ""))
        if match:
            name = match.group(1).strip("「」")
    if not name or name in _GENERIC_PAGE or name in _control_names(case):
        return ""
    return name


def _start_names(case: dict, plan: dict, target: str) -> list[str]:
    """走到起点要看到的名字。没有写具体起点时，不额外要求。"""
    from qa_platform.runner.planner import case_start

    raw = case_start(case, plan)
    names: list[str] = []
    if raw.startswith("从「") and "」" in raw:
        part = raw.split("「", 1)[1].split("」", 1)[0].strip()
        if part:
            names.append(part)
    elif raw != "从当前已经打开的页面开始。":
        for part in re.split(r"\s*[-—－]\s*", raw):
            part = part.strip("「」 ")
            if part and part not in names and part not in _GENERIC_PAGE:
                names.append(part)
    if target and target not in names:
        names.append(target)
    return names


def _window_url(text: str) -> str:
    found = re.findall(r"URL:\s*(https?://[^\s,]+)", text or "", flags=re.I)
    return found[-1] if found else ""


def _quoted_labels(page: str) -> list[str]:
    return re.findall(r"名称「([^」]+)」", page or "")


def _exact_named(session, app: str, name: str) -> tuple[bool, str]:
    """同名控件算数。别的控件名字里只是夹着这几个字，返回那个更长的名字。"""
    query = getattr(session, "query", None)
    longer = ""
    exact = False
    if name and callable(query):
        try:
            found = query(app, name, "")
        except (OSError, OcuError, TypeError):
            found = {}
        for item in _matches(found):
            label = _control_label(item)
            if label == name:
                exact = True
            elif name in label and not longer:
                longer = label
    return exact, longer


def _name_facts(session, app: str, names: list[str], page: str) -> tuple[bool, list[str]]:
    lines = []
    arrived = True
    labels = _quoted_labels(page)
    for name in names:
        exact, longer = _exact_named(session, app, name)
        if not longer:
            for label in labels:
                if label != name and name in label:
                    longer = label
                    break
        if exact:
            if _named_selected(session, app, name) is False:
                lines.append(f"起点「{name}」：页面上有这个页签，当前还没选中。")
            else:
                lines.append(f"起点「{name}」：当前窗口里有一个控件就叫这个名字。")
        else:
            arrived = False
            line = f"起点「{name}」：当前窗口里没有一个控件就叫这个名字。"
            if longer:
                line += f"「{longer}」里夹着这几个字，不是这个控件。"
            lines.append(line)
    return arrived, lines


def _named_selected(session, app: str, name: str) -> bool | None:
    query = getattr(session, "query", None)
    if not name or not callable(query):
        return None
    try:
        items = _matches(query(app, name, ""))
    except (OSError, OcuError, TypeError):
        return None
    found = [item for item in items if _control_label(item) == name]
    if not found:
        return None
    if any(_is_selected(item) for item in found):
        return True
    if any(_role_key(str(item.get("role") or "")) in {"tab", "radiobutton", "radio"} for item in found):
        return False
    return None


def _situation(goal: str, facts: list[str], url: str, previous: str | None, last: str, target: str = "") -> str:
    if not url:
        where = "窗口地址：这一轮没有读到地址。"
    elif previous is None:
        where = f"窗口地址：{url}。"
    elif previous == url:
        where = f"窗口地址：{url}，和上一轮一样。"
    else:
        where = f"窗口地址：已从 {previous} 变为 {url}。"
    lines = [f"当前目标：{goal}", where, *facts]
    if target:
        lines.append(f"这一条要测页签「{target}」。当前不是这一项就先点它。")
    lines.append(f"上一动作：{last or '还没有。'}")
    return "\n".join(lines)


def _product_shell(page: str) -> bool:
    """这一条要填的录入控件已经在当前页上。空白页和还没打开的菜单不算。"""
    return "当前能填：" in page and "还没有找到输入框" not in page


def _window_host_ready(page: str, base_url: str) -> bool | None:
    """True 地址属于这次的测试站，False 是别的窗口，None 树里没有地址。"""
    host = (urlparse(base_url or "").hostname or "").casefold()
    found = _window_hosts(page)
    if not found:
        return None
    if not host:
        return True
    return any(item == host for item in found)


def _chrome_only(page: str) -> bool:
    """无障碍树里只有浏览器自身控件。产品自己的按钮或录入框不算。"""
    if not page or not any(word in page for word in _CHROME_WORDS):
        return False
    if _product_shell(page):
        return False
    for line in page.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((
            "这一步", "当前能填", "当前能点", "同名", "输入框：", "可点：", "这份页面",
        )):
            continue
        if any(word in stripped for word in _CHROME_WORDS):
            continue
        if stripped.startswith(("URL:", "http://", "https://")):
            continue
        low = stripped.lower()
        if any(token in low for token in ("button", "radio", "checkbox", "link", "menuitem", "tab", "textfield", "textbox")):
            return False
    return True


def _login_page(page: str) -> bool:
    url = _window_url(page)
    if not url:
        return False
    path = urlparse(url).path or ""
    return bool(re.search(r"/(?:login|signin|sign-in|sso|auth)(?:/|$)", path, flags=re.I))


def _window_unready(page: str, base_url: str) -> bool:
    """当前窗口还不是这次要测的产品页。"""
    host = _window_host_ready(page, base_url)
    if host is False:
        return True
    if _chrome_only(page) or _login_page(page):
        return True
    return host is not True and not _product_shell(page)


def _mentioned(page: str, name: str) -> bool:
    if not name or name not in (page or ""):
        return False
    from qa_platform.runner.report import absence_line

    for line in page.splitlines():
        if name in line and not absence_line(line):
            return True
    return False


def _surface_known(state: dict, name: str) -> bool:
    found = state.get("seen_surfaces")
    return bool(name) and isinstance(found, list) and name in found


def _mark_surface(run_dir, state: dict, name: str) -> None:
    name = (name or "").strip()
    if not name or _surface_known(state, name):
        return
    fresh = read_state(run_dir) or {}
    found = fresh.get("seen_surfaces")
    if not isinstance(found, list):
        found = []
    if name not in found:
        found.append(name)
    fresh["seen_surfaces"] = found
    write_json(run_dir / "state.json", fresh)
    state["seen_surfaces"] = list(found)


def _model_skip_reason(text: str) -> str:
    reason = text.strip()
    if not reason or any(word in reason for word in _HIDDEN_WORDS):
        return "这一条没有继续做"
    return reason


def _page_probe(text: str, target: str, case: dict) -> bool:
    name = text.strip()
    if not target or not name or name in _control_names(case):
        return False
    if name == target:
        return True
    return len(name) >= 3 and target.startswith(name)


def _is_address_bar(text: str) -> bool:
    name = text.strip()
    return "地址和搜索" in name or name in {"地址栏", "网址栏"}


def _is_chrome_control(text: str) -> bool:
    name = (text or "").strip()
    if not name:
        return False
    if _is_address_bar(name):
        return True
    if any(word in name for word in _CHROME_WORDS):
        return True
    return "搜索标签页" in name or "内存使用率" in name


def _skip_case(run_dir, state, case, session, app: str, reason: str, base_url: str) -> None:
    shot = _capture_shot(run_dir, session, app, case, base_url)
    case["status"] = "skipped"
    case["result_reason"] = reason
    case["blocked_reason"] = reason
    case["observation"] = None
    case["sealed"] = True
    state["events"] = read_state(run_dir).get("events") or state.get("events")
    _remember(run_dir, state, case, reason, reason, shot)
    note(run_dir, step="执行", basis=reason)


def _skip_hidden_page(run_dir, state, case, session, app: str, name: str, base_url: str) -> None:
    """当前已打开的产品界面没有这一项。记下原因后结束这一条，不改地址，也不再找。"""
    label = name.strip() or "这一项"
    _skip_case(run_dir, state, case, session, app, f"当前部署的界面没有「{label}」，已跳过", base_url)


def _block_unopened(run_dir, state, case) -> None:
    case["status"] = "blocked"
    case["result_reason"] = PAGE_NOT_OPEN
    case["blocked_reason"] = PAGE_NOT_OPEN
    case["observation"] = None
    case["sealed"] = True
    _write_case(run_dir, case)
    state["events"] = read_state(run_dir).get("events") or state.get("events")
    record(run_dir, exit_code=2, event_status="阻塞", basis=PAGE_NOT_OPEN, step="执行")


def _close_case(run_dir, state, case, session, app: str, page: str, history: list[str], phase: str, typed: str, base_url: str = "") -> None:
    page = _page_for_result(session, app, page, case, phase, typed)
    shot = _capture_shot(run_dir, session, app, case, base_url)
    summary = "；".join(history[-6:]) or "这一轮结束"
    state["events"] = read_state(run_dir).get("events") or state.get("events")
    _remember(run_dir, state, case, summary, page[:4000], shot)
    case["status"] = "running"
    case["observed"] = True
    case["observation"] = "final"
    _write_case(run_dir, case)


def _think_and_act(
    run_dir, state, case, plan, session, model, phase: str, app: str, typed: str,
    *, ask=None, empty_data: bool = False, unqueried: bool = False,
) -> int:
    repo = str(state.get("repo_path") or "")
    base_url = str(state.get("base_url") or "")
    case["data_empty"] = empty_data
    case["data_unqueried"] = unqueried
    from qa_platform.runner.baseline import _requirement_text
    from qa_platform.runner.planner import case_goal
    brief = _plan_brief(
        case, plan, phase, typed, repo, _requirement_text(run_dir, state),
        str(state.get("front_repo_path") or ""),
    )
    target = _target_page_name(case, brief)
    if target:
        case["surface_name"] = target
    goal = case_goal(case)
    names = _start_names(case, plan, target)
    page = ""
    if "页面页签是「" in brief:
        tab = brief.split("页面页签是「", 1)[1].split("」", 1)[0]
        note(run_dir, step="执行", basis=f"代码里这一页的页签是「{tab}」")
    history: list[str] = []
    reads = 0
    acted = False
    unready = 0
    noted_fill = False
    previous_url: str | None = None
    last_action = ""
    tried_clicks: set[tuple[str, str]] = set()
    fill_names, click_names, select_names = _step_targets(case, phase)
    from qa_platform.runner.report import expects_result_count, filled_values, page_result_count

    wants_count = expects_result_count(case, phase)
    count_line = ""
    query_names = _query_click_names(case, phase)
    count_pending = False
    query_settled = False
    query_before = ""
    note(run_dir, step="执行", basis=f"正在对照测试方案和项目代码，操作「{app}」里的 {case.get('id') or ''}")
    if (empty_data or unqueried) and _case_types(case, phase):
        from qa_platform.runner.report import NO_DATA, UNQUERIED

        reason = NO_DATA if empty_data else UNQUERIED
        _skip_case(run_dir, state, case, session, app, reason, base_url)
        return 0
    ask = ask or _ask_to_continue
    closed = False

    def leave(code: int) -> int:
        nonlocal closed
        if code != 0 and acted and not closed:
            _close_case(run_dir, state, case, session, app, page, history, phase, typed, base_url)
            closed = True
        return code

    def hidden(name: str) -> int:
        nonlocal closed
        _skip_hidden_page(run_dir, state, case, session, app, name or "这一项", base_url)
        closed = True
        return 0

    def finish_skip(model_text: str) -> int | None:
        nonlocal closed
        from qa_platform.runner.report import NO_DATA
        if empty_data:
            _skip_case(run_dir, state, case, session, app, NO_DATA, base_url)
            closed = True
            return 0
        if _window_unready(page, base_url):
            history.append("窗口还只是浏览器。先打开产品页，不要跳过。")
            note(run_dir, step="执行", basis=history[-1])
            return None
        if target and (_surface_known(state, target) or _mentioned(page, target)):
            _mark_surface(run_dir, state, target)
            _skip_case(run_dir, state, case, session, app, _model_skip_reason(model_text), base_url)
            closed = True
            return 0
        return hidden(target)

    while True:
        for _ in range(ROUND_LIMIT):
            if manual_stop(run_dir):
                return leave(2)
            seen_now = _observe_page(session, app, fill_names, click_names, select_names, tab_names=names)
            if seen_now:
                page = seen_now
            if wants_count and count_pending and _count_wait_seconds(session) > 0:
                fresh = _probe_count(session, app)
                current = page_result_count(fresh)
                if current and query_before and current != query_before:
                    count_pending = False
                    query_settled = True
                    count_line = fresh
                elif fresh:
                    count_line = fresh
            if wants_count and count_line and not page_result_count(page):
                page = f"{page}\n{count_line}" if page else count_line
            shell = _product_shell(page)
            _arrived, facts = _name_facts(session, app, names, page)
            if _mentioned(page, target):
                _mark_surface(run_dir, state, target)
            if _window_unready(page, base_url):
                unready += 1
                if unready >= UNREADY_LIMIT:
                    _block_unopened(run_dir, state, case)
                    closed = True
                    return 2
            else:
                unready = 0
            if shell and not noted_fill:
                note(run_dir, step="执行", basis="页面上已经找到能填的输入框")
                noted_fill = True
            url = _window_url(page)
            if history:
                last_action = history[-1][:180]
            situation = _situation(goal, facts, url, previous_url, last_action, target)
            count = page_result_count(page) if wants_count else ""
            if wants_count and count_pending and query_before:
                situation += f"\n页面上还是点查询之前的「{query_before}」。等这一行变成这次查询的结果再结束。"
            elif wants_count and count and (query_settled or not query_names):
                situation += f"\n页面结果：{count}。这是查询后的条数，和输入条数不同也算结果。"
            prompt = _agent_prompt(brief, repo, page, history, situation)
            if url:
                previous_url = url
            try:
                decision = model(prompt)
            except (OSError, ValueError, TypeError):
                record(run_dir, exit_code=2, event_status="阻塞", basis="模型没有返回可执行的操作", step="执行")
                return leave(2)
            if not isinstance(decision, dict):
                record(run_dir, exit_code=2, event_status="阻塞", basis="模型没有返回可执行的操作", step="执行")
                return leave(2)
            reasoning = str(decision.get("_reasoning") or "").strip()
            if reasoning:
                note(run_dir, step="执行", basis=f"思考：{reasoning[:420]}")
            thought = str(decision.get("think") or "").strip()
            if thought:
                note(run_dir, step="执行", basis=f"想：{thought[:240]}")
                history.append(thought[:240])
            action = str(decision.get("action") or "").strip()
            if decision.get("done") is True or action.casefold() == "done":
                if not acted:
                    history.append("还没有在页面上填写或点击，不能结束")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                if wants_count and query_names and not query_settled:
                    if count_pending and _count_wait_seconds(session) > 0:
                        fresh = _probe_count(session, app)
                        current = page_result_count(fresh)
                        if current and query_before and current != query_before:
                            count_pending = False
                            query_settled = True
                            count_line = fresh
                            if current not in (page or ""):
                                page = f"{page}\n{fresh}" if page else fresh
                    if not query_settled:
                        if count_pending and query_before:
                            pending = f"结果条数还是点查询之前的「{query_before}」。等它变掉再结束。"
                        else:
                            pending = "还没有看到这次查询的结果。等页面上的「共 N 条」变化后再结束。"
                        history.append(pending)
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                from qa_platform.runner.report import judge_final
                checked = _page_for_result(session, app, page, case, phase, typed)
                page = checked or page
                prepared = [] if empty_data else (_split_values(typed) or None)
                prior = query_before if wants_count and query_before and not query_settled else ""
                outcome, reason = judge_final(
                    case, checked, prepared, data_empty=empty_data, prior_count=prior,
                )
                if outcome == "wait":
                    history.append(reason)
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                if reason:
                    history.append(reason)
                    note(run_dir, step="执行", basis=history[-1])
                _close_case(run_dir, state, case, session, app, page, history, phase, typed, base_url)
                closed = True
                return 0
            text = _named(str(decision.get("text") or ""))
            role = str(decision.get("role") or "")
            read_path = decision.get("read")
            if action == "read" and not read_path:
                read_path = decision.get("text") or decision.get("path") or ""
            if decision.get("read") or action == "read":
                if _window_unready(page, base_url):
                    history.append("窗口还只是浏览器，先看页面。")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                path = str(read_path or "").strip()
                if not path:
                    history.append("读代码要带上相对路径。")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                if reads >= 4:
                    history.append("代码已经看过几次，回到页面")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                reads += 1
                excerpt = _read_repo(repo, path, str(state.get("front_repo_path") or ""))
                history.append(f"读了 {path}：{excerpt[:500]}")
                page = page or "还没有看页面"
                note(run_dir, step="执行", basis=history[-1][:240])
                continue
            if action == "skip":
                ended = finish_skip(str(decision.get("text") or ""))
                if ended is None:
                    continue
                return ended
            if action not in {"look", "click", "type", "select", "set", "scroll", "press"}:
                history.append("上一条回复没有可执行的动作。下一句只返回一个 JSON，带 think 和 action。")
                note(run_dir, step="执行", basis=history[-1])
                continue
            if action in {"click", "type", "select", "set"} and not text:
                history.append("上一条回复没有写明要点或要填的控件。下一句的 JSON 带上 text。")
                note(run_dir, step="执行", basis=history[-1])
                continue
            if action == "press" and not str(decision.get("key") or "").strip():
                history.append("按键没有给出 key")
                note(run_dir, step="执行", basis=history[-1])
                continue
            quiet_look = False
            try:
                if action == "look" and not text:
                    page = _see_page(session, app) or page or "还没有整页内容"
                    page, count_line = _with_count(session, app, page, wants_count) if wants_count else (page, count_line)
                    history.append(f"看到当前页面：{page[:400]}")
                elif action == "look":
                    looked = session.query(app, text, "" if _role_key(role) in {"textbox", "text", "searchbox"} else role)
                    page = _page_lines(looked, text)
                    if page.startswith("页面上没有找到") and role:
                        page = _page_lines(session.query(app, text, ""), text)
                    if _page_probe(text, target, case) and not page.startswith("页面上没有找到"):
                        _mark_surface(run_dir, state, target)
                    page, count_line = _with_count(session, app, page, wants_count) if wants_count else (page, count_line)
                    history.append(f"看到「{text}」：{page[:240]}")
                    quiet_look = page.startswith("页面上没有找到")
                elif action == "click":
                    if _is_chrome_control(text):
                        history.append("不要点浏览器标签页和地址栏。")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if _looks_like_payload(text, typed):
                        history.append(SHAPE_HINT)
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    pressed = None
                    if text in fill_names or _role_key(role) in {"textbox", "text", "searchbox"}:
                        index, _found = _entry_index(session, app, text)
                        if not index:
                            history.append(f"「{_clip_label(text)}」是要填的输入框，页面上还没有这个框。")
                            note(run_dir, step="执行", basis=history[-1])
                            continue
                    else:
                        found = session.query(app, text, role)
                        pressed = _pick_match(found, text, role) or _pick_match(session.query(app, text, ""), text, role)
                        if pressed is None:
                            page = f"页面上没有可点的「{_clip_label(text)}」。重转、删除、提交不会点。"
                            history.append(page)
                            note(run_dir, step="执行", basis=page[:240])
                            continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备点击「{text}」")
                    wait_for_result = bool(
                        wants_count and text in query_names and filled_values(case) and _count_wait_seconds(session) > 0
                    )
                    if wait_for_result:
                        query_before = page_result_count(count_line) or page_result_count(page)
                        if not query_before:
                            query_before = page_result_count(_probe_count(session, app))
                    before = _window_url(page)
                    if pressed is None:
                        session.click(app, index)
                        on_control = True
                    else:
                        on_control = _press(session, app, pressed, text)
                    acted = True
                    if _page_probe(text, target, case):
                        _mark_surface(run_dir, state, target)
                    after = _window_url(_see_page(session, app))
                    stay = ""
                    if before and after and before == after:
                        key = (text, before)
                        stay = "这个动作已经做过，窗口没变。" if key in tried_clicks else "窗口还在原处。"
                        tried_clicks.add(key)
                    elif before and after and before != after:
                        stay = f"窗口已离开原处，现在是 {after}。"
                        tried_clicks.clear()
                    landed = "" if on_control else "点的是文字，已按它的位置点下去。看它有没有被选中。"
                    last_action = f"点击「{text}」。{stay}".strip()
                    page = _exact_seen(session.query(app, text, ""), text)
                    if wait_for_result:
                        found, changed = _wait_for_count(session, app, query_before, run_dir)
                        if _stop_marker(run_dir) and manual_stop(run_dir):
                            return leave(2)
                        if found:
                            count_line = found
                            fresh_count = page_result_count(found)
                            if fresh_count and fresh_count not in page:
                                page = f"{page}\n{found}" if page else found
                        count_pending = not changed
                        query_settled = changed
                    else:
                        page, count_line = _with_count(session, app, page, wants_count) if wants_count else (page, count_line)
                        if wants_count and text in query_names and filled_values(case):
                            query_before = ""
                            count_pending = False
                            query_settled = True
                        elif wants_count and not query_names and page_result_count(count_line or page):
                            query_settled = True
                    history.append(f"已点击「{text}」。{stay}{landed}点完看到：{page[:240]}")
                elif action == "select":
                    option = str(decision.get("option") or "").strip()
                    entry = _entry_index(session, app, text)[0]
                    opener = None if entry else _pick_match(session.query(app, text, ""), text, role)
                    if not entry and opener is None:
                        history.append(f"没有可选择的「{text}」")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备选择「{text}」" + (f"为「{option}」" if option else ""))
                    if entry:
                        session.click(app, entry)
                    else:
                        _press(session, app, opener, text)
                    acted = True
                    if option:
                        opt_item = _pick_match(session.query(app, option, ""), option, "")
                        if opt_item is None:
                            page = _page_lines(session.query(app, option, ""), option)
                            history.append(f"已点开「{text}」，没有看到选项「{option}」。看到：{page[:160]}")
                            note(run_dir, step="执行", basis=history[-1][:240])
                            continue
                        _press(session, app, opt_item, option)
                    page = _page_lines(session.query(app, option or text, ""), option or text)
                    history.append(f"已选择「{text}」" + (f"为「{option}」" if option else "") + f"。选完看到：{page[:160]}")
                elif action == "set":
                    if _looks_like_payload(text, typed):
                        history.append(SHAPE_HINT)
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    found = session.query(app, text, role)
                    chosen = _pick_match(found, text, role) or _pick_match(session.query(app, text, ""), text, "")
                    if chosen is None:
                        history.append(f"没有可勾选的「{_clip_label(text)}」")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备勾选「{text}」")
                    _press(session, app, chosen, text)
                    acted = True
                    page = _page_lines(session.query(app, text, ""), text)
                    history.append(f"已勾选「{text}」。勾完看到：{page[:160]}")
                elif action == "scroll":
                    scroller = getattr(session, "scroll", None)
                    if not callable(scroller):
                        history.append("这一步不能滚动")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if not text:
                        history.append("滚动需要页面上的一块文字，用来定位区域")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    found = session.query(app, text, "")
                    index = _match_index(found, text, "") or next(
                        (str(item.get("element_index") or item.get("index") or "") for item in _matches(found) if item.get("element_index") is not None or item.get("index") is not None),
                        "",
                    )
                    if not index:
                        history.append(f"没有可滚动的「{text}」")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    direction = str(decision.get("direction") or "down")
                    if direction not in {"up", "down", "left", "right"}:
                        direction = "down"
                    note(run_dir, step="执行", basis=f"准备滚动「{text}」{direction}")
                    scroller(app, index, direction)
                    acted = True
                    page = _observe_page(session, app, fill_names, click_names, select_names, tab_names=names) or _page_lines(session.query(app, text, ""), text)
                    history.append(f"已滚动「{text}」。滚完看到：{page[:180]}")
                elif action == "press":
                    press = getattr(session, "press_key", None)
                    key = str(decision.get("key") or "").strip()
                    if not callable(press):
                        history.append("这一步不能按键")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备按 {key}")
                    press(app, key)
                    acted = True
                    history.append(f"已按 {key}")
                elif action == "type":
                    if _is_address_bar(text):
                        history.append("不要改地址。")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    value_raw = str(decision.get("value") or "")
                    if _looks_like_payload(text, typed):
                        if value_raw.strip() and not _short_label(value_raw, typed):
                            history.append(SHAPE_HINT)
                            note(run_dir, step="执行", basis=history[-1])
                            continue
                        index, control = _entry_for_payload(session, app, value_raw, fill_names, typed)
                        if not index:
                            history.append(SHAPE_HINT)
                            note(run_dir, step="执行", basis=history[-1])
                            continue
                        value = text.strip()
                        text = control
                    else:
                        index, found = _entry_index(session, app, text)
                        if not index:
                            page = _page_lines(found, text)
                            if page.startswith("页面上没有找到"):
                                page = _page_lines(session.query(app, text, ""), text)
                            history.append(f"没有填「{_clip_label(text)}」。这一步看到的是：{page[:240]}")
                            note(run_dir, step="执行", basis=history[-1][:240])
                            continue
                        value = _fill_value(value_raw, typed)
                    if not value:
                        history.append("这一步没有给出要填的内容，也没有准备好的数据")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    preview = value if len(value) <= 48 else f"{value[:48]}…"
                    note(run_dir, step="执行", basis=f"准备填写「{text}」：{preview}")
                    session.click(app, index)
                    session.set_value(app, index, value)
                    acted = True
                    _remember_filled(case, value)
                    page = _page_lines(session.query(app, text, ""), text)
                    if page.startswith("页面上没有找到"):
                        page = "填写动作已经完成。"
                    history.append(f"已填写「{text}」。填完看到：{page[:180]}")
            except (OSError, OcuError):
                record(run_dir, exit_code=2, event_status="阻塞", basis="按方案操作页面失败", step="执行")
                return leave(2)
            if not quiet_look:
                note(run_dir, step="执行", basis=history[-1][:240])
        if ask(run_dir) != "continue":
            return leave(2)


def _run_case(run_dir, state, case, later, plan, base_url, session, model, query, phase: str = "test", app: str = APP) -> int:
    if case.get("status") == "waiting":
        case["status"] = "running"
        _write_case(run_dir, case)
    plan_steps = [
        step for step in (case.get("steps") or [])
        if isinstance(step, dict) and step.get("phase", "test") == phase
    ]
    if plan_steps and model is not None and hasattr(session, "query") and hasattr(session, "click") and hasattr(session, "set_value"):
        saved = (_load_json(run_dir / "baseline.json") or {}).get("test_values") or {}
        check_id = case.get("check_id")
        real = saved.get(check_id) if isinstance(saved, dict) else None
        empty_data = isinstance(real, list) and not real
        unqueried = isinstance(saved, dict) and bool(saved) and bool(check_id) and check_id not in saved
        typed = ",".join(str(item) for item in real) if isinstance(real, list) and real else ""
        return _think_and_act(
            run_dir, state, case, plan, session, model, phase, app, typed,
            empty_data=empty_data, unqueried=unqueried,
        )
    note(run_dir, step="执行", basis=f"正在让模型按方案在「{app}」里操作 {case.get('id') or ''}")
    session.reset()
    if case.get("pending"):
        expect = str(case["pending"].get("expect_state") or "")
        if expect and _db_matches(run_dir, case, expect, query) is True:
            case["pending"] = None
            case["status"] = "running"
            _remember(run_dir, state, case, "库已符合预期，未再次提交", case.get("seen") or "", None)
        else:
            case["status"] = "paused"
            _write_case(run_dir, case)
            record(run_dir, exit_code=3, event_status="暂停", basis="页面和库没有变化", step="执行")
            return 3
    for _ in range(8):
        saved = (_load_json(run_dir / "baseline.json") or {}).get("test_values") or {}
        real = saved.get(case.get("check_id")) if isinstance(saved, dict) else None
        typed = ",".join(str(item) for item in real) if isinstance(real, list) and real else ""
        try:
            decision = model(_case_prompt(case, plan, base_url, phase, typed, app, str(case.get("seen") or "")))
        except (OSError, ValueError, TypeError):
            record(run_dir, exit_code=2, event_status="阻塞", basis="模型没有返回可执行的操作", step="执行")
            return 2
        if not isinstance(decision, dict) or decision.get("done"):
            case["status"] = "running"
            case["observed"] = True
            _write_case(run_dir, case)
            return 0
        code = str(decision.get("js") or "")
        if not code.strip():
            record(run_dir, exit_code=2, event_status="阻塞", basis="模型没有返回浏览器操作，没有按方案执行", step="执行")
            return 2
        try:
            assert_product_js(code, app)
        except Exception:
            case["status"] = "blocked"
            case["blocked_reason"] = "控件环境错误"
            _write_case(run_dir, case)
            for item in later:
                item["status"] = "waiting"
                _write_case(run_dir, item)
            record(run_dir, exit_code=2, event_status="阻塞", basis="控件环境错误", step="执行")
            return 2
        if any(found and not navigation_allowed(found, base_url) for found in URL_RE.findall(code)):
            return _block_environment(run_dir, state, case, later)
        danger = _danger(code, case)
        if danger:
            case["status"] = "paused"
            case["hold"] = danger
            _write_case(run_dir, case)
            for item in later:
                item["status"] = "waiting"
                _write_case(run_dir, item)
            record(run_dir, exit_code=3, event_status="暂停", basis=f"{danger}不在该用例期望里", step="执行")
            return 3
        effect = decision.get("effect") or "read"
        if effect == "commit":
            case["status"] = "blocked"
            case["blocked_reason"] = "数据库只能查询"
            _write_case(run_dir, case)
            for item in later:
                item["status"] = "waiting"
                _write_case(run_dir, item)
            record(run_dir, exit_code=2, event_status="阻塞", basis="数据库只能查询，未提交", step="执行")
            return 2
        result = observe(session.run(code, app))
        seen = str(result.get("text") or "")
        if "unsupportedTool" in seen:
            record(run_dir, exit_code=2, event_status="阻塞", basis="computer use 没有 js 通道，没有操作页面", step="执行")
            return 2
        current = str(result.get("url") or base_url)
        shot = _save_shot(run_dir, str(case["id"]), len(case.get("rounds") or []) + 1, result.get("screenshot") or b"")
        note(
            run_dir,
            step="执行",
            basis=f"已让模型在「{app}」按方案操作" + (f"，读到：{seen[:60]}" if seen else "，页面没有返回内容"),
        )
        _remember(run_dir, state, case, code[:180], seen, shot)
        if not navigation_allowed(current, base_url):
            return _block_environment(run_dir, state, case, later)
    case["status"] = "paused"
    _write_case(run_dir, case)
    record(run_dir, exit_code=3, event_status="暂停", basis="界面没有继续变化", step="执行")
    return 3


def _baseline_approved(state: dict, baseline: dict, kind: str) -> bool:
    return find_approval(state, kind, baseline) is not None


def page_seed_ready(run_dir: Path, state: dict) -> bool:
    baseline = _load_json(run_dir / "baseline.json")
    if not isinstance(baseline, dict) or baseline.get("strategy") != "page_seed":
        return False
    if state.get("review"):
        return False
    return _baseline_approved(state, baseline, "baseline")


def _apply_committed(baseline: dict) -> bool:
    code = baseline.get("apply_exit_code")
    return type(code) is int and code == 0


def sql_test_ready(run_dir: Path, state: dict) -> bool:
    baseline = _load_json(run_dir / "baseline.json")
    if not isinstance(baseline, dict) or baseline.get("strategy") != "sql":
        return False
    if not _apply_committed(baseline) or state.get("review"):
        return False
    return _baseline_approved(state, baseline, "baseline_actual")


def _browser(browser, url: str, console_log: Path):
    if browser is None:
        browser = lambda binary, start, log: _launch(binary, start, log)
    binary = CACHE / "QA Platform Browser.app" / "Contents" / "MacOS" / "QAPlatformBrowser"
    browser(binary, url, console_log)


def _run_phase(run_dir, state, plan, base_url, session, model, query, phase: str, app: str) -> int:
    cases = [case for case in _prepare_cases(run_dir, plan, phase) if case.get("steps")]
    for index, case in enumerate(cases):
        if case.get("status") in {"blocked", "failed", "paused", "skipped"} and not case.get("pending"):
            continue
        outcome = _run_case(
            run_dir, state, case, cases[index + 1:], plan, base_url, session, model, query, phase, app,
        )
        if outcome != 0:
            return outcome
    return 0


def _reread_actual(run_dir: Path, baseline: dict, query) -> dict:
    state = read_state(run_dir)
    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    statements = baseline.get("select_sql") if isinstance(baseline.get("select_sql"), dict) else {}
    fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
    found = {}
    for check_id, sql in statements.items():
        fixture = fixtures.get(check_id) if isinstance(fixtures.get(check_id), dict) else {}
        database = fixture.get("database")
        if not isinstance(sql, str) or not isinstance(database, str):
            raise RuntimeError("查询失败")
        found[check_id] = query(database, sql, allowed)
    return found


def execute_page_seed(run_dir: Path, *, session=None, model=None, query=None, browser=None) -> int:
    """只跑 phase=seed，查完全部 select_sql 后停在 baseline_actual。"""
    state = read_state(run_dir)
    baseline = _load_json(run_dir / "baseline.json") or {}
    strategy = baseline.get("strategy")
    if strategy == "none":
        return 2
    if strategy != "page_seed":
        return 0
    review = state.get("review")
    if isinstance(review, dict) and review.get("kind") == "baseline_actual":
        return 4
    if not _baseline_approved(state, baseline, "baseline"):
        return 0
    record(run_dir, exit_code=2, event_status="阻塞", basis="数据库只能查询，未做页面铺垫", step="造数")
    return 2


def execute_sql_tests(run_dir: Path, *, session=None, model=None, query=None, browser=None) -> int:
    """只在改数退出码为 0 且 baseline_actual 已批准时跑 phase=test。"""
    state = read_state(run_dir)
    baseline = _load_json(run_dir / "baseline.json") or {}
    if baseline.get("strategy") == "none":
        return 2
    review = state.get("review")
    if isinstance(review, dict) and review.get("kind") == "baseline_actual":
        return 4
    if not sql_test_ready(run_dir, state):
        return 0
    plan = _load_json(run_dir / "plan.json") or {}
    base_url = str(state.get("base_url") or "")
    if session is None or model is None:
        record(run_dir, exit_code=2, event_status="阻塞", basis="没有执行会话", step="执行")
        return 2
    app_name, browser_error = show_test_page(run_dir, plan, state, session, query, browser, base_url)
    if not app_name:
        record(run_dir, exit_code=2, event_status="阻塞", basis=browser_error, step="执行")
        return 2
    outcome = _run_phase(run_dir, state, plan, base_url, session, model, query, "test", app_name)
    if outcome != 0:
        return outcome
    record(run_dir, exit_code=0, event_status="完成", basis="测试步骤已观察，尚未封口", step="执行")
    return 0
