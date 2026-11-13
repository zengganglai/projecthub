"""SQLite 模式匹配和只读完整性校验。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .schema import SCHEMA_SQL


def _tables(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='table'"))


def _columns(connection: sqlite3.Connection, table: str) -> dict:
    return {
        name: (kind.upper(), required, default, primary_key, hidden)
        for _, name, kind, required, default, primary_key, hidden in connection.execute(
            "SELECT * FROM pragma_table_xinfo(?)", (table,)
        )
    }


def _foreign_keys(connection: sqlite3.Connection, table: str) -> set[tuple]:
    return {
        tuple(row[2:]) for row in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))
    }


def _indexes(connection: sqlite3.Connection, table: str) -> tuple[dict, set]:
    named = {}
    unique = set()
    for _, index, is_unique, origin, partial in connection.execute(
        "SELECT * FROM pragma_index_list(?)", (table,)
    ):
        keys = tuple(
            tuple(row[1:])
            for row in connection.execute(
                'SELECT seqno, name, "desc", coll FROM pragma_index_xinfo(?) WHERE key ORDER BY seqno',
                (index,),
            )
        )
        definition = (keys, is_unique, partial)
        if origin == "c":
            named[index] = definition
        if is_unique:
            unique.add(definition)
    return named, unique


def _matches_schema(connection: sqlite3.Connection) -> bool:
    """按 SQLite 元数据核对列、索引、外键和约束。"""
    actual = _tables(connection)
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(SCHEMA_SQL)
        for table, ddl in _tables(reference).items():
            if table.startswith("sqlite_"):
                continue
            if table not in actual:
                return False
            columns = _columns(reference, table)
            checks = [
                line.strip().rstrip(",")
                for line in ddl.splitlines()
                if line.lstrip().startswith("CONSTRAINT ")
            ]
            if columns != _columns(connection, table) or _foreign_keys(reference, table) != _foreign_keys(
                connection, table
            ):
                return False
            named, unique = _indexes(reference, table)
            actual_named, actual_unique = _indexes(connection, table)
            if unique != actual_unique or any(
                actual_named.get(name) != value for name, value in named.items()
            ):
                return False
            actual_sql = re.sub(r"\s+", "", actual[table])
            if "AUTOINCREMENT" in ddl and "AUTOINCREMENT" not in actual_sql.upper():
                return False
            if any(re.sub(r"\s+", "", check) not in actual_sql for check in checks):
                return False
    return True


def connection_integrity(connection: sqlite3.Connection, *, thorough: bool = False) -> dict:
    """读取 SQLite 页检查与外键检查结果，不修改数据库。"""
    pragma = "integrity_check" if thorough else "quick_check"
    checks = tuple(row[0] for row in connection.execute(f"PRAGMA {pragma}"))
    foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
    return {
        "is_valid": checks == ("ok",) and not foreign_keys,
        "check_result": checks,
        "foreign_key_errors": foreign_keys,
    }


def _readonly_uri(path: Path) -> str:
    uri = path.as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        # Windows UNC paths need an empty URI authority and two leading path slashes.
        uri = "file:////" + uri.removeprefix("file://")
    return uri + "?mode=ro"


def verify_database_integrity(path: Path, *, thorough: bool = False) -> dict:
    """以只读方式检查磁盘数据库，不创建缺失数据库。"""
    with closing(sqlite3.connect(_readonly_uri(path.resolve()), uri=True)) as connection:
        return connection_integrity(connection, thorough=thorough)
