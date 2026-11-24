"""封口与报告。渲染只读用例状态，不再改写通过、失败或阻塞。"""

import hashlib
import json
import re
import struct
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

from qa_platform.store import read_state, timestamp, write_json

TERMINAL = {"passed", "failed", "blocked", "skipped"}
RESULT = {"passed": "通过", "failed": "失败", "blocked": "阻塞", "skipped": "跳过"}
CONSOLE_NOTE = "可访问性文本未包含预期"
MISSING_LOG = "日志无相关记录"
OBSERVATION_MISS = "观察不符"
WEAK_EXPECT = "通过条件只写了要操作的控件"
NO_DATA = "这一条没有库里的数据"
CONTROL_ACTIONS = {"click", "type", "select", "set"}
DEMO_NAMES = {"db-before.json", "db-after-1.json", "db-after-2.json", "sql-result.json"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 5 位宽、7 行高。只覆盖报告里会画到的 ASCII；其余字符用码位画成稳定色块。
_FONT = {
    " ": [0, 0, 0, 0, 0, 0, 0],
    "-": [0, 0, 0, 31, 0, 0, 0],
    "0": [14, 17, 19, 21, 25, 17, 14],
    "1": [4, 12, 4, 4, 4, 4, 14],
    "A": [14, 17, 17, 31, 17, 17, 17],
    "D": [30, 17, 17, 17, 17, 17, 30],
    "E": [31, 16, 16, 30, 16, 16, 31],
    "I": [14, 4, 4, 4, 4, 4, 14],
    "L": [16, 16, 16, 16, 16, 16, 31],
    "T": [31, 4, 4, 4, 4, 4, 4],
    "a": [0, 0, 14, 1, 15, 17, 15],
    "d": [1, 1, 13, 19, 17, 17, 15],
    "e": [0, 0, 14, 17, 31, 16, 14],
    "n": [0, 0, 26, 21, 17, 17, 17],
    "o": [0, 0, 14, 17, 17, 17, 14],
}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _glyph(char: str) -> list[int]:
    if char in _FONT:
        return _FONT[char]
    code = ord(char)
    return [((code * (row + 3)) ^ (code >> row)) & 31 for row in range(7)]


def console_png(lines: list[str]) -> bytes:
    """把控制台相关行画成 PNG。不打开页面，也不请求任何 URL。"""
    shown = [line.replace("\t", "    ")[:200] for line in lines[:80]] or [" "]
    pad, cell_w, cell_h = 8, 6, 10
    width = min(1400, pad * 2 + max(len(line) for line in shown) * cell_w)
    height = pad * 2 + len(shown) * cell_h
    pixels = bytearray(b"\xff\xff\xff" * width * height)
    for row, line in enumerate(shown):
        for column, char in enumerate(line):
            origin_x = pad + column * cell_w
            origin_y = pad + row * cell_h
            if origin_x + 5 >= width:
                break
            for gy, bits in enumerate(_glyph(char)):
                for gx in range(5):
                    if bits & (1 << (4 - gx)):
                        index = ((origin_y + gy) * width + origin_x + gx) * 3
                        pixels[index:index + 3] = b"\x14\x26\x38"
    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    text = "\n".join(shown).encode("utf-8")
    itxt = b"Comment\x00\x00\x00\x00\x00" + text
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"iTXt", itxt) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def _read_json(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _cases(run_dir: Path) -> list[tuple[Path, dict]]:
    root = run_dir / "cases"
    if root.is_symlink() or not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        file = child / "case.json"
        data = _read_json(file)
        if data is not None:
            found.append((file, data))
    return found


def _baseline(run_dir: Path) -> dict:
    data = _read_json(run_dir / "baseline.json")
    return data or {}


def _case_id(case: dict) -> str:
    raw = str(case.get("case_id") or case.get("id") or "case")
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        return "case"
    return raw


def control_names(case: dict, phase: str = "test") -> set[str]:
    names = set()
    for step in case.get("steps") or []:
        if not isinstance(step, dict) or step.get("phase", "test") != phase:
            continue
        if step.get("action") not in CONTROL_ACTIONS:
            continue
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def proof_texts(case: dict, phase: str = "test") -> list[str]:
    """做完后要在页面上看到的字。要点、要填的控件名不算。"""
    names = control_names(case, phase)
    found: list[str] = []

    def take(value) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in names and value.strip() not in found:
            found.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                take(item)

    take(case.get("expect_text"))
    for step in case.get("steps") or []:
        if isinstance(step, dict) and step.get("phase", "test") == phase:
            take(step.get("expect_text"))
    return found


ABSENCE_MARKS = ("没有找到", "未找到", "没找到", "没有出现", "未出现")
_GENERIC_SURFACE = {"当前", "这个", "该", "本", "同一", "对应", "结果", "列表", "这里", "上面", "下面"}


def absence_line(line: str) -> bool:
    """这行是在说某个字没出现。行里带上原字，也不算已经看见。"""
    return any(mark in line for mark in ABSENCE_MARKS)


def outside_entry(text: str, value: str) -> bool:
    """值出现在非输入框的一行上。只写在输入框里，或写在「没有找到」里，都不算结果。"""
    if not value or value not in text:
        return False
    entry_words = ("textfield", "textarea", "searchfield", "textbox", "combobox")
    for line in text.splitlines():
        if value not in line or absence_line(line):
            continue
        if "可以填写" in line or "可填写" in line:
            continue
        low = line.lower()
        if any(word in low for word in entry_words):
            continue
        return True
    return False


def surface_name(case: dict) -> str:
    """这一条用来进入界面的名字。它只说明走到了哪，不能单独当作结果。"""
    stored = case.get("surface_name")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    match = re.search(r"在([^，。；\s]{2,16})页", str(case.get("goal") or ""))
    if not match:
        return ""
    name = match.group(1).strip("「」")
    if not name or name in _GENERIC_SURFACE or name in control_names(case):
        return ""
    return name


_COUNT_LABEL = re.compile(r"^共\s*(\d+)\s*条$")
_EXAMPLE_COUNT = re.compile(r"^共\s*0\s*条$")
RESULT_MARK = "结果条数"
EXAMPLE_WORD = "查询完成"
_ENTRY_WORDS = ("textfield", "textarea", "searchfield", "textbox", "combobox")


def page_result_count(seen: str) -> str:
    """产品页上名称正好是「共 N 条」的那一行。长标题里夹着这几个字不算。"""
    found = ""
    for line in (seen or "").splitlines():
        if absence_line(line):
            continue
        low = line.lower()
        if "可以填写" in line or "可填写" in line or any(word in low for word in _ENTRY_WORDS):
            continue
        for label in _count_labels(line):
            match = _COUNT_LABEL.fullmatch(label.strip())
            if match:
                found = f"共 {int(match.group(1))} 条"
    return found


def _count_labels(line: str) -> list[str]:
    quoted = re.findall(r"名称「([^」]*)」", line)
    if quoted:
        return quoted
    plain = re.search(r"(?:AX)?StaticText\s+(.+)$", line.strip(), flags=re.I)
    if plain:
        return [plain.group(1).strip()]
    stripped = line.strip()
    if _COUNT_LABEL.fullmatch(stripped):
        return [stripped]
    return []


def satisfied_by_count(item: str, proof: list[str]) -> bool:
    """这条期望由页面上的结果条数来满足。具体的「共 1 条」仍要原样出现。"""
    text = item.strip()
    if text == RESULT_MARK or _EXAMPLE_COUNT.fullmatch(text):
        return True
    companions = [other.strip() for other in proof]
    return text == EXAMPLE_WORD and any(_EXAMPLE_COUNT.fullmatch(other) for other in companions)


def expects_result_count(case: dict, phase: str = "test") -> bool:
    proof = [item for item in proof_texts(case, phase) if item != surface_name(case)]
    return any(satisfied_by_count(item, proof) for item in proof)


def filled_values(case: dict) -> list[str]:
    filled = case.get("filled_values")
    if not isinstance(filled, list):
        return []
    return [str(item).strip() for item in filled if str(item).strip()]


def prepared_values(case: dict, baseline: dict | None) -> list[str]:
    """这一条库里准备好的值。没有记录时返回空，不拿别的用例的数据顶上。"""
    saved = baseline.get("test_values") if isinstance(baseline, dict) else None
    real = saved.get(case.get("check_id")) if isinstance(saved, dict) else None
    if isinstance(real, list):
        return [str(item).strip() for item in real if str(item).strip()]
    return []


def data_is_empty(case: dict, baseline: dict | None) -> bool:
    if case.get("data_empty") is True:
        return True
    saved = baseline.get("test_values") if isinstance(baseline, dict) else None
    if not isinstance(saved, dict) or case.get("check_id") not in saved:
        return False
    real = saved.get(case.get("check_id"))
    return isinstance(real, list) and not prepared_values(case, baseline)


def judge_final(case: dict, seen: str, prepared: list[str] | None, *, data_empty: bool = False) -> tuple[str, str]:
    """返回 passed、failed 或 wait，以及要写进进度的原因。wait 表示先不要结束。"""
    filled = filled_values(case)
    if data_empty and filled:
        return "failed", NO_DATA
    if prepared:
        own = [item for item in filled if item in set(prepared)]
        if filled and not own:
            return "failed", NO_DATA
    else:
        own = filled
    proof = [item for item in proof_texts(case) if item != surface_name(case)]
    by_count = [item for item in proof if satisfied_by_count(item, proof)]
    literal = [item for item in proof if item not in by_count]
    if literal:
        missing = [item for item in literal if not outside_entry(seen, item)]
        if missing:
            return "wait", "页面上还没有出现：" + "、".join(missing)
    count = page_result_count(seen)
    if by_count:
        if not count:
            return "wait", "页面上还没有出现结果条数"
        if len(own) > 1:
            return "passed", ""
    missing_values = [item for item in own if not outside_entry(seen, item)]
    if missing_values:
        return "wait", "写入的值还没有出现在录入控件以外：" + "、".join(missing_values)
    if not proof and not filled:
        return "failed", WEAK_EXPECT
    return "passed", ""


def unsealed_summary(run_dir: Path) -> str:
    bits = []
    for _file, case in _cases(run_dir):
        if case.get("status") in TERMINAL:
            continue
        label = str(case.get("case_id") or case.get("id") or "未命名")
        bits.append(f"{label} 停在 {case.get('status') or '未知'}")
    return "、".join(bits) or "没有可封口的观察"


def _steps(case: dict) -> list[dict]:
    steps = case.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _windows_from_rounds(case: dict) -> list[dict]:
    rounds = case.get("rounds")
    if not isinstance(rounds, list):
        return []
    found = []
    for index, item in enumerate(rounds):
        if not isinstance(item, dict) or not isinstance(item.get("seen_text"), str):
            continue
        shot = item.get("screenshot") if isinstance(item.get("screenshot"), dict) else {}
        found.append({
            "id": f"round-{index + 1}",
            "step_index": index,
            "kind": "window",
            "file": shot.get("file") or "",
            "sha256": shot.get("sha256") or "",
            "seen_text": item["seen_text"],
        })
    return found


def _view_evidence(case: dict) -> list[dict]:
    raw = case.get("evidence")
    stored = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if any(item.get("kind") == "window" for item in stored):
        return stored
    consoles = [item for item in stored if item.get("kind") == "console"]
    return _windows_from_rounds(case) + consoles


def _windows_for(evidence: list[dict], step: dict, index: int) -> list[dict]:
    ids = step.get("evidence_ids")
    if isinstance(ids, list) and ids:
        found = [item for item in evidence if item.get("kind") == "window" and item.get("id") in ids]
        if found:
            return found
    keys = {index}
    if isinstance(step.get("index"), int) and not isinstance(step.get("index"), bool):
        keys.add(step["index"])
    return [item for item in evidence if item.get("kind") == "window" and item.get("step_index") in keys]


def _seen(evidence: list[dict], step: dict, index: int) -> str | None:
    shots = _windows_for(evidence, step, index)
    if not shots or not isinstance(shots[-1].get("seen_text"), str):
        return None
    return shots[-1]["seen_text"]


def _shows(seen: str, expect) -> bool:
    if not isinstance(expect, list) or not expect:
        return True
    for item in expect:
        if isinstance(item, str) and item and item not in seen:
            return False
    return True


def _pk_literals(case: dict, baseline: dict) -> list[str]:
    raw = case.get("business_pk")
    if raw is None:
        fixtures = baseline.get("fixtures") if isinstance(baseline.get("fixtures"), dict) else {}
        fixture = fixtures.get(case.get("check_id")) if isinstance(fixtures.get(case.get("check_id")), dict) else {}
        raw = fixture.get("pk")
    if isinstance(raw, dict):
        return [str(value) for value in raw.values() if str(value)]
    if isinstance(raw, str) and raw:
        return [raw]
    if isinstance(raw, list):
        return [str(value) for value in raw if str(value)]
    return []


def _needles(case: dict, steps: list[dict], baseline: dict) -> list[str]:
    found = _pk_literals(case, baseline)
    for step in steps:
        expect = step.get("expect_text")
        if not isinstance(expect, list):
            continue
        for item in expect:
            if isinstance(item, str) and item and item not in found:
                found.append(item)
    return found


def _inside(run_dir: Path, relative: str) -> Path | None:
    if not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        return None
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        return None
    file = run_dir / relative
    try:
        if not file.resolve().is_relative_to(run_dir.resolve()):
            return None
    except OSError:
        return None
    if not file.is_file():
        return None
    return file


def _line_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
    return text


def _relevant_lines(run_dir: Path, needles: list[str]) -> list[str]:
    if not needles:
        return []
    file = _inside(run_dir, "console.log")
    if file is None:
        return []
    found = []
    for raw in file.read_text(encoding="utf-8", errors="replace").splitlines():
        text = _line_text(raw)
        if any(needle in text or needle in raw for needle in needles):
            found.append(text or raw)
    return found


def _db_matches(case: dict) -> bool:
    if "expect_state" not in case:
        return True
    return case.get("state_after") == case.get("expect_state")


def _steps_match(case: dict, evidence: list[dict], steps: list[dict], *, excuse_last: bool) -> bool:
    for index, step in enumerate(steps):
        if excuse_last and index == len(steps) - 1:
            continue
        seen = _seen(evidence, step, index)
        if seen is None or not _shows(seen, step.get("expect_text")):
            return False
    return _db_matches(case)


def _eligible(case: dict, evidence: list[dict], steps: list[dict]) -> bool:
    if case.get("sealed") is True or case.get("status") != "running" or case.get("pending") is not None:
        return False
    if not steps:
        return False
    return _seen(evidence, steps[-1], len(steps) - 1) is not None


def _remember_console(run_dir: Path, case: dict, lines: list[str], step_index: int) -> None:
    name = _case_id(case)
    relative = f"cases/{name}/console.png"
    path = run_dir / "cases" / name / "console.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = console_png(lines)
    path.write_bytes(blob)
    evidence = case.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence.append({
        "id": f"console-{name}",
        "step_index": step_index,
        "kind": "console",
        "file": relative,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "time": timestamp(),
        "note": CONSOLE_NOTE,
    })
    case["evidence"] = evidence
    steps = _steps(case)
    if steps:
        last = steps[-1]
        ids = last.get("evidence_ids")
        if not isinstance(ids, list):
            ids = []
        ids.append(f"console-{name}")
        last["evidence_ids"] = ids


def _seal_final_observation(case: dict, baseline: dict) -> dict:
    """一轮最终观察：用页面上的证明文字和结果值封口，不要求逐步对上序号。"""
    updated = json.loads(json.dumps(case, ensure_ascii=False))
    rounds = [
        item for item in (updated.get("rounds") or [])
        if isinstance(item, dict) and isinstance(item.get("seen_text"), str)
    ]
    seen = rounds[-1]["seen_text"] if rounds else ""
    shot = rounds[-1].get("screenshot") if rounds else None
    if isinstance(shot, dict) and shot.get("file") and shot.get("sha256"):
        evidence = updated.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        evidence.append({
            "id": "round-final",
            "step_index": 0,
            "kind": "window",
            "file": shot["file"],
            "sha256": shot["sha256"],
            "seen_text": seen,
        })
        updated["evidence"] = evidence
    prepared = prepared_values(updated, baseline)
    known = prepared if isinstance(baseline, dict) and isinstance(baseline.get("test_values"), dict) and updated.get("check_id") in baseline.get("test_values", {}) else None
    if updated.get("data_empty") is True:
        known = prepared
    outcome, reason = judge_final(
        updated, seen, known, data_empty=data_is_empty(updated, baseline),
    )
    if outcome == "wait":
        outcome = "failed"
        reason = reason or OBSERVATION_MISS
    updated["status"] = outcome
    updated["result_reason"] = reason
    updated["sealed"] = True
    return updated


def seal_case(run_dir: Path, case: dict, baseline: dict | None = None) -> dict | None:
    """只封仍在执行且末步已有窗口观察的用例。暂停、阻塞和已有失败原样返回。"""
    snapshot = baseline if isinstance(baseline, dict) else _baseline(run_dir)
    if case.get("observation") == "final":
        if case.get("sealed") is True or case.get("status") != "running" or case.get("pending") is not None:
            return None
        return _seal_final_observation(case, snapshot)
    evidence = _view_evidence(case)
    steps = _steps(case)
    if not _eligible(case, evidence, steps):
        return None
    updated = json.loads(json.dumps(case, ensure_ascii=False))
    view = _view_evidence(updated)
    last_seen = _seen(view, steps[-1], len(steps) - 1)
    page_ok = _shows(last_seen or "", steps[-1].get("expect_text"))
    if page_ok:
        matched = _steps_match(updated, view, steps, excuse_last=False)
        updated["status"] = "passed" if matched else "failed"
        updated["result_reason"] = "" if matched else OBSERVATION_MISS
    else:
        lines = _relevant_lines(run_dir, _needles(updated, steps, snapshot))
        if not lines:
            updated["status"] = "blocked"
            updated["result_reason"] = MISSING_LOG
            updated["blocked_reason"] = MISSING_LOG
        else:
            last = steps[-1]
            step_index = last["index"] if isinstance(last.get("index"), int) and not isinstance(last.get("index"), bool) else len(steps) - 1
            _remember_console(run_dir, updated, lines, step_index)
            matched = _steps_match(updated, view, steps, excuse_last=True)
            updated["status"] = "passed" if matched else "failed"
            updated["result_reason"] = "" if matched else OBSERVATION_MISS
    updated["sealed"] = True
    return updated


def seal_run(run_dir: Path) -> None:
    baseline = _baseline(run_dir)
    for file, case in _cases(run_dir):
        updated = seal_case(run_dir, case, baseline)
        if updated is not None:
            write_json(file, updated)


def _report_ready(cases: list[dict]) -> bool:
    return bool(cases) and all(case.get("status") in TERMINAL for case in cases)


def _host(state: dict) -> str:
    frozen = state.get("environment_host")
    if isinstance(frozen, str) and frozen.strip():
        return frozen.strip()
    base = state.get("base_url") if isinstance(state.get("base_url"), str) else ""
    return urlparse(base).hostname or ""


def _snap(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _image_text(run_dir: Path, item: dict, label: str) -> str:
    relative = str(item.get("file") or "")
    file = _inside(run_dir, relative)
    recorded = item.get("sha256")
    if file is None or not isinstance(recorded, str) or not recorded:
        return f"证据缺失：{relative or label}"
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    if digest != recorded:
        return f"证据缺失：{relative}"
    return f"![{label}]({relative})"


def _report_images(case: dict) -> list[dict]:
    raw = case.get("evidence")
    stored = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    windows = [item for item in stored if item.get("kind") == "window"]
    consoles = [item for item in stored if item.get("kind") == "console"]
    if not windows:
        windows = _windows_from_rounds(case)
    windows.sort(key=lambda item: item.get("step_index") if isinstance(item.get("step_index"), int) else 0)
    return windows + consoles


def _last_seen(case: dict) -> str:
    rounds = [
        item for item in (case.get("rounds") or [])
        if isinstance(item, dict) and isinstance(item.get("seen_text"), str)
    ]
    if rounds:
        return rounds[-1]["seen_text"]
    seen = case.get("seen")
    return seen if isinstance(seen, str) else ""


def _join_values(values: list[str]) -> str:
    return "、".join(values) if values else "—"


def _value_account(run_dir: Path, case: dict) -> list[str]:
    """准备、写入、出现在录入控件以外，用的是同一份值。条数是全量。"""
    prepared = prepared_values(case, _baseline(run_dir))
    filled = filled_values(case)
    seen = _last_seen(case)
    appeared = [item for item in filled if outside_entry(seen, item)]
    missing = [item for item in filled if item not in appeared]
    count = page_result_count(seen)
    lines = [
        f"- 准备：{len(prepared)} 条：{_join_values(prepared)}",
        f"- 写入：{len(filled)} 条：{_join_values(filled)}",
    ]
    if count:
        lines.append(f"- 页面结果：{count}")
    lines.append(f"- 出现在录入控件以外：{len(appeared)} 条：{_join_values(appeared)}")
    if missing and not count:
        lines.append(f"- 未出现：{len(missing)} 条：{_join_values(missing)}")
    return lines


def _case_section(run_dir: Path, case: dict) -> str:
    case_id = _case_id(case)
    title = str(case.get("title") or "").strip()
    goal = str(case.get("goal") or "").strip()
    heading = title or goal
    result = RESULT.get(str(case.get("status") or ""), str(case.get("status") or ""))
    pk = "、".join(_pk_literals(case, _baseline(run_dir))) or "—"
    lines = [
        f"## {case_id} {heading}",
        "",
        f"- 编号：{case_id}",
        f"- 标题：{heading}",
        f"- 结果：{result}",
    ]
    reason = str(case.get("result_reason") or "").strip()
    if reason:
        lines.append(f"- 原因：{reason}")
    lines.extend(_value_account(run_dir, case))
    lines.extend([
        f"- 业务主键：{pk}",
        f"- 操作前状态：{_snap(case.get('state_before'))}",
        f"- 操作后查库结果：{_snap(case.get('state_after'))}",
        "",
    ])
    for item in _report_images(case):
        label = "控制台" if item.get("kind") == "console" else f"步骤 {item.get('step_index', 0)}"
        lines.append(_image_text(run_dir, item, label))
        lines.append("")
        if item.get("kind") == "console" and isinstance(item.get("note"), str) and item.get("note"):
            lines.append(item["note"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _markdown(run_dir: Path, cases: list[dict]) -> str:
    state = read_state(run_dir)
    requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else {}
    lines = [
        "# 验收报告",
        "",
        f"- 需求编号：{requirement.get('id') or ''}",
        f"- 标题：{requirement.get('title') or ''}",
        f"- 测试环境：{_host(state)}",
        f"- 开发分支：{state.get('branch') or ''}",
        f"- commit：{state.get('commit') or ''}",
        "",
    ]
    ordered = sorted(cases, key=lambda item: str(item.get("case_id") or item.get("id") or ""))
    for case in ordered:
        lines.append(_case_section(run_dir, case).rstrip())
    return "\n".join(lines).rstrip() + "\n"


def replace_report_section(run_dir: Path, case_id: str) -> None:
    """报告已存在时只替换这一节。不改开头，也不改其它用例的状态。"""
    report = run_dir / "report.md"
    if report.is_symlink() or not report.is_file():
        return
    original = report.read_text(encoding="utf-8")
    case = None
    for _file, item in _cases(run_dir):
        if _case_id(item) == case_id:
            case = item
            break
    if case is None:
        return
    section = _case_section(run_dir, case).rstrip() + "\n"
    pattern = re.compile(rf"^## {re.escape(case_id)}\b.*?(?=^## |\Z)", re.M | re.S)
    if pattern.search(original):
        updated = pattern.sub(lambda _match: section + "\n", original, count=1)
    else:
        updated = original.rstrip() + "\n\n" + section
    if updated != original:
        _write_text(report, updated)
        _write_zip(run_dir)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _add_zip(archive: zipfile.ZipFile, run_dir: Path, path: Path) -> None:
    if path.name in DEMO_NAMES or path.is_symlink() or not path.is_file():
        return
    try:
        if not path.resolve().is_relative_to(run_dir.resolve()):
            return
    except OSError:
        return
    archive.write(path, path.relative_to(run_dir).as_posix())


def _write_zip(run_dir: Path) -> None:
    temporary = run_dir / "report.zip.tmp"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        report = run_dir / "report.md"
        if report.is_file() and not report.is_symlink():
            _add_zip(archive, run_dir, report)
        console = run_dir / "console.log"
        if console.is_file():
            _add_zip(archive, run_dir, console)
        for folder_name, suffixes in (("cases", {".png", ".jpg", ".jpeg", ".json"}), ("images", {".png", ".jpg", ".jpeg"})):
            root = run_dir / folder_name
            if root.is_symlink() or not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() not in suffixes:
                    continue
                _add_zip(archive, run_dir, path)
    temporary.replace(run_dir / "report.zip")


def render_report(run_dir: Path) -> Path | None:
    """全部用例已是通过、失败、阻塞或跳过时才写 report.md。不调用封口，不改用例 status。"""
    cases = [case for _file, case in _cases(run_dir)]
    if not _report_ready(cases):
        return None
    _write_text(run_dir / "report.md", _markdown(run_dir, cases))
    _write_zip(run_dir)
    state = read_state(run_dir)
    state["stage"] = "report"
    state["exit_code"] = 0
    write_json(run_dir / "state.json", state)
    return run_dir / "report.md"
