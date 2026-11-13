"""Model grants control discovery and access across all of a model's versions."""

from urllib.parse import quote

from test_http import client as client
from test_http import command


def setup_models(client):
    admin = {"user_id": client.app.state.db.one("SELECT id FROM users WHERE is_admin=1")["id"]}
    projects = client.app.state.projects
    a = projects.create_project_model("M910", actor=admin)
    b = projects.create_project_model("Other", actor=admin)
    empty = projects.create_project_model("Empty", actor=admin)
    versions = [projects.create_project_version("M910", name, actor=admin) for name in ("高配", "低配")]
    hidden = projects.create_project_version("Other", "高配", actor=admin)
    user = client.app.state.users.create_user(admin, {"name": "engineer", "password": "pw"})
    headers = {"X-User-Name": quote("engineer"), "X-Password": "pw"}
    return admin, a, b, empty, versions, hidden, user, headers


def test_ungranted_models_are_hidden_and_all_versions_share_current_grants(client):
    admin, model, other, empty, versions, hidden, actor, headers = setup_models(client)
    app = client.app.state
    root = f"/api/v1/projects/{model['id']}"
    paths = [f"{root}/versions/{version['id']}" for version in versions]
    assert client.get("/api/v1/projects", headers=headers).json()["data"]["items"] == []
    for path in (root + "/versions", paths[0] + "/files/public", paths[0] + "/config"):
        response = client.get(path, headers=headers)
        assert response.status_code == 404
        assert response.json()["data"]["error"]["code"] == "PROJECT_NOT_FOUND"
    assert app.projects.get_project_model(model["id"], actor=actor) is None
    assert app.projects.get_project_version(model["id"], versions[0]["id"], actor=actor) is None

    app.permissions.set_user_permissions(admin, actor["user_id"], [], {"curve.read": ["M910"]})
    assert [
        item["name"] for item in client.get("/api/v1/projects", headers=headers).json()["data"]["items"]
    ] == ["M910"]
    assert {
        item["version"] for item in client.get(root + "/versions", headers=headers).json()["data"]["items"]
    } == {"高配", "低配"}
    for path in paths:
        assert client.get(path + "/files/curve", headers=headers).status_code == 200
        assert client.get(path + "/files/firmware", headers=headers).status_code == 403
        assert client.get(path + "/files/public", headers=headers).status_code == 403
    assert client.get(f"/api/v1/projects/{other['id']}/versions", headers=headers).status_code == 404
    assert client.get(f"{root}/versions/{hidden['id']}/files/curve", headers=headers).status_code == 404
    assert (
        client.post("/api/v1/projects", headers=headers, json=command({"name": "Denied"})).status_code == 403
    )
    assert {row["name"] for row in app.projects.list_project_models(admin)} == {"M910", "Other", "Empty"}
    assert len(app.projects.list_projects(admin)) == 3
    assert app.projects.get_project_model(empty["id"], actor=actor) is None

    added = app.projects.create_project_version("M910", "客户定制", actor=admin)
    added_path = f"{root}/versions/{added['id']}/files/curve"
    assert client.get(added_path, headers=headers).status_code == 200
    # The cached actor still contains its original identity; discovery must re-read current grants.
    assert len(app.projects.list_projects(actor)) == 3
    app.permissions.set_user_permissions(admin, actor["user_id"], [], {})
    assert app.projects.list_project_models(actor) == []
    assert app.projects.list_projects(actor) == []
    assert app.projects.get_project_version(model["id"], versions[0]["id"], actor=actor) is None
    assert client.get(paths[0] + "/files/curve", headers=headers).status_code == 404


def test_global_storage_and_actor_flags_do_not_bypass_model_authorization(client):
    admin, model, _, _, versions, _, actor, headers = setup_models(client)
    state = client.app.state
    with state.db.transaction() as connection:
        connection.execute(
            "UPDATE users SET permissions=? WHERE id=?",
            ('["curve.read","firmware.read","public.read"]', actor["user_id"]),
        )
    forged = {**actor, "is_admin": True, "global_permissions": ["curve.read"]}
    assert state.projects.list_project_models(forged) == []
    identity = state.users.authenticate("engineer", "pw")
    assert identity["global_permissions"] == []
    response = client.put(
        f"/api/v1/users/{actor['user_id']}/permissions",
        json=command({"global_permissions": ["curve.read"], "project_permissions": {}}),
    )
    assert response.status_code == 422
    catalog = client.get("/api/v1/permissions").json()["data"]["permissions"]
    assert len(catalog) == 14 and all(row["scopes"] == ["project"] for row in catalog)
    path = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}/config"
    config = state.settings.storage.object_dir / "M910/高配/config.json"
    config.write_text('{"name":"private"}', encoding="utf-8")
    assert client.get(path, headers=headers).status_code == 404
    admin_headers = dict(client.headers)
    client.headers.clear()
    try:
        assert client.get(path).status_code == 401
    finally:
        client.headers.update(admin_headers)
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"dealer": ["M910"]})
    assert client.get(path, headers=headers).json()["data"] == {"name": "private"}
    assert state.projects.list_project_models(actor)[0]["id"] == model["id"]
    # An implicit admin grant also covers subsequently created models and versions.
    late = state.projects.create_project_model("Later", actor=admin)
    assert state.projects.get_project_model(late["id"], actor=admin) == late


def test_configuration_rechecks_grants_when_file_operation_begins(client, monkeypatch):
    admin, model, _, _, versions, _, actor, headers = setup_models(client)
    state = client.app.state
    state.permissions.set_user_permissions(admin, actor["user_id"], [], {"dealer": ["M910"]})
    config = state.settings.storage.object_dir / "M910/高配/config.json"
    config.write_text('{"secret":"model-only"}', encoding="utf-8")
    original = state.files.catalog.read_project_config

    def revoke_before_read(*args, **kwargs):
        state.permissions.set_user_permissions(admin, actor["user_id"], [], {})
        return original(*args, **kwargs)

    monkeypatch.setattr(state.files.catalog, "read_project_config", revoke_before_read)
    path = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}/config"
    response = client.get(path, headers=headers)
    assert response.status_code == 404
    assert response.json()["data"]["error"]["code"] == "PROJECT_NOT_FOUND"


def test_configuration_keeps_version_identity_when_a_name_is_reused(client, monkeypatch):
    admin, model, _, _, versions, _, _, _ = setup_models(client)
    state = client.app.state
    config = state.settings.storage.object_dir / "M910/高配/config.json"
    config.write_text('{"version":"original"}', encoding="utf-8")
    original = state.files.catalog.read_project_config

    def rename_before_read(*args, **kwargs):
        state.projects.update_project(versions[0]["id"], {"version": "旗舰"}, actor=admin)
        state.projects.create_project_version("M910", "高配", actor=admin)
        config.write_text('{"version":"replacement"}', encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(state.files.catalog, "read_project_config", rename_before_read)
    path = f"/api/v1/projects/{model['id']}/versions/{versions[0]['id']}/config"
    assert client.get(path).json()["data"] == {"version": "original"}
