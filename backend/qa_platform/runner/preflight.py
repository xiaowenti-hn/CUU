"""冻结运行上的 Git 前置。只查询本地仓库，不取需求，不调模型，不查库。"""

import subprocess
from pathlib import Path

from qa_platform.store import read_state, timestamp, write_json

LOCAL_TIMEOUT = 20


def _append_event(run_dir: Path, *, event_status: str, basis: str, step: str,
                  exit_code: int | None = None, commit: str | None = None) -> None:
    state = read_state(run_dir)
    if not state:
        state = {"events": []}
    if commit is not None:
        state["commit"] = commit
    if exit_code is not None:
        state["exit_code"] = exit_code
    event = {
        "time": timestamp(),
        "status": event_status,
        "step": step,
        "basis": basis,
    }
    events = state.get("events")
    if not isinstance(events, list):
        events = []
    events.append(event)
    state["events"] = events
    write_json(run_dir / "state.json", state)
    lines = ["# 执行进度", ""]
    for item in events:
        clock = str(item.get("time", ""))[11:19]
        lines.append(f"- `{clock}` **{item.get('status')} · {item.get('step')}**。{item.get('basis')}")
    text = "\n".join(lines) + "\n"
    progress = run_dir / "执行进度.md"
    temporary = progress.with_name(progress.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(progress)
    print(f"[{event['time'][11:19]}] {event_status} · {step} — {basis}", flush=True)


def record(run_dir: Path, *, exit_code: int, event_status: str, basis: str,
           commit: str | None = None, step: str = "前置检查") -> None:
    _append_event(
        run_dir,
        event_status=event_status,
        basis=basis,
        step=step,
        exit_code=exit_code,
        commit=commit,
    )


def note(run_dir: Path, *, step: str, basis: str) -> None:
    """记下正在做的一步。不改退出码，页面轮询能马上看到。"""
    _append_event(run_dir, event_status="进行中", basis=basis, step=step)


def block(run_dir: Path, basis: str, step: str = "前置检查") -> int:
    record(run_dir, exit_code=2, event_status="阻塞", basis=basis, step=step)
    return 2


READ_GIT = {"status", "rev-parse", "log", "show", "diff", "merge-base", "check-ref-format"}


class GitWriteDenied(RuntimeError):
    pass


def git_verb(args: list[str]) -> str:
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "-C":
            skip = True
            continue
        if arg == "git" or arg.startswith("-"):
            continue
        return arg
    return ""


def run_git(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    verb = git_verb(args)
    if verb not in READ_GIT:
        raise GitWriteDenied(verb or "git")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def execute(run_dir: Path) -> int:
    state = read_state(run_dir)
    repo = str(state.get("repo_path") or "")
    branch = str(state.get("branch") or "")
    try:
        status = run_git(["git", "-C", repo, "status", "--porcelain"], LOCAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return block(run_dir, "读不到工作区状态")
    if status.returncode != 0:
        return block(run_dir, "读不到工作区状态")
    if status.stdout.strip():
        return block(run_dir, "工作区有未提交改动，未执行 fetch")
    try:
        current = run_git(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return block(run_dir, "读不到当前分支")
    name = current.stdout.strip()
    if current.returncode != 0 or not name:
        return block(run_dir, "读不到当前分支")
    if name != branch:
        return block(run_dir, f"当前分支是 {name}，配置的分支是 {branch}")
    try:
        head = run_git(["git", "-C", repo, "rev-parse", "HEAD"], LOCAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return block(run_dir, "读不到 HEAD")
    commit = head.stdout.strip()
    if head.returncode != 0 or not commit:
        return block(run_dir, "读不到 HEAD")
    record(run_dir, exit_code=0, event_status="完成", basis=f"已记录 commit {commit}", commit=commit)
    return 0
