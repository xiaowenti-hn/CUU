"""按需求号摘录提交。只查询 Git，不改工作区。"""

import fcntl
import re
import subprocess
import uuid
from pathlib import Path

from qa_platform.runner.planner import propose, review_digest
from qa_platform.runner.preflight import READ_GIT, note, record
from qa_platform.store import read_state, timestamp, write_json

ALLOWED_GIT = READ_GIT
FORBIDDEN_GIT = {
    "fetch", "pull", "checkout", "switch", "merge", "rebase", "reset",
    "stash", "clean", "commit", "push",
}
BACKEND_SUFFIXES = {".py", ".java", ".kt", ".go", ".xml"}
FRONTEND_SUFFIXES = {".vue", ".html", ".tsx", ".jsx", ".js", ".ts"}
LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock",
    "poetry.lock", "gemfile.lock", "composer.lock", "packages.lock.json",
    "go.sum",
}
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
STRING_RE = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)""")
TEXT_NODE_RE = re.compile(r">([^<>{}\n][^<>{}]{0,240})<")
MATERIAL_LIMIT = 100_000


class GitDenied(RuntimeError):
    pass


def git_query(repo: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    if not args or args[0] in FORBIDDEN_GIT or args[0] not in ALLOWED_GIT:
        raise GitDenied(args[0] if args else "")
    if "--all" in args:
        raise GitDenied("--all")
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def whole_word(text: str, needle: str) -> bool:
    """与 git --word-regexp 相同：命中两端都必须落在非单词字符上。"""
    if not needle:
        return False
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return False
        before = index == 0 or not _word_char(text[index - 1])
        after_at = index + len(needle)
        after = after_at == len(text) or not _word_char(text[after_at])
        if before and after:
            return True
        start = index + 1


def _hashes(stdout: str) -> list[str]:
    found = []
    for line in stdout.splitlines():
        sha = line.strip()
        if SHA_RE.fullmatch(sha) and sha not in found:
            found.append(sha)
    return found


def _whole_word_hashes(stdout: str, needle: str) -> list[str]:
    found = []
    for record in stdout.split("\x1e"):
        body = record.strip("\n")
        if not body.strip():
            continue
        sha, sep, message = body.partition("\x1f")
        sha = sha.strip()
        if sep != "\x1f" or SHA_RE.fullmatch(sha) is None or sha in found:
            continue
        if whole_word(message, needle):
            found.append(sha)
    return found


def _word_regexp_unsupported(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stderr or ''}\n{result.stdout or ''}"
    return result.returncode != 0 and "unrecognized argument" in text and "--word-regexp" in text


def search_commits(repo: str, commit: str, serial: str) -> list[str] | None:
    if not serial or any(char in serial for char in "\r\n\0"):
        return None
    # 文档规定的 argv。--grep= 与需求号在同一个参数里，且不搜 --all。
    exact = git_query(
        repo,
        "log",
        commit,
        f"--grep={serial}",
        "--fixed-strings",
        "--word-regexp",
        "--format=%H",
    )
    if exact.returncode == 0:
        return _hashes(exact.stdout)
    # 本机 git log 不实现 --word-regexp。固定字符串候选再按整词边界过滤，
    # 这样 DSDD-123 不会命中 DSDD-1234。
    if not _word_regexp_unsupported(exact):
        return None
    scanned = git_query(
        repo,
        "log",
        commit,
        f"--grep={serial}",
        "--fixed-strings",
        "--format=%H%x1f%B%x1e",
    )
    if scanned.returncode != 0:
        return None
    return _whole_word_hashes(scanned.stdout, serial)


def _skipped(path: str) -> bool:
    name = Path(path).name.lower()
    parts = path.split("/")
    if "node_modules" in parts:
        return True
    if name in LOCK_NAMES or name.endswith(".lock") or name.endswith(".map"):
        return True
    if ".min." in name or name.endswith(".bundle.js") or name.endswith(".bundle.css"):
        return True
    return False


def _visible_text(path: str, diff: str) -> str:
    lines = []
    for raw in diff.splitlines():
        if raw.startswith(("diff ", "index ", "--- ", "+++ ", "@@", "Binary")):
            continue
        if raw.startswith("+"):
            lines.append(raw[1:])
        elif raw.startswith(" "):
            lines.append(raw[1:])
    snippet = "\n".join(lines)
    pieces: list[str] = []
    suffix = Path(path).suffix.lower()
    if suffix in {".vue", ".html", ".tsx", ".jsx"}:
        for match in TEXT_NODE_RE.finditer(snippet):
            text = match.group(1).strip()
            if text and not text.startswith("{{") and text not in pieces:
                pieces.append(text)
    for match in STRING_RE.finditer(snippet):
        literal = match.group(1)[1:-1].strip()
        if len(literal) >= 2 and literal not in pieces:
            pieces.append(literal)
    return "\n".join(pieces)


def material_for(repo: str, sha: str, path: str) -> str | None:
    if _skipped(path):
        return None
    suffix = Path(path).suffix.lower()
    if suffix not in BACKEND_SUFFIXES and suffix not in FRONTEND_SUFFIXES:
        return None
    shown = git_query(repo, "show", "-U30", "--format=", "--no-color", sha, "--", path)
    if shown.returncode != 0:
        return None
    text = shown.stdout
    if "\0" in text or "Binary files" in text or "GIT binary patch" in text:
        return None
    if suffix in BACKEND_SUFFIXES:
        body = text.strip()
        return f"## {path}\n{body}" if body else None
    visible = _visible_text(path, text)
    return f"## {path}\n{visible}" if visible else None


def changed_paths(repo: str, sha: str) -> list[str]:
    shown = git_query(repo, "show", "--name-only", "--pretty=format:", "--no-color", sha)
    if shown.returncode != 0:
        return []
    paths = []
    for line in shown.stdout.splitlines():
        path = line.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def collect_materials(repo: str, shas: list[str]) -> tuple[str, list[str], list[str]]:
    pieces: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    overflow = False
    for sha in shas:
        for path in changed_paths(repo, sha):
            rendered = material_for(repo, sha, path)
            if rendered is None:
                continue
            extra = len(rendered) + (2 if pieces else 0)
            if overflow or total + extra > MATERIAL_LIMIT:
                overflow = True
                if path not in dropped:
                    dropped.append(path)
                continue
            pieces.append(rendered)
            total += extra
            if path not in kept:
                kept.append(path)
    if overflow:
        return "", [], dropped
    return "\n\n".join(pieces), kept, []


def verify_commit(repo: str, sha: str, head: str) -> str | None:
    if SHA_RE.fullmatch(sha) is None:
        return None
    verified = git_query(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if verified.returncode != 0:
        return None
    full = verified.stdout.strip()
    if SHA_RE.fullmatch(full) is None:
        return None
    ancestor = git_query(repo, "merge-base", "--is-ancestor", full, head)
    if ancestor.returncode != 0:
        return None
    return full


def _halt(run_dir: Path, basis: str, step: str) -> int:
    record(run_dir, exit_code=2, event_status="阻塞", basis=basis, step=step)
    return 2


def open_commits_empty(run_dir: Path, serial: str) -> None:
    state = read_state(run_dir)
    payload = {"requirement_id": serial, "commits": []}
    state["review"] = {
        "id": uuid.uuid4().hex,
        "digest": review_digest("commits_empty", payload),
        "kind": "commits_empty",
        "step": "提交搜索",
        "title": "没有找到包含需求号的提交",
        "payload": payload,
        "requested_at": timestamp(),
    }
    state["exit_code"] = 4
    state["stage"] = "plan"
    write_json(run_dir / "state.json", state)
    record(
        run_dir,
        exit_code=4,
        event_status="待确认",
        basis="提交搜索结果为空，未请求模型",
        step="提交搜索",
    )


def continue_plan(run_dir: Path, deepseek_key: str, *, complete=None) -> int:
    state = read_state(run_dir)
    if state.get("review"):
        if state.get("exit_code") != 4:
            record(
                run_dir, exit_code=4, event_status="待确认",
                basis="已有待确认项，未读取手工提交", step="方案",
            )
        return 4
    if state.get("commit_search") is None:
        requirement = state.get("requirement") if isinstance(state.get("requirement"), dict) else {}
        serial = str(requirement.get("id") or "")
        repo = str(state.get("repo_path") or "")
        commit = str(state.get("commit") or "")
        if not serial:
            return _halt(run_dir, "没有需求号，不能搜索提交", "提交搜索")
        try:
            shas = search_commits(repo, commit, serial)
        except GitDenied:
            return _halt(run_dir, "提交搜索失败，未请求模型", "提交搜索")
        if shas is None:
            return _halt(run_dir, "提交搜索失败，未请求模型", "提交搜索")
        state = read_state(run_dir)
        state["commit_search"] = shas
        state["stage"] = "plan"
        write_json(run_dir / "state.json", state)
        if not shas:
            open_commits_empty(run_dir, serial)
            return 4
    state = read_state(run_dir)
    if state.get("review"):
        return 4
    searched = state.get("commit_search") or []
    manual = state.get("manual_commits") if isinstance(state.get("manual_commits"), list) else []
    chosen = list(searched) if searched else list(manual)
    if not chosen:
        return 4
    repo = str(state.get("repo_path") or "")
    text, files, dropped = collect_materials(repo, [str(item) for item in chosen])
    if dropped:
        listed = "、".join(dropped)
        return _halt(run_dir, f"材料超过 100000 字符，已丢下文件：{listed}", "方案")
    from qa_platform.runner.baseline import code_table_catalog
    from qa_platform.runner.menu_page import load_menus
    from qa_platform.runner.page_index import build_page_index, use_live_tables

    allowed = [item for item in state.get("allowed_databases") or [] if isinstance(item, str)]
    pages = build_page_index(repo, str(state.get("front_repo_path") or ""))
    extra = [str(row.get("table") or "") for row in pages if row.get("table")]
    catalog = code_table_catalog(allowed[0], text, extra) if len(allowed) == 1 else []
    pages = use_live_tables(pages, catalog)
    if pages:
        note(run_dir, step="方案", basis=f"页签索引对齐了 {len(pages)} 个页签")
    shown = []
    seen_tables = set()
    for item in catalog:
        table = item.get("table")
        if table in seen_tables:
            continue
        seen_tables.add(table)
        shown.append({key: value for key, value in item.items() if key != "annotation"})
    menus = load_menus(allowed[0]) if len(allowed) == 1 else []
    return propose(
        run_dir,
        key=deepseek_key,
        shas=[str(item) for item in chosen],
        files=files,
        materials=text,
        complete=complete,
        live_tables=shown,
        menus=menus,
        pages=pages,
    )


def apply_manual_commits(run_dir: Path, shas: list[str]) -> tuple[int, dict]:
    """在运行目录锁里核对手工提交。成功时已经落盘，调用方再启动 runner。"""
    if not shas:
        return 400, {"error": "请给出至少一个提交"}
    handle = (run_dir / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return 409, {"error": "已有测试在运行，请等当前运行结束"}
    try:
        state = read_state(run_dir)
        if state.get("commit_search") != []:
            return 409, {"error": "只有提交搜索结果为空时才能手工指定提交"}
        if (run_dir / "plan.json").is_file():
            return 409, {"error": "已经有方案，不能再改提交"}
        review = state.get("review")
        if review is not None and not (isinstance(review, dict) and review.get("kind") == "commits_empty"):
            return 409, {"error": "当前评审不能改为手工提交"}
        repo = str(state.get("repo_path") or "")
        head = str(state.get("commit") or "")
        canonical = []
        for sha in shas:
            full = verify_commit(repo, sha, head)
            if not full:
                return 400, {"error": "提交不可用或不在这次运行的历史上"}
            if full not in canonical:
                canonical.append(full)
        state["manual_commits"] = canonical
        if isinstance(review, dict) and review.get("kind") == "commits_empty":
            state["review"] = None
        write_json(run_dir / "state.json", state)
        return 200, {"manual_commits": canonical}
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
