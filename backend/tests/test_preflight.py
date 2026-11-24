import json
import subprocess
import sys
import time
from pathlib import Path

from qa_platform.runner.preflight import execute, note, record
from tests.conftest import auth, complete_body


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.email", "qa@example.com")
    git(path, "config", "user.name", "QA")
    git(path, "config", "commit.gpgsign", "false")


def commit_file(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(message + "\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", message)


def published(tmp: Path) -> tuple[Path, Path]:
    local = tmp / "local"
    init_repo(local)
    commit_file(local, "README", "base")
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    git(local, "remote", "add", "origin", str(bare))
    git(local, "push", "-u", "origin", "main")
    return bare, local


def clone(bare: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", str(bare), str(dest)], check=True, capture_output=True)
    git(dest, "config", "user.email", "qa@example.com")
    git(dest, "config", "user.name", "QA")
    git(dest, "config", "commit.gpgsign", "false")


def write_run(tmp: Path, repo: Path) -> Path:
    run = tmp / "runs" / "run-abc123abc123"
    run.mkdir(parents=True)
    state = {
        "id": run.name,
        "repo_path": str(repo.resolve()),
        "branch": "main",
        "git_remote": "origin",
        "commit": "",
        "events": [],
        "exit_code": None,
        "stage": "preflight",
        "stop_reason": "",
    }
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run


def load_state(run: Path) -> dict:
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def spy(monkeypatch):
    commands = []
    real = subprocess.run

    def wrapped(args, **kwargs):
        commands.append(list(args))
        assert kwargs.get("shell") is not True
        return real(args, **kwargs)

    monkeypatch.setattr("qa_platform.runner.preflight.subprocess.run", wrapped)
    return commands


def test_git_write_commands_are_refused():
    from qa_platform.runner.preflight import GitWriteDenied, run_git

    try:
        run_git(["git", "-C", "/tmp", "fetch", "origin"], 1)
    except GitWriteDenied as exc:
        assert "fetch" in str(exc)
    else:
        raise AssertionError("fetch 应被拒绝")


def test_progress_note_keeps_the_exit_code(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({"events": [], "exit_code": None}), encoding="utf-8")
    record(run, exit_code=0, event_status="完成", basis="已记录 commit abc", commit="abc")
    note(run, step="需求正文", basis="正在读取云效需求")
    state = load_state(run)
    assert state["exit_code"] == 0
    assert state["commit"] == "abc"
    assert state["events"][-1]["status"] == "进行中"
    assert state["events"][-1]["basis"] == "正在读取云效需求"
    assert "正在读取云效需求" in (run / "执行进度.md").read_text(encoding="utf-8")


def test_untracked_file_does_not_fetch(tmp_path, monkeypatch):
    _bare, local = published(tmp_path)
    (local / "notes.txt").write_text("untracked\n", encoding="utf-8")
    commands = spy(monkeypatch)
    run = write_run(tmp_path, local)
    assert execute(run) == 2
    assert all("fetch" not in args for args in commands)
    assert all("pull" not in args for args in commands)
    state = load_state(run)
    assert state["exit_code"] == 2
    assert state["commit"] == ""
    assert "未执行 fetch" in state["events"][-1]["basis"]
    assert (local / "notes.txt").is_file()


def test_diverged_remote_keeps_the_local_commit(tmp_path, monkeypatch):
    bare, local = published(tmp_path)
    other = tmp_path / "other"
    clone(bare, other)
    commit_file(local, "local.txt", "local")
    before = head(local)
    commit_file(other, "remote.txt", "remote")
    git(other, "push", "origin", "main")
    commands = spy(monkeypatch)
    run = write_run(tmp_path, local)
    assert execute(run) == 0
    assert head(local) == before
    assert all("fetch" not in args for args in commands)
    assert all("checkout" not in args for args in commands)
    assert all("merge" not in args for args in commands)
    assert all("pull" not in args for args in commands)
    state = load_state(run)
    assert state["exit_code"] == 0
    assert state["commit"] == before


def test_clean_repo_records_the_local_commit(tmp_path, monkeypatch):
    bare, local = published(tmp_path)
    before = head(local)
    other = tmp_path / "other"
    clone(bare, other)
    commit_file(other, "more.txt", "more")
    git(other, "push", "origin", "main")
    commands = spy(monkeypatch)
    run = write_run(tmp_path, local)
    assert execute(run) == 0
    assert head(local) == before
    assert any(args[3:6] == ["rev-parse", "--abbrev-ref", "HEAD"] for args in commands)
    assert any(args[3:5] == ["rev-parse", "HEAD"] for args in commands)
    assert all("fetch" not in args for args in commands)
    assert all("checkout" not in args for args in commands)
    assert all("merge" not in args for args in commands)
    assert all("pull" not in args for args in commands)
    state = load_state(run)
    assert state["commit"] == before
    assert state["exit_code"] == 0
    assert state["branch"] == "main"


def test_missing_branch_is_blocked(tmp_path):
    _bare, local = published(tmp_path)
    run = write_run(tmp_path, local)
    state = load_state(run)
    state["branch"] = "missing-topic"
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    before = head(local)
    assert execute(run) == 2
    assert head(local) == before
    saved = load_state(run)
    assert saved["exit_code"] == 2
    assert saved["commit"] == ""
    assert saved["events"][-1]["basis"] == "当前分支是 main，配置的分支是 missing-topic"


def test_lock_failure_exits_2(tmp_path):
    import fcntl

    run = write_run(tmp_path, tmp_path)
    handle = (run / ".run.lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    proc = subprocess.run(
        [sys.executable, "-m", "qa_platform.runner.main", "--out", str(run)],
        input=b"\n\n",
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 2
    assert b"Traceback" not in proc.stderr
    assert load_state(run)["exit_code"] == 2
    handle.close()


def wait_run(client, app, run_id: str) -> dict:
    last = None
    for _ in range(100):
        last = client.get(f"/api/runs/{run_id}", headers=auth(app)).json()
        if last["status"] != "running":
            return last
        time.sleep(0.05)
    return last


def test_api_clean_run_exposes_commit(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    root = tmp_path / "repos"
    root.mkdir()
    bare, local = published(root)
    other = root / "other"
    clone(bare, other)
    commit_file(other, "more.txt", "more")
    git(other, "push", "origin", "main")
    created = client.post("/api/projects", headers=auth(app), json=complete_body(local))
    project_id = created.json()["id"]
    started = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert started.status_code == 202
    run_id = started.json()["run"]
    body = wait_run(client, app, run_id)
    assert body["status"] == "blocked"
    assert body["branch"] == "main"
    assert body["commit"] == head(local)
    assert body["exit_code"] == 2
    assert body["requirement"] is None
    log = (app.state.var_dir / "runs" / run_id / "runner.log").read_text(encoding="utf-8")
    state_text = (app.state.var_dir / "runs" / run_id / "state.json").read_text(encoding="utf-8")
    assert "secret-token" not in log
    assert "secret-token" not in state_text
