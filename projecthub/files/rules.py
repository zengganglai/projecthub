"""文件分类、上传字段及固件版本规则。"""

import ntpath
import re
from datetime import datetime, timedelta, timezone

from projecthub.contracts import HubError
from projecthub.storage import HISTORY

TABLES = {
    "curve": "curve_configs",
    "firmware": "firmwares",
    "internal": "internal_files",
    "public": "public_files",
}


FIELDS = {
    "curve": {"curve_name", "curve_type", "description"},
    "firmware": {
        "firmware_type",
        "version",
        "release_notes",
        "min_version",
        "max_version",
        "supported_models",
    },
    "internal": {"description"},
    "public": {"description"},
}


WRITE_PERMISSIONS = {
    "curve": "curve.manage",
    "firmware": "firmware.upload",
    "internal": "internal.manage",
    "public": "public.manage",
}


def utc_text(timestamp_ns=None):
    # 文件时间按整纳秒截断至微秒，避免 float timestamp 舍入改变目录元数据。
    current = (
        datetime.now(timezone.utc)
        if timestamp_ns is None
        else datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=timestamp_ns // 1000)
    )
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def version_key(value):
    if (
        not isinstance(value, str)
        or len(value) > 128
        or re.fullmatch(r"[vV]?[0-9]+(?:\.[0-9]+){0,4}", value) is None
    ):
        raise HubError("INVALID_FIRMWARE_VERSION", "版本须为可选 v 前缀及 1～5 段整数。")
    numbers = [int(p) for p in value.lstrip("vV").split(".")]
    return tuple(numbers + [0] * (5 - len(numbers)))


def version_range(low, high):
    lower, upper = version_key(low), version_key(high)
    if lower > upper:
        raise HubError("INVALID_FIRMWARE_VERSION", "最低版本不能高于最高版本。")
    return None if not any(lower + upper) else (lower, upper)


def upload_fields(category, name, fields, digest):
    if category not in FIELDS or not isinstance(fields, dict):
        raise HubError("INVALID_UPLOAD", "文件分类或字段无效。")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 255
        or name in (".", "..")
        or any(c in name for c in "/\\:")
        or ntpath.isreserved(name)
        or HISTORY.search(name)
    ):
        raise HubError("INVALID_UPLOAD", "上传文件名无效。")
    if category == "curve" and not name.lower().endswith(".json"):
        raise HubError("INVALID_UPLOAD", "曲线须使用 JSON 后缀。")
    if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
        raise HubError("INVALID_SHA256", "文件摘要须为 64 位小写十六进制。")
    mandatory = FIELDS[category] - {"supported_models"}
    if not mandatory <= fields.keys() <= FIELDS[category] or any(
        not isinstance(fields[key], str) for key in mandatory
    ):
        raise HubError("INVALID_UPLOAD", "业务字段缺失、类型错误或存在未知字段。")
    result = dict(fields)
    if category in ("curve", "firmware") and not fields[category + "_type"].strip():
        raise HubError("INVALID_UPLOAD", "业务类型不能为空。")
    if category == "curve" and not fields["curve_name"].strip():
        raise HubError("INVALID_UPLOAD", "曲线名称不能为空。")
    if category == "firmware":
        try:
            version_key(fields["version"])
            version_range(fields["min_version"], fields["max_version"])
        except HubError as error:
            raise HubError("INVALID_UPLOAD", error.message) from error
        if "supported_models" in fields:
            models = fields["supported_models"]
            if not isinstance(models, list):
                raise HubError("INVALID_UPLOAD", "支持型号必须是数组。")
            seen = set()
            for model in models:
                if (
                    not isinstance(model, str)
                    or not model
                    or model.strip() != model
                    or len(model.encode("utf-16-le", errors="surrogatepass")) > 256
                    or any(c in model for c in "/\\:")
                    or ntpath.isreserved(model)
                    or model.casefold() in seen
                ):
                    raise HubError("INVALID_UPLOAD", "支持型号无效或重复。")
                try:
                    model.encode("utf-8")
                except UnicodeError as error:
                    raise HubError("INVALID_UPLOAD", "支持型号编码无效。") from error
                seen.add(model.casefold())
            result["supported_models"] = list(models)
    return result


def validate_pagination(offset, limit):
    if (
        type(offset) is not int
        or not 0 <= offset <= 9007199254740991
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise HubError("INVALID_DATA", "分页参数无效。")
