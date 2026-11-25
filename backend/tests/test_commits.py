import base64
import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from qa_platform.runner.commits import (
    GitDenied,
    collect_materials,
    continue_plan,
    git_query,
    search_commits,
    whole_word,
)
from qa_platform.runner.main import main
from qa_platform.runner.planner import acceptance_criteria, criteria_gaps, deepseek_complete, propose, review_digest, validate_plan
from tests.conftest import auth

FORBIDDEN = {
    "fetch", "pull", "checkout", "switch", "merge", "rebase", "reset",
    "stash", "clean", "commit", "push",
}
JAVA_PATH = "src/main/java/RefundService.java"
KEY = "sk-manual-secret"


def test_acceptance_criteria_are_taken_as_written():
    text = """业务场景
电商账单

验收标准
1. 支付宝账单按交易号能查出已有账单
2. 超过 500 个单号时按页面提示处理
- 京东、抖音同样按交易号查询

评论
阿宁：其他平台线上观察
"""
    items = acceptance_criteria(text)
    assert items == [
        "支付宝账单按交易号能查出已有账单",
        "超过 500 个单号时按页面提示处理",
        "京东、抖音同样按交易号查询",
    ]
    assert acceptance_criteria("只有业务场景，没有标准一节") == []
    covered = {"cases": [{"title": "支付宝账单按交易号能查出已有账单", "steps": [{"name": "超过 500 个单号时按页面提示处理"}]}]}
    gaps = criteria_gaps(covered, items)
    assert len(gaps) == 1
    assert "京东、抖音" in gaps[0]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def porcelain(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def snapshot(repo: Path) -> tuple[str, str]:
    return head(repo), porcelain(repo)


def assert_unchanged(repo: Path, before: tuple[str, str]) -> None:
    assert snapshot(repo) == before


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.email", "qa@example.com")
    git(path, "config", "user.name", "QA")
    git(path, "config", "commit.gpgsign", "false")


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    file = repo / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return head(repo)


def spy_git(monkeypatch):
    commands = []
    real = subprocess.run

    def wrapped(args, **kwargs):
        commands.append(list(args))
        assert kwargs.get("shell") is not True
        return real(args, **kwargs)

    monkeypatch.setattr("qa_platform.runner.commits.subprocess.run", wrapped)
    return commands


def subcommand(args: list[str]) -> str:
    if not args or args[0] != "git":
        return ""
    if len(args) > 3 and args[1] == "-C":
        return args[3]
    return args[1] if len(args) > 1 else ""


def assert_queries_only(commands) -> None:
    for args in commands:
        assert "--all" not in args
        name = subcommand(args)
        assert name not in FORBIDDEN
        assert name in {"status", "rev-parse", "log", "show", "diff", "merge-base", "check-ref-format"}


def exact_grep(repo: Path, commit: str, serial: str) -> list[str]:
    return ["git", "-C", str(repo), "log", commit, f"--grep={serial}", "--fixed-strings", "--word-regexp", "--format=%H"]


def write_run(parent: Path, repo: Path, **overrides) -> Path:
    run = parent / "run-abc123abc123"
    run.mkdir(parents=True, exist_ok=True)
    state = {
        "id": run.name,
        "project_id": "prj-abc123abc123",
        "stage": "plan",
        "repo_path": str(repo),
        "branch": "main",
        "commit": head(repo) if (repo / ".git").exists() else "",
        "commit_search": None,
        "manual_commits": [],
        "review": None,
        "approvals": [],
        "events": [],
        "exit_code": 0,
        "stop_reason": "",
        "requirement": {
            "id": "DSDD-123",
            "title": "确认退款",
            "source": "api",
            "body_file": "requirement.txt",
        },
        "allowed_databases": ["oms3_onlinepublish_biz"],
    }
    state.update(overrides)
    (run / "requirement.txt").write_text("确认退款\n", encoding="utf-8")
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return run


def load(run: Path) -> dict:
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def empty_review() -> dict:
    payload = {"requirement_id": "DSDD-123", "commits": []}
    return {
        "id": "a" * 32,
        "digest": review_digest("commits_empty", payload),
        "kind": "commits_empty",
        "step": "提交搜索",
        "title": "没有找到包含需求号的提交",
        "payload": payload,
        "requested_at": "2026-01-01T00:00:00+08:00",
    }


def valid_doc(sha: str, path: str) -> dict:
    return {
        "ui": {"seed_object_count": 1, "pages": []},
        "cases": [{
            "id": "C1",
            "title": "确认退款",
            "goal": "页面上能看到这一笔已经退款",
            "start": "",
            "expect_text": ["已退款"],
            "check_id": "K1",
            "pk_columns": ["detail_id"],
            "steps": [{
                "phase": "test",
                "action": "click",
                "role": "button",
                "name": "提交退款确认",
                "locator_status": "ready",
            }],
        }],
        "checks": [{
            "id": "K1",
            "kind": "table",
            "table": "refunds",
            "pk": ["detail_id"],
            "state_field": "status",
            "source": f"{path}:4",
        }],
        "open": [],
        "source": {
            "model": "replaced",
            "request_id": "replaced",
            "requirement_id": "WRONG",
            "commits": [sha],
            "files": ["secret.env"],
            "api_key": KEY,
        },
    }


def model_result(doc: dict) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-flash",
        "choices": [{"message": {"content": json.dumps(doc, ensure_ascii=False)}}],
    }


def test_word_regexp_does_not_match_longer_serial(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "README", "base\n", "base")
    long_sha = commit_file(repo, "Long.java", "class Long {}\n", "DSDD-1234")
    stuck = commit_file(repo, "Stuck.java", "class Stuck {}\n", "XDSDD-123")
    short_sha = commit_file(repo, JAVA_PATH, "class RefundService {}\n", "fix DSDD-123 now")
    before = snapshot(repo)
    commands = spy_git(monkeypatch)

    assert whole_word("DSDD-1234", "DSDD-123") is False
    assert whole_word("fix DSDD-123 now", "DSDD-123") is True
    assert whole_word("XDSDD-123", "DSDD-123") is False
    found = search_commits(str(repo), short_sha, "DSDD-123")
    longer = search_commits(str(repo), short_sha, "DSDD-1234")

    assert found == [short_sha]
    assert longer == [long_sha]
    assert stuck not in found
    assert exact_grep(repo, short_sha, "DSDD-123") in commands
    assert exact_grep(repo, short_sha, "DSDD-1234") in commands
    joined = commands[-1]
    assert any(arg == "--grep=DSDD-1234" for arg in joined)
    assert_queries_only(commands)
    assert_unchanged(repo, before)


def test_zero_commits_create_commits_empty_without_deepseek(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "README", "base\n", "base")
    before = snapshot(repo)
    commands = spy_git(monkeypatch)
    calls = []

    def complete(key, payload):
        calls.append(payload)
        raise AssertionError("零提交不应请求模型")

    run = write_run(tmp_path, repo)
    assert continue_plan(run, KEY, complete=complete) == 4
    state = load(run)
    assert calls == []
    assert state["commit_search"] == []
    assert state["review"]["kind"] == "commits_empty"
    assert state["review"]["payload"] == {"requirement_id": "DSDD-123", "commits": []}
    assert state["review"]["digest"] == review_digest("commits_empty", state["review"]["payload"])
    assert state["approvals"] == []
    assert state["exit_code"] == 4
    assert [item["basis"] for item in state["events"]].count("提交搜索结果为空，未请求模型") == 1
    assert not (run / "plan.json").exists()
    assert exact_grep(repo, state["commit"], "DSDD-123") in commands
    assert_queries_only(commands)
    assert_unchanged(repo, before)
    blob = (run / "state.json").read_text(encoding="utf-8")
    assert KEY not in blob


def test_review_still_set_does_not_start_model(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    sha = commit_file(repo, JAVA_PATH, "class RefundService {}\n", "fix DSDD-123 now")
    before = snapshot(repo)
    run = write_run(
        tmp_path, repo,
        commit=sha,
        commit_search=[],
        manual_commits=[sha],
        review=empty_review(),
        exit_code=4,
    )
    calls = []
    monkeypatch.setattr(
        "qa_platform.runner.main.continue_plan",
        lambda *args, **kwargs: calls.append("continue") or 0,
    )
    monkeypatch.setattr(
        "qa_platform.runner.planner.deepseek_complete",
        lambda *args, **kwargs: calls.append("deepseek"),
    )
    assert main(["--out", str(run)]) == 4
    assert continue_plan(run, KEY) == 4
    assert calls == []
    assert load(run)["manual_commits"] == [sha]
    assert load(run)["review"]["kind"] == "commits_empty"
    assert_unchanged(repo, before)


def test_manual_commits_starts_model_only_after_review_is_cleared(client, app, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    java = "package demo;\npublic class RefundService {\n    void confirm() {\n        save(detailId);\n    }\n}\n"
    sha = commit_file(repo, JAVA_PATH, java, "implement refund")
    git(repo, "checkout", "-b", "side")
    side = commit_file(repo, "side.txt", "side\n", "side only")
    git(repo, "checkout", "main")
    before = snapshot(repo)
    commands = spy_git(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    run = write_run(
        app.state.var_dir / "runs", repo,
        commit=sha,
        commit_search=[],
        manual_commits=[sha],
        review=empty_review(),
        exit_code=4,
        approvals=[{"id": "seen", "kind": "commits_empty", "digest": "old", "approved_at": "t"}],
    )
    raw = (run / "state.json").read_bytes()
    calls = []

    def forbid_model(*args, **kwargs):
        calls.append("early")
        raise AssertionError("review 仍是 commits_empty 时不能请求模型")

    monkeypatch.setattr("qa_platform.runner.planner.deepseek_complete", forbid_model)
    assert continue_plan(run, KEY) == 4
    assert calls == []
    assert (run / "state.json").read_bytes() == raw

    launched = []

    def fake_launch(slot, run_dir, project_id):
        handle = (run_dir / ".run.lock").open("a+")
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        launched.append((json.loads((run_dir / "state.json").read_text(encoding="utf-8")), project_id))

    monkeypatch.setattr("qa_platform.api.launch", fake_launch)
    headers = auth(app)
    url = "/api/runs/run-abc123abc123/manual-commits"

    (run / "plan.json").write_text("{}\n", encoding="utf-8")
    assert client.post(url, headers=headers, json={"shas": [sha]}).status_code == 409
    (run / "plan.json").unlink()
    assert (run / "state.json").read_bytes() == raw

    blocked = load(run)
    blocked["review"] = {**blocked["review"], "kind": "plan"}
    (run / "state.json").write_text(json.dumps(blocked, ensure_ascii=False), encoding="utf-8")
    assert client.post(url, headers=headers, json={"shas": [sha]}).status_code == 409
    (run / "state.json").write_bytes(raw)

    assert client.post(url, headers=headers, json={"shas": [side]}).status_code == 400
    assert (run / "state.json").read_bytes() == raw
    assert launched == []
    assert calls == []

    started = client.post(url, headers=headers, json={"shas": [sha[:12]]})
    assert started.status_code == 202
    assert started.json()["run"] == run.name
    assert len(launched) == 1
    disk, project_id = launched[0]
    assert disk["review"] is None
    assert disk["manual_commits"] == [sha]
    assert disk["approvals"] == [{"id": "seen", "kind": "commits_empty", "digest": "old", "approved_at": "t"}]
    assert project_id == "prj-abc123abc123"
    assert calls == []
    verify = [arg for args in commands for arg in args if arg.endswith("^{commit}")]
    assert any(arg == f"{sha[:12]}^{{commit}}" for arg in verify)

    def fake_complete(key, payload):
        calls.append((key, payload))
        doc = valid_doc(sha, JAVA_PATH)
        return model_result(doc)

    monkeypatch.setattr("qa_platform.runner.planner.deepseek_complete", fake_complete)
    monkeypatch.setattr("qa_platform.runner.main.execute", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("preflight")))
    monkeypatch.setattr("qa_platform.runner.main.fetch_requirement", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("yunxiao")))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{KEY}\nyunxiao-token\n"))
    assert main(["--out", str(run)]) == 4
    assert len(calls) == 1
    sent_key, payload = calls[0]
    assert sent_key == KEY
    assert KEY not in json.dumps(payload, ensure_ascii=False)
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 8000
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert "cases 必须是 count 和 notice" not in payload["messages"][0]["content"]
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert plan["source"] == {
        "model": "deepseek-flash",
        "request_id": "chatcmpl-1",
        "requirement_id": "DSDD-123",
        "commits": [sha],
        "files": [JAVA_PATH],
    }
    assert plan["cases"][0]["steps"][0]["effect"] == "read"
    state = load(run)
    assert state["review"]["kind"] == "plan"
    assert state["review"]["digest"] == review_digest("plan", plan)
    assert [item["basis"] for item in state["events"]].count("方案已通过校验，等待确认") == 1
    assert len(state["approvals"]) == 1
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (run / "plan.json", run / "state.json", run / "执行进度.md")
    )
    assert KEY not in text
    assert "yunxiao-token" not in text
    assert_queries_only(commands)
    assert_unchanged(repo, before)


def test_manual_commits_rejects_when_search_was_not_empty(client, app, tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    sha = commit_file(repo, "README", "base\n", "base")
    run = write_run(app.state.var_dir / "runs", repo, commit=sha, commit_search=None, review=None)
    raw = (run / "state.json").read_bytes()
    response = client.post(
        "/api/runs/run-abc123abc123/manual-commits",
        headers=auth(app),
        json={"shas": [sha]},
    )
    assert response.status_code == 409
    assert (run / "state.json").read_bytes() == raw

    busy = app.state.executor
    busy.process = type("Alive", (), {"poll": lambda self: None})()
    busy.run_id = "run-ffffffffffffffff"
    state = load(run)
    state["commit_search"] = []
    state["review"] = empty_review()
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    raw = (run / "state.json").read_bytes()
    response = client.post(
        "/api/runs/run-abc123abc123/manual-commits",
        headers=auth(app),
        json={"shas": [sha]},
    )
    assert response.status_code == 409
    assert (run / "state.json").read_bytes() == raw


def test_validate_plan_rejects_demo_decision_and_extra_sha(tmp_path):
    demo = {"cases": [{"id": "count", "expected": [0, 1, 1]}, {"id": "notice", "expected": "该明细已退款"}]}
    assert validate_plan(demo)
    assert any("缺少 ui" in item for item in validate_plan(demo))

    sha = "a" * 40
    path = JAVA_PATH
    only = valid_doc(sha, path)
    only["source"] = {"commits": [sha]}
    second = json.loads(json.dumps(only["cases"][0]))
    only["cases"][0]["id"] = "count"
    second["id"] = "notice"
    only["cases"].append(second)
    demo_errors = validate_plan(only, allowed_commits={sha}, material_files={path})
    assert any("count" in item and "notice" in item for item in demo_errors)

    decided = valid_doc(sha, path)
    decided["open"] = [{"id": "O1", "topic": "文案", "needs_decision": True, "decision": "按这个做"}]
    decision_errors = validate_plan(decided, allowed_commits={sha}, material_files={path})
    assert any("decision" in item for item in decision_errors)

    extra = valid_doc(sha, path)
    extra["source"]["commits"] = [sha, "b" * 40]
    extra_errors = validate_plan(extra, allowed_commits={sha}, material_files={path})
    assert any("多出" in item for item in extra_errors)

    counted = valid_doc(sha, path)
    counted["ui"]["seed_object_count"] = True
    assert any("seed_object_count" in item for item in validate_plan(counted, allowed_commits={sha}, material_files={path}))
    counted["ui"]["seed_object_count"] = "1"
    assert any("seed_object_count" in item for item in validate_plan(counted, allowed_commits={sha}, material_files={path}))

    for label, doc in (("demo", demo), ("decision", decided), ("extra", extra)):
        run = tmp_path / label
        run.mkdir()
        (run / "state.json").write_text(json.dumps({
            "requirement": {"id": "DSDD-123", "body_file": "requirement.txt"},
            "allowed_databases": ["oms3_onlinepublish_biz"],
            "events": [],
        }), encoding="utf-8")
        (run / "requirement.txt").write_text("确认退款\n", encoding="utf-8")
        code = propose(
            run, key=KEY, shas=[sha], files=[path], materials="class RefundService {}",
            complete=lambda key, payload, doc=doc: model_result(doc),
        )
        assert code == 2
        assert not (run / "plan.json").exists()
        saved = json.loads((run / "state.json").read_text(encoding="utf-8"))
        assert saved.get("review") in (None, {})
        assert saved["exit_code"] == 2
        assert KEY not in (run / "state.json").read_text(encoding="utf-8")


def test_propose_sends_the_requirement_screenshot(tmp_path):
    sha = "c" * 40
    path = JAVA_PATH
    seen = []

    pictured = valid_doc(sha, path)
    pictured["ui"]["pages"] = [{"title_text": "退款页", "path": ""}]
    pictured["ui"]["surfaces"] = []
    pictured["cases"][0]["start"] = "退款页"

    def complete(key, payload):
        seen.append(payload)
        return model_result(pictured)

    run = tmp_path / "vision"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "requirement": {"id": "DSDD-123", "body_file": "requirement.txt"},
        "allowed_databases": ["oms3_onlinepublish_biz"],
        "events": [],
    }), encoding="utf-8")
    (run / "requirement.txt").write_text("确认退款\n", encoding="utf-8")
    image = run / "images" / "requirement.png"
    image.parent.mkdir()
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ))
    assert propose(run, key=KEY, shas=[sha], files=[path], materials="class RefundService {}", complete=complete) == 4
    content = seen[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["image_url"]["detail"] == "high"
    assert "识" not in json.dumps(load(run)["events"])


def test_propose_rewrites_once_when_the_plan_fails_validation(tmp_path):
    sha = "c" * 40
    path = JAVA_PATH
    bad = valid_doc(sha, path)
    bad["checks"][0]["kind"] = "api"
    bad["cases"][0]["pk_columns"] = ["other_id"]
    good = valid_doc(sha, path)
    calls = []

    def complete(key, payload):
        calls.append(payload)
        return model_result(bad if len(calls) == 1 else good)

    run = tmp_path / "retry"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "requirement": {"id": "DSDD-123", "body_file": "requirement.txt"},
        "allowed_databases": ["oms3_onlinepublish_biz"],
        "events": [],
    }), encoding="utf-8")
    (run / "requirement.txt").write_text("确认退款\n", encoding="utf-8")
    assert propose(run, key=KEY, shas=[sha], files=[path], materials="class RefundService {}", complete=complete) == 4
    assert len(calls) == 2
    correction = calls[1]["messages"][-1]["content"]
    assert "checks.kind 只能是 table" in correction
    assert "pk_columns" in correction
    assert json.loads((run / "plan.json").read_text(encoding="utf-8"))["checks"][0]["kind"] == "table"
    bases = [item["basis"] for item in load(run)["events"]]
    assert "方案有问题，正在按校验结果重写" in bases
    assert "方案已通过校验，等待确认" in bases


def test_validate_plan_field_rules():
    sha = "c" * 40
    path = JAVA_PATH
    doc = valid_doc(sha, path)
    assert validate_plan(doc, allowed_commits={sha}, material_files={path}) == []

    pending = valid_doc(sha, path)
    pending["cases"][0]["steps"][0]["locator_status"] = "pending"
    pending["cases"][0]["steps"][0]["name"] = ""
    assert validate_plan(pending, allowed_commits={sha}, material_files={path}) == []

    ready = valid_doc(sha, path)
    ready["cases"][0]["steps"][0]["name"] = "  "
    assert validate_plan(ready, allowed_commits={sha}, material_files={path})

    click = valid_doc(sha, path)
    click["cases"][0]["steps"][0]["value"] = "x"
    assert any("click" in item for item in validate_plan(click, allowed_commits={sha}, material_files={path}))

    typed = valid_doc(sha, path)
    typed["cases"][0]["steps"][0] = {
        "role": "textbox", "name": "备注", "locator_status": "ready",
        "action": "type", "phase": "seed",
    }
    assert any("value" in item for item in validate_plan(typed, allowed_commits={sha}, material_files={path}))

    chosen = valid_doc(sha, path)
    chosen["cases"][0]["steps"][0] = {
        "role": "combobox", "name": "状态", "locator_status": "ready",
        "action": "select", "phase": "test", "option": "已退款",
    }
    assert validate_plan(chosen, allowed_commits={sha}, material_files={path}) == []

    flag = valid_doc(sha, path)
    flag["cases"][0]["steps"][0] = {
        "role": "checkbox", "name": "同意", "locator_status": "ready",
        "action": "set", "phase": "test", "checked": 1,
    }
    assert any("checked" in item for item in validate_plan(flag, allowed_commits={sha}, material_files={path}))
    flag["cases"][0]["steps"][0]["checked"] = True
    assert validate_plan(flag, allowed_commits={sha}, material_files={path}) == []

    api = valid_doc(sha, path)
    api["checks"][0]["kind"] = "api"
    assert any("table" in item for item in validate_plan(api, allowed_commits={sha}, material_files={path}))
    api["checks"][0]["kind"] = "state"
    assert any("table" in item for item in validate_plan(api, allowed_commits={sha}, material_files={path}))

    other = valid_doc(sha, path)
    other["cases"][0]["pk_columns"] = ["other_id"]
    assert any("pk_columns" in item for item in validate_plan(other, allowed_commits={sha}, material_files={path}))

    database = valid_doc(sha, path)
    database["checks"][0]["database"] = "not_a_project_db"
    assert any("database" in item for item in validate_plan(
        database, allowed_commits={sha}, material_files={path}, allowed_databases=["oms3_onlinepublish_biz"],
    ))
    database["checks"][0]["database"] = "oms3_onlinepublish_biz"
    assert validate_plan(
        database, allowed_commits={sha}, material_files={path}, allowed_databases=["oms3_onlinepublish_biz"],
    ) == []

    missing_goal = valid_doc(sha, path)
    del missing_goal["cases"][0]["goal"]
    assert any("goal" in item for item in validate_plan(missing_goal, allowed_commits={sha}, material_files={path}))

    missing_start = valid_doc(sha, path)
    del missing_start["cases"][0]["start"]
    assert any("start" in item for item in validate_plan(missing_start, allowed_commits={sha}, material_files={path}))
    missing_start["cases"][0]["start"] = ""
    assert validate_plan(missing_start, allowed_commits={sha}, material_files={path}) == []

    missing_expect = valid_doc(sha, path)
    missing_expect["cases"][0]["expect_text"] = []
    assert any("expect_text" in item for item in validate_plan(missing_expect, allowed_commits={sha}, material_files={path}))

    named = valid_doc(sha, path)
    named["cases"][0]["expect_text"] = ["提交退款确认", "已退款"]
    errors = validate_plan(named, allowed_commits={sha}, material_files={path})
    assert any("控件名" in item and "提交退款确认" in item for item in errors)

    example = valid_doc(sha, path)
    example["cases"][0]["expect_text"] = ["查询完成", "共 0 条"]
    errors = validate_plan(example, allowed_commits={sha}, material_files={path})
    assert any("示例结果" in item and "共 0 条" in item for item in errors)
    kept = validate_plan(
        example,
        allowed_commits={sha},
        material_files={path},
        requirement="页面应出现查询完成，并且共 0 条。",
    )
    assert kept == []

    counted = valid_doc(sha, path)
    counted["cases"][0]["expect_text"] = ["结果条数"]
    assert validate_plan(counted, allowed_commits={sha}, material_files={path}) == []
    counted["cases"][0]["expect_text"] = ["共 1 条"]
    assert validate_plan(counted, allowed_commits={sha}, material_files={path}) == []

    biased = valid_doc(sha, path)
    biased["cases"][0]["expect_text"] = ["结果条数"]
    biased["cases"][0]["goal"] = "列表只剩这些输入对应的行"
    errors = validate_plan(biased, allowed_commits={sha}, material_files={path})
    assert any("条数" in item and "只剩" in item for item in errors)

    pictured = valid_doc(sha, path)
    pictured["ui"]["pages"] = [{"title_text": "结算页", "path": ""}]
    pictured["ui"]["surfaces"] = ["甲页签", "乙页签"]
    pictured["cases"][0]["start"] = "甲页签"
    pictured["cases"][0]["goal"] = "把准备的输入填进条件，做查询，读页面上的结果条数。"
    pictured["cases"][0]["expect_text"] = ["结果条数"]
    errors = validate_plan(pictured, allowed_commits={sha}, material_files={path}, has_images=True)
    assert any("不要只写页签" in item for item in errors)
    pictured["cases"][0]["start"] = "结算页 - 甲页签"
    pictured["open"] = [{"id": "open-1", "question": "乙页签在材料里对不上表，要不要测？", "needs_decision": True, "decision": ""}]
    assert validate_plan(pictured, allowed_commits={sha}, material_files={path}, has_images=True) == []
    assert validate_plan(valid_doc(sha, path), allowed_commits={sha}, material_files={path}, has_images=False) == []


def _bound_page(source: str, table: str, tab: str = "有赞账单", route: str = "/bill/platformBill") -> dict:
    return {
        "tab": tab,
        "route": route,
        "mapping": "/bill/youZan",
        "class_name": "YouZanBillController",
        "table": table,
        "source": source,
        "filters": ["交易号"],
    }


def test_page_index_rejects_a_tab_bound_to_another_controller():
    sha = "c" * 40
    path = JAVA_PATH
    youzan = "app/YouZanBillController.java:4"
    page = _bound_page(youzan, "bfrs_you_zan_bill")
    wrong = valid_doc(sha, path)
    wrong["ui"]["pages"] = [{"title_text": "结算页", "path": ""}]
    wrong["ui"]["surfaces"] = ["有赞账单", "小红书账单"]
    wrong["cases"][0]["start"] = "结算页 - 有赞账单"
    wrong["cases"][0]["goal"] = "把准备的输入填进条件，做查询，读页面上的结果条数。"
    wrong["cases"][0]["expect_text"] = ["结果条数"]
    wrong["checks"][0]["source"] = "app/ShopifyBillController.java:2"
    wrong["checks"][0]["table"] = "shopify_bill"
    wrong["open"] = [{
        "id": "open-1",
        "question": "小红书账单对不上索引，要不要测？",
        "needs_decision": True,
        "decision": "",
    }]
    errors = validate_plan(
        wrong, allowed_commits={sha}, material_files={path}, has_images=True, page_index=[page],
    )
    assert any("YouZanBillController" in item and "ShopifyBillController.java" in item for item in errors)
    assert any("bfrs_you_zan_bill" in item for item in errors)

    wrong["checks"][0]["source"] = youzan
    wrong["checks"][0]["table"] = "you_zan_bill"
    assert validate_plan(
        wrong, allowed_commits={sha}, material_files={path}, has_images=True, page_index=[page],
    ) == []

    parked = valid_doc(sha, path)
    parked["ui"]["pages"] = [{"title_text": "结算页", "path": ""}]
    parked["ui"]["surfaces"] = ["有赞账单"]
    parked["cases"][0]["start"] = "结算页"
    parked["open"] = [{
        "id": "open-1",
        "question": "有赞账单这次测不测？",
        "needs_decision": True,
        "decision": "",
    }]
    errors = validate_plan(
        parked, allowed_commits={sha}, material_files={path}, has_images=True, page_index=[page],
    )
    assert any("要写成用例" in item and "有赞账单" in item for item in errors)


def test_start_keeps_symbols_inside_the_page_and_the_tab():
    sha = "c" * 40
    path = JAVA_PATH
    source = "app/RangeController.java:8"
    tab = "范围-报告"
    shorter = "报告"
    missing = "还没有的页签"
    page = _bound_page(source, "range_report", tab)
    doc = valid_doc(sha, path)
    doc["ui"]["pages"] = [{"title_text": "结算-页", "path": ""}]
    doc["ui"]["surfaces"] = [tab, shorter, missing]
    doc["cases"][0]["start"] = "「结算-页 - 范围-报告」"
    doc["cases"][0]["goal"] = "把准备的输入填进条件，做查询，读页面上的结果条数。"
    doc["cases"][0]["expect_text"] = ["结果条数"]
    doc["checks"][0]["source"] = source
    doc["checks"][0]["table"] = "range_report"
    doc["open"] = [
        {"id": "open-short", "question": "报告这次不单独测。", "needs_decision": True, "decision": ""},
        {"id": "open-missing", "question": "还没有的页签对不上索引，要不要测？", "needs_decision": True, "decision": ""},
    ]
    assert validate_plan(
        doc, allowed_commits={sha}, material_files={path}, has_images=True, page_index=[page],
    ) == []

    doc["cases"][0]["start"] = tab
    errors = validate_plan(
        doc, allowed_commits={sha}, material_files={path}, has_images=True, page_index=[page],
    )
    assert any("不要只写页签" in item for item in errors)
    assert any("要写成用例" in item and tab in item for item in errors)


def test_missing_pk_columns_and_locator_status_are_filled():
    sha = "c" * 40
    path = JAVA_PATH
    doc = valid_doc(sha, path)
    del doc["cases"][0]["pk_columns"]
    del doc["cases"][0]["steps"][0]["locator_status"]
    assert validate_plan(doc, allowed_commits={sha}, material_files={path}) == []
    assert doc["cases"][0]["pk_columns"] == ["detail_id"]
    assert doc["cases"][0]["steps"][0]["locator_status"] == "ready"

    wrong = valid_doc(sha, path)
    wrong["cases"][0]["pk_columns"] = ["other_id"]
    errors = validate_plan(wrong, allowed_commits={sha}, material_files={path})
    assert any("pk_columns" in item for item in errors)
    assert wrong["cases"][0]["pk_columns"] == ["other_id"]

    nameless = valid_doc(sha, path)
    del nameless["cases"][0]["steps"][0]["name"]
    del nameless["cases"][0]["steps"][0]["locator_status"]
    errors = validate_plan(nameless, allowed_commits={sha}, material_files={path})
    assert any("缺少 name" in item for item in errors)
    assert nameless["cases"][0]["steps"][0].get("locator_status") in (None, "")


def test_missing_check_kind_is_filled_with_table():
    sha = "c" * 40
    path = JAVA_PATH
    missing = valid_doc(sha, path)
    del missing["checks"][0]["kind"]
    assert validate_plan(missing, allowed_commits={sha}, material_files={path}) == []
    assert missing["checks"][0]["kind"] == "table"

    blank = valid_doc(sha, path)
    blank["checks"][0]["kind"] = "  "
    assert validate_plan(blank, allowed_commits={sha}, material_files={path}) == []
    assert blank["checks"][0]["kind"] == "table"

    api = valid_doc(sha, path)
    api["checks"][0]["kind"] = "api"
    errors = validate_plan(api, allowed_commits={sha}, material_files={path})
    assert any("api" in item and "table" in item for item in errors)
    assert api["checks"][0]["kind"] == "api"


def _case_for(doc, case_id, check_id, start, source, table):
    case = json.loads(json.dumps(doc["cases"][0]))
    case["id"] = case_id
    case["check_id"] = check_id
    case["start"] = start
    check = json.loads(json.dumps(doc["checks"][0]))
    check["id"] = check_id
    check["source"] = source
    check["table"] = table
    return case, check


def test_same_route_tabs_stay_even_when_the_screenshot_omits_them():
    sha = "c" * 40
    path = JAVA_PATH
    shown = "甲页签"
    hidden = "范围-报告"
    other = "别的页面"
    pages = [
        _bound_page("app/ShownController.java:2", "shown_bill", shown),
        _bound_page("app/RangeController.java:8", "range_report", hidden),
        _bound_page("app/OtherController.java:3", "other_bill", other, route="/other"),
    ]
    doc = valid_doc(sha, path)
    doc["ui"]["pages"] = [{"title_text": "结算-页", "path": ""}]
    doc["ui"]["surfaces"] = [shown, "还没有的页签"]
    doc["cases"][0]["start"] = "结算-页 - 甲页签"
    doc["cases"][0]["goal"] = "把准备的输入填进条件，做查询，读页面上的结果条数。"
    doc["cases"][0]["expect_text"] = ["结果条数"]
    doc["checks"][0]["source"] = "app/ShownController.java:2"
    doc["checks"][0]["table"] = "shown_bill"
    extra_case, extra_check = _case_for(
        doc, "C2", "K2", "结算-页 - 范围-报告", "app/RangeController.java:8", "range_report",
    )
    doc["cases"].append(extra_case)
    doc["checks"].append(extra_check)
    doc["open"] = [{
        "id": "open-missing",
        "question": "还没有的页签代码里没有同名页，要不要测？",
        "needs_decision": True,
        "decision": "",
    }]
    assert validate_plan(
        doc, allowed_commits={sha}, material_files={path}, has_images=True, page_index=pages,
    ) == []

    doc["cases"].pop()
    doc["checks"].pop()
    errors = validate_plan(
        doc, allowed_commits={sha}, material_files={path}, has_images=True, page_index=pages,
    )
    assert any("要写成用例" in item and hidden in item and "range_report" in item for item in errors)
    assert not any("删掉这条用例" in item for item in errors)
    assert not any("不要编用例" in item and hidden in item for item in errors)

    stray_case, stray_check = _case_for(
        doc, "C3", "K3", "结算-页 - 别的页面", "app/OtherController.java:3", "other_bill",
    )
    doc["cases"].append(stray_case)
    doc["checks"].append(stray_check)
    errors = validate_plan(
        doc, allowed_commits={sha}, material_files={path}, has_images=True, page_index=pages,
    )
    assert any("不在这一页的代码里" in item and other in item for item in errors)

    pictured = valid_doc(sha, path)
    pictured["ui"]["pages"] = [{"title_text": "结算-页", "path": ""}]
    pictured["ui"]["surfaces"] = [hidden]
    pictured["cases"][0]["start"] = hidden
    pictured["cases"][0]["goal"] = "把准备的输入填进条件，做查询，读页面上的结果条数。"
    pictured["cases"][0]["expect_text"] = ["结果条数"]
    pictured["checks"][0]["source"] = "app/RangeController.java:8"
    pictured["checks"][0]["table"] = "range_report"
    errors = validate_plan(
        pictured, allowed_commits={sha}, material_files={path}, has_images=True, page_index=pages[:2],
    )
    assert any("不要只写页签" in item for item in errors)
    assert not any("不在这一页的代码里" in item for item in errors)


def test_propose_sends_the_page_index_and_fills_the_front_route(tmp_path):
    sha = "c" * 40
    path = JAVA_PATH
    youzan = "app/YouZanBillController.java:4"
    page = _bound_page(youzan, "bfrs_you_zan_bill")
    seen = []
    doc = valid_doc(sha, path)
    doc["ui"]["pages"] = [{"title_text": "结算页", "path": ""}]
    doc["ui"]["surfaces"] = ["有赞账单"]
    doc["cases"][0]["start"] = "结算页 - 有赞账单"
    doc["checks"][0]["source"] = youzan
    doc["checks"][0]["table"] = "bfrs_you_zan_bill"

    def complete(key, payload):
        seen.append(payload)
        return model_result(doc)

    run = tmp_path / "indexed"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "requirement": {"id": "DSDD-123", "body_file": "requirement.txt"},
        "allowed_databases": ["oms3_onlinepublish_biz"],
        "events": [],
    }), encoding="utf-8")
    (run / "requirement.txt").write_text("确认退款\n", encoding="utf-8")
    assert propose(
        run, key=KEY, shas=[sha], files=[path], materials="class RefundService {}",
        complete=complete, pages=[page],
    ) == 4
    body = json.loads(seen[0]["messages"][1]["content"])
    assert body["pages"][0]["tab"] == "有赞账单"
    assert body["pages"][0]["source"] == youzan
    saved = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    assert saved["ui"]["pages"][0]["path"] == "/bill/platformBill"
    assert any("前端路由" in item["basis"] for item in load(run)["events"])


def test_materials_keep_backend_hunks_and_frontend_text(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    lines = [f"line {index} MARKER_{index}" for index in range(40)]
    lines[20] = "changed confirmRefund"
    java = "\n".join(lines) + "\n"
    vue = (
        "<template>\n  <button>提交退款确认</button>\n</template>\n"
        "<script>\nconst secret = \"internal-flag\"\nexport default { setup() { return {} } }\n</script>\n"
    )
    xml = "<mapper>\n  <select id=\"findRefund\">select 1</select>\n</mapper>\n"
    (repo / JAVA_PATH).parent.mkdir(parents=True, exist_ok=True)
    (repo / JAVA_PATH).write_text(java, encoding="utf-8")
    (repo / "web/RefundView.vue").parent.mkdir(parents=True, exist_ok=True)
    (repo / "web/RefundView.vue").write_text(vue, encoding="utf-8")
    (repo / "mapper/RefundMapper.xml").parent.mkdir(parents=True, exist_ok=True)
    (repo / "mapper/RefundMapper.xml").write_text(xml, encoding="utf-8")
    (repo / "package-lock.json").write_text("UNIQUE_LOCK_BODY\n", encoding="utf-8")
    node = repo / "web/node_modules/pkg/index.js"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("UNIQUE_NODE_MODULES\n", encoding="utf-8")
    (repo / "web/app.min.js").write_text("UNIQUE_MIN\n", encoding="utf-8")
    (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "frontend and locks")
    sha = head(repo)
    before = snapshot(repo)
    commands = spy_git(monkeypatch)
    text, files, dropped = collect_materials(str(repo), [sha])
    assert dropped == []
    assert JAVA_PATH in files
    assert "web/RefundView.vue" in files
    assert "mapper/RefundMapper.xml" in files
    assert "package-lock.json" not in files
    assert "MARKER_0" in text
    assert "changed confirmRefund" in text
    assert "findRefund" in text
    assert "提交退款确认" in text
    assert "internal-flag" in text
    assert "export default" not in text
    assert "UNIQUE_LOCK_BODY" not in text
    assert "UNIQUE_NODE_MODULES" not in text
    assert "UNIQUE_MIN" not in text
    assert any("-U30" in arg for args in commands for arg in args)
    assert_queries_only(commands)
    assert_unchanged(repo, before)


def test_material_budget_stops_without_deepseek(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    commit_file(repo, "big.py", "x" * 120_000 + "\n", "DSDD-123 huge")
    before = snapshot(repo)
    commands = spy_git(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "qa_platform.runner.planner.deepseek_complete",
        lambda *args, **kwargs: calls.append("deepseek"),
    )
    run = write_run(tmp_path, repo)
    assert continue_plan(run, KEY) == 2
    state = load(run)
    assert calls == []
    assert not (run / "plan.json").exists()
    assert state.get("review") in (None, {})
    assert "big.py" in state["events"][-1]["basis"]
    assert "100000" in state["events"][-1]["basis"]
    assert_queries_only(commands)
    assert_unchanged(repo, before)


def test_git_query_rejects_mutations(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError(args)

    monkeypatch.setattr("qa_platform.runner.commits.subprocess.run", fail)
    for name in FORBIDDEN:
        with pytest.raises(GitDenied):
            git_query("/tmp/unused-repo", name)
    with pytest.raises(GitDenied):
        git_query("/tmp/unused-repo", "log", "--all")


def test_review_digest_keeps_unicode():
    payload = {"requirement_id": "需求"}
    encoded = json.dumps({"kind": "commits_empty", "payload": payload}, sort_keys=True).encode()
    escaped = hashlib.sha256(encoded).hexdigest()
    assert review_digest("commits_empty", payload) != escaped


def test_deepseek_request_uses_json_object_and_ninety_seconds(monkeypatch):
    seen = {}

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return b'{"id":"chatcmpl-1","model":"deepseek-flash","choices":[]}'

    def urlopen(request, timeout):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["auth"] = request.get_header("Authorization")
        return Fake()

    monkeypatch.setattr("qa_platform.runner.planner.urllib.request.urlopen", urlopen)
    deepseek_complete(KEY, {"model": "deepseek-flash", "max_tokens": 8000})
    assert seen["timeout"] == 90
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(seen["body"])
