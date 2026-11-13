"""公开权限目录与型号授权 JSON 的校验规则。"""

from collections.abc import Collection

from projecthub.contracts import decode_json

_CATALOG = (
    ("factory.debug", "工厂调试", "显示客户端工厂调试界面；文件读取、上传及审核需分别授权"),
    ("dealer", "代理商", "显示客户端当前项目的代理商功能；文件操作需分别授权"),
    ("curve.read", "曲线读取", "读取曲线列表、JSON、模板、原始文件及 HEAD 信息"),
    ("curve.manage", "曲线管理", "提交新增、修改及删除曲线审核；读取与审核需分别授权"),
    ("curve.review", "曲线审核", "查看本项目曲线待审、下载待审文件、通过或驳回"),
    ("firmware.read", "固件读取", "读取本型号全部版本的固件列表、JSON、检查升级、下载及 HEAD 信息"),
    ("firmware.upload", "固件上传", "提交固件及字段审核、申请删除本项目固件；审核需分别授权"),
    ("firmware.review", "固件审核", "查看本项目固件待审、下载待审文件、通过或驳回"),
    ("internal.read", "内部文件读取", "读取内部文件列表、JSON、原始文件及 HEAD 信息"),
    ("internal.manage", "内部文件管理", "直接上传内部文件，备份并直接删除；读取需单独授权"),
    ("public.read", "公共文件读取", "读取本型号全部版本的公共文件列表、JSON、原始文件及 HEAD 信息"),
    ("public.manage", "公共文件管理", "直接上传、备份及删除本型号的公共文件；读取需单独授权"),
    ("authorization.list", "授权查询", "查询本项目下载授权列表"),
    ("authorization.manage", "授权管理", "创建和删除本项目下载授权；列表查询需单独授权"),
)
READ_PERMISSIONS = {category: f"{category}.read" for category in ("curve", "firmware", "internal", "public")}
REVIEW_PERMISSIONS = {"curve": "curve.review", "firmware": "firmware.review"}
PROJECT_PERMISSIONS = frozenset(code for code, _, _ in _CATALOG)


def permission_catalog() -> list[dict]:
    """按稳定顺序返回可独立修改的公开目录。"""
    return [
        {
            "code": code,
            "name": name,
            "description": description,
            "scopes": ["project"],
        }
        for code, name, description in _CATALOG
    ]


def normalize_project_permissions(values: object) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        raise TypeError("项目授权必须是权限编码到型号名称数组的对象。")
    result = {}
    for permission, names in values.items():
        if not isinstance(permission, str) or permission not in PROJECT_PERMISSIONS:
            raise ValueError("项目权限编码无效。")
        if not isinstance(names, list):
            raise TypeError("每项权限的型号名称必须是数组。")
        if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(
            names
        ):
            raise ValueError("同一权限的型号名称无效或重复。")
        if names:
            result[permission] = sorted(names)
    return dict(sorted(result.items()))


def parse_project_permissions(text: str, models: Collection[str]) -> dict[str, list[str]]:
    """读取权限编码到型号列表的存储格式。"""
    grants = normalize_project_permissions(decode_json(text))
    if any(name not in models for names in grants.values() for name in names):
        raise ValueError("项目授权包含不存在的项目。")
    return grants
