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

SYSTEM_PROMPT = """你是验收方案助手。只根据给出的需求正文、提交号和摘录材料返回一个 JSON 对象。
顶层字段只能围绕 ui、cases、checks、open、source。
ui.seed_object_count 是大于等于 0 的整数。
如果带了需求截图，ui.pages 写一条。title_text 是截图最外层正在打开的那一页的名字，不要写成里面的页签。path 先留空，不要编。
ui.surfaces 是字符串数组，按截图顺序列出这一页上的页签。没有页签就写空数组。
每条用例的 start：有页签时写成「外层页面 - 页签名」，没有页签时只写外层页面。不要只写页签。
图上有的页签，材料里对得上表的各写一条用例，页签名要在 ui.surfaces 里。对不上表的页签不要编用例，在 open 里用这个页签名提问。图上没有的页签，不要因为代码里有控制器就加。
没有截图时，ui.surfaces 写空数组。start 看不出页面就写空字符串，不要猜。
如果给出了 menus，页面名能对上唯一一条 menu_name 时，path 原样抄它的 route。对不上或对上多条就留空，不要猜。
cases 至少一条，id 在文件内唯一，不能把方案收成 count 和 notice。
每条用例要有 check_id 和 pk_columns。pk_columns 是对应 check 的 pk 的非空子集。
每个步骤都要有 role、name、locator_status、action、phase。
locator_status 只能是 ready 或 pending。ready 时 name 不能为空。pending 表示页面还没打开、名字还没对上，可以留下。
role 只能是 button、link、textbox、checkbox、radio、combobox。
action 为 type 时要有字符串 value；为 select 时要有 option；为 set 时要有布尔 checked；为 click 时不能带 value、option、checked。
effect 只能是 read 或 commit，不写则视为 read。phase 只能是 seed 或 test。
每条用例还要有 goal、start、expect_text。这三样是交给操作页面的人看的说明，不是定位脚本。
goal 是一句话。查询类写成：把准备的输入填进截图箭头指的条件，做查询，读页面上的结果条数。不要写列表只剩这些输入，也不要写条数等于输入条数。
start 见上面。没有截图、也看不出页面时写空字符串，不要猜路径。
expect_text 是字符串数组。每一项是做完后页面上新出现的短文字。不要把这一步要点、要填、要选的控件名写进去。页签名如果就是要点的那个控件，也不要写进去。
需求截图里的空表、箭头和数字只说明界面长什么样。空表上的 0 是示例数据，不是这次查询的结果。不要把「共 0 条」或「查询完成」写进 expect_text，除非需求正文里原样有这句。
库里的行是测试输入，方案阶段还没取出，条数也还不知道。不要把输入条数写成期望。
如果做完后要看的就是查询出来的条数，expect_text 写「结果条数」。不要写具体数字。执行时读取页面上名称正好是「共 N 条」的那一行，N 是多少都算结果，也可以和输入条数不同。
步骤的 name 必须是人在页面上能看见的控件文字。不要写代码字段名，也不要在名字后面加「输入框」或「列表字段」。
步骤只是参考顺序：填写、选择、勾选、点击。打开页面后对不上，执行的人会重新看眼前的控件再改。不要为此编 locator、CSS 或测试用例编号。
checks 至少一条，kind 只能是 table，不能是 api 或 state。不要输出 SQL。
每条 check 要有唯一 id、table、非空 pk、state_field、source。source 写成材料里的 path:line。
open 可以是空数组。要人拍板时写 id、question，needs_decision 为 true，decision 必须是空字符串，不要自己做决定。
state_field 必须是会变化的列，不能写成主键本身。
checks 只保留用例 check_id 用到的表。checks 用来事后核对数据库，不要把它写成页面上的点击步骤。
如果给出了 live_tables，checks.table 只能用其中的真实表名。代码里有类、库里没有的表不要写进 checks。
value 不要编造库里的单号或业务编号，写成「使用库里已有数据」。真正的值由后续从数据库取出，最多 500 条。
如果给出了 acceptance_criteria，每条标准原样成为一条用例的标题或步骤，不要改写，也不要另加标准里没有的平台或场景。checks 仍只用来核对库里的表。
source.commits 只能来自给出的提交，不要新编 SHA。source.files 只能来自给出的文件。
如果用户消息带了需求页截图，截图里的业务图片、箭头和界面文字都要读出来，浏览器书签和标签栏忽略。
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


_START_SPLIT = re.compile(r"\s*[-—－]\s*")
_GOAL_BIAS = ("只剩", "仅剩", "条数等于", "等于输入", "和输入一样", "一样多")


def _start_parts(start: str) -> list[str]:
    return [part.strip("「」 ") for part in _START_SPLIT.split(start.strip()) if part.strip("「」 ")]


def _page_title(ui) -> str:
    pages = ui.get("pages") if isinstance(ui, dict) and isinstance(ui.get("pages"), list) else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        title = str(page.get("title_text") or "").strip()
        if title:
            return title
    return ""


def _surface_errors(doc, ui, cases) -> list[str]:
    """有需求截图时，起点先写外层页面，再写页签。页签和用例或待决定一一对应。"""
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
    used: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        label = case.get("id") if isinstance(case.get("id"), str) and case.get("id") else "未命名"
        start = case.get("start") if isinstance(case.get("start"), str) else ""
        parts = _start_parts(start)
        if names:
            if len(parts) != 2 or (title and parts[0] != title) or parts[1] not in names:
                shown = title or "外层页面"
                errors.append(
                    f"用例「{label}」的 start 要写成「{shown} - 页签名」。"
                    "页签名必须在 ui.surfaces 里。不要只写页签。"
                )
            else:
                used.append(parts[1])
        elif parts != [title]:
            errors.append(f"用例「{label}」的 start 只写外层页面「{title}」。截图上没有页签。")
    if len(used) != len(set(used)):
        errors.append("同一个页签不要写两条用例。")
    for name in names:
        if name in used or name in open_text:
            continue
        errors.append(f"页签「{name}」没有用例。材料里对不上表时，在 open 里用这个页签名提问，不要编用例。")
    return errors


def validate_plan(doc, *, allowed_commits=None, material_files=None,
                  allowed_databases=None, requirement: str = "", has_images: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["方案必须是 JSON 对象"]
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
        errors.extend(_surface_errors(doc, ui if isinstance(ui, dict) else {}, cases))
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
        source = check.get("source")
        match = SOURCE_LINE.fullmatch(source) if isinstance(source, str) else None
        if not match:
            errors.append("checks.source 必须是 path:line")
        elif material_files is not None and match.group(1) not in material_files:
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


def propose(run_dir, *, key: str, shas: list[str], files: list[str], materials: str,
            complete=None, live_tables=None, menus=None) -> int:
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
        errors = validate_plan(
            doc,
            allowed_commits=set(shas),
            material_files=set(files),
            allowed_databases=list(state.get("allowed_databases") or []),
            requirement=body,
            has_images=bool(images),
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

    chosen = attach_page(doc, body, menus or [])
    if chosen:
        note(run_dir, step="方案", basis=f"方案没有页面路径，已按菜单「{chosen['name']}」补上 {chosen['path']}")
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
