"""Project and file reads, project administration and account management."""

from fastapi import APIRouter, Depends, Request

from projecthub.contracts import run_blocking
from projecthub.permissions import permission_catalog

from .dependencies import (
    AUTH_DOCS,
    PERMISSION_DOCS,
    Actor,
    Id,
    Model,
    Version,
    admin,
    authenticate,
    credential,
    curve_permission,
    download_permission,
)
from .protocol import respond, signed_data
from .schema import FILE_REFERENCE, PERMISSIONS_FIELDS, USER_FIELDS, command_schema
from .transfer import send_download

MODEL_PATH = "/projects/{model_id}"
VERSION_PATH = f"{MODEL_PATH}/versions/{{version_id}}"

catalog = APIRouter(dependencies=[Depends(authenticate)], responses=AUTH_DOCS)
accounts = APIRouter(tags=["用户与权限配置"], dependencies=[Depends(admin)])
configuration = APIRouter(tags=["客户端配置"])


@catalog.get("/me/permissions", summary="获取当前用户权限")
async def my_permissions(request: Request, actor: Actor):
    return respond(request, actor, no_store=True)


@catalog.put(
    "/me/password",
    summary="修改本人密码",
    openapi_extra=command_schema(
        {"new_password": "new-password"},
        fields={"new_password": {"type": "string", "minLength": 1, "writeOnly": True}},
        required=("new_password",),
    ),
)
async def change_password(request: Request, actor: Actor):
    data = await signed_data(request, {"new_password"}, {"new_password"})
    await run_blocking(
        request.app.state.users.change_password,
        actor,
        credential(request, "X-Password"),
        data["new_password"],
    )
    return respond(request, {"user_id": actor["user_id"], "changed": True}, no_store=True)


@accounts.get("/users", summary="获取用户列表")
async def list_users(request: Request, actor: Actor):
    users = await run_blocking(request.app.state.users.list_users, actor)
    return respond(request, {"users": users})


@accounts.post(
    "/users",
    summary="创建用户",
    status_code=201,
    openapi_extra=command_schema(
        {"name": "示例用户", "password": "example-password"},
        fields=USER_FIELDS,
        required=("name",),
    ),
)
async def create_user(request: Request, actor: Actor):
    data = await signed_data(request, USER_FIELDS, {"name"})
    return respond(request, await run_blocking(request.app.state.users.create_user, actor, data), 201)


@accounts.patch(
    "/users/{user_id}",
    summary="修改用户资料",
    openapi_extra=command_schema(
        {"phone": "00123"},
        fields=USER_FIELDS,
        minimum=1,
    ),
)
async def update_user(request: Request, actor: Actor, user_id: Id):
    data = await signed_data(request, USER_FIELDS)
    return respond(request, await run_blocking(request.app.state.users.update_user, actor, user_id, data))


@accounts.delete("/users/{user_id}", summary="删除用户")
async def delete_user(request: Request, actor: Actor, user_id: Id):
    await run_blocking(request.app.state.users.delete_user, actor, user_id)
    return respond(request, {"user_id": user_id, "deleted": True})


@accounts.get("/permissions", summary="获取可分配权限目录")
async def permission_directory(request: Request):
    return respond(request, {"permissions": permission_catalog()}, no_store=True)


@accounts.get("/users/{user_id}/permissions", summary="获取用户完整授权配置")
async def user_permissions(request: Request, actor: Actor, user_id: Id):
    data = await run_blocking(request.app.state.permissions.get_user_permissions, actor, user_id)
    return respond(request, data, no_store=True)


@accounts.put(
    "/users/{user_id}/permissions",
    summary="整体替换用户授权",
    openapi_extra=command_schema(
        {"global_permissions": [], "project_permissions": {"curve.manage": ["M920"]}},
        fields=PERMISSIONS_FIELDS,
        required=PERMISSIONS_FIELDS,
    ),
)
async def set_permissions(request: Request, actor: Actor, user_id: Id):
    data = await signed_data(request, PERMISSIONS_FIELDS, PERMISSIONS_FIELDS)
    result = await run_blocking(
        request.app.state.permissions.set_user_permissions,
        actor,
        user_id,
        data["global_permissions"],
        data["project_permissions"],
    )
    return respond(request, result, no_store=True)


@catalog.get("/projects", summary="获取项目型号列表")
async def list_models(request: Request, actor: Actor):
    models = await run_blocking(request.app.state.projects.list_project_models, actor)
    items = [
        {
            "id": model["id"],
            "name": model["name"],
            "description": model["description"],
            "reviewer_user_id": None,
        }
        for model in models
    ]
    return respond(
        request, {"projects": [model["name"] for model in models], "count": len(models), "items": items}
    )


@catalog.post(
    "/projects",
    status_code=201,
    summary="创建项目型号",
    dependencies=[Depends(admin)],
    responses=PERMISSION_DOCS,
    openapi_extra=command_schema({"name": "M920", "description": ""}, required=("name",)),
)
async def create_model(request: Request, actor: Actor):
    data = await signed_data(
        request,
        {"name", "description"},
        {"name"},
        strings=True,
        field_message="data 必须包含字符串 name，可选字符串 description，不允许其他字段。",
    )
    result = await run_blocking(
        request.app.state.projects.create_project_model,
        data["name"],
        data.get("description", ""),
        actor=actor,
    )
    return respond(request, result, 201)


@catalog.patch(
    MODEL_PATH,
    summary="修改项目型号",
    dependencies=[Depends(admin)],
    responses=PERMISSION_DOCS,
    openapi_extra=command_schema({"name": "M920", "description": ""}, minimum=1),
)
async def update_model(request: Request, actor: Actor, model: Model):
    changes = await signed_data(
        request,
        {"name", "description"},
        nonempty=True,
        strings=True,
        field_message="型号字段只能包含名称和描述。",
    )
    return respond(
        request,
        await run_blocking(
            request.app.state.projects.update_project_model,
            model["name"],
            changes,
            actor=actor,
        ),
    )


@catalog.get(f"{MODEL_PATH}/versions", summary="获取型号的版本列表")
async def list_versions(request: Request, actor: Actor, model: Model):
    projects = await run_blocking(request.app.state.projects.list_projects, actor)
    items = [project for project in projects if project["name"] == model["name"]]
    return respond(
        request,
        {
            "project": model["name"],
            "versions": [item["version"] for item in items],
            "count": len(items),
            "items": items,
        },
    )


@catalog.post(
    f"{MODEL_PATH}/versions",
    status_code=201,
    summary="创建型号下的项目版本",
    dependencies=[Depends(admin)],
    responses=PERMISSION_DOCS,
    openapi_extra=command_schema({"version": "低配版", "description": ""}, required=("version",)),
)
async def create_version(request: Request, actor: Actor, model: Model):
    data = await signed_data(
        request,
        {"version", "description"},
        {"version"},
        strings=True,
        field_message="data 必须包含字符串 version，可选字符串 description，不允许其他字段。",
    )
    result = await run_blocking(
        request.app.state.projects.create_project_version,
        model["name"],
        data["version"],
        data.get("description", ""),
        actor=actor,
    )
    return respond(request, result, 201)


@catalog.patch(
    VERSION_PATH,
    summary="修改项目版本",
    dependencies=[Depends(admin)],
    responses=PERMISSION_DOCS,
    openapi_extra=command_schema({"version": "低配版", "description": ""}, minimum=1),
)
async def update_version(request: Request, actor: Actor, project: Version):
    changes = await signed_data(
        request,
        {"version", "description"},
        nonempty=True,
        strings=True,
        field_message="版本字段只能包含版本名称和描述。",
    )
    return respond(
        request,
        await run_blocking(
            request.app.state.projects.update_project,
            project["id"],
            changes,
            actor=actor,
        ),
    )


@configuration.get(
    f"{VERSION_PATH}/config",
    summary="读取获授权型号的版本客户端配置",
    responses={
        **AUTH_DOCS,
        404: {"description": "型号/版本不可见或配置文件缺失"},
        422: {"description": "版本、路径或 JSON 无效"},
    },
)
async def version_config(request: Request, project: Version, actor: Actor):
    data = await run_blocking(request.app.state.files.catalog.read_project_config, project["id"], actor=actor)
    return respond(request, data, no_store=True)


@catalog.get(
    f"{VERSION_PATH}/curves/template",
    summary="读取项目曲线模板",
    dependencies=[Depends(curve_permission)],
    responses=PERMISSION_DOCS,
)
async def curve_template(request: Request, actor: Actor, project: Version):
    return respond(
        request,
        await run_blocking(request.app.state.files.catalog.read_curve_template, project["id"], actor=actor),
    )


@catalog.get(f"{VERSION_PATH}/firmwares/check", summary="检查固件更新")
async def firmware_update(request: Request, actor: Actor, project: Version, current_version: str):
    await download_permission(request, actor, project, "firmware")
    return respond(
        request,
        await run_blocking(
            request.app.state.files.catalog.check_firmware,
            project["id"],
            current_version,
            actor=actor,
        ),
    )


FILES = f"{VERSION_PATH}/files/{{category}}"
_DOWNLOAD = f"{FILES}/{{file_id}}/download"


@catalog.get(
    f"{FILES}/{{file_id}}/json",
    summary="读取 JSON 文件内容",
    dependencies=[Depends(download_permission)],
    responses=PERMISSION_DOCS,
)
async def file_json(request: Request, actor: Actor, project: Version, category: str, file_id: int):
    return respond(
        request,
        await run_blocking(
            request.app.state.files.catalog.read_json,
            file_id,
            category,
            project["id"],
            actor=actor,
        ),
    )


@catalog.post(
    f"{FILES}/json",
    summary="按文件编号读取 JSON 内容",
    openapi_extra=FILE_REFERENCE,
    dependencies=[Depends(download_permission)],
    responses=PERMISSION_DOCS,
)
async def signed_file_json(request: Request, actor: Actor, project: Version, category: str):
    data = await signed_data(
        request,
        {"file_id"},
        {"file_id"},
        file_reference=True,
        field_message="data 必须且只能包含 file_id。",
    )
    return respond(
        request,
        await run_blocking(
            request.app.state.files.catalog.read_json,
            data["file_id"],
            category,
            project["id"],
            actor=actor,
        ),
    )


@catalog.get(
    _DOWNLOAD, summary="下载原始文件", dependencies=[Depends(download_permission)], responses=PERMISSION_DOCS
)
@catalog.head(
    _DOWNLOAD,
    summary="获取文件下载信息",
    dependencies=[Depends(download_permission)],
    responses=PERMISSION_DOCS,
)
async def download(request: Request, actor: Actor, project: Version, category: str, file_id: int):
    return await send_download(
        request,
        request.app.state.files.catalog.prepare_download,
        file_id,
        category,
        project["id"],
        actor=actor,
    )


@catalog.get(
    FILES,
    summary="获取项目分类文件列表",
    dependencies=[Depends(download_permission)],
    responses=PERMISSION_DOCS,
)
async def list_files(
    request: Request,
    actor: Actor,
    project: Version,
    category: str,
    file_name: str | None = None,
    file_type: str | None = None,
    q: str | None = None,
    sha256: str | None = None,
    path: str | None = None,
):
    data = await run_blocking(
        request.app.state.files.catalog.list_files,
        project_id=project["id"],
        category=category,
        actor=actor,
        file_name=file_name,
        file_type=file_type,
        q=q,
        sha256=sha256,
        path=path,
    )
    return respond(request, data)
