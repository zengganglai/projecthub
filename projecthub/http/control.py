"""HTTP bindings for the separate VNC control protocol."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from projecthub.remote import RemoteError

from .protocol import BodyLimitExceeded, read_json

_STATUS = {
    "AUTH_REQUIRED": 401,
    "AUTH_FAILED": 401,
    "SESSION_OWNER": 403,
    "SNAPSHOT_OWNER": 403,
    "CLIENT_ADDRESS_REQUIRED": 403,
    "REQUEST_NOT_FOUND": 404,
    "SESSION_NOT_FOUND": 404,
    "DEVICE_OFFLINE": 404,
    "SNAPSHOT_EXPIRED": 404,
    "REQUEST_TERMINATED_OR_CHANGED": 409,
    "CONTROL_BUSY": 409,
    "SESSION_TERMINATED": 409,
    "BODY_TOO_LARGE": 413,
    "INVALID_LIMIT": 422,
    "INVALID_CURSOR": 422,
    "INVALID_REQUEST": 422,
    "REQUEST_CAPACITY": 429,
    "SNAPSHOT_CAPACITY": 429,
    "REMOTE_UNAVAILABLE": 503,
}
_NO_STORE = {"Cache-Control": "no-store"}
_REQUEST_PATTERN = r"^[0-9a-f]{32}$"


class _ControlRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def call(request):
            try:
                return await handler(request)
            except RemoteError as error:
                status = _STATUS.get(error.code, 500)
                headers = dict(_NO_STORE)
                if status == 401:
                    headers["WWW-Authenticate"] = "Bearer"
                return JSONResponse({"error": {"code": error.code}}, status_code=status, headers=headers)

        return call


class _Acquire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    deviceId: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    requestId: Annotated[str, Field(pattern=_REQUEST_PATTERN)]


_bearer = HTTPBearer(auto_error=False, scheme_name="VNCBearer")


async def owner_address(
    request: Request,
    response: Response,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    request.app.state.remote.authenticate_api(credentials.credentials if credentials else None)
    if request.client is None:
        raise RemoteError("CLIENT_ADDRESS_REQUIRED")
    response.headers.update(_NO_STORE)
    return request.client.host


Owner = Annotated[str, Depends(owner_address)]
RequestId = Annotated[str, Path(pattern=_REQUEST_PATTERN)]
SessionId = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
control = APIRouter(tags=["VNC 远程控制"], route_class=_ControlRoute)


@control.get("/devices", summary="获取在线设备列表")
async def devices(
    request: Request,
    owner: Owner,
    status: Literal["online"] = "online",
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
):
    return request.app.state.remote.list_devices(limit, cursor, owner)


@control.post(
    "/control-sessions",
    status_code=201,
    summary="申请远程控制会话",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _Acquire.model_json_schema()}},
        },
    },
)
async def acquire(request: Request, owner: Owner):
    try:
        payload = _Acquire.model_validate(await read_json(request, 4096))
    except BodyLimitExceeded:
        raise RemoteError("BODY_TOO_LARGE") from None
    except ValueError, RecursionError:
        raise RemoteError("INVALID_REQUEST") from None
    return await request.app.state.remote.acquire(payload.deviceId, payload.requestId, owner)


@control.get("/control-sessions/by-request/{request_id}", summary="按请求编号查询远程控制会话")
async def lookup(request: Request, owner: Owner, request_id: RequestId):
    return await request.app.state.remote.lookup(request_id, owner)


@control.delete("/control-sessions/by-request/{request_id}", status_code=204, summary="取消远程控制申请")
async def cancel(request: Request, owner: Owner, request_id: RequestId):
    await request.app.state.remote.cancel(request_id, owner)
    return Response(status_code=204, headers=_NO_STORE)


@control.post("/control-sessions/{session_id}/renew", summary="续期远程控制会话")
async def renew(request: Request, owner: Owner, session_id: SessionId):
    return await request.app.state.remote.renew(session_id, owner)


@control.delete("/control-sessions/{session_id}", status_code=204, summary="释放远程控制会话")
async def release(request: Request, owner: Owner, session_id: SessionId):
    await request.app.state.remote.release(session_id, owner)
    return Response(status_code=204, headers=_NO_STORE)


@control.get("/remote-control/health", summary="获取远程控制服务状态")
async def control_health(request: Request, owner: Owner):
    return request.app.state.remote.health()
