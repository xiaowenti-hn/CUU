"""从指定的一步接着做。不另开一次运行。"""

import json
from pathlib import Path

from qa_platform.runner.preflight import note
from qa_platform.store import ApprovalError, read_state, write_json

STEPS = ("前置检查", "需求正文", "方案", "基线", "执行")
DROP_APPROVALS = {
    "前置检查": {"plan", "baseline", "baseline_actual", "fixture_keys", "sql", "sql_offer"},
    "需求正文": {"plan", "baseline", "baseline_actual", "fixture_keys", "sql", "sql_offer"},
    "方案": {"plan", "baseline", "baseline_actual", "fixture_keys", "sql", "sql_offer"},
    "基线": {"baseline", "baseline_actual", "fixture_keys", "sql", "sql_offer"},
    "执行": set(),
}


def blocked_step(state: dict) -> str:
    events = state.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if isinstance(event, dict) and event.get("status") == "阻塞":
                step = event.get("step")
                if isinstance(step, str) and step.strip():
                    return step.strip()
    reason = state.get("stop_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "执行"


def _reset_blocked_cases(run_dir: Path) -> None:
    root = run_dir / "cases"
    if not root.is_dir() or root.is_symlink():
        return
    for folder in root.iterdir():
        path = folder / "case.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(case, dict) or case.get("status") not in {"blocked", "paused", "failed", "skipped"}:
            continue
        case["status"] = "waiting"
        case["blocked_reason"] = ""
        case["pending"] = None
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def _drop_approvals(state: dict, kinds: set[str]) -> None:
    approvals = state.get("approvals")
    if not isinstance(approvals, list) or not kinds:
        return
    state["approvals"] = [
        item for item in approvals
        if not (isinstance(item, dict) and item.get("kind") in kinds)
    ]


def _clear_baseline(run_dir: Path) -> None:
    baseline = run_dir / "baseline.json"
    if baseline.is_file() and not baseline.is_symlink():
        baseline.unlink()


def _rewind(run_dir: Path, state: dict, step: str) -> None:
    """留下这一步之前的结果，让这一步和它后面的事重做。"""
    _drop_approvals(state, DROP_APPROVALS[step])
    if step != "执行":
        _clear_baseline(run_dir)
    if step in {"前置检查", "需求正文"}:
        state["requirement"] = None
    if step == "前置检查":
        state["commit"] = ""
    state["review"] = None


def prepare_retry(run_dir: Path, step: str | None = None, browser: str | None = None) -> str:
    state = read_state(run_dir)
    review = state.get("review") if isinstance(state.get("review"), dict) else None
    if step is None:
        if review is not None:
            raise ApprovalError(409, "还有待确认，先确认或留下，再从阻塞重试")
        if state.get("exit_code") != 2 and not state.get("stop_reason"):
            raise ApprovalError(409, "这次运行没有阻塞")
        chosen = blocked_step(state)
    else:
        chosen = step.strip()
        if chosen not in STEPS:
            raise ApprovalError(400, "没有这一步")
        if review is not None and review.get("step") == chosen:
            raise ApprovalError(409, "这一步还在等确认")
    _rewind(run_dir, state, chosen)
    if browser in {"edge", "chrome"}:
        state["product_browser"] = browser
    state["stop_reason"] = ""
    state["exit_code"] = None
    write_json(run_dir / "state.json", state)
    if chosen in {"基线", "执行"}:
        _reset_blocked_cases(run_dir)
    note(run_dir, step=chosen, basis=f"从「{chosen}」重试，不重新开始")
    return chosen
