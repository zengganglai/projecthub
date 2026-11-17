"""Wire-level checks for the HTTP adapter and its ASGI ownership rules."""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
import rfc8785
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from projecthub.http import install_routes
from projecthub.remote import RemoteService
from projecthub.remote.config import RemoteControlConfig

VERSION = "/api/v1/projects/2/versions/3"
PROJECT = "/projects/{model_id}"
RELEASE = PROJECT + "/versions/{version_id}"
AUTH = [(b"x-user-name", b"%E5%90%8D%2B%25"), (b"x-password", b"%20secret%20")]


class Users:
    def authenticate(self, name, password):
        assert (name, password) == ("名+%", " secret ")
        return {"user_id": 1, "name": name, "is_admin": True, "global_permissions": [], "projects": []}

    def require_admin(self, actor):
        assert actor["user_id"] == 1

    def get_project_version(self, model_id, version_id, *, actor=None):
        if (model_id, version_id) == (2, 3):
            return {"id": 3, "name": "Model", "version": "v1", "description": "", "reviewer_user_id": None}

    def require_file_download(self, actor, category, project_id):
        assert (actor["user_id"], category, project_id) == (1, "curve", 3)


@dataclass
class Download:
    file_name: str = "曲线 1.json"
    body: bytes = b"{}"
    closed: bool = False
    reads: int = 0

    @property
    def sha256(self):
        return hashlib.sha256(self.body).hexdigest()

    @property
    def size(self):
        return len(self.body)

    def read_chunk(self, limit):
        self.reads += 1
        return self.body[:limit] if self.reads == 1 else b""

    def close(self):
        self.closed = True


@dataclass
class Upload:
    chunks: list[bytes] = field(default_factory=list)
    closed: bool = False
    finished: bool = False

    def write(self, chunk):
        self.chunks.append(chunk)

    def finish(self):
        self.finished = True
        return {"file_id": 4}

    def close(self):
        self.closed = True


class Files:
    def create_project_model(self, name, description, *, actor):
        assert actor["user_id"] == 1
        return {"name": name, "description": description}

    def prepare_download(self, file_id, category, project_id, *, actor):
        assert (file_id, category, project_id, actor["user_id"]) == (4, "curve", 3, 1)
        self.download = Download()
        return self.download

    def begin_upload(self, actor, project_id, category, file_name, fields, file_sha256):
        assert (actor["user_id"], project_id, category, file_name) == (1, 3, "curve", "curve.json")
        assert fields == {"curve_name": "test", "curve_type": "x", "description": "d"}
        assert file_sha256 == hashlib.sha256(b'{"a":1}').hexdigest()
        self.upload = Upload()
        return self.upload


def make_app(**options):
    app = FastAPI(docs_url=None, redoc_url=None)
    app.state.settings = SimpleNamespace(
        http=SimpleNamespace(
            **{
                "host_restriction_enabled": False,
                "allowed_host": "api.example.test",
                "port": 8001,
                "max_concurrent_requests": 16,
                "max_json_body_bytes": 1024 * 1024,
                **options,
            }
        )
    )
    app.state.ready = True
    app.state.reason = "ready"
    app.state.users = Users()
    app.state.files = Files()
    app.state.files.catalog = app.state.files
    app.state.files.reviews = app.state.files
    app.state.files.authorizations = app.state.files
    app.state.permissions = app.state.users
    app.state.projects = SimpleNamespace(
        get_project_model=getattr(app.state.users, "get_project_model", lambda identifier, **kwargs: None),
        get_project_version=app.state.users.get_project_version,
        create_project_model=app.state.files.create_project_model,
    )
    install_routes(app)
    return app


async def asgi_request(app, method, path, *, headers=(), query=b"", chunks=(), on_send=None):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "headers": list(headers),
    }
    messages = []
    payload = iter(chunks)

    async def receive():
        return next(payload)

    async def send(message):
        if on_send is not None:
            await on_send(message)
        messages.append(message)

    await app(scope, receive, send)
    status = messages[0]["status"]
    response_headers = httpx.Headers(messages[0]["headers"])
    body = b"".join(message.get("body", b"") for message in messages[1:])
    return status, response_headers, body


def request_chunk(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


def test_openapi_declares_every_business_method_and_path():
    signatures = {
        "/me/permissions": "get",
        "/me/password": "put",
        "/users": "get post",
        "/users/{user_id}": "patch delete",
        "/permissions": "get",
        "/users/{user_id}/permissions": "get put",
        "/projects": "get post",
        PROJECT: "patch",
        PROJECT + "/versions": "get post",
        RELEASE: "patch",
        RELEASE + "/curves/template": "get",
        RELEASE + "/curves/names": "get",
        RELEASE + "/firmwares/check": "get",
        RELEASE + "/files/{category}/{file_id}/json": "get",
        RELEASE + "/files/{category}/json": "post",
        RELEASE + "/files/{category}/{file_id}/download": "get head",
        RELEASE + "/files/{category}": "get",
        RELEASE + "/curves/{file_id}/position": "put",
        RELEASE + "/curves/{file_id}": "put",
        RELEASE + "/curves/upload": "post",
        RELEASE + "/firmwares/upload": "post",
        RELEASE + "/curves/{file_id}/delete": "post",
        RELEASE + "/firmwares/{file_id}/delete": "post",
        "/reviews": "get",
        "/reviews/{review_id}": "get",
        "/reviews/{review_id}/download": "get",
        "/reviews/{review_id}/approve": "post",
        "/reviews/{review_id}/reject": "post",
        RELEASE + "/internal-files/upload": "post",
        RELEASE + "/public-files/upload": "post",
        RELEASE + "/internal-files/{file_id}": "delete",
        RELEASE + "/public-files/{file_id}": "delete",
        RELEASE + "/download-authorizations": "get post",
        RELEASE + "/download-authorizations/{authorization_id}": "delete",
        "/authorized-files/{authorization_code}": "get",
        "/authorized-files/{authorization_code}/json": "get",
        "/authorized-files/{authorization_code}/download": "get",
        RELEASE + "/config": "get",
        "/devices": "get",
        "/control-sessions": "post",
        "/control-sessions/by-request/{request_id}": "get delete",
        "/control-sessions/{session_id}/renew": "post",
        "/control-sessions/{session_id}": "delete",
        "/remote-control/health": "get",
    }
    paths = make_app().openapi()["paths"]
    actual = {
        (path.removeprefix("/api/v1"), method)
        for path, methods in paths.items()
        if path.startswith("/api/v1/")
        for method in methods
    }
    expected = {(path, method) for path, methods in signatures.items() for method in methods.split()}
    assert len(expected) == 52
    assert actual == expected


def test_authentication_rejects_bad_credentials_without_consuming_body():
    status, headers, body = asyncio.run(
        asgi_request(
            make_app(),
            "POST",
            "/api/v1/projects",
            headers=[
                (b"x-request-id", b"valid-trace"),
                (b"x-user-name", b"%ff"),
                AUTH[1],
            ],
        )
    )
    assert status == 401
    assert headers["www-authenticate"] == 'Password realm="FileTrail"'
    assert headers["x-request-id"] == "valid-trace"
    assert json.loads(body)["data"]["error"]["code"] == "AUTHENTICATION_FAILED"


def test_digest_checks_original_data_before_defaults_and_echoes_integer_id():
    data = {"name": " 测试 "}
    digest = hashlib.sha256(rfc8785.dumps(data)).hexdigest()
    content = json.dumps({"id": 0, "data": data, "sha256": digest}).encode()
    status, _, body = asyncio.run(
        asgi_request(
            make_app(max_json_body_bytes=len(content)),
            "POST",
            "/api/v1/projects",
            headers=AUTH,
            chunks=[request_chunk(content[:30], True), request_chunk(content[30:])],
        )
    )
    assert status == 201
    payload = json.loads(body)
    assert set(payload) == {"id", "status", "timestamp", "data", "sha256"}
    assert payload["id"] == 0
    assert payload["status"] == 201
    assert type(payload["timestamp"]) is int
    assert payload["data"] == {"name": " 测试 ", "description": ""}
    assert payload["sha256"] == hashlib.sha256(rfc8785.dumps(payload["data"])).hexdigest()


@pytest.mark.parametrize(
    ("content", "limit", "status", "code", "response_id"),
    [
        (
            b'{"id":"duplicate","data":{"name":"a","name":"b"},"sha256":""}',
            100,
            422,
            "INVALID_REQUEST",
            "trace",
        ),
        (b'{"id":"deep","data":{"name":1e999},"sha256":""}', 100, 422, "INVALID_REQUEST", "trace"),
        (b'{"id":true,"data":{"name":"a"},"sha256":""}', 100, 422, "INVALID_ID", "trace"),
        (b'{"id":"schema","data":{"name":7},"sha256":""}', 100, 422, "INVALID_DATA", "schema"),
        (b'{"id":"oversize","data":{},"sha256":""}', 5, 413, "BODY_TOO_LARGE", "trace"),
        (
            b'{"id":"digest","data":{"name":"a"},"sha256":"' + b"0" * 64 + b'"}',
            150,
            422,
            "SHA256_MISMATCH",
            "digest",
        ),
    ],
)
def test_protocol_errors_preserve_only_valid_recoverable_client_ids(
    content, limit, status, code, response_id
):
    found, headers, body = asyncio.run(
        asgi_request(
            make_app(max_json_body_bytes=limit),
            "POST",
            "/api/v1/projects",
            headers=[*AUTH, (b"x-request-id", b"trace"), (b"content-length", b"1")],
            chunks=[request_chunk(content[:4], True), request_chunk(content[4:], status == 413)],
        )
    )
    payload = json.loads(body)
    assert found == status
    assert payload["id"] == response_id
    assert headers["x-request-id"] == "trace"
    assert payload["data"]["error"]["code"] == code


def test_head_auth_error_contains_no_asgi_response_body():
    status, headers, body = asyncio.run(
        asgi_request(
            make_app(),
            "HEAD",
            VERSION + "/files/curve/4/download",
            headers=[(b"x-request-id", b"head")],
        )
    )
    assert status == 401
    assert body == b""
    assert headers["x-request-id"] == "head"


def test_not_found_and_method_error_keep_protocol_and_allow_header():
    app = make_app()
    status, _, body = asyncio.run(
        asgi_request(
            app,
            "POST",
            "/missing",
            chunks=[request_chunk(b'{"id":"client-404"}')],
        )
    )
    assert status == 404
    assert json.loads(body)["id"] == "client-404"
    status, headers, body = asyncio.run(
        asgi_request(
            app,
            "POST",
            "/health/live",
            chunks=[request_chunk(b'{"id":0}')],
        )
    )
    assert status == 405
    assert headers["allow"] == "GET"
    assert json.loads(body)["id"] == 0


def test_download_get_and_head_have_matching_headers_and_release_handles():
    app = make_app()
    path = VERSION + "/files/curve/4/download"
    get_status, get_headers, body = asyncio.run(asgi_request(app, "GET", path, headers=AUTH))
    assert (get_status, body) == (200, b"{}")
    assert app.state.files.download.closed
    head_status, head_headers, body = asyncio.run(asgi_request(app, "HEAD", path, headers=AUTH))
    assert (head_status, body) == (200, b"")
    assert app.state.files.download.closed and app.state.files.download.reads == 0
    for key, value in {
        "content-type": "application/octet-stream",
        "content-length": "2",
        "cache-control": "no-store",
        "content-disposition": "attachment; filename=\"download\"; filename*=UTF-8''%E6%9B%B2%E7%BA%BF%201.json",
        "x-content-sha256": hashlib.sha256(b"{}").hexdigest(),
    }.items():
        assert get_headers[key] == head_headers[key] == value


def test_upload_consumes_original_chunks_and_closes_when_client_disconnects():
    app = make_app()
    upload_path = VERSION + "/curves/upload"
    digest = hashlib.sha256(b'{"a":1}').hexdigest()
    query = f"file_name=curve.json&curve_name=test&curve_type=x&description=d&file_sha256={digest}".encode()
    headers = [*AUTH, (b"content-type", b"application/json; charset=utf-8")]

    status, _, body = asyncio.run(
        asgi_request(
            app,
            "POST",
            upload_path,
            headers=headers,
            query=query,
            chunks=[request_chunk(b'{"a":', True), request_chunk(b"1}")],
        )
    )
    assert status == 202
    assert json.loads(body)["data"] == {"file_id": 4}
    assert app.state.files.upload.chunks == [b'{"a":', b"1}"]
    assert app.state.files.upload.closed and app.state.files.upload.finished

    async def disconnect():
        with pytest.raises(ClientDisconnect):
            await asgi_request(
                app,
                "POST",
                upload_path,
                headers=headers,
                query=query,
                chunks=[request_chunk(b'{"a":', True), {"type": "http.disconnect"}],
            )

    asyncio.run(disconnect())
    assert app.state.files.upload.closed
    assert not app.state.files.upload.finished


def test_host_validation_rejects_duplicates_and_ignores_forwarded_host():
    app = make_app(host_restriction_enabled=True)
    status, headers, body = asyncio.run(
        asgi_request(
            app,
            "GET",
            "/health/live",
            headers=[
                (b"host", b"wrong.test"),
                (b"x-forwarded-host", b"api.example.test"),
                (b"x-request-id", b"invalid request id"),
            ],
        )
    )
    assert status == 400
    assert headers["x-request-id"] != "invalid request id"
    assert json.loads(body)["data"]["error"]["code"] == "INVALID_HOST"
    status, _, body = asyncio.run(
        asgi_request(
            app,
            "HEAD",
            "/health/live",
            headers=[(b"host", b"api.example.test"), (b"host", b"bad")],
        )
    )
    assert (status, body) == (400, b"")
    for authority in (b"API.EXAMPLE.TEST:443", b"127.0.0.1:8001"):
        status, _, _ = asyncio.run(asgi_request(app, "GET", "/health/live", headers=[(b"host", authority)]))
        assert status == 200


def test_stream_disconnect_releases_concurrency_slot_and_download_handle():
    async def exercise():
        app = make_app(max_concurrent_requests=1)
        path = VERSION + "/files/curve/4/download"
        sending = asyncio.Event()
        blocked = asyncio.Event()

        async def pause_stream(message):
            if message["type"] == "http.response.start":
                sending.set()
                await blocked.wait()

        task = asyncio.create_task(asgi_request(app, "GET", path, headers=AUTH, on_send=pause_stream))
        try:
            await asyncio.wait_for(sending.wait(), timeout=2)
            status, _, body = await asgi_request(app, "GET", "/health/live")
            assert status == 200 and json.loads(body)["data"]["active_requests"] == 1
            status, _, body = await asgi_request(app, "HEAD", path, headers=AUTH)
            assert (status, body) == (503, b"")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert app.state.files.download.closed
        status, _, body = await asgi_request(app, "GET", "/health/live")
        assert status == 200 and json.loads(body)["data"]["active_requests"] == 0

    asyncio.run(exercise())


def test_documentation_routes_serve_request_metadata_and_local_favicon():
    async def exercise():
        app = make_app()
        app.state.ready, app.state.reason = False, "not_started"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["data"] == {"ready": False, "reason": "not_started"}
            assert (await client.get("/health")).json() == {"status": "ok"}
            app.state.ready = True
            for path in ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json", "/favicon.ico"):
                assert (await client.get(path)).status_code == 200
            response = await client.get("/favicon.ico")
            assert response.headers["content-type"] == "image/x-icon"
            assert response.content.startswith(b"\x00\x00\x01\x00")

    asyncio.run(exercise())


def test_admission_waits_for_a_500_response_before_signalling_drained():
    async def exercise():
        app = make_app(max_concurrent_requests=1)
        app.state.drained = asyncio.Event()
        app.state.drained.set()
        sending = asyncio.Event()
        finish = asyncio.Event()

        @app.get("/fails")
        async def fail():
            raise RuntimeError("unexpected")

        async def pause_error(message):
            if message["type"] == "http.response.body":
                assert json.loads(message["body"])["data"]["error"]["code"] == "INTERNAL_ERROR"
                sending.set()
                await finish.wait()

        task = asyncio.create_task(asgi_request(app, "GET", "/fails", on_send=pause_error))
        try:
            await asyncio.wait_for(sending.wait(), timeout=2)
            assert not app.state.drained.is_set()
            status, _, body = await asgi_request(app, "GET", "/api/v1/projects")
            assert status == 503
            assert json.loads(body)["data"]["error"]["code"] == "SERVER_BUSY"
        finally:
            finish.set()
            with pytest.raises(RuntimeError, match="unexpected"):
                await task
        assert app.state.drained.is_set()

    asyncio.run(exercise())


def test_remote_auth_precedes_json_and_remote_errors_have_a_separate_shell():
    async def exercise():
        app = make_app()
        remote = RemoteService(RemoteControlConfig(api_token="example-test-token"))
        await remote.initialize()
        app.state.remote = remote
        try:
            status, headers, body = await asgi_request(app, "POST", "/api/v1/control-sessions")
            assert status == 401
            assert headers["www-authenticate"] == "Bearer"
            assert headers["cache-control"] == "no-store"
            assert json.loads(body) == {"error": {"code": "AUTH_REQUIRED"}}

            authenticated = [(b"authorization", b"Bearer example-test-token")]
            status, headers, body = await asgi_request(
                app,
                "POST",
                "/api/v1/control-sessions",
                headers=authenticated,
                chunks=[request_chunk(b'{"deviceId":"a","deviceId":"b"}')],
            )
            assert status == 422
            assert headers["cache-control"] == "no-store"
            assert json.loads(body) == {"error": {"code": "INVALID_REQUEST"}}

            status, headers, body = await asgi_request(
                app,
                "POST",
                "/api/v1/control-sessions",
                headers=authenticated,
                chunks=[request_chunk(b" " * 4097, True)],
            )
            assert status == 413
            assert headers["cache-control"] == "no-store"
            assert json.loads(body) == {"error": {"code": "BODY_TOO_LARGE"}}

            status, headers, body = await asgi_request(
                app,
                "GET",
                "/api/v1/remote-control/health",
                headers=authenticated,
            )
            assert status == 200
            assert headers["cache-control"] == "no-store"
            payload = json.loads(body)
            assert payload["reason"] == "disabled"
            assert not payload["enabled"]
            assert "data" not in payload
        finally:
            await remote.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("upload", [False, True])
def test_cancel_during_handle_creation_waits_for_creation_and_close(upload):
    async def scenario():
        app = make_app(max_concurrent_requests=1)
        loop = asyncio.get_running_loop()
        opening, closing = asyncio.Event(), asyncio.Event()
        release_open, release_close = Event(), Event()
        handle = Upload() if upload else Download()
        original_close = handle.close

        def begin(*args, **kwargs):
            loop.call_soon_threadsafe(opening.set)
            assert release_open.wait(10)
            return handle

        def close():
            loop.call_soon_threadsafe(closing.set)
            assert release_close.wait(10)
            original_close()

        handle.close = close
        if upload:
            app.state.files.begin_upload = begin
            digest = hashlib.sha256(b'{"a":1}').hexdigest()
            request = asgi_request(
                app,
                "POST",
                VERSION + "/curves/upload",
                headers=[*AUTH, (b"content-type", b"application/json")],
                query=f"file_name=curve.json&curve_name=test&curve_type=x&description=d&file_sha256={digest}".encode(),
            )
        else:
            app.state.files.prepare_download = begin
            request = asgi_request(app, "GET", VERSION + "/files/curve/4/download", headers=AUTH)
        task = asyncio.create_task(request)
        try:
            await asyncio.wait_for(opening.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release_open.set()
            await asyncio.wait_for(closing.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert app.state.active_requests == 1
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert handle.closed
            assert app.state.active_requests == 0
            if upload:
                assert not handle.finished
        finally:
            release_open.set()
            release_close.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
