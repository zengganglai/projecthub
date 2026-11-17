from hashlib import sha256

import pytest


@pytest.fixture
def hub(tmp_path):
    from projecthub.application import AppConfig, resolve_settings
    from projecthub.database import Database
    from projecthub.files import FileService
    from projecthub.permissions import Permissions
    from projecthub.projects import ProjectService
    from projecthub.users import Users, UserStore

    settings = resolve_settings(AppConfig(project_root=tmp_path))
    db = Database(settings)
    cache = UserStore(db)
    permissions = Permissions(db, cache)
    users = Users(db, permissions)
    files = FileService(settings, db, permissions)
    projects = ProjectService(settings, db, permissions, files)
    try:
        db.open(initialize=files.scan_initial)
        cache.initialize()
        permissions.validate()
        projects.initialize()
        files.initialize()
        actor = {"user_id": next(row["id"] for row in cache.all() if row["is_admin"])}
        model = projects.create_project_model("M910", actor=actor)
        project = projects.create_project_version("M910", "v1", actor=actor)
        yield files, users, actor, model, project
    finally:
        files.close()
        cache.close()
        db.close()


def upload(files, actor, pid, category, name, content, fields):
    handle = files.begin_upload(actor, pid, category, name, fields, sha256(content).hexdigest())
    try:
        handle.write(content)
        return handle.finish()
    finally:
        handle.close()


def test_file_metadata_uses_cached_user_names(hub, monkeypatch):
    files, users, actor, _, project = hub
    users.update_user(actor, actor["user_id"], {"name": "cached-admin"})
    query_one = files.db.one

    def reject_user_name_query(sql, params=()):
        assert "SELECT name FROM users" not in sql, "File names must use the shared user cache"
        return query_one(sql, params)

    monkeypatch.setattr(files.db, "one", reject_user_name_query)
    pending = upload(
        files,
        actor,
        project["id"],
        "firmware",
        "cached.bin",
        b"firmware",
        {
            "firmware_type": "main",
            "version": "2",
            "min_version": "1",
            "max_version": "1.9",
            "release_notes": "cached user name",
        },
    )
    assert pending["submitter_name"] == "cached-admin"
    firmware = files.reviews.approve_review(actor, pending["id"])
    assert firmware["uploaded_by"] == "cached-admin"
    authorization = files.authorizations.create_download_authorization(
        actor, project["id"], firmware["path"], "2099-01-01", "1234567890abcdef", "001"
    )
    assert authorization["created_by"] == "cached-admin"


def test_curve_review_update_sort_delete_and_restart(hub):
    files, users, actor, model, project = hub
    pid = project["id"]
    pending = upload(
        files,
        actor,
        pid,
        "curve",
        "a.json",
        b'{"a":1}',
        {"curve_name": "test", "curve_type": "type", "description": ""},
    )
    assert files.catalog.list_files(project_id=pid, category="curve")["count"] == 0
    assert files.reviews.get_review(actor, pending["id"])["field_info"]["category"] == "curve"
    formal = files.reviews.approve_review(actor, pending["id"])
    assert files.catalog.read_json(formal["id"], "curve", pid, actor=actor) == {"a": 1}
    handle = files.begin_curve_update(
        actor,
        pid,
        formal["id"],
        {"curve_name": "test", "curve_type": "changed", "description": "new"},
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
        files,
        actor,
        project["id"],
        "curve",
        "a.json",
        b"{}",
        {"curve_name": "test", "curve_type": "c", "description": ""},
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
        actor,
        project["id"],
        "curve",
        "a.json",
        {"curve_name": "test", "curve_type": "c", "description": ""},
        "0" * 64,
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
        files,
        actor,
        project["id"],
        "curve",
        "owned.json",
        b"{}",
        {"curve_name": "test", "curve_type": "c", "description": ""},
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


def test_file_search_preserves_ascii_and_literal_matching(hub):
    files, _, actor, _, project = hub
    upload(files, actor, project["id"], "public", "Alpha.bin", b"a", {"description": "MiXeD Ä %_"})
    upload(files, actor, project["id"], "public", "beta.bin", b"b", {"description": "ä plain"})
    for query, names in (
        (None, ["Alpha.bin", "beta.bin"]),
        ("", ["Alpha.bin", "beta.bin"]),
        ("aLpHa", ["Alpha.bin"]),
        ("mixed", ["Alpha.bin"]),
        ("Ä", ["Alpha.bin"]),
        ("ä", ["beta.bin"]),
        ("%_", ["Alpha.bin"]),
        ("M910", ["Alpha.bin", "beta.bin"]),
        ("V1", ["Alpha.bin", "beta.bin"]),
        (".BIN", ["Alpha.bin", "beta.bin"]),
    ):
        result = files.catalog.list_files(project_id=project["id"], category="public", q=query, actor=actor)
        assert [row["file_name"] for row in result["files"]] == names, query
        assert result["count"] == len(names)


def test_review_listing_filters_before_pagination(hub):
    from projecthub.projects import ProjectService

    files, users, actor, _, project = hub
    projects = ProjectService(files.settings, files.db, users.permissions, files)
    projects.create_project_model("Other", actor=actor)
    hidden = projects.create_project_version("Other", "v1", actor=actor)
    sibling = projects.create_project_version("M910", "v2", actor=actor)
    reviewer = users.create_user(actor, {"name": "reviewer"})
    users.permissions.set_user_permissions(actor, reviewer["user_id"], [], {"curve.review": ["M910"]})
    upload(
        files,
        actor,
        hidden["id"],
        "curve",
        "hidden.json",
        b"{}",
        {"curve_name": "test", "curve_type": "c", "description": ""},
    )
    upload(
        files,
        actor,
        project["id"],
        "firmware",
        "firmware.bin",
        b"firmware",
        {
            "firmware_type": "main",
            "version": "1",
            "min_version": "0",
            "max_version": "0",
            "release_notes": "",
        },
    )
    for version, name, curve_name in (
        (project, "first.json", "first"),
        (sibling, "sibling.json", "sibling"),
        (project, "last.json", "last"),
    ):
        upload(
            files,
            actor,
            version["id"],
            "curve",
            name,
            b"{}",
            {"curve_name": curve_name, "curve_type": "c", "description": ""},
        )
    listing = files.reviews.list_reviews(reviewer)
    assert [row["file_name"] for row in listing["reviews"]] == ["first.json", "sibling.json", "last.json"]
    assert listing["count"] == 3
    page = files.reviews.list_reviews(reviewer, project_id=project["id"], offset=1, limit=1)
    assert page["offset"] == 1 and page["count"] == 1
    assert page["reviews"][0]["file_name"] == "last.json"
    assert page["reviews"][0]["project_name"] == "M910"
    assert page["reviews"][0]["field_info"]["category"] == "curve"
