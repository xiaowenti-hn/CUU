import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qa_platform.api import create_app


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("YUNXIAO_TOKEN", raising=False)


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><meta name="qa-token" content="@TOKEN@">\n',
        encoding="utf-8",
    )
    (root / "assets" / "app.js").write_text("/* app */\n", encoding="utf-8")
    (root / "favicon.ico").write_bytes(b"ico")
    return root


@pytest.fixture
def app(tmp_path, dist):
    return create_app(tmp_path / "var", dist)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8876") as test_client:
        yield test_client


@pytest.fixture
def var_dir(app):
    return app.state.var_dir


def auth(app, **extra):
    headers = {
        "X-QA-Token": app.state.token,
        "Origin": "http://127.0.0.1:8876",
    }
    headers.update(extra)
    return headers


def git_repo(path: Path) -> Path:
    path.mkdir()
    env = os.environ.copy()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "QA"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)
    return path


def complete_body(repo: Path) -> dict:
    return {
        "name": "退款验收",
        "repo_path": str(repo),
        "branch": "main",
        "git_remote": "origin",
        "yunxiao_entry": "https://devops.aliyun.com/projex/req/1",
        "yunxiao_organization_id": "org-1",
        "yunxiao_edition": "center",
        "yunxiao_browser_app": "",
        "base_url": "http://127.0.0.1:9000",
        "allowed_hosts": ["127.0.0.1"],
        "allowed_databases": ["oms3_onlinepublish_biz"],
    }
