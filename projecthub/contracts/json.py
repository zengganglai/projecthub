"""严格 JSON 和 RFC 8785 摘要；文件原字节摘要不使用本模块。"""

import hashlib
import json
import math
import time

import rfc8785


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON 包含重复字段。")
        value[key] = item
    return value


def _number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("JSON 数字必须有限。")
    return value


def _invalid_number(text):
    raise ValueError("JSON 数字必须有限。")


def decode_json(content: str | bytes):
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return json.loads(content, object_pairs_hook=_object, parse_float=_number, parse_constant=_invalid_number)


def digest_data(data) -> str:
    return hashlib.sha256(rfc8785.dumps(data)).hexdigest()


def encode_response(identifier, status: int, data) -> dict:
    return dict(id=identifier, status=status, timestamp=int(time.time()), data=data, sha256=digest_data(data))


def json_object(content: bytes) -> dict:
    value = decode_json(content.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON 文件顶层必须是对象。")
    digest_data(value)
    return value
