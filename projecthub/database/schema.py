"""当前 SQLite 模式、模式版本和空库初始数据。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA_REVISION = "0002"
_WHITESPACE = "char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"
_ID = "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT"
_PROJECT_ID = "project_id INTEGER NOT NULL REFERENCES projects(id)"
_FILE_FIELDS = """
    file_name VARCHAR(255) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    path VARCHAR(1024) NOT NULL UNIQUE
"""
SCHEMA_SQL = f"""
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
CREATE TABLE users (
    {_ID},
    name TEXT NOT NULL,
    employee_number TEXT DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL DEFAULT '[]',
    project_permissions TEXT NOT NULL DEFAULT '{{}}',
    is_admin INTEGER NOT NULL DEFAULT '0',
    CONSTRAINT ck_users_name_not_blank CHECK (length(trim(name, {_WHITESPACE})) > 0),
    CONSTRAINT ck_users_project_permissions_object CHECK (CASE WHEN json_valid(project_permissions) THEN json_type(project_permissions)='object' ELSE 0 END)
);
CREATE UNIQUE INDEX ix_users_name ON users(name);
CREATE TABLE project_models (
    {_ID},
    name VARCHAR(128) NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE projects (
    {_ID},
    name VARCHAR(128) NOT NULL REFERENCES project_models(name) ON UPDATE CASCADE,
    version VARCHAR(128) NOT NULL,
    reviewer_user_id INTEGER REFERENCES users(id),
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (name, version)
);
CREATE TABLE curve_configs (
    {_ID},
    {_PROJECT_ID},
    sort_order INTEGER NOT NULL DEFAULT '1',
    curve_type TEXT NOT NULL DEFAULT '未分类',
    description TEXT NOT NULL DEFAULT '',
    modified_at TEXT NOT NULL DEFAULT '',
    {_FILE_FIELDS}
);
CREATE INDEX ix_curve_configs_project_type ON curve_configs(project_id, curve_type);
CREATE INDEX ix_curve_configs_file_name ON curve_configs(file_name);
CREATE INDEX ix_curve_configs_project_order ON curve_configs(project_id, sort_order, id);
CREATE TABLE firmwares (
    {_ID},
    {_PROJECT_ID},
    firmware_type TEXT NOT NULL DEFAULT '未分类',
    supported_models JSON NOT NULL DEFAULT '[]',
    version VARCHAR(128) NOT NULL DEFAULT 'v0.00.00',
    release_notes TEXT NOT NULL DEFAULT '',
    min_version VARCHAR(128) NOT NULL DEFAULT 'v0.00.00',
    max_version VARCHAR(128) NOT NULL DEFAULT 'v0.00.00',
    uploaded_at TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT NOT NULL DEFAULT '',
    {_FILE_FIELDS},
    CONSTRAINT ck_firmwares_supported_models_array CHECK (CASE WHEN json_valid(supported_models) THEN json_type(supported_models)='array' ELSE 0 END)
);
CREATE INDEX ix_firmwares_project_type ON firmwares(project_id, firmware_type);
CREATE INDEX ix_firmwares_file_name ON firmwares(file_name);
CREATE TABLE internal_files (
    {_ID},
    {_PROJECT_ID},
    file_type VARCHAR(255) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    {_FILE_FIELDS}
);
CREATE INDEX ix_internal_files_project_type ON internal_files(project_id, file_type);
CREATE INDEX ix_internal_files_file_name ON internal_files(file_name);
CREATE TABLE public_files (
    {_ID},
    {_PROJECT_ID},
    file_type VARCHAR(255) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    {_FILE_FIELDS}
);
CREATE INDEX ix_public_files_project_type ON public_files(project_id, file_type);
CREATE INDEX ix_public_files_file_name ON public_files(file_name);
CREATE TABLE pending_reviews (
    {_ID},
    {_PROJECT_ID},
    reviewer_user_id INTEGER REFERENCES users(id),
    uploaded_at TEXT NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    path VARCHAR(1024) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    field_info TEXT NOT NULL,
    submitter_name TEXT NOT NULL DEFAULT '',
    CONSTRAINT ck_pending_reviews_project_id CHECK (typeof(project_id)='integer' AND project_id>0),
    CONSTRAINT ck_pending_reviews_reviewer_id CHECK (reviewer_user_id IS NULL OR (typeof(reviewer_user_id)='integer' AND reviewer_user_id>0)),
    CONSTRAINT ck_pending_reviews_category CHECK (CASE WHEN json_valid(field_info) THEN coalesce(json_extract(field_info,'$.category') IN ('curve','firmware'),0) ELSE 0 END),
    CONSTRAINT ck_pending_reviews_sha256 CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_pending_reviews_field_info CHECK (CASE WHEN json_valid(field_info) THEN json_type(field_info)='object' ELSE 0 END),
    CONSTRAINT ck_pending_reviews_uploaded_at_not_blank CHECK (length(trim(uploaded_at, {_WHITESPACE})) > 0),
    CONSTRAINT ck_pending_reviews_file_name_not_blank CHECK (length(trim(file_name, {_WHITESPACE})) > 0),
    CONSTRAINT ck_pending_reviews_path_not_blank CHECK (length(trim(path, {_WHITESPACE})) > 0)
);
CREATE INDEX ix_pending_reviews_project ON pending_reviews(project_id);
CREATE TABLE download_authorizations (
    {_ID},
    created_by TEXT NOT NULL,
    authorization_code VARCHAR(16) NOT NULL UNIQUE,
    {_PROJECT_ID},
    path VARCHAR(1024) NOT NULL,
    expires_at TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    device_code VARCHAR(128) NOT NULL DEFAULT '',
    CONSTRAINT ck_download_authorizations_not_blank CHECK (length(trim(created_by)) > 0 AND project_id > 0 AND length(path) > 0 AND length(expires_at) > 0),
    CONSTRAINT ck_download_authorizations_code CHECK (typeof(authorization_code)='text' AND length(authorization_code)=16 AND instr(authorization_code,char(0))=0 AND authorization_code NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT ck_download_authorizations_sha256 CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')
);
CREATE INDEX ix_download_authorizations_project ON download_authorizations(project_id);
"""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    from_revision: str | None
    to_revision: str


def initialize_schema(connection: sqlite3.Connection) -> None:
    """在调用方的事务内创建当前模式和初始账号。"""
    # executescript 会隐式结束传统 sqlite3 事务，逐条执行保持 DDL 与扫描同生效。
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("INSERT INTO alembic_version VALUES (?)", (SCHEMA_REVISION,))
    connection.executemany(
        "INSERT INTO users (name, employee_number, phone, password, permissions, is_admin)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            ("曾刚来", "246", "17815616805", "888888", "[]", 1),
            ("tool", "0", "", "123456", "[]", 0),
        ),
    )
