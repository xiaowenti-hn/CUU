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
        "content": "你在已打开的产品窗口里完成一个目标。每轮只返回一个 JSON，先写 think，再写一个动作：look、type、select、set、click、scroll、press、read、skip 或 done。先走到方案里的起点，再完成这条用例的目标。做法是参考，不要被某一步绑住。要填用页面上真实的输入框。不要写 js，不要改地址。窗口还只有浏览器自身控件时不要 skip。已经能填写、并且起点里的界面名确实不在时才 skip。没有库里的数据时 skip，text 写没有数据。走到起点之后，方案里要出现的结果出现了才 done。准备的值是测试输入。页面上名称正好是「共 N 条」时，这个条数就是查询结果，可以和输入条数不同，这时每个输入值不必再出现在列表里。还没有这种结果时，写入的每个值都要出现在录入控件以外。空表上的 0 和「查询完成」不是要对照的结果。",
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
ROUND_LIMIT = 20
UNREADY_LIMIT = 3
PAGE_NOT_OPEN = "测试页没有打开"
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


def _match_index(result: dict, wanted: str = "", role: str = "") -> str | None:
    """只点名称对得上的控件。填写只认输入框，不认列表里的同名列。"""
    matches = _matches(result)
    if not matches or _is_dangerous(wanted):
        return None
    wanted_role = role.lower()

    def allowed(item: dict) -> bool:
        label = _control_label(item)
        kind = str(item.get("role") or "")
        if _is_dangerous(label):
            return False
        if not label:
            return False
        if wanted and wanted not in label and label not in wanted:
            return False
        if wanted_role in {"textbox", "text", "searchbox"}:
            return _is_entry(kind)
        if _is_list_field(kind) and wanted_role in {"button", "link"}:
            return False
        return True

    pool = [item for item in matches if allowed(item)]
    exact = [item for item in pool if _control_label(item) == wanted] if wanted else []
    chosen = exact[0] if exact else (pool[0] if pool else None)
    if not isinstance(chosen, dict):
        return None
    for key in ("element_index", "index"):
        if chosen.get(key) is not None:
            return str(chosen[key])
    return None


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
        if action == "type":
            index, _found = _entry_index(session, app, name)
        else:
            index = _match_index(session.query(app, name, role), name, role)
        if not index:
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
            session.click(app, index)
            option = str(step.get("option") or "")
            if option and "请选择" not in option:
                option_index = _match_index(session.query(app, option, ""))
                if option_index:
                    session.click(app, option_index)
            done.append(f"已点开「{name}」")
        else:
            if tell:
                tell(f"正在点击「{name}」")
            session.click(app, index)
            done.append(f"已点击「{name}」")
    return "；".join(done)


def _read_repo(repo: str, relative: str) -> str:
    root = Path(repo).resolve() if repo else None
    if root is None or not root.is_dir():
        return "没有项目目录"
    cleaned = relative.strip().lstrip("./")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        return "只能读项目里的相对路径"
    path = (root / cleaned).resolve()
    front = _front_root(root)
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
    if not entries:
        body.append("这份页面里没有看到输入框。用控件上的文字再看一次，不要连续看整页。")
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


def _front_root(repo: Path) -> Path | None:
    sibling = repo.parent / f"{repo.name}-front"
    src = sibling / "src"
    if src.is_dir() and not src.is_symlink():
        return sibling
    return None


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


def implementation_brief(repo: str, source: str) -> str:
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
    found = re.search(r'@RequestMapping\("([^"]+)"\)', java)
    if found:
        mapping = found.group(1)
    tab = ""
    fields: list[str] = []
    front = _front_root(root)
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
        lines.append(f"页面页签是「{tab}」。先点这个页签，再操作它下面的筛选。")
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


def _plan_brief(case: dict, plan: dict, phase: str, typed: str, repo: str = "", requirement: str = "") -> str:
    from qa_platform.runner.planner import acceptance_criteria, case_goal, case_start

    criteria = acceptance_criteria(requirement)
    lines = [
        f"任务：{case.get('id') or ''} {case.get('title') or ''}".strip(),
        f"目标：{case_goal(case)}",
        f"起点：{case_start(case, plan)}",
        "做法是参考。还没走到起点时，不要按下面的做法填写或点击。",
        "以当前页面上的控件为准，对不上就换控件。",
        "不要点重转、删除、作废、审核、手工转化、手工下载、提交，也不要点表格里的列名。",
        "对不上就重新看当前页面，换一个控件。做法只是参考。",
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
    lines.extend(_data_lines(typed, bool(case.get("data_empty"))))
    source = ""
    for check in plan.get("checks") or []:
        if isinstance(check, dict) and check.get("id") == case.get("check_id"):
            source = str(check.get("source") or "")
            if check.get("source"):
                lines.append(f"相关代码：{check.get('source')}")
            break
    code = implementation_brief(repo, source)
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


def _format_control(item: dict) -> str:
    kind = str(item.get("role") or "")
    label = _control_label(item)
    if _is_dangerous(label):
        mark = "不要点"
    elif _is_entry(kind):
        mark = "可填写"
    elif _is_list_field(kind):
        mark = "列表字段"
    else:
        mark = ""
    return f"名称「{label}」 {kind} {mark}".strip()


def _collect_named(session, app: str, name: str, want_entry: bool) -> list[dict]:
    items = _matches(session.query(app, name, ""))
    if want_entry and not any(_is_entry(str(item.get("role") or "")) for item in items):
        items = items + _matches(session.query(app, name, "textfield"))
    return items


def _observe_page(session, app: str, fill_names: list[str], click_names: list[str], select_names: list[str] | None = None, *, announce: bool = True) -> str:
    """挑出页面上和这条用例相关的控件。还没走到起点时不宣布打算填写或点击。"""
    select_names = select_names or []
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
                    if _is_dangerous(key[0]):
                        not_entry.append(_format_control(item))
                    else:
                        clickable.append(_format_control(item))
        except (OSError, OcuError, TypeError):
            pass
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
        "你在已打开的产品窗口里完成下面的当前目标。每轮只做一个动作，动作由你按当前目标来定。",
        "做法是参考，不是这一轮必须执行的脚本。",
        "「当前能填 / 能点」只有走到起点之后才用来填写。同名按钮和列表列不要点。",
        "动作：look 看结果；type 填输入框；select 点开下拉再选，带 option；set 勾选；click 按钮或入口；scroll 被挡住时滚动；press 按键，填写后查询用 Return；read 读代码；skip 只在已经走到承载这一页的界面、且起点里的名字确实不在时使用。窗口还只有浏览器自身控件时不要 skip。",
        "重转、删除、作废、审核、手工转化、提交不会点。先写一句 think。",
        "{\"think\":\"...\",\"action\":\"look\",\"text\":\"页面上应出现的字\"}",
        "{\"think\":\"...\",\"action\":\"type\",\"text\":\"输入框上的字\",\"value\":\"\"}",
        "{\"think\":\"...\",\"action\":\"select\",\"text\":\"下拉上的字\",\"option\":\"要选的项\"}",
        "{\"think\":\"...\",\"action\":\"set\",\"text\":\"勾选框上的字\"}",
        "{\"think\":\"...\",\"action\":\"click\",\"text\":\"按钮上的字\"}",
        "{\"think\":\"...\",\"action\":\"scroll\",\"text\":\"被挡住的区域\",\"direction\":\"down\"}",
        "{\"think\":\"...\",\"action\":\"press\",\"key\":\"Return\"}",
        "{\"think\":\"...\",\"read\":\"相对路径\"}",
        "{\"think\":\"...\",\"action\":\"skip\",\"text\":\"当前部署的界面没有这一项\"}",
        "还没走到起点时不要 done，也不要按参考做法去填。走到之后，方案里要出现的文字已经出现，才 {\"think\":\"...\",\"done\":true}。准备的值是测试输入。页面上名称正好是「共 N 条」时，这个条数就是查询结果，可以和输入条数不同，这时不必再要求每个输入值都出现在列表里。还没有这种结果时，写入的每个值都要出现在录入控件以外。空表上的 0 和「查询完成」不是要对照的结果。value 留空则用这一条准备的全部数据。",
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


def _data_lines(typed: str, empty: bool) -> list[str]:
    if empty:
        return ["这一条库里没有数据，不要用别的用例的单号。"]
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
            lines.append(f"起点「{name}」：当前窗口里有一个控件就叫这个名字。")
        else:
            arrived = False
            line = f"起点「{name}」：当前窗口里没有一个控件就叫这个名字。"
            if longer:
                line += f"「{longer}」里夹着这几个字，不是这个控件。"
            lines.append(line)
    return arrived, lines


def _page_before_arrival(page: str) -> str:
    """没走到起点时，不把参考做法和待填控件说成这一轮的任务。"""
    kept = []
    skipping = False
    for line in page.splitlines():
        if line.startswith(("参考做法", "这一步打算", "当前能填", "同名但不是输入框", "当前能点")):
            skipping = True
            continue
        if skipping and line.startswith("- "):
            continue
        skipping = False
        kept.append(line)
    return "\n".join(kept)


def _situation(goal: str, names: list[str], arrived: bool, facts: list[str], url: str, previous: str | None, last: str) -> str:
    if not names or arrived:
        current = f"当前目标：{goal}"
    else:
        joined = "」、「".join(names)
        current = f"当前目标：走到起点「{joined}」。"
    if not url:
        where = "窗口地址：这一轮没有读到地址。"
    elif previous is None:
        where = f"窗口地址：{url}。"
    elif previous == url:
        where = f"窗口地址：{url}，和上一轮一样。"
    else:
        where = f"窗口地址：已从 {previous} 变为 {url}。"
    lines = [current, where, *facts]
    if names and arrived:
        lines.append("起点：已经走到。")
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


def _window_unready(page: str, base_url: str) -> bool:
    """当前窗口还不是这次要测的产品页。"""
    if _window_host_ready(page, base_url) is False:
        return True
    return _chrome_only(page)


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


def _named_missing(session, app: str, name: str) -> bool:
    query = getattr(session, "query", None)
    if not name or not callable(query):
        return False
    try:
        found = query(app, name, "")
    except (OSError, OcuError, TypeError):
        return False
    return _page_lines(found, name).startswith("页面上没有找到")


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


def _think_and_act(run_dir, state, case, plan, session, model, phase: str, app: str, typed: str, *, ask=None, empty_data: bool = False) -> int:
    repo = str(state.get("repo_path") or "")
    base_url = str(state.get("base_url") or "")
    case["data_empty"] = empty_data
    from qa_platform.runner.baseline import _requirement_text
    from qa_platform.runner.planner import case_goal
    brief = _plan_brief(case, plan, phase, typed, repo, _requirement_text(run_dir, state))
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
    arrived = False
    noted_fill = False
    previous_url: str | None = None
    last_action = ""
    tried_clicks: set[tuple[str, str]] = set()
    fill_names, click_names, select_names = _step_targets(case, phase)
    note(run_dir, step="执行", basis=f"正在对照测试方案和项目代码，操作「{app}」里的 {case.get('id') or ''}")
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

    def can_hide() -> bool:
        return bool(
            target
            and shell
            and not _window_unready(page, base_url)
            and not _surface_known(state, target)
            and _named_missing(session, app, target)
        )

    def finish_skip(model_text: str) -> int | None:
        nonlocal closed
        from qa_platform.runner.report import NO_DATA
        if empty_data:
            _skip_case(run_dir, state, case, session, app, NO_DATA, base_url)
            closed = True
            return 0
        if target and (_surface_known(state, target) or _mentioned(page, target)):
            _mark_surface(run_dir, state, target)
            _skip_case(run_dir, state, case, session, app, _model_skip_reason(model_text), base_url)
            closed = True
            return 0
        if _window_unready(page, base_url) or not can_hide():
            history.append("页面还在加载，或还没有确认当前部署没有这一项。继续在当前窗口里做。")
            note(run_dir, step="执行", basis=history[-1])
            return None
        return hidden(target)

    while True:
        for _ in range(ROUND_LIMIT):
            if manual_stop(run_dir):
                return leave(2)
            seen_now = _observe_page(session, app, fill_names, click_names, select_names)
            if seen_now:
                page = seen_now
            shell = _product_shell(page)
            arrived, facts = _name_facts(session, app, names, page)
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
            if not acted and can_hide():
                return hidden(target)
            if arrived and shell and not noted_fill:
                note(run_dir, step="执行", basis="页面上已经找到能填的输入框")
                noted_fill = True
            url = _window_url(page)
            if history:
                last_action = history[-1][:180]
            shown = page if arrived else _page_before_arrival(page)
            situation = _situation(goal, names, arrived, facts, url, previous_url, last_action)
            if arrived:
                from qa_platform.runner.report import page_result_count

                count = page_result_count(page)
                if count:
                    situation += f"\n页面结果：{count}。这是查询后的条数，和输入条数不同也算结果。"
            prompt = _agent_prompt(brief, repo, shown, history, situation)
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
            if decision.get("done"):
                if names and not arrived:
                    history.append("还没有走到起点，不能结束")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                if not acted:
                    history.append("还没有在页面上填写或点击，不能结束")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                from qa_platform.runner.report import judge_final
                checked = _page_for_result(session, app, page, case, phase, typed)
                page = checked or page
                prepared = [] if empty_data else (_split_values(typed) or None)
                outcome, reason = judge_final(case, checked, prepared, data_empty=empty_data)
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
            if decision.get("read"):
                if can_hide():
                    return hidden(target)
                if reads >= 4:
                    history.append("代码已经看过几次，回到页面")
                    note(run_dir, step="执行", basis=history[-1])
                    continue
                reads += 1
                excerpt = _read_repo(repo, str(decision.get("read")))
                history.append(f"读了 {decision.get('read')}：{excerpt[:500]}")
                page = page or "还没有看页面"
                note(run_dir, step="执行", basis=history[-1][:240])
                continue
            action = str(decision.get("action") or "")
            text = _named(str(decision.get("text") or ""))
            role = str(decision.get("role") or "")
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
            try:
                if action == "look" and not text:
                    page = _see_page(session, app) or page or "还没有整页内容"
                    history.append(f"看到当前页面：{page[:400]}")
                elif action == "look":
                    looked = session.query(app, text, "" if _role_key(role) in {"textbox", "text", "searchbox"} else role)
                    page = _page_lines(looked, text)
                    if page.startswith("页面上没有找到") and role:
                        page = _page_lines(session.query(app, text, ""), text)
                    if _page_probe(text, target, case) and page.startswith("页面上没有找到") and can_hide():
                        return hidden(target)
                    if _page_probe(text, target, case) and not page.startswith("页面上没有找到"):
                        _mark_surface(run_dir, state, target)
                    history.append(f"看到「{text}」：{page[:240]}")
                elif action == "click":
                    click_role = "" if _role_key(role) in {"textbox", "text", "searchbox"} else role
                    found = session.query(app, text, click_role)
                    index = _match_index(found, text, role) or _match_index(session.query(app, text, ""), text, role)
                    if not index:
                        if _page_probe(text, target, case) and can_hide():
                            return hidden(target)
                        page = f"页面上没有可点的「{text}」。重转、删除、提交不会点。"
                        history.append(page)
                        note(run_dir, step="执行", basis=page[:240])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备点击「{text}」")
                    before = _window_url(page)
                    session.click(app, index)
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
                    last_action = f"点击「{text}」。{stay}".strip()
                    page = _page_lines(session.query(app, text, ""), text)
                    history.append(f"已点击「{text}」。{stay}点完看到：{page[:180]}")
                elif action == "select":
                    option = str(decision.get("option") or "").strip()
                    index = _entry_index(session, app, text)[0] or _match_index(session.query(app, text, ""), text, role)
                    if not index:
                        history.append(f"没有可选择的「{text}」")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备选择「{text}」" + (f"为「{option}」" if option else ""))
                    session.click(app, index)
                    acted = True
                    if option:
                        opt = _match_index(session.query(app, option, ""), option, "")
                        if not opt:
                            page = _page_lines(session.query(app, option, ""), option)
                            history.append(f"已点开「{text}」，没有看到选项「{option}」。看到：{page[:160]}")
                            note(run_dir, step="执行", basis=history[-1][:240])
                            continue
                        session.click(app, opt)
                    page = _page_lines(session.query(app, option or text, ""), option or text)
                    history.append(f"已选择「{text}」" + (f"为「{option}」" if option else "") + f"。选完看到：{page[:160]}")
                elif action == "set":
                    found = session.query(app, text, role)
                    index = _match_index(found, text, role) or _match_index(session.query(app, text, ""), text, "")
                    if not index:
                        history.append(f"没有可勾选的「{text}」")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    if manual_stop(run_dir):
                        return leave(2)
                    note(run_dir, step="执行", basis=f"准备勾选「{text}」")
                    session.click(app, index)
                    acted = True
                    page = _page_lines(session.query(app, text, ""), text)
                    history.append(f"已勾选「{text}」。勾完看到：{page[:160]}")
                elif action == "scroll":
                    if can_hide():
                        return hidden(target)
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
                    page = _observe_page(session, app, fill_names, click_names, select_names, announce=arrived) or _page_lines(session.query(app, text, ""), text)
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
                        if can_hide():
                            return hidden(target)
                        if _window_unready(page, base_url):
                            history.append("页面还在加载。不要改地址。")
                        else:
                            history.append("不要改地址。")
                        note(run_dir, step="执行", basis=history[-1])
                        continue
                    index, found = _entry_index(session, app, text)
                    if not index:
                        page = _page_lines(found, text)
                        if page.startswith("页面上没有找到"):
                            page = _page_lines(session.query(app, text, ""), text)
                        history.append(f"没有填「{text}」。这一步看到的是：{page[:240]}")
                        note(run_dir, step="执行", basis=history[-1][:240])
                        continue
                    value = _fill_value(str(decision.get("value") or ""), typed)
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
        real = saved.get(case.get("check_id")) if isinstance(saved, dict) else None
        empty_data = isinstance(real, list) and not real
        typed = ",".join(str(item) for item in real) if isinstance(real, list) and real else ""
        return _think_and_act(
            run_dir, state, case, plan, session, model, phase, app, typed, empty_data=empty_data,
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
