"""Curve templates belong to a model and are shared by all its versions."""

from urllib.parse import quote

from fastapi.testclient import TestClient
from test_http import client as client
from test_model_permissions import setup_models

from projecthub.application import AppConfig, create_app


def test_versions_share_only_their_model_template_and_current_grants(client):
    admin, model, other, _, versions, hidden, actor, headers = setup_models(client)
    state = client.app.state
    root = state.settings.storage.object_dir
    template = root / "M910/模板.json"
    template.write_text('{"model":"M910"}', encoding="utf-8")
    (root / "Other/模板.json").write_text('{"model":"Other"}', encoding="utf-8")
    for version in versions:
        (root / "M910" / version["version"] / "curve/模板.json").write_text(
            '{"source":"old-version-path"}', encoding="utf-8"
        )
    paths = [
        f"/api/v1/projects/{model['id']}/versions/{version['id']}/curves/template" for version in versions
    ]
    other_path = f"/api/v1/projects/{other['id']}/versions/{hidden['id']}/curves/template"
    assert client.get(paths[0], headers=headers).status_code == 404
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"curve.read": ["M910"]})
    for path in paths:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"] == {"model": "M910"}
    assert client.get(other_path, headers=headers).status_code == 404
    assert client.get(other_path).json()["data"] == {"model": "Other"}
    template.write_text('{"revision":2}', encoding="utf-8")
    for path in paths:
        assert client.get(path, headers=headers).json()["data"] == {"revision": 2}
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"dealer": ["M910"]})
    assert client.get(paths[0], headers=headers).status_code == 403
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {})
    assert client.get(paths[0], headers=headers).status_code == 404


def test_missing_model_template_does_not_fall_back_to_a_version(client):
    _, model, _, _, versions, _, _, _ = setup_models(client)
    root = client.app.state.settings.storage.object_dir
    version = versions[0]
    (root / "M910" / version["version"] / "curve/模板.json").write_text("{}", encoding="utf-8")
    path = f"/api/v1/projects/{model['id']}/versions/{version['id']}/curves/template"
    response = client.get(path)
    assert response.status_code == 404
    assert response.json()["data"]["error"]["code"] == "TEMPLATE_NOT_FOUND"
    (root / "M910/模板.json").write_text("[]", encoding="utf-8")
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["data"]["error"]["code"] == "TEMPLATE_INVALID_JSON"


def test_model_template_survives_initial_scan_sync_and_model_version_renames(tmp_path):
    root = tmp_path / "data/files"
    for version in ("标准版", "旗舰版"):
        (root / "M910" / version / "curve").mkdir(parents=True)
    (root / "M910/模板.json").write_text('{"shared":true}', encoding="utf-8")
    app = create_app(AppConfig(project_root=tmp_path))
    with TestClient(app) as client:
        state = app.state
        row = next(row for row in state.user_store.all() if row["is_admin"])
        admin = {"user_id": row["id"]}
        client.headers.update({"X-User-Name": quote(row["name"]), "X-Password": quote(row["password"])})
        model = state.projects.list_project_models(admin)[0]
        versions = state.projects.list_projects(admin)
        assert {version["version"] for version in versions} == {"标准版", "旗舰版"}
        paths = [
            f"/api/v1/projects/{model['id']}/versions/{version['id']}/curves/template" for version in versions
        ]
        for path in paths:
            assert client.get(path).json()["data"] == {"shared": True}
        state.files.synchronizer.synchronize()
        assert state.db.query("SELECT id FROM curve_configs") == []
        state.projects.update_project(versions[0]["id"], {"version": "定制版"}, actor=admin)
        state.projects.update_project_model("M910", {"name": "M920"}, actor=admin)
        for path in paths:
            assert client.get(path).json()["data"] == {"shared": True}
        assert (root / "M920/模板.json").is_file()
        assert not (root / "M910").exists()
