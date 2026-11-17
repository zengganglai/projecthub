"""ASGI request tracking, Host policy, and admission through response completion."""

import re
from ipaddress import IPv6Address
from uuid import uuid4

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .protocol import error_response, internal_error

PROBES = {"/health", "/health/live", "/health/ready"}
_TRACE = re.compile(rb"[A-Za-z0-9._-]{1,128}")


def readiness(state) -> tuple[bool, str]:
    user_store = getattr(state, "user_store", None)
    if state.ready and user_store is not None and not user_store.ready:
        return False, "users_not_ready"
    if state.ready and getattr(getattr(state, "files", None), "recovery_required", False):
        return False, "review_recovery_required"
    return state.ready, state.reason


def _route_path(scope: Scope) -> str:
    path = scope["path"]
    root = scope.get("root_path", "").rstrip("/")
    if root and (path == root or path.startswith(f"{root}/")):
        return path[len(root) :]
    return path


def _allowed_host(scope: Scope, allowed: str, local_port: int) -> bool:
    hosts = [value for name, value in scope.get("headers", ()) if name.lower() == b"host"]
    if len(hosts) != 1:
        return False
    authority = hosts[0].lower()
    if authority == f"127.0.0.1:{local_port}".encode("ascii"):
        return True
    if authority.startswith(b"["):
        host, close, tail = authority[1:].partition(b"]")
        if not close or (tail and not tail.startswith(b":")):
            return False
        try:
            IPv6Address(host.decode("ascii"))
        except ValueError:
            return False
        separator, port = tail[:1], tail[1:]
    else:
        host, separator, port = authority.partition(b":")
    if host != allowed.lower().encode("ascii"):
        return False
    return not separator or (0 < len(port) <= 5 and port.isdigit() and 1 <= int(port) <= 65535)


class Admission:
    """Keep one admission counter for request capacity and lifespan draining."""

    def __init__(self, app: ASGIApp, state) -> None:
        self.app = app
        self.state = state
        self.http = state.settings.http

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        is_probe = _route_path(scope) in PROBES
        available = is_probe or self.state.ready
        correct_host = not self.http.host_restriction_enabled or _allowed_host(
            scope,
            self.http.allowed_host,
            self.http.port,
        )
        if kind == "websocket":
            if not available or not correct_host:
                await send({"type": "websocket.close", "code": 1008 if available else 1013})
            else:
                await self.app(scope, receive, send)
            return

        raw_trace = next((value for name, value in scope["headers"] if name.lower() == b"x-request-id"), b"")
        trace = raw_trace.decode("ascii") if _TRACE.fullmatch(raw_trace) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = trace
        started = False

        async def send_traced(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", ()))
                if not any(name.lower() == b"x-request-id" for name, _ in headers):
                    headers.append((b"x-request-id", trace.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        rejection = None
        if not available:
            rejection = (503, "SERVICE_UNAVAILABLE", "服务未就绪或正在关闭。")
        elif not correct_host:
            rejection = (400, "INVALID_HOST", "请通过允许的域名访问服务。")
        elif not is_probe and self.state.active_requests >= self.http.max_concurrent_requests:
            rejection = (503, "SERVER_BUSY", "服务正在处理过多请求，请稍后重试。")

        if rejection is not None:
            await error_response(Request(scope), *rejection)(scope, receive, send_traced)
            return

        drained = getattr(self.state, "drained", None)
        if not is_probe:
            self.state.active_requests += 1
            if drained is not None:
                drained.clear()

        try:
            await self.app(scope, receive, send_traced)
        except Exception as error:
            # Send the 500 inside admission so its final send is included in draining.
            if not started:
                response = internal_error(Request(scope), error)
                await response(scope, receive, send_traced)
            raise
        finally:
            if not is_probe:
                self.state.active_requests -= 1
                if drained is not None and self.state.active_requests == 0:
                    drained.set()
