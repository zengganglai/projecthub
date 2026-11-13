"""OpenAPI request examples for bodies the adapter reads as a bounded stream."""

from projecthub.contracts import digest_data
from projecthub.permissions import PROJECT_PERMISSIONS

from .protocol import SAFE_ID_MAX, SHA256_PATTERN

_TEXT = {"type": "string"}


def command_schema(example: dict, *, fields: dict | None = None, required=(), minimum: int = 0) -> dict:
    properties = fields if fields is not None else {key: _TEXT for key in example}
    data = {"type": "object", "additionalProperties": False, "properties": properties}
    if required:
        data["required"] = list(required)
    if minimum:
        data["minProperties"] = minimum
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "data", "sha256"],
        "properties": {
            "id": {
                "oneOf": [{"type": "string", "minLength": 1}, {"type": "integer"}],
                "description": "客户端请求 ID，响应原样回显。",
            },
            "data": data,
            "sha256": {
                "type": "string",
                "pattern": SHA256_PATTERN,
                "description": "原始 data 按 RFC 8785 序列化后的 SHA-256。Swagger 执行时自动重算。",
            },
        },
    }
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": schema,
                    "example": {"id": "example-1", "data": example, "sha256": digest_data(example)},
                }
            },
        }
    }


USER_FIELDS = {
    "name": {"type": "string", "minLength": 1},
    "employee_number": {"type": ["string", "null"]},
    "phone": _TEXT,
    "password": {"type": "string", "writeOnly": True},
}
PERMISSIONS_FIELDS = {
    "global_permissions": {
        "type": "array",
        "maxItems": 0,
        "description": "保留字段，必须为 []；权限统一在 project_permissions 中按型号配置。",
    },
    "project_permissions": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            permission: {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}}
            for permission in sorted(PROJECT_PERMISSIONS)
        },
    },
}
AUTHORIZATION_FIELDS = {
    "path": {"type": "string", "maxLength": 1024},
    "expires_at": {"type": "string", "description": "北京时间日期 YYYY-MM-DD，或带时区的未来 ISO 时间。"},
    "authorization_code": {"type": "string", "pattern": "^[0-9a-fA-F]{16}$"},
    "device_code": {"type": "string", "minLength": 1, "maxLength": 128},
}
FILE_REFERENCE = command_schema(
    {"file_id": 1},
    fields={"file_id": {"type": "integer", "minimum": 1, "maximum": SAFE_ID_MAX}},
    required=("file_id",),
)
