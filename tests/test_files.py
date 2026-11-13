from hashlib import sha256

import pytest


@pytest.fixture
def hub(tmp_path):
    from projecthub.application import AppConfig, resolve_settings
    from projecthub.database import Database
    from projecthub.files import FileService
    from projecthub.permissions import Permissions
    from projecthub.projects import ProjectService
    from projecthub.users import Users

    settings = resolve_settings(AppConfig(project_root=tmp_path))
    db = Database(settings)
    permissions = Permissions(db)
    users = Users(db, permissions)
    files = FileService(settings, db, permissions)
    projects = ProjectService(settings, db, permissions, files)
    db.open(initialize=files.scan_initial)
    projects.initialize()
    files.initialize()
    actor = {"user_id": db.one("SELECT id FROM users WHERE is_admin=1")["id"]}
    model = projects.create_project_model("M910", actor=actor)
    project = projects.create_project_version("M910", "v1", actor=actor)
    yield files, users, actor, model, project
    files.close()
    db.close()


def upload(files, actor, pid, category, name, content, fields):
    handle = files.begin_upload(actor, pid, category, name, fields, sha256(content).hexdigest())
    try:
        handle.write(content)
        return handle.finish()
    finally:
        handle.close()


def test_curve_review_update_sort_delete_and_restart(hub):
    files, users, actor, model, project = hub
    pid = project["id"]
    pending = upload(
        files, actor, pid, "curve", "a.json", b'{"a":1}', {"curve_type": "type", "description": ""}
    )
    assert files.catalog.list_files(project_id=pid, category="curve")["count"] == 0
    assert files.reviews.get_review(actor, pending["id"])["field_info"]["category"] == "curve"
    formal = files.reviews.approve_review(actor, pending["id"])
    assert files.catalog.read_json(formal["id"], "curve", pid, actor=actor) == {"a": 1}
    handle = files.begin_curve_update(
        actor,
        pid,
        formal["id"],
        {"curve_type": "changed", "description": "new"},
        sha256(b'{"a":2}').hexdigest(),
    )
    handle.write(b'{"a":2}')
    update = handle.finish()
    handle.close()
    updated = files.reviews.approve_review(actor, update["id"])
    assert updated["id"] == formal["id"]
    assert files.catalog.read_json(formal["id"], "curve", pid, actor=actor) == {"a": 2}
    pending = files.reviews.request_curve_delete(actor, pid, formal["id"])
    files.reviews.reject_review(actor, pending["id"])
    assert files.catalog.list_files(project_id=pid, category="curve")["count"] == 1
    pending = files.reviews.request_curve_delete(actor, pid, formal["id"])
    deleted = files.reviews.approve_review(actor, pending["id"])
    assert deleted["status"] == "deleted"
    assert files.catalog.list_files(project_id=pid, category="curve")["count"] == 0


def test_direct_file_and_authorization_revoke_and_hash(hub):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    row = upload(files, actor, project["id"], "public", "a.json", b'{"a":1}', {"description": "public"})
    authorization = files.authorizations.create_download_authorization(
        actor, project["id"], row["path"], "2099-01-01", "ABCDEF0011223344", "001"
    )
    assert authorization["authorization_code"] == "abcdef0011223344"
    assert files.authorizations.read_authorized_json("ABCDEF0011223344") == {"a": 1}
    files.authorizations.delete_download_authorization(actor, project["id"], authorization["id"])
    with pytest.raises(HubError) as caught:
        files.authorizations.get_download_authorization("abcdef0011223344")
    assert caught.value.code == "AUTHORIZATION_NOT_FOUND"
    assert files.delete_managed_file(actor, project["id"], "public", row["id"])["status"] == "deleted"


def test_project_rename_preserves_pending_and_permissions(hub):
    files, users, actor, model, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "a.json", b"{}", {"curve_type": "c", "description": ""}
    )
    from projecthub.projects import ProjectService

    projects = ProjectService(files.settings, files.db, users.permissions, files)
    projects.update_project_model("M910", {"name": "M920"}, actor=actor)
    projects.update_project(project["id"], {"version": "v2"}, actor=actor)
    row = files.reviews.approve_review(actor, pending["id"])
    assert row["path"] == "M920/v2/curve/a.json"
    assert files.catalog.read_json(row["id"], "curve", project["id"], actor=actor) == {}


def test_upload_hash_and_name_conflicts_leave_no_formal_file(hub):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    handle = files.begin_upload(
        actor, project["id"], "curve", "a.json", {"curve_type": "c", "description": ""}, "0" * 64
    )
    handle.write(b"{}")
    with pytest.raises(HubError) as caught:
        handle.finish()
    assert caught.value.code == "UPLOAD_HASH_MISMATCH"
    handle.close()
    assert files.reviews.list_reviews(actor)["count"] == 0
    first = upload(files, actor, project["id"], "public", "same.bin", b"abc", {"description": ""})
    with pytest.raises(HubError) as caught:
        upload(files, actor, project["id"], "internal", "SAME.bin", b"x", {"description": ""})
    assert caught.value.code == "FILE_ALREADY_EXISTS"
    assert files.catalog.list_files(project_id=project["id"])["files"][0]["id"] == first["id"]


def test_file_handlers_share_download_lifecycle(hub):
    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "owned.json", b"{}", {"curve_type": "c", "description": ""}
    )
    review_handle = files.reviews.prepare_review_download(actor, pending["id"])
    assert review_handle.read_chunk(100) == b"{}"
    review_handle.close()
    row = files.reviews.approve_review(actor, pending["id"])
    files.authorizations.create_download_authorization(
        actor, project["id"], row["path"], "2099-01-01", "0123456789abcdef", "device"
    )
    ordinary = files.catalog.prepare_download(row["id"], "curve", project["id"], actor=actor)
    authorized = files.authorizations.prepare_authorized_download("0123456789abcdef")
    files.close()
    assert review_handle.closed and ordinary.closed and authorized.closed
