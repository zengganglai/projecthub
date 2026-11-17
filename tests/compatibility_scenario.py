"""Replay the same external contract against either isolated application."""

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import rfc8785

_PREFIX = "/api/v1"
_DEFAULT_AUTH = object()
_TOKEN = "synthetic-compatibility-token"
_HEADERS = (
    "allow",
    "cache-control",
    "content-disposition",
    "content-type",
    "www-authenticate",
    "x-content-sha256",
)
_TIME_FIELDS = {"uploaded_at", "modified_at", "created_at", "updated_at"}
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_BACKUP_STAMP = re.compile(r"_(\d{14})(?=[_.])")
_CURVE_A = '{"名称": "一号曲线", "值":[1,2]}\n'.encode()
_CURVE_B = b'{"name": "secondary"}\n'
_CURVE_UPDATED = b'{"name": "edited"}\n'
_PUBLIC = b'{\n  "example": true\n}\n'
_INTERNAL = b"\0\xffinternal\0"


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _signed(data, identifier):
    return {"id": identifier, "data": data, "sha256": _digest(rfc8785.dumps(data))}


def _credentials(name, password):
    return {"X-User-Name": quote(name, safe=""), "X-Password": quote(password, safe="")}


def _normalized(value, deadline=None, key=None):
    if isinstance(value, dict):
        return {field: _normalized(item, deadline, field) for field, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item, deadline, key) for item in value]
    if isinstance(value, str):
        if key in _TIME_FIELDS and _ISO_UTC.fullmatch(value):
            return "<UTC timestamp>"
        if key == "expires_at" and value == deadline:
            return "<near deadline>"
        if key == "backup_path":
            return _BACKUP_STAMP.sub("_<backup timestamp>", value)
    return value


def _assert_no_password(value, password):
    if isinstance(value, dict):
        assert all(name.lower() not in {"password", "x-password"} for name in value), (
            "HTTP data contains a password"
        )
        for item in value.values():
            _assert_no_password(item, password)
    elif isinstance(value, list):
        for item in value:
            _assert_no_password(item, password)
    elif isinstance(value, str):
        assert value != password, "HTTP data contains the seeded password"


class Replay:
    def __init__(self, client, admin_name, admin_password, objects, implementation):
        self.client = client
        self.admin = _credentials(admin_name, admin_password)
        self._admin_password = admin_password
        self.objects = objects
        self.implementation = implementation
        self.results = {}
        self.deadline = None

    async def call(
        self,
        label,
        method,
        path,
        *,
        status=200,
        code=None,
        auth=_DEFAULT_AUTH,
        data=None,
        raw=None,
        media=None,
        params=None,
        expect_file=None,
        plain=False,
        body_id=None,
        extra_headers=None,
    ):
        assert label not in self.results
        if auth is _DEFAULT_AUTH:
            auth = self.admin
        trace = f"contract-{len(self.results):03d}"
        headers = {**(auth or {}), **(extra_headers or {}), "X-Request-ID": trace}
        if media:
            headers["Content-Type"] = media
        envelope_id = f"command-{label}" if body_id is None else body_id
        if data is not None:
            raw = json.dumps(_signed(data, envelope_id), ensure_ascii=False).encode()
        response = await self.client.request(method, path, params=params, headers=headers, content=raw)
        if response.status_code != status:
            detail = response.text.replace(self._admin_password, "<redacted>")
            detail = detail.replace(self.admin["X-Password"], "<redacted>")
            raise AssertionError((label, method, path, response.status_code, status, detail[:1200]))
        assert response.headers["X-Request-ID"] == trace, label
        observed_headers = {name: response.headers[name] for name in _HEADERS if name in response.headers}
        summary = {"status": status, "headers": observed_headers}
        self.results[label] = summary
        if expect_file is not None:
            expected_bytes = expect_file
            assert response.headers["X-Content-SHA256"] == _digest(expected_bytes), label
            assert int(response.headers["Content-Length"]) == len(expected_bytes), label
            assert response.content == (b"" if method == "HEAD" else expected_bytes), label
            summary["file_bytes"] = len(response.content)
            summary["file_sha256"] = _digest(response.content)
            return None
        if method == "HEAD" or status == 204:
            assert response.content == b"", label
            return None
        decoded = response.json()
        if plain:
            payload = decoded
        else:
            assert decoded.keys() == {"id", "status", "timestamp", "data", "sha256"}, label
            assert decoded["status"] == status and type(decoded["timestamp"]) is int, label
            expected_id = envelope_id if data is not None or body_id is not None else trace
            assert decoded["id"] == expected_id, (label, decoded["id"], expected_id)
            payload = decoded["data"]
            assert decoded["sha256"] == _digest(rfc8785.dumps(payload)), label
        _assert_no_password(payload, self._admin_password)
        if code is not None:
            assert payload["error"]["code"] == code, (label, payload)
            error = dict(payload["error"])
            error.pop("message", None)
            payload = {**payload, "error": error}
        summary["data"] = _normalized(payload, self.deadline)
        return payload

    async def upload(
        self,
        label,
        target,
        raw,
        name=None,
        *,
        curve=False,
        curve_name=None,
        status=202,
        auth=_DEFAULT_AUTH,
        **params,
    ):
        if name is not None:
            params["file_name"] = name
        if curve:
            assert isinstance(curve_name, str) and curve_name
            if self.implementation == "projecthub":
                params["curve_name"] = curve_name
        params["file_sha256"] = _digest(raw)
        return await self.call(
            label,
            "POST",
            target,
            status=status,
            auth=auth,
            raw=raw,
            params=params,
            media="application/json" if curve else "application/octet-stream",
        )

    async def approve(self, label, review):
        return await self.call(label, "POST", f"{_PREFIX}/reviews/{review['id']}/approve", data={})

    async def run(self):
        call = self.call
        hidden_status = 403 if self.implementation == "reference" else 404
        hidden_code = "PERMISSION_DENIED" if self.implementation == "reference" else "PROJECT_NOT_FOUND"
        public_status = 200 if self.implementation == "reference" else 404
        public_code = None if self.implementation == "reference" else "PROJECT_NOT_FOUND"
        await call("liveness", "GET", "/health/live", auth=None)
        await call("readiness", "GET", "/health/ready", auth=None)
        await call("admin-identity", "GET", f"{_PREFIX}/me/permissions")
        await call("permission-directory", "GET", f"{_PREFIX}/permissions")
        await call("initial-models", "GET", f"{_PREFIX}/projects")
        await call(
            "auth-required",
            "POST",
            f"{_PREFIX}/projects",
            auth=None,
            raw=b"{",
            status=401,
            code="AUTHENTICATION_FAILED",
        )
        await call(
            "invalid-sha",
            "POST",
            f"{_PREFIX}/projects",
            raw=b'{"id":0,"data":{"name":"X"},"sha256":""}',
            body_id=0,
            status=422,
            code="INVALID_SHA256",
        )
        await call(
            "not-found-id",
            "POST",
            f"{_PREFIX}/missing",
            raw=b'{"id":123}',
            body_id=123,
            status=404,
            code="NOT_FOUND",
        )
        await call(
            "method-not-allowed",
            "POST",
            "/health/live",
            raw=b'{"id":"other"}',
            body_id="other",
            status=405,
            code="METHOD_NOT_ALLOWED",
        )

        model = await call("create-model", "POST", f"{_PREFIX}/projects", data={"name": "M910"}, status=201)
        extra = await call(
            "create-second-model",
            "POST",
            f"{_PREFIX}/projects",
            data={"name": "Other", "description": "未分配"},
            status=201,
        )
        model_path = f"{_PREFIX}/projects/{model['id']}"
        await call("empty-versions", "GET", f"{model_path}/versions")
        version = await call(
            "create-version",
            "POST",
            f"{model_path}/versions",
            data={"version": "初版", "description": "原始版本"},
            status=201,
        )
        await call(
            "create-other-version", "POST", f"{model_path}/versions", data={"version": "扩展版"}, status=201
        )
        version_path = f"{model_path}/versions/{version['id']}"
        await call(
            "wrong-version-owner",
            "GET",
            f"{_PREFIX}/projects/{extra['id']}/versions/{version['id']}/config",
            status=404,
            code="PROJECT_NOT_FOUND",
        )
        await call(
            "config-missing",
            "GET",
            f"{version_path}/config",
            status=404,
            code="PROJECT_CONFIG_NOT_FOUND",
        )
        await call(
            "template-missing",
            "GET",
            f"{version_path}/curves/template",
            status=404,
            code="TEMPLATE_NOT_FOUND",
        )
        version_dir = self.objects / "M910" / "初版"
        (version_dir / "config.json").write_text('{"label":"配置","enabled":true}', encoding="utf-8")
        # Template location is intentionally model-scoped in ProjectHub.
        template_dir = version_dir / "curve" if self.implementation == "reference" else version_dir.parent
        (template_dir / "模板.json").write_text('{"points":[1,2,3]}', encoding="utf-8")
        await call("public-config", "GET", f"{version_path}/config")
        await call("curve-template", "GET", f"{version_path}/curves/template")

        operator = "测试员 +%"
        password = " test+% password "
        user = await call(
            "create-user",
            "POST",
            f"{_PREFIX}/users",
            data={"name": operator, "employee_number": "007", "phone": "00123", "password": password},
            status=201,
        )
        user_path = f"{_PREFIX}/users/{user['user_id']}"
        actor = _credentials(operator, password)
        await call("update-user", "PATCH", user_path, data={"phone": "00456"})
        await call("list-users", "GET", f"{_PREFIX}/users")
        await call("read-empty-permissions", "GET", f"{user_path}/permissions")
        await call("read-user-defaults", "GET", f"{_PREFIX}/me/permissions", auth=actor)
        await call(
            "default-public-read",
            "GET",
            f"{version_path}/files/public",
            auth=actor,
            status=public_status,
            code=public_code,
        )
        await call(
            "curve-read-forbidden",
            "GET",
            f"{version_path}/files/curve",
            auth=actor,
            status=hidden_status,
            code=hidden_code,
        )
        await call(
            "admin-operation-forbidden",
            "POST",
            f"{_PREFIX}/projects",
            auth=actor,
            raw=b"{",
            status=403,
            code="PERMISSION_DENIED",
        )
        await call(
            "grant-permissions",
            "PUT",
            f"{user_path}/permissions",
            data={
                "global_permissions": [],
                "project_permissions": {
                    "factory.debug": ["M910"],
                    "curve.read": ["M910"],
                    "curve.manage": ["M910"],
                    "dealer": ["M910"],
                    "firmware.read": ["M910"],
                    "public.read": ["M910"],
                },
            },
        )
        await call("read-granted-permissions", "GET", f"{user_path}/permissions")
        await call("read-granted-identity", "GET", f"{_PREFIX}/me/permissions", auth=actor)
        await call(
            "change-password",
            "PUT",
            f"{_PREFIX}/me/password",
            auth=actor,
            data={"new_password": " 新密码 +% "},
        )
        await call(
            "reject-old-password",
            "GET",
            f"{_PREFIX}/me/permissions",
            auth=actor,
            status=401,
            code="AUTHENTICATION_FAILED",
        )
        actor = _credentials(operator, " 新密码 +% ")

        curves = f"{version_path}/curves"
        firmware = f"{version_path}/firmwares"
        files = f"{version_path}/files"
        first = await self.upload(
            "submit-curve-a",
            f"{curves}/upload",
            _CURVE_A,
            "曲线一.json",
            curve=True,
            curve_name="曲线A",
            auth=actor,
            curve_type="工况",
            description="匹配%_",
        )
        await call("pending-curve-hidden", "GET", f"{files}/curve", auth=actor)
        review_path = f"{_PREFIX}/reviews/{first['id']}"
        await call(
            "review-scope-requires-pair",
            "GET",
            f"{_PREFIX}/reviews",
            params={"model_id": model["id"]},
            status=422,
            code="INVALID_DATA",
        )
        await call(
            "review-list",
            "GET",
            f"{_PREFIX}/reviews",
            params={"model_id": model["id"], "version_id": version["id"], "limit": 1},
        )
        await call("review-detail", "GET", review_path)
        await call("review-download", "GET", f"{review_path}/download", expect_file=_CURVE_A)
        curve_a = await self.approve("approve-curve-a", first)
        curve_a_id = curve_a["id"]
        await call("read-curve-json", "GET", f"{files}/curve/{curve_a_id}/json", auth=actor)
        await call(
            "signed-curve-json", "POST", f"{files}/curve/json", auth=actor, data={"file_id": curve_a_id}
        )
        await call(
            "filter-by-literal", "GET", f"{files}/curve", auth=actor, params={"q": "%_", "file_type": "工况"}
        )
        await call(
            "download-curve", "GET", f"{files}/curve/{curve_a_id}/download", auth=actor, expect_file=_CURVE_A
        )
        await call(
            "head-curve", "HEAD", f"{files}/curve/{curve_a_id}/download", auth=actor, expect_file=_CURVE_A
        )
        await call("head-without-auth", "HEAD", f"{files}/curve/{curve_a_id}/download", auth=None, status=401)
        second = await self.upload(
            "submit-curve-b",
            f"{curves}/upload",
            _CURVE_B,
            "曲线二.json",
            curve=True,
            curve_name="曲线B",
            auth=actor,
            curve_type="次要",
            description="",
        )
        curve_b = await self.approve("approve-curve-b", second)
        await call(
            "sort-curves", "PUT", f"{curves}/{curve_b['id']}/position", auth=actor, data={"position": 1}
        )
        ordered = await call("sorted-curves", "GET", f"{files}/curve", auth=actor)
        assert [item["id"] for item in ordered["files"]] == [curve_b["id"], curve_a_id]

        public = await self.upload(
            "publish-public",
            f"{version_path}/public-files/upload",
            _PUBLIC,
            "public.json",
            status=201,
            description="公共",
        )
        internal = await self.upload(
            "publish-internal", f"{version_path}/internal-files/upload", _INTERNAL, "internal.bin", status=201
        )
        await call("internal-list", "GET", f"{files}/internal")
        await call(
            "internal-binary", "GET", f"{files}/internal/{internal['id']}/download", expect_file=_INTERNAL
        )
        await call(
            "internal-json-mismatch",
            "GET",
            f"{files}/internal/{internal['id']}/json",
            status=415,
            code="NOT_JSON_FILE",
        )
        await call("public-list", "GET", f"{files}/public", auth=actor)
        await call("public-json", "GET", f"{files}/public/{public['id']}/json", auth=actor)

        auth_codes = f"{version_path}/download-authorizations"
        code = "ABCDEF0011223344"
        code_path = f"{_PREFIX}/authorized-files/{code}"
        grant = {
            "path": public["path"],
            "expires_at": "2099-01-01",
            "authorization_code": code,
            "device_code": "000-A",
        }
        authorization = await call("authorize-file", "POST", auth_codes, status=201, data=grant)
        await call(
            "duplicate-authorization",
            "POST",
            auth_codes,
            status=409,
            data=grant,
            code="AUTHORIZATION_CODE_EXISTS",
        )
        await call("authorization-list", "GET", auth_codes, params={"limit": 1})
        await call("authorization-info", "GET", code_path, auth=None)
        await call("authorization-json", "GET", f"{code_path}/json", auth=None)
        await call("authorization-download", "GET", f"{code_path}/download", auth=None, expect_file=_PUBLIC)

        update = await call(
            "submit-curve-update",
            "PUT",
            f"{curves}/{curve_a_id}",
            status=202,
            auth=actor,
            raw=_CURVE_UPDATED,
            media="application/json",
            params={
                "curve_type": "已调整",
                "description": "新的描述",
                "file_sha256": _digest(_CURVE_UPDATED),
            },
        )
        await call("rename-model", "PATCH", model_path, data={"name": "M920", "description": "型号改名"})
        await call(
            "rename-version", "PATCH", version_path, data={"version": "新版", "description": "版本改名"}
        )
        await call("renamed-model-list", "GET", f"{_PREFIX}/projects")
        await call("renamed-version-list", "GET", f"{model_path}/versions")
        await call("renamed-permissions", "GET", f"{user_path}/permissions")
        await call("renamed-config", "GET", f"{version_path}/config")
        await call("renamed-authorization", "GET", code_path, auth=None)
        await call("renamed-pending-review", "GET", f"{_PREFIX}/reviews/{update['id']}")
        modified = await self.approve("approve-curve-update", update)
        assert modified["id"] == curve_a_id
        await call(
            "updated-curve-bytes",
            "GET",
            f"{files}/curve/{curve_a_id}/download",
            auth=actor,
            expect_file=_CURVE_UPDATED,
        )
        deletion = await call(
            "request-curve-deletion", "POST", f"{curves}/{curve_a_id}/delete", status=202, auth=actor, data={}
        )
        await call("reject-curve-deletion", "POST", f"{_PREFIX}/reviews/{deletion['id']}/reject", data={})
        deletion = await call(
            "request-curve-deletion-again",
            "POST",
            f"{curves}/{curve_a_id}/delete",
            status=202,
            auth=actor,
            data={},
        )
        await self.approve("approve-curve-deletion", deletion)
        await call(
            "deleted-curve",
            "GET",
            f"{files}/curve/{curve_a_id}/json",
            auth=actor,
            status=404,
            code="FILE_NOT_FOUND",
        )
        rejected = await self.upload(
            "submit-rejected-curve",
            f"{curves}/upload",
            b"{}",
            "rejected.json",
            curve=True,
            curve_name="驳回曲线",
            auth=actor,
            curve_type="x",
            description="",
        )
        await call("reject-upload", "POST", f"{_PREFIX}/reviews/{rejected['id']}/reject", data={})
        await call(
            "rejected-review-gone",
            "GET",
            f"{_PREFIX}/reviews/{rejected['id']}",
            status=404,
            code="REVIEW_NOT_FOUND",
        )

        await self.firmwares(firmware, files, actor)
        await call(
            "replace-without-project-grants",
            "PUT",
            f"{user_path}/permissions",
            data={"global_permissions": [], "project_permissions": {}},
        )
        await call(
            "revoked-curve-access",
            "GET",
            f"{files}/curve",
            auth=actor,
            status=hidden_status,
            code=hidden_code,
        )
        await call(
            "default-access-persists",
            "GET",
            f"{files}/public",
            auth=actor,
            status=public_status,
            code=public_code,
        )
        await call("delete-user", "DELETE", user_path)
        await call(
            "deleted-user-cannot-authenticate",
            "GET",
            f"{_PREFIX}/me/permissions",
            auth=actor,
            status=401,
            code="AUTHENTICATION_FAILED",
        )

        await call("revoke-authorization", "DELETE", f"{auth_codes}/{authorization['id']}")
        await call(
            "revoked-authorization-not-found",
            "GET",
            code_path,
            auth=None,
            status=404,
            code="AUTHORIZATION_NOT_FOUND",
        )
        await call(
            "past-expiry-rejected",
            "POST",
            auth_codes,
            status=422,
            code="INVALID_DATA",
            data={**grant, "path": "M920/新版/public/public.json", "expires_at": "2001-01-01"},
        )
        expires = datetime.now(UTC) + timedelta(seconds=2)
        self.deadline = expires.isoformat(timespec="microseconds").replace("+00:00", "Z")
        soon = "0123456789abcdef"
        await call(
            "authorize-near-expiry",
            "POST",
            auth_codes,
            status=201,
            data={
                **grant,
                "path": "M920/新版/public/public.json",
                "expires_at": self.deadline,
                "authorization_code": soon,
            },
        )
        await asyncio.sleep(max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.05)
        await call(
            "expired-authorization",
            "GET",
            f"{_PREFIX}/authorized-files/{soon}",
            auth=None,
            status=410,
            code="AUTHORIZATION_EXPIRED",
        )
        await call("expired-authorization-omitted", "GET", auth_codes)
        await call("delete-internal", "DELETE", f"{version_path}/internal-files/{internal['id']}")
        await call("delete-public", "DELETE", f"{version_path}/public-files/{public['id']}")
        await self.remote()

    async def firmwares(self, path, files, actor):
        async def publish(label, name, content, kind, version, minimum, maximum, **optional):
            pending = await self.upload(
                f"submit-{label}",
                f"{path}/upload",
                content,
                name,
                firmware_type=kind,
                version=version,
                release_notes=f"notes-{label}",
                min_version=minimum,
                max_version=maximum,
                **optional,
            )
            return pending, await self.approve(f"approve-{label}", pending)

        _, first = await publish(
            "firmware-a",
            "sensor-v2.bin",
            b"sensor-v2",
            "sensor",
            "2",
            "1",
            "1.9",
            supported_models='["M920","Other"]',
        )
        _, other = await publish("firmware-b", "screen-v2.bin", b"screen-v2", "screen", "2", "1", "1.9")
        upgrades = await self.call(
            "firmware-check-multiple-types",
            "GET",
            f"{path}/check",
            auth=actor,
            params={"current_version": "v1.02"},
        )
        assert [item["firmware_type"] for item in upgrades["firmwares"]] == ["screen", "sensor"]
        await self.call(
            "firmware-check-invalid",
            "GET",
            f"{path}/check",
            auth=actor,
            params={"current_version": "bad"},
            status=422,
            code="INVALID_FIRMWARE_VERSION",
        )
        inherited, updated = await publish(
            "firmware-inherit", "sensor-v3.bin", b"sensor-v3", "sensor", "3", "2", "2.9"
        )
        assert inherited["field_info"]["fields"]["supported_models"] == ["M920", "Other"]
        assert updated["id"] == first["id"] and updated["supported_models"] == ["M920", "Other"]
        _, emptied = await publish(
            "firmware-clear", "sensor-v4.bin", b"sensor-v4", "sensor", "4", "3", "3.9", supported_models="[]"
        )
        assert emptied["id"] == first["id"] and emptied["supported_models"] == []
        await self.call(
            "firmware-download",
            "GET",
            f"{files}/firmware/{first['id']}/download",
            auth=actor,
            expect_file=b"sensor-v4",
        )
        removal = await self.call(
            "request-firmware-removal", "POST", f"{path}/{other['id']}/delete", data={}, status=202
        )
        await self.approve("approve-firmware-removal", removal)

    async def remote(self):
        call = self.call
        auth = {"Authorization": f"Bearer {_TOKEN}"}
        base = f"{_PREFIX}/control-sessions"
        request_id = "a" * 32
        await call("remote-health", "GET", f"{_PREFIX}/remote-control/health", auth=auth, plain=True)
        await call(
            "remote-no-token",
            "GET",
            f"{_PREFIX}/devices",
            auth=None,
            plain=True,
            status=401,
            code="AUTH_REQUIRED",
        )
        await call(
            "remote-invalid-payload",
            "POST",
            base,
            auth=auth,
            plain=True,
            status=422,
            code="INVALID_REQUEST",
            raw=b'{"deviceId":"a","deviceId":"b"}',
        )
        await call(
            "remote-disabled-devices",
            "GET",
            f"{_PREFIX}/devices",
            auth=auth,
            plain=True,
            status=503,
            code="REMOTE_UNAVAILABLE",
        )
        for label, method, path, raw in [
            ("acquire", "POST", base, json.dumps({"deviceId": "device", "requestId": request_id}).encode()),
            ("lookup", "GET", f"{base}/by-request/{request_id}", None),
            ("cancel", "DELETE", f"{base}/by-request/{request_id}", None),
            ("renew", "POST", f"{base}/synthetic-session/renew", None),
            ("release", "DELETE", f"{base}/synthetic-session", None),
        ]:
            await call(
                f"remote-disabled-{label}",
                method,
                path,
                raw=raw,
                auth=auth,
                plain=True,
                status=503,
                code="REMOTE_UNAVAILABLE",
            )


async def execute(implementation, root):
    if implementation == "reference":
        from device_bridge.application import create_app
        from device_bridge.config import AppConfig, RemoteControlConfig
    else:
        from projecthub.application import AppConfig, RemoteControlConfig, create_app

    root.mkdir(parents=True, exist_ok=False)
    settings = AppConfig(project_root=root, remote_control=RemoteControlConfig(api_token=_TOKEN))
    app = create_app(settings)
    lifespan = app.lifespan() if implementation == "reference" else app.router.lifespan_context(app)
    async with lifespan:
        database_path = root / settings.database.path
        uri = database_path.as_uri()
        if uri.startswith("file://") and not uri.startswith("file:///"):
            # SQLite expects the UNC host and share within the URI path.
            uri = "file:////" + uri.removeprefix("file://")
        with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as connection:
            name, password = connection.execute(
                "SELECT name, password FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1",
            ).fetchone()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://contract"
        ) as client:
            replay = Replay(client, name, password, root / settings.storage.object_dir, implementation)
            await replay.run()
            return replay.results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("implementation", choices=["reference", "projecthub"])
    parser.add_argument("temporary_root", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(execute(arguments.implementation, arguments.temporary_root)), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
