"""File uploads, review actions, and download authorizations."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from projecthub.contracts import HubError, decode_json, run_blocking

from .catalog import VERSION_PATH
from .dependencies import (
    Actor,
    Id,
    Offset,
    PageSize,
    Version,
    authenticate,
    project_permission,
    review_scope,
)
from .protocol import SHA256_PATTERN, respond, signed_data
from .schema import AUTHORIZATION_FIELDS, command_schema
from .transfer import binary_body, receive_upload, send_download

changes = APIRouter(tags=["上传与审核"], dependencies=[Depends(authenticate)])
managed = APIRouter(tags=["内部与公共文件管理"])
authorizations = APIRouter(tags=["授权下载"])
_CURVES = f"{VERSION_PATH}/curves"
_FIRMWARES = f"{VERSION_PATH}/firmwares"
_CODES = f"{VERSION_PATH}/download-authorizations"
_PUBLIC_CODES = "/authorized-files/{authorization_code}"
_EMPTY_COMMAND = command_schema({})
_SHA256 = Annotated[str, Query(pattern=SHA256_PATTERN, description="原始文件字节的 SHA-256")]


@changes.put(
    f"{_CURVES}/{{file_id}}/position",
    summary="调整曲线排列位置",
    dependencies=[project_permission("curve.manage")],
    openapi_extra=command_schema(
        {"position": 1},
        fields={"position": {"type": "integer", "minimum": 1}},
        required=("position",),
    ),
)
async def set_curve_position(request: Request, actor: Actor, project: Version, file_id: Id):
    data = await signed_data(request, {"position"}, {"position"})
    return respond(
        request,
        await run_blocking(
            request.app.state.files.set_curve_position,
            actor,
            project["id"],
            file_id,
            data["position"],
        ),
    )


@changes.put(
    f"{_CURVES}/{{file_id}}",
    status_code=202,
    summary="修改曲线并提交审核",
    openapi_extra=binary_body("application/json"),
)
async def replace_curve(
    request: Request,
    actor: Actor,
    project: Version,
    file_id: Id,
    curve_type: str,
    description: str,
    file_sha256: _SHA256,
):
    return await receive_upload(
        request,
        actor,
        project["id"],
        "curve",
        None,
        {"curve_type": curve_type, "description": description},
        file_sha256,
        replaces=file_id,
    )


@changes.post(
    f"{_CURVES}/upload",
    status_code=202,
    summary="上传曲线并提交审核",
    openapi_extra=binary_body("application/json"),
)
async def upload_curve(
    request: Request,
    actor: Actor,
    project: Version,
    file_name: str,
    curve_type: str,
    description: str,
    file_sha256: _SHA256,
):
    return await receive_upload(
        request,
        actor,
        project["id"],
        "curve",
        file_name,
        {"curve_type": curve_type, "description": description},
        file_sha256,
    )


@changes.post(
    f"{_FIRMWARES}/upload",
    status_code=202,
    summary="上传固件并提交审核",
    openapi_extra=binary_body("application/octet-stream"),
)
async def upload_firmware(
    request: Request,
    actor: Actor,
    project: Version,
    file_name: str,
    firmware_type: str,
    version: str,
    release_notes: str,
    min_version: str,
    max_version: str,
    file_sha256: _SHA256,
    supported_models: Annotated[str | None, Query(description="支持型号的 JSON 字符串数组")] = None,
):
    fields = {
        "firmware_type": firmware_type,
        "version": version,
        "release_notes": release_notes,
        "min_version": min_version,
        "max_version": max_version,
    }
    if supported_models is not None:
        try:
            fields["supported_models"] = decode_json(supported_models)
        except ValueError, RecursionError:
            raise HubError("INVALID_UPLOAD", "supported_models 必须是有效 JSON 型号数组。") from None
    return await receive_upload(request, actor, project["id"], "firmware", file_name, fields, file_sha256)


@changes.post(
    f"{_CURVES}/{{file_id}}/delete",
    status_code=202,
    summary="申请删除曲线",
    dependencies=[project_permission("curve.manage")],
    openapi_extra=_EMPTY_COMMAND,
)
async def request_curve_delete(request: Request, actor: Actor, project: Version, file_id: Id):
    await signed_data(request)
    return respond(
        request,
        await run_blocking(
            request.app.state.files.reviews.request_curve_delete,
            actor,
            project["id"],
            file_id,
        ),
        202,
    )


@changes.post(
    f"{_FIRMWARES}/{{file_id}}/delete",
    status_code=202,
    summary="申请删除固件",
    dependencies=[project_permission("firmware.upload")],
    openapi_extra=_EMPTY_COMMAND,
)
async def request_firmware_delete(request: Request, actor: Actor, project: Version, file_id: Id):
    await signed_data(request)
    return respond(
        request,
        await run_blocking(
            request.app.state.files.reviews.request_firmware_delete,
            actor,
            project["id"],
            file_id,
        ),
        202,
    )


@changes.get("/reviews", summary="获取待审核列表")
async def reviews(
    request: Request,
    actor: Actor,
    project_id: Annotated[int | None, Depends(review_scope)],
    offset: Offset = 0,
    limit: PageSize = 50,
):
    return respond(
        request,
        await run_blocking(
            request.app.state.files.reviews.list_reviews,
            actor,
            project_id=project_id,
            offset=offset,
            limit=limit,
        ),
    )


@changes.get("/reviews/{review_id}", summary="获取待审核详情")
async def review(request: Request, actor: Actor, review_id: Id):
    return respond(request, await run_blocking(request.app.state.files.reviews.get_review, actor, review_id))


@changes.get("/reviews/{review_id}/download", summary="下载待审核文件")
async def review_download(request: Request, actor: Actor, review_id: Id):
    return await send_download(
        request, request.app.state.files.reviews.prepare_review_download, actor, review_id
    )


@changes.post("/reviews/{review_id}/approve", summary="通过审核并执行文件变更", openapi_extra=_EMPTY_COMMAND)
async def approve(request: Request, actor: Actor, review_id: Id):
    files = request.app.state.files
    await run_blocking(files.reviews.get_review, actor, review_id)
    await signed_data(request)
    return respond(request, await run_blocking(files.reviews.approve_review, actor, review_id))


@changes.post("/reviews/{review_id}/reject", summary="驳回文件变更", openapi_extra=_EMPTY_COMMAND)
async def reject(request: Request, actor: Actor, review_id: Id):
    files = request.app.state.files
    await run_blocking(files.reviews.get_review, actor, review_id)
    await signed_data(request)
    return respond(request, await run_blocking(files.reviews.reject_review, actor, review_id))


def _add_managed_category(category: str, label: str) -> None:
    path = f"{VERSION_PATH}/{category}-files"
    gates = [project_permission(f"{category}.manage")]

    @managed.post(
        f"{path}/upload",
        status_code=201,
        summary=f"上传并发布{label}",
        dependencies=gates,
        openapi_extra=binary_body("application/octet-stream"),
    )
    async def upload(
        request: Request,
        actor: Actor,
        project: Version,
        file_name: str,
        file_sha256: _SHA256,
        description: str = "",
    ):
        return await receive_upload(
            request,
            actor,
            project["id"],
            category,
            file_name,
            {"description": description},
            file_sha256,
        )

    @managed.delete(f"{path}/{{file_id}}", summary=f"备份并删除{label}", dependencies=gates)
    async def delete(request: Request, actor: Actor, project: Version, file_id: Id):
        return respond(
            request,
            await run_blocking(
                request.app.state.files.delete_managed_file,
                actor,
                project["id"],
                category,
                file_id,
            ),
        )


_add_managed_category("internal", "内部文件")
_add_managed_category("public", "公共文件")


@authorizations.get(
    _CODES, summary="查询项目下载授权", dependencies=[project_permission("authorization.list")]
)
async def list_codes(
    request: Request, actor: Actor, project: Version, offset: Offset = 0, limit: PageSize = 50
):
    data = await run_blocking(
        request.app.state.files.authorizations.list_download_authorizations,
        actor,
        project["id"],
        offset=offset,
        limit=limit,
    )
    return respond(request, data, no_store=True)


@authorizations.post(
    _CODES,
    status_code=201,
    summary="创建限时下载授权",
    dependencies=[project_permission("authorization.manage")],
    openapi_extra=command_schema(
        {
            "path": "M910/低配版/curve/曲线.json",
            "expires_at": "2030-12-31",
            "authorization_code": "001122334455aabb",
            "device_code": "DEVICE-001",
        },
        fields=AUTHORIZATION_FIELDS,
        required=AUTHORIZATION_FIELDS,
    ),
)
async def create_code(request: Request, actor: Actor, project: Version):
    data = await signed_data(request, AUTHORIZATION_FIELDS, AUTHORIZATION_FIELDS)
    result = await run_blocking(
        request.app.state.files.authorizations.create_download_authorization,
        actor,
        project["id"],
        data["path"],
        data["expires_at"],
        data["authorization_code"],
        data["device_code"],
    )
    return respond(request, result, 201, no_store=True)


@authorizations.delete(
    f"{_CODES}/{{authorization_id}}",
    summary="撤销下载授权",
    dependencies=[project_permission("authorization.manage")],
)
async def delete_code(request: Request, actor: Actor, project: Version, authorization_id: Id):
    data = await run_blocking(
        request.app.state.files.authorizations.delete_download_authorization,
        actor,
        project["id"],
        authorization_id,
    )
    return respond(request, data, no_store=True)


@authorizations.get(_PUBLIC_CODES, summary="凭授权码读取授权信息")
async def code_info(request: Request, authorization_code: str):
    data = await run_blocking(
        request.app.state.files.authorizations.get_download_authorization, authorization_code
    )
    return respond(request, data, no_store=True)


@authorizations.get(f"{_PUBLIC_CODES}/json", summary="凭授权码读取 JSON 内容")
async def code_json(request: Request, authorization_code: str):
    data = await run_blocking(request.app.state.files.authorizations.read_authorized_json, authorization_code)
    return respond(request, data, no_store=True)


@authorizations.get(f"{_PUBLIC_CODES}/download", summary="凭授权码下载原始文件")
async def code_download(request: Request, authorization_code: str):
    return await send_download(
        request, request.app.state.files.authorizations.prepare_authorized_download, authorization_code
    )
