import hashlib
import json
import sqlite3

import pytest

from projecthub.application import AppConfig, resolve_settings
from projecthub.contracts import HubError
from projecthub.database import Database
from projecthub.files import FileService
from projecthub.permissions import Permissions
from projecthub.projects import ProjectService
from projecthub.users import Users, UserStore


@pytest.fixture
def service(tmp_path):
    settings = resolve_settings(AppConfig(project_root=tmp_path))
    db = Database(settings)
    cache = UserStore(db)
    permissions = Permissions(db, cache)
    files = FileService(settings, db, permissions)
    projects = ProjectService(settings, db, permissions, files)
    try:
        db.open()
        cache.initialize()
        permissions.validate()
        projects.initialize()
        files.initialize()
        yield projects
    finally:
        files.close()
        cache.close()
        db.close()


@pytest.fixture
def users(service):
    return Users(service.db, service.permissions)


def expect_code(code, action, *args, **kwargs):
    with pytest.raises(HubError) as caught:
        action(*args, **kwargs)
    assert caught.value.code == code


def test_create_version_reuses_existing_safe_folder_and_rolls_back_path_conflict(service):
    actor = {"user_id": 1}
    root = service.settings.storage.object_dir
    expect_code("PROJECT_NOT_FOUND", service.create_project_version, "Missing", "V1", actor=actor)
    model = service.create_project_model(" Model ", "型号说明", actor=actor)
    assert model == {"id": 1, "name": "Model", "description": "型号说明"}
    assert list((root / "Model").iterdir()) == []
    blocked = root / "Model" / "Blocked"
    blocked.mkdir()
    (blocked / "firmware").write_text("external")
    expect_code("PROJECT_PATH_EXISTS", service.create_project_version, "Model", "Blocked", actor=actor)
    assert (blocked / "firmware").read_text() == "external"
    assert not (blocked / "curve").exists()
    assert service.list_projects(actor) == []

    row = service.create_project_version("Model", " V1 ", "版本说明", actor=actor)
    assert row == {"id": 1, "name": "Model", "version": "V1", "description": "版本说明"}
    assert {path.name for path in (root / "Model" / "V1").iterdir()} == {
        "curve",
        "firmware",
        "internal",
        "public",
    }
    expect_code("PROJECT_EXISTS", service.create_project_version, "Model", "V1", actor=actor)


def test_renaming_model_updates_formal_review_and_download_paths_and_grants(service, users):
    admin = {"user_id": 1}
    storage = service.settings.storage
    model = service.create_project_model("A", actor=admin)
    first = service.create_project_version("A", "V1", actor=admin)
    second = service.create_project_version("A", "V2", actor=admin)
    user = users.create_user(admin, {"name": "reviewer"})
    uid = user["user_id"]
    service.permissions.set_user_permissions(admin, uid, [], {"curve.review": ["A"]})

    content = b'{"x":1}'
    digest = hashlib.sha256(content).hexdigest()
    formal = "A/V1/curve/example.json"
    (storage.object_dir / formal).write_bytes(content)
    handle = service.files.begin_upload(
        admin,
        second["id"],
        "curve",
        "pending.json",
        {"curve_name": "test", "curve_type": "c", "description": ""},
        digest,
    )
    try:
        handle.write(content)
        pending_review = handle.finish()
    finally:
        handle.close()
    with service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO curve_configs(project_id,curve_name,file_name,sha256,path) VALUES(?,?,?,?,?)",
            (first["id"], "test", "example.json", digest, formal),
        )
        connection.execute(
            "INSERT INTO download_authorizations(created_by,authorization_code,project_id,path,expires_at,sha256)"
            " VALUES(?,?,?,?,?,?)",
            ("admin", "0123456789abcdef", first["id"], formal, "future", digest),
        )

    updated = service.update_project_model("A", {"name": "B", "description": "new"}, actor=admin)
    assert updated == {"id": model["id"], "name": "B", "description": "new"}
    assert {row["name"] for row in service.list_projects(admin)} == {"B"}
    assert service.permissions.get_user_permissions(admin, uid)["project_permissions"] == {
        "curve.review": ["B"]
    }
    service.permissions.require(user, "curve.review", second["id"])
    cached = service.permissions.user_store.get(uid)
    assert json.loads(cached["project_permissions"]) == {"curve.review": ["B"]}
    assert service.db.one("SELECT * FROM users WHERE id=?", (uid,)) == cached
    assert service.db.one("SELECT path FROM curve_configs")["path"] == "B/V1/curve/example.json"
    assert service.db.one("SELECT path FROM download_authorizations")["path"] == "B/V1/curve/example.json"
    review = service.db.one("SELECT path, field_info FROM pending_reviews")
    assert review["path"] == "B/V2/curve/pending.json"
    assert json.loads(review["field_info"]) == pending_review["field_info"]
    assert (storage.object_dir / "B/V1/curve/example.json").read_bytes() == content
    assert (storage.audit_dir / "B/V2/curve/pending.json").read_bytes() == content
    for root, suffix in (
        (storage.object_dir, "V1/curve/example.json"),
        (storage.audit_dir, "V2/curve/pending.json"),
    ):
        (history,) = root.glob("A-mod_*")
        assert (history / suffix).read_bytes() == content
        assert not (root / "A").exists()


def test_rename_version_moves_only_selected_directory_and_keeps_stable_id(service):
    admin = {"user_id": 1}
    model = service.create_project_model("A", actor=admin)
    first = service.create_project_version("A", "V1", actor=admin)
    second = service.create_project_version("A", "V2", actor=admin)
    root = service.settings.storage.object_dir / "A"
    (root / "V1/config.json").write_text('{"version":1}', encoding="utf-8")
    result = service.update_project(first["id"], {"version": "New", "description": "changed"}, actor=admin)

    assert result == {
        "id": first["id"],
        "name": "A",
        "version": "New",
        "reviewer_user_id": None,
        "description": "changed",
    }
    assert service.get_project_version(model["id"], first["id"]) == result
    assert service.get_project_version(model["id"], second["id"])["version"] == "V2"
    assert service.files.catalog.read_project_config(first["id"]) == {"version": 1}
    assert len(list(root.glob("V1-mod_*"))) == 1
    assert (root / "V2").is_dir()


def test_audit_publish_failure_rolls_back_formal_folder_sql_and_users(service, users, monkeypatch):
    from projecthub.projects import renames

    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    version = service.create_project_version("Before", "V1", actor=admin)
    storage = service.settings.storage
    (storage.audit_dir / "Before/V1").mkdir(parents=True)
    user = users.create_user(admin, {"name": "reviewer"})
    uid = user["user_id"]
    service.permissions.set_user_permissions(admin, uid, [], {"curve.review": ["Before"]})
    cache = service.permissions.user_store
    original_user = cache.get(uid)
    move = renames._move_directory

    def fail_audit_target(source, target):
        if target == storage.audit_dir / "After":
            assert json.loads(cache.get(uid)["project_permissions"]) == {"curve.review": ["After"]}
            raise OSError("synthetic directory sharing failure")
        return move(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(renames, "_move_directory", fail_audit_target)
        expect_code(
            "PROJECT_DIRECTORY_ERROR", service.update_project_model, "Before", {"name": "After"}, actor=admin
        )

    assert service.db.one("SELECT name FROM projects WHERE id=?", (version["id"],)) == {"name": "Before"}
    assert cache.get(uid) == original_user
    assert service.db.one("SELECT * FROM users WHERE id=?", (uid,)) == original_user
    service.permissions.require(user, "curve.review", version["id"])
    assert not service.files.recovery_required
    for root in (storage.object_dir, storage.audit_dir):
        assert (root / "Before/V1").is_dir()
        assert not (root / "After").exists()
        assert not list(root.glob("Before-mod_*"))


def test_failed_rename_commit_restores_user_cache_database_and_directory(service, users):
    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    version = service.create_project_version("Before", "V1", actor=admin)
    user = users.create_user(admin, {"name": "reviewer"})
    uid = user["user_id"]
    service.permissions.set_user_permissions(admin, uid, [], {"curve.review": ["Before"]})
    cache = service.permissions.user_store
    original_user = cache.get(uid)
    attempts = []

    def reject_commit(action, argument, _second, _database, _source):
        if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
            attempts.append(cache.get(uid))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    service.db.connection.set_authorizer(reject_commit)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            service.update_project_model("Before", {"name": "After"}, actor=admin)
    finally:
        service.db.connection.set_authorizer(None)

    assert len(attempts) == 1
    assert json.loads(attempts[0]["project_permissions"]) == {"curve.review": ["After"]}
    assert cache.get(uid) == original_user
    assert service.db.one("SELECT * FROM users WHERE id=?", (uid,)) == original_user
    service.permissions.require(user, "curve.review", version["id"])
    assert service.db.one("SELECT name FROM project_models") == {"name": "Before"}
    root = service.settings.storage.object_dir
    assert (root / "Before/V1").is_dir()
    assert not (root / "After").exists()
    assert not list(root.glob("Before-mod_*"))
    assert not service.files.recovery_required


def test_failed_rename_rollback_retains_journal_until_transaction_finishes(service, users):
    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    version = service.create_project_version("Before", "V1", actor=admin)
    user = users.create_user(admin, {"name": "reviewer"})
    uid = user["user_id"]
    service.permissions.set_user_permissions(admin, uid, [], {"curve.review": ["Before"]})
    cache = service.permissions.user_store
    original_user = cache.get(uid)
    root = service.settings.storage.object_dir
    (root / "Before/V1/public/original.txt").write_bytes(b"original")
    journal_dir = service.settings.storage.backups_dir / ".project-renames"
    attempts = []

    def reject_commit_and_rollback(action, argument, _second, _database, _source):
        if action == sqlite3.SQLITE_TRANSACTION and argument in ("COMMIT", "ROLLBACK"):
            attempts.append(argument)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    service.db.connection.set_authorizer(reject_commit_and_rollback)
    try:
        expect_code(
            "PROJECT_RESTORE_FAILED", service.update_project_model, "Before", {"name": "After"}, actor=admin
        )
        assert attempts == ["COMMIT", "ROLLBACK"]
        assert not cache.ready
        assert service.db.connection.in_transaction
        assert service.files.recovery_required
        (journal,) = journal_dir.glob("*.json")
        manifest = journal.read_bytes()
        assert (root / "After/V1/public/original.txt").read_bytes() == b"original"
        expect_code("REVIEW_RECOVERY_FAILED", service.recover)
        assert service.files.recovery_required
        assert journal.read_bytes() == manifest
        assert service.db.connection.in_transaction
    finally:
        service.db.connection.set_authorizer(None)
        if service.db.connection.in_transaction:
            service.db.connection.execute("ROLLBACK")

    cache.initialize()
    service.permissions.validate()
    service.recover()
    assert cache.get(uid) == original_user
    assert service.db.one("SELECT * FROM users WHERE id=?", (uid,)) == original_user
    service.permissions.require(user, "curve.review", version["id"])
    assert service.db.one("SELECT name FROM project_models") == {"name": "Before"}
    assert service.db.one("SELECT name FROM projects WHERE id=?", (version["id"],)) == {"name": "Before"}
    assert (root / "Before/V1/public/original.txt").read_bytes() == b"original"
    assert not (root / "After").exists()
    assert not list(root.glob("Before-mod_*"))
    assert not list(journal_dir.glob("*.json"))
    assert not service.files.recovery_required


def test_recovery_without_journal_keeps_gate_closed_until_transaction_finishes(service):
    service.files.mark_recovery_required()
    with service.db.transaction():
        expect_code("REVIEW_RECOVERY_FAILED", service.recover)
        assert service.files.recovery_required
    service.recover()
    assert not service.files.recovery_required


def test_restart_finishes_committed_rename_when_manifest_cleanup_was_interrupted(service, monkeypatch):
    from projecthub.projects import renames

    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    root = service.settings.storage.object_dir

    def retain_journal(_path):
        raise OSError("synthetic cleanup interruption")

    with monkeypatch.context() as patch:
        patch.setattr(renames, "_remove_journal", retain_journal)
        service.update_project_model("Before", {"name": "After"}, actor=admin)

    journal_dir = service.settings.storage.backups_dir / ".project-renames"
    assert len(list(journal_dir.glob("*.json"))) == 1
    service.files.close()
    service.initialize()
    service.files.initialize()
    assert (root / "After").is_dir()
    assert len(list(root.glob("Before-mod_*"))) == 1
    assert not list(journal_dir.glob("*.json"))


def test_restart_restores_uncommitted_original_without_removing_foreign_target(service, monkeypatch):
    from projecthub.projects import renames

    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    root = service.settings.storage.object_dir
    (root / "Before/original.txt").write_bytes(b"original")
    write_directories = renames._publish_rename

    def interrupted(path, manifest, directories):
        write_directories(path, manifest, directories)
        raise OSError("synthetic interruption before SQL commit")

    def unavailable(*_):
        raise OSError("synthetic interruption during recovery")

    with monkeypatch.context() as patch:
        patch.setattr(renames, "_publish_rename", interrupted)
        patch.setattr(renames, "_recover_manifest", unavailable)
        expect_code(
            "PROJECT_RESTORE_FAILED", service.update_project_model, "Before", {"name": "After"}, actor=admin
        )

    assert service.files.recovery_required
    assert service.db.one("SELECT name FROM project_models") == {"name": "Before"}
    assert not (root / "Before").exists()
    (root / "After").rename(root / "untouched-copy")
    (root / "After").mkdir()
    (root / "After/foreign.txt").write_bytes(b"foreign")

    service.files.close()
    service.permissions.user_store.close()
    service.db.close()
    service.db.open()
    service.permissions.user_store.initialize()
    service.permissions.validate()
    service.initialize()
    service.files.initialize()
    assert not service.files.recovery_required
    assert (root / "Before/original.txt").read_bytes() == b"original"
    assert (root / "After/foreign.txt").read_bytes() == b"foreign"
    assert not list(root.glob("Before-mod_*"))
    assert not list((service.settings.storage.backups_dir / ".project-renames").glob("*.json"))


def test_rename_leaves_an_active_audit_upload_at_original_path(service):
    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)
    project = service.create_project_version("Before", "V1", actor=admin)
    body = b'{"test":1}'
    upload = service.files.begin_upload(
        admin,
        project["id"],
        "curve",
        "new.json",
        {"curve_name": "test", "curve_type": "test", "description": ""},
        hashlib.sha256(body).hexdigest(),
    )
    try:
        upload.write(body)
        expect_code(
            "PROJECT_DIRECTORY_ERROR", service.update_project_model, "Before", {"name": "After"}, actor=admin
        )
        assert upload.finish()["path"] == "Before/V1/curve/new.json"
    finally:
        upload.close()


def test_retained_rename_journal_staging_name_cannot_become_a_project(service, monkeypatch):
    from types import SimpleNamespace

    from projecthub.projects import renames

    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)

    def retain_journal(_path):
        raise OSError("synthetic cleanup interruption")

    with monkeypatch.context() as patch:
        patch.setattr(renames, "_remove_journal", retain_journal)
        patch.setattr(renames, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
        service.update_project_model("Before", {"name": "After"}, actor=admin)

    (journal,) = (service.settings.storage.backups_dir / ".project-renames").glob("*.json")
    stage = json.loads(journal.read_bytes())["directories"][0]["stage"]
    expect_code("INVALID_PROJECT_NAME", service.create_project_model, stage, actor=admin)
    service.recover()
    assert not journal.exists()


def test_model_and_version_lookup_include_independent_models(service):
    admin = {"user_id": 1}
    service.create_project_model("Beta", actor=admin)
    version_id = service.create_project_version("Beta", "V1", actor=admin)["id"]
    service.create_project_model("Alpha", "暂无版本", actor=admin)

    alpha, beta = service.list_project_models(admin)
    assert (alpha["name"], alpha["description"], beta["name"]) == ("Alpha", "暂无版本", "Beta")
    assert service.get_project_model(alpha["id"]) == alpha
    assert service.get_project_version(alpha["id"], version_id) is None
    version = service.get_project_version(beta["id"], version_id)
    assert version == {
        "id": version_id,
        "name": "Beta",
        "version": "V1",
        "description": "",
        "reviewer_user_id": None,
    }
    assert service.list_projects(admin) == [version]


def test_project_queries_reject_stale_actor_and_invalid_identifiers(service, users):
    admin = {"user_id": 1}
    actor = users.create_user(admin, {"name": "temporary"})
    users.delete_user(admin, actor["user_id"])

    expect_code("PERMISSION_DENIED", service.list_project_models, actor)
    expect_code("PERMISSION_DENIED", service.list_projects, actor)
    for identifier in (None, True, 0, -1, 2**63, "1"):
        assert service.get_project_model(identifier) is None
        assert service.get_project_version(1, identifier) is None
        assert service.get_project_version(identifier, 1) is None


def test_project_version_requires_name_and_admin_at_mutation(service, users):
    admin = {"user_id": 1}
    service.create_project_model("Model", actor=admin)
    expect_code("INVALID_PROJECT_NAME", service.create_project_version, "Model", None, actor=admin)
    actor = users.create_user(admin, {"name": "reader"})
    expect_code("PERMISSION_DENIED", service.create_project_model, "Blocked", actor=actor)
    expect_code("PERMISSION_DENIED", service.update_project_model, "Model", {"name": "Blocked"}, actor=actor)
    assert [row["name"] for row in service.list_project_models()] == ["Model"]


def test_failed_project_recovery_blocks_files_until_retried(service, monkeypatch):
    from projecthub.projects import renames

    admin = {"user_id": 1}
    service.create_project_model("Before", actor=admin)

    def retain_journal(_path):
        raise OSError("synthetic cleanup interruption")

    with monkeypatch.context() as patch:
        patch.setattr(renames, "_remove_journal", retain_journal)
        service.update_project_model("Before", {"name": "After"}, actor=admin)
        assert service.files.recovery_required
        service.files.recover_reviews()
        assert service.files.recovery_required
        expect_code("REVIEW_RECOVERY_FAILED", service.recover)
        assert service.files.recovery_required
        expect_code("REVIEW_RECOVERY_FAILED", service.create_project_model, "Later", actor=admin)
        assert [row["name"] for row in service.list_project_models()] == ["After"]

    service.create_project_model("Later", actor=admin)
    assert not service.files.recovery_required
    assert [row["name"] for row in service.list_project_models()] == ["After", "Later"]
