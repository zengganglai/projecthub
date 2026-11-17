"""RAM 用户缓存的读路径、事务、边界及生命周期回归。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from projecthub.contracts import HubError
from projecthub.database import Database
from projecthub.users import UserStore


@pytest.fixture
def db(tmp_path):
    config = SimpleNamespace(
        database=SimpleNamespace(path=tmp_path / "hub.sqlite3", busy_timeout_ms=150),
        storage=SimpleNamespace(backups_dir=tmp_path / "backups"),
        migrations=SimpleNamespace(lock_path=tmp_path / "migration.lock"),
    )
    database = Database(config)
    database.open()
    try:
        yield database
    finally:
        with database.lock:
            if database.connection.in_transaction:
                database.connection.execute("ROLLBACK")
        database.close()


@pytest.fixture
def store_type():
    return UserStore


@pytest.fixture
def store(db, store_type):
    cache = store_type(db)
    cache.initialize()
    return cache


def expect_code(code, action, *args):
    with pytest.raises(HubError) as caught:
        action(*args)
    assert caught.value.code == code


class FaultConnection:
    """把故障放在真实 SQLite 语句执行边界，保留事务真实结果。"""

    def __init__(self, connection):
        self.connection = connection
        self.fail_before = set()
        self.fail_after = set()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        command = sql.split()[0].upper()
        if command in self.fail_before:
            raise sqlite3.OperationalError(f"before {command}")
        result = self.connection.execute(sql, parameters)
        if command in self.fail_after:
            raise sqlite3.OperationalError(f"after {command}")
        return result


def test_construction_has_no_database_io_and_reads_require_initialization(db, store_type):
    statements = []
    db.connection.set_trace_callback(statements.append)
    cache = store_type(db)
    assert statements == []
    assert not cache.ready
    for action, args in ((cache.get, (1,)), (cache.find, ("tool",)), (cache.all, ())):
        expect_code("USERS_NOT_READY", action, *args)
    cache.initialize()
    first_load = list(statements)
    assert len([sql for sql in first_load if "FROM users" in sql]) == 1
    assert any("LIMIT 201" in sql for sql in first_load)
    assert any("sqlite_sequence" in sql for sql in first_load)
    cache.initialize()
    assert statements == first_load


def test_reads_are_ram_only_and_return_independent_copies(db, store):
    statements = []
    db.connection.set_trace_callback(statements.append)
    row = store.find("tool")
    assert store.get(row["id"])["name"] == "tool"
    row["password"] = "tampered"
    users = store.all()
    users[0]["is_admin"] = 0
    users.clear()
    assert store.find("tool")["password"] == "123456"
    assert store.get(1)["is_admin"] == 1
    assert len(store.all()) == 2
    assert store.get(9999) is None
    assert store.find("missing") is None
    assert statements == []


@pytest.mark.parametrize("limit", [0, -1, 201, True, 2.0, "200", None])
def test_invalid_configured_limit_is_rejected(db, store_type, limit):
    expect_code("USERS_CONFIGURATION_ERROR", store_type, db, limit)


def test_initialize_overflow_keeps_cache_unavailable(db, store_type):
    with db.transaction() as connection:
        connection.executemany("INSERT INTO users(name) VALUES(?)", [(f"user-{i}",) for i in range(199)])
    cache = store_type(db)
    expect_code("USER_LIMIT_REACHED", cache.initialize)
    assert not cache.ready
    expect_code("USERS_NOT_READY", cache.all)
    with db.transaction() as connection:
        connection.execute("DELETE FROM users WHERE name='user-198'")
    cache.initialize()
    assert len(cache.all()) == 200


@pytest.mark.parametrize("limit", [2, 200])
def test_create_enforces_limit_before_sql_and_returns_schema_defaults(db, store_type, limit):
    cache = store_type(db, limit)
    cache.initialize()
    with cache.transaction():
        for i in range(limit - 2):
            row = cache.create({"name": f"new-{i}"})
            assert row == {
                "id": i + 3,
                "name": f"new-{i}",
                "employee_number": "",
                "phone": "",
                "password": "",
                "permissions": "[]",
                "project_permissions": "{}",
                "is_admin": 0,
            }
    statements = []
    db.connection.set_trace_callback(statements.append)
    with cache.transaction():
        expect_code("USER_LIMIT_REACHED", cache.create, {"name": "overflow"})
    assert not any("INSERT INTO users" in sql for sql in statements)
    assert len(cache.all()) == limit


def test_writes_change_ram_before_dml_and_persist_without_user_selects(db, store):
    observed = []
    statements = []

    def trace(sql):
        statements.append(sql)
        if sql.startswith("INSERT INTO users"):
            observed.append(("create", store.find("engineer")["password"]))
        elif sql.startswith("UPDATE users"):
            observed.append(("update", store.find("renamed")["password"]))
            assert store.find("engineer") is None
        elif sql.startswith("DELETE FROM users"):
            observed.append(("delete", store.find("tool")))

    db.connection.set_trace_callback(trace)
    with store.transaction():
        created = store.create({"name": "engineer", "password": "old"})
        changed = store.update(created["id"], {"name": "renamed", "password": "new"})
        store.delete(2)
    assert observed == [("create", "old"), ("update", "new"), ("delete", None)]
    assert not any(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    db.connection.set_trace_callback(None)
    assert changed == db.one("SELECT * FROM users WHERE id=?", (created["id"],))
    assert db.one("SELECT * FROM users WHERE id=2") is None
    created["password"] = "mutated"
    changed["password"] = "mutated"
    assert store.find("renamed")["password"] == "new"


def test_rollback_restores_rows_names_password_and_next_id(db, store):
    before = store.all()
    with pytest.raises(RuntimeError, match="abort"), store.transaction():
        first = store.create({"name": "temporary"})
        store.update(1, {"name": "new-admin", "password": "new-password"})
        store.delete(2)
        raise RuntimeError("abort")
    assert store.all() == before
    assert db.query("SELECT * FROM users ORDER BY id") == before
    with store.transaction():
        second = store.create({"name": "lasting"})
    assert first["id"] == second["id"] == 3


def test_committed_deleted_id_is_not_reused_across_initialize(db, store, store_type):
    with store.transaction():
        deleted = store.create({"name": "deleted"})
    with store.transaction():
        store.delete(deleted["id"])
    store.close()
    cache = store_type(db)
    cache.initialize()
    with cache.transaction():
        created = cache.create({"name": "next"})
    assert deleted["id"] == 3
    assert created["id"] == 4


def test_duplicate_names_rejected_without_losing_original_name_index(db, store):
    with store.transaction():
        expect_code("USER_CONFLICT", store.create, {"name": "tool"})
        expect_code("USER_CONFLICT", store.update, 1, {"name": "tool"})
        created = store.create({"name": "Tool"})
    assert created["id"] == 3
    assert store.find("tool")["id"] == 2
    assert store.get(1)["name"] == "曾刚来"
    assert len(db.query("SELECT id FROM users")) == 3


@pytest.mark.parametrize(
    "action,args", [("create", ({"name": "new"},)), ("update", (1, {"name": "new"})), ("delete", (1,))]
)
def test_mutations_require_store_owned_transaction(store, action, args):
    expect_code("USER_STORE_ERROR", getattr(store, action), *args)


def test_nested_and_external_transactions_are_rejected(db, store):
    with db.transaction(), pytest.raises(HubError, match="事务"):
        with store.transaction():
            pytest.fail("external transaction accepted")
    with store.transaction(), pytest.raises(HubError, match="事务"):
        with store.transaction():
            pytest.fail("nested transaction accepted")


@pytest.mark.parametrize("values", [{"id": 80, "name": "bad"}, {"name); DROP TABLE users; --": "bad"}])
def test_column_whitelist_rejects_untrusted_columns_before_mutation(store, values):
    before = store.all()
    with store.transaction():
        expect_code("INVALID_DATA", store.create, values)
        expect_code("INVALID_DATA", store.update, 1, values)
    assert store.all() == before


def test_constraint_failure_cannot_be_swallowed_to_commit_a_stale_cache(db, store):
    before = store.all()
    with pytest.raises(HubError), store.transaction():
        with pytest.raises(sqlite3.IntegrityError):
            store.update(2, {"project_permissions": "[]"})
    assert store.all() == before
    assert db.query("SELECT * FROM users ORDER BY id") == before


@pytest.mark.parametrize("rollback", [False, True])
def test_readers_cannot_observe_incomplete_transaction(store, rollback):
    started, finished = Event(), Event()

    def read_user():
        started.set()
        result = store.get(2)
        finished.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as workers:
        try:
            with store.transaction():
                store.update(2, {"password": "changed"})
                future = workers.submit(read_user)
                assert started.wait(2)
                assert not finished.wait(0.1)
                if rollback:
                    raise ValueError("abort")
        except ValueError:
            pass
        assert future.result(timeout=2)["password"] == ("123456" if rollback else "changed")


def test_close_clears_sensitive_state_and_reopen_requires_explicit_initialize(db, store):
    store.close()
    assert not store.ready
    expect_code("USERS_NOT_READY", store.all)
    store.initialize()
    db.close()
    assert not store.ready
    expect_code("USERS_NOT_READY", store.get, 1)
    db.open()
    assert not store.ready
    expect_code("USERS_NOT_READY", store.find, "tool")
    store.initialize()
    assert store.find("tool")["password"] == "123456"


@pytest.mark.parametrize("fault", ["before", "after"])
def test_commit_failure_recovers_actual_sqlite_result(db, store_type, fault):
    proxy = FaultConnection(db.connection)
    db._connection = proxy
    cache = store_type(db)
    cache.initialize()
    getattr(proxy, f"fail_{fault}").add("COMMIT")
    with pytest.raises(sqlite3.OperationalError), cache.transaction():
        cache.update(2, {"password": "changed"})
    assert cache.ready
    assert cache.get(2)["password"] == ("changed" if fault == "after" else "123456")
    assert cache.all() == db.query("SELECT * FROM users ORDER BY id")
    proxy.fail_before.clear()
    proxy.fail_after.clear()


def test_failed_rollback_blocks_stale_permissions_until_reinitialized(db, store_type):
    proxy = FaultConnection(db.connection)
    db._connection = proxy
    cache = store_type(db)
    cache.initialize()
    proxy.fail_before.add("ROLLBACK")
    with pytest.raises(HubError), cache.transaction():
        cache.update(2, {"is_admin": 1})
        raise ValueError("abort")
    assert not cache.ready
    expect_code("USERS_NOT_READY", cache.get, 2)
    proxy.fail_before.clear()
    proxy.execute("ROLLBACK")
    cache.initialize()
    assert cache.get(2)["is_admin"] == 0


def test_commit_succeeded_but_recovery_read_failure_invalidates_cache(db, store_type):
    proxy = FaultConnection(db.connection)
    db._connection = proxy
    cache = store_type(db)
    cache.initialize()
    proxy.fail_after.add("COMMIT")
    proxy.fail_before.add("SELECT")
    with pytest.raises(HubError), cache.transaction():
        cache.update(2, {"is_admin": 1})
    assert not cache.ready
    expect_code("USERS_NOT_READY", cache.get, 2)
    proxy.fail_after.clear()
    proxy.fail_before.clear()
    cache.initialize()
    assert cache.get(2)["is_admin"] == 1


def test_ready_does_not_wait_for_a_transaction_on_another_thread(store):
    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.transaction():
            future = workers.submit(lambda: store.ready)
            assert future.result(timeout=2) is True


def test_other_thread_cannot_borrow_an_active_store_transaction(store):
    started, finished = Event(), Event()

    def update_without_transaction():
        started.set()
        try:
            store.update(2, {"password": "borrowed"})
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.transaction():
            future = workers.submit(update_without_transaction)
            assert started.wait(2)
            assert not finished.wait(0.1)
        with pytest.raises(HubError) as caught:
            future.result(timeout=2)
    assert caught.value.code == "USER_STORE_ERROR"
    assert store.get(2)["password"] == "123456"


def test_rollback_that_succeeded_before_raising_reloads_confirmed_state(db, store_type):
    proxy = FaultConnection(db.connection)
    db._connection = proxy
    cache = store_type(db)
    cache.initialize()
    proxy.fail_after.add("ROLLBACK")
    with pytest.raises(ValueError, match="abort"), cache.transaction():
        cache.update(2, {"is_admin": 1})
        raise ValueError("abort")
    assert cache.ready
    assert cache.get(2)["is_admin"] == 0
    assert cache.all() == db.query("SELECT * FROM users ORDER BY id")
    proxy.fail_after.clear()


def test_limit_one_is_supported_and_startup_checks_configured_cap(db, store_type):
    cache = store_type(db, max_users=1)
    expect_code("USER_LIMIT_REACHED", cache.initialize)
    with db.transaction() as connection:
        connection.execute("DELETE FROM users WHERE id=2")
    cache.initialize()
    assert len(cache.all()) == 1
    with cache.transaction():
        expect_code("USER_LIMIT_REACHED", cache.create, {"name": "overflow"})


@pytest.mark.parametrize("user_id", [True, False, 0, -1, 1.0, "1", None, 2**63])
def test_invalid_ids_never_alias_the_administrator(store, user_id):
    before = store.all()
    expect_code("INVALID_USER", store.get, user_id)
    with store.transaction():
        expect_code("INVALID_USER", store.update, user_id, {"password": "changed"})
        expect_code("INVALID_USER", store.delete, user_id)
    assert store.all() == before


@pytest.mark.parametrize("flag", [2, -1, "1", 1.0, None])
def test_administrator_flag_accepts_only_boolean_values(store, flag):
    before = store.all()
    with store.transaction():
        expect_code("INVALID_DATA", store.create, {"name": "bad", "is_admin": flag})
        expect_code("INVALID_DATA", store.update, 2, {"is_admin": flag})
    assert store.all() == before


def test_boolean_administrator_flag_is_stored_as_integer(db, store):
    with store.transaction():
        row = store.create({"name": "admin", "is_admin": True})
    assert row["is_admin"] == 1
    assert type(row["is_admin"]) is int
    assert row == db.one("SELECT * FROM users WHERE id=?", (row["id"],))
