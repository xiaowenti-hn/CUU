"""根据摘录材料生成方案，并校验产品 JSON。不复用演示的 count/notice 规则。"""

import base64
import json
import os
import re
import uuid
import urllib.error
import urllib.request

from qa_platform.runner.preflight import note, record
from qa_platform.store import read_state, review_digest, timestamp, write_json

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ROLES = {"button", "link", "textbox", "checkbox", "radio", "combobox"}
ACTIONS = {"click", "type", "select", "set"}
CLICK_FIELDS = {"value", "option", "checked"}
SOURCE_LINE = re.compile(r"^(.+):([0-9]+)$")

SYSTEM_PROMPT = """你是验收方案助手。读需求正文、截图、材料和 pages，只返回一个 JSON。顶层是 ui、cases、checks、open、source。

截图只说明界面长什么样。最外层页面写入 ui.pages[0].title_text，图上的页签按顺序写入 ui.surfaces，用来认出是哪一页。忽略浏览器书签和标签栏。
测哪些页签以 pages 为准。和截图页签落在同一条 route 上的行，每一行写一条用例，check 用这一行的 source 和 table。图上没有、但在这条 route 上的页签也要写。
图上有、pages 里没有同名 tab 的，放进 open，不编用例。别的 route 上的页签不写。
没有 pages 时，只给图上能在材料里对上表的页签写用例。没有截图时 surfaces 为空，start 为空。

每条用例给执行的人三句话：goal 是要完成的事，start 是「外层页面 - 页签」或只有外层页面，要看条数时 expect_text 写「结果条数」。步骤只写页面上看得见的字，value 写「使用库里已有数据」。
checks 只为事后查表。table 必须使用 live_tables 中的真实表名。pk 和 state_field 必须是该表 columns 中真实存在的列名；state_field 只能写一个列标识符，不能写中文、说明或路径。source 必须是 path:line，不能带提交哈希。页签能对应 pages 时，直接照抄该行的 source 和 table；否则 source 必须引用本次 files/materials 中能证明表或字段的源码行。提交哈希只写在顶层 source.commits。

正确格式示例："state_field":"status","source":"bfrs-app/src/main/java/com/greatonce/bfrs/controller/AlipayBillController.java:42"。示例行号仅说明格式，必须使用 pages 或源码中实际给出的行号。不要把类似 "ae5accb5 <path>" 的提交号前缀写进 checks.source。

照这个形状填，没有的数组写 []：
{"ui":{"seed_object_count":0,"pages":[{"title_text":"","path":""}],"surfaces":[]},"cases":[{"id":"","title":"","goal":"","start":"","expect_text":[],"check_id":"","pk_columns":[],"steps":[{"phase":"test","action":"type","role":"textbox","name":"","locator_status":"ready","value":""}]}],"checks":[{"id":"","kind":"table","table":"","pk":[],"state_field":"","source":""}],"open":[],"source":{"commits":[],"files":[]}}
"""


def acceptance_criteria(text: str) -> list[str]:
    """需求里有「验收标准」时，取出后面的条目。没有这一节就返回空。"""
    if not text or "验收标准" not in text:
        return []
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if "验收标准" in line:
            start = index + 1
            inline = line.split("验收标准", 1)[1].strip(" ：:　")
            if inline:
                return [inline]
            break
    if start is None:
        return []
    items = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if items:
                break
            continue
        if re.match(r"^(业务场景|变更点|评论|描述|附件|产品方案)\s*[:：]?$", stripped):
            break
        cleaned = re.sub(r"^[\d]+[.、)]\s*", "", stripped)
        cleaned = re.sub(r"^[-*]\s*", "", cleaned).strip()
        if cleaned and cleaned != "验收标准":
            items.append(cleaned)
        if len(items) >= 12:
            break
    return items


def case_goal(case: dict) -> str:
    """页面上怎样算通过。旧方案没有 goal 时，用标题顶上。"""
    goal = case.get("goal")
    if isinstance(goal, str) and goal.strip():
        return goal.strip()
    title = case.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "按下面的做法在当前页面上做完，并看到通过条件。"


def case_start(case: dict, plan: dict | None = None) -> str:
    """从哪一页开始。没有写起点时，用方案里已经知道的页面名。"""
    start = case.get("start")
    if isinstance(start, str) and start.strip():
        return start.strip()
    ui = plan.get("ui") if isinstance(plan, dict) else None
    pages = ui.get("pages") if isinstance(ui, dict) else None
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            title = str(page.get("title_text") or page.get("id") or "").strip()
            if title:
                return f"从「{title}」这一页开始。"
    return "从当前已经打开的页面开始。"


def criteria_gaps(doc: dict, criteria: list[str]) -> list[str]:
    blob = json.dumps(doc.get("cases"), ensure_ascii=False)
    gaps = []
    for item in criteria:
        needle = item if len(item) <= 12 else item[:8]
        if needle not in blob:
            gaps.append(f"验收标准未写入用例：{item}")
    return gaps


def _halt(run_dir, basis: str) -> int:
    record(run_dir, exit_code=2, event_status="阻塞", basis=basis, step="方案")
    return 2


_EXAMPLE_RESULT = re.compile(r"^共\s*0\s*条$")
_EXAMPLE_WORDS = {"查询完成"}


def _example_expect(item: str, requirement: str) -> bool:
    """截图或占位写出来的结果。需求正文里原样有这句时，才当作真的期望。"""
    text = item.strip()
    if text in requirement:
        return False
    return text in _EXAMPLE_WORDS or _EXAMPLE_RESULT.fullmatch(text) is not None


_GOAL_BIAS = ("只剩", "仅剩", "条数等于", "等于输入", "和输入一样", "一样多")


def _plain_label(text: str) -> str:
    """去掉首尾空白和书写用的引号。页签名中间的符号保留。"""
    return text.strip().replace("「", "").replace("」", "").strip()


def _start_parts(start: str, title: str = "", surfaces: list[str] | None = None) -> list[str]:
    """起点是「外层页面 + 分隔符 + 页签名」两段整句。页签名里的横线不算分隔符。"""
    text = _plain_label(start)
    names = [item.strip() for item in (surfaces or []) if isinstance(item, str) and item.strip()]
    if names:
        found: list[tuple[str, str]] = []
        wanted = _plain_label(title) if title else ""
        for name in names:
            plain_name = _plain_label(name)
            if not plain_name:
                continue
            match = re.fullmatch(rf"(.+)\s*[-—－]\s*{re.escape(plain_name)}", text)
            if match is None:
                continue
            page = match.group(1).strip()
            if not page:
                continue
            if wanted and page != wanted:
                continue
            found.append((title or page, name))
        if not found:
            return []
        found.sort(key=lambda item: len(item[1]), reverse=True)
        return [found[0][0], found[0][1]]
    if title and text == _plain_label(title):
        return [title]
    return []


def _matched_tab(start: str, candidates: list[str]) -> str:
    """起点末尾整段对上的最长页签名。页签名中间的横线留在页签名里。"""
    text = _plain_label(start)
    found: list[str] = []
    for name in candidates:
        plain = _plain_label(name)
        if not plain:
            continue
        if text == plain or re.fullmatch(rf"(.+)\s*[-—－]\s*{re.escape(plain)}", text):
            found.append(name)
    if not found:
        return ""
    found.sort(key=lambda item: len(_plain_label(item)), reverse=True)
    return found[0]


def _fill_plan_defaults(doc) -> None:
    """主键没写时用检查的主键。步骤有名字却没写状态时视为 ready。"""
    if not isinstance(doc, dict):
        return
    checks = doc.get("checks") if isinstance(doc.get("checks"), list) else []
    pk_by_id: dict[str, list[str]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        check_id = check.get("id")
        pk = check.get("pk")
        if isinstance(check_id, str) and isinstance(pk, list) and all(isinstance(item, str) and item for item in pk) and pk:
            pk_by_id[check_id] = pk
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return
    for case in cases:
        if not isinstance(case, dict):
            continue
        columns = case.get("pk_columns")
        if columns is None or columns == []:
            pk = pk_by_id.get(case.get("check_id"))
            if pk:
                case["pk_columns"] = list(pk)
        steps = case.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = step.get("name")
            if isinstance(name, str) and name.strip() and step.get("locator_status") in (None, ""):
                step["locator_status"] = "ready"


def _fill_check_kind(checks) -> None:
    """库检查只有 table 一种合法值。没写时补上，写错的留给校验打回。"""
    if not isinstance(checks, list):
        return
    for check in checks:
        if not isinstance(check, dict):
            continue
        kind = check.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            check["kind"] = "table"


def _page_title(ui) -> str:
    pages = ui.get("pages") if isinstance(ui, dict) and isinstance(ui.get("pages"), list) else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        title = str(page.get("title_text") or "").strip()
        if title:
            return title
    return ""


def _scope_tabs(page_index, surfaces: list[str]) -> dict[str, dict]:
    """截图页签对上的那几条 route，加上这些 route 上的全部索引页签。"""
    rows = _index_rows(page_index)
    routes: list[str] = []
    for name in surfaces:
        row = rows.get(name)
        if not row:
            continue
        route = str(row.get("route") or "").strip()
        if route and route not in routes:
            routes.append(route)
    if not routes:
        return {name: rows[name] for name in surfaces if name in rows}
    return {
        tab: row
        for tab, row in rows.items()
        if str(row.get("route") or "").strip() in routes
    }


def _surface_errors(doc, ui, cases, page_index=None) -> list[str]:
    """有需求截图时，起点先写外层页面，再写页签。页签名来自这一页的代码或截图。"""
    errors = []
    title = _page_title(ui)
    if not title:
        errors.append("有需求截图时，ui.pages 要写一条，title_text 是截图最外层正在打开的页面，不要写成里面的页签。")
    surfaces = ui.get("surfaces") if isinstance(ui, dict) else None
    if not isinstance(surfaces, list) or any(not isinstance(item, str) or not item.strip() for item in surfaces):
        errors.append("有需求截图时，ui.surfaces 要列出这一页上的页签。没有页签就写空数组。")
        return errors
    names = [item.strip() for item in surfaces]
    if len(names) != len(set(names)):
        errors.append("ui.surfaces 里的页签名不要重复。")
    opens = [
        str(item.get("question") or "")
        for item in (doc.get("open") or [])
        if isinstance(item, dict)
    ]
    open_text = "\n".join(opens)
    scope = _scope_tabs(page_index, names)
    indexed = list(_index_rows(page_index))
    known = list(dict.fromkeys([*names, *scope, *indexed]))
    used: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        label = case.get("id") if isinstance(case.get("id"), str) and case.get("id") else "未命名"
        start = case.get("start") if isinstance(case.get("start"), str) else ""
        parts = _start_parts(start, title, known)
        if names or scope:
            if len(parts) == 2 and (parts[1] in names or parts[1] in scope):
                used.append(parts[1])
            elif len(parts) == 2 and parts[1] in indexed:
                continue
            else:
                shown = title or "外层页面"
                errors.append(
                    f"用例「{label}」的 start 要写成「{shown} - 页签名」。"
                    "页签名整段照抄这一页代码或截图里的名字。不要只写页签。"
                )
        elif parts != [title]:
            errors.append(f"用例「{label}」的 start 只写外层页面「{title}」。截图上没有页签。")
    if len(used) != len(set(used)):
        errors.append("同一个页签不要写两条用例。")
    for name in names:
        if name in used or name in open_text or name in scope:
            continue
        errors.append(f"页签「{name}」代码里没有同名页，放进 open，不要编用例。")
    return errors


def _same_table(written: str, wanted: str) -> bool:
    if not written or not wanted:
        return False
    if written == wanted:
        return True
    return wanted.endswith("_" + written) or written.endswith("_" + wanted)


def _mentions_tab(tab: str, questions: list[str]) -> bool:
    if not tab:
        return False
    pattern = re.compile(rf"(?<![\w\u4e00-\u9fff]){re.escape(tab)}(?![\w\u4e00-\u9fff])")
    return any(pattern.search(text) for text in questions)


def _index_rows(page_index) -> dict[str, dict]:
    if not isinstance(page_index, list) or not page_index:
        return {}
    grouped: dict[str, list[dict]] = {}
    for row in page_index:
        if not isinstance(row, dict):
            continue
        tab = str(row.get("tab") or "").strip()
        source = str(row.get("source") or "")
        path = source.split(":", 1)[0].strip()
        table = str(row.get("table") or "").strip()
        if not tab or not path or not table:
            continue
        grouped.setdefault(tab, []).append(row)
    clean = {}
    for tab, rows in grouped.items():
        paths = {str(row.get("source") or "").split(":", 1)[0] for row in rows}
        tables = {str(row.get("table") or "") for row in rows}
        if len(paths) == 1 and len(tables) == 1:
            clean[tab] = rows[0]
    return clean


def _index_paths(page_index) -> set[str]:
    found = set()
    if not isinstance(page_index, list):
        return found
    for row in page_index:
        if not isinstance(row, dict):
            continue
        path = str(row.get("source") or "").split(":", 1)[0].strip()
        if path:
            found.add(path)
    return found


def _canonicalize_indexed_checks(doc, page_index) -> None:
    """页签索引是控制器和表的权威绑定；据此修正模型拼错的来源路径。"""
    if not isinstance(doc, dict) or not isinstance(page_index, list):
        return
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return
    ui = doc.get("ui") if isinstance(doc.get("ui"), dict) else {}
    surfaces = ui.get("surfaces") if isinstance(ui.get("surfaces"), list) else []
    names = [item.strip() for item in surfaces if isinstance(item, str) and item.strip()]
    rows = _scope_tabs(page_index, names)
    if not rows:
        return
    title = _page_title(ui)
    known = list(dict.fromkeys([*names, *_index_rows(page_index)]))
    checks = doc.get("checks") if isinstance(doc.get("checks"), list) else []
    check_by_id = {
        check.get("id"): check for check in checks
        if isinstance(check, dict) and isinstance(check.get("id"), str)
    }
    targets: dict[str, set[tuple[str, str]]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        parts = _start_parts(str(case.get("start") or ""), title, known)
        if len(parts) != 2 or parts[1] not in rows:
            continue
        check_id = case.get("check_id")
        if not isinstance(check_id, str) or check_id not in check_by_id:
            continue
        row = rows[parts[1]]
        targets.setdefault(check_id, set()).add((str(row.get("source") or ""), str(row.get("table") or "")))
    for check_id, bindings in targets.items():
        if len(bindings) != 1:
            continue
        source, table = next(iter(bindings))
        if source and table:
            check_by_id[check_id]["source"] = source
            check_by_id[check_id]["table"] = table


def _binding_errors(doc, ui, cases, check_by_id, page_index) -> list[str]:
    """这一页代码里的页签必须绑到自己的控制器。别的页面上的页签不写用例。"""
    rows = _index_rows(page_index)
    if not rows:
        return []
    errors = []
    surfaces = ui.get("surfaces") if isinstance(ui, dict) else None
    names = [item.strip() for item in surfaces if isinstance(item, str) and item.strip()] if isinstance(surfaces, list) else []
    scope = _scope_tabs(page_index, names)
    questions = [
        str(item.get("question") or "")
        for item in (doc.get("open") or [])
        if isinstance(item, dict)
    ]
    known = list(dict.fromkeys([*names, *rows]))
    used: set[str] = set()
    title = _page_title(ui)
    for case in cases:
        if not isinstance(case, dict):
            continue
        label = case.get("id") if isinstance(case.get("id"), str) and case.get("id") else "未命名"
        parts = _start_parts(
            case.get("start") if isinstance(case.get("start"), str) else "",
            title,
            known,
        )
        if len(parts) != 2:
            continue
        tab = parts[1]
        if tab in scope:
            used.add(tab)
            row = scope[tab]
            want_path = str(row.get("source") or "").split(":", 1)[0]
            want_name = str(row.get("class_name") or want_path)
            want_table = str(row.get("table") or "")
            check = check_by_id.get(case.get("check_id"))
            if isinstance(check, dict):
                got_path = str(check.get("source") or "").split(":", 1)[0]
                got_table = str(check.get("table") or "")
                if got_path != want_path:
                    errors.append(f"用例「{label}」的页签「{tab}」对应 {want_name}，检查写成了 {got_path or '空'}。")
                if not _same_table(got_table, want_table):
                    errors.append(f"用例「{label}」的页签「{tab}」对应表 {want_table}，检查写成了 {got_table or '空'}。")
            if _mentions_tab(tab, questions):
                errors.append(f"页签「{tab}」已经有用例，不要再写进 open。")
        elif tab in names:
            errors.append(f"页签「{tab}」对不上页签索引。不要编用例，在 open 里用这个页签名提问。")
        elif tab in rows:
            errors.append(
                f"用例「{label}」的页签「{tab}」不在这一页的代码里。"
                "删掉这条用例和只被它使用的库检查。"
            )
    for name, row in scope.items():
        if name in used:
            continue
        table = str(row.get("table") or "")
        errors.append(f"页签「{name}」在这一页的代码里有表 {table}，要写成用例，不要只写进 open。")
    return errors


def validate_plan(doc, *, allowed_commits=None, material_files=None,
                  allowed_databases=None, requirement: str = "", has_images: bool = False,
                  page_index=None, table_columns=None) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["方案必须是 JSON 对象"]
    _fill_plan_defaults(doc)
    index_paths = _index_paths(page_index)
    for key in ("ui", "cases", "checks", "open", "source"):
        if key not in doc:
            errors.append(f"缺少 {key}")
    if "ui" not in doc or "checks" not in doc:
        return errors
    ui = doc.get("ui")
    if not isinstance(ui, dict):
        errors.append("ui 必须是对象")
        count = None
    else:
        count = ui.get("seed_object_count", None)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            errors.append("ui.seed_object_count 必须是大于等于 0 的整数")
    cases = doc.get("cases")
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks 至少一条")
        checks = []
    if not isinstance(cases, list) or not cases:
        errors.append("cases 至少一条")
        cases = []
    if has_images:
        errors.extend(_surface_errors(
            doc, ui if isinstance(ui, dict) else {}, cases, page_index,
        ))
    _fill_check_kind(checks)
    case_ids = [item.get("id") for item in cases if isinstance(item, dict)]
    if case_ids and set(case_ids) <= {"count", "notice"}:
        errors.append("演示方案不能只有 count 和 notice")
    if len(case_ids) != len(set(case_ids)):
        errors.append("cases.id 必须唯一")
    check_by_id = {}
    seen_checks = set()
    for check in checks:
        if not isinstance(check, dict):
            errors.append("check 必须是对象")
            continue
        check_id = check.get("id")
        if check.get("kind") != "table":
            label = check_id if isinstance(check_id, str) and check_id else "未命名"
            kind = check.get("kind")
            shown = kind if isinstance(kind, str) and kind else "空"
            errors.append(
                f"库检查「{label}」写成了 {shown}，checks.kind 只能是 table。"
                "这一项必须是查一张数据库表"
            )
        if not isinstance(check_id, str) or not check_id or check_id in seen_checks:
            errors.append("checks.id 必须唯一且非空")
        else:
            seen_checks.add(check_id)
            check_by_id[check_id] = check
        if not isinstance(check.get("table"), str) or IDENT.fullmatch(check.get("table", "")) is None:
            errors.append("checks.table 必须是标识符")
        pk = check.get("pk")
        if (not isinstance(pk, list) or not pk
                or any(not isinstance(item, str) or IDENT.fullmatch(item) is None for item in pk)):
            errors.append("checks.pk 必须是非空列名数组")
        if not isinstance(check.get("state_field"), str) or IDENT.fullmatch(str(check.get("state_field"))) is None:
            errors.append("checks.state_field 必须是标识符")
        table_name = check.get("table") if isinstance(check.get("table"), str) else ""
        known_columns = (table_columns or {}).get(table_name)
        if isinstance(known_columns, set):
            known_columns = list(known_columns)
        if isinstance(known_columns, (list, tuple)) and known_columns:
            if isinstance(pk, list) and any(column not in known_columns for column in pk):
                errors.append(f"checks「{check_id or '未命名'}」的 pk 必须来自表 {check.get('table')} 的真实列名")
            state_field = check.get("state_field")
            if isinstance(state_field, str) and IDENT.fullmatch(state_field) and state_field not in known_columns:
                errors.append(f"checks「{check_id or '未命名'}」的 state_field 不在表 {check.get('table')} 的真实列名中")
        source = check.get("source")
        match = SOURCE_LINE.fullmatch(source) if isinstance(source, str) else None
        if not match:
            errors.append("checks.source 必须是 path:line")
        elif material_files is not None and match.group(1) not in material_files and match.group(1) not in index_paths:
            errors.append("checks.source 的路径不在本次材料里")
        if "database" in check and check.get("database") not in (allowed_databases or []):
            errors.append("checks.database 不在允许的库名单里")
    for case in cases:
        if not isinstance(case, dict):
            errors.append("case 必须是对象")
            continue
        case_label = case.get("id") if isinstance(case.get("id"), str) and case.get("id") else "未命名"
        goal = case.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            errors.append(f"用例「{case_label}」要有 goal，用一句话写清页面上怎样算通过")
        if not isinstance(case.get("start"), str):
            errors.append(f"用例「{case_label}」要有 start。看不出从哪一页开始就写空字符串")
        expect = case.get("expect_text")
        expect_ok = isinstance(expect, str) and bool(expect.strip())
        if isinstance(expect, list):
            expect_ok = any(isinstance(item, str) and item.strip() for item in expect)
        if not expect_ok:
            errors.append(f"用例「{case_label}」要有 expect_text，写出做完后页面上应出现的文字")
        else:
            names = set()
            for step in case.get("steps") or []:
                if not isinstance(step, dict) or step.get("action") not in {"click", "type", "select", "set"}:
                    continue
                name = step.get("name")
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
            items = [expect.strip()] if isinstance(expect, str) else [
                item.strip() for item in expect if isinstance(item, str) and item.strip()
            ]
            overlap = [item for item in items if item in names]
            if overlap:
                errors.append(
                    f"用例「{case_label}」的 expect_text 不要写成要操作的控件名：{'、'.join(overlap)}"
                )
            examples = [item for item in items if _example_expect(item, requirement or "")]
            if examples:
                errors.append(
                    f"用例「{case_label}」的 expect_text 写成了示例结果：{'、'.join(examples)}。"
                    "空表上的 0 和「查询完成」不是查询结果。要看条数就写「结果条数」，执行时再从页面读取。"
                )
            if isinstance(goal, str) and "结果条数" in items and any(word in goal for word in _GOAL_BIAS):
                errors.append(
                    f"用例「{case_label}」的目标把结果写成了只剩输入或条数相等。"
                    "查询结果是页面上的条数，可以和输入条数不同。"
                )
        check = check_by_id.get(case.get("check_id"))
        if check is None:
            errors.append("case.check_id 必须指向某个 check")
            pk = []
        else:
            pk = check.get("pk") if isinstance(check.get("pk"), list) else []
        columns = case.get("pk_columns")
        if (not isinstance(columns, list) or not columns
                or any(not isinstance(item, str) or item not in pk for item in columns)):
            errors.append(
                f"用例「{case_label}」的 pk_columns 必须是对应 check 的 pk 的非空子集。"
                f"主键是 {pk or '空'}，现在写的是 {columns!r}"
            )
        steps = case.get("steps")
        if not isinstance(steps, list):
            errors.append("case.steps 必须是数组")
            continue
        for step in steps:
            if not isinstance(step, dict):
                errors.append("步骤必须是对象")
                continue
            for field in ("role", "name", "locator_status", "action", "phase"):
                if field not in step:
                    errors.append(f"步骤缺少 {field}")
            if step.get("role") not in ROLES:
                errors.append("步骤 role 不在允许的控件类型里")
            locator = step.get("locator_status")
            if locator not in {"ready", "pending"}:
                errors.append("locator_status 只能是 ready 或 pending")
            if locator == "ready" and not str(step.get("name") or "").strip():
                errors.append("ready 步骤的 name 不能为空")
            action = step.get("action")
            if action not in ACTIONS:
                errors.append("步骤 action 只能是 click、type、select 或 set")
            if action == "type" and not isinstance(step.get("value"), str):
                errors.append("type 必须有字符串 value")
            if action == "select" and not isinstance(step.get("option"), str):
                errors.append("select 必须有 option")
            if action == "set" and not isinstance(step.get("checked"), bool):
                errors.append("set 必须有布尔 checked")
            if action == "click" and any(field in step for field in CLICK_FIELDS):
                errors.append("click 不能带 value、option 或 checked")
            if "effect" in step and step.get("effect") not in {"read", "commit"}:
                errors.append("effect 只能是 read 或 commit")
            if step.get("phase") not in {"seed", "test"}:
                errors.append("phase 只能是 seed 或 test")
    opens = doc.get("open")
    if not isinstance(opens, list):
        errors.append("open 必须是数组")
    else:
        for item in opens:
            if not isinstance(item, dict) or item.get("decision") != "":
                errors.append("open.decision 必须是空字符串")
    source = doc.get("source")
    commits = source.get("commits") if isinstance(source, dict) else None
    if not isinstance(commits, list) or any(not isinstance(item, str) for item in commits):
        errors.append("source.commits 必须是字符串数组")
        commits = []
    if allowed_commits is not None and any(item not in allowed_commits for item in commits):
        errors.append("source.commits 多出本次提交")
    errors.extend(_binding_errors(
        doc, ui if isinstance(ui, dict) else {}, cases, check_by_id, page_index,
    ))
    return errors


def deepseek_complete(key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def _requirement_text(run_dir, state: dict) -> tuple[str, str]:
    requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else {}
    serial = str(requirement.get("id") or "")
    body_file = requirement.get("body_file") or "requirement.txt"
    path = run_dir / str(body_file)
    body = ""
    if path.is_file() and not path.is_symlink():
        body = path.read_text(encoding="utf-8")
    return serial, body


def _validation_basis(errors: list[str]) -> str:
    unique = list(dict.fromkeys(errors))
    shown = unique[:6]
    text = "；".join(shown)
    if len(unique) > 6:
        text += f"；还有 {len(unique) - 6} 处"
    return "方案未通过校验：" + text


def _parse_plan(result: dict) -> dict:
    content = result["choices"][0]["message"]["content"]
    doc = json.loads(content)
    if not isinstance(doc, dict):
        raise TypeError("方案必须是对象")
    return doc


def _requirement_images(run_dir) -> list[str]:
    folder = run_dir / "images"
    if not folder.is_dir() or folder.is_symlink():
        return []
    found = []
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or not path.name.startswith("requirement"):
            continue
        suffix = path.suffix.lower()
        if suffix == ".png":
            mime = "image/png"
        elif suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        else:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) < 32 or len(data) > 8_000_000:
            continue
        if mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        if mime == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
            continue
        encoded = base64.b64encode(data).decode("ascii")
        found.append(f"data:{mime};base64,{encoded}")
        if len(found) == 4:
            break
    return found


def _public_page(row: dict) -> dict:
    return {
        "tab": row.get("tab") or "",
        "route": row.get("route") or "",
        "mapping": row.get("mapping") or "",
        "class_name": row.get("class_name") or "",
        "table": row.get("table") or "",
        "source": row.get("source") or "",
        "filters": [item for item in (row.get("filters") or []) if isinstance(item, str)],
    }


def propose(run_dir, *, key: str, shas: list[str], files: list[str], materials: str,
            complete=None, live_tables=None, menus=None, pages=None) -> int:
    state = read_state(run_dir)
    serial, body = _requirement_text(run_dir, state)
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    caller = complete or deepseek_complete
    message = {
        "requirement_id": serial,
        "requirement": body,
        "commits": shas,
        "files": files,
        "materials": materials,
    }
    if live_tables:
        message["live_tables"] = live_tables
    if menus:
        message["menus"] = menus
    if pages:
        message["pages"] = [_public_page(row) for row in pages if isinstance(row, dict)]
    criteria = acceptance_criteria(body)
    if criteria:
        message["acceptance_criteria"] = criteria
        note(run_dir, step="方案", basis="需求里有验收标准，方案按这些原文写")
    payload_text = json.dumps(message, ensure_ascii=False)
    images = _requirement_images(run_dir)
    if images:
        user_content = [
            {
                "type": "text",
                "text": payload_text + "\n请阅读需求里的图片。正文插图、箭头和界面文字都要读出来。",
            },
            *[
                {"type": "image_url", "image_url": {"url": image, "detail": "high"}}
                for image in images
            ],
        ]
    else:
        user_content = payload_text

    def ask(extra: str | None = None) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if extra:
            messages.append({"role": "user", "content": extra})
        return caller(key, {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": 8000,
        })

    def checked(result: dict) -> tuple[dict | None, list[str] | None]:
        try:
            doc = _parse_plan(result)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None, None
        _canonicalize_indexed_checks(doc, pages)
        table_columns = {
            str(table.get("table")): [
                str(column.get("name")) for column in table.get("columns", [])
                if isinstance(column, dict) and column.get("name")
            ]
            for table in (live_tables or [])
            if isinstance(table, dict) and table.get("table")
            and isinstance(table.get("columns"), list)
        }
        errors = validate_plan(
            doc,
            allowed_commits=set(shas),
            material_files=set(files),
            allowed_databases=list(state.get("allowed_databases") or []),
            requirement=body,
            has_images=bool(images),
            page_index=pages,
            table_columns=table_columns,
        )
        errors.extend(criteria_gaps(doc, criteria))
        return doc, errors

    try:
        result = ask()
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return _halt(run_dir, "DeepSeek 请求失败")
    doc, errors = checked(result)
    if doc is None:
        return _halt(run_dir, "DeepSeek 没有返回方案 JSON")
    if errors:
        note(run_dir, step="方案", basis="方案有问题，正在按校验结果重写")
        try:
            result = ask(
                "上次方案未通过校验。请返回改正后的完整 JSON，只改这些问题：\n"
                + "\n".join(dict.fromkeys(errors))
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return _halt(run_dir, "DeepSeek 请求失败")
        doc, errors = checked(result)
        if doc is None:
            return _halt(run_dir, "DeepSeek 没有返回方案 JSON")
    if errors:
        return _halt(run_dir, _validation_basis(errors))
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        blob = json.dumps(case, ensure_ascii=False)
        if criteria and any((item if len(item) <= 12 else item[:8]) in blob for item in criteria):
            case["origin"] = "验收标准原文"
        elif criteria:
            case["origin"] = "验收标准原文"
        else:
            case["origin"] = "需求没有验收标准，步骤来自代码"
        for step in case.get("steps") or []:
            if isinstance(step, dict) and "effect" not in step:
                step["effect"] = "read"
    from qa_platform.runner.execute import implementation_brief
    notes = []
    repo = str(state.get("repo_path") or "")
    for check in doc.get("checks") or []:
        if not isinstance(check, dict):
            continue
        brief = implementation_brief(repo, str(check.get("source") or ""))
        if brief:
            notes.append({"check_id": check.get("id"), "text": brief})
    ui = doc.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        doc["ui"] = ui
    if notes:
        ui["code_notes"] = notes
    ui["step_source"] = "验收标准原文" if criteria else "需求没有验收标准，步骤来自代码"
    allowed = set(shas)
    commits = [item for item in doc.get("source", {}).get("commits", []) if item in allowed]
    doc["source"] = {
        "model": result.get("model") or model,
        "request_id": str(result.get("id") or ""),
        "requirement_id": serial,
        "commits": commits,
        "files": list(files),
    }
    from qa_platform.runner.menu_page import attach_page
    from qa_platform.runner.page_index import fill_route

    chosen = attach_page(doc, body, menus or [])
    if chosen:
        note(run_dir, step="方案", basis=f"方案没有页面路径，已按菜单「{chosen['name']}」补上 {chosen['path']}")
    else:
        route = fill_route(doc, pages or [])
        if route:
            note(run_dir, step="方案", basis=f"方案没有页面路径，已按前端路由补上 {route}")
    write_json(run_dir / "plan.json", doc)
    fresh = read_state(run_dir)
    fresh["stage"] = "plan"
    fresh["exit_code"] = 4
    fresh["review"] = {
        "id": uuid.uuid4().hex,
        "digest": review_digest("plan", doc),
        "kind": "plan",
        "step": "方案",
        "title": "确认界面和用例",
        "payload": doc,
        "requested_at": timestamp(),
    }
    write_json(run_dir / "state.json", fresh)
    record(run_dir, exit_code=4, event_status="待确认", basis="方案已通过校验，等待确认", step="方案")
    return 4
