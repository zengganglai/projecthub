from hashlib import sha256
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient


def command(data, identifier="test"):
    import rfc8785

    return dict(id=identifier, data=data, sha256=sha256(rfc8785.dumps(data)).hexdigest())


@pytest.fixture
def client(tmp_path):
    from projecthub.application import AppConfig, create_app

    app = create_app(AppConfig(project_root=tmp_path))
    assert not (tmp_path / "data").exists()
    with TestClient(app) as client:
        row = app.state.db.one("SELECT name,password FROM users WHERE is_admin=1")
        client.headers.update(
            {"X-User-Name": quote(row["name"], safe=""), "X-Password": quote(row["password"], safe="")}
        )
        yield client
    assert app.state.ready is False
    assert app.state.resources == []


def test_http_end_to_end_files_permissions_and_download(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["data"]["ready"] is True
    response = client.post("/api/v1/projects", json=command({"name": "M910"}))
    assert response.status_code == 201, response.text
    model = response.json()["data"]["id"]
    response = client.post(f"/api/v1/projects/{model}/versions", json=command({"version": "basic"}))
    assert response.status_code == 201, response.text
    version = response.json()["data"]["id"]
    base = f"/api/v1/projects/{model}/versions/{version}"
    raw = b'{"temperature":42}'
    response = client.post(
        base + "/curves/upload",
        params={
            "file_name": "sample.json",
            "curve_type": "thermal",
            "description": "test",
            "file_sha256": sha256(raw).hexdigest(),
        },
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 202, response.text
    review = response.json()["data"]["id"]
    response = client.post(f"/api/v1/reviews/{review}/approve", json=command({}))
    assert response.status_code == 200, response.text
    file_id = response.json()["data"]["id"]
    response = client.get(base + f"/files/curve/{file_id}/download")
    assert response.content == raw
    assert response.headers["x-content-sha256"] == sha256(raw).hexdigest()
    response = client.head(base + f"/files/curve/{file_id}/download")
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-length"] == str(len(raw))
    response = client.get(base + f"/files/curve/{file_id}/json")
    assert response.json()["data"] == {"temperature": 42}
    assert set(response.json()) == {"id", "status", "timestamp", "data", "sha256"}
