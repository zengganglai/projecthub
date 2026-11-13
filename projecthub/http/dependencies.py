"""HTTP credentials, permission gates and version ownership resolution."""

import re
from typing import Annotated
from urllib.parse import unquote_to_bytes

from fastapi import Depends, Header, Path, Query, Request

from projecthub.contracts import HubError, run_blocking

from .protocol import SAFE_ID_MAX

Id = Annotated[int, Path(ge=1, le=SAFE_ID_MAX)]
Offset = Annotated[int, Query(ge=0, le=SAFE_ID_MAX)]
PageSize = Annotated[int, Query(ge=1, le=100)]
AUTH_DOCS = {401: {"description": "用户姓名或密码缺失或不正确；HEAD 无正文。"}}
PERMISSION_DOCS = {403: {"description": "身份验证成功但缺少操作权限；HEAD 无正文。"}}


def credential(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) == 1:
        raw = values[0]
        if raw.isascii() and not re.search(r"%(?![a-fA-F0-9]{2})", raw):
            try:
                value = unquote_to_bytes(raw).decode("utf-8")
            except UnicodeError:
                pass
            else:
                if value.strip():
                    return value
    raise HubError("AUTHENTICATION_FAILED", "用户姓名或密码缺失或不正确。")


async def authenticate(
    request: Request,
    x_user_name: Annotated[
        str | None,
        Header(
            alias="X-User-Name",
            description="原始姓名；Swagger 自动编码，其他客户端使用 UTF-8 百分号编码。",
        ),
    ] = None,
    x_password: Annotated[
        str | None,
        Header(
            alias="X-Password",
            description="原始密码；Swagger 自动编码，输入时无需预编码。",
        ),
    ] = None,
) -> dict:
    users = request.app.state.users
    return await run_blocking(
        users.authenticate, credential(request, "X-User-Name"), credential(request, "X-Password")
    )


Actor = Annotated[dict, Depends(authenticate)]


async def admin(request: Request, actor: Actor) -> None:
    await run_blocking(request.app.state.permissions.require_admin, actor)


async def locate_model(request: Request, model_id: Id, actor: Actor) -> dict:
    result = await run_blocking(request.app.state.projects.get_project_model, model_id, actor=actor)
    if result is None:
        raise HubError("PROJECT_NOT_FOUND", "型号不存在。")
    return result


async def locate_version(request: Request, model_id: Id, version_id: Id, actor: Actor) -> dict:
    result = await run_blocking(
        request.app.state.projects.get_project_version, model_id, version_id, actor=actor
    )
    if result is None:
        raise HubError("PROJECT_NOT_FOUND", "型号或所属版本不存在。")
    return result


Model = Annotated[dict, Depends(locate_model)]
Version = Annotated[dict, Depends(locate_version)]


async def review_scope(
    request: Request,
    actor: Actor,
    model_id: Annotated[int | None, Query(ge=1, le=SAFE_ID_MAX)] = None,
    version_id: Annotated[int | None, Query(ge=1, le=SAFE_ID_MAX)] = None,
) -> int | None:
    if model_id is None and version_id is None:
        return None
    if model_id is None or version_id is None:
        raise HubError("INVALID_DATA", "筛选审核列表时须同时提供 model_id 和 version_id。")
    return (await locate_version(request, model_id, version_id, actor))["id"]


def project_permission(permission: str):
    async def check(request: Request, actor: Actor, project: Version) -> None:
        await run_blocking(request.app.state.permissions.require, actor, permission, project["id"])

    return Depends(check)


async def download_permission(request: Request, actor: Actor, project: Version, category: str) -> None:
    await run_blocking(request.app.state.permissions.require_file_download, actor, category, project["id"])


async def curve_permission(request: Request, actor: Actor, project: Version) -> None:
    await download_permission(request, actor, project, "curve")
