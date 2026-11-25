import json
import subprocess
import time

from qa_platform.validate import token_configured
from tests.conftest import auth, complete_body, git_repo

FORBIDDEN = {"fetch", "pull", "checkout", "merge", "reset", "clean", "stash", "switch", "clone", "rebase"}


def codes(body):
    return [item["code"] for item in body["errors"]]


def test_token_file_must_be_0600_and_nonempty(tmp_path, monkeypatch):
    monkeypatch.delenv("YUNXIAO_TOKEN", raising=False)
    assert token_configured() is False
    folder = tmp_path / "home" / ".grok" / "qa-platform"
    folder.mkdir(parents=True)
    secret = folder / "yunxiao-token"
    secret.write_text("secret-token\n", encoding="utf-8")
    secret.chmod(0o644)
    assert token_configured() is False
    secret.chmod(0o600)
    assert token_configured() is True
    secret.write_text(" \n", encoding="utf-8")
    assert token_configured() is False


def test_create_keeps_default_database_and_hides_token(client, app, var_dir, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    created = client.post("/api/projects", headers=auth(app), json={"name": "草稿"})
    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("prj-")
    assert body["allowed_databases"] == ["oms3_onlinepublish_biz"]
    assert body["git_remote"] == "origin"
    assert body["yunxiao_edition"] == "center"
    assert body["token_configured"] is True
    assert "secret-token" not in created.text
    stored = json.loads((var_dir / "projects" / body["id"] / "project.json").read_text())
    assert "token" not in stored
    assert "token_configured" not in stored
    assert "current_commit" not in stored
    assert "secret-token" not in json.dumps(stored)


def test_put_replaces_whole_record_and_rejects_secrets(client, app, var_dir):
    created = client.post("/api/projects", headers=auth(app), json={"name": "草稿", "base_url": "http://127.0.0.1:9"})
    project_id = created.json()["id"]
    rejected = client.put(
        f"/api/projects/{project_id}",
        headers=auth(app),
        json={"token": "secret-token", "name": "不要写"},
    )
    assert rejected.status_code == 400
    assert "secret-token" not in (var_dir / "projects" / project_id / "project.json").read_text()
    replaced = client.put(
        f"/api/projects/{project_id}",
        headers=auth(app),
        json={"name": "只剩名称"},
    )
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["name"] == "只剩名称"
    assert body["base_url"] == ""
    assert body["allowed_databases"] == []
    assert body["git_remote"] == "origin"
    assert body["id"] == project_id


def test_front_repo_is_optional_and_must_contain_src(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    created = client.post("/api/projects", headers=auth(app), json={})
    project_id = created.json()["id"]
    assert created.json()["front_repo_path"] == ""
    body = complete_body(repo)
    saved = client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    assert saved.status_code == 200
    assert saved.json()["front_repo_path"] == ""
    checked = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "front_repo_path" not in codes(checked)

    front = tmp_path / "custom-front" / "src"
    front.mkdir(parents=True)
    body["front_repo_path"] = str(front.parent)
    client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    ready = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "front_repo_path" not in codes(ready)

    body["front_repo_path"] = str(tmp_path / "missing-front")
    client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    broken = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "front_repo_path" in codes(broken)


def test_center_requires_org_and_host_mismatch_is_an_error(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    created = client.post("/api/projects", headers=auth(app), json={})
    project_id = created.json()["id"]
    body = complete_body(repo)
    body["yunxiao_organization_id"] = ""
    saved = client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    assert saved.status_code == 200
    checked = client.post(f"/api/projects/{project_id}/validate", headers=auth(app))
    assert checked.status_code == 200
    payload = checked.json()
    assert payload["ok"] is False
    assert "yunxiao_organization_id" in payload["missing"]

    body["yunxiao_edition"] = "region"
    client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    region = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "yunxiao_organization_id" not in region["missing"]
    assert region["ok"] is True

    body["yunxiao_edition"] = "center"
    body["yunxiao_organization_id"] = "org-1"
    body["allowed_hosts"] = ["example.com"]
    client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    mismatch = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert mismatch["ok"] is False
    assert "base_url" not in mismatch["missing"]
    assert "base_url_host" in codes(mismatch)


def test_missing_config_does_not_create_a_run(client, app, var_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    created = client.post("/api/projects", headers=auth(app), json=complete_body(repo))
    project_id = created.json()["id"]
    client.put(
        f"/api/projects/{project_id}",
        headers=auth(app),
        json={**complete_body(repo), "base_url": "", "yunxiao_entry": ""},
    )
    before = list((var_dir / "runs").glob("*")) if (var_dir / "runs").exists() else []
    refused = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert refused.status_code == 409
    assert "run" not in refused.json()
    assert "base_url" in refused.json()["missing"]
    assert "yunxiao_entry" in refused.json()["missing"]
    after = list((var_dir / "runs").glob("*")) if (var_dir / "runs").exists() else []
    assert after == before


def test_complete_config_creates_run_without_fetch(client, app, var_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
    commands = []
    real = subprocess.run

    def spy(args, **kwargs):
        commands.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr("qa_platform.validate.subprocess.run", spy)
    created = client.post("/api/projects", headers=auth(app), json=complete_body(repo))
    project_id = created.json()["id"]
    shown = client.get(f"/api/projects/{project_id}", headers=auth(app)).json()
    assert shown["current_branch"] == "main"
    assert shown["current_commit"]
    assert shown["git_error"] is None
    checked = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert checked["ok"] is True
    assert checked["dirty"] is True
    started = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert started.status_code == 202
    run_id = started.json()["run"]
    assert run_id.startswith("run-")
    state_path = var_dir / "runs" / run_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["repo_path"] == str(repo.resolve())
    assert state["branch"] == "main"
    assert state["base_url"] == "http://127.0.0.1:9000"
    assert state["allowed_hosts"] == ["127.0.0.1"]
    assert state["allowed_databases"] == ["oms3_onlinepublish_biz"]
    assert "secret-token" not in state_path.read_text(encoding="utf-8")
    assert (repo / "notes.txt").is_file()
    for args in commands:
        assert FORBIDDEN.isdisjoint(args)
    detail = None
    for _ in range(100):
        detail = client.get(f"/api/runs/{run_id}", headers=auth(app)).json()
        if detail["status"] != "running":
            break
        time.sleep(0.05)
    assert detail["status"] == "blocked"
    assert detail["exit_code"] == 2
    assert detail["commit"] == ""
    assert detail["branch"] == "main"
    assert any("未执行 fetch" in item["basis"] for item in detail["events"])
    log = (var_dir / "runs" / run_id / "runner.log").read_text(encoding="utf-8")
    assert "secret-token" not in log


def test_no_token_requires_browser_app(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr("qa_platform.validate.ocu_available", lambda: False)
    repo = git_repo(tmp_path / "repo")
    created = client.post("/api/projects", headers=auth(app), json=complete_body(repo))
    project_id = created.json()["id"]
    missing = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "yunxiao_browser_app" in missing["missing"]
    refused = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert refused.status_code == 409
    assert not (app.state.var_dir / "runs").exists()

    body = complete_body(repo)
    body["yunxiao_browser_app"] = "Google Chrome"
    client.put(f"/api/projects/{project_id}", headers=auth(app), json=body)
    blocked = client.post(f"/api/projects/{project_id}/validate", headers=auth(app)).json()
    assert "yunxiao_browser_app" not in blocked["missing"]
    assert "ocu_unavailable" in codes(blocked)
    assert client.post(f"/api/projects/{project_id}/runs", headers=auth(app)).status_code == 409


def test_busy_executor_and_delete(client, app, var_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    created = client.post("/api/projects", headers=auth(app), json=complete_body(repo))
    project_id = created.json()["id"]
    first = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert first.status_code == 202
    run_id = first.json()["run"]

    class Alive:
        def poll(self):
            return None

    app.state.executor.process = Alive()
    app.state.executor.project_id = project_id
    app.state.executor.run_id = run_id
    again = client.post(f"/api/projects/{project_id}/runs", headers=auth(app))
    assert again.status_code == 409
    assert again.json()["error"] == "已有测试在运行，请等当前运行结束"
    assert "run" not in again.json()
    assert len(list((var_dir / "runs").iterdir())) == 1
    denied = client.delete(f"/api/projects/{project_id}", headers=auth(app))
    assert denied.status_code == 409
    assert (var_dir / "projects" / project_id / "project.json").is_file()

    app.state.executor.process = None
    removed = client.delete(f"/api/projects/{project_id}", headers=auth(app))
    assert removed.status_code == 204
    assert not (var_dir / "projects" / project_id / "project.json").exists()
    assert (var_dir / "runs" / run_id / "state.json").is_file()


def test_bad_base_url_and_database_are_errors(client, app, tmp_path, monkeypatch):
    monkeypatch.setenv("YUNXIAO_TOKEN", "secret-token")
    repo = git_repo(tmp_path / "repo")
    body = complete_body(repo)
    body["base_url"] = "http://127.0.0.1:9000/order"
    body["allowed_databases"] = ["oms-db"]
    created = client.post("/api/projects", headers=auth(app), json=body)
    payload = client.post(f"/api/projects/{created.json()['id']}/validate", headers=auth(app)).json()
    assert "base_url" not in payload["missing"]
    assert "base_url" in codes(payload)
    assert "allowed_databases" in codes(payload)
    assert client.post(f"/api/projects/{created.json()['id']}/runs", headers=auth(app)).status_code == 409
