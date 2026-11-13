from hashlib import sha256
from pathlib import Path

import pytest
from test_files import hub as hub
from test_files import upload


def test_firmware_upgrade_preserves_id_models_and_rejects_overlap(hub):
    files, _, actor, _, project = hub
    pid = project["id"]
    fields = {
        "firmware_type": "main",
        "version": "2",
        "min_version": "1",
        "max_version": "1.9",
        "release_notes": "first",
        "supported_models": ["M910", "M910.A"],
    }
    pending = upload(files, actor, pid, "firmware", "v2.bin", b"v2", fields)
    first = files.reviews.approve_review(actor, pending["id"])
    assert first["uploaded_at"] == pending["uploaded_at"]
    assert files.catalog.check_firmware(pid, "1.5", actor=actor)["firmwares"][0]["id"] == first["id"]
    assert files.catalog.check_firmware(pid, "2", actor=actor)["update_available"] is False
    fields.update(version="3", min_version="1", max_version="2.9")
    del fields["supported_models"]
    pending = upload(files, actor, pid, "firmware", "v3.bin", b"v3", fields)
    assert pending["field_info"]["fields"]["supported_models"] == ["M910", "M910.A"]
    updated = files.reviews.approve_review(actor, pending["id"])
    assert updated["id"] == first["id"]
    assert updated["file_name"] == "v3.bin"
    assert not (files.settings.storage.object_dir / first["path"]).exists()
    pending = files.reviews.request_firmware_delete(actor, pid, first["id"])
    assert files.reviews.approve_review(actor, pending["id"])["status"] == "deleted"


def test_revoked_upload_does_not_publish(hub):
    from projecthub.contracts import HubError

    files, users, actor, _, project = hub
    user = users.create_user(actor, {"name": "uploader", "password": "pw"})
    users.permissions.set_user_permissions(actor, user["user_id"], [], {"curve.manage": ["M910"]})
    handle = files.begin_upload(
        user,
        project["id"],
        "curve",
        "secret.json",
        {"curve_type": "c", "description": ""},
        sha256(b"{}").hexdigest(),
    )
    try:
        handle.write(b"{}")
        users.permissions.set_user_permissions(actor, user["user_id"], [], {})
        with pytest.raises(HubError) as error:
            handle.finish()
        assert error.value.code == "PERMISSION_DENIED"
    finally:
        handle.close()
    assert files.reviews.list_reviews(actor)["count"] == 0
    assert files.catalog.list_files(project_id=project["id"])["count"] == 0


def test_failed_sql_window_restores_original_and_keeps_review(hub, monkeypatch):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pid = project["id"]
    pending = upload(files, actor, pid, "curve", "a.json", b'{"a":1}', {"curve_type": "c", "description": ""})
    original = files.reviews.approve_review(actor, pending["id"])
    update = files.begin_curve_update(
        actor, pid, original["id"], {"curve_type": "c", "description": "new"}, sha256(b'{"a":2}').hexdigest()
    )
    update.write(b'{"a":2}')
    pending = update.finish()
    update.close()
    apply = files.publisher.apply

    def interrupted(*args, **kwargs):
        apply(*args, **kwargs)
        raise OSError("simulated disk/commit boundary failure")

    monkeypatch.setattr(files.publisher, "apply", interrupted)
    with pytest.raises(HubError):
        files.reviews.approve_review(actor, pending["id"])
    assert files.catalog.read_json(original["id"], "curve", pid, actor=actor) == {"a": 1}
    assert files.reviews.get_review(actor, pending["id"])["id"] == pending["id"]
    assert not files.recovery_required


def test_failed_recovery_blocks_reads_then_can_retry(hub, monkeypatch):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "a.json", b"{}", {"curve_type": "c", "description": ""}
    )
    apply = files.publisher.apply
    recover = files.publisher.recover

    def interrupted(*args, **kwargs):
        apply(*args, **kwargs)
        raise OSError("stop before commit")

    def unavailable():
        raise OSError("storage temporarily unavailable")

    monkeypatch.setattr(files.publisher, "apply", interrupted)
    monkeypatch.setattr(files.publisher, "recover", unavailable)
    with pytest.raises(HubError) as error:
        files.reviews.approve_review(actor, pending["id"])
    assert error.value.code == "REVIEW_RECOVERY_FAILED"
    with pytest.raises(HubError):
        files.catalog.list_files(project_id=project["id"])
    monkeypatch.setattr(files.publisher, "recover", recover)
    files.recover_reviews()
    assert not files.recovery_required
    assert files.catalog.list_files(project_id=project["id"])["count"] == 0
    assert files.reviews.get_review(actor, pending["id"])["id"] == pending["id"]


def test_initial_scan_skips_reviewed_categories_and_sync_rejects_changes(tmp_path):
    from projecthub.application import AppConfig, resolve_settings
    from projecthub.contracts import HubError
    from projecthub.database import Database
    from projecthub.files import FileService
    from projecthub.permissions import Permissions

    settings = resolve_settings(AppConfig(project_root=tmp_path))
    directory = tmp_path / "data/files/M/v1"
    for category in ["curve", "firmware", "internal", "public"]:
        (directory / category).mkdir(parents=True)
    (directory / "curve/unreviewed.json").write_text("{}")
    (directory / "public/a.txt").write_text("hello")
    db = Database(settings)
    files = FileService(settings, db, Permissions(db))
    try:
        db.open(initialize=files.scan_initial)
        files.initialize()
        assert files.catalog.list_files()["count"] == 1
        with pytest.raises(HubError) as error:
            files.synchronizer.synchronize()
        assert error.value.code == "REVIEW_REQUIRED"
    finally:
        files.close()
        db.close()


def test_committed_delete_cleanup_is_retryable(hub, monkeypatch):
    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "cleanup.json", b"{}", {"curve_type": "c", "description": ""}
    )
    row = files.reviews.approve_review(actor, pending["id"])
    pending = files.reviews.request_curve_delete(actor, project["id"], row["id"])
    original_unlink = Path.unlink

    def fail_removed(path, *args, **kwargs):
        if path.name == "removed":
            raise PermissionError("injected tombstone cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_removed)
        assert files.reviews.approve_review(actor, pending["id"])["status"] == "deleted"
    files.recover_reviews()
    assert not list(files.publisher.directory.iterdir())
    assert files.catalog.list_files(project_id=project["id"], category="curve")["count"] == 0


def test_cleanup_failure_blocks_changes_until_recovery(hub, monkeypatch):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "cleanup.json", b"{}", {"curve_type": "c", "description": ""}
    )
    original_unlink = Path.unlink

    def fail_content(path, *args, **kwargs):
        if path.name == "content":
            raise PermissionError("injected witness cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_content)
        row = files.reviews.approve_review(actor, pending["id"])
    assert files.recovery_required
    with pytest.raises(HubError) as failure:
        files.set_curve_position(actor, project["id"], row["id"], 1)
    assert failure.value.code == "REVIEW_RECOVERY_FAILED"
    files.recover_reviews()
    assert not files.recovery_required
    assert files.catalog.list_files(project_id=project["id"], category="curve")["count"] == 1


def test_delete_snapshot_copy_failure_leaves_no_name_reservation(hub, monkeypatch):
    import projecthub.storage.io as storage

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "delete.json", b"{}", {"curve_type": "c", "description": ""}
    )
    row = files.reviews.approve_review(actor, pending["id"])

    def interrupted(source, destination, length):
        destination.write(b"{")
        raise OSError("injected partial copy failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage.shutil, "copyfileobj", interrupted)
        with pytest.raises(OSError):
            files.reviews.request_curve_delete(actor, project["id"], row["id"])
    assert files.db.query("SELECT * FROM pending_reviews") == []
    pending = files.reviews.request_curve_delete(actor, project["id"], row["id"])
    assert files.reviews.approve_review(actor, pending["id"])["status"] == "deleted"


@pytest.mark.parametrize("interruption", ["cleanup", "resolved_marker"])
def test_rollback_cleanup_retries_after_original_file_is_restored(hub, monkeypatch, interruption):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    fields = {"curve_type": "c", "description": ""}
    first = upload(files, actor, project["id"], "curve", "rollback.json", b"{}", fields)
    row = files.reviews.approve_review(actor, first["id"])
    handle = files.begin_curve_update(
        actor, project["id"], row["id"], fields, sha256(b'{"new":1}').hexdigest()
    )
    handle.write(b'{"new":1}')
    pending = handle.finish()
    handle.close()
    import projecthub.files.publication as publication

    apply, unlink = files.publisher.apply, Path.unlink
    write_json = publication.write_json

    def marker_failure(path, value):
        if value.get("resolved"):
            raise OSError("injected failure before recovery marker")
        return write_json(path, value)

    def interrupted(*args, **kwargs):
        apply(*args, **kwargs)
        raise OSError("injected after replacement before commit")

    def cleanup_failure(path, *args, **kwargs):
        if path.name == "content":
            raise PermissionError("injected cleanup failure after rollback")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files.publisher, "apply", interrupted)
        if interruption == "cleanup":
            patch.setattr(Path, "unlink", cleanup_failure)
        else:
            patch.setattr(publication, "write_json", marker_failure)
        with pytest.raises(HubError) as failure:
            files.reviews.approve_review(actor, pending["id"])
        assert failure.value.code == "REVIEW_RECOVERY_FAILED"
    files.recover_reviews()
    assert files.catalog.read_json(row["id"], "curve", project["id"], actor=actor) == {}
    assert files.reviews.get_review(actor, pending["id"])["id"] == pending["id"]
    assert not list(files.publisher.directory.iterdir())


@pytest.mark.parametrize("close_fails", [False, True])
def test_upload_preparation_failure_keeps_resource_owned_until_closed(hub, monkeypatch, close_fails):
    import projecthub.files.transfers as transfers

    files, _, actor, _, project = hub
    opened = []
    original_open = Path.open

    class TrackedStream:
        def __init__(self, stream):
            self.stream = stream
            self.fail_close = close_fails

        def close(self):
            if self.fail_close:
                self.fail_close = False
                raise OSError("injected upload close failure")
            self.stream.close()

    def track_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.name == "preparation.bin":
            stream = TrackedStream(stream)
            opened.append(stream)
        return stream

    def fail_identity(path):
        raise OSError("injected upload metadata failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", track_open)
        patch.setattr(transfers, "file_identity", fail_identity)
        with pytest.raises(OSError, match="metadata failure"):
            files.begin_upload(
                actor, project["id"], "public", "preparation.bin", {"description": ""}, "0" * 64
            )
    try:
        assert opened[0].stream.closed is (not close_fails)
        files.close()
        assert opened[0].stream.closed
        files.initialize()
        retry = files.begin_upload(
            actor, project["id"], "public", "preparation.bin", {"description": ""}, "0" * 64
        )
        retry.close()
    finally:
        opened[0].stream.close()


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("failure", ["stage", "journal"])
def test_rejected_review_cleanup_is_committed_and_recoverable(hub, monkeypatch, delete, failure):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "rejection.json", b"{}", {"curve_type": "c", "description": ""}
    )
    if delete:
        row = files.reviews.approve_review(actor, pending["id"])
        pending = files.reviews.request_curve_delete(actor, project["id"], row["id"])
    stage = files.settings.storage.audit_dir / pending["path"]
    original_unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if failure == "stage" and path == stage or failure == "journal" and path.name == "operation.json":
            raise PermissionError("injected rejected-review cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cleanup)
        assert files.reviews.reject_review(actor, pending["id"]) == {
            "review_id": pending["id"],
            "status": "rejected",
        }
    assert files.db.one("SELECT id FROM pending_reviews WHERE id=?", (pending["id"],)) is None
    assert files.recovery_required
    with pytest.raises(HubError) as caught:
        files.reviews.list_reviews(actor)
    assert caught.value.code == "REVIEW_RECOVERY_FAILED"
    files.close()
    files.initialize()
    assert not files.recovery_required
    assert not stage.exists()
    assert not list(files.publisher.directory.iterdir())
    if delete:
        assert files.catalog.read_json(row["id"], "curve", project["id"], actor=actor) == {}
        files.reviews.request_curve_delete(actor, project["id"], row["id"])
    else:
        upload(
            files,
            actor,
            project["id"],
            "curve",
            "rejection.json",
            b"{}",
            {"curve_type": "c", "description": ""},
        )


def test_committed_publication_blocks_on_recovery_validation_error(hub, monkeypatch):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "validation.json", b"{}", {"curve_type": "c", "description": ""}
    )
    original_cleanup = files.publisher._cleanup

    def unexpected_artifact(folder):
        (folder / "unknown").write_bytes(b"preserve")
        original_cleanup(folder)

    with monkeypatch.context() as patch:
        patch.setattr(files.publisher, "_cleanup", unexpected_artifact)
        row = files.reviews.approve_review(actor, pending["id"])
    assert files.recovery_required
    with pytest.raises(HubError) as caught:
        files.set_curve_position(actor, project["id"], row["id"], 1)
    assert caught.value.code == "REVIEW_RECOVERY_FAILED"
    unknown = list(files.publisher.directory.glob("*/unknown"))
    assert len(unknown) == 1 and unknown[0].read_bytes() == b"preserve"
    unknown[0].unlink()
    files.recover_reviews()
    assert files.catalog.read_json(row["id"], "curve", project["id"], actor=actor) == {}


def test_synchronization_refreshes_unchanged_curve_modification_time(hub):
    import os

    files, _, actor, _, project = hub
    pending = upload(
        files, actor, project["id"], "curve", "mtime.json", b"{}", {"curve_type": "c", "description": ""}
    )
    row = files.reviews.approve_review(actor, pending["id"])
    path = files.settings.storage.object_dir / row["path"]
    os.utime(path, ns=(1700000000123456789, 1700000000123456789))
    files.synchronizer.synchronize()
    refreshed = files.catalog.list_files(project_id=project["id"], category="curve")["files"][0]
    assert refreshed["id"] == row["id"]
    assert refreshed["sha256"] == row["sha256"]
    assert refreshed["modified_at"] == "2023-11-14T22:13:20.123456Z"


def test_rejection_sql_rollback_preserves_review_and_stage(hub, monkeypatch):
    from contextlib import contextmanager

    files, _, actor, _, project = hub
    pending = upload(
        files,
        actor,
        project["id"],
        "curve",
        "reject-rollback.json",
        b"{}",
        {"curve_type": "c", "description": ""},
    )
    transaction = files.db.transaction

    @contextmanager
    def interrupted_commit():
        with transaction() as connection:
            yield connection
            raise OSError("injected rejection commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(files.db, "transaction", interrupted_commit)
        with pytest.raises(OSError, match="commit failure"):
            files.reviews.reject_review(actor, pending["id"])
    assert files.reviews.get_review(actor, pending["id"])["id"] == pending["id"]
    assert (files.settings.storage.audit_dir / pending["path"]).read_bytes() == b"{}"
    assert not files.recovery_required
    assert not list(files.publisher.directory.iterdir())


@pytest.mark.parametrize("operation", ["reject", "approve"])
def test_review_recovery_preserves_replaced_stage(hub, monkeypatch, operation):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files,
        actor,
        project["id"],
        "curve",
        "reject-foreign.json",
        b"{}",
        {"curve_type": "c", "description": ""},
    )
    stage = files.settings.storage.audit_dir / pending["path"]
    original_unlink = Path.unlink

    def fail_stage(path, *args, **kwargs):
        if path == stage:
            raise PermissionError("injected rejection cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_stage)
        action = files.reviews.reject_review if operation == "reject" else files.reviews.approve_review
        action(actor, pending["id"])
    saved = stage.with_name("saved-rejection.json")
    stage.rename(saved)
    stage.write_bytes(b'{"foreign":true}')
    with pytest.raises(HubError) as caught:
        files.recover_reviews()
    assert caught.value.code == "REVIEW_RECOVERY_FAILED"
    assert stage.read_bytes() == b'{"foreign":true}'
    assert files.recovery_required
    stage.unlink()
    saved.rename(stage)
    files.recover_reviews()
    assert not stage.exists()
    assert not files.recovery_required


def test_approved_delete_stage_cleanup_is_committed_and_recoverable(hub, monkeypatch):
    from projecthub.contracts import HubError

    files, _, actor, _, project = hub
    pending = upload(
        files,
        actor,
        project["id"],
        "curve",
        "delete-stage.json",
        b"{}",
        {"curve_type": "c", "description": ""},
    )
    row = files.reviews.approve_review(actor, pending["id"])
    pending = files.reviews.request_curve_delete(actor, project["id"], row["id"])
    stage = files.settings.storage.audit_dir / pending["path"]
    original_unlink = Path.unlink

    def fail_stage(path, *args, **kwargs):
        if path == stage:
            raise PermissionError("injected approved-delete stage cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_stage)
        assert files.reviews.approve_review(actor, pending["id"])["status"] == "deleted"
    assert files.db.one("SELECT id FROM pending_reviews WHERE id=?", (pending["id"],)) is None
    assert files.db.one("SELECT id FROM curve_configs WHERE id=?", (row["id"],)) is None
    assert files.recovery_required
    with pytest.raises(HubError) as caught:
        files.reviews.list_reviews(actor)
    assert caught.value.code == "REVIEW_RECOVERY_FAILED"
    files.recover_reviews()
    assert not stage.exists()
    assert not list(files.publisher.directory.iterdir())
    upload(
        files,
        actor,
        project["id"],
        "curve",
        "delete-stage.json",
        b"{}",
        {"curve_type": "c", "description": ""},
    )
