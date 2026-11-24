from pathlib import Path

from fastapi.testclient import TestClient

from qa_platform.api import create_app
from tests.conftest import auth


def test_pages_replace_token_and_assets_skip_it(client, app):
    for path in ("/", "/index.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert app.state.token in response.text
        assert "@TOKEN@" not in response.text
        assert "blob:" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "/* app */\n"
    assert "content-security-policy" not in {key.lower() for key in asset.headers}
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.content == b"ico"


def test_api_requires_token_and_write_requires_origin(client, app):
    assert client.get("/api/projects").status_code == 403
    assert client.get("/api/health").status_code == 403
    assert client.get("/api/runs/run-abcdefabcdef/report").status_code == 403
    assert client.get("/api/runs/run-abcdefabcdef/artifacts/state.json").status_code == 403
    wrong_origin = client.post(
        "/api/projects",
        headers={"X-QA-Token": app.state.token, "Origin": "http://127.0.0.1:5173"},
        json={},
    )
    assert wrong_origin.status_code == 403
    health = client.get("/api/health", headers=auth(app))
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["busy"] is False


def test_wrong_host_is_forbidden(app):
    with TestClient(app, base_url="http://localhost:8876") as other:
        response = other.get("/api/health", headers={"X-QA-Token": app.state.token})
    assert response.status_code == 403


def test_unknown_paths_do_not_fall_back_to_html(client, app):
    missing = client.get("/runs/somewhere")
    assert missing.status_code == 404
    assert "qa-token" not in missing.text
    assert app.state.token not in missing.text
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/assets/").status_code == 404
    assert client.get("/assets").status_code == 404


def test_assets_reject_escape_and_allow_internal_symlink(client, dist):
    outside = dist.parent / "outside.js"
    outside.write_text("secret", encoding="utf-8")
    (dist / "assets" / "leak.js").symlink_to(outside)
    (dist / "assets" / "real.js").write_text("kept", encoding="utf-8")
    (dist / "assets" / "link.js").symlink_to(dist / "assets" / "real.js")
    assert client.get("/assets/leak.js").status_code == 404
    escaped = client.get("/assets/%2e%2e/favicon.ico")
    assert escaped.status_code == 404
    assert escaped.content != b"ico"
    linked = client.get("/assets/link.js")
    assert linked.status_code == 200
    assert linked.text == "kept"


def test_report_and_artifact_need_a_real_file(client, app, var_dir, tmp_path):
    run = var_dir / "runs" / "run-abc123abc123"
    run.mkdir(parents=True)
    (run / "state.json").write_text('{"id":"run-abc123abc123"}\n', encoding="utf-8")
    (run / "images").mkdir()
    (run / "images" / "shot.png").write_bytes(b"\x89PNG")
    (run / "images" / "requirement-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (run / "requirement.txt").symlink_to(outside)
    headers = auth(app)
    missing = client.get("/api/runs/run-abc123abc123/report", headers=headers)
    assert missing.status_code == 409
    assert missing.json()["cases"] == []
    state = client.get("/api/runs/run-abc123abc123/artifacts/state.json", headers=headers)
    assert state.status_code == 200
    image = client.get("/api/runs/run-abc123abc123/artifacts/images/shot.png", headers=headers)
    assert image.status_code == 200
    listed = client.get("/api/runs/run-abc123abc123", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["requirement_images"] == ["images/requirement-1.png"]
    assert client.get("/api/runs/run-abc123abc123/artifacts/requirement.txt", headers=headers).status_code == 404
    assert client.get("/api/runs/run-abc123abc123/artifacts/../state.json", headers=headers).status_code == 404
    link = var_dir / "runs" / "run-ffffffffffff"
    link.symlink_to(run)
    assert client.get("/api/runs/run-ffffffffffff", headers=headers).status_code == 404


def test_source_index_keeps_placeholder():
    index = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    text = index.read_text(encoding="utf-8")
    assert 'content="@TOKEN@"' in text
    assert "5173" not in text


def test_unbuilt_frontend_is_not_served_raw(tmp_path):
    app = create_app(tmp_path / "var", tmp_path / "missing-dist")
    with TestClient(app, base_url="http://127.0.0.1:8876") as test_client:
        response = test_client.get("/")
    assert response.status_code == 500
    assert "@TOKEN@" not in response.text
