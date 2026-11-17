"""The application shares one bounded user cache across account and permission operations."""

import re

from test_http import client as client
from test_http import command


def test_application_defaults_to_200_users(client):
    assert client.app.state.settings.users.max_users == 200


def test_account_and_permission_reads_do_not_select_user_table(client):
    state = client.app.state
    admin = {"user_id": state.db.one("SELECT id FROM users WHERE is_admin=1")["id"]}
    state.projects.create_project_model("M", actor=admin)
    version = state.projects.create_project_version("M", "high", actor=admin)
    reader = state.users.create_user(admin, {"name": "reader", "password": "pw"})
    state.permissions.set_user_permissions(admin, reader["user_id"], [], {"curve.read": ["M"]})
    headers = {"X-User-Name": "reader", "X-Password": "pw"}
    statements = []
    state.db.connection.set_trace_callback(statements.append)
    try:
        assert client.get("/api/v1/users").status_code == 200
        assert client.get("/api/v1/me/permissions", headers=headers).status_code == 200
        assert client.get("/api/v1/projects", headers=headers).json()["data"]["projects"] == ["M"]
        assert "curve.read" in state.permissions.permissions_for(reader, version["id"])
        assert state.permissions.get_user_permissions(admin, reader["user_id"])["project_permissions"] == {
            "curve.read": ["M"]
        }
        assert not [sql for sql in statements if re.search(r"\b(?:FROM|JOIN)\s+users\b", sql, re.I)]
    finally:
        state.db.connection.set_trace_callback(None)
    assert state.user_store is state.users.user_store is state.permissions.user_store


def test_password_and_grants_change_cached_identity_immediately(client):
    state = client.app.state
    admin = {"user_id": state.db.one("SELECT id FROM users WHERE is_admin=1")["id"]}
    state.projects.create_project_model("M", actor=admin)
    reader = state.users.create_user(admin, {"name": "reader", "password": "old"})
    state.permissions.set_user_permissions(admin, reader["user_id"], [], {"dealer": ["M"]})
    old = {"X-User-Name": "reader", "X-Password": "old"}
    new = {"X-User-Name": "reader", "X-Password": "new"}
    assert (
        client.put("/api/v1/me/password", headers=old, json=command({"new_password": "new"})).status_code
        == 200
    )
    assert client.get("/api/v1/projects", headers=old).status_code == 401
    assert client.get("/api/v1/projects", headers=new).json()["data"]["projects"] == ["M"]
    state.permissions.set_user_permissions(admin, reader["user_id"], [], {})
    assert client.get("/api/v1/projects", headers=new).json()["data"]["items"] == []
    state.users.delete_user(admin, reader["user_id"])
    assert client.get("/api/v1/projects", headers=new).status_code == 401


def test_invalidated_user_cache_blocks_authentication_and_readiness(client):
    state = client.app.state
    state.user_store.close()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["data"]["reason"] == "users_not_ready"
    response = client.get("/api/v1/me/permissions")
    assert response.status_code == 503
    assert response.json()["data"]["error"]["code"] == "USERS_NOT_READY"


def test_startup_loads_200_users_and_rejects_201_without_partial_service(tmp_path):
    import pytest
    from fastapi.testclient import TestClient

    from projecthub.application import AppConfig, create_app, resolve_settings
    from projecthub.contracts import HubError
    from projecthub.database import Database

    settings = resolve_settings(AppConfig(project_root=tmp_path))
    database = Database(settings)
    database.open()
    try:
        with database.transaction() as connection:
            connection.executemany(
                "INSERT INTO users(name) VALUES(?)", [(f"member-{i}",) for i in range(198)]
            )
    finally:
        database.close()
    app = create_app(settings)
    with TestClient(app) as client:
        assert len(app.state.user_store.all()) == 200
        assert client.get("/health/ready").status_code == 200
    assert not app.state.user_store.ready
    assert app.state.resources == []
    database.open()
    try:
        with database.transaction() as connection:
            connection.execute("INSERT INTO users(name) VALUES('member-201')")
    finally:
        database.close()
    oversized = create_app(settings)
    with pytest.raises(HubError) as failure:
        with TestClient(oversized):
            pass
    assert failure.value.code == "USER_LIMIT_REACHED"
    assert not oversized.state.user_store.ready
    assert oversized.state.resources == []
