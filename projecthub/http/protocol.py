"""Shared wire format and error responses for the project and file API."""

import asyncio
import hmac
import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from rfc8785 import CanonicalizationError
from starlette.exceptions import HTTPException

from projecthub.contracts import HubError, decode_json, digest_data, encode_response

SAFE_ID_MAX = (1 << 53) - 1
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_HEX_DIGEST = re.compile(SHA256_PATTERN)
_LOG = logging.getLogger(__name__)
_STATUS_BY_CODE = {
    "AUTHENTICATION_FAILED": 401,
    "PERMISSION_DENIED": 403,
    "AUTHORIZATION_NOT_FOUND": 404,
    "FILE_NOT_FOUND": 404,
    "PROJECT_CONFIG_NOT_FOUND": 404,
    "PROJECT_NOT_FOUND": 404,
    "REVIEW_NOT_FOUND": 404,
    "TEMPLATE_NOT_FOUND": 404,
    "USER_NOT_FOUND": 404,
    "AUTHORIZATION_CODE_EXISTS": 409,
    "AUTHORIZATION_HASH_MISMATCH": 409,
    "AUTHORIZATION_INVALID": 409,
    "CATALOG_STALE": 409,
    "CURVE_NAME_ALREADY_EXISTS": 409,
    "FILE_ALREADY_EXISTS": 409,
    "PROJECT_CONFLICT": 409,
    "PROJECT_EXISTS": 409,
    "PROJECT_PATH_CONFLICT": 409,
    "PROJECT_PATH_EXISTS": 409,
    "REVIEW_ALREADY_PENDING": 409,
    "REVIEW_CONFLICT": 409,
    "REVIEW_HASH_MISMATCH": 409,
    "REVIEW_REQUIRED": 409,
    "UPLOAD_CLOSED": 409,
    "USER_CONFLICT": 409,
    "USER_LIMIT_REACHED": 409,
    "USER_REFERENCED": 409,
    "AUTHORIZATION_EXPIRED": 410,
    "UPLOAD_TOO_LARGE": 413,
    "INVALID_UPLOAD_CONTENT_TYPE": 415,
    "NOT_JSON_FILE": 415,
    "INVALID_AUTHORIZATION_CODE": 422,
    "INVALID_CATEGORY": 422,
    "INVALID_DATA": 422,
    "INVALID_FILTER": 422,
    "INVALID_FIRMWARE_VERSION": 422,
    "INVALID_PROJECT_DESCRIPTION": 422,
    "INVALID_PROJECT_NAME": 422,
    "INVALID_SHA256": 422,
    "INVALID_UPLOAD": 422,
    "INVALID_USER": 422,
    "PROJECT_CONFIG_INVALID_JSON": 422,
    "PROJECT_CONFIG_PATH_INVALID": 422,
    "TEMPLATE_INVALID_JSON": 422,
    "TEMPLATE_PATH_INVALID": 422,
    "UPLOAD_HASH_MISMATCH": 422,
    "FILE_SERVICE_NOT_READY": 503,
    "REVIEW_RECOVERY_FAILED": 503,
    "USERS_NOT_READY": 503,
}


class BodyLimitExceeded(ValueError):
    """The bytes actually received exceeded the route's allowance."""


async def read_json(request: Request, limit: int):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise BodyLimitExceeded
        body.extend(chunk)
    return decode_json(bytes(body))


def valid_id(value: object) -> bool:
    if type(value) is int:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


async def signed_data(
    request: Request,
    allowed=(),
    required=(),
    *,
    strings=False,
    nonempty=False,
    file_reference=False,
    field_message="请求业务字段无效。",
) -> dict:
    """Validate the wire object against endpoint fields before testing its digest."""
    try:
        payload = await read_json(request, request.app.state.settings.http.max_json_body_bytes)
    except BodyLimitExceeded:
        raise HubError("BODY_TOO_LARGE", "请求正文超过字节上限。", 413) from None
    except ValueError, RecursionError:
        raise HubError("INVALID_REQUEST", "请求体不是有效 JSON。", 422) from None

    if not isinstance(payload, dict):
        raise HubError("INVALID_REQUEST", "请求体必须是 JSON 对象。", 422)
    client_id = payload.get("id")
    if not valid_id(client_id):
        raise HubError("INVALID_ID", "id 必须是有效 UTF-8 非空字符串或非布尔整数。", 422)
    request.state.client_id = client_id
    if payload.keys() != {"id", "data", "sha256"}:
        raise HubError("INVALID_REQUEST", "请求体只能包含 id、data 和 sha256。", 422)

    data = payload["data"]
    if (
        not isinstance(data, dict)
        or data.keys() - set(allowed)
        or set(required) - data.keys()
        or (nonempty and not data)
        or (strings and any(not isinstance(value, str) for value in data.values()))
    ):
        raise HubError("INVALID_DATA", field_message, 422)
    if file_reference:
        file_id = data["file_id"]
        if type(file_id) is not int or not 1 <= file_id <= SAFE_ID_MAX:
            raise HubError("INVALID_FILE_ID", "file_id 必须是 RFC 8785 可表示的正整数。", 422)

    supplied = payload["sha256"]
    if not isinstance(supplied, str) or not _HEX_DIGEST.fullmatch(supplied):
        raise HubError("INVALID_SHA256", "sha256 必须是 64 位小写十六进制值。", 422)
    try:
        calculated = digest_data(data)
    except CanonicalizationError, UnicodeError, RecursionError:
        raise HubError("INVALID_DATA", "data 无法按 RFC 8785 计算摘要。", 422) from None
    if not hmac.compare_digest(calculated, supplied):
        raise HubError("SHA256_MISMATCH", "data 摘要与 sha256 不一致。", 422)
    return data


def respond(request: Request, data, status: int = 200, *, no_store: bool = False) -> JSONResponse:
    identifier = getattr(request.state, "client_id", None)
    if not valid_id(identifier):
        identifier = getattr(request.state, "request_id", "unknown")
    headers = {"Cache-Control": "no-store"} if no_store else None
    return JSONResponse(encode_response(identifier, status, data), status_code=status, headers=headers)


def error_response(request: Request, status: int, code: str, message: str) -> Response:
    no_store = code.startswith(("AUTHORIZATION_", "PROJECT_CONFIG_")) or code == "AUTHENTICATION_FAILED"
    if request.method == "HEAD":
        response = Response(status_code=status)
        if no_store:
            response.headers["Cache-Control"] = "no-store"
    else:
        response = respond(request, {"error": {"code": code, "message": message}}, status, no_store=no_store)
    if code == "AUTHENTICATION_FAILED":
        response.headers["WWW-Authenticate"] = 'Password realm="FileTrail"'
    return response


def internal_error(request: Request, error: Exception) -> Response:
    _LOG.error(
        "HTTP 未处理异常 request_id=%s type=%s",
        getattr(request.state, "request_id", "unknown"),
        type(error).__name__,
    )
    return error_response(request, 500, "INTERNAL_ERROR", "服务内部错误。")


def register_errors(app: FastAPI) -> None:
    @app.exception_handler(HubError)
    async def business_error(request: Request, error: HubError) -> Response:
        status = error.status if error.status is not None else _STATUS_BY_CODE.get(error.code, 500)
        return error_response(request, status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        return error_response(request, 422, "VALIDATION_ERROR", "请求参数校验失败。")

    @app.exception_handler(HTTPException)
    async def routing_error(request: Request, error: HTTPException) -> Response:
        if request.method == "POST" and error.status_code in (404, 405):
            try:
                async with asyncio.timeout(0.1):
                    payload = await read_json(request, 4096)
            except ValueError, RecursionError, RuntimeError, TimeoutError:
                payload = None
            if isinstance(payload, dict) and valid_id(payload.get("id")):
                request.state.client_id = payload["id"]

        code, message = {
            404: ("NOT_FOUND", "接口不存在。"),
            405: ("METHOD_NOT_ALLOWED", "请求方法不允许。"),
        }.get(error.status_code, ("HTTP_ERROR", "请求无法完成。"))
        response = error_response(request, error.status_code, code, message)
        if error.headers:
            response.headers.update(error.headers)
        return response
