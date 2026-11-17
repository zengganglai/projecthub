import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from projecthub.contracts import HubError
from projecthub.database import Database
from projecthub.permissions import Permissions, permission_catalog
from projecthub.users import Users, UserStore


def settings(root, *, max_users=200):
    root = root.resolve()
    return SimpleNamespace(
        database=SimpleNamespace(path=root / "sql" / "hub.sqlite3", busy_timeout_ms=150),
        storage=SimpleNamespace(backups_dir=root / "backups"),
        migrations=SimpleNamespace(lock_path=root / "sql" / "migration.lock"),
        users=SimpleNamespace(max_users=max_users),
    )


@pytest.fixture
def db(tmp_path):
    database = Database(settings(tmp_path))
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def user_store(db):
    store = UserStore(db)
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def permissions(db, user_store):
    return Permissions(db, user_store)


@pytest.fixture
def users(db, permissions):
    return Users(db, permissions)


@pytest.fixture
def admin(db, users):
    admin_id = db.one("SELECT id FROM users WHERE is_admin=1")["id"]
    return {"user_id": admin_id}


def add_project(db, name, version):
    with db.transaction() as connection:
        connection.execute("INSERT OR IGNORE INTO project_models (name) VALUES (?)", (name,))
        return connection.execute(
            "INSERT INTO projects (name, version) VALUES (?, ?)", (name, version)
        ).lastrowid


def expect_code(code, action, *args):
    with pytest.raises(HubError) as caught:
        action(*args)
    assert caught.value.code == code


def test_reopen_preserves_changed_credentials_and_deleted_seed(db, users, admin):
    users.change_password(admin, "888888", "  密码 α  ")
    tool = users.authenticate("tool", "123456")
    users.delete_user(admin, tool["user_id"])
    name = db.one("SELECT name FROM users WHERE id=?", (admin["user_id"],))["name"]

    db.close()
    db.open()
    users.user_store.initialize()

    assert db.one("SELECT version_num FROM alembic_version") == {"version_num": "0004"}
    assert users.authenticate(name, "  密码 α  ")["is_admin"] is True
    expect_code("AUTHENTICATION_FAILED", users.authenticate, name, "888888")
    expect_code("AUTHENTICATION_FAILED", users.authenticate, "tool", "123456")


def test_credentials_keep_name_case_and_nonempty_whitespace(users, admin):
    created = users.create_user(admin, {"name": " Test ", "password": " 密码 ", "employee_number": None})
    assert users.authenticate(" Test ", " 密码 ") == created
    users.create_user(admin, {"name": " test ", "password": "different", "employee_number": None})
    expect_code("AUTHENTICATION_FAILED", users.authenticate, "TEST", " 密码 ")
    expect_code("AUTHENTICATION_FAILED", users.authenticate, " Test ", "密码")
    expect_code("INVALID_DATA", users.change_password, created, " 密码 ", "　")
    expect_code("AUTHENTICATION_FAILED", users.change_password, created, "wrong", "new")
    renamed = users.update_user(admin, created["user_id"], {"name": "工程师"})
    assert users.authenticate("工程师", " 密码 ") == renamed
    expect_code("USER_CONFLICT", users.create_user, admin, {"name": "工程师"})
    expect_code("INVALID_DATA", users.create_user, admin, {"name": "　"})


def test_live_permission_recheck_and_full_replacement(db, users, permissions, admin):
    first = add_project(db, "M", "v1")
    second = add_project(db, "M", "v2")
    other = add_project(db, "Other", "v1")
    actor = users.create_user(admin, {"name": "reader"})
    assert actor["global_permissions"] == []
    expect_code("PERMISSION_DENIED", permissions.require_file_download, actor, "firmware", first)
    expect_code("PERMISSION_DENIED", permissions.require_file_download, actor, "public", second)
    expect_code("PERMISSION_DENIED", permissions.require_admin, {**actor, "is_admin": True})
    expect_code(
        "PERMISSION_DENIED",
        permissions.require,
        {**actor, "global_permissions": ["curve.manage"]},
        "curve.manage",
        first,
    )

    user_id = actor["user_id"]
    configured = permissions.set_user_permissions(
        admin, user_id, [], {"curve.read": ["M"], "curve.manage": ["M"]}
    )
    assert configured == {
        "user_id": user_id,
        "global_permissions": [],
        "project_permissions": {"curve.manage": ["M"], "curve.read": ["M"]},
    }
    assert permissions.require(actor, "curve.manage", second)["id"] == second
    permissions.require_file_download(actor, "curve", first)
    expect_code("PERMISSION_DENIED", permissions.require_file_download, actor, "curve", other)
    expect_code("PERMISSION_DENIED", permissions.require, actor, "curve.manage", other)
    expect_code(
        "PROJECT_NOT_FOUND",
        permissions.set_user_permissions,
        admin,
        user_id,
        [],
        {"curve.manage": ["Missing"]},
    )
    expect_code(
        "INVALID_DATA", permissions.set_user_permissions, admin, user_id, [], {"curve.manage": ["M", "M"]}
    )
    expect_code("INVALID_DATA", permissions.set_user_permissions, admin, user_id, ["dealer"], {})
    assert permissions.get_user_permissions(admin, user_id) == configured

    permissions.set_user_permissions(admin, user_id, [], {})
    expect_code("PERMISSION_DENIED", permissions.require, actor, "curve.manage", second)
    expect_code("PERMISSION_DENIED", permissions.require_file_download, actor, "curve", first)
    expect_code("PERMISSION_DENIED", permissions.require_file_download, actor, "firmware", first)


def test_user_removal_protects_admin_project_grants_and_pending_submitters(db, users, permissions, admin):
    project_id = add_project(db, "M", "v1")
    reviewer = users.create_user(admin, {"name": "reviewer"})
    author = users.create_user(admin, {"name": "author"})
    reviewer_id, author_id = reviewer["user_id"], author["user_id"]
    expect_code("USER_REFERENCED", users.delete_user, admin, admin["user_id"])
    permissions.set_user_permissions(admin, author_id, [], {"curve.review": ["M"]})
    expect_code("USER_REFERENCED", users.delete_user, admin, author_id)
    permissions.set_user_permissions(admin, author_id, [], {})

    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO pending_reviews (project_id, reviewer_user_id, uploaded_at, file_name, path, sha256, field_info)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                reviewer_id,
                "now",
                "a.json",
                "audit/a.json",
                "0" * 64,
                json.dumps({"category": "curve", "uploaded_by": author_id}),
            ),
        )

    expect_code("USER_REFERENCED", users.delete_user, admin, author_id)
    users.delete_user(admin, reviewer_id)
    assert db.one("SELECT reviewer_user_id FROM pending_reviews") == {"reviewer_user_id": None}
    expect_code("PERMISSION_DENIED", permissions.permissions_for, reviewer, project_id)
    expect_code("INVALID_USER", users.delete_user, admin, True)


def test_user_limit_rejects_overflow_without_writing(db, admin):
    store = UserStore(db, max_users=2)
    store.initialize()
    users = Users(db, Permissions(db, store))
    expect_code("USER_LIMIT_REACHED", users.create_user, admin, {"name": "extra"})
    assert [row["user_id"] for row in users.list_users(admin)] == [1, 2]
    store.close()


def test_permission_catalog_preserves_order_and_scopes():
    rows = permission_catalog()
    assert [item["code"] for item in rows] == [
        "factory.debug",
        "dealer",
        "curve.read",
        "curve.manage",
        "curve.review",
        "firmware.read",
        "firmware.upload",
        "firmware.review",
        "internal.read",
        "internal.manage",
        "public.read",
        "public.manage",
        "authorization.list",
        "authorization.manage",
    ]
    assert all(item["scopes"] == ["project"] for item in rows)
    rows[0]["scopes"].clear()
    assert permission_catalog()[0]["scopes"] == ["project"]


def test_nested_transaction_is_rolled_back_independently(db):
    with db.transaction() as connection:
        connection.execute("INSERT INTO project_models (name) VALUES ('first')")
        with pytest.raises(ValueError, match="abort"), db.transaction() as nested:
            nested.execute("INSERT INTO project_models (name) VALUES ('abort')")
            raise ValueError("abort")
        connection.execute("INSERT INTO project_models (name) VALUES ('second')")
    assert db.query("SELECT name FROM project_models ORDER BY id") == [{"name": "first"}, {"name": "second"}]


@pytest.mark.parametrize("damage", ["0001", "9000", "missing index", "malformed"])
def test_unknown_or_damaged_database_is_rejected_without_schema_repair(tmp_path, damage):
    config = settings(tmp_path)
    database = Database(config)
    database.open()
    database.close()
    if damage == "malformed":
        config.database.path.write_bytes(b"this is not a sqlite database")
    else:
        with closing(sqlite3.connect(config.database.path)) as connection, connection:
            if damage == "missing index":
                connection.execute("DROP INDEX ix_firmwares_project_type")
            else:
                connection.execute("UPDATE alembic_version SET version_num=?", (damage,))
    before = config.database.path.read_bytes()
    expect_code("DATABASE_MIGRATION_ERROR", database.open)
    assert config.database.path.read_bytes() == before
    assert not config.storage.backups_dir.exists()


def test_download_authorization_schema_rejects_codes_outside_the_current_format(db):
    project_id = add_project(db, "M", "V")
    query = (
        "INSERT INTO download_authorizations(created_by,authorization_code,project_id,path,expires_at,sha256)"
        " VALUES('author',?,?,'M/V/public/a.bin','future',?)"
    )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as connection:
        connection.execute(query, ("a" * 43, project_id, "0" * 64))
    with db.transaction() as connection:
        connection.execute(query, ("a" * 16, project_id, "0" * 64))
    assert db.query("SELECT authorization_code FROM download_authorizations") == [
        {"authorization_code": "a" * 16}
    ]


def test_initial_catalog_failure_can_be_retried_as_new_database(tmp_path):
    database = Database(settings(tmp_path))

    def failed_scan():
        add_project(database, "Partial", "V")
        raise HubError("INVALID_DATA", "scan failed")

    with pytest.raises(HubError, match="scan failed"):
        database.open(initialize=failed_scan)
    assert database.migration_report is None
    database.open()
    try:
        assert database.migration_report.from_revision is None
        assert database.query("SELECT name FROM project_models") == []
    finally:
        database.close()


def test_migration_lock_excludes_another_opener(tmp_path):
    config = settings(tmp_path)
    database, competing = Database(config), Database(config)
    database.open(initialize=lambda: expect_code("DATABASE_MIGRATION_LOCKED", competing.open))
    try:
        competing.open()
        assert competing.one("SELECT version_num FROM alembic_version") == {"version_num": "0004"}
    finally:
        competing.close()
        database.close()


def test_startup_validates_model_grants_and_ignores_unused_global_column(db, permissions):
    with db.transaction() as connection:
        connection.execute("UPDATE users SET permissions=? WHERE name='tool'", ('["unknown.permission"]',))
    permissions.validate()
    assert permissions.identity(db.one("SELECT * FROM users WHERE name='tool'"))["global_permissions"] == []
    with db.transaction() as connection:
        connection.execute(
            "UPDATE users SET permissions='[]', project_permissions=? WHERE name='tool'",
            ('{"curve.review":["missing"]}',),
        )
    permissions.user_store.close()
    permissions.user_store.initialize()
    expect_code("USER_STORE_ERROR", permissions.validate)


def test_migration_lock_open_failure_remains_a_database_error(tmp_path):
    config = settings(tmp_path)
    config.migrations.lock_path.mkdir(parents=True)
    database = Database(config)
    expect_code("DATABASE_MIGRATION_ERROR", database.open)
    expect_code("DATABASE_NOT_READY", lambda: database.connection)
    assert database.migration_report is None
