import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DATABASE_RE = re.compile(r"^[A-Za-z0-9_]+$")
GIT_TIMEOUT = 10


def token_configured() -> bool:
    if os.environ.get("YUNXIAO_TOKEN", "").strip():
        return True
    path = Path.home() / ".grok" / "qa-platform" / "yunxiao-token"
    try:
        if not path.is_file():
            return False
        if path.stat().st_mode & 0o777 != 0o600:
            return False
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


def ocu_available() -> bool:
    if shutil.which("ocu") or shutil.which("open-computer-use"):
        return True
    fallback = Path.home() / ".grok" / "bin" / "ocu"
    return fallback.is_file() and os.access(fallback, os.X_OK)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    from qa_platform.runner.preflight import READ_GIT, GitWriteDenied, git_verb

    if git_verb(args) not in READ_GIT:
        raise GitWriteDenied(git_verb(args) or "git")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )


def git_snapshot(repo: str) -> tuple[str | None, str | None, str | None, bool]:
    if not repo:
        return None, None, None, False
    path = Path(repo)
    if not path.is_dir():
        return None, None, "不是本地 Git 工作树", False
    try:
        inside = _run_git(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None, None, "不是本地 Git 工作树", False
        branch = _run_git(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
        commit = _run_git(["git", "-C", str(path), "rev-parse", "HEAD"])
        status = _run_git(["git", "-C", str(path), "status", "--porcelain"])
    except (OSError, subprocess.TimeoutExpired):
        return None, None, "读不到当前分支或 commit", False
    if branch.returncode != 0 or commit.returncode != 0:
        return None, None, "读不到当前分支或 commit", False
    dirty = status.returncode == 0 and bool(status.stdout.strip())
    return branch.stdout.strip() or None, commit.stdout.strip() or None, None, dirty


def branch_ok(name: str) -> bool:
    # 以 - 开头的名字会被 git 当成选项，也不是合法分支。
    if name.startswith("-") or any(char in name for char in "\n\r\0"):
        return False
    try:
        result = _run_git(["git", "check-ref-format", "--branch", name])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def entry_ok(value: str) -> bool:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    return True


def check_project(project: dict) -> dict:
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    repo = project.get("repo_path") or ""
    branch_name = project.get("branch") or ""
    remote = project.get("git_remote") or ""
    entry = project.get("yunxiao_entry") or ""
    edition = project.get("yunxiao_edition") or ""
    org = project.get("yunxiao_organization_id") or ""
    app_name = project.get("yunxiao_browser_app") or ""
    base = project.get("base_url") or ""
    hosts = project.get("allowed_hosts") or []
    databases = project.get("allowed_databases") or []

    dirty = False
    if not repo:
        missing.append("repo_path")
    else:
        _branch, _commit, git_error, dirty = git_snapshot(repo)
        if git_error:
            errors.append({"code": "repo_path", "message": git_error})
            dirty = False

    if not branch_name:
        missing.append("branch")
    elif not branch_ok(branch_name):
        errors.append({"code": "branch", "message": "分支名没有通过 git check-ref-format --branch"})

    if not remote:
        missing.append("git_remote")
    elif REMOTE_RE.fullmatch(remote) is None:
        errors.append({"code": "git_remote", "message": "git 远程名只允许字母、数字、点、下划线和连字符"})

    if not entry:
        missing.append("yunxiao_entry")
    elif not entry_ok(entry):
        errors.append({"code": "yunxiao_entry", "message": "云效入口必须是 http 或 https 地址，且不能带账号口令"})

    if edition not in {"center", "region"}:
        errors.append({"code": "yunxiao_edition", "message": "yunxiao_edition 只能是 center 或 region"})
    elif edition == "center" and not org:
        missing.append("yunxiao_organization_id")

    token = token_configured()
    ocu = ocu_available()
    if not token and not app_name:
        missing.append("yunxiao_browser_app")
    elif not token and not ocu:
        errors.append({"code": "ocu_unavailable", "message": "没有云效令牌，本机也找不到 ocu"})

    base_host = None
    if not base:
        missing.append("base_url")
    elif BASE_URL_RE.fullmatch(base) is None:
        errors.append({
            "code": "base_url",
            "message": "base_url 只能是 http(s) 的主机，可带端口，不能有路径、查询或片段",
        })
    else:
        base_host = urlsplit(base).hostname

    if not isinstance(hosts, list) or not hosts:
        missing.append("allowed_hosts")
    elif any(not isinstance(item, str) or HOST_RE.fullmatch(item) is None for item in hosts):
        errors.append({"code": "allowed_hosts", "message": "允许的域名不能包含协议、路径或端口"})
    elif base_host and all(item.casefold() != base_host.casefold() for item in hosts):
        errors.append({"code": "base_url_host", "message": "base_url 的 host 不在允许的域名里"})

    if not isinstance(databases, list) or not databases:
        missing.append("allowed_databases")
    elif any(not isinstance(item, str) or DATABASE_RE.fullmatch(item) is None for item in databases):
        errors.append({"code": "allowed_databases", "message": "库名只允许字母、数字和下划线"})

    return {
        "ok": not missing and not errors,
        "missing": missing,
        "errors": errors,
        "dirty": dirty,
        "token_configured": token,
        "ocu_available": ocu,
    }
