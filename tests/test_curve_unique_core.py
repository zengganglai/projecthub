"""Curve names and file names reserve independent keys within each project version."""

import json
import sqlite3
from hashlib import sha256

import pytest
from test_files import hub as hub
from test_files import upload

from projecthub.contracts import HubError
from projecthub.projects import ProjectService

CONTENT = b"{}"
DIGEST = sha256(CONTENT).hexdigest()


def begin(files, actor, project_id, file_name, curve_name):
    return files.begin_upload(
        actor,
        project_id,
        "curve",
        file_name,
        {"curve_name": curve_name, "curve_type": "type", "description": ""},
        DIGEST,
    )


def submit(files, actor, project_id, file_name, curve_name):
    return upload(
        files,
        actor,
        project_id,
        "curve",
        file_name,
        CONTENT,
        {"curve_name": curve_name, "curve_type": "type", "description": ""},
    )


def publish(files, actor, project_id, file_name, curve_name):
    pending = submit(files, actor, project_id, file_name, curve_name)
    return files.reviews.approve_review(actor, pending["id"])


@pytest.mark.parametrize("state", ["receiving", "pending", "published"])
@pytest.mark.parametrize(
    ("file_name", "curve_name", "published_error"),
    [
        ("other.json", "Reserved", "CURVE_NAME_ALREADY_EXISTS"),
        ("CURVE.JSON", "Other", "FILE_ALREADY_EXISTS"),
        ("CURVE.JSON", "Reserved", "FILE_ALREADY_EXISTS"),
    ],
)
def test_each_name_blocks_other_uploads_in_the_same_version(
    hub, state, file_name, curve_name, published_error
):
    files, _, actor, _, project = hub
    pid = project["id"]
    active = begin(files, actor, pid, "curve.json", "Reserved")
    try:
        if state != "receiving":
            active.write(CONTENT)
            review = active.finish()
            if state == "published":
                files.reviews.approve_review(actor, review["id"])
        with pytest.raises(HubError) as caught:
            begin(files, actor, pid, file_name, curve_name)
        assert caught.value.code == (published_error if state == "published" else "REVIEW_ALREADY_PENDING")
    finally:
        active.close()


def test_file_names_keep_unicode_casefold_comparison(hub):
    files, _, actor, _, project = hub
    publish(files, actor, project["id"], "Straße.json", "First")
    with pytest.raises(HubError) as caught:
        begin(files, actor, project["id"], "STRASSE.JSON", "Second")
    assert caught.value.code == "FILE_ALREADY_EXISTS"


def test_curve_names_keep_case_and_surrounding_spaces(hub):
    files, _, actor, _, project = hub
    for file_name, curve_name in [("upper.json", "A"), ("lower.json", "a"), ("spaces.json", " a ")]:
        publish(files, actor, project["id"], file_name, curve_name)
    assert files.catalog.list_curve_names(project["id"], actor=actor) == {
        "names": [" a ", "A", "a"],
        "count": 3,
    }


@pytest.mark.parametrize("state", ["receiving", "pending", "published"])
def test_both_names_can_be_reused_in_another_version(hub, state):
    files, users, actor, _, project = hub
    projects = ProjectService(files.settings, files.db, users.permissions, files)
    sibling = projects.create_project_version("M910", "v2", actor=actor)
    active = begin(files, actor, project["id"], "curve.json", "Shared")
    try:
        if state != "receiving":
            active.write(CONTENT)
            review = active.finish()
            if state == "published":
                files.reviews.approve_review(actor, review["id"])
        other = publish(files, actor, sibling["id"], "curve.json", "Shared")
        assert other["curve_name"] == "Shared"
        assert other["file_name"] == "curve.json"
    finally:
        active.close()


@pytest.mark.parametrize("omit_name", [False, True])
def test_update_ignores_its_own_names_and_keeps_original_file_name(hub, omit_name):
    files, _, actor, _, project = hub
    pid = project["id"]
    original = publish(files, actor, pid, "curve.json", "Own")
    fields = {"curve_type": "changed", "description": "updated"}
    if not omit_name:
        fields["curve_name"] = "Own"
    active = files.begin_curve_update(actor, pid, original["id"], fields, DIGEST)
    try:
        active.write(CONTENT)
        review = active.finish()
    finally:
        active.close()
    updated = files.reviews.approve_review(actor, review["id"])
    assert updated["id"] == original["id"]
    assert updated["file_name"] == "curve.json"
    assert updated["curve_name"] == "Own"
    assert updated["curve_type"] == "changed"


@pytest.mark.parametrize("state", ["receiving", "pending", "published"])
def test_update_cannot_take_another_curves_reserved_name(hub, state):
    files, _, actor, _, project = hub
    pid = project["id"]
    original = publish(files, actor, pid, "original.json", "Original")
    other = begin(files, actor, pid, "other.json", "Other")
    try:
        if state != "receiving":
            other.write(CONTENT)
            review = other.finish()
            if state == "published":
                files.reviews.approve_review(actor, review["id"])
        with pytest.raises(HubError) as caught:
            files.begin_curve_update(
                actor,
                pid,
                original["id"],
                {"curve_name": "Other", "curve_type": "type", "description": ""},
                DIGEST,
            )
        assert caught.value.code == (
            "CURVE_NAME_ALREADY_EXISTS" if state == "published" else "REVIEW_ALREADY_PENDING"
        )
    finally:
        other.close()


@pytest.mark.parametrize("state", ["receiving", "pending"])
def test_update_reserves_new_name_while_published_name_remains_taken(hub, state):
    files, _, actor, _, project = hub
    pid = project["id"]
    original = publish(files, actor, pid, "original.json", "Original")
    active = files.begin_curve_update(
        actor,
        pid,
        original["id"],
        {"curve_name": "Proposed", "curve_type": "type", "description": ""},
        DIGEST,
    )
    try:
        if state == "pending":
            active.write(CONTENT)
            active.finish()
        for curve_name, code in [
            ("Original", "CURVE_NAME_ALREADY_EXISTS"),
            ("Proposed", "REVIEW_ALREADY_PENDING"),
        ]:
            with pytest.raises(HubError) as caught:
                begin(files, actor, pid, "new.json", curve_name)
            assert caught.value.code == code
    finally:
        active.close()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("phase", ["finish", "approve"])
@pytest.mark.parametrize("conflict", ["curve_name", "file_name"])
def test_finish_and_approval_recheck_published_names(hub, operation, phase, conflict):
    files, _, actor, _, project = hub
    pid = project["id"]
    other = publish(files, actor, pid, "other.json", "Other")
    if operation == "update":
        original = publish(files, actor, pid, "target.json", "Original")
        active = files.begin_curve_update(
            actor,
            pid,
            original["id"],
            {"curve_name": "Proposed", "curve_type": "type", "description": ""},
            DIGEST,
        )
    else:
        active = begin(files, actor, pid, "target.json", "Proposed")
    try:
        active.write(CONTENT)
        if phase == "approve":
            pending = active.finish()
        if conflict == "curve_name":
            with files.db.transaction() as connection:
                connection.execute(
                    "UPDATE curve_configs SET curve_name='Proposed' WHERE id=?", (other["id"],)
                )
        else:
            # A new file can appear on disk after the initial upload check.
            collision = files.settings.storage.object_dir / "M910/v1/public/target.json"
            collision.write_bytes(CONTENT)
        with pytest.raises(HubError) as caught:
            if phase == "finish":
                active.finish()
            else:
                files.reviews.approve_review(actor, pending["id"])
        assert caught.value.code == (
            "CURVE_NAME_ALREADY_EXISTS" if conflict == "curve_name" else "FILE_ALREADY_EXISTS"
        )
        assert files.catalog.list_files(project_id=pid, category="curve")["count"] == (
            2 if operation == "update" else 1
        )
        assert files.reviews.list_reviews(actor)["count"] == (1 if phase == "approve" else 0)
    finally:
        active.close()


def test_finish_rechecks_pending_curve_names(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    other = submit(files, actor, pid, "other.json", "Other")
    active = begin(files, actor, pid, "target.json", "Proposed")
    try:
        info = other["field_info"]
        info["fields"]["curve_name"] = "Proposed"
        with files.db.transaction() as connection:
            connection.execute(
                "UPDATE pending_reviews SET field_info=? WHERE id=?",
                (json.dumps(info), other["id"]),
            )
        active.write(CONTENT)
        with pytest.raises(HubError) as caught:
            active.finish()
        assert caught.value.code == "REVIEW_ALREADY_PENDING"
        assert files.reviews.list_reviews(actor)["count"] == 1
    finally:
        active.close()


@pytest.mark.parametrize("release", ["cancel", "reject", "delete"])
def test_completed_lifecycle_releases_both_names(hub, release):
    files, _, actor, _, project = hub
    pid = project["id"]
    active = begin(files, actor, pid, "curve.json", "Reused")
    try:
        if release != "cancel":
            active.write(CONTENT)
            pending = active.finish()
            if release == "reject":
                files.reviews.reject_review(actor, pending["id"])
            else:
                formal = files.reviews.approve_review(actor, pending["id"])
                deletion = files.reviews.request_curve_delete(actor, pid, formal["id"])
                with pytest.raises(HubError) as caught:
                    begin(files, actor, pid, "other.json", "Reused")
                assert caught.value.code == "REVIEW_ALREADY_PENDING"
                files.reviews.approve_review(actor, deletion["id"])
    finally:
        active.close()
    replacement = publish(files, actor, pid, "curve.json", "Reused")
    assert replacement["curve_name"] == "Reused"
    assert replacement["file_name"] == "curve.json"


def test_approved_rename_releases_old_name_and_reserves_new_name(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    original = publish(files, actor, pid, "original.json", "Original")
    active = files.begin_curve_update(
        actor,
        pid,
        original["id"],
        {"curve_name": "Renamed", "curve_type": "type", "description": ""},
        DIGEST,
    )
    try:
        active.write(CONTENT)
        review = active.finish()
    finally:
        active.close()
    files.reviews.approve_review(actor, review["id"])
    publish(files, actor, pid, "new.json", "Original")
    with pytest.raises(HubError) as caught:
        begin(files, actor, pid, "duplicate.json", "Renamed")
    assert caught.value.code == "CURVE_NAME_ALREADY_EXISTS"


@pytest.mark.parametrize(
    ("curve_name", "file_name"),
    [("Reserved", "other.json"), ("Other", "curve.json"), ("Other", "CURVE.JSON")],
)
def test_database_enforces_each_unique_name_within_the_version(hub, curve_name, file_name):
    files, _, actor, _, project = hub
    publish(files, actor, project["id"], "curve.json", "Reserved")
    with pytest.raises(sqlite3.IntegrityError), files.db.transaction() as connection:
        connection.execute(
            "INSERT INTO curve_configs(project_id,curve_name,file_name,sha256,path) VALUES(?,?,?,?,?)",
            (project["id"], curve_name, file_name, DIGEST, "unique-path.json"),
        )


@pytest.mark.parametrize("column", ["curve_name", "file_name"])
@pytest.mark.parametrize("value", [None, "", " \t\n\u3000\u00a0"])
def test_database_rejects_missing_or_blank_names(hub, column, value):
    files, _, _, _, project = hub
    fields = {"curve_name": "Name", "file_name": "curve.json", column: value}
    with pytest.raises(sqlite3.IntegrityError), files.db.transaction() as connection:
        connection.execute(
            "INSERT INTO curve_configs(project_id,curve_name,file_name,sha256,path) VALUES(?,?,?,?,?)",
            (project["id"], fields["curve_name"], fields["file_name"], DIGEST, "unique-path.json"),
        )
