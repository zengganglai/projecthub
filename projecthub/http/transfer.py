"""Own the synchronous file handles for the complete asynchronous response."""

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from projecthub.contracts import HubError, finish_task, run_blocking

from .protocol import respond


async def open_handle(operation, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await finish_task(task)
    except asyncio.CancelledError:
        if not task.cancelled():
            await run_blocking(task.result().close)
        raise


def binary_body(media_type: str) -> dict:
    return {
        "requestBody": {
            "required": True,
            "content": {
                media_type: {
                    "schema": {"type": "string", "format": "binary"},
                    "description": "file_sha256 是原始请求体字节的 SHA-256。",
                }
            },
        }
    }


async def receive_upload(
    request: Request,
    actor: dict,
    project_id: int,
    category: str,
    file_name: str | None,
    fields: dict,
    sha256: str,
    *,
    replaces: int | None = None,
) -> Response:
    media_type = "application/json" if category == "curve" else "application/octet-stream"
    if request.headers.get("content-type", "").partition(";")[0].lower() != media_type:
        raise HubError("INVALID_UPLOAD_CONTENT_TYPE", f"文件请求体须使用 {media_type}。")
    files = request.app.state.files
    if replaces is None:
        handle = await open_handle(files.begin_upload, actor, project_id, category, file_name, fields, sha256)
    else:
        handle = await open_handle(files.begin_curve_update, actor, project_id, replaces, fields, sha256)

    try:
        async for chunk in request.stream():
            if chunk:
                await run_blocking(handle.write, chunk)
        result = await run_blocking(handle.finish)
        return respond(request, result, 201 if category in ("internal", "public") else 202)
    finally:
        await run_blocking(handle.close)


async def _chunks(handle) -> AsyncIterator[bytes]:
    while chunk := await run_blocking(handle.read_chunk, 1024 * 1024):
        yield chunk


class _DownloadStream(StreamingResponse):
    def __init__(self, handle, headers: dict[str, str]) -> None:
        self.handle = handle
        super().__init__(_chunks(handle), headers=headers, media_type="application/octet-stream")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_blocking(self.handle.close)


async def send_download(request: Request, operation, *args, **kwargs) -> Response:
    handle = await open_handle(operation, *args, **kwargs)
    try:
        encoded_name = quote(handle.file_name, safe="")
        headers = {
            "Content-Disposition": f"""attachment; filename="download"; filename*=UTF-8''{encoded_name}""",
            "Content-Length": str(handle.size),
            "X-Content-SHA256": handle.sha256,
            "Cache-Control": "no-store",
        }
        if request.method != "HEAD":
            return _DownloadStream(handle, headers)
    except BaseException:
        await run_blocking(handle.close)
        raise
    await run_blocking(handle.close)
    return Response(media_type="application/octet-stream", headers=headers)
