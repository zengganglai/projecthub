"""Curve names remain independent metadata until their review is approved."""

from contextlib import contextmanager
from hashlib import sha256

import pytest
from test_files import hub as hub
from test_files import upload

from projecthub.contracts import HubError
from projecthub.projects import ProjectService


def submit_curve(files, actor, project_id, file_name, curve_name):
    return upload(
        files,
        actor,
        project_id,
        "curve",
        file_name,
        b"{}",
        {"curve_name": curve_name, "curve_type": "type", "description": ""},
    )


def submit_update(files, actor, project_id, file_id, **fields):
    handle = files.begin_curve_update(
        actor,
        project_id,
        file_id,
        {"curve_type": "updated", "description": "new description", **fields},
        sha256(b'{"updated":true}').hexdigest(),
    )
    try:
        handle.write(b'{"updated":true}')
        return handle.finish()
    finally:
        handle.close()


@pytest.mark.parametrize(
    "name_fields",
    [{}, {"curve_name": ""}, {"curve_name": " \t\n\u3000"}, {"curve_name": None}, {"curve_name": 7}],
)
def test_new_curve_upload_requires_a_nonblank_string_name(hub, name_fields):
    files, _, actor, _, project = hub
    with pytest.raises(HubError) as caught:
        files.begin_upload(
            actor,
            project["id"],
            "curve",
            "new.json",
            {"curve_type": "type", "description": "", **name_fields},
            sha256(b"{}").hexdigest(),
        )
    assert caught.value.code == "INVALID_UPLOAD"
    assert files.reviews.list_reviews(actor)["count"] == 0


def test_names_include_only_published_current_version_values_without_normalizing(hub):
    files, users, actor, _, project = hub
    pid = project["id"]
    projects = ProjectService(files.settings, files.db, users.permissions, files)
    sibling = projects.create_project_version("M910", "v2", actor=actor)
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [], "count": 0}
    first = submit_curve(files, actor, pid, "unrelated.json", "重复")
    assert files.reviews.get_review(actor, first["id"])["field_info"]["fields"]["curve_name"] == "重复"
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [], "count": 0}
    formal = files.reviews.approve_review(actor, first["id"])
    assert formal["curve_name"] == "重复"
    assert formal["file_name"] == "unrelated.json"
    for file_name, curve_name in (
        ("upper.json", "A"),
        ("lower.json", "a"),
        ("spaced.json", " a "),
    ):
        pending = submit_curve(files, actor, pid, file_name, curve_name)
        files.reviews.approve_review(actor, pending["id"])
    submit_curve(files, actor, pid, "pending.json", "待审核")
    other = submit_curve(files, actor, sibling["id"], "other.json", "另一版本")
    files.reviews.approve_review(actor, other["id"])
    assert files.catalog.list_curve_names(pid, actor=actor) == {
        "names": [" a ", "A", "a", "重复"],
        "count": 4,
    }
    assert files.catalog.list_curve_names(sibling["id"], actor=actor) == {"names": ["另一版本"], "count": 1}
    listed = files.catalog.list_files(project_id=pid, category="curve", actor=actor)["files"]
    assert listed[0]["curve_name"] == "重复"


def test_rename_and_delete_names_follow_review_outcomes(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    pending = submit_curve(files, actor, pid, "curve.json", "原名称")
    original = files.reviews.approve_review(actor, pending["id"])
    for approved in (False, True):
        renamed = submit_update(files, actor, pid, original["id"], curve_name=" 新名称 ")
        info = files.reviews.get_review(actor, renamed["id"])["field_info"]
        assert info["fields"]["curve_name"] == " 新名称 "
        assert files.catalog.list_curve_names(pid, actor=actor) == {"names": ["原名称"], "count": 1}
        if approved:
            published = files.reviews.approve_review(actor, renamed["id"])
            assert published["id"] == original["id"]
            assert published["file_name"] == "curve.json"
            assert published["curve_name"] == " 新名称 "
        else:
            files.reviews.reject_review(actor, renamed["id"])
            assert files.catalog.list_curve_names(pid, actor=actor) == {"names": ["原名称"], "count": 1}
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [" 新名称 "], "count": 1}
    deletion = files.reviews.request_curve_delete(actor, pid, original["id"])
    files.reviews.reject_review(actor, deletion["id"])
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [" 新名称 "], "count": 1}
    deletion = files.reviews.request_curve_delete(actor, pid, original["id"])
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [" 新名称 "], "count": 1}
    files.reviews.approve_review(actor, deletion["id"])
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": [], "count": 0}


def test_update_omitted_name_preserves_original_and_other_field_validation(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    pending = submit_curve(files, actor, pid, "curve.json", " Original ")
    original = files.reviews.approve_review(actor, pending["id"])
    updated = submit_update(files, actor, pid, original["id"])
    info = files.reviews.get_review(actor, updated["id"])["field_info"]
    assert info["fields"]["curve_name"] == " Original "
    published = files.reviews.approve_review(actor, updated["id"])
    assert published["curve_name"] == " Original "
    assert published["curve_type"] == "updated"
    assert files.catalog.read_json(original["id"], "curve", pid, actor=actor) == {"updated": True}
    invalid_fields = [
        None,
        [],
        "invalid",
        {"curve_type": "type"},
        {"curve_type": "type", "description": "", "unknown": "value"},
        {"curve_type": "type", "description": "", "curve_name": ""},
        {"curve_type": "type", "description": "", "curve_name": " \t\u3000"},
        {"curve_type": "type", "description": "", "curve_name": None},
    ]
    for fields in invalid_fields:
        with pytest.raises(HubError) as caught:
            files.begin_curve_update(actor, pid, original["id"], fields, sha256(b"{}").hexdigest())
        assert caught.value.code == "INVALID_UPLOAD", fields
    with pytest.raises(HubError) as caught:
        files.begin_curve_update(actor, pid, original["id"], {"curve_type": "type", "description": ""}, "bad")
    assert caught.value.code == "INVALID_SHA256"


def test_review_detects_a_changed_published_curve_name(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    pending = submit_curve(files, actor, pid, "curve.json", "Original")
    original = files.reviews.approve_review(actor, pending["id"])
    updated = submit_update(files, actor, pid, original["id"], curve_name="Proposed")
    with files.db.transaction() as connection:
        connection.execute("UPDATE curve_configs SET curve_name=? WHERE id=?", ("Changed", original["id"]))
    with pytest.raises(HubError) as caught:
        files.reviews.approve_review(actor, updated["id"])
    assert caught.value.code == "REVIEW_CONFLICT"
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": ["Changed"], "count": 1}


def test_names_require_current_model_curve_read_permission_inside_operation(hub, monkeypatch):
    files, users, admin, _, project = hub
    pid = project["id"]
    actor = users.create_user(admin, {"name": "reader"})
    pending = submit_curve(files, admin, pid, "private.json", "Private")
    files.reviews.approve_review(admin, pending["id"])
    for current_actor in (None, actor):
        with pytest.raises(HubError) as caught:
            files.catalog.list_curve_names(pid, actor=current_actor)
        assert caught.value.code == "PERMISSION_DENIED"
    files.permissions.set_user_permissions(admin, actor["user_id"], [], {"firmware.read": ["M910"]})
    with pytest.raises(HubError) as caught:
        files.catalog.list_curve_names(pid, actor=actor)
    assert caught.value.code == "PERMISSION_DENIED"
    files.permissions.set_user_permissions(admin, actor["user_id"], [], {"curve.read": ["M910"]})
    assert files.catalog.list_curve_names(pid, actor=actor) == {"names": ["Private"], "count": 1}
    operation = files.operation

    @contextmanager
    def revoke_on_operation():
        with operation() as settings:
            files.permissions.set_user_permissions(admin, actor["user_id"], [], {})
            yield settings

    monkeypatch.setattr(files, "operation", revoke_on_operation)
    with pytest.raises(HubError) as caught:
        files.catalog.list_curve_names(pid, actor=actor)
    assert caught.value.code == "PERMISSION_DENIED"
